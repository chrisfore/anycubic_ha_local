"""The "Camera source" setting and the camera it selects on printers running Rinkhals."""
import asyncio
import json
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.components.camera import CameraEntityFeature, async_get_image
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from multidict import CIMultiDict
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import mock_stream

from custom_components.anycubic import rinkhals_camera as rinkhals
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult
from custom_components.anycubic.const import DOMAIN

HOST = "printer.example"
HS = HandshakeResult("192.168.1.50", 9883, "u", "p", "DEV", "20029", "SER-1")
# A model with no camera of its own: no camera entity at all on the printer's own source.
HS_NO_CAMERA = HandshakeResult("192.168.1.50", 9883, "u", "p", "DEV", "20021", "SER-1")
CAMERA = "camera.anycubic_kobra_s1_max_camera"
JPEG = b"\xff\xd8" + b"j" * 2000 + b"\xff\xd9"
MULTIPART = "multipart/x-mixed-replace;boundary=boundarydonotcross"
FRAMES = (b"--boundarydonotcross\r\nContent-Type: image/jpeg\r\nContent-Length: 2004\r\n\r\n"
          + JPEG + b"\r\n") * 3


class RecordingTransport:
    """Records everything published, so "no capture command" can be asserted outright."""
    published = []

    def __init__(self, hs, on_report, **k): pass
    def connect(self): pass
    def disconnect(self): pass
    def query(self, t): pass

    def publish(self, t, p):
        type(self).published.append((t.rsplit("/", 1)[-1], json.loads(p).get("action")))


def _video_commands():
    return [action for topic, action in RecordingTransport.published if topic == "video"]


class FakeResponse:
    def __init__(self, status=200, content_type="image/jpeg", body=JPEG, headers=None):
        self.status = status
        self.headers = CIMultiDict(headers or {})
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        self.content = mock_stream(body)
        self.closed = False

    def close(self):
        self.closed = True

    def release(self):
        self.closed = True


class FakeSession:
    """Stands in for Home Assistant's client session; answers in order, records each GET."""

    def __init__(self):
        self.answers = []
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((str(url), kwargs))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        if answer == "hang":
            await asyncio.sleep(3600)
        return answer


@pytest.fixture
def feed():
    session = FakeSession()
    RecordingTransport.published = []
    with patch.object(rinkhals, "async_get_clientsession", lambda hass: session):
        yield session


async def _setup(hass, options=None, hs=HS, host=HOST):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": host},
                            options=options or {})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=hs), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt",
               RecordingTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _camera_id(hass):
    return er.async_get(hass).async_get_entity_id("camera", DOMAIN, "SER-1_camera")


RINKHALS = {"camera_source": "rinkhals"}


# ------------------------------------------------- the setting


async def test_the_camera_source_defaults_to_the_printer_camera(hass, feed):
    entry = await _setup(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.FORM and flow["step_id"] == "init"
    schema = flow["data_schema"]
    assert schema({}) == {"camera_source": "printer"}
    assert schema({"camera_source": "rinkhals"}) == {"camera_source": "rinkhals"}
    with pytest.raises(Exception):  # noqa: B017 - voluptuous's own error; any refusal will do
        schema({"camera_source": "http://192.0.2.9:8080/"})
    state = hass.states.get(CAMERA)
    assert state.attributes["supported_features"] & CameraEntityFeature.STREAM


async def test_choosing_rinkhals_reloads_the_entry_into_the_other_camera(hass, feed):
    entry = await _setup(hass)
    before = er.async_get(hass).async_get(CAMERA)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch("custom_components.anycubic.do_handshake", return_value=HS) as handshake, \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt",
               RecordingTransport):
        done = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"camera_source": "rinkhals"})
        await hass.async_block_till_done()
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"camera_source": "rinkhals"}
    assert handshake.call_count == 1, "the entry was not reloaded, or was reloaded twice"
    assert entry.state is ConfigEntryState.LOADED
    # The same entity, by the same ids: no second camera, none left behind.
    after = er.async_get(hass).async_get(CAMERA)
    assert after.id == before.id and after.unique_id == "SER-1_camera"
    assert len([e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
                if e.domain == "camera"]) == 1
    assert not hass.states.get(CAMERA).attributes["supported_features"] & CameraEntityFeature.STREAM
    # And the form now opens on what was chosen.
    again = await hass.config_entries.options.async_init(entry.entry_id)
    assert again["data_schema"]({}) == {"camera_source": "rinkhals"}


async def test_saving_the_same_source_does_not_reload(hass, feed):
    entry = await _setup(hass, RINKHALS)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch("custom_components.anycubic.do_handshake", return_value=HS) as handshake:
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"camera_source": "rinkhals"})
        await hass.async_block_till_done()
    assert handshake.call_count == 0


async def test_the_form_strings_exist_in_both_files():
    from pathlib import Path
    base = Path(rinkhals.__file__).parent
    for name in ("strings.json", "translations/en.json"):
        strings = json.loads((base / name).read_text())
        step = strings["options"]["step"]["init"]
        assert step["data"] == {"camera_source": "Camera source"}
        assert "description" not in step and "data_description" not in step
        assert strings["selector"]["camera_source"]["options"] == {
            "printer": "Printer camera", "rinkhals": "Rinkhals camera"}


# ------------------------------------------------- which camera there is


async def test_a_printer_without_a_camera_has_one_only_on_the_rinkhals_source(hass, feed):
    entry = await _setup(hass, hs=HS_NO_CAMERA)
    assert _camera_id(hass) is None
    with patch("custom_components.anycubic.do_handshake", return_value=HS_NO_CAMERA), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt",
               RecordingTransport):
        hass.config_entries.async_update_entry(entry, options=RINKHALS)
        await hass.async_block_till_done()
        assert _camera_id(hass) is not None
        assert hass.states.get(_camera_id(hass)) is not None
        # Back on the printer's own source there is no camera to show, and the entity is
        # taken away with it, not left behind as one that is never provided again.
        hass.config_entries.async_update_entry(entry, options={"camera_source": "printer"})
        await hass.async_block_till_done()
    assert _camera_id(hass) is None


async def test_diagnostics_name_the_source_and_not_the_address(hass, feed):
    from custom_components.anycubic.diagnostics import async_get_config_entry_diagnostics

    for options, expected in (({}, "printer"), (RINKHALS, "rinkhals")):
        entry = await _setup(hass, options)
        diag = await async_get_config_entry_diagnostics(hass, entry)
        assert diag["camera_source"] == expected
        assert "8080" not in json.dumps(diag)
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()


# ------------------------------------------------- the snapshot


async def test_the_snapshot_is_the_cameras_image(hass, feed):
    await _setup(hass, RINKHALS)
    feed.answers = [FakeResponse()]
    image = await async_get_image(hass, CAMERA)
    assert image.content == JPEG and image.content_type == "image/jpeg"
    url, kwargs = feed.calls[0]
    # Only the path and port are fixed here; the host is always the configured one.
    assert url == "http://printer.example:8080/?action=snapshot"
    assert kwargs["allow_redirects"] is False
    assert feed.answers[0].closed
    assert hass.states.get(CAMERA).state == "idle"


async def test_a_configured_ipv6_address_is_bracketed_in_the_feed_address(hass, feed):
    await _setup(hass, RINKHALS, host="2001:db8::10")
    feed.answers = [FakeResponse()]
    await async_get_image(hass, CAMERA)
    assert feed.calls[0][0] == "http://[2001:db8::10]:8080/?action=snapshot"


@pytest.mark.parametrize(("answer", "why"), [
    (lambda: FakeResponse(content_type="text/html", body=b"<html>not a picture</html>"),
     "what came back is not an image"),
    (lambda: FakeResponse(content_type=None), "what came back does not say what it is"),
    (lambda: FakeResponse(status=404), "the feed answered with an error"),
    (lambda: FakeResponse(status=302, headers={"Location": "http://192.0.2.9/elsewhere"}),
     "the feed answered with a redirect, which is not followed"),
    (lambda: FakeResponse(body=b"j" * (rinkhals.SNAPSHOT_MAX_BYTES + 1)),
     "the image is larger than any snapshot"),
    (lambda: FakeResponse(headers={"Content-Length": str(rinkhals.SNAPSHOT_MAX_BYTES + 1)}),
     "the image says it is larger than any snapshot"),
    (lambda: aiohttp.ClientConnectionError("Cannot connect to host 192.168.1.50:8080"),
     "nothing is listening"),
    (lambda: "hang", "the feed never answers"),
])
async def test_a_snapshot_that_cannot_be_used_is_no_image(hass, feed, caplog, answer, why):
    await _setup(hass, RINKHALS, host="192.168.1.50")
    feed.answers = [answer()]
    with patch.object(rinkhals, "SNAPSHOT_TIMEOUT", 0.05), \
         caplog.at_level("DEBUG", logger=rinkhals.__name__):
        with pytest.raises(HomeAssistantError):
            await async_get_image(hass, CAMERA)
    assert hass.states.get(CAMERA).state == "unavailable", why
    assert len(feed.calls) == 1, "something else was tried"
    assert _video_commands() == [], "the printer's own camera was tried instead"
    assert any(r.name == rinkhals.__name__ for r in caplog.records), "nothing was logged"
    for text in ("192.168.1.50", "192.0.2.9", "8080", "action=", "not a picture", "elsewhere"):
        assert text not in caplog.text


async def test_an_unavailable_camera_is_tried_again_later_without_polling(hass, feed):
    """What says the camera is unavailable is the last time it was asked. Nothing asks it in
    the background, so after a while the entity is offered again, and the next card that
    shows it finds out."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    await _setup(hass, RINKHALS)
    feed.answers = [FakeResponse(status=503)]
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, CAMERA)
    assert hass.states.get(CAMERA).state == "unavailable"

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=rinkhals.FEED_RETRY_AFTER + 1))
    await hass.async_block_till_done()
    assert hass.states.get(CAMERA).state == "idle"
    assert len(feed.calls) == 1, "the feed was asked with nobody looking"

    feed.answers = [FakeResponse()]
    assert (await async_get_image(hass, CAMERA)).content == JPEG


async def test_an_outage_is_a_warning_once_however_often_it_is_asked(hass, feed, caplog):
    """A card left open on a printer that is off asks again every time the camera is
    offered. One line at the normal level for the outage, and one again for the next."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    await _setup(hass, RINKHALS)

    async def ask(answer, later=0):
        feed.answers = [answer]
        if later:
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=later))
            await hass.async_block_till_done()
        try:
            await async_get_image(hass, CAMERA)
        except HomeAssistantError:
            pass

    def warnings():
        return [r for r in caplog.records
                if r.name == rinkhals.__name__ and r.levelname == "WARNING"]

    with caplog.at_level("DEBUG", logger=rinkhals.__name__):
        await ask(FakeResponse(status=503))
        await ask(FakeResponse(status=503), later=rinkhals.FEED_RETRY_AFTER + 1)
        await ask(FakeResponse(status=503), later=2 * rinkhals.FEED_RETRY_AFTER + 2)
        assert len(warnings()) == 1 and "status 503" in warnings()[0].getMessage()
        await ask(FakeResponse())
        await ask(FakeResponse(status=500))
        assert len(warnings()) == 2


async def test_a_good_answer_brings_the_camera_back_at_once(hass, feed):
    await _setup(hass, RINKHALS)
    feed.answers = [FakeResponse(status=503), FakeResponse()]
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, CAMERA)
    assert (await async_get_image(hass, CAMERA)).content == JPEG
    assert hass.states.get(CAMERA).state == "idle"


# ------------------------------------------------- the live view


async def test_the_live_view_is_the_printers_stream_passed_through(hass, feed, hass_client):
    await _setup(hass, RINKHALS)
    feed.answers = [FakeResponse(content_type=MULTIPART, body=FRAMES)]
    client = await hass_client()
    response = await client.get(f"/api/camera_proxy_stream/{CAMERA}")
    assert response.status == 200
    assert response.headers["Content-Type"] == MULTIPART
    assert await response.read() == FRAMES
    url, kwargs = feed.calls[0]
    assert url == "http://printer.example:8080/?action=stream"
    assert kwargs["allow_redirects"] is False
    assert feed.answers[0].closed
    assert _video_commands() == []


@pytest.mark.parametrize("answer", [
    lambda: FakeResponse(content_type="image/jpeg"),
    lambda: FakeResponse(content_type="text/html", body=b"<html></html>"),
    lambda: FakeResponse(status=500, content_type=MULTIPART),
    lambda: FakeResponse(status=301, content_type=MULTIPART,
                         headers={"Location": "http://192.0.2.9/elsewhere"}),
    lambda: aiohttp.ClientConnectionError("Cannot connect to host 192.168.1.50:8080"),
    lambda: "hang",
])
async def test_a_stream_that_is_not_one_is_refused(hass, feed, hass_client, answer):
    await _setup(hass, RINKHALS)
    feed.answers = [answer()]
    client = await hass_client()
    with patch.object(rinkhals, "CONNECT_TIMEOUT", 0.05):
        response = await client.get(f"/api/camera_proxy_stream/{CAMERA}")
    assert response.status == 502
    assert hass.states.get(CAMERA).state == "unavailable"
    assert len(feed.calls) == 1 and _video_commands() == []
    if isinstance(feed.answers[0], FakeResponse):
        assert feed.answers[0].closed


# ------------------------------------------------- nothing is asked of the printer's own camera


async def test_no_capture_command_is_ever_published_on_the_rinkhals_source(
        hass, feed, hass_client, hass_ws_client):
    """Capture commands start the printer's own camera service and light the chamber. On
    this source the integration sends none: not for a picture, not for the live view, not
    when the printer comes back from a recovery, not when the entity is removed."""
    entry = await _setup(hass, RINKHALS)
    coord = entry.runtime_data
    # The websocket client first: it sets up websocket_api, whose route can't be added
    # once hass_client() has started the HTTP app and frozen its router.
    ws = await hass_ws_client(hass)
    feed.answers = [FakeResponse()]
    await async_get_image(hass, CAMERA)
    feed.answers = [FakeResponse(content_type=MULTIPART, body=FRAMES)]
    client = await hass_client()
    await (await client.get(f"/api/camera_proxy_stream/{CAMERA}")).read()
    # What the frontend asks a camera with a stream; this one has none to give.
    await ws.send_json({"id": 1, "type": "camera/stream", "entity_id": CAMERA})
    assert (await ws.receive_json())["success"] is False
    coord._recoveries = 2
    coord._apply("info", {"state": "free"})
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _video_commands() == []


async def test_the_printer_camera_still_sends_its_capture_commands(hass, feed):
    """The other half of the same statement: the default source is as it was."""
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _video_commands() == ["stopCapture"]
    assert feed.calls == []
