"""Print control buttons: pause, resume, stop; and, where printing from Home Assistant is on,
Print again and Bed is clear."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized, UnknownUser
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .actions import async_print_again
from .anycubic_local.const import JOB_PHASES
from .anycubic_local.models import PrinterState
from .const import PRINT_MODELS
from .coordinator import AnycubicCoordinator
from .entity import AnycubicEntity


@dataclass(frozen=True, kw_only=True)
class AnycubicButtonEntityDescription(ButtonEntityDescription):
    command: str
    available_fn: Callable[[PrinterState], bool]


# Stop is gated on every job phase (JOB_PHASES), not `printing` alone, which left a job
# in preheating or auto-leveling with no way to stop it.

BUTTONS: tuple[AnycubicButtonEntityDescription, ...] = (
    AnycubicButtonEntityDescription(key="pause", translation_key="pause", command="pause",
        icon="mdi:pause", available_fn=lambda p: p.printing),
    AnycubicButtonEntityDescription(key="resume", translation_key="resume", command="resume",
        icon="mdi:play", available_fn=lambda p: p.paused),
    # Stop cancels the running print (validated on hardware) — gated on an active job.
    AnycubicButtonEntityDescription(key="stop", translation_key="stop", command="stop",
        icon="mdi:stop",
        available_fn=lambda p: p.printing or p.paused or p.status in JOB_PHASES),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator: AnycubicCoordinator = entry.runtime_data
    buttons: list[ButtonEntity] = [AnycubicButton(coordinator, d) for d in BUTTONS]
    if coordinator.hs.model_id in PRINT_MODELS:
        buttons += [AnycubicPrintAgainButton(coordinator), AnycubicBedClearButton(coordinator)]
    add(buttons)


class AnycubicButton(AnycubicEntity, ButtonEntity):
    entity_description: AnycubicButtonEntityDescription

    def __init__(self, coordinator: AnycubicCoordinator, description: AnycubicButtonEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        # Resume is not offered for a pause only a checked Clear the bed press may resume.
        if (self.entity_description.command == "resume"
                and self.coordinator.clearing.resume_locked):
            return False
        return super().available and self.entity_description.available_fn(self.coordinator.data.printer)

    async def async_press(self) -> None:
        await self.coordinator.async_send_command(self.entity_description.command)


class AdminOnlyButton(AnycubicEntity, ButtonEntity):
    """A button only an admin may press, as only an admin may call the print actions.

    A press from a person carries their user id in its context, and theirs must be an admin
    account. A press with no user behind it (an automation, a script run by the system) is
    let through, as Home Assistant's admin-only actions let it through.
    """

    async def _async_check_admin(self) -> None:
        context = self._context
        if context is None or not context.user_id:
            return
        user = await self.hass.auth.async_get_user(context.user_id)
        if user is None:
            raise UnknownUser(context=context)
        if not user.is_admin:
            raise Unauthorized(context=context)


class AnycubicPrintAgainButton(AdminOnlyButton):
    """Start the last file sent from Home Assistant again, through every guard it passed.

    Never with push-off: that is ticked for each print, and a button has nothing to tick.
    """

    _attr_translation_key = "print_again"
    _attr_icon = "mdi:repeat"

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "print_again")

    @property
    def available(self) -> bool:
        coordinator = self.coordinator
        return (super().available and coordinator.last_file is not None
                and coordinator.reachable and coordinator.idle)

    async def async_press(self) -> None:
        await self._async_check_admin()
        await async_print_again(self.hass, self.coordinator)


class AnycubicBedClearButton(AdminOnlyButton):
    """The user's word that the bed is clear. Offered only while the bed needs clearing and
    the printer is known to be idle; never something to automate."""

    _attr_translation_key = "bed_clear"
    _attr_icon = "mdi:check-bold"

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "bed_clear")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.bed_clear_allowed

    async def async_press(self) -> None:
        await self._async_check_admin()
        self.coordinator.async_mark_bed_clear()
