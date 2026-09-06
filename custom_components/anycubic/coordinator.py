"""Push coordinator: owns the transport, holds merged PrinterState + ACE boxes."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .anycubic_local import mqtt as mqtt_mod
from .anycubic_local.commands import build as build_command
from .anycubic_local.exceptions import CloudModeError
from .anycubic_local.handshake import HandshakeResult, do_handshake
from .anycubic_local.models import (
    AceBox,
    ExternalSpool,
    LightState,
    PrinterState,
    apply_fan,
    apply_progress,
    apply_temperature,
    merge_boxes,
    parse_extfilbox,
    parse_info,
    parse_light,
    parse_multicolorbox,
)
from .const import (
    ACE_DRYING_DEFAULT_DURATION_MIN,
    ACE_DRYING_DEFAULT_TEMP,
    ACE_MODEL_NAMES,
    DEFAULT_QUERY_INTERVAL,
    DOMAIN,
    ace_suffix,
)

_LOGGER = logging.getLogger(__name__)

# Printer status (info/tempature/fan/light) is pushed by the printer during activity, but the ACE
# box (multiColorBox) is NOT pushed — it only answers an on-demand getInfo — so we re-poll on an interval.
# The push rates differ by orders of magnitude: `tempature` lands within a second of a reading
# changing, while `info` can go minutes between arrivals when no other client (the Anycubic Slicer)
# is talking to the printer. Every type here must therefore be applied, not just `info` (issue #9).
# `extfilbox` is polled as well as pushed: a spool already sitting in the holder when Home
# Assistant restarts generates no event of its own, so without a poll the entity would not
# appear until the user next loaded or unloaded filament (issue #12).
_QUERY_TYPES = ("info", "tempature", "fan", "light", "multiColorBox", "extfilbox")

# `peripherie` is a static capability inventory ({camera, multiColorBox, udisk} presence flags) — it
# doesn't change, so we ask for it once at connect (for diagnostics / model onboarding) and never poll it.
_CONNECT_ONLY_QUERY_TYPES = ("peripherie",)

# Camera capture kick (see async_start_capture): the official client's stop -> pause -> start
# sequence, then a bounded wait for the printer's video report.
VIDEO_KICK_DELAY = 1.0
VIDEO_REPORT_TIMEOUT = 4.0

# Dead-session recovery. A healthy printer answers every poll, so silence this long
# means the session is gone even when the socket still looks open to paho — which is
# the shape of the bug in issue #9: polls kept "succeeding" into a session the printer
# had already dropped, and entities held their last values while reporting healthy.
SILENCE_POLLS_BEFORE_RECOVERY = 4
STALE_AFTER = SILENCE_POLLS_BEFORE_RECOVERY * DEFAULT_QUERY_INTERVAL
# Recovery re-handshakes and rebuilds the transport. Only after this many consecutive
# attempts have failed to produce a single report do the entities go unavailable —
# recovering quietly is right, but hiding a printer we genuinely cannot reach is not.
MAX_RECOVERIES_BEFORE_UNAVAILABLE = 2


@dataclass
class AnycubicData:
    printer: PrinterState = field(default_factory=PrinterState)
    ace: list[AceBox] = field(default_factory=list)
    light: LightState = field(default_factory=LightState)
    # None until the printer reports one. It only reports `extfilbox` while no ACE unit is
    # attached, so None means "no bare spool in use", not "not read yet".
    external_spool: ExternalSpool | None = None


class AnycubicCoordinator(DataUpdateCoordinator[AnycubicData]):
    def __init__(self, hass: HomeAssistant, hs: HandshakeResult,
                 host: str | None = None, transport_factory=None) -> None:
        super().__init__(hass, logger=_LOGGER, name=DOMAIN,
                         update_interval=timedelta(seconds=DEFAULT_QUERY_INTERVAL))
        self.hs = hs
        # The host the user entered (IP or DNS/mDNS name). Used for the HTTP-facing URLs
        # (camera, device link) so a name is honored and re-resolved; MQTT uses the
        # printer-reported broker. Falls back to the broker host when not supplied.
        self.host = host or hs.broker_host
        # ACE drying setpoints per box id (number entities edit these; the drying switch
        # uses them). Unset boxes fall back to the validated app defaults.
        self._drying_temps: dict[int, int] = {}
        self._drying_hours: dict[int, int] = {}
        self.data = AnycubicData()
        # Capability data captured for diagnostics / new-model onboarding (see diagnostics.py).
        # Non-sensitive: the printer's reported feature map, the peripheral presence inventory, and
        # which report types this printer actually emits.
        self.raw_features: dict | None = None
        self.peripherie: dict | None = None
        # Last raw multiColorBox payload, verbatim — diagnostics exposes it so protocol
        # differences across printer/ACE firmwares (unknown or renamed slot keys) can be
        # triaged from a diagnostics attachment alone.
        self.raw_multicolorbox: dict | None = None
        self.seen_report_types: set[str] = set()
        # Stream URL from the latest video report. New-generation firmware (Kobra 4 / X)
        # answers startCapture with a per-session tokenized URL (:18088/live/<token>);
        # kept out of PrinterState because parse_info rebuilds that on every info report.
        self.video_stream_url: str | None = None
        self._video_report: asyncio.Event | None = None
        self._factory = transport_factory if transport_factory is not None else mqtt_mod.AnycubicMqtt
        self._transport = None
        # Monotonic timestamp of the last report the printer sent us, and how many
        # recovery attempts have run since one arrived. Monotonic, not wall clock:
        # a system clock jump must not read as hours of silence.
        self._last_report: float | None = None
        self._recoveries = 0

    def _build_and_connect(self):
        """Construct the transport (paho client + blocking tls_set) and connect.

        Runs in an executor — `tls_set()` loads CA certs from disk, which must not
        happen on the event loop.
        """
        transport = self._factory(self.hs, on_report=self._on_report)
        transport.connect()
        for t in (*_QUERY_TYPES, *_CONNECT_ONLY_QUERY_TYPES):
            transport.query(t)
        return transport

    async def async_start(self) -> None:
        self._transport = await self.hass.async_add_executor_job(self._build_and_connect)
        # Start the silence clock at connect, so a printer that never answers at all
        # is caught by the same watchdog as one that goes quiet later.
        self._last_report = time.monotonic()

    def _rebuild(self):
        """Executor: drop the dead session, re-handshake, connect again.

        The handshake is re-run rather than reusing self.hs because the broker
        credentials are issued per session — a printer that rebooted, or that
        dropped us when another client (the Slicer) took the connection, will
        refuse the old ones.
        """
        old, self._transport = self._transport, None
        if old is not None:
            old.disconnect()
        hs = do_handshake(self.host)
        if self.hs.serial and hs.serial and hs.serial != self.hs.serial:
            # The address now answers for a DIFFERENT printer (DHCP reuse). Rebuilding
            # would silently repoint every entity at someone else's machine.
            raise UpdateFailed(
                f"{self.host} now answers for a different printer ({hs.serial})")
        self.hs = hs
        self._transport = self._build_and_connect()

    async def _async_recover(self, reason: str = "session looks dead") -> None:
        """Try to get a live session back. Raises UpdateFailed once it's hopeless.

        The rebuild is attempted on EVERY cycle, including after this has started
        reporting failure — giving up permanently would mean a printer that comes
        back stays dead until someone reloads the integration by hand.
        """
        self._recoveries += 1
        _LOGGER.warning("re-handshaking printer (attempt %s): %s",
                        self._recoveries, reason)
        try:
            await self.hass.async_add_executor_job(self._rebuild)
        except CloudModeError as err:
            # LAN Mode was turned off on the printer — same reauth path as setup.
            raise ConfigEntryAuthFailed(str(err)) from err
        except UpdateFailed:
            raise
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"reconnect failed: {err}") from err
        # Give the rebuilt session a full silence window before judging it again.
        self._last_report = time.monotonic()
        if self._recoveries > MAX_RECOVERIES_BEFORE_UNAVAILABLE:
            # Reconnecting keeps working but the printer never answers. Say so, rather
            # than serving values that stopped being true minutes ago.
            raise UpdateFailed(
                f"reconnected {self._recoveries} times without a single report")

    def _silent(self) -> bool:
        return (self._last_report is not None
                and time.monotonic() - self._last_report > STALE_AFTER)

    def _poll(self) -> None:
        for t in _QUERY_TYPES:
            self._transport.query(t)

    async def async_shutdown(self) -> None:
        if self._transport is not None:
            await self.hass.async_add_executor_job(self._transport.disconnect)
        await super().async_shutdown()

    async def _async_update_data(self) -> AnycubicData:
        # Re-poll on the interval so the ACE box (which the printer never pushes) stays fresh;
        # printer status also arrives via push between polls.
        # Three ways a session dies: paho notices (refused CONNACK, dropped socket); it
        # doesn't, and the printer simply stops answering; or an earlier rebuild failed
        # and left us with no transport at all. All three used to be invisible.
        #
        # Which one fired is logged, because they have different causes and a user's
        # debug log is the only way to tell them apart from here (issue #9).
        if self._transport is None:
            await self._async_recover("no transport")
        elif not getattr(self._transport, "connected", True):
            await self._async_recover("broker reports us disconnected")
        elif self._silent():
            await self._async_recover(
                f"no reports for {STALE_AFTER}s while the broker still reports connected")
        await self.hass.async_add_executor_job(self._poll)
        return self.data

    def _on_report(self, msg_type: str, data: dict) -> None:
        """Called on the paho network thread — marshal onto the HA event loop.

        Must be call_soon_threadsafe: add_job with a plain (non-@callback) function
        dispatches to an executor thread, and async_set_updated_data off the event
        loop trips HA's thread-safety check on every report.
        """
        self.hass.loop.call_soon_threadsafe(self._apply, msg_type, data)

    async def async_start_capture(self) -> None:
        """Start camera capture the way the official client does.

        The slicer's LAN camera always sends stopCapture, pauses, then startCapture —
        new-generation firmware (Kobra 4 / X) doesn't begin pushing on a bare start —
        and the startCapture answer carries the tokenized stream URL, captured into
        video_stream_url by _apply. S1-family printers answer with no URL; the wait
        just ends early and callers fall back to the info-report URL.
        """
        self._video_report = asyncio.Event()
        await self.async_send_command("camera_stop")
        await asyncio.sleep(VIDEO_KICK_DELAY)
        await self.async_send_command("camera_start")
        try:
            async with asyncio.timeout(VIDEO_REPORT_TIMEOUT):
                await self._video_report.wait()
        except TimeoutError:
            _LOGGER.debug("no video report within %ss of startCapture", VIDEO_REPORT_TIMEOUT)

    @property
    def job_active(self) -> bool:
        """Is there a print task for a `print`/`update` settings command to apply to?

        Confirmed on a Kobra 3 (issue #10): mid-print the printer answers a settings
        update with `code=200 state=updated msg=done`; idle it discards the message
        without acking at all. `taskid: "-1"` means "the current job", so with no job
        there is nothing to update. Paused counts — the task still exists.
        """
        printer = self.data.printer
        return printer.printing or printer.paused

    def _redact(self, topic: str) -> str:
        """Topics embed the device id, and these log lines get pasted into bug reports."""
        return topic.replace(self.hs.device_id, "**REDACTED**") if self.hs.device_id else topic

    async def async_send_command(self, command: str, **kwargs) -> None:
        """Build a control command and publish it (executor — paho publish is blocking-ish)."""
        if self._transport is None:
            _LOGGER.debug("dropping command %s: no transport", command)
            return
        topic, payload = build_command(self.hs.model_id, self.hs.device_id, command, **kwargs)
        body = json.dumps(payload)
        # The only record of what we actually put on the wire. A report is easy to observe
        # (it moves an entity); a command the printer silently discards left no trace at
        # all, which is what made issue #10 unfalsifiable from a user's debug log.
        _LOGGER.debug("publish %s -> %s %s", command, self._redact(topic), body)
        await self.hass.async_add_executor_job(self._transport.publish, topic, body)

    @callback
    def _apply(self, msg_type: str, data: dict) -> None:
        # Any report at all proves the session is alive; that, not a successful
        # publish, is what clears the watchdog.
        self._last_report = time.monotonic()
        self._recoveries = 0
        self.seen_report_types.add(msg_type)
        if msg_type == "info":
            self.data.printer = parse_info(data)
            features = data.get("features")
            if isinstance(features, dict):
                self.raw_features = features
        elif msg_type == "tempature":
            # Not a typo — the firmware's wire string. Folded rather than parsed into a
            # fresh state: it carries only temperatures, and is the fastest-moving source
            # of them by far. Reports are applied in arrival order, so whichever of
            # `tempature` and `info` lands last is the newest reading.
            apply_temperature(self.data.printer, data)
        elif msg_type == "fan":
            apply_fan(self.data.printer, data)
        elif msg_type == "print":
            # Pushed on every change during a job (and never polled), so it beats `info`
            # to progress/layer/remaining-time by the same margin `tempature` beats it to
            # temperatures. Command acks share this topic and are ignored by the fold.
            apply_progress(self.data.printer, data)
        elif msg_type == "multiColorBox":
            self.raw_multicolorbox = data
            self.data.ace = merge_boxes(self.data.ace, parse_multicolorbox(data))
            self._sync_ace_device_model()
        elif msg_type == "extfilbox":
            # The bare spool holder, reported only when no ACE unit is attached (issue #12).
            self.data.external_spool = parse_extfilbox(data)
        elif msg_type == "light":
            self.data.light = parse_light(data)
        elif msg_type == "peripherie" and isinstance(data, dict):
            self.peripherie = data
        elif msg_type == "video":
            url = (data.get("urls") or {}).get("rtspUrl") if isinstance(data, dict) else None
            if url:
                self.video_stream_url = url
            if self._video_report is not None:
                self._video_report.set()
        # Deliberately NOT async_set_updated_data: that helper resets the refresh
        # interval ("Manually update data, notify listeners and reset refresh interval"),
        # so every inbound report pushed the next poll a full 30s out. A printer pushing
        # info/tempature/fan every few seconds mid-print therefore never got polled at
        # all — and multiColorBox is poll-ONLY, so ACE humidity, temperature and drying
        # froze for the whole job. Notify listeners without touching the schedule.
        self.last_update_success = True
        self.async_update_listeners()

    def drying_temp(self, box_id: int) -> int:
        return self._drying_temps.get(box_id, ACE_DRYING_DEFAULT_TEMP)

    def set_drying_temp(self, box_id: int, value: int) -> None:
        self._drying_temps[box_id] = value

    def drying_hours(self, box_id: int) -> int:
        return self._drying_hours.get(box_id, ACE_DRYING_DEFAULT_DURATION_MIN // 60)

    def set_drying_hours(self, box_id: int, value: int) -> None:
        self._drying_hours[box_id] = value

    @callback
    def _sync_ace_device_model(self) -> None:
        """Show each box's real model (ACE Pro vs ACE 2) once it reports it.

        Boxes register before their model is known (box 0 as the literal "ACE 2" so
        entity IDs stay deterministic, further boxes as "ACE #N"); this renames only
        the registry display name/model. A user rename (name_by_user) still wins.
        """
        from .entity import ace_device_model, ace_device_name  # local: entity.py imports this module

        registry = dr.async_get(self.hass)
        for box in self.data.ace:
            if box.model_id is None or str(box.model_id) not in ACE_MODEL_NAMES:
                continue
            model = ace_device_model(box.id, box.model_id)
            name = ace_device_name(box.id, box.model_id)
            device = registry.async_get_device(
                identifiers={(DOMAIN, f"{self.hs.serial}_{ace_suffix(box.id)}")})
            if device is not None and (device.name != name or device.model != model):
                registry.async_update_device(device.id, name=name, model=model)
