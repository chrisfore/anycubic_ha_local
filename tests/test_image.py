# tests/test_image.py
import json
import pathlib
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic.anycubic_local.handshake import HandshakeResult
from custom_components.anycubic.const import DOMAIN

HS = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER-1")

THUMB = "image.anycubic_kobra_s1_max_object_thumbnail"
TOP = "image.anycubic_kobra_s1_max_object_top_view"


class FakeTransport:
    def __init__(s, hs, on_report, **k): s.on_report = on_report
    def connect(s): pass
    def disconnect(s): pass
    def query(s, t): pass
    def publish(s, t, p): pass


def _details(filename="boat.gcode"):
    d = json.loads((pathlib.Path(__file__).parent / "fixtures" / "file_details.json").read_text())
    d["filename"] = filename
    return d


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_image_entities_appear_only_once_the_printer_answers(hass):
    # Issue #13: the printer answers a fileDetails request with renders of the job.
    # Nothing exists before one arrives — an idle printer has no object to show.
    entry = await _setup(hass)
    assert hass.states.get(THUMB) is None
    assert hass.states.get(TOP) is None

    coord = entry.runtime_data
    coord._apply("print", {"taskid": "-1", "progress": 5, "filename": "boat.gcode"})
    coord._apply("file", _details("boat.gcode"))
    await hass.async_block_till_done()

    assert hass.states.get(THUMB) is not None
    assert hass.states.get(TOP) is not None


async def test_image_entities_serve_the_bytes_the_printer_sent(hass):
    entry = await _setup(hass)
    coord = entry.runtime_data
    coord._apply("print", {"taskid": "-1", "progress": 5, "filename": "boat.gcode"})
    coord._apply("file", _details("boat.gcode"))
    await hass.async_block_till_done()

    # Reach the entities through the platform rather than over HTTP: the bytes are the point.
    comp = hass.data["entity_components"]["image"]
    thumb = next(e for e in comp.entities if e.entity_id == THUMB)
    top = next(e for e in comp.entities if e.entity_id == TOP)
    assert (await thumb.async_image()).startswith(b"\x89PNG")
    assert (await top.async_image()).startswith(b"\x89PNG")
    assert await thumb.async_image() != await top.async_image()


async def test_image_last_updated_advances_on_a_new_job(hass):
    # The frontend re-fetches when image_last_updated moves; a new object must show.
    entry = await _setup(hass)
    coord = entry.runtime_data
    coord._apply("print", {"taskid": "-1", "progress": 5, "filename": "boat.gcode"})
    coord._apply("file", _details("boat.gcode"))
    await hass.async_block_till_done()
    first = hass.states.get(THUMB).state

    coord._apply("print", {"taskid": "-1", "progress": 1, "filename": "benchy.gcode"})
    coord._apply("file", _details("benchy.gcode"))
    await hass.async_block_till_done()

    assert hass.states.get(THUMB).state != first
