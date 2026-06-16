"""Printer binary sensors and ACE 2 drying binary sensor."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AnycubicCoordinator
from .definitions import ACE_BINARY_SENSORS, AceBinaryEntityDescription, AnycubicBinaryEntityDescription, PRINTER_BINARY_SENSORS
from .entity import AnycubicAceEntity, AnycubicEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: AnycubicCoordinator = entry.runtime_data
    entities = [AnycubicBinarySensor(coord, d) for d in PRINTER_BINARY_SENSORS]
    entities += [AnycubicAceBinarySensor(coord, d) for d in ACE_BINARY_SENSORS]
    add(entities)


class AnycubicBinarySensor(AnycubicEntity, BinarySensorEntity):
    entity_description: AnycubicBinaryEntityDescription

    def __init__(self, coordinator, description) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return bool(self.entity_description.is_on_fn(self.coordinator.data.printer))


class AnycubicAceBinarySensor(AnycubicAceEntity, BinarySensorEntity):
    entity_description: AceBinaryEntityDescription

    def __init__(self, coordinator, description):
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self):
        return None if self._box is None else bool(self.entity_description.is_on_fn(self._box))
