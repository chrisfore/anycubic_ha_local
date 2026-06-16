from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic.const import DOMAIN
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult

HS = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER-1")
ENTITY = "light.anycubic_kobra_s1_max_chamber_light"


class FakeTransport:
    def __init__(self, hs, on_report, **k):
        pass

    def connect(self):
        pass

    def disconnect(self):
        pass

    def query(self, t):
        pass

    def publish(self, t, p):
        pass


async def test_light_state_and_control(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("light", {"lights": [{"type": 2, "status": 1, "brightness": 100}]})
        await hass.async_block_till_done()

        st = hass.states.get(ENTITY)
        assert st is not None and st.state == "on"
        # On/off light (chamber LED is not dimmable) — no brightness control.
        assert st.attributes.get("supported_color_modes") == ["onoff"]
        assert "brightness" not in st.attributes

        # Turn off: the command is sent AND the state flips immediately (optimistic),
        # since the printer only echoes the new light state on the next poll.
        coord.async_send_command = AsyncMock()
        await hass.services.async_call(
            "light", "turn_off", {"entity_id": ENTITY}, blocking=True)
        coord.async_send_command.assert_awaited_with("light", on=False)
        await hass.async_block_till_done()
        assert hass.states.get(ENTITY).state == "off"

        # Turn back on: optimistic state returns to on without a new report.
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": ENTITY}, blocking=True)
        coord.async_send_command.assert_awaited_with("light", on=True)
        await hass.async_block_till_done()
        assert hass.states.get(ENTITY).state == "on"
