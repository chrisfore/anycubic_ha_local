"""ACE 2 switches: drying on/off and auto-feed."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AnycubicCoordinator
from .entity import AnycubicAceEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: AnycubicCoordinator = entry.runtime_data
    add([AnycubicAceDryingSwitch(coord), AnycubicAceAutoFeedSwitch(coord)])


class AnycubicAceDryingSwitch(AnycubicAceEntity, SwitchEntity):
    """Turn the box dryer on/off using the drying temperature/time setpoints."""

    _attr_translation_key = "ace_drying"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:weather-sunny"

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "drying")

    @property
    def is_on(self) -> bool:
        return bool(self._box and self._box.drying_active)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(
            "drying_start",
            target_temp=self.coordinator.drying_set_temp,
            duration=self.coordinator.drying_set_hours * 60,
        )
        self._set_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command("drying_stop")
        self._set_optimistic(False)

    def _set_optimistic(self, on: bool) -> None:
        """drying_status is activity-gated and only echoed on the next poll; reflect the
        change now so the switch holds, then let polls reconcile to real state."""
        if self._box is not None:
            self._box.drying_active = on
            self.coordinator.async_set_updated_data(self.coordinator.data)


class AnycubicAceAutoFeedSwitch(AnycubicAceEntity, SwitchEntity):
    """Automatic filament feed for the loaded slot."""

    _attr_translation_key = "ace_auto_feed"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:autorenew"

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "auto_feed")

    @property
    def is_on(self) -> bool:
        return bool(self._box and self._box.auto_feed)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command("auto_feed", on=True)
        self._set_optimistic(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command("auto_feed", on=False)
        self._set_optimistic(0)

    def _set_optimistic(self, value: int) -> None:
        if self._box is not None:
            self._box.auto_feed = value
            self.coordinator.async_set_updated_data(self.coordinator.data)
