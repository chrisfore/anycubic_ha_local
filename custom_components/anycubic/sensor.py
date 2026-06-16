"""Printer sensors and ACE 2 box + slot sensors."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ACE_SLOT_COUNT
from .coordinator import AnycubicCoordinator
from .definitions import (
    ACE_SENSORS,
    AceSensorEntityDescription,
    AnycubicSensorEntityDescription,
    PRINTER_SENSORS,
    slot_attributes,
)
from .entity import AnycubicAceEntity, AnycubicEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: AnycubicCoordinator = entry.runtime_data
    entities: list = [AnycubicSensor(coord, d) for d in PRINTER_SENSORS]
    entities += [AnycubicAceBoxSensor(coord, d) for d in ACE_SENSORS]
    entities += [AnycubicAceSlotSensor(coord, i) for i in range(1, ACE_SLOT_COUNT + 1)]
    add(entities)


class AnycubicSensor(AnycubicEntity, SensorEntity):
    entity_description: AnycubicSensorEntityDescription

    def __init__(self, coordinator: AnycubicCoordinator, description: AnycubicSensorEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.data.printer)


class AnycubicAceBoxSensor(AnycubicAceEntity, SensorEntity):
    entity_description: AceSensorEntityDescription

    def __init__(self, coordinator, description):
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return None if self._box is None else self.entity_description.value_fn(self._box)


class AnycubicAceSlotSensor(AnycubicAceEntity, SensorEntity):
    _attr_translation_key = "ace_slot"

    def __init__(self, coordinator, slot_index: int):
        super().__init__(coordinator, f"slot_{slot_index}")
        self._slot_index = slot_index
        self._attr_translation_placeholders = {"n": str(slot_index)}

    @property
    def _slot(self):
        return None if self._box is None else self._box.slots.get(self._slot_index)

    @property
    def native_value(self):
        s = self._slot
        return None if s is None else (s.material or "Empty")

    @property
    def extra_state_attributes(self):
        return slot_attributes(self._slot)
