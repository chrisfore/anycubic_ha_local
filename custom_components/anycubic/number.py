"""Printer setpoints (nozzle/bed target, fans) + ACE drying temperature/time setpoints."""
from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ENCLOSED_MODELS, box_has_dryer
from .coordinator import AnycubicCoordinator
from .definitions import PRINTER_NUMBERS, AnycubicNumberEntityDescription
from .entity import AnycubicAceEntity, AnycubicEntity, async_setup_ace_entities


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: AnycubicCoordinator = entry.runtime_data
    enclosed = coord.hs.model_id in ENCLOSED_MODELS
    add([AnycubicNumber(coord, d) for d in PRINTER_NUMBERS if enclosed or not d.enclosed_only])
    async_setup_ace_entities(
        entry, coord, add,
        lambda box_id: [AnycubicDryingTempNumber(coord, box_id),
                        AnycubicDryingTimeNumber(coord, box_id)] if box_has_dryer(box_id) else [])


class AnycubicNumber(AnycubicEntity, NumberEntity):
    """A live printer setpoint — reads the reported target, writes via print/update settings.

    Writable only while a job is running. These are *print-job* settings: the firmware applies
    them to the current task and silently discards them when there is none, so an idle write
    used to look like it worked and then revert on the next poll (issue #10). The value stays
    readable when idle — it is the printer's real reported target — but a write is refused
    with an explanation rather than sent into a void. min=0 turns the heater off.

    There is no idle preheat command in this protocol; `print`/`update` is the only way to
    move a setpoint, and it needs a task.
    """

    entity_description: AnycubicNumberEntityDescription

    def __init__(self, coordinator: AnycubicCoordinator, description: AnycubicNumberEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        return getattr(self.coordinator.data.printer, self.entity_description.attr)

    async def async_set_native_value(self, value: float) -> None:
        if not self.coordinator.job_active:
            raise HomeAssistantError(
                "The printer only accepts setpoint changes while a print is running. "
                "It ignores them when idle, so this would have had no effect.")
        await self.coordinator.async_send_command(self.entity_description.command, value=int(value))
        # Reflect immediately; the printer echoes the new target within a second. Safe now
        # that the command is only sent when the firmware will actually act on it.
        setattr(self.coordinator.data.printer, self.entity_description.attr, int(value))
        self.coordinator.async_set_updated_data(self.coordinator.data)


class _AnycubicDryingSetpoint(AnycubicAceEntity, NumberEntity):
    """Drying setpoint shown on the ACE device but NOT gated on box presence: the box is
    activity-gated (idle reports nothing), and these must be set BEFORE drying is turned on."""

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success


class AnycubicDryingTempNumber(_AnycubicDryingSetpoint):
    """Drying target temperature the drying switch uses when turned on."""

    _attr_translation_key = "drying_temp"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 35
    _attr_native_max_value = 65
    _attr_native_step = 5
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator: AnycubicCoordinator, box_id: int = 0) -> None:
        super().__init__(coordinator, "drying_temp", box_id)

    @property
    def native_value(self) -> float:
        return self.coordinator.drying_temp(self._box_id)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.set_drying_temp(self._box_id, int(value))
        self.coordinator.async_set_updated_data(self.coordinator.data)


class AnycubicDryingTimeNumber(_AnycubicDryingSetpoint):
    """Drying duration (hours) the drying switch uses when turned on."""

    _attr_translation_key = "drying_duration"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_native_min_value = 1
    _attr_native_max_value = 12
    _attr_native_step = 1
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator: AnycubicCoordinator, box_id: int = 0) -> None:
        super().__init__(coordinator, "drying_duration", box_id)

    @property
    def native_value(self) -> float:
        return self.coordinator.drying_hours(self._box_id)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.set_drying_hours(self._box_id, int(value))
        self.coordinator.async_set_updated_data(self.coordinator.data)
