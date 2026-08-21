"""ACE 2 switches: drying on/off and auto-feed."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import box_has_dryer
from .coordinator import AnycubicCoordinator
from .entity import AnycubicAceEntity, async_setup_ace_entities


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: AnycubicCoordinator = entry.runtime_data
    async_setup_ace_entities(
        entry, coord, add,
        lambda box_id: ([AnycubicAceDryingSwitch(coord, box_id)] if box_has_dryer(box_id) else [])
        + [AnycubicAceAutoFeedSwitch(coord, box_id)])


class AnycubicAceDryingSwitch(AnycubicAceEntity, SwitchEntity):
    """Turn the box dryer on/off using the drying temperature/time setpoints."""

    _attr_translation_key = "ace_drying"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:weather-sunny"

    def __init__(self, coordinator: AnycubicCoordinator, box_id: int = 0) -> None:
        super().__init__(coordinator, "drying", box_id)

    @property
    def is_on(self) -> bool:
        return bool(self._box and self._box.drying_active)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(
            "drying_start",
            target_temp=self.coordinator.drying_temp(self._box_id),
            duration=self.coordinator.drying_hours(self._box_id) * 60,
            box_id=self._box_id,
        )
        self._set_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command("drying_stop", box_id=self._box_id)
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

    def __init__(self, coordinator: AnycubicCoordinator, box_id: int = 0) -> None:
        super().__init__(coordinator, "auto_feed", box_id)

    @property
    def is_on(self) -> bool:
        return bool(self._box and self._box.auto_feed)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command("auto_feed", on=True, box_id=self._box_id)
        self._set_optimistic(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command("auto_feed", on=False, box_id=self._box_id)
        self._set_optimistic(0)

    def _set_optimistic(self, value: int) -> None:
        if self._box is not None:
            self._box.auto_feed = value
            self.coordinator.async_set_updated_data(self.coordinator.data)
