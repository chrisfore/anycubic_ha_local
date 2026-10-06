# tests/test_coordinator.py
import threading

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.anycubic import coordinator as coord_mod
from custom_components.anycubic.coordinator import AnycubicCoordinator
from custom_components.anycubic.anycubic_local.exceptions import CloudModeError
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult

HS = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER-1")


class FakeTransport:
    def __init__(self, hs, on_report, **k):
        self.on_report = on_report; self.queries = []; self.connected = False
    def connect(self): self.connected = True
    def disconnect(self): self.connected = False
    def query(self, t): self.queries.append(t)
    def publish(self, topic, payload): pass


async def test_coordinator_applies_info_report(hass):
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._on_report("info", {"state": "busy", "model": "Anycubic Kobra S1 Max",
                              "temp": {"curr_nozzle_temp": 210},
                              "project": {"state": "printing", "progress": 42, "pause": 0}})
    await hass.async_block_till_done()
    assert coord.data.printer.nozzle_temp == 210
    assert coord.data.printer.progress == 42
    assert coord.data.printer.printing is True


async def test_coordinator_merges_ace(hass):
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._on_report("multiColorBox", {"multi_color_box": [{"id": 0, "humidity": 24, "temp": 35, "slots": []}]})
    await hass.async_block_till_done()
    assert coord.data.ace[0].humidity == 24


async def test_tempature_push_updates_temps_without_blanking_the_rest(hass):
    # `tempature` is a raw temp dict, not an info envelope. It carries the newest
    # reading — often minutes ahead of the next `info` (issue #9) — so it must move
    # the temperature fields while leaving job state alone.
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._apply("info", {"state": "busy", "temp": {"curr_nozzle_temp": 210},
                          "project": {"state": "printing", "progress": 42, "pause": 0}})
    await hass.async_block_till_done()
    coord._apply("tempature", {"taskid": "", "curr_nozzle_temp": 230,
                               "target_nozzle_temp": 235, "curr_hotbed_temp": 60,
                               "target_hotbed_temp": 60})
    await hass.async_block_till_done()
    assert coord.data.printer.nozzle_temp == 230
    assert coord.data.printer.nozzle_target == 235
    assert coord.data.printer.bed_temp == 60
    assert coord.data.printer.progress == 42                # job state untouched
    assert coord.data.printer.printing is True


async def test_tempature_without_a_chamber_keeps_the_known_chamber_temp(hass):
    # A Kobra 3 omits the chamber keys entirely; a printer that has one reports them
    # in `info`. Folding an omitted key in as None would blank a live reading.
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._apply("info", {"state": "free",
                          "temp": {"curr_nozzle_temp": 30, "curr_chamber_temp": 41}})
    await hass.async_block_till_done()
    coord._apply("tempature", {"curr_nozzle_temp": 31})
    await hass.async_block_till_done()
    assert coord.data.printer.nozzle_temp == 31
    assert coord.data.printer.chamber_temp == 41


async def test_fan_push_updates_fan_speeds(hass):
    # Same defect as `tempature`: polled every cycle, answered, then discarded.
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._apply("info", {"state": "busy", "fan_speed_pct": 100, "aux_fan_speed_pct": 0,
                          "project": {"state": "printing", "progress": 42, "pause": 0}})
    await hass.async_block_till_done()
    coord._apply("fan", {"taskid": "-1", "fan_speed_pct": 40, "box_fan_level": 2})
    await hass.async_block_till_done()
    assert coord.data.printer.fan_speed_pct == 40
    assert coord.data.printer.box_fan_level == 2
    assert coord.data.printer.aux_fan_speed_pct == 0        # omitted, keeps its value
    assert coord.data.printer.progress == 42


async def test_a_tempature_arriving_before_any_info_is_kept(hass):
    # Reports are applied in arrival order; nothing guarantees `info` lands first.
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._apply("tempature", {"curr_nozzle_temp": 27, "curr_hotbed_temp": 24})
    await hass.async_block_till_done()
    assert coord.data.printer.nozzle_temp == 27
    assert coord.data.printer.bed_temp == 24


async def test_on_report_applies_on_the_event_loop_thread(hass):
    # Reports arrive on the paho network thread. _apply notifies listeners, which drives
    # entity state writes — and HA only allows those on the event loop. Running it
    # anywhere else (e.g. an executor thread) raises RuntimeError on every report and
    # floods the log.
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    loop_thread = threading.current_thread()
    applied_on = []
    orig_apply = coord._apply

    def spy(msg_type, data):
        applied_on.append(threading.current_thread())
        orig_apply(msg_type, data)

    coord._apply = spy
    worker = threading.Thread(target=coord._on_report, args=("info", {"state": "free"}))
    worker.start()
    worker.join()
    await hass.async_block_till_done()
    assert applied_on, "_apply never ran"
    assert applied_on[0] is loop_thread
    assert coord.data.printer is not None


async def test_coordinator_queries_peripherie_once_at_connect(hass):
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    # peripherie (the capability inventory) is asked for at connect, alongside the normal poll types.
    assert "peripherie" in coord._transport.queries
    assert "info" in coord._transport.queries


async def test_coordinator_captures_capabilities(hass):
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._on_report("info", {"state": "free", "model": "Anycubic Kobra S1 Max",
                              "temp": {"curr_chamber_temp": 36},
                              "features": {"camera_timelapse_support": True, "fod_support": True}})
    coord._on_report("peripherie", {"camera": 1, "multiColorBox": 1, "udisk": 0})
    await hass.async_block_till_done()
    # The raw feature map and peripheral inventory are stashed verbatim for diagnostics / onboarding.
    assert coord.raw_features == {"camera_timelapse_support": True, "fod_support": True}
    assert coord.peripherie == {"camera": 1, "multiColorBox": 1, "udisk": 0}
    assert {"info", "peripherie"} <= coord.seen_report_types


# ------------------------------------------------- dead-session detection (issue #9)
#
# The reported symptom: entities freeze at their last values while the integration
# still reports update_success: true. Every test below asserts on the RECOVERY, not
# just on a flag, because a flag nobody acts on is what the bug was.


def _go_silent(coord):
    """Wind the last-report clock past the watchdog window."""
    coord._last_report -= coord_mod.STALE_AFTER + 1


def _handshakes(monkeypatch, result=HS, error=None):
    calls = []

    def fake(host, *a, **k):
        calls.append(host)
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(coord_mod, "do_handshake", fake)
    return calls


async def test_a_healthy_poll_neither_recovers_nor_fails(hass, monkeypatch):
    calls = _handshakes(monkeypatch)
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    await coord._async_update_data()
    assert calls == [], "re-handshaked a printer that was answering fine"
    assert "info" in coord._transport.queries


async def test_a_silent_printer_is_re_handshaked(hass, monkeypatch):
    """The failure that produced issue #9: paho still thinks it is connected, so
    nothing raises — the printer has simply stopped answering."""
    calls = _handshakes(monkeypatch)
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    first = coord._transport
    _go_silent(coord)
    await coord._async_update_data()
    assert calls == ["1.2.3.4"], "silence did not trigger a re-handshake"
    assert coord._transport is not first, "transport was not rebuilt"
    assert first.connected is False, "the dead session was left open"


async def test_a_dropped_connection_is_re_handshaked(hass, monkeypatch):
    calls = _handshakes(monkeypatch)
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._transport.connected = False           # refused CONNACK / dropped socket
    await coord._async_update_data()
    assert calls == ["1.2.3.4"]


async def test_a_report_clears_the_watchdog(hass, monkeypatch):
    calls = _handshakes(monkeypatch)
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    _go_silent(coord)
    coord._apply("info", {"state": "free"})      # the printer answered after all
    await hass.async_block_till_done()
    await coord._async_update_data()
    assert calls == [], "recovered despite a fresh report"


async def test_entities_stay_available_while_recovery_is_working(hass, monkeypatch):
    """A rebuild that succeeds must not flap entities to unavailable — a printer
    that is merely quiet for a couple of minutes is not an outage."""
    _handshakes(monkeypatch)
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    _go_silent(coord)
    await coord._async_update_data()             # attempt 1, no UpdateFailed


async def test_repeated_silence_finally_marks_the_printer_unavailable(hass, monkeypatch):
    """Recovering quietly is right; hiding a printer we genuinely cannot reach is not."""
    _handshakes(monkeypatch)
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    for _ in range(coord_mod.MAX_RECOVERIES_BEFORE_UNAVAILABLE):
        _go_silent(coord)
        await coord._async_update_data()         # rebuilds, still no reports
    _go_silent(coord)
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_a_failing_handshake_marks_the_printer_unavailable(hass, monkeypatch):
    _handshakes(monkeypatch, error=OSError("no route to host"))
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    _go_silent(coord)
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_a_printer_that_comes_back_recovers_on_its_own(hass, monkeypatch):
    """Giving up permanently would leave a returning printer dead until someone
    reloaded the integration by hand, so recovery is retried every cycle."""
    calls = _handshakes(monkeypatch, error=OSError("no route to host"))
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    _go_silent(coord)
    for _ in range(4):                           # printer off the network
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
    assert len(calls) == 4, "stopped trying to reach the printer"

    _handshakes(monkeypatch)                     # printer back on the network
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()         # reconnected, but nothing has answered
    assert coord._transport is not None, "did not rebuild once the printer returned"
    coord._apply("info", {"state": "free"})      # ...and now it answers
    await hass.async_block_till_done()
    assert coord._recoveries == 0, "still flagged as recovering after a report"
    await coord._async_update_data()             # available again, no UpdateFailed


async def test_lan_mode_turned_off_during_recovery_asks_for_reauth(hass, monkeypatch):
    """Same path as setup: CloudModeError is a user action, not an outage."""
    from homeassistant.exceptions import ConfigEntryAuthFailed
    _handshakes(monkeypatch, error=CloudModeError("LAN Mode off"))
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    _go_silent(coord)
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_recovery_refuses_to_adopt_a_different_printer(hass, monkeypatch):
    """The address can be reassigned by DHCP. Rebuilding blindly would silently
    repoint every entity at someone else's machine."""
    other = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV2", "20029", "SER-2")
    _handshakes(monkeypatch, result=other)
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    _go_silent(coord)
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord.hs.serial == "SER-1", "entities were repointed at another printer"


async def test_the_different_printer_error_names_neither_address_nor_serial(hass, monkeypatch):
    """An UpdateFailed lands in Home Assistant's ordinary ERROR log, which gets pasted into
    issues with no debug logging switched on at all. The serial in it would not even be the
    reporter's own printer."""
    mine = HandshakeResult("192.168.1.50", 9883, "u", "p", "DEV", "20029", "SERIAL-TEST-0001")
    other = HandshakeResult("192.168.1.50", 9883, "u", "p", "DEV2", "20029", "SERIAL-TEST-0002")
    _handshakes(monkeypatch, result=other)
    coord = AnycubicCoordinator(hass, mine, host="192.168.1.50", transport_factory=FakeTransport)
    await coord.async_start()
    _go_silent(coord)
    with pytest.raises(UpdateFailed) as err:
        await coord._async_update_data()
    text = str(err.value)
    assert "different printer" in text          # still says what happened
    assert "192.168.1.50" not in text
    assert "SERIAL-TEST-0002" not in text
    assert "SERIAL-TEST-0001" not in text


async def test_video_report_url_is_captured(hass):
    """New-generation firmware (Kobra 4 / X) answers startCapture with a video report
    whose data carries the tokenized stream URL. It must be kept in its own field —
    the next info report rebuilds PrinterState and would wipe it."""
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._on_report("video", {"urls": {"rtspUrl": "http://1.2.3.4:18088/live/k5DawnaQ"}})
    await hass.async_block_till_done()
    assert coord.video_stream_url == "http://1.2.3.4:18088/live/k5DawnaQ"
    coord._on_report("info", {"state": "free", "model": "Anycubic Kobra 4"})
    await hass.async_block_till_done()
    assert coord.video_stream_url == "http://1.2.3.4:18088/live/k5DawnaQ"


async def test_print_progress_push_updates_the_job_without_touching_lifecycle(hass):
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._apply("info", {"state": "busy", "temp": {"curr_nozzle_temp": 210},
                          "project": {"state": "printing", "progress": 5,
                                      "curr_layer": 1, "pause": 0}})
    await hass.async_block_till_done()
    coord._apply("print", {"taskid": "-1", "progress": 42, "curr_layer": 120,
                           "total_layers": 900, "remain_time": 31})
    await hass.async_block_till_done()
    assert coord.data.printer.progress == 42
    assert coord.data.printer.current_layer == 120
    assert coord.data.printer.remain_time == 31
    assert coord.data.printer.printing is True
    assert coord.data.printer.nozzle_temp == 210


async def test_a_print_command_ack_does_not_wipe_progress(hass):
    # Acks for pause/resume/settings arrive on the same `print` topic as progress.
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._apply("print", {"taskid": "-1", "progress": 42, "curr_layer": 120})
    await hass.async_block_till_done()
    coord._apply("print", {"taskid": "-1", "settings": {"target_nozzle_temp": 210}})
    await hass.async_block_till_done()
    assert coord.data.printer.progress == 42
    assert coord.data.printer.current_layer == 120


async def test_a_report_does_not_push_out_the_next_poll(hass):
    # multiColorBox is poll-ONLY (the printer never pushes it during a steady print), so
    # the 30s poll must keep its cadence no matter how chatty the pushed types are.
    # async_set_updated_data re-arms the refresh timer on every call, which starved the
    # poll completely while info/tempature/fan were arriving every few seconds.
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    coord.async_add_listener(lambda: None)      # scheduling only happens with listeners
    await coord.async_start()
    await coord.async_refresh()
    # HA stores the poll timer as TimerHandle.cancel, so __self__ is the handle itself.
    # A reschedule replaces the handle; keeping the same one is the assertion.
    handle = coord._unsub_refresh.__self__
    scheduled_at = handle.when()
    for _ in range(5):
        coord._apply("tempature", {"curr_nozzle_temp": 210})
        coord._apply("fan", {"fan_speed_pct": 100})
        coord._apply("info", {"state": "free", "temp": {"curr_nozzle_temp": 210}})
    await hass.async_block_till_done()
    assert coord._unsub_refresh.__self__ is handle
    assert coord._unsub_refresh.__self__.when() == scheduled_at


async def test_a_report_still_brings_entities_back_from_unavailable(hass):
    # The watchdog marks the coordinator failed; the next report must clear that, which
    # is what async_set_updated_data used to do via last_update_success.
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord.last_update_success = False
    updated = []
    coord.async_add_listener(lambda: updated.append(1))
    coord._apply("tempature", {"curr_nozzle_temp": 210})
    await hass.async_block_till_done()
    assert coord.last_update_success is True
    assert updated                                # listeners were notified


class RecordingTransport(FakeTransport):
    """Captures publishes as well as queries, so probes can be asserted on."""
    def __init__(self, hs, on_report, **k):
        super().__init__(hs, on_report, **k)
        self.published = []
    def publish(self, topic, payload):
        import json
        self.published.append((topic.rsplit("/", 1)[-1], json.loads(payload)))


async def test_extfilbox_is_probed_at_connect_not_polled(hass):
    # Issue #12: a polled extfilbox with action "query" is answered by nothing at all
    # (confirmed on a Kobra 3 V2 with a spool loaded and no ACE). multiColorBox needs
    # "getInfo", and the push carries "reportInfo" — so try both, once, at connect.
    coord = AnycubicCoordinator(hass, HS, transport_factory=RecordingTransport)
    await coord.async_start()

    assert "extfilbox" not in coord._transport.queries    # no longer wasted per poll
    actions = [p["action"] for t, p in coord._transport.published if t == "extfilbox"]
    assert actions == ["reportInfo"]   # the one the printer actually answers


async def test_file_details_is_requested_once_per_job(hass):
    # Issue #13: the printer never pushes `file` on its own — it only answers the Slicer.
    # So ask for it ourselves, when a print report first names a file, and only once.
    coord = AnycubicCoordinator(hass, HS, transport_factory=RecordingTransport)
    await coord.async_start()
    coord._transport.published.clear()

    coord._on_report("print", {"taskid": "-1", "progress": 5, "filename": "boat.gcode"})
    await hass.async_block_till_done()
    coord._on_report("print", {"taskid": "-1", "progress": 6, "filename": "boat.gcode"})
    await hass.async_block_till_done()

    asks = [p for t, p in coord._transport.published if t == "file"]
    assert len(asks) == 1
    assert asks[0]["action"] == "fileDetails"
    assert asks[0]["data"]["filename"] == "boat.gcode"

    # A new job asks again.
    coord._on_report("print", {"taskid": "-1", "progress": 1, "filename": "benchy.gcode"})
    await hass.async_block_till_done()
    assert len([p for t, p in coord._transport.published if t == "file"]) == 2


async def test_file_details_asks_with_the_raw_prefixed_filename(hass):
    # The display name is tidied, but the request must carry what the printer called it:
    # printer-started jobs confirmed working on the wire with the ".3mf_temp/" prefix intact.
    coord = AnycubicCoordinator(hass, HS, transport_factory=RecordingTransport)
    await coord.async_start()
    coord._transport.published.clear()

    raw = ".3mf_temp/0907-2001-Plant wall clip_plate(01)_PLA_0.28_5m39s.gcode"
    coord._on_report("print", {"taskid": "-1", "progress": 3, "filename": raw})
    await hass.async_block_till_done()

    asks = [p for t, p in coord._transport.published if t == "file"]
    assert len(asks) == 1
    assert asks[0]["data"]["filename"] == raw


def _details(filename="boat.gcode"):
    import json
    import pathlib
    d = json.loads((pathlib.Path(__file__).parent / "fixtures" / "file_details.json").read_text())
    d["filename"] = filename
    return d


async def test_file_details_response_populates_object_images(hass):
    coord = AnycubicCoordinator(hass, HS, transport_factory=RecordingTransport)
    await coord.async_start()
    coord._on_report("print", {"taskid": "-1", "progress": 5, "filename": "boat.gcode"})
    await hass.async_block_till_done()

    coord._on_report("file", _details("boat.gcode"))
    await hass.async_block_till_done()

    assert coord.data.object_images.thumbnail.startswith(b"\x89PNG")
    assert coord.data.object_images.top_view.startswith(b"\x89PNG")


async def test_file_report_that_is_not_details_is_ignored(hass):
    # listLocal shares the `file` topic and carries no renders.
    coord = AnycubicCoordinator(hass, HS, transport_factory=RecordingTransport)
    await coord.async_start()
    coord._on_report("file", {"root": "local", "records": [{"name": "a.gcode"}]})
    await hass.async_block_till_done()
    assert coord.data.object_images is None


async def test_late_answer_for_the_previous_job_is_discarded(hass):
    # A slow fileDetails answer must not paint the last print's object over the new one.
    coord = AnycubicCoordinator(hass, HS, transport_factory=RecordingTransport)
    await coord.async_start()
    coord._on_report("print", {"taskid": "-1", "progress": 5, "filename": "boat.gcode"})
    await hass.async_block_till_done()
    coord._on_report("file", _details("boat.gcode"))
    await hass.async_block_till_done()

    coord._on_report("print", {"taskid": "-1", "progress": 1, "filename": "benchy.gcode"})
    coord._on_report("file", _details("benchy.gcode"))
    await hass.async_block_till_done()
    assert coord.data.object_images.filename == "benchy.gcode"

    coord._on_report("file", _details("boat.gcode"))     # stale answer arrives late
    await hass.async_block_till_done()
    assert coord.data.object_images.filename == "benchy.gcode"   # must not be painted over


# ------------------------------------------------- what an error says in the ordinary log
#
# UpdateFailed and ConfigEntryNotReady are logged by Home Assistant at its normal level, with
# no debug logging on. Their text used to be the exception's own, and an exception says
# whatever the library, or the device that answered, put in it.

async def test_a_failed_reconnect_does_not_put_the_address_in_the_error(hass, monkeypatch):
    mine = HandshakeResult("192.168.1.50", 9883, "u", "p", "0123456789abcdef0123456789abcdef",
                           "20029", "SERIAL-TEST-0001", mac="AA-BB-CC-DD-EE-FF")
    _handshakes(monkeypatch, error=OSError(
        "192.168.1.50 unreachable (kobra-s1.local, SERIAL-TEST-0001, aa:bb:cc:dd:ee:ff, "
        "0123456789abcdef0123456789abcdef)"))
    coord = AnycubicCoordinator(hass, mine, host="kobra-s1.local", transport_factory=FakeTransport)
    await coord.async_start()
    _go_silent(coord)
    with pytest.raises(UpdateFailed) as err:
        await coord._async_update_data()
    text = str(err.value)
    assert text.startswith("reconnect failed: ") and "unreachable" in text   # still says what
    for secret in ("192.168.1.50", "kobra-s1.local", "0123456789abcdef0123456789abcdef",
                   "SERIAL-TEST-0001", "aa:bb:cc:dd:ee:ff"):
        assert secret not in text, secret


async def test_a_short_hostname_the_redactor_leaves_alone_still_stays_out_of_the_error(hass, monkeypatch):
    # A one-word name is not scrubbed out of printer payloads (it is an ordinary word, and
    # under seven characters nothing is). An error is not a payload: there the word is the
    # address, and it is masked wherever it stands on its own.
    mine = HandshakeResult("192.168.1.50", 9883, "u", "p", "0123456789abcdef0123456789abcdef",
                           "20029", "SERIAL-TEST-0001")
    for host, said, logged in (
        ("kobra", "cannot resolve kobra: Name or service not known",
         "reconnect failed: cannot resolve **REDACTED**: Name or service not known"),
        ("anycubic", "<urlopen error http://Anycubic:18910 refused>",
         "reconnect failed: <urlopen error http://**REDACTED**:18910 refused>"),
        # Inside another word it is not the address, and it cannot be cut out of one. The
        # text would still hold it, so the kind of error is all that is said.
        ("pi", "session expired", "reconnect failed: OSError"),
        ("pi", "", "reconnect failed: OSError"),
    ):
        _handshakes(monkeypatch, error=OSError(said))
        coord = AnycubicCoordinator(hass, mine, host=host, transport_factory=FakeTransport)
        await coord.async_start()
        _go_silent(coord)
        with pytest.raises(UpdateFailed) as err:
            await coord._async_update_data()
        assert str(err.value) == logged, host


async def test_an_ordinary_reconnect_error_keeps_its_text(hass, monkeypatch):
    # "Connection refused" against "timed out" is most of what a log is read for.
    _handshakes(monkeypatch, error=OSError(111, "Connection refused"))
    coord = AnycubicCoordinator(hass, HS, host="192.168.1.50", transport_factory=FakeTransport)
    await coord.async_start()
    _go_silent(coord)
    with pytest.raises(UpdateFailed) as err:
        await coord._async_update_data()
    assert str(err.value) == "reconnect failed: [Errno 111] Connection refused"


async def test_a_failed_setup_does_not_put_the_address_in_the_log(hass, caplog):
    from unittest.mock import patch

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.anycubic.anycubic_local.exceptions import HandshakeError
    from custom_components.anycubic.const import DOMAIN

    for error in (OSError("<urlopen error connect to 192.168.1.50:18910 failed: No route to host>"),
                  HandshakeError("/ctrl failed: device 192.168.1.50 is busy")):
        entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "192.168.1.50"})
        entry.add_to_hass(hass)
        with patch("custom_components.anycubic.do_handshake", side_effect=error):
            assert not await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        assert entry.reason and "192.168.1.50" not in entry.reason
        assert "No route to host" in entry.reason or "is busy" in entry.reason
        await hass.config_entries.async_remove(entry.entry_id)
    assert "192.168.1.50" not in caplog.text
    assert "not ready yet" in caplog.text           # the line this is about was written
