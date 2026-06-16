"""Chamber light — on/off (the printer's chamber LED is not dimmable)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .anycubic_local.models import LightState
from .const import ENCLOSED_MODELS
from .coordinator import AnycubicCoordinator
from .entity import AnycubicEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    # The chamber light is enclosure hardware (KS1 / KS1 Max only).
    coord: AnycubicCoordinator = entry.runtime_data
    if coord.hs.model_id in ENCLOSED_MODELS:
        add([AnycubicLight(coord)])


class AnycubicLight(AnycubicEntity, LightEntity):
    _attr_translation_key = "chamber_light"
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "chamber_light")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.light.on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command("light", on=True)
        self._set_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command("light", on=False)
        self._set_optimistic(False)

    def _set_optimistic(self, on: bool) -> None:
        """The printer only echoes light state on the next poll; reflect the change now so the
        toggle holds and the icon updates immediately. The next poll reconciles to real state."""
        self.coordinator.data.light = LightState(on=on)
        self.coordinator.async_set_updated_data(self.coordinator.data)
