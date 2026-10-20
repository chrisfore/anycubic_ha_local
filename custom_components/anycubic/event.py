"""Print events: started, paused, resumed, finished, stopped, and error for a fault."""
from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import EVENT_TYPES, AnycubicCoordinator
from .entity import AnycubicEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([AnycubicPrintEvent(entry.runtime_data)])


class AnycubicPrintEvent(AnycubicEntity, EventEntity):
    """What a phone notification hangs on. `error` fires once per fault the user did not
    cause (a clog, say), as soon as the printer reports it; the rest follow the status.
    Paused, stopped and error carry `reason_code` and `reason`.
    """

    _attr_translation_key = "print_event"
    _attr_event_types = list(EVENT_TYPES)

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "print_event")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_event_listener(self._handle_event))

    @callback
    def _handle_event(self, event_type: str, attributes: dict) -> None:
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()
