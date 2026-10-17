"""Chamber light and the Kobra 4 camera light — on/off (neither is dimmed here)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .anycubic_local.models import LightState
from .const import CAMERA_LIGHT_MODELS, ENCLOSED_MODELS
from .coordinator import AnycubicCoordinator
from .entity import AnycubicEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    # The chamber light is enclosure hardware (KS1 / KS1 Max only).
    coord: AnycubicCoordinator = entry.runtime_data
    if coord.hs.model_id in ENCLOSED_MODELS:
        add([AnycubicLight(coord)])
    if coord.hs.model_id in CAMERA_LIGHT_MODELS:
        _add_camera_light_when_known(entry, coord, add)


def _add_camera_light_when_known(entry: ConfigEntry, coord: AnycubicCoordinator,
                                 add: AddEntitiesCallback) -> None:
    """Add the camera light once peripherie says the printer has a camera (issue #17).

    peripherie is asked for at connect and may not have been answered yet when this platform
    is set up, so the check waits for it rather than deciding on a missing answer.
    """
    unsub = None

    @callback
    def _check() -> None:
        nonlocal unsub
        peripherie = coord.peripherie
        if peripherie is None:
            return
        if unsub is not None:
            unsub()
            unsub = None
        if peripherie.get("camera"):
            add([AnycubicCameraLight(coord)])

    _check()
    if coord.peripherie is None:
        unsub = coord.async_add_listener(_check)
        entry.async_on_unload(lambda: unsub() if unsub is not None else None)


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
        """Reflect the change now, so the toggle holds and the icon updates immediately.

        The printer confirms a light command about 0.2s later with a `light` report of its
        own, carrying the state the light took (issue #14). That report replaces this guess,
        and every poll answer after it reconciles to real state the same way.
        """
        self.coordinator.data.light = LightState(on=on)
        self.coordinator.async_notify_write()


class AnycubicCameraLight(AnycubicEntity, LightEntity):
    """The Kobra 4's camera light (light type 3), tracked apart from the chamber light."""

    _attr_translation_key = "camera_light"
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "camera_light")

    @property
    def is_on(self) -> bool | None:
        # None (unknown) until the printer reports the camera light.
        light = self.coordinator.data.camera_light
        return None if light is None else light.on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command("camera_light", on=True)
        self._set_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command("camera_light", on=False)
        self._set_optimistic(False)

    def _set_optimistic(self, on: bool) -> None:
        """Reflect the change now; the printer's own light report replaces it."""
        self.coordinator.data.camera_light = LightState(on=on)
        self.coordinator.async_notify_write()
