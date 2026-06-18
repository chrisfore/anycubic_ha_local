from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic.const import DOMAIN
from custom_components.anycubic.diagnostics import async_get_config_entry_diagnostics
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult

HS = HandshakeResult("1.2.3.4", 9883, "u", "secretpw", "DEV", "20029", "SER-1",
                     model_name="Anycubic Kobra S1 Max", device_type="fdm")


class FakeTransport:
    def __init__(self, hs, on_report, **k): pass
    def connect(self): pass
    def disconnect(self): pass
    def query(self, t): pass
    def publish(self, t, p): pass


async def test_diagnostics_redacts_identifiers(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "10.0.0.5"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("info", {"model": "Kobra S1 Max", "ip": "10.0.0.5", "state": "free",
                              "temp": {"curr_chamber_temp": 36},
                              "features": {"camera_timelapse_support": True, "fod_support": True},
                              "urls": {"rtspUrl": "http://10.0.0.5:18088/flv"},
                              "project": {"filename": "JaneDoe_secret_part.gcode.3mf"}})
        coord._apply("peripherie", {"camera": 1, "multiColorBox": 1, "udisk": 0})
        await hass.async_block_till_done()

        diag = await async_get_config_entry_diagnostics(hass, entry)

    # Non-secret diagnostic context survives.
    assert diag["model_id"] == "20029"
    assert diag["update_success"] is True
    # The capability block carries everything needed to add a new model — and nothing sensitive.
    caps = diag["capabilities"]
    assert caps["model_id"] == "20029"
    assert caps["model_name"] == "Anycubic Kobra S1 Max"
    assert caps["device_type"] == "fdm"
    assert caps["has_chamber_temp"] is True
    assert caps["features"] == {"camera_timelapse_support": True, "fod_support": True}
    assert caps["peripherie"] == {"camera": 1, "multiColorBox": 1, "udisk": 0}
    assert {"info", "peripherie"} <= set(caps["report_types_seen"])
    # Addresses and identifiers are redacted everywhere they appear.
    assert diag["host"] == "**REDACTED**"
    assert diag["entry_data"]["host"] == "**REDACTED**"
    assert diag["printer"]["ip"] == "**REDACTED**"
    # And no raw secret/identifier value leaks anywhere in the blob.
    blob = str(diag)
    for secret in ("10.0.0.5", "secretpw", "SER-1", "DEV", "JaneDoe"):
        assert secret not in blob
