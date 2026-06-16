from unittest.mock import AsyncMock, patch

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


async def test_ace_sensors(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("multiColorBox", {"multi_color_box": [{
            "id": 0, "humidity": 24, "temp": 35, "loaded_slot": 1,
            "drying_status": {"status": 0},
            "slots": [{"index": 1, "type": "PETG", "color": [67, 82, 59],
                       "status": 5, "consumables_percent": 95}]}]})
        await hass.async_block_till_done()

    assert hass.states.get("sensor.ace_2_humidity").state == "24"
    assert hass.states.get("sensor.ace_2_box_temperature").state == "35"
    assert hass.states.get("sensor.ace_2_loaded_slot").state == "1"
    assert hass.states.get("switch.ace_2_drying").state == "off"
    slot = hass.states.get("sensor.ace_2_slot_1")
    assert slot.state == "PETG"
    assert slot.attributes["remaining"] == 95
    assert slot.attributes["color"] == "#43523B"
    # ACE is its own device, linked to the printer
    from homeassistant.helpers import device_registry as dr
    dev = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "SER-1_ace0")})
    assert dev is not None and dev.via_device_id is not None


async def test_loaded_slot_none_when_unloaded(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        # -1 is the printer's "no slot loaded" sentinel -> show "None", not "-1".
        coord._apply("multiColorBox", {"multi_color_box": [{"id": 0, "loaded_slot": -1, "temp": 30}]})
        await hass.async_block_till_done()
    assert hass.states.get("sensor.ace_2_loaded_slot").state == "None"


async def test_ace_drying_switch(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        # box exists but idle (no drying_status -> drying unknown -> switch off)
        coord._apply("multiColorBox", {"multi_color_box": [{"id": 0, "temp": 30}]})
        await hass.async_block_till_done()
        assert hass.states.get("switch.ace_2_drying").state == "off"

        coord.async_send_command = AsyncMock()
        # Turn on -> sends drying_start with the validated 45C/240min and flips optimistically.
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.ace_2_drying"}, blocking=True)
        coord.async_send_command.assert_awaited_with("drying_start", target_temp=45, duration=240)
        await hass.async_block_till_done()
        assert hass.states.get("switch.ace_2_drying").state == "on"

        # A later report that omits drying_status must NOT flip it back off.
        coord._apply("multiColorBox", {"multi_color_box": [{"id": 0, "temp": 31}]})
        await hass.async_block_till_done()
        assert hass.states.get("switch.ace_2_drying").state == "on"

        # Turn off -> sends drying_stop and flips optimistically.
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.ace_2_drying"}, blocking=True)
        coord.async_send_command.assert_awaited_with("drying_stop")
        await hass.async_block_till_done()
        assert hass.states.get("switch.ace_2_drying").state == "off"
