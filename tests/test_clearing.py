"""Clear the bed: the end of a prepared print, the press and its checks, and the resume lock."""
import json
import time
from unittest.mock import patch

import pytest
from homeassistant.components import persistent_notification as pn
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic import clearing as clearing_mod
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult
from custom_components.anycubic.anycubic_local.models import JobReason
from custom_components.anycubic.const import CONF_DOOR_SENSOR, DOMAIN
from custom_components.anycubic.store import STORAGE_VERSION, ArmedJob

HS = HandshakeResult("192.168.1.50", 9883, "u", "p", "DEVICE-ID-0001", "20029", "SER-1")
HS_S1 = HandshakeResult("192.168.1.52", 9883, "u", "p", "DEVICE-ID-0003", "20025", "SER-S1")
ENTRY_ID = "printerentry0001"
KEY = f"{DOMAIN}.print.{ENTRY_ID}"
DOOR = "binary_sensor.printer_door"
TASK = "555000111"
NAME = "part-ha-pushoff.gcode"
MD5 = "a" * 32
RESUME = "button.anycubic_kobra_s1_max_resume"


class Transport:
    last = None

    def __init__(self, hs, on_report, **kwargs):
        self.published, self.queries = [], []
        type(self).last = self

    def connect(self): pass
    def disconnect(self): pass
    def query(self, msg_type): self.queries.append(msg_type)
    def publish(self, topic, payload): self.published.append(json.loads(payload))

    def actions(self):
        return [body["action"] for body in self.published if body.get("type") == "print"]


async def _setup(hass, hs=HS, door=None):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=hs.serial, data={"host": "printer.example"},
                            options={CONF_DOOR_SENSOR: door} if door else {}, entry_id=ENTRY_ID)
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=hs), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", Transport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry.runtime_data


def _info(coord, *, state="printing", pause=0, task=TASK, name=NAME, bed_target=0, bed=25,
          chamber=25, layer=None, total=None):
    project = {"state": state, "pause": pause, "filename": name, "curr_layer": layer,
               "total_layers": total}
    if task is not None:
        project["task_id"] = int(task)
    coord._apply("info", {"state": "busy", "project": project, "temp": {
        "curr_hotbed_temp": bed, "target_hotbed_temp": bed_target, "curr_chamber_temp": chamber,
        "curr_nozzle_temp": 140, "target_nozzle_temp": 140}})


def _paused(coord, **kwargs):
    _info(coord, state="paused", pause=1, **kwargs)


def _free(coord, *, last_state="finished", task=TASK):
    coord._apply("info", {"state": "free", "project": None,
                          "last_project": {"task_id": int(task), "state": last_state}})


def _arm(coord, bed_limit=28):
    coord.clearing.async_arm(name=NAME, md5=MD5, task_id=TASK, bed_limit=bed_limit)


async def _to_ready(hass, coord, **kwargs):
    _arm(coord)
    _info(coord, bed_target=60)
    _paused(coord, **kwargs)
    await hass.async_block_till_done()
    assert coord.clearing.state == "ready"


async def _press(coord, **kwargs):
    await coord.clearing.async_press(**kwargs)


async def _refused(key, coord, **kwargs):
    before = list(Transport.last.published)
    with pytest.raises(ServiceValidationError) as err:
        await _press(coord, **kwargs)
    assert err.value.translation_key == key
    assert Transport.last.published == before                   # nothing was sent
    return err.value


def _notes(hass):
    return [n["message"] for n in pn._async_get_or_create_notifications(hass).values()]


@pytest.fixture(autouse=True)
def no_window(monkeypatch):
    """Most tests are not about the motor window; those that are set it themselves."""
    monkeypatch.setattr(clearing_mod, "MOTOR_WINDOW", None)


# ------------------------------------------------- pauses in an armed job


async def test_an_armed_job_paused_with_the_bed_heater_off_is_ready_and_nothing_is_sent(hass):
    coord = await _setup(hass)
    events = []
    coord.async_add_event_listener(lambda kind, attrs: events.append((kind, attrs.get("reason"))))
    await _to_ready(hass, coord)
    assert Transport.last.actions() == []                       # no resume, no stop
    assert clearing_mod.MESSAGE_READY in _notes(hass)
    assert ("clear_ready", None) in events
    assert coord.print_state.armed.state == "ready"


async def test_no_layer_data_is_needed_to_be_ready(hass):
    coord = await _setup(hass)
    await _to_ready(hass, coord, layer=None, total=None)


async def test_home_assistant_never_resumes_without_a_press_however_long_it_waits(hass):
    coord = await _setup(hass)
    await _to_ready(hass, coord)
    for _ in range(50):
        _paused(coord, bed=20, chamber=20)
        coord.clearing.async_tick()
    await hass.async_block_till_done()
    assert coord.clearing.state == "ready" and Transport.last.actions() == []


async def test_a_pause_with_the_bed_still_heated_is_left_alone_with_the_lock_on(hass):
    coord = await _setup(hass)
    _arm(coord)
    _info(coord, bed_target=60)
    _paused(coord, bed_target=60)
    await hass.async_block_till_done()
    assert coord.clearing.state == "armed" and coord.clearing.resume_locked
    assert Transport.last.actions() == []
    assert hass.states.get(RESUME).state == "unavailable"


async def test_an_unknown_bed_target_keeps_the_button_away_until_a_fresh_reading(hass):
    coord = await _setup(hass)
    _arm(coord)
    _info(coord, bed_target=60)
    _paused(coord, bed_target=None)
    coord.data.printer.bed_target = None
    assert coord.clearing.state == "armed" and coord.clearing.resume_locked
    assert not coord.clearing.press_possible
    _paused(coord, bed_target=0)
    assert coord.clearing.state == "ready"


async def test_a_job_that_paused_earlier_is_told_once_and_is_never_ready(hass):
    coord = await _setup(hass)
    _arm(coord)
    _info(coord, bed_target=60)
    _paused(coord, bed_target=60)
    _info(coord, bed_target=60)                                  # resumed at the printer
    _info(coord, bed_target=60)
    assert _notes(hass).count(clearing_mod.MESSAGE_PAUSED_EARLIER) == 1
    assert coord.print_state.armed.paused_earlier
    _paused(coord, bed_target=0)                                 # should it ever be seen
    await hass.async_block_till_done()
    assert coord.clearing.state == "armed" and coord.clearing.resume_locked
    assert Transport.last.actions() == []
    await _refused("clear_not_ready", coord, door_open_confirmed=True)


# ------------------------------------------------- jobs that are not Home Assistant's


@pytest.mark.parametrize(("kwargs", "stopped"), [
    ({"name": NAME, "bed_target": 60, "layer": 5, "total": 10}, True),      # marked: any pause
    ({"name": "PART-HA-PUSHOFF.gcode", "bed_target": 60}, True),
    ({"name": "other.gcode", "bed_target": 0}, True),                       # the signature
    ({"name": "other.gcode", "bed_target": 0, "layer": 10, "total": 10}, True),
    ({"name": "other.gcode", "bed_target": 0, "layer": 5, "total": 10}, False),   # mid-print
    ({"name": "other.gcode", "bed_target": 60}, False),
    ({"name": "other.gcode", "bed_target": None}, False),
])
async def test_a_foreign_paused_job_is_stopped_if_marked_or_at_the_signature(hass, kwargs, stopped):
    coord = await _setup(hass)
    _info(coord, task="777", **{**kwargs, "bed_target": 60})
    _paused(coord, task="777", **kwargs)
    if kwargs["bed_target"] is None:
        coord.data.printer.bed_target = None
    _paused(coord, task="777", **kwargs)                         # a second report, one stop
    await hass.async_block_till_done()
    assert Transport.last.actions() == (["stop"] if stopped else [])
    assert (clearing_mod.MESSAGE_FOREIGN in _notes(hass)) is stopped
    assert coord.bed_needs_clearing


async def test_a_prepared_file_started_from_somewhere_else_is_stopped_even_while_one_is_armed(hass):
    coord = await _setup(hass)
    _arm(coord)                                                  # armed, but never seen running
    _info(coord, task="777", bed_target=60)
    _paused(coord, task="777", bed_target=0)
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["stop"]
    assert coord.clearing.state == "armed"


async def test_a_job_that_names_no_task_is_never_taken_for_the_armed_one(hass):
    coord = await _setup(hass)
    _arm(coord)
    _paused(coord, task=None, bed_target=0)
    await hass.async_block_till_done()
    assert coord.clearing.state == "armed"
    assert Transport.last.actions() == ["stop"]                  # marked name: stopped


async def test_other_models_are_left_entirely_alone(hass):
    coord = await _setup(hass, HS_S1)
    _info(coord, task="777", bed_target=60)
    _paused(coord, task="777", name=NAME, bed_target=0)
    await hass.async_block_till_done()
    assert Transport.last.actions() == [] and not coord.clearing.resume_locked
    await coord.async_send_command("resume")
    assert Transport.last.actions() == ["resume"]


# ------------------------------------------------- the press checks, in order


async def test_a_press_that_passes_every_check_sends_one_resume(hass):
    coord = await _setup(hass)
    await _to_ready(hass, coord)
    await _press(coord, door_open_confirmed=True)
    assert Transport.last.actions() == ["resume"]
    assert coord.clearing.state == "pushing"
    assert coord.print_state.armed.resumed
    # The one resume is on record before anything else can happen.
    await _refused("clear_not_ready", coord, door_open_confirmed=True)


@pytest.mark.parametrize("state", ["off", "armed", "pushing", "done", "failed", "stopped"])
async def test_a_press_in_any_state_but_ready_is_refused(hass, state):
    coord = await _setup(hass)
    await _to_ready(hass, coord)
    coord.clearing.state = state
    await _refused("clear_not_ready", coord, door_open_confirmed=True)


async def test_a_press_is_refused_if_the_job_is_no_longer_paused_at_the_signature(hass):
    coord = await _setup(hass)
    await _to_ready(hass, coord)
    coord.data.printer.bed_target = 40
    await _refused("clear_not_ready", coord, door_open_confirmed=True)
    coord.data.printer.bed_target = float("nan")
    await _refused("clear_not_ready", coord, door_open_confirmed=True)
    coord.data.printer.bed_target = 0
    coord.clearing.task = "999"
    await _refused("clear_not_ready", coord, door_open_confirmed=True)


async def test_a_press_is_refused_when_the_printer_has_gone_quiet(hass):
    coord = await _setup(hass)
    await _to_ready(hass, coord)
    coord._report_at = time.monotonic() - 1000
    await _refused("clear_not_ready", coord, door_open_confirmed=True)


async def test_with_no_door_sensor_a_press_needs_the_confirmation(hass):
    coord = await _setup(hass)
    await _to_ready(hass, coord)
    await _refused("clear_no_door_sensor", coord)
    await _refused("clear_no_door_sensor", coord, door_open_confirmed=False)
    await _refused("clear_no_door_sensor", coord, door_open_confirmed="yes")
    assert coord.clearing.state == "ready"


@pytest.mark.parametrize(("bed", "limit", "key"), [
    (34, 28, "clear_bed_hot"), (28.4, 28, "clear_bed_hot"), (26, 25, "clear_bed_hot"),
    (None, 28, "clear_reading_missing"), (float("nan"), 28, "clear_reading_missing"),
    ("warm", 28, "clear_reading_missing"),
])
async def test_a_bed_above_its_limit_or_unread_refuses_with_what_to_wait_for(hass, bed, limit, key):
    coord = await _setup(hass)
    _arm(coord, bed_limit=limit)
    _info(coord, bed_target=60)
    _paused(coord, bed=30)
    coord.data.printer.bed_temp = bed
    err = await _refused(key, coord, door_open_confirmed=True)
    if key == "clear_bed_hot":
        assert err.translation_placeholders == {"temp": str(round(bed)), "limit": str(limit)}
    assert coord.clearing.state == "ready"


async def test_a_bed_at_its_limit_passes(hass):
    coord = await _setup(hass)
    await _to_ready(hass, coord, bed=28)
    await _press(coord, door_open_confirmed=True)
    assert coord.clearing.state == "pushing"


@pytest.mark.parametrize(("chamber", "key"), [
    (31, "clear_chamber_hot"), (None, "clear_reading_missing"),
    (float("inf"), "clear_reading_missing")])
async def test_a_warm_or_unread_chamber_refuses(hass, chamber, key):
    coord = await _setup(hass)
    await _to_ready(hass, coord)
    coord.data.printer.chamber_temp = chamber
    await _refused(key, coord, door_open_confirmed=True)


async def test_the_first_failing_check_is_the_one_reported(hass):
    coord = await _setup(hass, door=DOOR)
    hass.states.async_set(DOOR, "off")
    await _to_ready(hass, coord, bed=60, chamber=45)
    await _refused("clear_door_closed", coord)                   # door before bed
    hass.states.async_set(DOOR, "on")
    await hass.async_block_till_done()
    await _refused("clear_bed_hot", coord)                       # bed before chamber
    coord.data.printer.bed_temp = 20
    await _refused("clear_chamber_hot", coord)


# ------------------------------------------------- the door, with a sensor


async def test_a_door_left_open_since_before_the_pause_is_refused(hass):
    coord = await _setup(hass, door=DOOR)
    hass.states.async_set(DOOR, "on")
    await _to_ready(hass, coord)
    await _refused("clear_door_not_opened", coord)
    await _refused("clear_door_not_opened", coord, door_open_confirmed=True)   # no way round


async def test_a_fresh_open_after_the_pause_passes_even_from_an_event_only_sensor(hass):
    coord = await _setup(hass, door=DOOR)
    hass.states.async_set(DOOR, "off")
    await _to_ready(hass, coord)
    hass.states.async_set(DOOR, "on")
    await hass.async_block_till_done()
    await _press(coord)
    assert Transport.last.actions() == ["resume"]


@pytest.mark.parametrize("reading", ["off", "unavailable", "unknown", None])
async def test_closed_unknown_unavailable_or_missing_reads_as_closed(hass, reading):
    coord = await _setup(hass, door=DOOR)
    hass.states.async_set(DOOR, "off")
    await _to_ready(hass, coord)
    hass.states.async_set(DOOR, "on")
    await hass.async_block_till_done()
    if reading is None:
        hass.states.async_remove(DOOR)
    else:
        hass.states.async_set(DOOR, reading)
    await hass.async_block_till_done()
    await _refused("clear_door_closed", coord, door_open_confirmed=True)


@pytest.mark.parametrize("reading", ["off", "unavailable", "unknown"])
async def test_any_change_away_from_open_during_the_sweep_stops_it(hass, reading):
    coord = await _setup(hass, door=DOOR)
    hass.states.async_set(DOOR, "off")
    await _to_ready(hass, coord)
    hass.states.async_set(DOOR, "on")
    await hass.async_block_till_done()
    await _press(coord)
    hass.states.async_set(DOOR, reading)
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["resume", "stop"]
    assert coord.clearing.state == "failed" and coord.bed_needs_clearing


# ------------------------------------------------- the window, and Stop


async def test_the_window_ending_makes_it_expired_with_one_notice(hass, monkeypatch):
    monkeypatch.setattr(clearing_mod, "MOTOR_WINDOW", 600.0)
    coord = await _setup(hass)
    events = []
    coord.async_add_event_listener(lambda kind, attrs: events.append((kind, attrs.get("reason"))))
    await _to_ready(hass, coord)
    assert coord.clearing.press_possible
    coord.clearing._pause_at -= 600 - clearing_mod.WINDOW_MARGIN + 1
    assert not coord.clearing.press_possible
    _paused(coord)
    _paused(coord)
    assert coord.clearing.state == "expired"
    assert _notes(hass).count(clearing_mod.MESSAGE_EXPIRED) == 1
    assert ("clear_failed", "expired") in events
    await _refused("clear_window_over", coord, door_open_confirmed=True)


async def test_a_pause_nobody_saw_begin_counts_as_expired(hass, monkeypatch):
    monkeypatch.setattr(clearing_mod, "MOTOR_WINDOW", 600.0)
    coord = await _setup(hass)
    _arm(coord)
    _paused(coord)                                               # first seen already paused
    assert coord.clearing.state == "expired"
    await _refused("clear_window_over", coord, door_open_confirmed=True)


async def test_a_report_gap_while_ready_ends_the_window(hass, monkeypatch):
    monkeypatch.setattr(clearing_mod, "MOTOR_WINDOW", 600.0)
    coord = await _setup(hass)
    await _to_ready(hass, coord)
    coord._report_at = time.monotonic() - 1000
    _paused(coord)
    assert coord.clearing.state == "expired"


async def test_with_no_window_a_pause_nobody_saw_begin_is_still_ready(hass):
    coord = await _setup(hass)
    _arm(coord)
    _paused(coord)
    assert coord.clearing.state == "ready"


async def test_stop_at_ready_leaves_the_part_the_flag_and_says_stopped(hass):
    coord = await _setup(hass)
    await _to_ready(hass, coord)
    await coord.async_send_command("stop")                      # never locked
    _free(coord, last_state="stoped")
    assert coord.clearing.state == "stopped"
    assert coord.bed_needs_clearing and coord.print_state.armed is None


# ------------------------------------------------- pushing


async def _to_pushing(hass, coord):
    await _to_ready(hass, coord)
    await _press(coord, door_open_confirmed=True)


async def test_a_sweep_watched_to_its_end_clears_the_flag(hass):
    coord = await _setup(hass)
    events = []
    coord.async_add_event_listener(lambda kind, attrs: events.append(kind))
    await _to_pushing(hass, coord)
    _info(coord)
    _info(coord, state="finished")
    assert coord.bed_needs_clearing                              # not until the printer is free
    _free(coord)
    assert coord.clearing.state == "done" and not coord.bed_needs_clearing
    assert "clear_done" in events and coord.print_state.armed is None
    assert Transport.last.actions() == ["resume"]


async def test_a_pause_during_the_sweep_is_stopped_and_fails(hass):
    coord = await _setup(hass)
    await _to_pushing(hass, coord)
    _info(coord)
    _paused(coord)
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["resume", "stop"]
    assert coord.clearing.state == "failed"
    _free(coord, last_state="stoped")
    assert coord.clearing.state == "failed" and coord.bed_needs_clearing


async def test_a_fault_reported_during_the_sweep_is_stopped_and_fails(hass):
    coord = await _setup(hass)
    await _to_pushing(hass, coord)
    coord._apply_reason(JobReason(code=10110, reason="Head blocked", phase="paused", by_user=False))
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["resume", "stop"]
    assert coord.clearing.state == "failed"
    assert any("did not finish" in note for note in _notes(hass))


async def test_a_job_that_ends_any_way_but_finished_fails(hass):
    coord = await _setup(hass)
    await _to_pushing(hass, coord)
    _free(coord, last_state="failed")
    assert coord.clearing.state == "failed" and coord.bed_needs_clearing


async def test_a_lost_connection_during_the_sweep_fails_even_if_the_job_then_finishes(hass):
    coord = await _setup(hass)
    await _to_pushing(hass, coord)
    coord._report_at = time.monotonic() - 1000
    _free(coord)
    assert coord.clearing.state == "failed" and coord.bed_needs_clearing


async def test_only_one_resume_is_ever_sent_for_a_job(hass):
    coord = await _setup(hass)
    await _to_pushing(hass, coord)
    _paused(coord)                                               # paused again, back at the start
    await hass.async_block_till_done()
    coord.clearing.state = "ready"                               # whatever else went wrong
    await _refused("clear_not_ready", coord, door_open_confirmed=True)


# ------------------------------------------------- outside resumes, restarts, the lock


async def test_a_resume_from_outside_during_ready_is_stopped_at_once(hass):
    coord = await _setup(hass)
    await _to_ready(hass, coord)
    _info(coord)                                                 # running again; not our press
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["stop"]
    assert coord.clearing.state == "failed" and coord.bed_needs_clearing


async def test_resume_is_refused_under_the_lock_from_every_caller_but_the_press(hass):
    coord = await _setup(hass)
    await _to_ready(hass, coord)
    with pytest.raises(ServiceValidationError) as err:
        await coord.async_send_command("resume")
    assert err.value.translation_key == "resume_locked"
    assert hass.states.get(RESUME).state == "unavailable"
    # A press of the unavailable button is dropped by Home Assistant before it gets here.
    await hass.services.async_call("button", "press", {"entity_id": RESUME}, blocking=True)
    assert Transport.last.actions() == []


async def test_an_ordinary_pause_can_still_be_resumed(hass):
    coord = await _setup(hass)
    _info(coord, task="777", name="other.gcode", bed_target=60)
    _paused(coord, task="777", name="other.gcode", bed_target=60)
    await hass.async_block_till_done()
    assert not coord.clearing.resume_locked
    await coord.async_send_command("resume")
    assert Transport.last.actions() == ["resume"]


def _stored(hass_storage, armed):
    hass_storage[KEY] = {"version": STORAGE_VERSION, "minor_version": 1, "key": KEY,
                         "data": {"last_file": None, "armed": armed}}


def _record(**changes):
    return {**ArmedJob(name=NAME, md5=MD5, task_id=TASK, bed_limit=28, seen=True).as_dict(),
            **changes}


async def test_after_a_restart_the_job_is_recognized_only_by_its_task_id(hass, hass_storage):
    _stored(hass_storage, _record(state="ready"))
    coord = await _setup(hass)
    assert coord.clearing.state == "armed"
    _paused(coord)
    assert coord.clearing.state == "ready" and Transport.last.actions() == []


async def test_after_a_restart_a_marked_job_with_another_task_id_is_stopped(hass, hass_storage):
    _stored(hass_storage, _record(state="ready"))
    coord = await _setup(hass)
    _paused(coord, task="777")
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["stop"]
    assert coord.clearing.state != "ready"


async def test_a_restart_during_the_sweep_is_failed_and_never_resumed_again(hass, hass_storage):
    _stored(hass_storage, _record(state="pushing", resumed=True))
    coord = await _setup(hass)
    assert coord.clearing.state == "failed"
    _paused(coord)
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["stop"]
    await _refused("clear_not_ready", coord, door_open_confirmed=True)
    _free(coord)
    assert coord.bed_needs_clearing and coord.print_state.armed is None


@pytest.mark.parametrize("change", [
    {"task_id": 5}, {"task_id": "abc"}, {"task_id": ""}, {"md5": "x"}, {"name": ""},
    {"bed_limit": 0}, {"bed_limit": "nan"}, {"bed_limit": 400}, {"state": "done"},
    {"resumed": "no"}, {"seen": 1},
])
async def test_a_garbled_record_is_dropped(hass, hass_storage, change):
    assert ArmedJob.from_dict(_record(**change)) is None
    _stored(hass_storage, _record(**change))
    coord = await _setup(hass)
    assert coord.print_state.armed is None and coord.clearing.state == "off"


async def test_the_record_is_written_when_armed_and_when_ready(hass, hass_storage):
    coord = await _setup(hass)
    await _to_ready(hass, coord)
    await coord.async_save_print_state()
    assert hass_storage[KEY]["data"]["armed"] == _record(state="ready")
    assert NAME not in json.dumps(coord.clearing.reason)


async def test_a_start_the_printer_turns_down_disarms(hass):
    coord = await _setup(hass)
    _arm(coord)
    coord.clearing.async_disarm()
    assert coord.print_state.armed is None and coord.clearing.state == "off"


def test_the_strictest_bed_limit_wins():
    assert clearing_mod.bed_limit_for(("PLA",)) == 28
    assert clearing_mod.bed_limit_for(("PETG",)) == 25
    assert clearing_mod.bed_limit_for(("PETG", "PLA")) == 25


# ------------------------------------------------- just after the press


async def test_a_report_still_saying_paused_just_after_the_press_does_not_stop_the_sweep(hass):
    coord = await _setup(hass)
    await _to_pushing(hass, coord)
    _paused(coord)                                               # sent before the resume landed
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["resume"] and coord.clearing.state == "pushing"
    _info(coord)                                                 # now it is running
    _paused(coord)                                               # and this is a real pause
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["resume", "stop"] and coord.clearing.state == "failed"


async def test_a_resume_that_never_takes_is_stopped_after_a_short_wait(hass):
    coord = await _setup(hass)
    await _to_pushing(hass, coord)
    coord.clearing._pressed_at -= clearing_mod.RESUME_GRACE + 1
    _paused(coord)
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["resume", "stop"] and coord.clearing.state == "failed"


async def test_an_ordinary_start_drops_a_record_left_from_a_start_that_never_began(hass):
    coord = await _setup(hass)
    _arm(coord)
    assert coord.print_state.armed is not None
    Transport_reply = {"action": "start", "code": 200, "state": "printing", "msg": ""}
    with patch.object(coord, "_async_send_start", return_value=Transport_reply):
        await coord.async_start_print(name="other.gcode", size=1, md5=MD5, filetype=1,
                                      ams_settings={})
    assert coord.print_state.armed is None and coord.clearing.state == "off"


# ------------------------------------------------- findings of the independent review


async def test_a_press_that_fails_while_it_is_being_recorded_sends_no_resume(hass):
    coord = await _setup(hass)
    await _to_ready(hass, coord)

    async def save_and_lose_the_printer():
        coord.clearing.async_on_gap()                           # as a report gap would

    with patch.object(coord, "async_save_print_state", save_and_lose_the_printer):
        with pytest.raises(ServiceValidationError):
            await _press(coord, door_open_confirmed=True)
    await hass.async_block_till_done()
    assert "resume" not in Transport.last.actions()
    assert coord.clearing.state == "failed" and coord.print_state.armed.resumed


async def test_a_job_that_ends_without_ever_running_after_the_press_is_not_a_sweep(hass):
    coord = await _setup(hass)
    await _to_pushing(hass, coord)
    _free(coord)                                                # finished, but never seen running
    assert coord.clearing.state == "failed" and coord.bed_needs_clearing


async def test_the_armed_job_must_also_carry_the_prepared_copys_name(hass):
    coord = await _setup(hass)
    _arm(coord)
    _info(coord, name="other.gcode", bed_target=60)
    _paused(coord, name="other.gcode")
    await hass.async_block_till_done()
    assert coord.clearing.state == "armed"
    assert Transport.last.actions() == ["stop"]                 # the signature, not ours


async def test_a_press_needs_a_recent_status_report_not_just_any_report(hass):
    coord = await _setup(hass)
    await _to_ready(hass, coord)
    coord.clearing._info_at -= 1000
    coord._apply("tempature", {"curr_hotbed_temp": 20})         # the printer is still talking
    await _refused("clear_not_ready", coord, door_open_confirmed=True)
