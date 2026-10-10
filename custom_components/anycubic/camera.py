"""Live camera — the printer's on-demand H.264 FLV stream."""
from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import CAMERA_MODELS, DOMAIN, MANUFACTURER, MODEL_NAMES
from .coordinator import AnycubicCoordinator

if TYPE_CHECKING:
    from homeassistant.components.stream import Stream

_LOGGER = logging.getLogger(__name__)

# Capture re-kick (issue #15). Home Assistant asks stream_source() once per camera entity
# and keeps the Stream it builds from the answer, so the kick in stream_source() happens
# once; when the printer stops capturing (a power cycle) the kept Stream's worker retries
# a URL nothing is serving. While that Stream is in use and failing, capture is started
# again — but a printer whose camera service is broken answers every start with a failure
# a quarter of a minute later, so never more often than this, and half as often after each
# kick that did not bring the stream back, down to once per MAX.
CAPTURE_REKICK_MIN_INTERVAL = 30.0
CAPTURE_REKICK_MAX_INTERVAL = 300.0
# Capture lights the chamber, so it is started again by itself only while somebody is
# watching — and the Stream cannot say whether anybody is. An output's idle timer starts
# when a segment arrives, so a worker that cannot open the URL keeps the output a card
# made, and keeps retrying, long after the browser has gone. What is gone by instead is
# how long ago anything last asked for the stream: for this long after, a failing stream
# is kicked; once it has passed, a stream still failing is stopped, and the next request
# starts it, and capture, afresh. "Preload camera stream" is exempt: that is the user
# asking for the stream to be kept alive, and only the MAX interval above holds it back.
CAPTURE_VIEWER_WINDOW = 600.0
# A stream that drops while it is being watched counts as asked for again (a card left
# open on a long print makes only the one request). The evidence is its output: Home
# Assistant removes a playing stream's output 30 s after the last segment request. But a
# NEW output — and every restart here makes one — is kept for a 60 s startup timeout
# whether or not anything is requested, so STREAM_HEALTHY_AFTER proves nothing about
# viewers: a camera that plays for fifty seconds and drops would reopen the window each
# time, for ever. That startup timeout runs from the output's first segment, and the
# time measured here runs from the start of the attempt, before the source is opened,
# which can itself take the worker's 30 s source timeout. So 90 s at the least; only an
# output still there after this long has had a segment requested within the last 30 s.
STREAM_WATCHED_AFTER = 120.0
# Once a Stream has failed, its reporting "available" again says nothing: it does so at
# the start of every attempt to open the URL. An attempt that is going to fail has done
# so within the worker's 30 s source timeout; one still up after this long is playing.
STREAM_HEALTHY_AFTER = 45.0
# Stopping a worker waits for it, and one stuck opening a printer that does not answer
# gives up only at its source timeout. Removing the entity does not wait that long: the
# worker has been told to quit by then and ends on its own.
STREAM_STOP_TIMEOUT = 10.0

# Home Assistant's name for the Stream output a recording is written through.
_RECORDER_OUTPUT = "recorder"

# The schemes a reported stream URL may have (see AnycubicCamera._stream_url).
_STREAM_SCHEMES = ("http", "https", "rtsp")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    # Only models with a local camera (built-in on enclosed and on the Kobra 4 / X, add-on on
    # the Kobra 3 family). Kobra 2 has no camera.
    coord: AnycubicCoordinator = entry.runtime_data
    if coord.hs.model_id in CAMERA_MODELS:
        add([AnycubicCamera(coord)])


async def async_stop_stream(stream: Stream) -> None:
    """Stop a Stream's worker for good.

    The outputs are removed one by one first, rather than left for stop() to drop,
    because removing one is what releases a playlist request still waiting on it.

    Stream.stop() is the rest of it unless "Preload camera stream" is on: then it
    drops the outputs and leaves the worker running, on purpose, and nothing public
    ends it. `_stop` is the method stop() itself calls otherwise; it is looked up
    rather than assumed, so a Home Assistant without it costs only the preload case.
    """
    for output in stream.outputs().values():
        await stream.remove_provider(output)
    await stream.stop()
    if stream.dynamic_stream_settings.preload_stream:
        stop_worker = getattr(stream, "_stop", None)
        if stop_worker is not None:
            await stop_worker()


async def async_restart_stream(stream: Stream, source: str) -> None:
    """Point a Stream that is in use at `source` and restart its worker at once.

    Not Stream.update_source(), which reads as if it did this. It restarts a worker
    that is reading; a failing worker spends nearly all its time waiting to retry, and
    there update_source() ends it instead: the outputs go, the access token the open
    card holds goes with them, and the flag it leaves set makes the next stop()
    restart the worker rather than end it — a stop() that never returns. So the
    worker is stopped the ordinary way and started again, which also drops the retry
    delay it had built up (ten seconds more per failure, with no ceiling).

    The outputs and the token are then put back, so the address a card already
    holds is still served.
    """
    outputs = stream.outputs()
    if _RECORDER_OUTPUT in outputs:
        # Removing the recorder output ends the recording. The worker reads the source
        # afresh on each attempt, so its own next retry picks the new one up.
        stream.source = source
        return
    token = stream.access_token
    await async_stop_stream(stream)
    stream.source = source
    for fmt in outputs:
        stream.add_provider(fmt)
    stream.access_token = token
    await stream.start()


class AnycubicCamera(Camera):
    _attr_has_entity_name = True
    _attr_translation_key = "camera"
    _attr_icon = "mdi:camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__()
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.hs.serial}_camera"
        # One capture kick at a time: they share the coordinator's wait for the answer.
        self._kick_lock = asyncio.Lock()
        # Monotonic time of the last kick, and how long the next automatic one must wait.
        self._last_kick: float | None = None
        self._rekick_interval = CAPTURE_REKICK_MIN_INTERVAL
        # Has the Stream failed since it last played, and the monotonic time its current
        # attempt started (None between attempts). See _stream_healthy.
        self._stream_failing = False
        self._stream_up_since: float | None = None
        self._rekick_task: asyncio.Task | None = None
        # Monotonic time the stream was last asked for (see CAPTURE_VIEWER_WINDOW), the
        # timer that runs the window out, and the task stopping a Stream it ran out on.
        self._last_viewed: float | None = None
        self._viewer_timer = None
        self._unwatched_task: asyncio.Task | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.hs.serial)},
            manufacturer=MANUFACTURER,
            name=MODEL_NAMES.get(self.coordinator.hs.model_id) or "Anycubic printer",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_session_listener(self._async_session_back))

    async def async_refresh_providers(self, *args, **kwargs) -> None:
        """Opt out of HA's WebRTC-provider probe for this camera.

        The probe calls stream_source() on every entity add (and whenever a provider
        registers), and stream_source() commands camera_start — which makes the printer
        firmware switch the chamber LED on. With go2rtc bundled in HA, that lit the
        chamber on every HA start / entry reload. Skipping the probe costs only WebRTC
        playback; the frontend falls back to HLS, which reaches stream_source() at real
        playback time instead.
        """

    async def stream_source(self) -> str:
        """Kick the printer's capture, then hand HA its stream URL.

        Freshest source wins: the startCapture answer (new-generation firmware
        returns a per-session tokenized URL there — issue #6, Kobra 4), then the
        info report's urls.rtspUrl, then the S1-family :18088/flv. Only the host
        is replaced: the user-entered name must keep winning over the
        printer-reported IP. (See _stream_url for what is taken from a reported URL.)
        """
        async with self._kick_lock:
            return await self._async_kick()

    async def _async_kick(self) -> str:
        """Start capture and return the URL it is served on. Call with _kick_lock held."""
        self._last_kick = monotonic()
        await self.coordinator.async_start_capture()
        return self._stream_url(
            self.coordinator.video_stream_url or self.coordinator.data.printer.camera_url)

    def _stream_url(self, reported: str | None) -> str:
        """The address Home Assistant opens, for the URL the printer reported (if any).

        Only the path and port are taken from the printer; the host is always the
        configured one. The address part is built, not edited: the configured host,
        in brackets if it is an IPv6 literal, and the reported port when there is one.
        Path and query go through exactly as reported — on new-generation firmware
        the path is the session token. A reported URL is used only if it is http,
        https or rtsp, which is what a printer serves its stream on, and reads as an
        address with a host and a valid port; otherwise the S1-family default is.
        """
        host = self.coordinator.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            parts = urlsplit(reported or "")
            port = parts.port                    # raises for a port that is not one
        except ValueError:
            parts = port = None
        if (parts is None or not parts.hostname
                or parts.scheme.lower() not in _STREAM_SCHEMES):
            return f"http://{host}:18088/flv"
        return parts._replace(netloc=host if port is None else f"{host}:{port}").geturl()

    async def async_create_stream(self) -> Stream | None:
        """Hand out this camera's Stream, with capture running behind it (issue #15).

        Every `camera/stream` request comes through here. Home Assistant calls
        stream_source() only for the first; after that it returns the Stream it kept,
        whatever has happened to the printer since. Three cases for a kept Stream:

        Nobody is using it — its worker stopped when the last viewer left, and the
        caller is about to start it again. Whether the printer is still capturing
        cannot be known (it may have been power cycled, or another client may have
        stopped capture), so it is kicked here as it would be for a first request.

        In use and failing — capture is started again in the background, without
        waiting out the automatic backoff: someone has just opened the card.

        In use and playing — left alone.
        """
        kept = self.stream
        stream = await super().async_create_stream()
        if stream is None:
            return None
        self._async_viewer_seen()
        if kept is None:
            # New, from a stream_source() that has just kicked. Home Assistant set its
            # own state writer as the callback; ours writes the state too, then looks.
            stream.set_update_callback(self._async_stream_state_changed)
        elif not stream.outputs():
            async with self._kick_lock:
                # Looked at again under the lock: a second request that waited here for
                # the first one's kick must not send another.
                if not stream.outputs() and self._kick_due(CAPTURE_REKICK_MIN_INTERVAL):
                    stream.source = await self._async_kick()
                    self._stream_failing = False
        elif not self._stream_healthy():
            self._async_schedule_rekick(
                "the stream was requested while failing", CAPTURE_REKICK_MIN_INTERVAL)
        return stream

    def _kick_due(self, interval: float) -> bool:
        """May capture be started now, `interval` being the least time between kicks?

        Never without a transport: the commands would be dropped (the printer is off,
        or between sessions), and the kick would only sit out its wait for an answer.
        """
        if not self.coordinator.has_transport:
            return False
        return self._last_kick is None or monotonic() - self._last_kick >= interval

    def _stream_healthy(self) -> bool:
        """Is the Stream playing, as far as its reports tell?

        One that has not failed is taken to be: a card opened a second time moments
        after the first must not restart capture under the stream it is watching. One
        that has failed is believed again only once an attempt has stayed up for
        STREAM_HEALTHY_AFTER.
        """
        if not self._stream_failing:
            return True
        return (self._stream_up_since is not None
                and monotonic() - self._stream_up_since >= STREAM_HEALTHY_AFTER)

    @callback
    def _async_stream_state_changed(self) -> None:
        """The Stream's worker started an attempt (available) or failed one (not).

        Installed in place of the state writer Home Assistant gives every camera's
        Stream, so it does that job first.
        """
        stream = self.stream
        if stream is None:
            return  # removed; a worker on its way out can still report
        self.async_write_ha_state()
        if stream.available:
            self._stream_up_since = monotonic()
            return
        if self._stream_healthy():
            # It had been playing, so this is a new failure and not one more in a run
            # of them: the backoff starts over.
            self._rekick_interval = CAPTURE_REKICK_MIN_INTERVAL
        if (self._stream_up_since is not None
                and monotonic() - self._stream_up_since >= STREAM_WATCHED_AFTER):
            # It was being watched just now (see STREAM_WATCHED_AFTER), which counts
            # as asking — or a card left open on a long print would be cut off by the
            # first hiccup after the window.
            self._async_viewer_seen()
        self._stream_failing = True
        self._stream_up_since = None
        if self._unwatched(stream):
            self._async_stop_unwatched(stream)
            return
        self._async_schedule_rekick("the stream stopped", self._rekick_interval)

    @callback
    def _async_viewer_seen(self) -> None:
        """Something asked for the stream: the viewer window starts again from now."""
        self._last_viewed = monotonic()
        if self._viewer_timer is not None:
            self._viewer_timer()
        # A timer, and not the worker's next failure report, is what ends the window:
        # the worker waits ten seconds longer before each retry, with no ceiling, so
        # after a long outage its next report can be most of an hour away.
        self._viewer_timer = async_call_later(
            self.hass, CAPTURE_VIEWER_WINDOW, self._async_viewer_window_lapsed)

    def _unwatched(self, stream: Stream) -> bool:
        """Has the viewer window run out on this Stream (see CAPTURE_VIEWER_WINDOW)?"""
        if stream.dynamic_stream_settings.preload_stream:
            return False
        return (self._last_viewed is None
                or monotonic() - self._last_viewed >= CAPTURE_VIEWER_WINDOW)

    @callback
    def _async_viewer_window_lapsed(self, _now=None) -> None:
        self._viewer_timer = None
        stream = self.stream
        # A Stream that is playing is left to Home Assistant's idle timer, which works
        # once segments arrive. If it fails later, that report either shows it was
        # still being watched, and opens the window again, or finds the window closed
        # and stops it.
        if stream is not None and self._unwatched(stream) and not self._stream_healthy():
            self._async_stop_unwatched(stream)

    @callback
    def _async_stop_unwatched(self, stream: Stream) -> None:
        """Stop a failing Stream nobody has asked for within the viewer window.

        Its worker would otherwise retry, and log an error each time, until Home
        Assistant restarts. Stopped, it has no outputs, so nothing here kicks capture
        for it again, and the next request for the stream starts both.
        """
        if not stream.outputs():
            return
        if self._unwatched_task is not None and not self._unwatched_task.done():
            return
        self._unwatched_task = self.hass.async_create_background_task(
            self._async_stop_unwatched_stream(stream), "anycubic camera stream stop")

    async def _async_stop_unwatched_stream(self, stream: Stream) -> None:
        async with self._kick_lock:
            # Looked at again under the lock: a request may have come in while a kick
            # that was already running finished.
            if self.stream is not stream or not self._unwatched(stream):
                return
            _LOGGER.debug("stopping the camera stream: failing, and nobody has asked for it")
            try:
                async with asyncio.timeout(STREAM_STOP_TIMEOUT):
                    await async_stop_stream(stream)
            except TimeoutError:
                _LOGGER.debug("the stream worker is still closing; it ends on its own")

    @callback
    def _async_session_back(self) -> None:
        """The printer has answered again after a recovery — the reporter's case.

        It was off, or dropped us; either way capture is no longer running, and this is
        the first moment a kick can work, so the backoff built up while it could not is
        forgotten. A Stream that is playing is left alone (a re-handshake also follows
        another client taking the connection for a moment); if it is in fact stuck on
        a dead connection it will fail shortly, and the cleared backoff lets that
        failure kick at once.
        """
        self._last_kick = None
        self._rekick_interval = CAPTURE_REKICK_MIN_INTERVAL
        if not self._stream_healthy():
            self._async_schedule_rekick("the printer session is back", self._rekick_interval)

    @callback
    def _async_schedule_rekick(self, reason: str, interval: float) -> None:
        """Start capture again in the background, if anyone is using the stream.

        Capture switches the chamber LED on, so not for a Stream with no outputs (its
        worker has stopped), and not for one nobody has asked for within the viewer
        window. Nor while a kick is already running.
        """
        stream = self.stream
        if stream is None or not stream.outputs() or self._unwatched(stream):
            return
        if self._kick_lock.locked() or not self._kick_due(interval):
            return
        if self._rekick_task is not None and not self._rekick_task.done():
            return
        self._rekick_task = self.hass.async_create_background_task(
            self._async_rekick(stream, reason), "anycubic camera capture restart")

    async def _async_rekick(self, stream: Stream, reason: str) -> None:
        async with self._kick_lock:
            if self.stream is not stream or not stream.outputs():
                return
            _LOGGER.debug("starting camera capture again: %s", reason)
            source = await self._async_kick()
            self._rekick_interval = min(self._rekick_interval * 2, CAPTURE_REKICK_MAX_INTERVAL)
            if self.stream is stream and stream.outputs():
                await async_restart_stream(stream, source)

    async def async_will_remove_from_hass(self) -> None:
        if self._viewer_timer is not None:
            self._viewer_timer()
            self._viewer_timer = None
        # Home Assistant does not stop a camera's Stream when the entity is removed. A
        # worker that never received data never idles out either, so in the blank state
        # each reload left one more retrying, and logging, until the next restart
        # (issue #15). Stream.stop() is safe to call twice, should core ever do it too.
        stream, self.stream = self.stream, None
        # A restart or a stop of our own may be part-way through the same Stream. It is
        # ended first, and waited for, so it cannot start the worker again behind the
        # stop below.
        for task in (self._rekick_task, self._unwatched_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.wait([task])
        if stream is not None:
            try:
                async with asyncio.timeout(STREAM_STOP_TIMEOUT):
                    await async_stop_stream(stream)
            except TimeoutError:
                _LOGGER.debug("the stream worker is still closing; it ends on its own")
        await self.coordinator.async_send_command("camera_stop")
        await super().async_will_remove_from_hass()
