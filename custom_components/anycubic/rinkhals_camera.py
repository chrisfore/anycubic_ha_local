"""Camera for a printer running the Rinkhals firmware overlay — its MJPEG feed on port 8080.

Rinkhals' camera app takes the camera away from the printer's own service: startCapture is
answered with a failure and the stock stream never opens. It serves the camera itself
instead, always on, as a JPEG snapshot and a multipart JPEG stream. Which camera an entry
uses is the "Camera source" option, and it is one or the other: nothing here falls back to
the printer's own camera, and nothing in camera.py tries this one.

This camera sends the printer nothing. No capture command is published for it, so reading
it never switches the chamber light, and none of the capture re-kick in camera.py applies.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.components.camera import Camera
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import (
    async_aiohttp_proxy_stream,
    async_get_clientsession,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, MANUFACTURER, MODEL_NAMES
from .coordinator import AnycubicCoordinator

if TYPE_CHECKING:
    from aiohttp import web

_LOGGER = logging.getLogger(__name__)

# Where Rinkhals serves the camera. The port and both paths are the same on every printer
# model it supports, so they are fixed here and nothing the printer reports goes into the
# address: only the host varies, and the host is always the configured one.
RINKHALS_PORT = 8080
_SNAPSHOT_PATH = "/?action=snapshot"
_STREAM_PATH = "/?action=stream"

# A feed on the same LAN answers in a few hundredths of a second. These are for one that
# does not: long enough for a busy printer, and the snapshot's short of the ten seconds
# Home Assistant itself allows a camera image, so the failure reported is ours, with its
# reason, and not a bare timeout from above.
CONNECT_TIMEOUT = 5.0
SNAPSHOT_TIMEOUT = 8.0
# A snapshot is some tens of kilobytes. Whatever answers on that port is read into memory,
# so it is read only up to this, which is far more than any frame and far less than harm.
SNAPSHOT_MAX_BYTES = 2 * 1024 * 1024
# Whether the feed is there is known only from the last time it was asked, and it is asked
# only when a card or the entity's picture is shown: nothing polls it. A camera shown as
# unavailable is not asked at all, so after this long it is offered again, and the next
# request finds out. Without anyone looking, that costs nothing and asks nothing.
FEED_RETRY_AFTER = 60.0


def _mime(response: aiohttp.ClientResponse) -> str:
    """The media type a response declares, without its parameters; "" if it declares none."""
    return response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()


class AnycubicRinkhalsCamera(Camera):
    """The Rinkhals feed, as a still-image camera with an MJPEG live view.

    No STREAM feature: Home Assistant's stream component reads H.264 and this is JPEG
    frames, which the frontend shows directly from the proxied multipart stream. So
    stream_source() is never asked, and neither HLS nor WebRTC is involved.

    The entity carries the same unique id as the printer's own camera, so changing the
    source changes what is behind the one entity and does not make a second.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "camera"
    _attr_icon = "mdi:camera"

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__()
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.hs.serial}_camera"
        # Did the feed fail the last time it was asked, and the timer that ends that
        # (see FEED_RETRY_AFTER).
        self._feed_down = False
        self._retry_timer = None
        # Has this outage been logged at the normal level yet (cleared by a good answer).
        self._outage_logged = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.hs.serial)},
            manufacturer=MANUFACTURER,
            name=MODEL_NAMES.get(self.coordinator.hs.model_id) or "Anycubic printer",
        )

    @property
    def available(self) -> bool:
        return not self._feed_down

    def _url(self, path: str) -> str:
        """The feed's address: the configured host, in brackets if it is an IPv6 literal."""
        host = self.coordinator.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{RINKHALS_PORT}{path}"

    async def _async_get(self, path: str) -> aiohttp.ClientResponse:
        """Open a GET to the feed. GET only — it answers HEAD with an error.

        Redirects are not followed: the feed has none, and following one would have Home
        Assistant fetch whatever address the answer named.
        """
        session = async_get_clientsession(self.hass)
        return await session.get(
            self._url(path), allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT))

    @callback
    def _async_feed_answered(self, problem: str | None) -> None:
        """Record what the feed did when asked: None for a good answer, else what was wrong.

        `problem` is fixed text, a status code or the name of an exception's class, never
        anything the feed sent and never the address: these lines are pasted into issues.
        """
        if self._retry_timer is not None:
            self._retry_timer()
            self._retry_timer = None
        was_down, self._feed_down = self._feed_down, problem is not None
        if problem is None:
            self._outage_logged = False
        else:
            # Once at the normal level when it stops answering; a card left open asks
            # again every time the camera is offered, and those are for the debug log.
            _LOGGER.log(logging.DEBUG if self._outage_logged else logging.WARNING,
                        "the Rinkhals camera feed could not be read (%s)", problem)
            self._outage_logged = True
            self._retry_timer = async_call_later(
                self.hass, FEED_RETRY_AFTER, self._async_offer_again)
        if was_down != self._feed_down:
            self.async_write_ha_state()

    @callback
    def _async_offer_again(self, _now=None) -> None:
        self._retry_timer = None
        self._feed_down = False
        self.async_write_ha_state()

    async def async_camera_image(
            self, width: int | None = None, height: int | None = None) -> bytes | None:
        """The feed's snapshot, or None if it cannot be had. Never another source's."""
        try:
            async with asyncio.timeout(SNAPSHOT_TIMEOUT):
                response = await self._async_get(_SNAPSHOT_PATH)
                try:
                    image, problem = await self._async_read_snapshot(response)
                finally:
                    response.close()
        except TimeoutError:
            image, problem = None, "timed out"
        except aiohttp.ClientError as err:
            image, problem = None, type(err).__name__
        self._async_feed_answered(problem)
        return image

    async def _async_read_snapshot(
            self, response: aiohttp.ClientResponse) -> tuple[bytes | None, str | None]:
        """A snapshot response's image, or what is wrong with the response."""
        if response.status != 200:
            return None, f"status {response.status}"
        if not _mime(response).startswith("image/"):
            return None, "not an image"
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > SNAPSHOT_MAX_BYTES:
            return None, "image too large"
        # Counted as it is read as well: the length is the sender's word, and need not
        # be given at all.
        chunks, size = [], 0
        async for chunk in response.content.iter_chunked(65536):
            size += len(chunk)
            if size > SNAPSHOT_MAX_BYTES:
                return None, "image too large"
            chunks.append(chunk)
        if not size:
            return None, "empty image"
        return b"".join(chunks), None

    async def handle_async_mjpeg_stream(
            self, request: web.Request) -> web.StreamResponse | None:
        """Pass the feed's multipart stream through to the browser.

        The printer's bytes are relayed as they come, under the content type it gave
        (that is where the part boundary is). None — which Home Assistant answers as a
        bad gateway — if the feed does not answer or what answers is not such a stream.
        """
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                response = await self._async_get(_STREAM_PATH)
        except TimeoutError:
            self._async_feed_answered("timed out")
            return None
        except aiohttp.ClientError as err:
            self._async_feed_answered(type(err).__name__)
            return None
        try:
            if response.status != 200:
                self._async_feed_answered(f"status {response.status}")
                return None
            if _mime(response) != "multipart/x-mixed-replace":
                self._async_feed_answered("not a multipart stream")
                return None
            self._async_feed_answered(None)
            return await async_aiohttp_proxy_stream(
                self.hass, request, response.content, response.headers.get("Content-Type"))
        finally:
            response.close()

    async def async_will_remove_from_hass(self) -> None:
        if self._retry_timer is not None:
            self._retry_timer()
            self._retry_timer = None
        await super().async_will_remove_from_hass()
