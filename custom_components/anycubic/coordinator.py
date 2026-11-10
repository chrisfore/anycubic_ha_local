"""Push coordinator: owns the transport, holds merged PrinterState + ACE boxes."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .anycubic_local import mqtt as mqtt_mod
from .anycubic_local.commands import build as build_command, build_print_start
from .anycubic_local.const import (
    JOB_PHASES,
    LIGHT_TYPE_CAMERA,
    PAUSED_PHASES,
    QUERY_TYPES,
    REASON_TEXT,
    STATE_FREE,
    STOPPED_PHASES,
    query_topic,
    redacted,
    redacted_error,
    runtime_identifiers,
)
from .anycubic_local.exceptions import CloudModeError
from .anycubic_local.handshake import HandshakeResult, do_handshake
from .anycubic_local.models import (
    AceBox,
    ExternalSpool,
    JobReason,
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
    REPORT_GAP_BOUND,
    ace_device_model,
    ace_device_name,
    ace_suffix,
)
from .store import LastFile, PrintState, PrintStore

_LOGGER = logging.getLogger(__name__)

# `extfilbox` is NOT in QUERY_TYPES (anycubic_local/const.py), the list polled from. Polling it with action "query" is answered by nothing at all —
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
# A printer that is switched off is re-handshaked on every poll, which is right — it is
# how it is found again — but each attempt was a WARNING, and one user's log held twelve
# thousand of them from six days (issue #15). The first attempt of an outage is still a
# WARNING; after that one is repeated at that level this often, and the rest are DEBUG.
RECOVERY_LOG_REMINDER = 3600.0

# Starting a print (async_start_print). The printer answers a start on the `print` topic, with
# a code: 200 took it, 10101 says the previous task is still active. Every start is sent once
# only, a busy answer included (see async_start_print for why). With no answer at all, a
# short watch for the job follows, asking for `info` now and then: a large 3MF can take a
# while to unpack before the job shows.
START_REPLY_TIMEOUT = 10.0
START_WATCH_TIMEOUT = 20.0
START_WATCH_QUERY_EVERY = 5.0
START_ACCEPTED = 200
START_BUSY = 10101
# Any job phase, its end included: a job that has finished or stopped still left something
# on the bed. A busy printer with no job (drying, loading filament) is in none of these.
_JOB_SEEN = JOB_PHASES | STOPPED_PHASES | {"finished"}


def _after_colon(reason: str) -> str:
    """A reason as it reads after a colon: "Filament broken" becomes "filament broken".

    A first word in capitals stays as it is, so "ACE Pro response timeout" keeps its name.
    """
    return reason[:1].lower() + reason[1:] if reason[1:2].islower() else reason


@dataclass
class AnycubicData:
    printer: PrinterState = field(default_factory=PrinterState)
    ace: list[AceBox] = field(default_factory=list)
    light: LightState = field(default_factory=LightState)
    # The Kobra 4's camera light (issue #17). None until the printer says how it is.
    camera_light: LightState | None = None
    # None until the printer reports one. It only reports `extfilbox` while no ACE unit is
    # attached, so None means "no bare spool in use", not "not read yet".
    external_spool: ExternalSpool | None = None
    # The printer's renders of the running job. None until a fileDetails answer lands.
    object_images: ObjectImages | None = None
    # Why the job is paused or stopped. Kept here, not in PrinterState, because every
    # `info` report rebuilds that, and info never carries the reason. None while the job
    # runs, and once it is over.
    reason: JobReason | None = None


# What the print event entity fires (see AnycubicCoordinator._track_status).
EVENT_STARTED = "started"
EVENT_PAUSED = "paused"
EVENT_RESUMED = "resumed"
EVENT_FINISHED = "finished"
EVENT_STOPPED = "stopped"
EVENT_ERROR = "error"
EVENT_TYPES = (EVENT_STARTED, EVENT_PAUSED, EVENT_RESUMED, EVENT_FINISHED, EVENT_STOPPED,
               EVENT_ERROR)
# A job ends idle or finished; any other status outside JOB_PHASES (a stop, or a busy
# printer with no job such as drying) is not a job either.
_JOB_ENDED = frozenset({"idle", "finished"})


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
        # When a recovery attempt was last logged at WARNING (see RECOVERY_LOG_REMINDER).
        self._recovery_warned_at: float | None = None
        # Called when the printer answers again after a recovery (see _apply).
        self._session_listeners: list[Callable[[], None]] = []
        # Filename we have already asked fileDetails about, so one job asks once.
        self._file_details_asked: str | None = None
        # Told of print events (see async_add_event_listener).
        self._event_listeners: list[Callable[[str, dict], None]] = []
        # The status the last info report gave, to turn changes into events. Unset until
        # the first one: Home Assistant starting mid-print is not a new job.
        self._status: str | None = None
        self._status_seen = False
        # The fault an error event was fired for. A pause is reported twice, pausing and
        # paused, with the same code; it is one fault.
        self._error_code: int | None = None
        # The ids of the multi-material units attached right now, as the last report that
        # said so gave them. data.ace keeps every box ever seen, so it cannot say this.
        self.attached_box_ids: frozenset[int] = frozenset()

        # Printing from Home Assistant (see actions.py). The bed flag and the last file are
        # in print_state; only the last file is kept across restarts. The defaults are the
        # safe ones, so the bed needs clearing.
        self.print_state = PrintState()
        self._print_store: PrintStore | None = None
        # Monotonic time of the last report. Kept apart from _last_report, which the session
        # watchdog winds forward when it reconnects: a gap must not be hidden by that.
        self._report_at: float | None = None
        # One submission at a time to this printer, upload and start together.
        self.submit_lock = asyncio.Lock()
        # The file being sent, for the redactor (see _job_names), and the secrets held while
        # it is sent (see redaction_identifiers).
        self._sending: tuple[str, ...] = ()
        self._held_secrets: tuple[str, ...] = ()
        # The answer a start is waiting for (see async_start_print).
        self._start_reply: asyncio.Future[dict] | None = None
        # How the last submission went: the refusal's key, or how the start ended. For
        # diagnostics; never a name.
        self.last_guard_outcome: str | None = None

    def _build_and_connect(self):
        """Construct the transport (paho client + blocking tls_set) and connect.

        Runs in an executor — `tls_set()` loads CA certs from disk, which must not
        happen on the event loop.
        """
        transport = self._factory(self.hs, on_report=self._on_report,
                                  identifiers=self.redaction_identifiers,
                                  job_names=self._job_names, on_reason=self._on_reason,
                                  on_start_reply=self._on_start_reply)
        try:
            transport.connect()
            for t in (*QUERY_TYPES, *_CONNECT_ONLY_QUERY_TYPES):
                transport.query(t)
            self._probe_extfilbox(transport)
        except Exception:
            # connect() starts paho's network thread. A transport that got that far and
            # then failed here is handed to nobody, so nobody would ever stop it: the
            # thread would keep the dead session reconnecting for the life of Home
            # Assistant. Harmless on one that never connected.
            transport.disconnect()
            raise
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
        now = time.monotonic()
        if (self._recoveries == 1 or self._recovery_warned_at is None
                or now - self._recovery_warned_at >= RECOVERY_LOG_REMINDER):
            level, self._recovery_warned_at = logging.WARNING, now
        else:
            level = logging.DEBUG
        _LOGGER.log(level, "re-handshaking printer (attempt %s): %s",
                    self._recoveries, reason)
        try:
            await self.hass.async_add_executor_job(self._rebuild)
        except CloudModeError as err:
            # LAN Mode was turned off on the printer — same reauth path as setup.
            raise ConfigEntryAuthFailed(self._error_text(err)) from err
        except UpdateFailed:
            raise
        except Exception as err:
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
        for t in QUERY_TYPES:
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
        #
        # First, before a recovery winds any clock forward: a silence longer than the gap
        # bound means nobody knows what happened at the printer, so the bed needs clearing.
        if self._report_gap():
            self._raise_bed_flag("no report within the gap bound")
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

    def _on_reason(self, reason: JobReason) -> None:
        """Called on the paho network thread, as _on_report is, and marshalled the same way."""
        self.hass.loop.call_soon_threadsafe(self._apply_reason, reason)

    def _on_start_reply(self, envelope: dict) -> None:
        """Called on the paho network thread with a print start's answer, marshalled the same way."""
        self.hass.loop.call_soon_threadsafe(self._apply_start_reply, envelope)

    @callback
    def _apply_start_reply(self, envelope: dict) -> None:
        # Only an answer with a code says yes or no. Another client's start is answered on
        # the same topic, and one arriving while ours waits is taken for ours: the answer
        # names no file to tell them apart by. That needs another client starting a job
        # within the same few seconds, and the job is seen either way.
        code = envelope.get("code")
        waiter = self._start_reply
        if type(code) is int and waiter is not None and not waiter.done():
            waiter.set_result(envelope)

    @callback
    def _apply_reason(self, reason: JobReason) -> None:
        self.data.reason = reason
        # One error per fault the user did not cause. Fired here, on the `print` report,
        # rather than on the next info: that can be half a minute away, and this is the
        # event a phone notification hangs on.
        if not reason.by_user and reason.code != self._error_code:
            self._error_code = reason.code
            self._fire(EVENT_ERROR, {"reason_code": reason.code, "reason": reason.reason})
        self.async_update_listeners()

    @callback
    def async_add_event_listener(self, listener: Callable[[str, dict], None]) -> Callable[[], None]:
        """Call `listener(event_type, attributes)` on each print event. Returns the remover."""
        self._event_listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._event_listeners:
                self._event_listeners.remove(listener)

        return remove

    @callback
    def _fire(self, event_type: str, attributes: dict | None = None) -> None:
        printer = self.data.printer
        attributes = {"filename": printer.filename, "progress": printer.progress,
                      **(attributes or {})}
        for listener in list(self._event_listeners):
            listener(event_type, attributes)

    @callback
    def _track_status(self) -> None:
        """Turn a change of the job's status, as info gives it, into a print event."""
        status = self.data.printer.status
        if not self._status_seen:
            self._status_seen, self._status = True, status
            return
        before, self._status = self._status, status
        if status == before:
            return
        if status not in PAUSED_PHASES | STOPPED_PHASES:
            # Running again, or over: what paused or stopped it no longer applies.
            self.data.reason, self._error_code = None, None
        reason = self.data.reason
        why = {"reason_code": reason.code, "reason": reason.reason} if reason else {}
        if status in JOB_PHASES and before not in JOB_PHASES:
            self._fire(EVENT_STARTED)
        if status in PAUSED_PHASES and before not in PAUSED_PHASES:
            self._fire(EVENT_PAUSED, why)
        elif before in PAUSED_PHASES and status in JOB_PHASES - PAUSED_PHASES:
            self._fire(EVENT_RESUMED)
        elif status in STOPPED_PHASES and before not in STOPPED_PHASES:
            self._fire(EVENT_STOPPED, why)
        elif status in _JOB_ENDED and before in JOB_PHASES:
            self._fire(EVENT_FINISHED)

    async def async_start_capture(self) -> None:
        """Start camera capture the way the official client does.

        The slicer's LAN camera always sends stopCapture, pauses, then startCapture —
        new-generation firmware (Kobra 4 / X) doesn't begin pushing on a bare start —
        and the startCapture answer carries the tokenized stream URL, captured into
        video_stream_url by _apply. S1-family printers answer with no URL; the wait
        just ends early and callers fall back to the info-report URL.

        The wait ends on the startCapture answer only. The stopCapture answer is a
        video report too, and from a slow printer it arrives after the start has been
        sent; the transport holds it back (issue #15), so it cannot end this early.
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
    def has_transport(self) -> bool:
        """Is there a session a command could be published into right now?

        False from a failed rebuild until the next one that works. async_send_command
        drops commands then, so a caller with something to wait for can skip the wait.
        """
        return self._transport is not None

    @callback
    def async_add_session_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` when the printer answers again after a recovery.

        The first report after a re-handshake, not the re-handshake itself: reconnecting
        can keep working against a printer that never answers. The camera uses it to
        start capture again once a power-cycled printer is back (issue #15). Returns
        the function that removes the listener.
        """
        self._session_listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._session_listeners:
                self._session_listeners.remove(listener)

        return remove

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

        While a file is being sent, the secrets held for it (the printer's upload token)
        are on the list as well; see `holding`.
        """
        return (*runtime_identifiers(self.hs, self.host), *self._held_secrets)

    def _job_names(self) -> tuple[str, ...]:
        """What is printing, for redacted() to scrub by value (see its `job_names`).

        A report that names the job is scrubbed on its own evidence. This covers what does
        not: a report of another type, a command. Also called from the transport, on paho's
        thread, each time it logs a report; it only reads two attributes. While a file is
        being sent, its names are here too (see `submitting`).
        """
        name = self.data.printer.filename
        return ((name,) if name else ()) + self._sending

    @contextmanager
    def submitting(self, *names: str | None) -> Iterator[None]:
        """Scrub these file names out of every log line while the block runs."""
        self._sending = tuple(n for n in names if n)
        try:
            yield
        finally:
            self._sending = ()

    @contextmanager
    def holding(self, held: Iterable[str]) -> Iterator[None]:
        """Treat these values as identifiers while the block runs, and forget them after.

        For the printer's upload token: redacted_error masks an identifier wherever it
        stands as a word, however short, and _error_text hands it every identifier there is.
        """
        self._held_secrets = tuple(s for s in held if s)
        try:
            yield
        finally:
            self._held_secrets = ()

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
        await self._async_publish(command, topic, payload)

    async def _async_publish(self, command: str, topic: str, payload: dict) -> None:
        """Publish a built command, with one redacted debug line saying what went out."""
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
        transport = self._transport
        if transport is None:
            # A recovery failed while this was being built. Nothing to publish into.
            _LOGGER.debug("dropping command %s: no transport", command)
            return
        await self.hass.async_add_executor_job(transport.publish, topic, body)

    @callback
    def _apply(self, msg_type: str, data: dict) -> None:
        # Any report at all proves the session is alive; that, not a successful
        # publish, is what clears the watchdog.
        self._last_report = time.monotonic()
        recovered, self._recoveries = self._recoveries, 0
        self.seen_report_types.add(msg_type)
        # The first report after a silence longer than the gap bound: whatever happened at
        # the printer meanwhile, nobody saw it. Judged before this report counts as the last.
        if self._report_gap():
            self._raise_bed_flag("report after a gap")
        self._report_at = self._last_report
        if msg_type == "info":
            self.data.printer = parse_info(data)
            features = data.get("features")
            if isinstance(features, dict):
                self.raw_features = features
            self._track_status()
            if self._bed_may_hold_something(data):
                self._raise_bed_flag("the printer was busy, or a job was reported")
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
                self._raise_bed_flag("a job's progress was reported")
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
                self.attached_box_ids = frozenset(box["id"] for box in boxes)
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
            # The camera light is tracked apart, by the same rule (issue #17).
            camera_light = parse_light(data, LIGHT_TYPE_CAMERA)
            if camera_light is not None:
                self.data.camera_light = camera_light
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
        if recovered:
            # The one line that closes an outage in the log. INFO, as Home Assistant's
            # own "recovered" lines are; the attempts it answers were the warnings.
            _LOGGER.info("printer session is back after %s re-handshake attempt(s)", recovered)
            self._recovery_warned_at = None
            for listener in list(self._session_listeners):
                listener()

    @callback
    def async_notify_write(self) -> None:
        """Tell the entities a control has just changed self.data, ahead of the printer.

        Listeners only, for the reason _apply gives, and one more: a write is not news
        from the printer, so it must not mark the last update good either.
        async_set_updated_data does both. It re-arms the refresh timer, so an automation
        writing more often than the poll interval stopped the polls — and the session
        watchdog, which runs inside the poll — for as long as it kept writing; and it
        sets last_update_success, so a write to a printer that could not be reached
        showed every entity as available again, with the values it had when it was lost.
        """
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
        registry = dr.async_get(self.hass)
        for box in self.data.ace:
            if box.model_id is None or str(box.model_id) not in ACE_MODEL_NAMES:
                continue
            model = ace_device_model(box.id, box.model_id)
            name = ace_device_name(box.id, box.model_id)
            device = self.registered_device((DOMAIN, f"{self.hs.serial}_{ace_suffix(box.id)}"))
            if device is not None and (device.name != name or device.model != model):
                registry.async_update_device(device.id, name=name, model=model)

    # ------------------------------------------------------- printing from Home Assistant
    #
    # The actions and buttons are in actions.py and button.py. What lives here is what has to
    # watch every report: whether the bed needs clearing, whether the printer is reachable
    # and idle, and the printer's answer to a start.

    async def async_load_print_state(self, store: PrintStore) -> None:
        """Take up the last file on record, before the session starts.

        Every load starts with the bed needing clearing, whatever is on record: Home
        Assistant was not watching while it was down, and no stored time can say for how
        long, since the host's clock may be wrong until it syncs.
        """
        self._print_store = store
        self.print_state = await store.async_load()
        self.print_state.bed_flag = True

    async def async_save_print_state(self) -> None:
        """Write the remembered state out now: on unload, so that a reload reads it fresh."""
        if self._print_store is not None:
            await self._print_store.async_save(self.print_state)

    @callback
    def _schedule_print_save(self) -> None:
        if self._print_store is not None:
            self._print_store.async_schedule_save(self.print_state)

    def report_age(self) -> float | None:
        """Seconds since the printer's last report in this session; None before the first."""
        return None if self._report_at is None else time.monotonic() - self._report_at

    def _report_gap(self) -> bool:
        """Has the printer been silent, in this session, for longer than the gap bound?

        Before the session's first report that is a gap too. On the monotonic clock only, so
        a wall clock that jumps can neither hide a gap nor invent one.
        """
        age = self.report_age()
        return age is None or age > REPORT_GAP_BOUND

    @property
    def reachable(self) -> bool:
        """Has the printer reported within the gap bound?"""
        age = self.report_age()
        return age is not None and age <= REPORT_GAP_BOUND

    @property
    def idle(self) -> bool:
        """Is the printer free, with no job in any phase, and no attached unit drying?"""
        printer = self.data.printer
        if (printer.raw_state != STATE_FREE or printer.status in JOB_PHASES
                or printer.printing or printer.paused):
            return False
        return not (self.ace_present and any(
            box.drying_active for box in self.data.ace if box.id in self.attached_box_ids))

    def _job_seen(self) -> bool:
        printer = self.data.printer
        return printer.printing or printer.paused or printer.status in _JOB_SEEN

    def _bed_may_hold_something(self, info: dict) -> bool:
        """Does this `info` report leave any doubt that the bed is as it was?

        Anything but "free" counts, busy work with no print included, and so does a current
        job in any status. Seen on the printer: after a refused start it reported "busy" with
        no job and the last project "failed", while a real job ran on. A clear bed costs one
        more "Bed is clear" press; a missed job costs a nozzle driven into a part. The last
        project of a free printer is the previous job, already seen, and does not count, or
        the flag could never be cleared.
        """
        project = info.get("project")
        return (self.data.printer.raw_state != STATE_FREE or self._job_seen()
                or (isinstance(project, dict) and bool(project)))

    @property
    def bed_needs_clearing(self) -> bool:
        return self.print_state.bed_flag

    @property
    def bed_clear_allowed(self) -> bool:
        """Can "Bed is clear" be pressed: the flag is on and the printer is known to be idle."""
        return self.print_state.bed_flag and self.reachable and self.idle

    @callback
    def _raise_bed_flag(self, why: str) -> None:
        if self.print_state.bed_flag:
            return
        self.print_state.bed_flag = True
        _LOGGER.debug("the bed needs clearing: %s", why)
        self.async_update_listeners()

    @callback
    def async_mark_bed_clear(self) -> None:
        """The user says the bed is clear: the only thing that turns the flag off."""
        if not self.print_state.bed_flag:
            return
        if not (self.reachable and self.idle):
            raise ServiceValidationError(translation_domain=DOMAIN,
                                         translation_key="clear_while_busy")
        self.print_state.bed_flag = False
        self.async_update_listeners()

    @property
    def last_file(self) -> LastFile | None:
        return self.print_state.last_file

    @callback
    def async_set_last_file(self, record: LastFile) -> None:
        self.print_state.last_file = record
        self._schedule_print_save()
        self.async_update_listeners()

    async def async_start_print(self, *, name: str, size: int, md5: str, filetype: int,
                                ams_settings: dict, again: bool = False) -> None:
        """Start a file that is already on the printer, and hear that it started.

        Returns once the printer has taken the job, or once a job is seen. Raises when the
        printer refuses, when it is busy, and when it says nothing and no job appears.
        `again` is for Print again, whose refusal tells the user to send the file again.
        """
        if self._transport is None:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="printer_not_connected")
        taskid = str(100_000_000 + secrets.randbelow(900_000_000))
        topic, payload = build_print_start(
            self.hs.model_id, self.hs.device_id, taskid=taskid, filename=name,
            filesize=size, md5=md5, filetype=filetype, ams_settings=ams_settings)
        answer = await self._async_send_start(topic, payload)
        if answer is not None:
            code = answer["code"]
            if code == START_ACCEPTED:
                self._raise_bed_flag("a print was started")
                return
            if code == START_BUSY:
                # Never tried again. Seen on the printer: a refused start overwrites its
                # record of the job that is running, which then reads as failed under the
                # refused task's id and never reports finishing. A second start would do
                # that damage twice.
                raise HomeAssistantError(translation_domain=DOMAIN,
                                         translation_key="printer_busy")
            # The code itself is for a debug log, not for the user.
            _LOGGER.debug("the printer refused a print start with code %s", code)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="print_again_refused" if again else "start_refused",
                translation_placeholders={
                    "reason": _after_colon(REASON_TEXT.get(code, "no reason given"))})
        if await self._async_job_appears():
            self._raise_bed_flag("a job was seen after a start")
            return
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="start_unconfirmed")

    async def _async_send_start(self, topic: str, payload: dict) -> dict | None:
        """Publish one start and wait for its answer; None if none comes in time."""
        waiter: asyncio.Future[dict] = self.hass.loop.create_future()
        self._start_reply = waiter
        try:
            await self._async_publish("print_start", topic, payload)
            async with asyncio.timeout(START_REPLY_TIMEOUT):
                return await waiter
        except TimeoutError:
            return None
        finally:
            self._start_reply = None

    async def _async_job_appears(self) -> bool:
        """Watch for a job for a short while, asking for `info` now and then."""
        loop = self.hass.loop
        deadline = loop.time() + START_WATCH_TIMEOUT
        ask_at = loop.time()
        while not self._job_seen():
            now = loop.time()
            if now >= deadline:
                return False
            transport = self._transport
            if now >= ask_at and transport is not None:
                await self.hass.async_add_executor_job(transport.query, "info")
                ask_at = now + START_WATCH_QUERY_EVERY
            await asyncio.sleep(min(0.5, deadline - now))
        return True
