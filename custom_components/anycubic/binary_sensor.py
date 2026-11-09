"""Printer binary sensors. (ACE drying is a switch — see switch.py — not a sensor here.)"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import PRINT_MODELS
from .coordinator import AnycubicCoordinator
from .definitions import PRINTER_BINARY_SENSORS, AnycubicBinaryEntityDescription
from .entity import AnycubicEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: AnycubicCoordinator = entry.runtime_data
    sensors: list[BinarySensorEntity] = [
        AnycubicBinarySensor(coord, d) for d in PRINTER_BINARY_SENSORS]
    if coord.hs.model_id in PRINT_MODELS:
        sensors.append(AnycubicBedFlagSensor(coord))
    add(sensors)


class AnycubicBinarySensor(AnycubicEntity, BinarySensorEntity):
    entity_description: AnycubicBinaryEntityDescription

    def __init__(self, coordinator, description) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return bool(self.entity_description.is_on_fn(self.coordinator.data.printer))


class AnycubicBedFlagSensor(AnycubicEntity, BinarySensorEntity):
    """On while Home Assistant will not start a print here: after any job, after any gap in
    the printer's reports, and whenever nothing is on record. Off only once someone has
    pressed "Bed is clear"."""

    _attr_translation_key = "bed_needs_clearing"
    _attr_icon = "mdi:printer-3d-nozzle-alert"

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "bed_needs_clearing")

    @property
    def is_on(self) -> bool:
        return self.coordinator.bed_needs_clearing
