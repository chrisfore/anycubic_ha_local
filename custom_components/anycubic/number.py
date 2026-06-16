"""Printer setpoints (nozzle/bed target, fans) + ACE drying temperature/time setpoints."""
from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ENCLOSED_MODELS
from .coordinator import AnycubicCoordinator
from .definitions import PRINTER_NUMBERS, AnycubicNumberEntityDescription
from .entity import AnycubicAceEntity, AnycubicEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: AnycubicCoordinator = entry.runtime_data
    enclosed = coord.hs.model_id in ENCLOSED_MODELS
    entities: list = [
        AnycubicNumber(coord, d) for d in PRINTER_NUMBERS if enclosed or not d.enclosed_only
    ]
    entities += [AnycubicDryingTempNumber(coord), AnycubicDryingTimeNumber(coord)]
    add(entities)


class AnycubicNumber(AnycubicEntity, NumberEntity):
    """A live printer setpoint — reads the reported target, writes via print/update settings.

    Note: the temperature setpoints (nozzle/bed) are intentionally always writable so the user
    can preheat or change filament while idle — they command the heaters even with no job running.
    min=0 turns the heater off. The fans are harmless. This is a reviewed decision, not an oversight.
    """

    entity_description: AnycubicNumberEntityDescription

    def __init__(self, coordinator: AnycubicCoordinator, description: AnycubicNumberEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        return getattr(self.coordinator.data.printer, self.entity_description.attr)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_send_command(self.entity_description.command, value=int(value))
        # Reflect immediately; the printer echoes the new target on the next poll.
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

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "drying_temp")

    @property
    def native_value(self) -> float:
        return self.coordinator.drying_set_temp

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.drying_set_temp = int(value)
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

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "drying_duration")

    @property
    def native_value(self) -> float:
        return self.coordinator.drying_set_hours

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.drying_set_hours = int(value)
        self.coordinator.async_set_updated_data(self.coordinator.data)
