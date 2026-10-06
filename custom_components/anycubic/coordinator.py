"""Push coordinator: owns the transport, holds merged PrinterState + ACE boxes."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .anycubic_local import mqtt as mqtt_mod
from .anycubic_local.commands import build as build_command
from .anycubic_local.const import query_topic, redacted, redacted_error, runtime_identifiers
from .anycubic_local.exceptions import CloudModeError
from .anycubic_local.handshake import HandshakeResult, do_handshake
from .anycubic_local.models import (
    AceBox,
    ExternalSpool,
    LightState,
    ObjectImages,
    PrinterState,
    apply_fan,
    apply_progress,
    apply_temperature,
    merge_boxes,
    merge_external_spool,
    parse_extfilbox,
    parse_file_details,
    parse_info,
    parse_light,
    parse_multicolorbox,
)
from .const import (
    ACE_DRYING_DEFAULT_DURATION_MIN,
    ACE_DRYING_DEFAULT_TEMP,
    ACE_MODEL_NAMES,
    BUILTIN_ACE_MODELS,
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
_QUERY_TYPES = ("info", "tempature", "fan", "light", "multiColorBox")

# `extfilbox` is NOT here. Polling it with action "query" is answered by nothing at all —
# confirmed on a Kobra 3 V2 whose holder had a spool loaded and no ACE attached, where a
# full connect + poll cycle answered every other type and never once sent `extfilbox`
# (issue #12). It arrives only when the Slicer connects or the ACE is unplugged. These are
# "reportInfo" is the one it answers: a reload with the ACE off returned a report three
# timestamp units after the connect multiColorBox, with nothing touched physically, and
# only one report came back for the two actions tried. Asked once at connect rather than
# every poll, because this covers the one case pushes miss — a spool already loaded at
# startup. Note the ANSWER is partial; see parse_extfilbox.
_EXTFILBOX_PROBE_ACTIONS = ("reportInfo",)

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
    # The printer's renders of the running job. None until a fileDetails answer lands.
    object_images: ObjectImages | None = None


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
        # Is a multi-material unit attached right now? None until one report arrives.
        self.ace_present: bool | None = None
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
        # Filename we have already asked fileDetails about, so one job asks once.
        self._file_details_asked: str | None = None

    def _build_and_connect(self):
        """Construct the transport (paho client + blocking tls_set) and connect.

        Runs in an executor — `tls_set()` loads CA certs from disk, which must not
        happen on the event loop.
        """
        transport = self._factory(self.hs, on_report=self._on_report,
                                  identifiers=self.redaction_identifiers,
                                  job_names=self._job_names)
        transport.connect()
        for t in (*_QUERY_TYPES, *_CONNECT_ONLY_QUERY_TYPES):
            transport.query(t)
        self._probe_extfilbox(transport)
        return transport

    def _probe_extfilbox(self, transport) -> None:
        """Ask for the external spool with each action the firmware might accept (issue #12)."""
        topic = query_topic(self.hs.model_id, self.hs.device_id, "extfilbox")
        for action in _EXTFILBOX_PROBE_ACTIONS:
            transport.publish(topic, json.dumps({
                "type": "extfilbox", "action": action,
                "timestamp": int(time.time() * 1000),
                "msgid": uuid.uuid4().hex, "data": None}))

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
            #
            # Named by neither address nor serial: an UpdateFailed lands in Home Assistant's
            # ordinary ERROR log, which gets pasted into issues with no debug logging on at
            # all, and the serial here is not even the reporter's own printer.
            raise UpdateFailed(
                "the configured address now answers for a different printer")
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
            raise ConfigEntryAuthFailed(self._error_text(err)) from err
        except UpdateFailed:
            raise
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"reconnect failed: {self._error_text(err)}") from err
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

    @property
    def redaction_identifiers(self) -> tuple[str, ...]:
        """What redacted() has to scrub for this printer, wherever it turns up.

        The handshake's identifiers plus the address the user entered, which no handshake
        reports. Read from the live handshake on every use rather than kept, so a
        re-handshake cannot leave the list describing a session that is gone. (Not the
        device registry's "identifiers": these are strings to keep out of anything shared.)
        """
        return runtime_identifiers(self.hs, self.host)

    def _job_names(self) -> tuple[str, ...]:
        """What is printing, for redacted() to scrub by value (see its `job_names`).

        A report that names the job is scrubbed on its own evidence. This covers what does
        not: a report of another type, a command. Also called from the transport, on paho's
        thread, each time it logs a report; it only reads one attribute.
        """
        name = self.data.printer.filename
        return (name,) if name else ()

    def _error_text(self, err: Exception) -> str:
        """An exception's text for a message Home Assistant logs at its normal level.

        The entered address is handed over as it stands, as well as through
        redaction_identifiers, which leaves a one-word hostname out: in a payload that word
        is ordinary text, in an error it is the address.
        """
        return redacted_error(err, (*self.redaction_identifiers, self.host))

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
        #
        # Logged as a redacted COPY: the topic embeds the device id, and a file_details
        # request names the file being printed. What is published below is the original
        # body, or the printer could not act on it.
        #
        # Making the line must not cost the command, which is published below whatever
        # happens here. If it cannot be made, one fixed line says so: the command's name,
        # which is ours, and nothing of the topic, the payload or the error.
        if _LOGGER.isEnabledFor(logging.DEBUG):
            try:
                ids, names = self.redaction_identifiers, self._job_names()
                _LOGGER.debug("publish %s -> %s %s", command, redacted(topic, ids, names),
                              json.dumps(redacted(payload, ids, names)))
            except Exception:  # noqa: BLE001
                _LOGGER.debug("publish %s: the line could not be written; sent as usual", command)
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
            if apply_progress(self.data.printer, data):
                self._request_file_details()
        elif msg_type == "multiColorBox":
            self.raw_multicolorbox = data
            # Everything below reads the boxes this printer can really have, not the list
            # as sent (see _real_boxes). The raw report above is kept as it came.
            boxes = self._real_boxes(data)
            # Attached units answer getInfo with a full box list; with nothing attached the
            # list comes back empty. Tracked separately from data.ace because merge_boxes
            # keeps every box it has ever seen — deliberately, so devices and their entity
            # IDs survive — which means data.ace can never report a unit going away (#12).
            #
            # A list that had entries and none of them a box says neither: it is the "no
            # box" entry a finished feed sends, on a printer whose box is still attached.
            # Reading it as "nothing attached" would take that box's entities away until
            # the next poll, so what was known is kept.
            if boxes or not data.get("multi_color_box"):
                self.ace_present = bool(boxes)
            if boxes:
                # An ACE unit is attached, so the bare spool holder is not in use. Reconnecting
                # the ACE sends no closing `extfilbox` — the reports simply stop (confirmed on a
                # Kobra 3 V2, issue #12) — so without this the sensor would hold the last spool
                # forever. Read from the RAW report, not self.data.ace: merge_boxes deliberately
                # keeps previously-seen boxes, so data.ace never empties when a unit is unplugged.
                self.data.external_spool = None
            self.data.ace = merge_boxes(
                self.data.ace, parse_multicolorbox({"multi_color_box": boxes}))
            self._sync_ace_device_model()
        elif msg_type == "file":
            # Answer to the fileDetails request we send at the start of a job (issue #13).
            # parse_file_details returns None for the other payloads that share this topic.
            images = parse_file_details(data)
            if images is not None:
                current = self.data.printer.filename
                if images.filename and current and images.filename != current:
                    # A late answer for the previous print. Applying it would show the
                    # last object while a different one is on the plate.
                    _LOGGER.debug("file report: ignoring details for a finished job")
                else:
                    self.data.object_images = images
        elif msg_type == "extfilbox":
            # The bare spool holder, reported only when no ACE unit is attached (issue #12).
            # Merged rather than replaced: the answer to our connect query omits the load
            # state that a pushed report carries.
            self.data.external_spool = merge_external_spool(
                self.data.external_spool, parse_extfilbox(data))
        elif msg_type == "light":
            # None means the report did not say how the chamber light is (a shape we do not
            # know, or another lamp), so keep the state we have. The answer to a light
            # command used to be such a shape, and installing a default for it switched the
            # entity off after every command, ours or another client's, until the next poll
            # put it right (issue #14).
            light = parse_light(data)
            if light is not None:
                self.data.light = light
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

    def _real_boxes(self, data: dict) -> list[dict]:
        """The entries of a multiColorBox report that are boxes this printer can have.

        A negative id is a real unit only on a printer whose changer is built into the
        toolhead (BUILTIN_ACE_MODELS), which reports it as -1. Every other printer uses -1
        to say "no box": when a feed or an unload finishes it sends one entry with id -1 and
        loaded_slot -1. Taken for a box, that was merged, and the entity scan registered it
        as a built-in unit the printer does not have, with six entities. It is dropped here,
        where the model is known, before anything can be made of it.

        An entry with no id, or one that is not a whole number, names no box either. It is
        skipped so the boxes beside it are still applied, where it used to raise in the
        report handler. (bool is an int to Python, and not an id.)
        """
        entries = data.get("multi_color_box")
        if not isinstance(entries, list):
            return []
        builtin = self.hs.model_id in BUILTIN_ACE_MODELS
        return [entry for entry in entries
                if isinstance(entry, dict) and type(entry.get("id")) is int
                and (entry["id"] >= 0 or builtin)]

    @callback
    def _request_file_details(self) -> None:
        """Ask the printer for the current job's thumbnail and top view (issue #13).

        Driven off `print` rather than a poll because the printer never volunteers `file` —
        it only answers the Slicer — and because a job names its file exactly once. Asking
        per poll would re-fetch several hundred KB of base64 every 30 seconds.
        """
        name = self.data.printer.filename
        if not name or name == self._file_details_asked:
            return
        self._file_details_asked = name
        self.hass.async_create_task(
            self.async_send_command("file_details", filename=name))

    def drying_temp(self, box_id: int) -> int:
        return self._drying_temps.get(box_id, ACE_DRYING_DEFAULT_TEMP)

    def set_drying_temp(self, box_id: int, value: int) -> None:
        self._drying_temps[box_id] = value

    def drying_hours(self, box_id: int) -> int:
        return self._drying_hours.get(box_id, ACE_DRYING_DEFAULT_DURATION_MIN // 60)

    def set_drying_hours(self, box_id: int, value: int) -> None:
        self._drying_hours[box_id] = value

    @callback
    def registered_device(self, identifier: tuple[str, str]) -> dr.DeviceEntry | None:
        """This entry's device with `identifier`, or None if it is not registered.

        Home Assistant used to keep an identifier unique across the whole registry and looked
        a device up by it alone (async_get_device). It no longer does, and wants the config
        entry named as well (async_get_device_by_identifier); the old lookup is on its way
        out. The new one is used wherever it exists, the old one where it does not, which
        includes 2024.9.
        """
        registry = dr.async_get(self.hass)
        by_identifier = getattr(registry, "async_get_device_by_identifier", None)
        if by_identifier is None:
            return registry.async_get_device(identifiers={identifier})
        entry = self.config_entry
        return by_identifier(identifier, entry.entry_id) if entry is not None else None

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
            device = self.registered_device((DOMAIN, f"{self.hs.serial}_{ace_suffix(box.id)}"))
            if device is not None and (device.name != name or device.model != model):
                registry.async_update_device(device.id, name=name, model=model)
