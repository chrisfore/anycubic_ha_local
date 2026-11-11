"""Clear the bed: what happens at the end of a print Home Assistant prepared.

A prepared print ends paused over the purge chute, with the head lifted. Home Assistant never
resumes that pause on its own. A press of "Clear the bed" is checked, and only if every check
passes is the one resume sent; the file then cools the nozzle and pushes the parts out of the
door. Everything that goes wrong ends the same way: no push, the part left where it is, the
bed still needing clearing, and the owner told.

States: `off`; `armed` (a prepared print is running); `ready` (it is paused at its end, and a
press is possible); `pushing`; `done`; `failed`; `expired` (the motors can no longer be
trusted to be on); `stopped` (the job ended with no push).

A job is Home Assistant's own only if the printer echoes the task id of the start that was
sent for it. Any other paused job that carries the marker in its name, or that is paused with
the bed heater off and is not plainly mid-print, is stopped: it may be a prepared copy
started from somewhere else, and its pause must never be resumed into a sweep.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.components import persistent_notification
from homeassistant.const import STATE_ON
from homeassistant.core import Event, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from .anycubic_local.const import PAUSED_PHASES, STATE_FREE
from .anycubic_local.models import JobReason
from .anycubic_local.printfile import finite_number, is_marked, split_suffix
from .const import (
    CONF_DOOR_SENSOR,
    DOMAIN,
    DOOR_SENSOR_OPTION_ENABLED,
    ENCLOSED_MODELS,
    MODEL_NAMES,
    PUSH_OFF_MARKER,
    PUSH_OFF_MODELS,
    REPORT_GAP_BOUND,
)
from .store import ArmedJob

_LOGGER = logging.getLogger(__name__)

STATE_OFF = "off"
STATE_ARMED = "armed"
STATE_READY = "ready"
STATE_PUSHING = "pushing"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_EXPIRED = "expired"
STATE_STOPPED = "stopped"
STATES = (STATE_OFF, STATE_ARMED, STATE_READY, STATE_PUSHING, STATE_DONE, STATE_FAILED,
          STATE_EXPIRED, STATE_STOPPED)

EVENT_CLEAR_READY = "clear_ready"
EVENT_CLEAR_DONE = "clear_done"
EVENT_CLEAR_FAILED = "clear_failed"

# How long after the pause a press is still taken, in seconds, and the margin kept inside
# it; None for no limit. Measured on a Kobra S1 Max (firmware 2.7.1.4): paused for 95
# minutes, the motors stayed on and homed, the nozzle held 140 C, and the resume ran the
# rest of the file cleanly. Nothing longer has been tried, so the window stays inside that.
MOTOR_WINDOW: float | None = 90 * 60.0
WINDOW_MARGIN = 60.0

# How long after a press the printer may still report the pause, in seconds, before the
# resume counts as not taken. A report sent just before the resume arrived says "paused".
RESUME_GRACE = 30.0

# The bed is this cool, or cooler, before a push: what print farms release parts at.
BED_LIMITS = {"PLA": 28, "PETG": 25}
CHAMBER_LIMIT = 30
# How often the printer is asked how it is while a press is possible or a sweep is running.
FAST_POLL = timedelta(seconds=5)

# What the owner is told. Constants, as strings.json has no place for a notification's text.
MESSAGE_READY = "Print finished. Ready to clear the bed."
MESSAGE_EXPIRED = ("Clear the bed is no longer available for this print. Press Stop, then "
                   "clear the bed by hand.")
MESSAGE_PAUSED_EARLIER = ("This print paused earlier, so Clear the bed will not be offered. "
                          "Press Stop when it finishes, then clear the bed by hand.")
MESSAGE_FOREIGN = ("A paused print that Home Assistant did not start was stopped, because "
                   "resuming it could push parts into a closed door. Clear the bed by hand.")
MESSAGE_FAILED = "Clear the bed did not finish: {reason}. Check the printer, then clear the bed by hand."

REASON_PAUSED = "the printer paused during the push"
REASON_ERROR = "the printer reported a fault during the push"
REASON_DOOR = "the door sensor stopped reading open"
REASON_LOST = "the printer stopped answering during the push"
REASON_OUTSIDE = "the print was resumed from somewhere other than Home Assistant"
REASON_UNWATCHED = "Home Assistant restarted during the push"
REASON_ENDED = "the print ended before the push finished"


def bed_limit_for(families) -> int:
    """The strictest bed limit among the materials a file prints with."""
    return min(BED_LIMITS[family] for family in families)


def _stem(name: str | None) -> str | None:
    """A file's name with no folder and no suffix. The printer reports a job from an
    archive under a working folder and as plain G-code: ".3mf_temp/<stem>.gcode"."""
    if not name:
        return None
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    parts = split_suffix(base)
    return parts[0] if parts else base


def _refusal(key: str, **placeholders) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=key,
        translation_placeholders={k: str(v) for k, v in placeholders.items()} or None)


class Clearing:
    """One printer's Clear the bed state. Owned by the coordinator, and fed by it."""

    def __init__(self, coordinator) -> None:
        self._c = coordinator
        self.state = STATE_OFF
        self.reason: str | None = None
        # The task id of the job the printer is running, as its last `info` gave it.
        self.task: str | None = None
        # Monotonic time the push-off pause was seen to begin; None if nobody saw it begin.
        self._pause_at: float | None = None
        self._seen_running = False
        self._was_paused = False
        self._door_opened = False
        self._finished = False
        # Monotonic time of the press, and whether the job has been seen running since.
        self._pressed_at: float | None = None
        # Monotonic time of the last `info` report: the only report that says paused or not.
        self._info_at: float | None = None
        self._push_running = False
        # The paused job a stop was already sent for, so one pause gets one stop.
        self._stopped: str | None = None
        self._unsub_door = None
        self._unsub_poll = None

    # ------------------------------------------------------------------ the record

    @property
    def _job(self) -> ArmedJob | None:
        return self._c.print_state.armed

    @callback
    def async_loaded(self) -> None:
        """Take up the record a restart left. Nothing was watched while it was down."""
        job = self._job
        if job is None:
            return
        if job.state == STATE_PUSHING or job.resumed:
            # A sweep may have run with nobody watching. It is never resumed again, and if
            # the job is found paused it is stopped.
            job.resumed = True
            self._set(STATE_FAILED, REASON_UNWATCHED)
        else:
            job.state = STATE_ARMED
            self._set(STATE_ARMED)

    @callback
    def async_arm(self, *, name: str, md5: str, task_id: str, bed_limit: int) -> None:
        """A start is about to be sent for a prepared copy: remember it, before it is."""
        self._release()
        self._c.print_state.armed = ArmedJob(name=name, md5=md5, task_id=task_id,
                                             bed_limit=bed_limit)
        self._seen_running = self._was_paused = self._finished = False
        self._set(STATE_ARMED)
        self._save()

    @callback
    def async_disarm(self) -> None:
        """The printer turned the start down: there is no job to wait for."""
        if self._job is not None:
            self._retire(STATE_OFF)

    @callback
    def async_shutdown(self) -> None:
        self._release()

    # ------------------------------------------------------------------ what is read

    @property
    def door_entity(self) -> str | None:
        entry = self._c.config_entry
        if entry is None or not DOOR_SENSOR_OPTION_ENABLED:
            return None
        return entry.options.get(CONF_DOOR_SENSOR) or None

    @property
    def _paused(self) -> bool:
        printer = self._c.data.printer
        return printer.raw_state != STATE_FREE and (
            printer.paused or printer.status in PAUSED_PHASES)

    @property
    def _ours(self) -> bool:
        job = self._job
        # The task id of the start, and the name of the copy it started: a refused start
        # can leave its task id on a job that was already running.
        return (job is not None and self.task is not None and self.task == job.task_id
                and _stem(self._c.data.printer.filename) == _stem(job.name))

    @property
    def resume_locked(self) -> bool:
        """May nothing but a checked press resume the pause the printer is in?

        Yes for a pause in Home Assistant's own prepared job, in any job whose name carries
        the marker, and in any job paused with the bed heater off or its target unknown.
        """
        if self._c.hs.model_id not in PUSH_OFF_MODELS or not self._paused:
            return False
        printer = self._c.data.printer
        target = finite_number(printer.bed_target)
        return (self._ours or is_marked(printer.filename or "", PUSH_OFF_MARKER)
                or target is None or target == 0)

    @property
    def press_possible(self) -> bool:
        """Is a press worth offering: ready, inside the window, and the printer reporting."""
        return self.state == STATE_READY and self._c.reachable and not self._window_over()

    def _info_fresh(self) -> bool:
        """Has the printer said how the job stands within the gap bound?"""
        return (self._info_at is not None
                and time.monotonic() - self._info_at <= REPORT_GAP_BOUND)

    def _window_over(self) -> bool:
        if MOTOR_WINDOW is None:
            return False
        return (self._pause_at is None
                or time.monotonic() - self._pause_at > MOTOR_WINDOW - WINDOW_MARGIN)

    # ------------------------------------------------------------------ reports

    @callback
    def async_on_info(self, info: dict) -> None:
        """Every `info` report, after the coordinator has read it."""
        self._info_at = time.monotonic()
        printer = self._c.data.printer
        project = info.get("project")
        running = printer.raw_state != STATE_FREE and isinstance(project, dict) and bool(project)
        task = project.get("task_id") if running else None
        self.task = None if task is None or isinstance(task, bool) else str(task)
        job = self._job
        if not running:
            if printer.raw_state == STATE_FREE and job is not None and job.seen:
                self._job_ended(info)
            self._was_paused, self._stopped = False, None
            return
        if self._ours:
            self._own_job(job)
            return
        # Somebody else's job, or one that names no task.
        if job is not None and job.seen and self.task is not None:
            self._retire(STATE_FAILED if self.state == STATE_PUSHING else STATE_STOPPED,
                         REASON_ENDED if self.state == STATE_PUSHING else None)
        self._foreign_job()

    def _own_job(self, job: ArmedJob) -> None:
        paused = self._paused
        if not job.seen:
            job.seen = True
            self._save()
        if self.state == STATE_PUSHING:
            if not paused:
                self._push_running = True
                if self._c.data.printer.status == "finished":
                    self._finished = True
            elif self._push_running or self._pressed_at is None or (
                    time.monotonic() - self._pressed_at > RESUME_GRACE):
                # Paused again after running, or the resume never took.
                self._fail(REASON_PAUSED, stop=True)
        elif self.state == STATE_FAILED:
            if paused:
                self._stop_once()
        elif paused:
            target = finite_number(self._c.data.printer.bed_target)
            if self.state == STATE_ARMED and target == 0 and not job.paused_earlier:
                self._enter_ready()
            elif self.state == STATE_READY and self._window_over():
                self._expire()
        elif self.state in (STATE_READY, STATE_EXPIRED):
            # Running again, and not by a press: stop it before the sweep.
            self._fail(REASON_OUTSIDE, stop=True)
        elif self._was_paused and not job.paused_earlier:
            job.paused_earlier = True
            self._save()
            self._notify(MESSAGE_PAUSED_EARLIER)
        if not paused:
            self._seen_running = True
        self._was_paused = paused

    def _foreign_job(self) -> None:
        if not self._paused:
            self._stopped = None
            return
        if self._c.hs.model_id not in PUSH_OFF_MODELS:
            return
        printer = self._c.data.printer
        layer = finite_number(printer.current_layer)
        total = finite_number(printer.total_layers)
        mid_print = layer is not None and total is not None and 0 < layer < total
        if is_marked(printer.filename or "", PUSH_OFF_MARKER) or (
                finite_number(printer.bed_target) == 0 and not mid_print):
            if self._stop_once():
                self._notify(MESSAGE_FOREIGN)

    def _job_ended(self, info: dict) -> None:
        job = self._job
        last = info.get("last_project")
        finished = self._finished or (
            isinstance(last, dict) and str(last.get("task_id")) == job.task_id
            and last.get("state") == "finished")
        if self.state == STATE_PUSHING:
            if finished and self._push_running and self._stopped is None:
                # The sweep ran to its end with Home Assistant watching all of it.
                self._c.print_state.bed_flag = False
                self._retire(STATE_DONE)
                self._c.fire_event(EVENT_CLEAR_DONE, {"reason": None})
            else:
                self._fail(REASON_ENDED, stop=False)
                self._retire(STATE_FAILED, REASON_ENDED)
        elif self.state == STATE_FAILED:
            self._retire(STATE_FAILED, self.reason)
        elif self.state in (STATE_READY, STATE_EXPIRED):
            self._retire(STATE_STOPPED)
        else:
            self._retire(STATE_OFF)

    @callback
    def async_on_reason(self, reason: JobReason) -> None:
        """A `print` report said the job paused or stopped, and why."""
        if self.state == STATE_PUSHING:
            self._fail(REASON_PAUSED if reason.paused and reason.by_user else REASON_ERROR,
                       stop=reason.paused)

    @callback
    def async_on_gap(self) -> None:
        """The printer was silent for longer than the gap bound: nobody saw what it did."""
        self._seen_running = False
        self._pause_at = None
        if self.state == STATE_PUSHING:
            self._fail(REASON_LOST, stop=False)
        elif self.state == STATE_READY and self._window_over():
            self._expire()

    @callback
    def async_tick(self) -> None:
        """Any report at all: time may have run out."""
        if self.state == STATE_READY and self._window_over():
            self._expire()

    # ------------------------------------------------------------------ transitions

    def _set(self, state: str, reason: str | None = None) -> None:
        self.state, self.reason = state, reason
        job = self._job
        if job is not None and state in (STATE_ARMED, STATE_READY, STATE_PUSHING):
            job.state = state
        if state not in (STATE_READY, STATE_PUSHING):
            self._release()

    def _enter_ready(self) -> None:
        self._pause_at = time.monotonic() if self._seen_running else None
        self._door_opened = False
        self._set(STATE_READY)
        self._save()
        hass = self._c.hass
        door = self.door_entity
        if door and self._unsub_door is None:
            self._unsub_door = async_track_state_change_event(hass, [door], self._door_changed)
        if self._unsub_poll is None:
            self._unsub_poll = async_track_time_interval(hass, self._poll, FAST_POLL)
        if self._window_over():
            self._expire()
            return
        self._notify(MESSAGE_READY)
        self._c.fire_event(EVENT_CLEAR_READY, {"reason": None})

    def _expire(self) -> None:
        self._set(STATE_EXPIRED, "the time to clear the bed has passed")
        self._notify(MESSAGE_EXPIRED)
        self._c.fire_event(EVENT_CLEAR_FAILED, {"reason": STATE_EXPIRED})

    def _fail(self, reason: str, *, stop: bool) -> None:
        already = self.state == STATE_FAILED
        self._set(STATE_FAILED, reason)
        if stop:
            self._stop_once()
        if not already:
            self._notify(MESSAGE_FAILED.format(reason=reason))
            self._c.fire_event(EVENT_CLEAR_FAILED, {"reason": reason})

    def _retire(self, state: str, reason: str | None = None) -> None:
        self._c.print_state.armed = None
        self._set(state, reason)
        self._save()

    def _stop_once(self) -> bool:
        """Send a stop for the paused job, once for each pause."""
        key = self.task or self._c.data.printer.filename or "?"
        if self._stopped == key:
            return False
        self._stopped = key
        _LOGGER.warning("stopping a paused print rather than letting it be resumed into a sweep")
        self._c.hass.async_create_task(self._c.async_send_command("stop"))
        return True

    def _release(self) -> None:
        for name in ("_unsub_door", "_unsub_poll"):
            unsub = getattr(self, name)
            if unsub is not None:
                unsub()
                setattr(self, name, None)

    def _save(self) -> None:
        self._c.async_clearing_changed()

    def _notify(self, message: str) -> None:
        coordinator = self._c
        entry = coordinator.config_entry
        if entry is None:
            return
        device = coordinator.registered_device((DOMAIN, coordinator.hs.serial))
        title = (device and (device.name_by_user or device.name)) or MODEL_NAMES.get(
            coordinator.hs.model_id, "Anycubic printer")
        persistent_notification.async_create(
            coordinator.hass, message, title=title,
            notification_id=f"{DOMAIN}_clearing_{entry.entry_id}")

    # ------------------------------------------------------------------ door and polling

    @callback
    def _door_changed(self, event: Event) -> None:
        new, old = event.data.get("new_state"), event.data.get("old_state")
        is_open = new is not None and new.state == STATE_ON
        if self.state == STATE_READY:
            if is_open and (old is None or old.state != STATE_ON):
                self._door_opened = True
        elif self.state == STATE_PUSHING and not is_open:
            # Closed, unknown or unavailable: all the same during a sweep.
            self._fail(REASON_DOOR, stop=True)
            self._c.async_update_listeners()

    async def _poll(self, _now=None) -> None:
        await self._c.async_query_info()

    # ------------------------------------------------------------------ the press

    async def async_press(self, *, door_open_confirmed: bool = False) -> None:
        """A checked press of Clear the bed. The caller has checked who is pressing.

        The checks run in a fixed order and the first that fails refuses, with what to wait
        for or do, and sends nothing. If all pass, the one resume is sent.
        """
        coordinator = self._c
        printer = coordinator.data.printer
        job = self._job
        if self.state == STATE_EXPIRED:
            raise _refusal("clear_window_over")
        if (self.state != STATE_READY or job is None or job.resumed or not self._ours
                or not coordinator.reachable or not self._info_fresh() or not self._paused
                or finite_number(printer.bed_target) != 0):
            raise _refusal("clear_not_ready")
        if self._window_over():
            self._expire()
            coordinator.async_update_listeners()
            raise _refusal("clear_window_over")
        door = self.door_entity
        if door:
            state = coordinator.hass.states.get(door)
            if state is None or state.state != STATE_ON:
                raise _refusal("clear_door_closed")
            if not self._door_opened:
                raise _refusal("clear_door_not_opened")
        elif door_open_confirmed is not True:
            raise _refusal("clear_no_door_sensor")
        bed = finite_number(printer.bed_temp)
        if bed is None:
            raise _refusal("clear_reading_missing")
        if bed > job.bed_limit:
            raise _refusal("clear_bed_hot", temp=round(bed), limit=job.bed_limit)
        if coordinator.hs.model_id in ENCLOSED_MODELS:
            chamber = finite_number(printer.chamber_temp)
            if chamber is None:
                raise _refusal("clear_reading_missing")
            if chamber > CHAMBER_LIMIT:
                raise _refusal("clear_chamber_hot", temp=round(chamber), limit=CHAMBER_LIMIT)
        # On record before it is sent, so that no restart can lead to a second resume.
        job.resumed = True
        self._finished = self._push_running = False
        self._pressed_at = time.monotonic()
        self._set(STATE_PUSHING)
        await coordinator.async_save_print_state()
        coordinator.async_update_listeners()
        if self.state != STATE_PUSHING:
            # Something failed the press while it was being recorded (the door, a fault,
            # a silence). Nothing is sent, and this job is never resumed.
            raise _refusal("clear_not_ready")
        await coordinator.async_send_command("resume", press=True)
