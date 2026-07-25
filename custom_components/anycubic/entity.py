"""Base entity for AnyCubic — links every entity to the printer device."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ACE_MODEL_NAMES, DOMAIN, MANUFACTURER, MODEL_NAMES, ace_suffix
from .coordinator import AnycubicCoordinator


class AnycubicEntity(CoordinatorEntity[AnycubicCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: AnycubicCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.hs.serial}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        p = self.coordinator.data.printer
        info = DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.hs.serial)},
            manufacturer=MANUFACTURER,
            name=MODEL_NAMES.get(self.coordinator.hs.model_id) or p.model or "AnyCubic printer",
            model=MODEL_NAMES.get(self.coordinator.hs.model_id),
            sw_version=p.firmware,
            configuration_url=f"http://{self.coordinator.host}",
        )
        if self.coordinator.hs.mac:
            info["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(self.coordinator.hs.mac))}
        return info


def ace_device_name(box_id: int, model_id) -> str:
    """Display name for an ACE box device.

    Box 0 keeps the literal "ACE 2" until its model is known, so entity IDs minted at
    registration stay deterministic (ace_2_*). Additional boxes only register after
    they have reported, so they can carry the real model plus a unit number ("ACE Pro #2").
    """
    model = ACE_MODEL_NAMES.get(str(model_id)) if model_id is not None else None
    if box_id == 0:
        return model or "ACE 2"
    return f"{model or 'ACE'} #{box_id + 1}"


class AnycubicAceEntity(CoordinatorEntity[AnycubicCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: AnycubicCoordinator, key: str, box_id: int = 0) -> None:
        super().__init__(coordinator)
        self._box_id = box_id
        self._attr_unique_id = f"{coordinator.hs.serial}_{ace_suffix(box_id)}_{key}"

    @property
    def _box(self):
        for box in self.coordinator.data.ace:
            if box.id == self._box_id:
                return box
        return None

    @property
    def available(self) -> bool:
        return super().available and self._box is not None

    @property
    def device_info(self) -> DeviceInfo:
        box = self._box
        model = ACE_MODEL_NAMES.get(str(box.model_id)) if box and box.model_id is not None else None
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.hs.serial}_{ace_suffix(self._box_id)}")},
            manufacturer=MANUFACTURER,
            name=ace_device_name(self._box_id, box.model_id if box else None),
            model=model or "ACE 2",
            via_device=(DOMAIN, self.coordinator.hs.serial),
        )


@callback
def async_setup_ace_entities(
    entry: ConfigEntry,
    coordinator: AnycubicCoordinator,
    add: AddEntitiesCallback,
    factory: Callable[[int], list[Entity]],
) -> None:
    """Create ACE entities for box 0 now, and for each further box id as it reports.

    Box 0's entities always exist (the box is activity-gated and may not have reported
    yet — availability handles absence); additional boxes (issue #4) appear whenever a
    report first carries their id, including mid-session hot-plug.
    """
    known = {0}
    add(factory(0))

    @callback
    def _scan() -> None:
        for box in coordinator.data.ace:
            if box.id not in known:
                known.add(box.id)
                add(factory(box.id))

    _scan()
    entry.async_on_unload(coordinator.async_add_listener(_scan))
