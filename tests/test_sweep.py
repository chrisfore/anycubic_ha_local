"""The sweep planner: the block of G-code that parks, pauses, cools and pushes (spec 7.2, 7.3)."""
import dataclasses
import re

import pytest

from custom_components.anycubic.anycubic_local import sweep
from custom_components.anycubic.anycubic_local.printfile import FileRefused

P = sweep.S1_MAX
SQUARE = (((170.0, 170.0), (180.0, 170.0), (180.0, 180.0), (170.0, 180.0)),)
_WORD = re.compile(r"([XYZFS])(-?\d+(?:\.\d+)?)")


def _block(max_z=20.0, footprints=SQUARE, **kwargs):
    return sweep.clearing_block(P, max_z, footprints, **kwargs)


def _refused(key, *args, **kwargs):
    with pytest.raises(FileRefused) as err:
        _block(*args, **kwargs)
    assert err.value.key == key


def _commands(lines):
    return [line.split(";")[0].strip() for line in lines if line.split(";")[0].strip()]


def _moves(lines):
    """Every G1 as (x, y, z) after it, tracking the position from the park onward."""
    x = y = z = None
    out = []
    for command in _commands(lines):
        if not command.startswith("G1 "):
            continue
        words = dict(_WORD.findall(command))
        x = float(words["X"]) if "X" in words else x
        y = float(words["Y"]) if "Y" in words else y
        z = float(words["Z"]) if "Z" in words else z
        out.append((x, y, z))
    return out


# ------------------------------------------------- the order of the block


def test_the_block_runs_heaters_off_lift_park_pause_cool_sweep_raise():
    commands = _commands(_block())
    pause = commands.index("M601")
    assert commands[:2] == ["M140 S0", "M141 S0"]
    assert commands[2] == "G90"
    assert commands[3] == "G1 Z30.00 F600"                      # tallest part + 10
    assert commands[4] == "G1 X105.00 Y352.00 F12000"           # over the chute
    # The chamber fan and the auxiliary fan, set here because the printer takes no fan
    # command from outside while it is paused, and keeps what the file set.
    assert commands[5:7] == ["M106 P3 S204", "M106 P2 S204"]
    assert pause == 7
    assert commands[-3:-1] == ["M106 P2 S0", "M106 P3 S0"]      # off again after the sweep
    after = commands[pause + 1:]
    cool = after.index("M109 S50")
    first_descent = next(i for i, c in enumerate(after) if c == "G1 Z2.00 F600")
    assert cool < first_descent
    assert after[cool + 1] == "M104 S0"
    assert after[-1] == "G1 Z40.00 F600"                        # the final raise


def test_nothing_moves_between_the_pause_and_the_end_of_the_nozzle_cooling():
    commands = _commands(_block())
    between = commands[commands.index("M601") + 1:commands.index("M104 S0")]
    assert not [c for c in between if c.startswith(("G0", "G1", "G2", "G3", "G28"))]


def test_the_part_fan_runs_for_the_cooling_and_is_off_before_the_sweep():
    commands = _commands(_block())
    assert commands.index("M106 S255") < commands.index("M109 S50")
    assert commands.index("M109 S50") < commands.index("M106 S0")


def test_a_timed_dwell_can_stand_in_for_the_cooling_wait():
    commands = _commands(_block(cooling=sweep.Dwell(420)))
    assert "M109 S50" not in commands
    assert commands[commands.index("M601") + 1:][:4] == ["M106 S255", "G4 P420000", "M106 S0", "G90"]


@pytest.mark.parametrize("seconds", [0, -5, 3601, float("nan"), float("inf"), True, "60"])
def test_a_dwell_that_is_not_a_sensible_time_is_refused(seconds):
    _refused("clear_out_of_reach", cooling=sweep.Dwell(seconds))


def test_there_is_no_m117_no_homing_no_extrusion_and_no_relative_move():
    for footprints in (SQUARE, SQUARE + (((40.0, 40.0), (90.0, 40.0), (90.0, 120.0)),)):
        for line in _block(footprints=footprints):
            command = line.split(";")[0].upper()
            assert not re.search(r"\bM117\b|\bG28\b|\bG29\b|\bG91\b|\bG92\b|\bM84\b|\bM18\b",
                                 command), line
            assert not re.search(r"\bE-?[\d.]", command), line


# ------------------------------------------------- the sweep itself


def test_each_column_descends_behind_the_parts_pushes_to_the_front_and_lifts():
    moves = _moves(_block())
    sweep_moves = moves[2:-1]                                   # after lift and park
    assert len(sweep_moves) % 4 == 0 and sweep_moves
    for travel, down, push, up in zip(*[iter(sweep_moves)] * 4, strict=True):
        assert travel[1] == P.descent_y and travel[2] == 30.0    # behind, at lift height
        assert down == (travel[0], P.descent_y, P.sweep_height)
        assert push == (travel[0], P.front_y, P.sweep_height)    # straight toward the door
        assert up == (travel[0], P.front_y, 30.0)


def test_the_push_is_slow_and_z_moves_stay_inside_the_firmware_speed():
    for line in _block():
        command = line.split(";")[0]
        words = dict(_WORD.findall(command))
        if command.startswith("G1") and "Z" in words:
            assert float(words["F"]) <= 600
        if command.startswith("G1") and words.get("Y") == f"{P.front_y:.2f}":
            assert 1500 <= float(words["F"]) <= 2400


def test_the_columns_cover_the_parts_with_a_margin_and_no_gap_wider_than_the_spacing():
    wide = (((40.0, 100.0), (300.0, 100.0), (300.0, 200.0), (40.0, 200.0)),)
    columns = sorted({m[0] for m in _moves(_block(footprints=wide))[2:-1]})
    assert columns[0] <= 40.0 - P.side_margin + 1e-6
    assert columns[-1] >= 300.0 + P.side_margin - 1e-6
    assert max(b - a for a, b in zip(columns, columns[1:], strict=False)) <= P.column_spacing + 1e-6


def test_columns_stop_at_the_bed_edge():
    edge = (((0.0, 100.0), (20.0, 100.0), (20.0, 120.0), (0.0, 120.0)),)
    columns = {m[0] for m in _moves(_block(footprints=edge))[2:-1]}
    assert min(columns) == P.bed_x[0]


def test_every_coordinate_is_inside_the_travel_limits_and_under_the_ceiling():
    cases = [(200.0, SQUARE), (4.0, SQUARE),
             (60.0, (((0.0, 0.0), (350.0, 0.0), (350.0, 300.0), (0.0, 300.0)),))]
    for max_z, footprints in cases:
        for x, y, z in _moves(_block(max_z, footprints)):
            assert x is None or P.x_min <= x <= P.x_max
            assert y is None or P.y_min <= y <= P.y_max
            assert 0 < z <= P.z_cap


def test_the_parked_height_leaves_room_for_the_pause_to_lift_under_the_ceiling():
    # 200 mm is the tallest part allowed: 210 parked, 212 after PAUSE's own lift.
    commands = _commands(_block(200.0))
    assert "G1 Z210.00 F600" in commands
    assert commands[-1] == "G1 Z220.00 F600"
    low_ceiling = dataclasses.replace(P, z_cap=211.0)
    with pytest.raises(FileRefused) as err:
        sweep.clearing_block(low_ceiling, 200.0, SQUARE)
    assert err.value.key == "clear_out_of_reach"


def test_the_final_raise_never_passes_the_ceiling():
    low_ceiling = dataclasses.replace(P, z_cap=215.0)
    assert _commands(sweep.clearing_block(low_ceiling, 200.0, SQUARE))[-1] == "G1 Z215.00 F600"


def test_an_air_sweep_height_can_be_given_for_the_first_live_test():
    moves = _moves(_block(sweep_height=20.0, max_z=45.0))
    assert {m[2] for m in moves[2:-1]} == {20.0, 55.0}


# ------------------------------------------------- refusals


@pytest.mark.parametrize("max_z", [3.9, 0.6, 0.0, -1.0, float("nan"), float("inf")])
def test_a_part_shorter_than_twice_the_sweep_height_is_refused(max_z):
    _refused("clear_part_too_short", max_z)


def test_a_part_exactly_twice_the_sweep_height_passes_and_200_mm_is_the_tallest():
    assert _block(2 * P.sweep_height)
    assert _block(200.0)
    _refused("clear_part_too_tall", 200.1)


@pytest.mark.parametrize("footprint", [
    ((-0.5, 10.0), (10.0, 10.0), (10.0, 20.0)),
    ((340.0, 10.0), (350.5, 10.0), (345.0, 20.0)),
    ((10.0, -0.1), (20.0, 10.0), (10.0, 20.0)),
    ((10.0, 10.0), (20.0, 10.0), (15.0, float("nan"))),
    ((10.0, 10.0), (20.0, float("inf")), (15.0, 20.0)),
])
def test_an_outline_off_the_bed_or_unreadable_is_refused(footprint):
    _refused("clear_part_off_bed", footprints=(footprint,))


def test_a_part_too_close_to_the_back_for_the_head_to_come_down_behind_is_refused():
    limit = P.descent_y - P.rear_clearance
    ok = (((10.0, limit - 20), (20.0, limit - 20), (15.0, limit)),)
    assert _block(footprints=ok)
    _refused("clear_part_too_far_back",
             footprints=(((10.0, limit - 20), (20.0, limit - 20), (15.0, limit + 0.1)),))


def test_no_outline_at_all_is_refused():
    _refused("clear_part_off_bed", footprints=())
    _refused("clear_part_off_bed", footprints=((),))


def test_the_profile_is_only_for_the_s1_max():
    assert sweep.profile_for("20029") is P
    assert sweep.profile_for("20025") is None
    assert sweep.profile_for("20030") is None


def test_the_s1_max_profile_stays_inside_what_was_measured():
    assert (P.x_min, P.x_max, P.y_min, P.y_max) == (-3.0, 358.0, -2.0, 380.0)
    assert P.z_cap == 253.0 and P.pause_lift == 2.0
    assert P.front_y >= P.y_min and P.descent_y <= P.y_max
    assert P.descent_y > 352.45                                 # behind the purge line
    assert 1.0 <= P.sweep_height <= 5.0
    assert 1500 <= P.push_speed <= 2400
    assert P.nozzle_cool <= 50


def test_the_writer_itself_refuses_any_line_outside_travel_or_over_the_ceiling():
    # Belt and braces: whatever the planning above lets through, no such line is written.
    for bad in (dataclasses.replace(P, descent_y=P.y_max + 1),
                dataclasses.replace(P, front_y=P.y_min - 1),
                dataclasses.replace(P, park=(P.x_max + 1, 352.0)),
                dataclasses.replace(P, bed_x=(-10.0, 350.0), side_margin=0.0),
                dataclasses.replace(P, z_cap=25.0, pause_lift=-100.0)):
        footprints = SQUARE if bad.bed_x == P.bed_x else (((-10.0, 10.0), (5.0, 10.0), (5.0, 20.0)),)
        with pytest.raises(FileRefused) as err:
            sweep.clearing_block(bad, 20.0, footprints)
        assert err.value.key == "clear_out_of_reach"


def test_cooling_can_be_a_wait_followed_by_a_dwell():
    commands = _commands(_block(cooling=(sweep.CoolTo(50), sweep.Dwell(300))))
    after = commands[commands.index("M601") + 1:]
    assert after[:6] == ["M106 S255", "M109 S50", "M104 S0", "G4 P300000", "M106 S0", "G90"]
    _refused("clear_out_of_reach", cooling=())
    _refused("clear_out_of_reach", cooling=(sweep.CoolTo(50), "G28"))
    _refused("clear_out_of_reach", cooling=sweep.CoolTo(60))
