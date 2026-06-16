"""Base entity for AnyCubic — links every entity to the printer device."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ACE_MODEL_NAMES, ACE_SUFFIX, DOMAIN, MANUFACTURER, MODEL_NAMES
from .coordinator import AnycubicCoordinator


class AnycubicEntity(CoordinatorEntity[AnycubicCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: AnycubicCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.hs.serial}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        p = self.coordinator.data.printer
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.hs.serial)},
            manufacturer=MANUFACTURER,
            name=MODEL_NAMES.get(self.coordinator.hs.model_id) or p.model or "AnyCubic printer",
            model=MODEL_NAMES.get(self.coordinator.hs.model_id),
            sw_version=p.firmware,
            configuration_url=f"http://{self.coordinator.host}",
        )


class AnycubicAceEntity(CoordinatorEntity[AnycubicCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: AnycubicCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.hs.serial}_{ACE_SUFFIX}_{key}"

    @property
    def _box(self):
        boxes = self.coordinator.data.ace
        return boxes[0] if boxes else None

    @property
    def available(self) -> bool:
        return super().available and self._box is not None

    @property
    def device_info(self) -> DeviceInfo:
        # Stable name so entity IDs (ace_2_*) are fixed at registration before the box reports.
        # The box's actual model (ACE Pro vs ACE 2) shows as the device model once it reports.
        box = self._box
        model = ACE_MODEL_NAMES.get(str(box.model_id)) if box and box.model_id is not None else None
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.hs.serial}_{ACE_SUFFIX}")},
            manufacturer=MANUFACTURER, name="ACE 2", model=model or "ACE 2",
            via_device=(DOMAIN, self.coordinator.hs.serial),
        )
