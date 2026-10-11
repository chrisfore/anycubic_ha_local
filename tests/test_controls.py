from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic.anycubic_local.handshake import HandshakeResult
from custom_components.anycubic.anycubic_local.models import PrinterState
from custom_components.anycubic.const import DOMAIN

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
    coord.data.printer = PrinterState(printing=True)

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

    coord.data.printer = PrinterState(print_speed_mode=2, printing=True)
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
    assert coord.drying_temp(0) == 55 and coord.drying_hours(0) == 6

    coord._apply("multiColorBox", {"multi_color_box": [{"id": 0, "temp": 30}]})
    coord.async_send_command = AsyncMock()
    sw = AnycubicAceDryingSwitch(coord)
    await sw.async_turn_on()
    coord.async_send_command.assert_awaited_with(
        "drying_start", target_temp=55, duration=360, box_id=0)


async def test_auto_feed_switch(hass):
    coord = await _setup(hass)
    coord._apply("multiColorBox", {"multi_color_box": [{"id": 0, "auto_feed": 0, "temp": 30}]})
    coord.async_send_command = AsyncMock()
    from custom_components.anycubic.switch import AnycubicAceAutoFeedSwitch
    sw = AnycubicAceAutoFeedSwitch(coord)

    assert sw.is_on is False
    await sw.async_turn_on()
    coord.async_send_command.assert_awaited_with("auto_feed", on=True, box_id=0)
    assert sw.is_on is True
    await sw.async_turn_off()
    coord.async_send_command.assert_awaited_with("auto_feed", on=False, box_id=0)
    assert sw.is_on is False


async def test_setpoints_are_refused_when_no_job_is_running(hass):
    """Idle, the printer discards a print/update settings command without acking it —
    confirmed on a Kobra 3. Refuse loudly instead of pretending it worked (issue #10)."""
    coord = await _setup(hass)
    coord.async_send_command = AsyncMock()
    from custom_components.anycubic.definitions import PRINTER_NUMBERS
    from custom_components.anycubic.number import AnycubicNumber
    from custom_components.anycubic.select import AnycubicSpeedSelect

    coord.data.printer = PrinterState(nozzle_target=0, printing=False, paused=False)
    num = {d.key: AnycubicNumber(coord, d) for d in PRINTER_NUMBERS}

    for key in ("nozzle_target", "bed_target", "fan_speed", "aux_fan", "box_fan"):
        with pytest.raises(HomeAssistantError):
            await num[key].async_set_native_value(60)
    with pytest.raises(HomeAssistantError):
        await AnycubicSpeedSelect(coord).async_select_option("sport")

    # Nothing was published, and the displayed target was not moved behind the user's back.
    coord.async_send_command.assert_not_awaited()
    assert coord.data.printer.nozzle_target == 0
    # Still readable while idle — it is the printer's real reported target.
    assert num["nozzle_target"].available


async def test_setpoints_are_writable_while_paused(hass):
    """A paused job still has a task, so the settings update still applies."""
    coord = await _setup(hass)
    coord.async_send_command = AsyncMock()
    from custom_components.anycubic.definitions import PRINTER_NUMBERS
    from custom_components.anycubic.number import AnycubicNumber

    coord.data.printer = PrinterState(printing=False, paused=True)
    num = {d.key: AnycubicNumber(coord, d) for d in PRINTER_NUMBERS}
    await num["nozzle_target"].async_set_native_value(210)
    coord.async_send_command.assert_awaited_with("set_nozzle_temp", value=210)


# ------------------------------------------------- a write is not a poll
#
# Every control reflects its change at once, ahead of the printer's own report. It used to
# do that through async_set_updated_data, which is Home Assistant's "a poll has just
# succeeded": it cancels the refresh timer and starts it again, and marks the last update
# good. So an automation writing more often than the poll interval stopped the polls
# altogether, and the session watchdog with them, since recovery runs inside the poll; and a
# write to a printer that could not be reached brought every entity back as available, with
# the values it had when it was lost.


def _writes(coord):
    """Each control's write, as (name, coroutine function)."""
    from custom_components.anycubic.definitions import PRINTER_NUMBERS
    from custom_components.anycubic.light import AnycubicLight
    from custom_components.anycubic.number import (
        AnycubicDryingTempNumber,
        AnycubicDryingTimeNumber,
        AnycubicNumber,
    )
    from custom_components.anycubic.select import AnycubicSpeedSelect
    from custom_components.anycubic.switch import AnycubicAceAutoFeedSwitch, AnycubicAceDryingSwitch

    nozzle = next(d for d in PRINTER_NUMBERS if d.key == "nozzle_target")
    return [
        ("light", lambda: AnycubicLight(coord).async_turn_on()),
        ("speed mode", lambda: AnycubicSpeedSelect(coord).async_select_option("sport")),
        ("setpoint", lambda: AnycubicNumber(coord, nozzle).async_set_native_value(215)),
        ("drying temperature", lambda: AnycubicDryingTempNumber(coord).async_set_native_value(55)),
        ("drying time", lambda: AnycubicDryingTimeNumber(coord).async_set_native_value(6)),
        ("drying", lambda: AnycubicAceDryingSwitch(coord).async_turn_on()),
        ("auto feed", lambda: AnycubicAceAutoFeedSwitch(coord).async_turn_on()),
    ]


async def _setup_for_writes(hass):
    coord = await _setup(hass)
    coord._apply("multiColorBox", {"multi_color_box": [{"id": 0, "auto_feed": 0, "temp": 30}]})
    coord.data.printer = PrinterState(printing=True, print_speed_mode=2)
    coord.async_send_command = AsyncMock()
    return coord


async def test_a_write_does_not_push_out_the_next_poll(hass):
    coord = await _setup_for_writes(hass)
    # HA stores the poll timer as TimerHandle.cancel, so __self__ is the handle itself.
    # A reschedule replaces the handle; keeping the same one is the assertion.
    handle = coord._unsub_refresh.__self__
    for name, write in _writes(coord):
        await write()
        assert coord._unsub_refresh.__self__ is handle, f"the {name} write re-armed the poll"
    assert coord.async_send_command.await_count == 5     # the two setpoints send nothing


async def test_a_write_does_not_bring_an_unreachable_printer_back(hass):
    coord = await _setup_for_writes(hass)
    coord.last_update_success = False            # the watchdog has given up on the session
    for name, write in _writes(coord):
        await write()
        assert coord.last_update_success is False, f"the {name} write marked the printer reachable"


async def test_a_write_still_reaches_the_listeners_at_once(hass):
    coord = await _setup_for_writes(hass)
    heard = []
    coord.async_add_listener(lambda: heard.append(1))
    for count, (name, write) in enumerate(_writes(coord), start=1):
        await write()
        assert len(heard) == count, f"the {name} write did not notify the listeners"
    assert coord.data.light.on is True
    assert coord.data.printer.print_speed_mode == 3 and coord.data.printer.nozzle_target == 215
    assert coord.drying_temp(0) == 55 and coord.drying_hours(0) == 6
    assert coord.data.ace[0].drying_active is True and coord.data.ace[0].auto_feed == 1
