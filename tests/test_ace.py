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
        # Printer reports slots 0-indexed (0..3); loaded_slot 3 = physical slot 4.
        coord._apply("multiColorBox", {"multi_color_box": [{
            "id": 0, "humidity": 24, "temp": 35, "loaded_slot": 3,
            "drying_status": {"status": 0},
            "slots": [{"index": 0, "type": "PETG", "color": [67, 82, 59],
                       "status": 5, "consumables_percent": 95},
                      {"index": 3, "type": "TPU", "color": [10, 20, 30],
                       "status": 5, "consumables_percent": 50}]}]})
        await hass.async_block_till_done()

    assert hass.states.get("sensor.ace_2_humidity").state == "24"
    assert hass.states.get("sensor.ace_2_box_temperature").state == "35"
    # loaded_slot 3 -> displayed as slot 4
    assert hass.states.get("sensor.ace_2_loaded_slot").state == "4"
    assert hass.states.get("switch.ace_2_drying").state == "off"
    # Slot 1 maps to printer index 0; Slot 4 maps to printer index 3 (the off-by-one fix).
    slot1 = hass.states.get("sensor.ace_2_slot_1")
    assert slot1.state == "PETG"
    assert slot1.attributes["remaining"] == 95
    assert slot1.attributes["color"] == "#43523B"
    assert hass.states.get("sensor.ace_2_slot_4").state == "TPU"
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
        coord.async_send_command.assert_awaited_with(
            "drying_start", target_temp=45, duration=240, box_id=0)
        await hass.async_block_till_done()
        assert hass.states.get("switch.ace_2_drying").state == "on"

        # A later report that omits drying_status must NOT flip it back off.
        coord._apply("multiColorBox", {"multi_color_box": [{"id": 0, "temp": 31}]})
        await hass.async_block_till_done()
        assert hass.states.get("switch.ace_2_drying").state == "on"

        # Turn off -> sends drying_stop and flips optimistically.
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.ace_2_drying"}, blocking=True)
        coord.async_send_command.assert_awaited_with("drying_stop", box_id=0)
        await hass.async_block_till_done()
        assert hass.states.get("switch.ace_2_drying").state == "off"


async def test_second_ace_box_gets_its_own_device_and_entities(hass):
    # Issue #4: a printer can have two ACE units attached. Every reported box id must
    # surface as its own device with the full entity set — not just box 0.
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        # First report: box 0 only (second unit plugged in / reported later).
        coord._apply("multiColorBox", {"multi_color_box": [{
            "id": 0, "model_id": 40001, "humidity": 24, "temp": 35,
            "slots": [{"index": 0, "type": "PLA", "color": [255, 0, 0],
                       "status": 5, "consumables_percent": 80}]}]})
        await hass.async_block_till_done()
        assert hass.states.get("sensor.ace_2_humidity").state == "24"

        # Later report carries both boxes.
        coord._apply("multiColorBox", {"multi_color_box": [
            {"id": 0, "model_id": 40001, "humidity": 24, "temp": 35, "slots": []},
            {"id": 1, "model_id": 40001, "humidity": 31, "temp": 33, "loaded_slot": 0,
             "slots": [{"index": 0, "type": "ASA", "color": [0, 0, 255],
                        "status": 5, "consumables_percent": 60}]},
        ]})
        await hass.async_block_till_done()

    registry = dr.async_get(hass)
    # Box 0: identity totally unchanged (upgrades must not break dashboards/history).
    assert registry.async_get_device(identifiers={(DOMAIN, "SER-1_ace0")}) is not None
    assert hass.states.get("sensor.ace_2_humidity").state == "24"
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get("sensor.ace_2_humidity").unique_id == "SER-1_ace0_humidity"

    # Box 1: own device, numbered after its model, linked to the printer.
    dev1 = registry.async_get_device(identifiers={(DOMAIN, "SER-1_ace1")})
    assert dev1 is not None and dev1.name == "ACE Pro #2" and dev1.via_device_id is not None
    assert hass.states.get("sensor.ace_pro_2_humidity").state == "31"
    assert hass.states.get("sensor.ace_pro_2_box_temperature").state == "33"
    assert hass.states.get("sensor.ace_pro_2_loaded_slot").state == "1"
    slot1 = hass.states.get("sensor.ace_pro_2_slot_1")
    assert slot1.state == "ASA"
    assert slot1.attributes["remaining"] == 60
    assert ent_reg.async_get("sensor.ace_pro_2_humidity").unique_id == "SER-1_ace1_humidity"


async def test_second_box_controls_target_box_1(hass):
    # Controls on the second box must command box id 1 — and use its own drying setpoints.
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("multiColorBox", {"multi_color_box": [
            {"id": 0, "model_id": 40001, "temp": 30, "auto_feed": 0},
            {"id": 1, "model_id": 40001, "temp": 31, "auto_feed": 0},
        ]})
        await hass.async_block_till_done()
        coord.async_send_command = AsyncMock()

        # Box-1 setpoints are independent of box 0's.
        coord.set_drying_temp(1, 55)
        coord.set_drying_hours(1, 6)
        assert coord.drying_temp(0) == 45 and coord.drying_hours(0) == 4

        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.ace_pro_2_drying"}, blocking=True)
        coord.async_send_command.assert_awaited_with(
            "drying_start", target_temp=55, duration=360, box_id=1)
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.ace_pro_2_drying"}, blocking=True)
        coord.async_send_command.assert_awaited_with("drying_stop", box_id=1)

        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.ace_pro_2_auto_feed"}, blocking=True)
        coord.async_send_command.assert_awaited_with("auto_feed", on=True, box_id=1)

        # Box 0 keeps commanding box id 0 with its own (default) setpoints.
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.ace_2_drying"}, blocking=True)
        coord.async_send_command.assert_awaited_with(
            "drying_start", target_temp=45, duration=240, box_id=0)


async def test_second_box_renamed_when_model_arrives_late(hass):
    # A second box that first reports without model_id registers under the generic
    # "ACE #2"; once the model arrives, the display name/model update (ids stay put).
    from homeassistant.helpers import device_registry as dr

    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        registry = dr.async_get(hass)
        coord._apply("multiColorBox", {"multi_color_box": [
            {"id": 0, "temp": 30}, {"id": 1, "temp": 31}]})
        await hass.async_block_till_done()
        dev1 = registry.async_get_device(identifiers={(DOMAIN, "SER-1_ace1")})
        assert dev1 is not None and dev1.name == "ACE #2"

        coord._apply("multiColorBox", {"multi_color_box": [
            {"id": 1, "model_id": 40002, "temp": 31}]})
        await hass.async_block_till_done()
        dev1 = registry.async_get_device(identifiers={(DOMAIN, "SER-1_ace1")})
        assert dev1.name == "ACE 2 #2"
        assert dev1.model == "ACE 2"


async def test_ace_device_renamed_to_reported_model(hass):
    # The ACE device registers as "ACE 2" (before the box reports) so entity IDs are
    # deterministic, but once the box reports its model_id the device display name and
    # model must reflect the real hardware (issue #3: an ACE Pro showed as "ACE 2").
    from homeassistant.helpers import device_registry as dr

    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        registry = dr.async_get(hass)
        dev = registry.async_get_device(identifiers={(DOMAIN, "SER-1_ace0")})
        assert dev is not None and dev.name == "ACE 2"

        coord._apply("multiColorBox", {"multi_color_box": [{
            "id": 0, "model_id": 40001, "humidity": 24, "temp": 35, "slots": []}]})
        await hass.async_block_till_done()

        dev = registry.async_get_device(identifiers={(DOMAIN, "SER-1_ace0")})
        assert dev.name == "ACE Pro"
        assert dev.model == "ACE Pro"
        # Entity IDs stay on the registration-time name — dashboards keep working.
        assert hass.states.get("sensor.ace_2_humidity").state == "24"


async def test_ace_device_user_rename_respected(hass):
    # If the user renamed the device in the UI, the model-based rename must not clobber it.
    from homeassistant.helpers import device_registry as dr

    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        registry = dr.async_get(hass)
        dev = registry.async_get_device(identifiers={(DOMAIN, "SER-1_ace0")})
        registry.async_update_device(dev.id, name_by_user="Filament box")
        coord._apply("multiColorBox", {"multi_color_box": [{
            "id": 0, "model_id": 40001, "humidity": 24, "temp": 35, "slots": []}]})
        await hass.async_block_till_done()
        dev = registry.async_get_device(identifiers={(DOMAIN, "SER-1_ace0")})
        assert dev.name_by_user == "Filament box"
        assert dev.model == "ACE Pro"


HS_KX = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20030", "SER-KX")

# The Kobra X's 4-color changer is built into the toolhead (Anycubic's "ACE Gen 2"), not an
# attached box: it reports id -1 with head_tools_model 1, where an external ACE reports id 0.
KX_BUILTIN = {"head_tools_model": 1, "multi_color_box": [{
    "id": -1, "model_id": 40002, "status": 1, "temp": 0, "humidity": 0, "loaded_slot": -1,
    "slots": [{"index": 0, "type": "PLA", "color": [255, 255, 255],
               "status": 5, "consumables_percent": 100}]}]}


async def _setup_kobra_x(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-KX", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS_KX), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry.runtime_data


def _ace_devices(hass, serial):
    from homeassistant.helpers import device_registry as dr

    return [d for d in dr.async_get(hass).devices.values()
            if any(i[1].startswith(f"{serial}_ace") for i in d.identifiers)]


async def test_builtin_multicolor_unit_registers_once(hass):
    """Issue #8, Kobra X: the built-in changer reports as box -1, so pre-registering
    box 0 spawned a *second*, permanently-dead "ACE 2" device beside the real one.
    A printer with one multi-material unit must produce exactly one device."""
    from homeassistant.helpers import device_registry as dr

    coord = await _setup_kobra_x(hass)
    coord._apply("multiColorBox", KX_BUILTIN)
    await hass.async_block_till_done()

    assert len(_ace_devices(hass, "SER-KX")) == 1
    registry = dr.async_get(hass)
    assert registry.async_get_device(identifiers={(DOMAIN, "SER-KX_ace0")}) is None
    dev = registry.async_get_device(identifiers={(DOMAIN, "SER-KX_ace-1")})
    # Named for what it is: the user owns no separate ACE 2 box.
    assert dev is not None and dev.name == "Multi-color unit"
    assert dev.via_device_id is not None
    assert hass.states.get("sensor.multi_color_unit_slot_1").state == "PLA"


async def test_builtin_unit_commands_target_its_own_box_id(hass):
    """The built-in unit keeps its real id on the wire — commands must carry -1, not 0."""
    coord = await _setup_kobra_x(hass)
    coord._apply("multiColorBox", KX_BUILTIN)
    await hass.async_block_till_done()
    coord.async_send_command = AsyncMock()

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.multi_color_unit_auto_feed"}, blocking=True)
    coord.async_send_command.assert_awaited_with("auto_feed", on=True, box_id=-1)


async def test_kobra_x_external_box_adds_its_own_device(hass):
    """The Kobra X expands with external ACE 2 Pro units; those still report as
    attached boxes (id 0+) and must each get their own device."""
    from homeassistant.helpers import device_registry as dr

    coord = await _setup_kobra_x(hass)
    coord._apply("multiColorBox", {"head_tools_model": 1, "multi_color_box": [
        KX_BUILTIN["multi_color_box"][0],
        {"id": 0, "model_id": 40002, "temp": 30, "humidity": 20, "slots": []},
    ]})
    await hass.async_block_till_done()

    assert len(_ace_devices(hass, "SER-KX")) == 2
    registry = dr.async_get(hass)
    assert registry.async_get_device(
        identifiers={(DOMAIN, "SER-KX_ace-1")}).name == "Multi-color unit"
    assert registry.async_get_device(identifiers={(DOMAIN, "SER-KX_ace0")}).name == "ACE 2"


async def test_stale_box_device_is_deletable_but_live_ones_are_not(hass):
    """A pre-fix install on a Kobra X left a phantom box-0 device (issue #8). Without
    async_remove_config_entry_device Home Assistant hides the Delete button, so the user
    is stuck with it — but a device the printer still reports must not be removable."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.anycubic import async_remove_config_entry_device

    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-KX", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS_KX), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("multiColorBox", KX_BUILTIN)
        await hass.async_block_till_done()

    registry = dr.async_get(hass)
    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "SER-KX_ace0")}, name="ACE 2")
    printer = registry.async_get_device(identifiers={(DOMAIN, "SER-KX")})
    live_box = registry.async_get_device(identifiers={(DOMAIN, "SER-KX_ace-1")})

    assert await async_remove_config_entry_device(hass, entry, stale) is True
    assert await async_remove_config_entry_device(hass, entry, printer) is False
    assert await async_remove_config_entry_device(hass, entry, live_box) is False


async def test_builtin_unit_has_no_dryer_entities(hass):
    """The Kobra X's built-in changer is a feeder, not a dry box — its owner confirmed
    the spools "just hang loosely on top of the printer" (issue #8), and it reports a
    constant 0 humidity/temperature. Dryer controls and dry-box sensors there would be
    permanently meaningless, so they are not created. Feeding entities still are."""
    coord = await _setup_kobra_x(hass)
    coord._apply("multiColorBox", KX_BUILTIN)
    await hass.async_block_till_done()

    g = hass.states.get
    assert g("switch.multi_color_unit_drying") is None
    assert g("number.multi_color_unit_drying_temperature") is None
    assert g("number.multi_color_unit_drying_time") is None
    assert g("sensor.multi_color_unit_humidity") is None
    assert g("sensor.multi_color_unit_box_temperature") is None
    # What the unit really does: feed filament from four slots.
    assert g("switch.multi_color_unit_auto_feed") is not None
    assert g("sensor.multi_color_unit_loaded_slot") is not None
    assert g("sensor.multi_color_unit_slot_1").state == "PLA"


async def test_external_box_on_builtin_printer_keeps_its_dryer(hass):
    """Gating is per box, not per printer: a Kobra X expanded with an external ACE 2 Pro
    has a real dry box on that unit."""
    coord = await _setup_kobra_x(hass)
    coord._apply("multiColorBox", {"head_tools_model": 1, "multi_color_box": [
        KX_BUILTIN["multi_color_box"][0],
        {"id": 0, "model_id": 40002, "temp": 30, "humidity": 20,
         "drying_status": {"status": 0}, "slots": []},
    ]})
    await hass.async_block_till_done()

    g = hass.states.get
    assert g("switch.ace_2_drying") is not None
    assert g("number.ace_2_drying_temperature") is not None
    assert g("sensor.ace_2_humidity").state == "20"
    assert g("sensor.ace_2_box_temperature").state == "30"


async def test_ace_sensors_go_unavailable_when_the_unit_is_detached(hass):
    # Reported on #12: switching the ACE off left its sensors sitting on stale values.
    # merge_boxes deliberately keeps boxes it has seen, so data.ace never empties and
    # `self._box is not None` stayed true — the mirror of the external-spool staleness.
    # With the unit attached, getInfo answers with a full box list; detached, an empty
    # one. So an empty list means absent, and availability must follow it.
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("multiColorBox", {"multi_color_box": [
            {"id": 0, "model_id": 40001, "humidity": 24, "temp": 35, "slots": []}]})
        await hass.async_block_till_done()
        assert hass.states.get("sensor.ace_2_humidity").state == "24"

        coord._apply("multiColorBox", {"multi_color_box": []})
        await hass.async_block_till_done()

    assert hass.states.get("sensor.ace_2_humidity").state == "unavailable"
    # The device itself must survive — dropping the box would destroy its entity IDs.
    from homeassistant.helpers import device_registry as dr
    assert dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "SER-1_ace0")}) is not None


# ------------------------------------------- the device registry, old way and new way
#
# Home Assistant is retiring `via_device` (an identifier, which several config entries can
# now share) for `via_device_id`, and `async_get_device` for a lookup that names the config
# entry. The tests above run the old way on whatever is installed. These stand in for a
# Home Assistant that has the new one, so both are exercised whichever is installed.

async def _setup_with_two_boxes(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entry.runtime_data._apply("multiColorBox", {"multi_color_box": [
            {"id": 0, "temp": 30}, {"id": 1, "temp": 31}]})
        await hass.async_block_till_done()
    return entry


def _new_registry_lookup(registry, asked):
    """The lookup newer Home Assistant has, over the same registry; the old one refuses."""
    old = registry.devices.get_entry

    def by_identifier(identifier, config_entry_id):
        asked.append((identifier, config_entry_id))
        device = old({identifier}, None)
        return device if device and config_entry_id in device.config_entries else None

    def retired(*args, **kwargs):
        raise AssertionError("async_get_device is deprecated; it must not be called")

    return (patch.object(type(registry), "async_get_device_by_identifier",
                         staticmethod(by_identifier), create=True),
            patch.object(type(registry), "async_get_device", retired))


async def test_a_box_is_renamed_through_the_lookup_that_names_the_config_entry(hass):
    from homeassistant.helpers import device_registry as dr

    entry = await _setup_with_two_boxes(hass)
    registry = dr.async_get(hass)
    box = registry.devices.get_entry({(DOMAIN, "SER-1_ace1")}, None)
    assert box.name == "ACE #2"
    asked = []
    new, old = _new_registry_lookup(registry, asked)
    with new, old:
        entry.runtime_data._apply("multiColorBox", {"multi_color_box": [
            {"id": 1, "model_id": 40002, "temp": 31}]})
        await hass.async_block_till_done()
    assert ((DOMAIN, "SER-1_ace1"), entry.entry_id) in asked
    assert registry.devices.get_entry({(DOMAIN, "SER-1_ace1")}, None).name == "ACE 2 #2"


async def test_a_box_links_to_the_printer_by_device_id_where_home_assistant_wants_that(hass, monkeypatch):
    from homeassistant.helpers import device_registry as dr

    from custom_components.anycubic import entity as entity_mod

    entry = await _setup_with_two_boxes(hass)
    registry = dr.async_get(hass)
    printer = registry.devices.get_entry({(DOMAIN, "SER-1")}, None)
    box = entity_mod.AnycubicAceEntity(entry.runtime_data, "probe", box_id=1)

    # As installed here or on 2024.9: by identifier, the only way there is.
    monkeypatch.setattr(entity_mod, "_VIA_BY_ID", False)
    assert box.device_info["via_device"] == (DOMAIN, "SER-1")
    assert "via_device_id" not in box.device_info

    monkeypatch.setattr(entity_mod, "_VIA_BY_ID", True)
    asked = []
    new, old = _new_registry_lookup(registry, asked)
    with new, old:
        info = box.device_info
        assert info["via_device_id"] == printer.id and "via_device" not in info
        assert asked == [((DOMAIN, "SER-1"), entry.entry_id)]
        # A printer that is not registered yet is not linked to, rather than named by an id
        # that does not exist, which newer Home Assistant refuses outright.
        registry.async_remove_device(printer.id)
        info = box.device_info
        assert "via_device_id" not in info and "via_device" not in info


def test_the_way_to_link_a_box_is_read_from_home_assistant_itself():
    from homeassistant.helpers.device_registry import DeviceInfo

    from custom_components.anycubic import entity as entity_mod

    assert entity_mod._VIA_BY_ID is ("via_device_id" in DeviceInfo.__annotations__)


# ------------------------------------------------- "no box" is not a box (id -1 as a sentinel)
#
# When a filament feed or unload finishes, a printer with an external box reports
# multiColorBox once with a single entry whose id is -1: no box, nothing loaded. Read as a
# box, it was merged and registered, and because negative ids are how the Kobra X reports
# the changer built into its toolhead, a printer that has no such thing grew a "Multi-color
# unit" device with six entities. The shape below is that report's `data`, as captured.

FEED_DONE = {"multi_color_box": [{
    "id": -1, "loaded_slot": -1,
    "feed_status": {"type": 2, "current_status": 1, "slot_index": -1}}]}
BOX_0 = {"multi_color_box": [{
    "id": 0, "model_id": 40002, "status": 1, "temp": 30, "humidity": 24, "loaded_slot": 1,
    "slots": [{"index": 0, "type": "PETG", "color": [67, 82, 59],
               "status": 5, "consumables_percent": 95}]}]}


async def _setup_s1_max(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry.runtime_data


async def test_the_no_box_sentinel_does_not_become_a_device(hass):
    import copy

    from homeassistant.helpers import device_registry as dr, entity_registry as er

    coord = await _setup_s1_max(hass)
    coord._apply("multiColorBox", BOX_0)
    await hass.async_block_till_done()
    box_before = copy.deepcopy(coord.data.ace)
    devices = set(dr.async_get(hass).devices)
    entities = set(er.async_get(hass).entities)
    states = {s.entity_id: s.state for s in hass.states.async_all()}

    coord._apply("multiColorBox", FEED_DONE)
    await hass.async_block_till_done()

    assert [box.id for box in coord.data.ace] == [0]
    assert coord.data.ace == box_before                 # loaded slot 1 is still loaded slot 1
    assert set(dr.async_get(hass).devices) == devices
    assert set(er.async_get(hass).entities) == entities
    assert len(_ace_devices(hass, "SER-1")) == 1
    assert {s.entity_id: s.state for s in hass.states.async_all()} == states
    # Kept as it came, for the diagnostics download: that is how this was found.
    assert coord.raw_multicolorbox == FEED_DONE


async def test_the_same_report_from_a_built_in_changer_is_that_changer(hass):
    # On a Kobra X, -1 is a real unit: the one in the toolhead.
    coord = await _setup_kobra_x(hass)
    coord._apply("multiColorBox", KX_BUILTIN)
    await hass.async_block_till_done()
    coord._apply("multiColorBox", FEED_DONE)
    await hass.async_block_till_done()
    assert [box.id for box in coord.data.ace] == [-1]
    assert coord.data.ace[0].feed_current_status == 1
    assert coord.ace_present is True
    assert len(_ace_devices(hass, "SER-KX")) == 1
    # And first heard of this way, it is still accepted.
    fresh = await _setup_kobra_x_again(hass)
    fresh._apply("multiColorBox", FEED_DONE)
    await hass.async_block_till_done()
    assert [box.id for box in fresh.data.ace] == [-1] and fresh.ace_present is True


async def _setup_kobra_x_again(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-KX2", data={"host": "1.2.3.5"})
    entry.add_to_hass(hass)
    other = HandshakeResult("1.2.3.5", 9883, "u", "p", "DEV2", "20030", "SER-KX2")
    with patch("custom_components.anycubic.do_handshake", return_value=other), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry.runtime_data


async def test_a_report_of_only_the_sentinel_says_nothing_is_attached(hass):
    # With no box attached the bare spool holder is in use, and a report that names no box
    # must neither announce one nor wipe the spool the holder reported.
    coord = await _setup_s1_max(hass)
    coord._apply("extfilbox", {"type": "PETG", "color": [117, 120, 123], "loaded": 1})
    await hass.async_block_till_done()
    spool = coord.data.external_spool
    assert spool is not None and spool.material == "PETG"

    coord._apply("multiColorBox", FEED_DONE)
    await hass.async_block_till_done()

    assert coord.ace_present is not True
    assert coord.data.external_spool is spool
    assert coord.data.ace == []
    assert len(_ace_devices(hass, "SER-1")) == 1        # the one pre-registered for box 0


async def test_a_box_entry_without_a_usable_id_is_skipped_and_the_rest_applied(hass):
    coord = await _setup_s1_max(hass)
    real = BOX_0["multi_color_box"][0]
    for junk in ({"temp": 99}, {"id": None, "temp": 99}, {"id": "0", "temp": 99},
                 {"id": True, "temp": 99}, {"id": 1.0, "temp": 99}, None, "box", 7):
        coord._apply("multiColorBox", {"multi_color_box": [junk, real]})
        await hass.async_block_till_done()
        assert [box.id for box in coord.data.ace] == [0], junk
        assert coord.data.ace[0].temp == 30 and coord.ace_present is True, junk
    # A report that is not shaped like one at all applies nothing and raises nothing.
    for data in ({"multi_color_box": None}, {"multi_color_box": {"id": 0}}, {}):
        coord._apply("multiColorBox", data)
    assert [box.id for box in coord.data.ace] == [0]
    assert len(_ace_devices(hass, "SER-1")) == 1
