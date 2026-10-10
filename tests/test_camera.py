import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic.const import DOMAIN
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult

HS = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER-1")


class FakeTransport:
    def __init__(self, hs, on_report, **k): pass
    def connect(self): pass
    def disconnect(self): pass
    def query(self, t): pass
    def publish(self, t, p): pass


@pytest.fixture(autouse=True)
def _fast_capture_kick():
    """The capture kick's real pauses (1s + 4s report wait) would dominate every test."""
    with patch("custom_components.anycubic.coordinator.VIDEO_KICK_DELAY", 0), \
         patch("custom_components.anycubic.coordinator.VIDEO_REPORT_TIMEOUT", 0.05):
        yield


async def test_camera_stream_source_starts_capture(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data

    from custom_components.anycubic.camera import AnycubicCamera
    cam = AnycubicCamera(coord)
    coord.async_send_command = AsyncMock()
    url = await cam.stream_source()
    assert url == "http://1.2.3.4:18088/flv"
    coord.async_send_command.assert_awaited_with("camera_start")


async def test_camera_uses_entered_hostname(hass):
    """When the user enters a DNS/mDNS name, the camera URL uses that name (resolved by
    the OS), not the printer-reported broker IP."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "printer.local"})
    entry.add_to_hass(hass)
    # broker reports a bare IP; the user typed a name -> the name must win for HTTP URLs.
    hs = HandshakeResult("10.0.0.5", 9883, "u", "p", "DEV", "20029", "SER-1")
    with patch("custom_components.anycubic.do_handshake", return_value=hs), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data

    from custom_components.anycubic.camera import AnycubicCamera
    cam = AnycubicCamera(coord)
    coord.async_send_command = AsyncMock()
    assert await cam.stream_source() == "http://printer.local:18088/flv"


async def test_setup_with_webrtc_provider_does_not_start_capture(hass):
    """HA probes every stream-capable camera for WebRTC provider support when the
    entity is added (async_refresh_providers -> stream_source). stream_source()
    commands camera_start, and the printer firmware switches the chamber LED on
    with capture — so with a provider registered (go2rtc ships with HA), every HA
    start / entry reload lit the chamber. Setup must publish no video command."""
    from homeassistant.components.camera.webrtc import (
        CameraWebRTCProvider,
        async_register_webrtc_provider,
    )
    from homeassistant.setup import async_setup_component

    class DummyProvider(CameraWebRTCProvider):
        @property
        def domain(self):
            return "dummy"

        def async_is_supported(self, stream_source):
            return True

        async def async_handle_async_webrtc_offer(self, camera, offer_sdp, session_id,
                                                  send_message):
            pass

        async def async_on_webrtc_candidate(self, session_id, candidate):
            pass

    assert await async_setup_component(hass, "camera", {})
    async_register_webrtc_provider(hass, DummyProvider())
    await hass.async_block_till_done()

    published = []

    class RecordingTransport(FakeTransport):
        def publish(self, t, p):
            published.append(t)

    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", RecordingTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    video_cmds = [t for t in published if t.rsplit("/", 1)[-1] == "video"]
    assert not video_cmds, f"setup commanded the printer camera: {video_cmds}"


async def test_stream_source_prefers_printer_reported_url(hass):
    """The printer self-reports its camera URL in the info report (urls.rtspUrl).
    Prefer it over the hardcoded :18088/flv — an unvalidated model may serve its
    stream on a different scheme/port/path (issue #6, Kobra 4 "no feed"). The
    host is swapped for the user-entered one (mDNS names must keep winning)."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "printer.local"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("info", {"model": "Kobra 4", "state": "free",
                              "urls": {"rtspUrl": "rtsp://10.0.0.5:8554/streaming/live/1"}})
        await hass.async_block_till_done()

    from custom_components.anycubic.camera import AnycubicCamera
    cam = AnycubicCamera(coord)
    coord.async_send_command = AsyncMock()
    assert await cam.stream_source() == "rtsp://printer.local:8554/streaming/live/1"
    coord.async_send_command.assert_awaited_with("camera_start")


async def test_stream_source_kicks_capture_and_uses_video_report_url(hass):
    """New-generation firmware (Kobra 4 / X): the official client always sends
    stopCapture, waits, then startCapture (a bare start doesn't begin pushing),
    and the startCapture answer carries the tokenized stream URL. stream_source
    must follow that flow and hand out the fresh URL, host-swapped."""
    import json as _json

    published = []

    class KickTransport(FakeTransport):
        def __init__(self, hs, on_report, **k):
            self.on_report = on_report
        def publish(self, t, p):
            published.append((t, _json.loads(p)))
            if _json.loads(p).get("action") == "startCapture":
                self.on_report("video", {"urls": {"rtspUrl": "http://10.0.0.9:18088/live/k5DawnaQ"}})

    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "printer.local"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", KickTransport), \
         patch("custom_components.anycubic.coordinator.VIDEO_KICK_DELAY", 0):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data

        from custom_components.anycubic.camera import AnycubicCamera
        cam = AnycubicCamera(coord)
        url = await cam.stream_source()

    assert url == "http://printer.local:18088/live/k5DawnaQ"
    video_actions = [p["action"] for t, p in published if t.rsplit("/", 1)[-1] == "video"]
    assert video_actions == ["stopCapture", "startCapture"]


# ------------------------------------------------- what is taken from a reported URL
#
# Only the path and port are taken from the printer; the host is always the configured one.


async def _source_for(hass, reported, host="printer.local"):
    """stream_source() for a printer whose startCapture answer carried `reported`."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": host})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data

    from custom_components.anycubic.camera import AnycubicCamera
    cam = AnycubicCamera(coord)
    coord.async_send_command = AsyncMock()
    coord.video_stream_url = reported
    return await cam.stream_source()


async def test_a_reported_host_in_another_case_is_still_replaced(hass):
    """A host name reads the same in any case, and the reported one is not ours to keep."""
    assert await _source_for(hass, "http://PRINTER-Cam.Example:18088/flv") == \
        "http://printer.local:18088/flv"


async def test_reported_port_and_tokenised_path_are_kept_as_reported(hass):
    assert await _source_for(hass, "http://192.0.2.9:28088/live/k5DawnaQ?session=Ab3") == \
        "http://printer.local:28088/live/k5DawnaQ?session=Ab3"
    assert await _source_for(hass, "RTSP://192.0.2.9/streaming/live/1") == \
        "rtsp://printer.local/streaming/live/1"


async def test_user_information_in_a_reported_url_is_not_passed_on(hass):
    assert await _source_for(hass, "http://name:word@192.0.2.9:18088/live/k5DawnaQ") == \
        "http://printer.local:18088/live/k5DawnaQ"


@pytest.mark.parametrize("reported", [
    "file:///live/k5DawnaQ",
    "file://192.0.2.9/live/k5DawnaQ",
    "ftp://192.0.2.9:18088/flv",
    "http://192.0.2.9:port/flv",                 # a port that is not a number
    "http://192.0.2.9:99999/flv",                # or not a port
    "http://[192.0.2.9/flv",                     # cannot be parsed at all
    "//192.0.2.9:18088/flv",                     # no scheme
    "http:///flv",                               # no host
])
async def test_a_reported_url_that_is_not_a_stream_address_falls_back_to_the_default(
        hass, reported):
    """http, https and rtsp are what a printer serves its stream on. Anything else, or
    anything that does not read as an address, is not used at all."""
    assert await _source_for(hass, reported) == "http://printer.local:18088/flv"


async def test_a_configured_ipv6_address_is_bracketed(hass):
    assert await _source_for(hass, "http://192.0.2.9:18088/live/k5DawnaQ",
                             host="2001:db8::10") == "http://[2001:db8::10]:18088/live/k5DawnaQ"
    assert await _source_for(hass, "rtsp://192.0.2.9/streaming/live/1",
                             host="[2001:db8::10]") == "rtsp://[2001:db8::10]/streaming/live/1"


# ------------------------------------------------- capture re-kick (issue #15)
#
# Home Assistant asks stream_source() once per camera entity and keeps the Stream it
# built from the answer for as long as the entity lives. The capture kick lived only
# in stream_source(), so once the printer stopped capturing (a power cycle) nothing
# ever started it again and the card stayed blank. Everything below drives the entity
# Home Assistant really holds, through the same calls its `camera/stream` command makes.


class FakeStream:
    """The parts of Home Assistant's Stream the camera uses, minus the worker thread."""

    def __init__(self, source, preload=False):
        from types import SimpleNamespace
        self.source = source
        self.available = True
        self.access_token = None
        self.dynamic_stream_settings = SimpleNamespace(preload_stream=preload)
        self._outputs = {}
        self._callback = None
        self.log = []                            # what was done to it, in order

    def outputs(self):
        return dict(self._outputs)

    def set_update_callback(self, update_callback):
        self._callback = update_callback

    def add_provider(self, fmt, timeout=None):
        self.log.append(f"add:{fmt}")
        return self._outputs.setdefault(fmt, type("Output", (), {"name": fmt})())

    async def remove_provider(self, provider):
        self._outputs.pop(provider.name, None)
        if not self._outputs:
            await self.stop()

    async def start(self):
        self.log.append(f"start:{self.source}")

    async def stop(self):
        self.log.append("stop")
        self._outputs = {}
        self.access_token = None

    def update_source(self, new_source):         # must never be used: see the real-Stream test
        raise AssertionError("update_source ends a worker that is waiting to retry")

    # -- what the worker thread would do
    def play(self, fmt="hls"):
        """A card asked for the stream: the provider is added and the worker started."""
        self.add_provider(fmt)
        self.access_token = "token-a-card-holds"
        self.set_state(True)

    def set_state(self, available):
        self.available = available
        if self._callback:
            self._callback()


class CaptureTransport(FakeTransport):
    """Records the video commands and answers startCapture the way an S1 does."""

    def __init__(self, hs, on_report, **k):
        self.on_report = on_report
        self.actions = []
        self.answer = {}                         # S1 family: an answer with no URL in it

    def publish(self, t, p):
        import json as _json
        if t.rsplit("/", 1)[-1] != "video":
            return
        action = _json.loads(p)["action"]
        self.actions.append(action)
        if action == "startCapture" and self.answer is not None:
            self.on_report("video", self.answer)

    @property
    def starts(self):
        return self.actions.count("startCapture")


@pytest.fixture
async def rig(hass):
    """The camera entity as Home Assistant holds it, a fake Stream, and a movable clock."""
    from types import SimpleNamespace

    from homeassistant.components.camera import DATA_COMPONENT
    from homeassistant.helpers import entity_registry as er

    from custom_components.anycubic import camera as camera_mod

    clock = SimpleNamespace(now=1000.0)
    streams = []

    made = SimpleNamespace(stream=FakeStream)    # a test can put Home Assistant's own here

    def fake_create_stream(hass_, source, *a, **k):
        streams.append(made.stream(source))
        return streams[-1]

    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "printer.local"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", CaptureTransport), \
         patch("homeassistant.components.camera.create_stream", fake_create_stream), \
         patch.object(camera_mod, "monotonic", lambda: clock.now):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        entity_id = er.async_get(hass).async_get_entity_id("camera", DOMAIN, "SER-1_camera")
        cam = hass.data[DATA_COMPONENT].get_entity(entity_id)

        async def settle():
            """Let a re-kick the camera started in the background run to its end."""
            await hass.async_block_till_done()
            for task in (cam._rekick_task, cam._unwatched_task):
                if task is not None:
                    await task
            await hass.async_block_till_done()

        async def window_lapses():
            """Nobody asks for the stream for the whole viewer window, and its timer fires."""
            from homeassistant.util import dt as dt_util
            from pytest_homeassistant_custom_component.common import async_fire_time_changed

            clock.now += camera_mod.CAPTURE_VIEWER_WINDOW + 1
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(
                seconds=camera_mod.CAPTURE_VIEWER_WINDOW + 1))
            await settle()

        async def open_card():
            """What `camera/stream` does: get the entity's Stream, add a provider, start it."""
            stream = await cam.async_create_stream()
            stream.play()
            await stream.start()
            return stream

        yield SimpleNamespace(hass=hass, entry=entry, coord=coord, cam=cam, clock=clock,
                              transport=coord._transport, settle=settle, open_card=open_card,
                              window_lapses=window_lapses, made=made, mod=camera_mod)
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_alone_starts_no_capture(rig):
    """Capture switches the chamber LED on, so nothing may start it until a stream is asked for."""
    assert rig.transport.actions == []
    assert rig.cam.stream is None


async def test_a_failing_stream_in_use_restarts_capture(rig):
    """The reported case, as the stream sees it: the worker cannot open the URL any more.
    Capture is started again and the worker restarted on the fresh URL, with the token the
    open card holds still valid."""
    stream = await rig.open_card()
    assert rig.transport.starts == 1
    rig.clock.now += 60
    rig.transport.answer = {"urls": {"rtspUrl": "http://192.0.2.9:18088/live/fresh"}}

    stream.set_state(False)                      # the worker failed and is waiting to retry
    await rig.settle()

    assert rig.transport.actions[-2:] == ["stopCapture", "startCapture"]
    assert rig.transport.starts == 2
    assert stream.source == "http://printer.local:18088/live/fresh"
    assert stream.log[-3:] == ["stop", "add:hls", "start:http://printer.local:18088/live/fresh"]
    assert stream.access_token == "token-a-card-holds"


async def test_restarts_are_rate_limited_and_back_off(rig):
    """A printer whose camera service is broken answers every start with a failure, so
    starting again on every worker retry would hammer it. One per 30 s, doubling to 5 min."""
    stream = await rig.open_card()               # the first kick, at t=0
    # Kept going past the viewer window by "Preload camera stream", which is exempt from
    # it — and is the case this cap is for.
    stream.dynamic_stream_settings.preload_stream = True

    async def fail_at(seconds):
        rig.clock.now = 1000.0 + seconds
        stream.set_state(True)                   # the worker tries again...
        stream.set_state(False)                  # ...and fails again
        await rig.settle()
        return rig.transport.starts

    assert await fail_at(10) == 1                # inside the minimum interval
    assert await fail_at(29) == 1
    assert await fail_at(30) == 2                # allowed; the next wait is 60 s
    assert await fail_at(80) == 2
    assert await fail_at(90) == 3                # the next wait is 120 s
    assert await fail_at(200) == 3
    assert await fail_at(210) == 4               # 240 s
    assert await fail_at(450) == 5               # 300 s: the cap
    assert await fail_at(740) == 5
    assert await fail_at(750) == 6
    assert await fail_at(1040) == 6              # still 300 s
    assert await fail_at(1050) == 7


async def test_the_backoff_resets_once_the_stream_has_been_healthy(rig):
    stream = await rig.open_card()
    for seconds in (30, 90, 210):                # three failed restarts: the wait is now 240 s
        rig.clock.now = 1000.0 + seconds
        stream.set_state(True)
        stream.set_state(False)
        await rig.settle()
    assert rig.transport.starts == 4

    rig.clock.now = 1300.0
    stream.set_state(True)                       # this time the worker stays up
    rig.clock.now = 1300.0 + rig.mod.STREAM_HEALTHY_AFTER + 1
    stream.set_state(False)                      # a new failure, long after
    await rig.settle()
    assert rig.transport.starts == 5, "a failure after a healthy spell waited out the old backoff"
    for later, starts in ((59, 5), (1, 6)):      # and the wait after that kick is 60 s, not 300
        rig.clock.now += later
        stream.set_state(True)
        stream.set_state(False)
        await rig.settle()
        assert rig.transport.starts == starts, "the backoff did not start over"


# "Nobody is watching" cannot be read off the Stream. An output's idle timer starts when a
# segment arrives, so a worker that cannot open the URL keeps the output a card made, and
# with it the worker, for ever after the browser has gone. What the camera goes by instead
# is how long ago anything last asked for the stream. In every test below the output is
# still there, as it is on the real thing.


async def test_a_failing_stream_is_kicked_inside_the_viewer_window(rig):
    stream = await rig.open_card()
    stream.set_state(False)                      # fails at once; too soon after the first kick
    await rig.settle()
    rig.clock.now += rig.mod.CAPTURE_VIEWER_WINDOW - 1
    stream.set_state(True)
    stream.set_state(False)
    await rig.settle()
    assert rig.transport.starts == 2
    assert stream.log[-1].startswith("start:")


async def test_a_failing_stream_nobody_asks_for_is_stopped_and_stays_dark(rig):
    """The owner watches, switches the printer off with the card open, closes the browser.
    Days later the printer is switched on. Nothing may start capture — it lights the
    chamber — and the worker must not still be retrying, and logging, either."""
    stream = await rig.open_card()
    rig.clock.now += 5
    stream.set_state(False)
    await rig.settle()
    assert stream.outputs(), "the output outlives the viewer on a stream that never plays"
    starts = rig.transport.starts

    await rig.window_lapses()                    # nothing but the timer: no worker report

    assert stream.outputs() == {}, "the Stream was left retrying with nobody asking for it"
    assert "stop" in stream.log
    assert rig.transport.starts == starts
    rig.clock.now += 3 * 86400
    rig.coord._recoveries = 5                    # the printer is switched back on
    rig.coord._apply("info", {"state": "free"})
    await rig.settle()
    assert rig.transport.starts == starts, "capture was started with nobody watching"

    rig.transport.answer = {"urls": {"rtspUrl": "http://192.0.2.9:18088/live/later"}}
    again = await rig.open_card()                # and now somebody is
    assert again is stream
    assert rig.transport.starts == starts + 1
    assert stream.log[-1] == "start:http://printer.local:18088/live/later"


async def test_a_failure_after_the_window_kicks_nothing_and_stops_the_stream(rig):
    """The same rule met from the other side: a worker report arriving after the window,
    should the timer not have dealt with the Stream already."""
    stream = await rig.open_card()
    stream.set_state(False)
    await rig.settle()
    rig.clock.now += rig.mod.CAPTURE_VIEWER_WINDOW + 1
    stream.set_state(True)
    stream.set_state(False)
    await rig.settle()
    assert rig.transport.starts == 1
    assert stream.outputs() == {}


async def test_a_session_that_comes_back_after_the_window_sends_nothing(rig):
    stream = await rig.open_card()
    stream.set_state(False)
    await rig.settle()
    rig.clock.now += rig.mod.CAPTURE_VIEWER_WINDOW + 1
    rig.coord._recoveries = 1
    rig.coord._apply("info", {"state": "free"})
    await rig.settle()
    assert rig.transport.starts == 1


async def test_a_playing_stream_is_not_stopped_when_the_window_lapses(rig):
    """The window is counted from the last request, and a card that stays open makes only
    the one. Home Assistant's own idle timer looks after a stream that is playing."""
    stream = await rig.open_card()
    await rig.window_lapses()
    assert "stop" not in stream.log
    assert stream.outputs()


async def test_a_stream_that_fails_while_being_watched_is_still_restarted(rig):
    """Twenty minutes into watching a print the stream drops. Home Assistant removes a
    playing stream's output half a minute after its last viewer, so one that had been
    playing and still has its output was being watched just now: that counts as asking."""
    stream = await rig.open_card()
    rig.clock.now += 1200
    stream.set_state(False)
    await rig.settle()
    assert rig.transport.starts == 2
    await rig.window_lapses()                    # and from then it is on the clock again
    assert stream.outputs() == {}


@pytest.mark.parametrize("played", [50, 100])
async def test_a_stream_that_played_under_a_minute_does_not_count_as_watched(rig, played):
    """A new output is kept for a minute from its first segment whether or not anybody
    ever requests one. So a printer camera that plays for fifty seconds and drops, over and
    over, with the browser long gone, must not keep the window open — or capture would be
    restarted about once a minute for ever, with the chamber lit. Nor at a hundred seconds
    on our clock, which starts before the stream is opened: a slow open can take thirty."""
    stream = await rig.open_card()
    stream.set_state(False)
    await rig.settle()
    rig.clock.now = 1000.0 + rig.mod.CAPTURE_VIEWER_WINDOW - played + 10
    stream.set_state(True)                       # it plays...
    rig.clock.now += played                      # ...and drops, into the lapsed window
    stream.set_state(False)
    await rig.settle()
    assert rig.transport.starts == 1, "capture was restarted for a stream nobody asked for"
    assert stream.outputs() == {}


async def test_a_stream_that_played_past_the_startup_timeout_counts_as_watched(rig):
    """An output still there after more than two minutes has had a segment requested in the
    last half minute: somebody is watching."""
    stream = await rig.open_card()
    stream.set_state(False)
    await rig.settle()
    rig.clock.now = 1000.0 + rig.mod.CAPTURE_VIEWER_WINDOW - 120
    stream.set_state(True)
    rig.clock.now += 130
    stream.set_state(False)
    await rig.settle()
    assert rig.transport.starts == 2
    assert stream.log[-1].startswith("start:")


async def test_preload_is_exempt_from_the_viewer_window(rig):
    """"Preload camera stream" is the user asking for the stream to be kept alive."""
    stream = await rig.open_card()
    stream.dynamic_stream_settings.preload_stream = True
    stream.set_state(False)
    await rig.settle()
    await rig.window_lapses()
    assert "stop" not in stream.log
    stream.set_state(True)
    stream.set_state(False)
    await rig.settle()
    assert rig.transport.starts == 2


async def test_no_restart_without_a_transport(rig):
    """With no session to the printer a command is dropped, so a kick would only wait."""
    stream = await rig.open_card()
    rig.clock.now += 600
    rig.coord._transport = None
    stream.set_state(False)
    await rig.settle()
    assert rig.transport.starts == 1
    assert stream.log.count("stop") == 0, "the worker was restarted with nothing new to open"


async def test_overlapping_restarts_do_not_run_together(rig):
    stream = await rig.open_card()
    rig.clock.now += 600
    rig.transport.answer = None                  # the printer is slow: the kick waits for it
    stream.set_state(False)
    await asyncio.sleep(0)
    first = rig.cam._rekick_task
    rig.clock.now += 600                         # even with the interval long past
    stream.set_state(True)
    stream.set_state(False)
    rig.cam._async_session_back()
    assert rig.cam._rekick_task is first, "a second kick was started while one was running"
    await rig.settle()
    assert rig.transport.starts == 2


async def test_a_session_that_comes_back_restarts_capture_at_once(rig):
    """The reporter's case: the printer was off, the worker has been retrying the whole
    time, and the printer is back. Not rate-limited: this is the moment a kick can work."""
    stream = await rig.open_card()
    rig.clock.now += 30
    stream.set_state(False)
    await rig.settle()
    assert rig.transport.starts == 2             # the next automatic one is 60 s away
    rig.clock.now += 5
    stream.set_state(True)
    stream.set_state(False)

    rig.coord._recoveries = 3                    # the printer went away and was re-handshaked
    rig.coord._apply("info", {"state": "free"})  # ...and has now answered
    await rig.settle()

    assert rig.transport.starts == 3
    assert stream.log[-1].startswith("start:")


async def test_a_session_that_comes_back_leaves_a_healthy_stream_alone(rig):
    """A re-handshake also happens when another client took the connection for a moment.
    A stream that is playing must not be interrupted for that."""
    stream = await rig.open_card()
    rig.clock.now += 600
    rig.coord._recoveries = 1
    rig.coord._apply("info", {"state": "free"})
    await rig.settle()
    assert rig.transport.starts == 1
    assert "stop" not in stream.log
    # If it was in fact stuck on a dead connection, it fails soon after — and that
    # failure is not made to wait: the session coming back cleared the backoff.
    rig.clock.now += 1
    stream.set_state(False)
    await rig.settle()
    assert rig.transport.starts == 2


async def test_a_session_that_comes_back_starts_nothing_when_no_stream_is_in_use(rig):
    rig.coord._recoveries = 1
    rig.coord._apply("info", {"state": "free"})
    await rig.settle()
    assert rig.transport.actions == []


async def test_reopening_the_card_kicks_capture_for_a_stopped_stream(rig):
    """The card was closed, the worker idled out, the printer was power cycled, the card is
    opened again. Home Assistant hands back the Stream it kept, so the kick has to be given
    here: capture is started and the kept Stream pointed at the fresh URL before it starts."""
    stream = await rig.open_card()
    await stream.remove_provider(stream.outputs()["hls"])     # idle timeout
    rig.clock.now += 3600
    rig.transport.answer = {"urls": {"rtspUrl": "http://192.0.2.9:18088/live/second"}}

    again = await rig.open_card()

    assert again is stream
    assert rig.transport.starts == 2
    assert stream.log[-1] == "start:http://printer.local:18088/live/second"


async def test_reopening_the_card_respects_the_minimum_interval(rig):
    stream = await rig.open_card()
    await stream.remove_provider(stream.outputs()["hls"])
    rig.clock.now += 5
    await rig.open_card()
    assert rig.transport.starts == 1


async def test_a_second_card_on_a_playing_stream_kicks_nothing(rig):
    """Stopping and starting capture under a stream someone is watching would interrupt
    it. Neither soon after it started nor much later."""
    stream = await rig.open_card()
    for later in (35, 600):
        rig.clock.now += later
        await rig.open_card()
        await rig.settle()
    assert rig.transport.starts == 1
    assert "stop" not in stream.log


async def test_a_card_opened_on_a_failing_stream_kicks_at_the_minimum_interval(rig):
    """The automatic kicks have backed off to minutes; someone opening the card should not
    have to wait that out."""
    stream = await rig.open_card()
    for seconds in (30, 90, 210):                # the automatic wait is now 240 s
        rig.clock.now = 1000.0 + seconds
        stream.set_state(True)
        stream.set_state(False)
        await rig.settle()
    assert rig.transport.starts == 4
    rig.clock.now += 31
    await rig.open_card()
    stream.available = False                     # still failing when the kick is decided
    await rig.settle()
    assert rig.transport.starts == 5


async def test_a_recording_is_not_cut_short_by_a_restart(rig):
    """Removing the providers is how the worker is restarted, and removing the recorder
    ends the recording. With one running, the fresh URL is left for the worker's own retry."""
    stream = await rig.open_card()
    stream.add_provider("recorder")
    rig.clock.now += 60
    rig.transport.answer = {"urls": {"rtspUrl": "http://192.0.2.9:18088/live/fresh"}}
    stream.set_state(False)
    await rig.settle()
    assert rig.transport.starts == 2
    assert stream.source == "http://printer.local:18088/live/fresh"
    assert "stop" not in stream.log
    assert set(stream.outputs()) == {"hls", "recorder"}


async def test_removing_the_entity_stops_its_stream(rig):
    """Home Assistant does not stop a camera's Stream when the entity goes. In the blank
    state the worker never idles out either, so every reload left one more retrying."""
    stream = await rig.open_card()
    stream.set_state(False)
    await rig.settle()
    assert await rig.hass.config_entries.async_unload(rig.entry.entry_id)
    await rig.hass.async_block_till_done()
    assert stream.log[-1] == "stop"
    assert stream.outputs() == {}
    assert rig.transport.actions[-1] == "stopCapture"
    stream.set_state(False)                      # a late report from the dying worker
    await rig.hass.async_block_till_done()
    assert rig.transport.actions[-1] == "stopCapture", "a removed camera started capture"


async def test_removing_the_entity_stops_a_preloaded_stream_too(rig):
    """With "Preload camera stream" on, Stream.stop() leaves the worker running on purpose."""
    stream = await rig.open_card()
    stream.dynamic_stream_settings.preload_stream = True
    stopped = []

    async def _stop():
        stopped.append(True)

    stream._stop = _stop
    assert await rig.hass.config_entries.async_unload(rig.entry.entry_id)
    assert stopped == [True]


# The tests below run Home Assistant's own Stream, with only the decoding worker
# replaced, because what they pin down is its behaviour and not ours.


@pytest.fixture
def real_stream(hass):
    import sys
    import threading
    import types
    from unittest.mock import MagicMock

    from homeassistant.components.camera.prefs import DynamicStreamSettings
    from homeassistant.components.stream import Stream
    from homeassistant.components.stream.core import PROVIDERS
    from homeassistant.components.stream.exceptions import StreamWorkerError

    opened = []
    refuse = threading.Event()
    refuse.set()

    def stream_worker(source, options, settings, state, keyframes, quit_event):
        opened.append(source)
        if refuse.is_set():
            raise StreamWorkerError("Connection refused")
        quit_event.wait()

    class Output:
        name = "hls"
        idle = False
        def __init__(self, *a): pass
        def cleanup(self): pass

    worker = types.ModuleType("homeassistant.components.stream.worker")
    worker.stream_worker = stream_worker
    worker.StreamState = type("StreamState", (), {
        "__init__": lambda self, *a: None, "discontinuity": lambda self: None})
    with patch.dict(sys.modules, {"homeassistant.components.stream.worker": worker}), \
         patch.dict(PROVIDERS, {"hls": Output}):
        made = []

        def make(source="http://printer.local:18088/flv", preload=False):
            made.append(Stream(hass, source, {}, MagicMock(),
                               DynamicStreamSettings(preload_stream=preload), "camera.test"))
            return made[-1]

        yield make, opened, refuse
        for stream in made:
            stream._thread_quit.set()            # whatever the test left running


async def _until(predicate, what):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def test_restart_of_a_waiting_worker_keeps_the_token_and_can_be_stopped(hass, real_stream):
    """Why the camera restarts the worker itself instead of calling Stream.update_source().

    A failing worker spends nearly all its time waiting to retry. update_source() during
    that wait does not restart it: the worker ends, its outputs and access token are thrown
    away, and the flag update_source() left set makes the NEXT stop() restart the worker
    instead of ending it, so that stop() never returns."""
    from custom_components.anycubic.camera import async_restart_stream

    make, opened, refuse = real_stream
    stream = make()
    stream.add_provider("hls")
    stream.access_token = "token-a-card-holds"
    await stream.start()
    await _until(lambda: not stream.available, "the worker to fail and wait")

    refuse.clear()                               # capture is back
    await async_restart_stream(stream, "http://printer.local:18088/live/fresh")
    await _until(lambda: len(opened) == 2, "the restarted worker to open the stream")

    assert opened[-1] == "http://printer.local:18088/live/fresh"
    assert list(stream.outputs()) == ["hls"]
    assert stream.access_token == "token-a-card-holds"
    async with asyncio.timeout(5):
        await stream.stop()                      # hangs if a restart was left pending
    assert len(opened) == 2


async def test_restart_of_a_worker_that_is_reading_also_works(hass, real_stream):
    from custom_components.anycubic.camera import async_restart_stream

    make, opened, refuse = real_stream
    stream = make()
    refuse.clear()
    stream.add_provider("hls")
    await stream.start()
    await _until(lambda: len(opened) == 1, "the worker to open the stream")
    await async_restart_stream(stream, "http://printer.local:18088/live/fresh")
    await _until(lambda: len(opened) == 2, "the restarted worker to open the stream")
    assert opened[-1] == "http://printer.local:18088/live/fresh"
    async with asyncio.timeout(5):
        await stream.stop()


def _worker_running(stream):
    return stream._thread is not None and stream._thread.is_alive()


async def test_restart_with_preload_on_restarts_the_worker_and_it_can_still_be_ended(
        hass, real_stream):
    """With "Preload camera stream" on, Stream.stop() leaves the worker running, so a
    restart through stop() alone would restart nothing: the worker would sit out its retry
    delay on the old thread. And nothing public would ever end it when the entity goes."""
    from custom_components.anycubic.camera import async_restart_stream, async_stop_stream

    make, opened, refuse = real_stream
    stream = make(preload=True)
    stream.add_provider("hls")
    stream.access_token = "token-a-card-holds"
    await stream.start()
    await _until(lambda: not stream.available, "the worker to fail and wait")
    waiting = stream._thread

    refuse.clear()
    async with asyncio.timeout(5):
        await async_restart_stream(stream, "http://printer.local:18088/live/fresh")
    await _until(lambda: len(opened) == 2, "the restarted worker to open the stream")

    assert stream._thread is not waiting and not waiting.is_alive()
    assert opened[-1] == "http://printer.local:18088/live/fresh"
    assert list(stream.outputs()) == ["hls"]
    assert stream.access_token == "token-a-card-holds"

    async with asyncio.timeout(5):
        await async_stop_stream(stream)
    assert not _worker_running(stream)
    assert len(opened) == 2


@pytest.mark.parametrize("moment", [
    "during the kick", "as the restart begins", "while the restart runs"])
async def test_removing_the_entity_while_capture_is_being_restarted(rig, real_stream, moment):
    """A reload can land while a re-kick is part-way through the same Stream. Whichever
    half it is in, the removal has to return, and leave no worker and no restart behind."""
    make, opened, refuse = real_stream
    rig.made.stream = make
    restarting, go_on = asyncio.Event(), asyncio.Event()
    real_restart = rig.mod.async_restart_stream

    async def announced_restart(stream, source):
        restarting.set()
        await go_on.wait()                       # held here until the test says
        await real_restart(stream, source)

    stream = await rig.cam.async_create_stream()
    stream.add_provider("hls")
    stream.access_token = "token-a-card-holds"
    rig.clock.now += 60                          # a kick is due when the worker fails
    if moment == "during the kick":
        rig.transport.answer = None              # the printer has not answered yet
    with patch.object(rig.mod, "async_restart_stream", announced_restart):
        await stream.start()
        if moment == "during the kick":
            await _until(lambda: rig.cam._rekick_task is not None, "the re-kick to start")
        else:
            await _until(restarting.is_set, "the restart to begin")
        task = rig.cam._rekick_task
        assert not task.done()
        if moment == "while the restart runs":
            go_on.set()                          # both now run, in whatever order falls out

        async with asyncio.timeout(5):
            assert await rig.hass.config_entries.async_unload(rig.entry.entry_id)

    assert task.done()
    assert not _worker_running(stream)
    assert stream.outputs() == {}
    count = len(opened)
    await asyncio.sleep(0.1)
    assert len(opened) == count and not _worker_running(stream), "the worker came back"
