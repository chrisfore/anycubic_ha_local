from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic.const import DOMAIN
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult
from custom_components.anycubic.anycubic_local.models import PrinterState

HS = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER-1")


class FakeTransport:
    def __init__(self, hs, on_report, **k): pass
    def connect(self): pass
    def disconnect(self): pass
    def query(self, t): pass
    def publish(self, t, p): pass


async def _setup(hass):
    """Set up the full integration (all platforms forwarded) and return the coordinator."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry.runtime_data


async def test_print_buttons_gated_on_state(hass):
    coord = await _setup(hass)
    coord.async_send_command = AsyncMock()
    from custom_components.anycubic.button import BUTTONS, AnycubicButton
    btn = {d.key: AnycubicButton(coord, d) for d in BUTTONS}

    coord.data.printer = PrinterState(printing=True)
    assert btn["pause"].available and btn["stop"].available
    assert not btn["resume"].available
    await btn["pause"].async_press()
    coord.async_send_command.assert_awaited_with("pause")
    await btn["stop"].async_press()
    coord.async_send_command.assert_awaited_with("stop")

    coord.data.printer = PrinterState(paused=True)
    assert btn["resume"].available and btn["stop"].available
    assert not btn["pause"].available
    await btn["resume"].async_press()
    coord.async_send_command.assert_awaited_with("resume")

    # Nothing printing -> all three hidden.
    coord.data.printer = PrinterState()
    assert not any(btn[k].available for k in ("pause", "resume", "stop"))


async def test_printer_number_setpoints(hass):
    coord = await _setup(hass)
    coord.async_send_command = AsyncMock()
    from custom_components.anycubic.definitions import PRINTER_NUMBERS
    from custom_components.anycubic.number import AnycubicNumber
    num = {d.key: AnycubicNumber(coord, d) for d in PRINTER_NUMBERS}

    await num["nozzle_target"].async_set_native_value(250)
    coord.async_send_command.assert_awaited_with("set_nozzle_temp", value=250)
    assert coord.data.printer.nozzle_target == 250 and num["nozzle_target"].native_value == 250

    await num["box_fan"].async_set_native_value(40)
    coord.async_send_command.assert_awaited_with("set_box_fan", value=40)
    assert coord.data.printer.box_fan_level == 40


async def test_speed_mode_select(hass):
    coord = await _setup(hass)
    coord.async_send_command = AsyncMock()
    from custom_components.anycubic.select import AnycubicSpeedSelect
    sel = AnycubicSpeedSelect(coord)

    coord.data.printer = PrinterState(print_speed_mode=2)
    assert sel.current_option == "standard"
    await sel.async_select_option("sport")
    coord.async_send_command.assert_awaited_with("set_speed_mode", value=3)
    assert sel.current_option == "sport"


async def test_drying_numbers_feed_the_switch(hass):
    coord = await _setup(hass)
    from custom_components.anycubic.number import AnycubicDryingTempNumber, AnycubicDryingTimeNumber
    from custom_components.anycubic.switch import AnycubicAceDryingSwitch

    temp, dur = AnycubicDryingTempNumber(coord), AnycubicDryingTimeNumber(coord)
    # Setpoints must be available even with no ACE box reported yet (box is idle-gated),
    # so the user can dial them in BEFORE turning drying on.
    assert coord.data.ace == []
    assert temp.available and dur.available
    assert temp.native_value == 45 and dur.native_value == 4
    await temp.async_set_native_value(55)
    await dur.async_set_native_value(6)
    assert coord.drying_set_temp == 55 and coord.drying_set_hours == 6

    coord._apply("multiColorBox", {"multi_color_box": [{"id": 0, "temp": 30}]})
    coord.async_send_command = AsyncMock()
    sw = AnycubicAceDryingSwitch(coord)
    await sw.async_turn_on()
    coord.async_send_command.assert_awaited_with("drying_start", target_temp=55, duration=360)


async def test_auto_feed_switch(hass):
    coord = await _setup(hass)
    coord._apply("multiColorBox", {"multi_color_box": [{"id": 0, "auto_feed": 0, "temp": 30}]})
    coord.async_send_command = AsyncMock()
    from custom_components.anycubic.switch import AnycubicAceAutoFeedSwitch
    sw = AnycubicAceAutoFeedSwitch(coord)

    assert sw.is_on is False
    await sw.async_turn_on()
    coord.async_send_command.assert_awaited_with("auto_feed", on=True)
    assert sw.is_on is True
    await sw.async_turn_off()
    coord.async_send_command.assert_awaited_with("auto_feed", on=False)
    assert sw.is_on is False
