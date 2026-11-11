"""Skip the purge line: the line of filament the slicer lays at the back of the bed before a
print is taken out of the copy that is sent. The head's moves stay; only the filament goes."""
# ruff: noqa: F811  (the fixtures imported below are named again as test arguments)
import io
import zipfile
from pathlib import Path

import pytest
from homeassistant.exceptions import ServiceValidationError
from test_clear_actions import _part
from test_print_actions import (  # noqa: F401  (env and quick_start are fixtures)
    Transport,
    _nothing_sent,
    _stage,
    env,
    quick_start,
)

from custom_components.anycubic.anycubic_local import printfile as pf
from custom_components.anycubic.anycubic_local.printfile import FileRefused
from custom_components.anycubic.const import DOMAIN

# As the Anycubic slicer writes it.
PURGE = """;LAYER_CHANGE
;Z:0.2
;HEIGHT:0.2
 ; PURGE LINE
M204 P500
SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=9
G1 Z0.75 F900 ; for object exclusion
G1 X135 Y352 F18000
G1 Z.25 F900
G1 E0.8 F2400
G1 E0.6
G1 F3000
G1 X205 Y352 E3.72454
G1 X205 Y352.45 E.0252
M73 P1 R6
G1 X135 Y352.45 E3.72454
G1 X135 Y352.02 E.02305
G1 Z0.5 F600

G1 E-.4 F1800
; AFTER_LAYER_CHANGE 0 @ 0.2mm
"""
SKIPPED = """;LAYER_CHANGE
;Z:0.2
;HEIGHT:0.2
 ; PURGE LINE
M204 P500
SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=9
G1 Z0.75 F900 ; for object exclusion
G1 X135 Y352 F18000
G1 Z.25 F900
; purge line skipped
; purge line skipped
G1 F3000
G1 X205 Y352
G1 X205 Y352.45
M73 P1 R6
G1 X135 Y352.45
G1 X135 Y352.02
G1 Z0.5 F600

G1 E-.4 F1800
; AFTER_LAYER_CHANGE 0 @ 0.2mm
"""
HEAD = "; HEADER_BLOCK_START\n; max_z_height: 12.40\n; HEADER_BLOCK_END\nG9111 bedTemp=60 extruderTemp=220\nM83\n"
REST = ";TYPE:Outer wall\nG1 Z.2\nG1 X170 Y170 E.4\nM140 S0\nM104 S0\nM84\n"


def _file(tmp_path, purge=PURGE, name="a.gcode", newline="\n"):
    path = tmp_path / name
    path.write_bytes((HEAD + purge + REST).replace("\n", newline).encode())
    return path


def _skipped(path, kind=pf.KIND_GCODE):
    dst = path.parent / "out.gcode"
    pf.write_prepared_copy(path, dst, kind, None, (), edits=pf.scan_purge_line(path, kind))
    return dst.read_bytes().decode()


def _refused(path, key="purge_not_found"):
    with pytest.raises(FileRefused) as err:
        pf.scan_purge_line(path, pf.KIND_GCODE)
    assert err.value.key == key


def test_the_filament_goes_and_every_move_stays(tmp_path):
    assert _skipped(_file(tmp_path)) == HEAD + SKIPPED + REST


def test_windows_line_ends_are_kept(tmp_path):
    out = _skipped(_file(tmp_path, newline="\r\n"))
    assert out == (HEAD + SKIPPED + REST).replace("\n", "\r\n")


def test_the_final_retract_stays_so_the_first_move_of_the_print_finds_what_it_expects(tmp_path):
    assert "G1 E-.4 F1800\n; AFTER_LAYER_CHANGE" in _skipped(_file(tmp_path))


def test_an_archive_gives_its_plate_with_the_purge_line_skipped(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Metadata/plate_1.gcode", HEAD + PURGE + REST)
    path = tmp_path / "a.gcode.3mf"
    path.write_bytes(buffer.getvalue())
    assert _skipped(path, pf.KIND_3MF) == HEAD + SKIPPED + REST


@pytest.mark.parametrize("purge", [
    "",                                                           # no purge line at all
    PURGE.replace(" ; PURGE LINE\n", ""),                         # not marked as one
    PURGE.replace("; AFTER_LAYER_CHANGE 0 @ 0.2mm\n", ""),        # runs on into the print
    PURGE.replace("G1 X205 Y352 E3.72454", "G1 X205 Y152 E3.72454"),   # not at the back
    PURGE.replace("M73 P1 R6", "G28"),                            # something else in it
    PURGE.replace("M73 P1 R6", "M104 S250"),
    PURGE.replace("G1 X205 Y352 E3.72454\n", "").replace("G1 X205 Y352.45 E.0252\n", "")
         .replace("G1 X135 Y352.45 E3.72454\n", "").replace("G1 X135 Y352.02 E.02305\n", ""),
    PURGE.replace("G1 E0.6", "G1 E0.6\n" * 60),                   # far too long to be one
])
def test_a_purge_line_that_is_not_the_one_expected_is_not_touched(tmp_path, purge):
    _refused(_file(tmp_path, purge))


def test_a_copy_is_refused_if_the_file_changed_after_it_was_read(tmp_path):
    path = _file(tmp_path)
    edits = pf.scan_purge_line(path, pf.KIND_GCODE)
    path.write_bytes(path.read_bytes().replace(b"E3.72454", b"E9.99999"))
    with pytest.raises(FileRefused):
        pf.write_prepared_copy(path, tmp_path / "out.gcode", pf.KIND_GCODE, None, (), edits=edits)
    assert not (tmp_path / "out.gcode").exists()


def test_skipping_and_the_cool_down_block_go_into_one_copy(tmp_path):
    path = _file(tmp_path)
    scan = pf.scan_for_clearing(path, pf.KIND_GCODE, push=False)
    dst = tmp_path / "out.gcode"
    pf.write_prepared_copy(path, dst, pf.KIND_GCODE, scan, ["M140 S0", "M601"],
                           edits=pf.scan_purge_line(path, pf.KIND_GCODE))
    assert dst.read_text() == HEAD + SKIPPED + REST.replace(
        "M104 S0\n", "M104 S0\nM140 S0\nM601\n")


# ------------------------------------------------- through the action


PRINT = _part().replace("M140 S60\n", "M83\nM140 S60\n" + PURGE)


async def _print(env, file_id, **options):
    await env.hass.services.async_call(
        DOMAIN, "print_file", {"printer": env.device, "file": file_id, **options}, blocking=True)


async def test_skipping_alone_sends_a_copy_starts_it_and_waits_for_nothing(env):
    sent = []

    async def send(session, target, path, name, size, **kwargs):
        sent.append((name, Path(path).read_text()))

    env.send.side_effect = send
    await _print(env, _stage(env, "part.gcode", PRINT), skip_purge_line=True)
    (first, original), (second, copy) = sent
    assert (first, second) == ("part.gcode", "part-ha-pushoff.gcode")
    assert original == PRINT and copy == PRINT.replace(PURGE, SKIPPED)
    assert "M601" not in copy
    (_, start), = Transport.last.starts()
    assert start["data"]["filename"] == "part-ha-pushoff.gcode"
    assert env.coord.print_state.armed is None and env.coord.clearing.state == "off"
    assert env.coord.last_file.name == "part.gcode"


async def test_both_switches_give_one_copy_with_both_changes(env):
    sent = []

    async def send(session, target, path, name, size, **kwargs):
        sent.append(Path(path).read_text())

    env.send.side_effect = send
    await _print(env, _stage(env, "part.gcode", PRINT), skip_purge_line=True,
                 allow_clear_bed=True)
    assert "G1 X205 Y352\n" in sent[1] and "M601" in sent[1]
    assert env.coord.print_state.armed is not None


async def test_a_file_with_no_purge_line_to_skip_is_refused_and_nothing_is_sent(env):
    with pytest.raises(ServiceValidationError) as err:
        await _print(env, _stage(env, "part.gcode", _part()), skip_purge_line=True)
    assert err.value.translation_key == "purge_not_found"
    _nothing_sent(env)


async def test_with_the_switch_off_the_purge_line_is_left_alone(env):
    await _print(env, _stage(env, "part.gcode", PRINT))
    assert [call.args[3] for call in env.send.await_args_list] == ["part.gcode"]


# ------------------------------------------------- findings of the independent review


def test_absolute_extrusion_is_refused_since_its_numbers_are_positions(tmp_path):
    path = tmp_path / "a.gcode"
    path.write_text((HEAD + PURGE + REST).replace("M83\n", "M82\n"))
    _refused(path)
    path.write_text((HEAD + PURGE + REST).replace("M83\n", ""))          # never said: absolute
    _refused(path)


def test_a_second_purge_line_is_refused(tmp_path):
    _refused(_file(tmp_path, PURGE + PURGE))


def test_a_purge_line_must_go_to_the_back_itself_before_it_prints(tmp_path):
    _refused(_file(tmp_path, PURGE.replace("G1 X135 Y352 F18000\n", "G1 X135 F18000\n")
                   .replace("G1 X205 Y352 E3.72454", "G1 X205 E3.72454")))


def test_a_move_with_two_extrusion_words_is_refused(tmp_path):
    _refused(_file(tmp_path, PURGE.replace("G1 X205 Y352 E3.72454", "G1 X205 Y352 E-.5 E2")))


def test_a_changed_file_is_refused_in_the_purge_lines_own_words(tmp_path):
    path = _file(tmp_path)
    edits = pf.scan_purge_line(path, pf.KIND_GCODE)
    path.write_bytes(path.read_bytes().replace(b"E3.72454", b"E9.99999"))
    with pytest.raises(FileRefused) as err:
        pf.write_prepared_copy(path, tmp_path / "out.gcode", pf.KIND_GCODE, None, (), edits=edits)
    assert err.value.key == "purge_not_found"
