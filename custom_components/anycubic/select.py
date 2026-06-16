"""Print speed mode select: silent / standard / sport (print_speed_mode 1/2/3)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AnycubicCoordinator
from .entity import AnycubicEntity

# Wire value <-> option. 1/2/3 confirmed from the live capture.
_MODES = {1: "silent", 2: "standard", 3: "sport"}
_VALUES = {v: k for k, v in _MODES.items()}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([AnycubicSpeedSelect(entry.runtime_data)])


class AnycubicSpeedSelect(AnycubicEntity, SelectEntity):
    _attr_translation_key = "speed_mode"
    _attr_icon = "mdi:speedometer"
    _attr_options = list(_MODES.values())

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "speed_mode")

    @property
    def current_option(self) -> str | None:
        return _MODES.get(self.coordinator.data.printer.print_speed_mode)

    async def async_select_option(self, option: str) -> None:
        value = _VALUES[option]
        await self.coordinator.async_send_command("set_speed_mode", value=value)
        self.coordinator.data.printer.print_speed_mode = value
        self.coordinator.async_set_updated_data(self.coordinator.data)
