from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic.const import DOMAIN
from custom_components.anycubic.anycubic_local.exceptions import HandshakeError
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult, do_handshake


class FakeTransport:
    def __init__(self, hs, on_report, **k): pass
    def connect(self): pass
    def disconnect(self): pass
    def query(self, t): pass
    def publish(self, t, p): pass


async def _setup(hass, model_id, serial):
    hs = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", model_id, serial)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=serial, data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=hs), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry.runtime_data


async def test_open_frame_kobra3_gates_enclosure_hardware(hass):
    await _setup(hass, "20024", "SER-K3")  # AnyCubic Kobra 3 (open frame)
    g = hass.states.get
    # Core printer entities + the FLV camera (built-in/add-on) are present.
    assert g("sensor.anycubic_kobra_3_nozzle_temperature") is not None
    assert g("camera.anycubic_kobra_3_camera") is not None
    # Enclosure-only hardware must NOT be created on an open-frame printer.
    assert g("sensor.anycubic_kobra_3_chamber_temperature") is None
    assert g("number.anycubic_kobra_3_chamber_fan") is None
    assert g("light.anycubic_kobra_3_chamber_light") is None


async def test_kobra_x_has_no_local_camera(hass):
    await _setup(hass, "20030", "SER-KX")  # AnyCubic Kobra X (WebRTC camera, no local FLV)
    assert hass.states.get("camera.anycubic_kobra_x_camera") is None
    assert hass.states.get("sensor.anycubic_kobra_x_nozzle_temperature") is not None


async def test_s1max_keeps_full_entity_set(hass):
    await _setup(hass, "20029", "SER-S1M")  # validated reference device — must be unchanged
    g = hass.states.get
    assert g("sensor.anycubic_kobra_s1_max_chamber_temperature") is not None
    assert g("number.anycubic_kobra_s1_max_chamber_fan") is not None
    assert g("light.anycubic_kobra_s1_max_chamber_light") is not None
    assert g("camera.anycubic_kobra_s1_max_camera") is not None


def test_handshake_rejects_unsigned_models_cleanly():
    # A Kobra 2-style /info lacks the signed-handshake fields -> a clear HandshakeError, not a KeyError.
    info = {"modelId": "20021", "cn": "K2P"}
    with pytest.raises(HandshakeError):
        do_handshake("1.2.3.4", fetch=lambda method, url: info)


def test_parse_mac_from_usn():
    from custom_components.anycubic.anycubic_local.handshake import _parse_mac
    assert _parse_mac("uuid:fdm:AA-BB-CC-DD-EE-FF") == "AA-BB-CC-DD-EE-FF"
    assert _parse_mac(None) is None
    assert _parse_mac("no-mac-here") is None


async def test_device_has_network_mac_connection(hass):
    from homeassistant.helpers import device_registry as dr
    hs = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER-MAC", mac="AA-BB-CC-DD-EE-FF")
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-MAC", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=hs), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    dev = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "SER-MAC")})
    assert (dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff") in dev.connections
