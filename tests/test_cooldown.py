"""Cool the bed when done: a prepared print ends parked with the fans on, Home Assistant ends
the job once the bed has cooled and says so, and the part comes off by hand. No push."""
import hashlib
import time
from pathlib import Path

import pytest
from homeassistant.exceptions import ServiceValidationError
from test_clear_actions import _part, _print_file, _sent  # noqa: F401
from test_clearing import (
    MD5,
    NAME,
    TASK,
    Transport,
    _arm,
    _free,
    _info,
    _notes,
    _paused,
    _setup,
)
from test_print_actions import (  # noqa: F401  (env and quick_start are fixtures)
    Transport as ActionTransport,
    _boxes,
    _nothing_sent,
    _ready,
    _stage,
    env,
    quick_start,
)

from custom_components.anycubic import clearing as clearing_mod, const
from custom_components.anycubic.anycubic_local import printfile as pf, sweep
from custom_components.anycubic.anycubic_local.printfile import FileRefused

# ruff: noqa: F811  (the fixtures imported above are named again as test arguments)
CLEAR = "button.anycubic_kobra_s1_max_clear_the_bed"
STATE = "sensor.anycubic_kobra_s1_max_bed_cool_down"
RESUME = "button.anycubic_kobra_s1_max_resume"


def test_the_push_is_switched_off_and_the_door_option_with_it():
    assert const.PUSH_ENABLED is False and const.DOOR_SENSOR_OPTION_ENABLED is False


# ------------------------------------------------- the block, with no sweep in it


def test_the_block_parks_turns_the_fans_on_and_pauses_and_nothing_pushes():
    lines = sweep.clearing_block(sweep.S1_MAX, 6.0, (), push=False)
    commands = [line for line in lines if not line.startswith(";")]
    assert commands == ["M140 S0", "M141 S0", "G90", "G1 Z16.00 F600",
                        "G1 X105.00 Y352.00 F12000", "M106 P3 S204", "M106 P2 S204", "M601",
                        "M106 P2 S0", "M106 P3 S0", "G1 Z26.00 F600"]


def test_the_block_needs_only_a_height_it_can_lift_above():
    assert sweep.clearing_block(sweep.S1_MAX, 0.6, (), push=False)       # any part, however low
    for bad in (0, -1, float("nan"), 300):
        with pytest.raises(FileRefused):
            sweep.clearing_block(sweep.S1_MAX, bad, (), push=False)


def test_a_file_with_a_brim_or_no_outline_can_still_be_cooled(tmp_path):
    body = _part(extra=";TYPE:Brim\nG1 X171 Y171 E1\n").replace(
        "EXCLUDE_OBJECT_DEFINE NAME=a CENTER=175,175 "
        "POLYGON=[[170,170],[180,170],[180,180],[170,180]]\n", "")
    path = tmp_path / "a.gcode"
    path.write_text(body)
    with pytest.raises(FileRefused):
        pf.scan_for_clearing(path, pf.KIND_GCODE)
    scan = pf.scan_for_clearing(path, pf.KIND_GCODE, push=False)
    assert scan.max_z == 12.4 and scan.footprints == ()
    for broken, key in ((body.replace("M84\n", ""), "clear_end_code"),
                        (body + "M601\n", "clear_own_pause"),
                        (body.replace("; max_z_height: 12.40\n", ""), "clear_no_height"),
                        (body.replace("G1 Z.2\n", "G1 Z40\n"), "clear_no_height")):
        path.write_text(broken)
        with pytest.raises(FileRefused) as err:
            pf.scan_for_clearing(path, pf.KIND_GCODE, push=False)
        assert err.value.key == key


# ------------------------------------------------- sending a file


async def test_ticked_sends_the_copy_with_no_sweep_and_arms_with_the_materials_limit(env):
    copies = []

    async def send(session, target, path, name, size, **kwargs):
        copies.append((name, Path(path).read_bytes()))

    env.send.side_effect = send
    await _print_file(env, _stage(env, "part.gcode", _part()), True)
    (first, _), (second, copy) = copies
    assert (first, second) == ("part.gcode", "part-ha-pushoff.gcode")
    assert b"M601" in copy and b"M106 P3 S204" in copy
    assert b"M109" not in copy and b"Y-2.00" not in copy               # no cooling wait, no push
    armed = env.coord.print_state.armed
    assert armed.bed_limit == 28 and armed.md5 == hashlib.md5(copy).hexdigest()
    assert env.hass.states.get(CLEAR) is None                           # no button to press
    assert not env.hass.services.has_service("anycubic", "clear_bed")


async def test_any_material_and_a_brim_are_fine_and_get_their_own_limit(env):
    _ready(env.coord, _boxes(("ABS", [1, 2, 3]), ("PETG", [0, 0, 0])))
    await _print_file(env, _stage(env, "a.gcode", _part(
        material="ABS", extra=";TYPE:Brim\nG1 X171 Y171 E1\n")), True)
    assert env.coord.print_state.armed.bed_limit == 30
    env.coord.clearing.async_disarm()
    _ready(env.coord, _boxes(("ABS", [1, 2, 3]), ("PETG", [0, 0, 0])))
    ActionTransport.replies = [200]
    await _print_file(env, _stage(env, "b.gcode", _part(material="PETG", slot=2,
                                                        plate="Smooth PEI Plate")), True)
    assert env.coord.print_state.armed.bed_limit == 25


# ------------------------------------------------- the end of the print


@pytest.fixture
def no_push_window(monkeypatch):
    monkeypatch.setattr(clearing_mod, "MOTOR_WINDOW", 5400.0)


async def _to_cooling(hass, coord, bed=60):
    _arm(coord)
    _info(coord, bed_target=60)
    _paused(coord, bed=bed)
    await hass.async_block_till_done()
    assert coord.clearing.state == "ready"


async def test_at_the_pause_it_waits_and_says_the_bed_is_cooling(hass, no_push_window):
    coord = await _setup(hass)
    events = []
    coord.async_add_event_listener(lambda kind, attrs: events.append(kind))
    await _to_cooling(hass, coord)
    assert Transport.last.actions() == []
    assert clearing_mod.MESSAGE_COOLING in _notes(hass) and "clear_ready" in events
    assert not coord.clearing.resume_locked
    assert hass.states.get(RESUME).state != "unavailable"               # harmless: no sweep to run
    assert hass.states.get(STATE).state == "ready"


async def test_once_the_bed_is_cool_the_job_is_ended_and_the_owner_told(hass, no_push_window):
    coord = await _setup(hass)
    events = []
    coord.async_add_event_listener(lambda kind, attrs: events.append(kind))
    await _to_cooling(hass, coord)
    _paused(coord, bed=29)
    await hass.async_block_till_done()
    assert Transport.last.actions() == []                               # one degree too warm
    _paused(coord, bed=28)
    _paused(coord, bed=27)
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["stop"]                         # once, and never a resume
    _free(coord, last_state="stoped")
    assert coord.clearing.state == "done" and coord.print_state.armed is None
    assert coord.bed_needs_clearing                                     # the part is still there
    assert "clear_done" in events
    assert any("Take the print off" in note for note in _notes(hass))


async def test_a_temperature_report_alone_can_bring_the_end(hass, no_push_window):
    coord = await _setup(hass)
    await _to_cooling(hass, coord)
    coord._apply("tempature", {"curr_hotbed_temp": 24})
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["stop"]


@pytest.mark.parametrize("bed", [None, float("nan"), "cold"])
async def test_a_bed_reading_that_is_not_a_number_ends_nothing(hass, no_push_window, bed):
    coord = await _setup(hass)
    await _to_cooling(hass, coord)
    coord.data.printer.bed_temp = bed
    coord.clearing.async_tick()
    await hass.async_block_till_done()
    assert Transport.last.actions() == []


async def test_a_bed_that_does_not_cool_in_time_is_ended_anyway_and_said_so(hass, no_push_window):
    coord = await _setup(hass)
    await _to_cooling(hass, coord)
    coord.clearing._pause_at -= 5400
    _paused(coord, bed=40)
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["stop"]
    _free(coord, last_state="stoped")
    assert coord.clearing.state == "expired" and coord.bed_needs_clearing
    assert any("still warm" in note for note in _notes(hass))


async def test_a_resume_from_anywhere_just_finishes_the_print(hass, no_push_window):
    coord = await _setup(hass)
    await _to_cooling(hass, coord)
    await coord.async_send_command("resume")                            # not locked
    _info(coord)
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["resume"]                       # and no stop
    _free(coord)
    assert coord.clearing.state == "stopped" and coord.bed_needs_clearing


async def test_stop_by_hand_before_it_is_cool_says_stopped(hass, no_push_window):
    coord = await _setup(hass)
    await _to_cooling(hass, coord)
    _free(coord, last_state="stoped")
    assert coord.clearing.state == "stopped" and coord.bed_needs_clearing


async def test_a_pause_first_seen_after_a_restart_still_cools_and_ends(hass, no_push_window):
    coord = await _setup(hass)
    _arm(coord)
    _paused(coord, bed=50)                                              # never seen running
    await hass.async_block_till_done()
    assert coord.clearing.state == "ready" and Transport.last.actions() == []
    _paused(coord, bed=20)
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["stop"]


async def test_other_peoples_paused_jobs_are_left_alone(hass, no_push_window):
    coord = await _setup(hass)
    _info(coord, task="777", name="other.gcode", bed_target=60)
    _paused(coord, task="777", name=NAME, bed_target=0)                 # even a marked name
    await hass.async_block_till_done()
    assert Transport.last.actions() == [] and not coord.clearing.resume_locked


async def test_there_is_no_press(hass, no_push_window):
    coord = await _setup(hass)
    await _to_cooling(hass, coord, bed=20)
    with pytest.raises(ServiceValidationError):
        await coord.clearing.async_press(door_open_confirmed=True)
    assert "resume" not in Transport.last.actions()


def test_the_limit_is_the_coolest_any_material_in_the_print_asks_for():
    assert clearing_mod.cool_limit_for(("PLA",)) == 28
    assert clearing_mod.cool_limit_for(("PETG", "PLA")) == 25
    assert clearing_mod.cool_limit_for(("ABS",)) == 30
    assert clearing_mod.cool_limit_for(()) == 30
    assert clearing_mod.cool_limit_for((None, "PLA")) == 28
    assert (MD5, TASK, time) is not None


# ------------------------------------------------- findings of the independent review


@pytest.mark.parametrize("label", ["", ";TYPE:Custom\n", ";TYPE:Outer wall\n"])
def test_printing_above_the_stated_height_is_refused_whatever_it_is_labeled(tmp_path, label):
    # The head parks by travelling at the stated height plus 10 mm: a taller print is in its way.
    body = _part(extra=f"{label}G1 Z50\nG1 X175 Y175 E1\n")
    if not label:
        body = "\n".join(line for line in body.splitlines() if not line.startswith(";TYPE")) + "\n"
    path = tmp_path / "a.gcode"
    path.write_text(body)
    with pytest.raises(FileRefused) as err:
        pf.scan_for_clearing(path, pf.KIND_GCODE, push=False)
    assert err.value.key == "clear_no_height"


def test_printing_at_an_unknown_height_is_refused_whatever_it_is_labeled(tmp_path):
    path = tmp_path / "a.gcode"
    path.write_text(_part().replace("G1 Z.2\n", "").replace(";TYPE:Outer wall\n", ";TYPE:Custom\n"))
    with pytest.raises(FileRefused) as err:
        pf.scan_for_clearing(path, pf.KIND_GCODE, push=False)
    assert err.value.key == "clear_unreadable"


def test_a_purge_line_at_the_back_is_still_fine(tmp_path):
    path = tmp_path / "a.gcode"
    path.write_text(_part().replace("G1 Z.2\n", "G1 Z.25\nG1 X135 Y352\nG1 X205 Y352 E3.7\nG1 Z.2\n"))
    assert pf.scan_for_clearing(path, pf.KIND_GCODE, push=False).max_z == 12.4


async def test_a_stop_that_does_not_take_is_sent_again_and_then_the_owner_is_told(
        hass, no_push_window):
    coord = await _setup(hass)
    await _to_cooling(hass, coord)
    _paused(coord, bed=20)
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["stop"]
    _paused(coord, bed=20)                                              # too soon to judge
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["stop"]
    for sent in (2, 3):
        coord.clearing._stop_at -= clearing_mod.STOP_RETRY_AFTER + 1
        _paused(coord, bed=20)
        await hass.async_block_till_done()
        assert Transport.last.actions() == ["stop"] * sent
    coord.clearing._stop_at -= clearing_mod.STOP_RETRY_AFTER + 1
    _paused(coord, bed=20)
    _paused(coord, bed=20)
    await hass.async_block_till_done()
    assert Transport.last.actions() == ["stop"] * 3                     # and no more
    assert _notes(hass).count(clearing_mod.MESSAGE_STOP_FAILED) == 1
    _free(coord, last_state="stoped")                                   # the owner pressed Stop
    assert coord.clearing.state == "done"


async def test_a_resume_at_the_printer_while_cooling_is_not_called_an_earlier_pause(
        hass, no_push_window):
    coord = await _setup(hass)
    await _to_cooling(hass, coord)
    _info(coord)
    await hass.async_block_till_done()
    assert not coord.print_state.armed.paused_earlier
    assert not any("paused earlier" in note for note in _notes(hass))


async def test_a_print_that_paused_earlier_is_told_what_to_do_without_the_push_words(
        hass, no_push_window):
    coord = await _setup(hass)
    _arm(coord)
    _info(coord, bed_target=60)
    _paused(coord, bed_target=60)
    _info(coord, bed_target=60)
    notes = [note for note in _notes(hass) if "paused earlier" in note]
    assert len(notes) == 1 and "Clear the bed" not in notes[0] and "Press Stop" in notes[0]
