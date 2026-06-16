"""Print control buttons: pause, resume, stop."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .anycubic_local.models import PrinterState
from .coordinator import AnycubicCoordinator
from .entity import AnycubicEntity


@dataclass(frozen=True, kw_only=True)
class AnycubicButtonEntityDescription(ButtonEntityDescription):
    command: str
    available_fn: Callable[[PrinterState], bool]


BUTTONS: tuple[AnycubicButtonEntityDescription, ...] = (
    AnycubicButtonEntityDescription(key="pause", translation_key="pause", command="pause",
        icon="mdi:pause", available_fn=lambda p: p.printing),
    AnycubicButtonEntityDescription(key="resume", translation_key="resume", command="resume",
        icon="mdi:play", available_fn=lambda p: p.paused),
    # Stop cancels the running print (validated on hardware) — gated on an active job.
    AnycubicButtonEntityDescription(key="stop", translation_key="stop", command="stop",
        icon="mdi:stop", available_fn=lambda p: p.printing or p.paused),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([AnycubicButton(entry.runtime_data, d) for d in BUTTONS])


class AnycubicButton(AnycubicEntity, ButtonEntity):
    entity_description: AnycubicButtonEntityDescription

    def __init__(self, coordinator: AnycubicCoordinator, description: AnycubicButtonEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.available_fn(self.coordinator.data.printer)

    async def async_press(self) -> None:
        await self.coordinator.async_send_command(self.entity_description.command)
