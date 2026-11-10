"""Bed needs clearing: on after any job and any gap, kept across restarts, off only by hand."""
import time
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic.anycubic_local.handshake import HandshakeResult
from custom_components.anycubic.const import DOMAIN, REPORT_GAP_BOUND
from custom_components.anycubic.store import STORAGE_VERSION, LastFile, PrintState

HS = HandshakeResult("192.168.1.50", 9883, "u", "p", "DEVICE-ID-0001", "20029", "SER-1")
ENTRY_ID = "printerentry0001"
KEY = f"{DOMAIN}.print.{ENTRY_ID}"
FLAG = "binary_sensor.anycubic_kobra_s1_max_bed_needs_clearing"
CLEAR = "button.anycubic_kobra_s1_max_bed_is_clear"


class Transport:
    def __init__(self, hs, on_report, **kwargs): self.published = []
    def connect(self): pass
    def disconnect(self): pass
    def query(self, msg_type): pass
    def publish(self, topic, payload): self.published.append(topic)


def _patches():
    return (patch("custom_components.anycubic.do_handshake", return_value=HS),
            patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", Transport))


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "printer.example"},
                            entry_id=ENTRY_ID)
    entry.add_to_hass(hass)
    handshake, transport = _patches()
    with handshake, transport:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _stored(hass_storage, data):
    hass_storage[KEY] = {"version": STORAGE_VERSION, "minor_version": 1, "key": KEY, "data": data}


def _idle(coord):
    coord._apply("info", {"state": "free", "project": None})


def _clear(coord):
    _idle(coord)
    coord.async_mark_bed_clear()
    assert not coord.bed_needs_clearing


# ------------------------------------------------- what was remembered


async def test_with_nothing_on_record_the_bed_needs_clearing(hass):
    entry = await _setup(hass)
    assert entry.runtime_data.bed_needs_clearing
    assert hass.states.get(FLAG).state == "on"


LAST_FILE = {"name": "a.gcode", "size": 10, "md5": "0" * 32, "filetype": 1,
             "used_slots": [1], "materials": {"1": "PLA"}, "colors": {"1": "#FF0000"},
             "model_named": True}


@pytest.mark.parametrize("data", [
    # What an earlier build stored: a clear bed and a report a moment ago. A restart is a
    # gap all the same; the host's clock may be wrong until it syncs.
    {"bed_flag": False, "last_report": time.time() - 1, "last_file": LAST_FILE},
    {"bed_flag": False, "last_report": time.time() + 3600},
    {"bed_flag": False, "last_report": "nan"},
    {"last_file": LAST_FILE},
    {"last_file": {**LAST_FILE, "md5": 5}},
    "not a dict",
], ids=["clear-and-recent", "future", "nan", "file-only", "garbled-file", "garbage"])
async def test_every_load_starts_with_the_bed_needing_clearing(hass, hass_storage, data):
    _stored(hass_storage, data)
    entry = await _setup(hass)
    assert entry.runtime_data.bed_needs_clearing
    assert hass.states.get(FLAG).state == "on"


async def test_the_last_file_on_record_is_taken_up(hass, hass_storage):
    _stored(hass_storage, {"last_file": LAST_FILE})
    entry = await _setup(hass)
    assert entry.runtime_data.last_file == LastFile.from_dict(LAST_FILE)


async def test_reports_and_polls_write_nothing(hass, hass_storage):
    entry = await _setup(hass)
    coord = entry.runtime_data
    coord._print_store.async_schedule_save = MagicMock()
    _clear(coord)
    for _ in range(3):
        _idle(coord)
        await coord._async_update_data()
    coord._apply("info", {"state": "busy", "project": {"state": "printing", "pause": 0}})
    await hass.async_block_till_done()
    coord._print_store.async_schedule_save.assert_not_called()
    assert KEY not in hass_storage


def test_a_garbled_last_file_record_is_dropped():
    good = LAST_FILE
    assert LastFile.from_dict(good) == LastFile("a.gcode", 10, "0" * 32, 1, (1,), {1: "PLA"},
                                                {1: "#FF0000"}, True)
    for change in ({"size": "nan"}, {"size": 0}, {"md5": "xyz"}, {"name": ""},
                   {"used_slots": [0]}, {"used_slots": ["inf"]}, {"used_slots": []},
                   {"materials": {"x": "PLA"}}, {"colors": {"1": "red"}}, {"filetype": True}):
        assert LastFile.from_dict({**good, **change}) is None, change
    state = PrintState.from_dict({"bed_flag": False, "last_report": 1.0,
                                  "last_file": {**good, "md5": 5}})
    assert state.last_file is None and state.bed_flag is True
    # The last file and the prepared print being waited for are written; the flag never.
    assert PrintState(bed_flag=False, last_file=LastFile.from_dict(good)).as_dict() == {
        "last_file": LastFile.from_dict(good).as_dict(), "armed": None}


# ------------------------------------------------- what turns it on


@pytest.mark.parametrize(("msg_type", "report"), [
    ("info", {"state": "busy", "project": {"state": "printing", "pause": 0}}),
    ("info", {"state": "busy", "project": {"state": "preheating"}}),
    ("info", {"state": "busy", "project": {"state": "auto_leveling"}}),
    ("info", {"state": "busy", "project": {"state": "paused", "pause": 1}}),
    ("info", {"state": "busy", "project": {"state": "finished"}}),
    ("info", {"state": "busy", "project": {"state": "stoped"}}),
    ("print", {"taskid": "-1", "progress": 3, "filename": "someone_elses.gcode"}),
    # As seen live after a refused start, while a real job ran on.
    ("info", {"state": "busy", "project": None, "last_project": {"state": "failed"}}),
    ("info", {"state": "busy", "project": {"state": "failed"}}),
    ("info", {"state": "busy", "project": None}),                  # busy, no print
    ("info", {"state": "free", "project": {"state": "something new"}}),
], ids=["printing", "preheating", "leveling", "paused", "finished", "stopped", "progress",
        "busy-no-job-last-failed", "busy-failed", "busy-no-job", "free-with-a-job"])
async def test_a_job_from_any_source_or_a_busy_printer_turns_it_on(hass, msg_type, report):
    coord = (await _setup(hass)).runtime_data
    _clear(coord)
    coord._apply(msg_type, report)
    await hass.async_block_till_done()
    assert coord.bed_needs_clearing
    assert hass.states.get(FLAG).state == "on"


async def test_a_free_printer_leaves_it_off_whatever_its_last_job_was(hass):
    coord = (await _setup(hass)).runtime_data
    _clear(coord)
    for msg_type, report in (("info", {"state": "free", "project": None}),
                             # The previous job, already seen: or the flag never clears.
                             ("info", {"state": "free", "project": None,
                                       "last_project": {"state": "finished"}}),
                             ("info", {"state": "free", "project": None,
                                       "last_project": {"state": "failed"}}),
                             ("tempature", {"curr_hotbed_temp": 30}),
                             ("multiColorBox", {"multi_color_box": []}),
                             ("print", {"taskid": "-1"})):           # an answer, not progress
        coord._apply(msg_type, report)
    assert not coord.bed_needs_clearing


async def test_a_gap_seen_by_the_poll_turns_it_on(hass):
    coord = (await _setup(hass)).runtime_data
    _clear(coord)
    await coord._async_update_data()
    assert not coord.bed_needs_clearing, "a poll within the bound is not a gap"
    coord._report_at -= REPORT_GAP_BOUND + 1
    await coord._async_update_data()
    assert coord.bed_needs_clearing


async def test_a_report_after_a_gap_turns_it_on(hass):
    coord = (await _setup(hass)).runtime_data
    _clear(coord)
    coord._report_at -= REPORT_GAP_BOUND + 1
    _idle(coord)
    assert coord.bed_needs_clearing


async def test_a_reconnect_does_not_hide_a_gap(hass, monkeypatch):
    """The session watchdog winds its own clock forward when it reconnects; the gap is
    judged on a clock it does not touch."""
    from custom_components.anycubic import coordinator as coord_mod

    coord = (await _setup(hass)).runtime_data
    _clear(coord)
    monkeypatch.setattr(coord_mod, "do_handshake", lambda host: HS)
    coord._report_at -= coord_mod.STALE_AFTER + 1
    coord._last_report -= coord_mod.STALE_AFTER + 1
    await coord._async_update_data()
    assert coord.bed_needs_clearing


# ------------------------------------------------- what turns it off


async def test_only_bed_is_clear_turns_it_off(hass):
    coord = (await _setup(hass)).runtime_data
    _idle(coord)
    for _ in range(3):
        _idle(coord)
        await coord._async_update_data()
    assert coord.bed_needs_clearing, "idle reports and polls do not clear it"
    await hass.async_block_till_done()
    await hass.services.async_call("button", "press", {"entity_id": CLEAR}, blocking=True)
    assert not coord.bed_needs_clearing
    await hass.async_block_till_done()
    assert hass.states.get(FLAG).state == "off"


async def test_bed_is_clear_is_unavailable_during_a_job_and_while_unreachable(hass):
    coord = (await _setup(hass)).runtime_data
    _idle(coord)
    await hass.async_block_till_done()
    assert hass.states.get(CLEAR).state != "unavailable"
    coord._apply("info", {"state": "busy", "project": {"state": "printing", "pause": 0}})
    await hass.async_block_till_done()
    assert hass.states.get(CLEAR).state == "unavailable"
    with pytest.raises(ServiceValidationError) as err:
        coord.async_mark_bed_clear()
    assert err.value.translation_key == "clear_while_busy"
    assert coord.bed_needs_clearing
    _idle(coord)
    coord._report_at -= REPORT_GAP_BOUND + 1
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(CLEAR).state == "unavailable"
    with pytest.raises(ServiceValidationError):
        coord.async_mark_bed_clear()


# ------------------------------------------------- kept, and taken away with the printer


async def test_a_reload_keeps_the_last_file_and_sets_the_flag(hass, hass_storage):
    entry = await _setup(hass)
    record = LastFile.from_dict(LAST_FILE)
    entry.runtime_data.async_set_last_file(record)
    _clear(entry.runtime_data)
    handshake, transport = _patches()
    with handshake, transport:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert hass_storage[KEY]["data"] == {"last_file": record.as_dict(), "armed": None}
    assert entry.runtime_data.last_file == record
    assert entry.runtime_data.bed_needs_clearing, "every load is a gap"


async def test_removing_the_printer_removes_its_record(hass, hass_storage):
    entry = await _setup(hass)
    _clear(entry.runtime_data)
    await hass.config_entries.async_unload(entry.entry_id)
    assert KEY in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert KEY not in hass_storage
