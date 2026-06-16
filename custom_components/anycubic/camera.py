"""Live camera — the printer's on-demand H.264 FLV stream."""
from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CAMERA_MODELS, DOMAIN, MANUFACTURER, MODEL_NAMES
from .coordinator import AnycubicCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    # Only models with a local FLV camera (built-in on enclosed, add-on on the Kobra 3 family).
    # Kobra 2 has no camera; Kobra X streams WebRTC with no local FLV.
    coord: AnycubicCoordinator = entry.runtime_data
    if coord.hs.model_id in CAMERA_MODELS:
        add([AnycubicCamera(coord)])


class AnycubicCamera(Camera):
    _attr_has_entity_name = True
    _attr_translation_key = "camera"
    _attr_icon = "mdi:camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__()
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.hs.serial}_camera"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.hs.serial)},
            manufacturer=MANUFACTURER,
            name=MODEL_NAMES.get(self.coordinator.hs.model_id) or "AnyCubic printer",
        )

    async def stream_source(self) -> str:
        """Ask the printer to start the feed, then hand HA's ffmpeg the FLV URL."""
        await self.coordinator.async_send_command("camera_start")
        return f"http://{self.coordinator.host}:18088/flv"

    async def async_will_remove_from_hass(self) -> None:
        await self.coordinator.async_send_command("camera_stop")
        await super().async_will_remove_from_hass()
