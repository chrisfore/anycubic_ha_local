"""Printer binary sensors. (ACE drying is a switch — see switch.py — not a sensor here.)"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AnycubicCoordinator
from .definitions import AnycubicBinaryEntityDescription, PRINTER_BINARY_SENSORS
from .entity import AnycubicEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: AnycubicCoordinator = entry.runtime_data
    add([AnycubicBinarySensor(coord, d) for d in PRINTER_BINARY_SENSORS])


class AnycubicBinarySensor(AnycubicEntity, BinarySensorEntity):
    entity_description: AnycubicBinaryEntityDescription

    def __init__(self, coordinator, description) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return bool(self.entity_description.is_on_fn(self.coordinator.data.printer))

