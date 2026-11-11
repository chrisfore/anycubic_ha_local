"""What a file must say before it may be prepared for Clear the bed (spec 7.1)."""
import zipfile

import pytest

from custom_components.anycubic.anycubic_local import printfile as pf
from custom_components.anycubic.anycubic_local.printfile import FileRefused

HEADER = """; HEADER_BLOCK_START
; total layer number: 3
; max_z_height: 12.40
; HEADER_BLOCK_END
"""
START = """M83
M140 S60
M190 S60
EXCLUDE_OBJECT_DEFINE NAME=cube_id_0_copy_0 CENTER=175,175 POLYGON=[[170,170],[180,170],[180,180],[170,180],[170,170]]
G1 Z.2 F600
;TYPE:Outer wall
G1 X170 Y170 E.4 F1800
;TYPE:Inner wall
G1 X180 Y180 E.4
"""
END = """G1 E-.4 F1800
M106 S0
M400
M140 S0
M104 S0
M141 S0
M84
M73 P100 R0
"""
CONFIG = """; CONFIG_BLOCK_START
; brim_type = no_brim
; raft_layers = 0
; curr_bed_type = Textured PEI Plate
; filament_type = PLA
; CONFIG_BLOCK_END
"""


def _gcode(tmp_path, *, header=HEADER, start=START, end=END, config=CONFIG, name="part.gcode",
           newline="\n"):
    path = tmp_path / name
    path.write_bytes((header + start + end + config).replace("\n", newline).encode())
    return path


def _scan(path, kind=pf.KIND_GCODE, **kwargs):
    return pf.scan_for_clearing(path, kind, **kwargs)


def _refused(key, *args, **kwargs):
    with pytest.raises(FileRefused) as err:
        _scan(*args, **kwargs)
    assert err.value.key == key
    return err.value


# ------------------------------------------------- a file that passes


def test_a_plain_part_passes_and_says_what_the_planner_needs(tmp_path):
    scan = _scan(_gcode(tmp_path))
    assert scan.max_z == 12.4
    assert scan.footprints == (((170.0, 170.0), (180.0, 170.0), (180.0, 180.0), (170.0, 180.0),
                                (170.0, 170.0)),)
    assert scan.textured_plate is True


def test_the_insert_point_is_just_after_the_last_nozzle_off_line(tmp_path):
    path = _gcode(tmp_path)
    data = path.read_bytes()
    scan = _scan(path)
    assert data[:scan.insert_at].endswith(b"M104 S0\n")
    assert data[scan.insert_at:].startswith(b"M141 S0\n")
    assert scan.newline == b"\n"


def test_windows_line_ends_are_kept_for_the_block(tmp_path):
    path = _gcode(tmp_path, newline="\r\n")
    scan = _scan(path)
    assert path.read_bytes()[:scan.insert_at].endswith(b"M104 S0\r\n")
    assert scan.newline == b"\r\n"


def test_an_earlier_nozzle_off_does_not_count_only_the_last(tmp_path):
    start = START + "M104 S0\nG1 X171 Y171\nM104 S220\nG1 X175 Y175 E1\n"
    path = _gcode(tmp_path, start=start)
    scan = _scan(path)
    assert path.read_bytes()[scan.insert_at:].startswith(b"M141 S0\n")


def test_a_comment_or_spacing_on_the_nozzle_off_line_is_still_recognized(tmp_path):
    scan = _scan(_gcode(tmp_path, end=END.replace("M104 S0\n", "m104  s0 ; nozzle off\n")))
    assert scan.max_z == 12.4


# ------------------------------------------------- brim, skirt, raft, towers (D11, D23)


@pytest.mark.parametrize(("label", "key"), [
    (";TYPE:Brim", "clear_brim"),
    (";TYPE:Skirt/Brim", "clear_brim"),
    ("; FEATURE: Brim", "clear_brim"),
    (";type:brim", "clear_brim"),
    (";TYPE:Skirt", "clear_skirt"),
    ("; feature: skirt", "clear_skirt"),
    (";TYPE:Prime tower", "clear_tower"),
    ("; FEATURE: Wipe tower", "clear_tower"),
    (";TYPE:Wipe Tower", "clear_tower"),
])
def test_a_brim_skirt_or_tower_in_the_body_is_refused(tmp_path, label, key):
    _refused(key, _gcode(tmp_path, start=START + label + "\nG1 X1 Y1 E1\n"))


def test_auto_brim_decides_nothing_the_body_does(tmp_path):
    config = CONFIG.replace("no_brim", "auto_brim")
    assert _scan(_gcode(tmp_path, config=config)).max_z == 12.4
    _refused("clear_brim", _gcode(tmp_path, config=config,
                                  start=START + ";TYPE:Brim\nG1 X1 Y1 E1\n"))


@pytest.mark.parametrize("config", [
    CONFIG.replace("raft_layers = 0", "raft_layers = 2"),
    CONFIG.replace("; raft_layers = 0\n", ""),
    CONFIG.replace("raft_layers = 0", "raft_layers = nan"),
])
def test_a_raft_or_an_unreadable_raft_setting_is_refused(tmp_path, config):
    _refused("clear_raft", _gcode(tmp_path, config=config))


# ------------------------------------------------- pauses and heat


@pytest.mark.parametrize("line", ["M601", "M600", "m601 ; pause", "M600 X0 Y0"])
def test_a_file_with_its_own_pause_is_refused(tmp_path, line):
    _refused("clear_own_pause", _gcode(tmp_path, start=START + line + "\n"))


def test_a_pause_named_only_in_a_comment_does_not_count(tmp_path):
    assert _scan(_gcode(tmp_path, start=START + "; M601 would pause here\n")).max_z == 12.4


def test_a_file_that_never_heats_the_bed_is_refused(tmp_path):
    start = START.replace("M140 S60\nM190 S60\n", "M140 S0\n")
    _refused("clear_bed_not_heated", _gcode(tmp_path, start=start))


def test_the_printer_start_macro_counts_as_heating_the_bed(tmp_path):
    start = START.replace("M140 S60\nM190 S60\n", "G9111 bedTemp=60 extruderTemp=220\n")
    assert _scan(_gcode(tmp_path, start=start)).max_z == 12.4
    _refused("clear_bed_not_heated", _gcode(
        tmp_path, start=START.replace("M140 S60\nM190 S60\n", "G9111 bedTemp=0 extruderTemp=220\n")))


# ------------------------------------------------- the end code (7.2)


@pytest.mark.parametrize("end", [
    END.replace("M104 S0\n", ""),                         # the nozzle is never turned off
    END.replace("M84\n", ""),                             # the motors are never released
    END.replace("M141 S0\n", "G1 X10 Y10 F3000\n"),       # the head moves in between
    END.replace("M141 S0\n", "G0 Z5\n"),
    END.replace("M141 S0\n", "G28\n"),                    # homing is a move
    "M84\n" + END.replace("M84\n", ""),                   # released before, not after
])
def test_an_end_that_is_not_nozzle_off_then_motors_off_is_not_recognized(tmp_path, end):
    _refused("clear_end_code", _gcode(tmp_path, end=end))


def test_m18_ends_the_file_as_m84_does(tmp_path):
    assert _scan(_gcode(tmp_path, end=END.replace("M84", "M18"))).max_z == 12.4


# ------------------------------------------------- what must be readable


def test_no_part_outline_is_refused(tmp_path):
    start = "\n".join(line for line in START.splitlines()
                      if not line.startswith("EXCLUDE_OBJECT_DEFINE")) + "\n"
    _refused("clear_no_outline", _gcode(tmp_path, start=start))


@pytest.mark.parametrize("polygon", [
    "POLYGON=[[170,170],[180,170]]",                      # not an area
    "POLYGON=[[170,170],[nan,170],[180,180]]",
    "POLYGON=[[170,170],[inf,170],[180,180]]",
    "POLYGON=[[170,170],[180],[180,180]]",
    "POLYGON=garbage",
    "CENTER=1,1",
])
def test_an_outline_that_cannot_be_read_is_refused_not_skipped(tmp_path, polygon):
    start = START + f"EXCLUDE_OBJECT_DEFINE NAME=b {polygon}\n"
    _refused("clear_no_outline", _gcode(tmp_path, start=start))


@pytest.mark.parametrize("header", [
    HEADER.replace("; max_z_height: 12.40\n", ""),
    HEADER.replace("12.40", "nan"),
    HEADER.replace("12.40", "0"),
    HEADER.replace("12.40", "-3"),
])
def test_no_readable_height_is_refused(tmp_path, header):
    _refused("clear_no_height", _gcode(tmp_path, header=header))


def test_every_outline_is_kept(tmp_path):
    start = START + ("EXCLUDE_OBJECT_DEFINE NAME=b CENTER=50,60 "
                     "POLYGON=[[40,50],[60,50],[60.5,70.25],[40,70]]\n")
    assert len(_scan(_gcode(tmp_path, start=start)).footprints) == 2


def test_a_plate_that_is_not_said_to_be_textured_reads_as_not_textured(tmp_path):
    for config in (CONFIG.replace("Textured PEI Plate", "Cool Plate"),
                   CONFIG.replace("; curr_bed_type = Textured PEI Plate\n", "")):
        assert _scan(_gcode(tmp_path, config=config)).textured_plate is False


def test_a_line_too_long_to_be_gcode_stops_the_scan(tmp_path):
    _refused("clear_unreadable", _gcode(tmp_path, start=START + "G1 X1 " + "9" * 70_000 + "\n"))


def test_a_file_that_cannot_be_opened_is_unreadable(tmp_path):
    _refused("file_unreadable", tmp_path / "gone.gcode")


# ------------------------------------------------- archives


def _archive(tmp_path, body: bytes, name="part.gcode.3mf"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("Metadata/plate_1.gcode", body)
        archive.writestr("Metadata/plate_1.gcode.md5", "0" * 32)
    return path


def test_an_archive_is_scanned_through_its_one_plate(tmp_path):
    body = (HEADER + START + END + CONFIG).encode()
    scan = _scan(_archive(tmp_path, body), pf.KIND_3MF)
    assert scan.max_z == 12.4
    assert body[:scan.insert_at].endswith(b"M104 S0\n")
    _refused("clear_brim", _archive(
        tmp_path, (HEADER + START + ";TYPE:Brim\n" + END + CONFIG).encode(), "b.gcode.3mf"),
        pf.KIND_3MF)


def test_an_archive_scan_is_held_to_the_size_caps(tmp_path):
    body = (HEADER + START + "G1 X175 Y175 E1\n" * 5000 + END + CONFIG).encode()
    _refused("archive_too_large", _archive(tmp_path, body), pf.KIND_3MF, member_cap=1000)


# ------------------------------------------------- materials (D15)


def _facts(used, materials):
    return pf.FileFacts(kind=pf.KIND_GCODE, size=1, md5="0" * 32, used_slots=used,
                        materials=materials)


def test_the_families_of_the_used_slots_are_what_counts():
    assert pf.clearing_families(_facts((1,), {1: "PLA+", 2: "TPU"})) == ("PLA",)
    assert pf.clearing_families(_facts((1, 2), {1: "PLA Silk", 2: "PETG HF"})) == ("PETG", "PLA")


def test_with_no_used_slots_every_listed_slot_counts():
    assert pf.clearing_families(_facts(None, {1: "PLA", 3: "PETG"})) == ("PETG", "PLA")
    with pytest.raises(FileRefused) as err:
        pf.clearing_families(_facts(None, {1: "PLA", 2: "ABS"}))
    assert err.value.key == "clear_material"


@pytest.mark.parametrize(("used", "materials"), [
    ((1,), {1: "ABS"}), ((1,), {1: "PCTG"}), ((1,), {1: "TPU for ACE"}), ((1,), {1: ""}),
    ((1,), {}), ((2,), {1: "PLA"}), (None, {}),
])
def test_any_other_or_unknown_material_is_refused(used, materials):
    with pytest.raises(FileRefused) as err:
        pf.clearing_families(_facts(used, materials))
    assert err.value.key == "clear_material"


# ------------------------------------------------- what the file says against what it prints


def test_a_file_with_no_feature_labels_cannot_be_checked_for_a_brim(tmp_path):
    start = "\n".join(line for line in START.splitlines() if not line.startswith(";TYPE")) + "\n"
    _refused("clear_unreadable", _gcode(tmp_path, start=start))


@pytest.mark.parametrize(("label", "key"), [
    ("; feature skirt", "clear_skirt"), (";TYPE Brim", "clear_brim"),
    ("; FEATURE = Prime tower", "clear_tower")])
def test_a_label_written_without_a_colon_is_still_read(tmp_path, label, key):
    _refused(key, _gcode(tmp_path, start=START + label + "\n"))


@pytest.mark.parametrize(("line", "key"), [
    ("N10 M601", "clear_own_pause"), ("N22 G92 Z50", "clear_unreadable"),
    ("n5 m600", "clear_own_pause")])
def test_a_line_number_in_front_hides_nothing(tmp_path, line, key):
    _refused(key, _gcode(tmp_path, start=START + line + "\n"))


def test_printing_outside_the_outlines_the_file_gives_is_refused(tmp_path):
    for move in ("G1 X300 Y320 E1", "G1 Y330 E.5", "G1 X100 E.5", "G2 X175 Y240 I1 J1 E.5"):
        _refused("clear_no_outline", _gcode(tmp_path, start=START + move + "\n"))
    # A little outside is a support or a wall's width, not another part.
    assert _scan(_gcode(tmp_path, start=START + "G1 X183 Y166 E.5\n")).max_z == 12.4
    # A travel move is not printing.
    assert _scan(_gcode(tmp_path, start=START + "G1 X300 Y320 F9000\nG1 X175 Y175\n")).max_z == 12.4


def test_the_purge_line_and_other_start_code_may_print_anywhere(tmp_path):
    start = ";TYPE:Custom\nG1 X135 Y352 F18000\nG1 X205 Y352 E3.7\n" + START
    assert _scan(_gcode(tmp_path, start=start)).max_z == 12.4


def test_printing_higher_than_the_height_the_file_gives_is_refused(tmp_path):
    _refused("clear_no_height", _gcode(tmp_path, start=START + "G1 Z20\nG1 X175 Y175 E1\n"))
    assert _scan(_gcode(tmp_path, start=START + "G1 Z12.4\nG1 X175 Y175 E1\nG1 Z30\n")).max_z == 12.4


def test_absolute_extrusion_counts_only_when_it_advances(tmp_path):
    start = START + "M82\nG92 E0\nG1 X175 Y175 E1\nG1 X300 Y320 E1\nG1 X175 Y175 E.5\n"
    assert _scan(_gcode(tmp_path, start=start)).max_z == 12.4
    _refused("clear_no_outline", _gcode(tmp_path, start=start + "G1 X300 Y320 E2\n"))


def test_printing_in_relative_moves_cannot_be_followed(tmp_path):
    _refused("clear_unreadable", _gcode(tmp_path, start=START + "G91\nG1 X5 Y5 E1\nG90\n"))


def test_a_move_after_the_motors_are_released_is_not_a_recognized_end(tmp_path):
    for line in ("G1 X10 Y10", "G28", "G1 Z5"):
        _refused("clear_end_code", _gcode(tmp_path, end=END + line + "\n"))


def test_outlines_with_more_points_than_any_plate_has_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "MAX_OUTLINE_POINTS_TOTAL", 8)
    start = START + ("EXCLUDE_OBJECT_DEFINE NAME=b POLYGON=[[170,170],[180,170],[180,180],"
                     "[170,180]]\n")
    _refused("clear_no_outline", _gcode(tmp_path, start=start))


def test_printing_at_a_height_the_file_does_not_give_cannot_be_followed(tmp_path):
    for lost in ("G91\nG1 Z50\nG90\n", "G28 Z\n"):
        _refused("clear_unreadable", _gcode(tmp_path, start=START + lost + "G1 X175 Y175 E1\n"))
    start = START.replace("G1 Z.2 F600\n", "")
    _refused("clear_unreadable", _gcode(tmp_path, start=start))


def test_a_plus_sign_or_a_checksum_hides_nothing(tmp_path):
    _refused("clear_no_outline", _gcode(tmp_path, start=START + "G1 X+300 Y+320 E+1\n"))
    _refused("clear_own_pause", _gcode(tmp_path, start=START + "N10 M601*45\n"))


def test_an_arc_counts_by_its_whole_circle_not_its_end_points(tmp_path):
    assert _scan(_gcode(tmp_path, start=START + "G1 X174 Y175\nG2 X176 Y175 I1 J0 E.2\n")).max_z
    _refused("clear_no_outline",
             _gcode(tmp_path, start=START + "G1 X174 Y175\nG2 X176 Y175 I1 J-80 E.2\n"))
    _refused("clear_unreadable",
             _gcode(tmp_path, start=START + "G1 X174 Y175\nG3 X176 Y175 R200 E.2\n"))


def test_relative_moves_make_extrusion_relative_too(tmp_path):
    _refused("clear_unreadable",
             _gcode(tmp_path, start=START + "M82\nG92 E50\nG91\nG1 X5 Y5 E1\nG90\n"))


def test_a_full_circle_with_no_end_point_is_measured_too(tmp_path):
    _refused("clear_no_outline",
             _gcode(tmp_path, start=START + "G1 X175 Y175\nG2 I100 J0 E5\n"))
    _refused("clear_unreadable",
             _gcode(tmp_path, start=START + "G91\nG2 I100 J0 E5\nG90\n"))
