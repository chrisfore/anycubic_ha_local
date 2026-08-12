from unittest.mock import AsyncMock, patch

import pytest
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
