"""The printer's own renders of the job it is running (issue #13)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.image import ImageEntity, ImageEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .anycubic_local.models import ObjectImages
from .coordinator import AnycubicCoordinator
from .entity import AnycubicEntity


@dataclass(frozen=True, kw_only=True)
class AnycubicImageEntityDescription(ImageEntityDescription):
    value_fn: Callable[[ObjectImages], bytes | None]


OBJECT_IMAGES: tuple[AnycubicImageEntityDescription, ...] = (
    AnycubicImageEntityDescription(key="object_thumbnail", translation_key="object_thumbnail",
                                   value_fn=lambda i: i.thumbnail),
    AnycubicImageEntityDescription(key="object_top_view", translation_key="object_top_view",
                                   value_fn=lambda i: i.top_view),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: AnycubicCoordinator = entry.runtime_data
    created: set[bool] = set()

    @callback
    def _scan() -> None:
        # Created on the first answer rather than at setup: an idle printer has no object
        # to show, and the printer only answers while a job names a file. Same pattern as
        # the external spool sensor.
        if coord.data.object_images is not None and not created:
            created.add(True)
            add([AnycubicObjectImage(hass, coord, d) for d in OBJECT_IMAGES])

    _scan()
    entry.async_on_unload(coord.async_add_listener(_scan))


class AnycubicObjectImage(AnycubicEntity, ImageEntity):
    entity_description: AnycubicImageEntityDescription
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, coordinator: AnycubicCoordinator,
                 description: AnycubicImageEntityDescription) -> None:
        AnycubicEntity.__init__(self, coordinator, description.key)
        ImageEntity.__init__(self, hass)
        self.entity_description = description
        self._shown: str | None = None
        self._attr_image_last_updated = dt_util.utcnow()

    @property
    def _bytes(self) -> bytes | None:
        images = self.coordinator.data.object_images
        return None if images is None else self.entity_description.value_fn(images)

    @property
    def available(self) -> bool:
        return super().available and self._bytes is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        # image_last_updated is what makes the frontend re-fetch. Moved only when the job
        # changes: bumping it on every report would re-download the same PNG every poll.
        images = self.coordinator.data.object_images
        filename = images.filename if images else None
        if filename != self._shown:
            self._shown = filename
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        return self._bytes
