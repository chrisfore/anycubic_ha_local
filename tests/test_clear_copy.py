"""The prepared copy of a file: the original with one block inserted (spec 7.2)."""
import hashlib
import zipfile

import pytest
from test_clear_scan import CONFIG, END, HEADER, START

from custom_components.anycubic.anycubic_local import printfile as pf
from custom_components.anycubic.anycubic_local.printfile import FileRefused

BODY = HEADER + START + END + CONFIG
PLATE = BODY.encode()
BLOCK = ["; block start", "M140 S0", "M601", "G1 Z30.00 F600", "; block end"]


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _copy(src, dst, kind, **kwargs):
    scan = pf.scan_for_clearing(src, kind)
    return pf.write_prepared_copy(src, dst, kind, scan, BLOCK, **kwargs)


def test_a_gcode_copy_is_the_original_with_the_block_after_the_last_nozzle_off(tmp_path):
    src, dst = tmp_path / "a.gcode", tmp_path / "out.gcode"
    src.write_bytes(BODY.encode())
    size, md5 = _copy(src, dst, pf.KIND_GCODE)
    out = dst.read_bytes()
    before, after = BODY.split("M104 S0\n")
    assert out == (before + "M104 S0\n" + "\n".join(BLOCK) + "\n" + after).encode()
    assert (size, md5) == (len(out), _md5(out))
    assert src.read_bytes() == BODY.encode()                   # the original is untouched


def test_the_block_uses_the_files_own_line_ends(tmp_path):
    src, dst = tmp_path / "a.gcode", tmp_path / "out.gcode"
    src.write_bytes(BODY.replace("\n", "\r\n").encode())
    _copy(src, dst, pf.KIND_GCODE)
    out = dst.read_bytes()
    assert b"M104 S0\r\n; block start\r\nM140 S0\r\nM601\r\n" in out
    assert b"\n" not in out.replace(b"\r\n", b"")


def test_a_file_that_changed_since_the_scan_is_refused_and_leaves_no_copy(tmp_path):
    src, dst = tmp_path / "a.gcode", tmp_path / "out.gcode"
    src.write_bytes(BODY.encode())
    scan = pf.scan_for_clearing(src, pf.KIND_GCODE)
    src.write_bytes(("; one more line\n" + BODY).encode())
    with pytest.raises(FileRefused) as err:
        pf.write_prepared_copy(src, dst, pf.KIND_GCODE, scan, BLOCK)
    assert err.value.key == "clear_end_code"
    assert not dst.exists()


@pytest.mark.parametrize("line", ["M84", "G28", "M117 hello", "G1 X1\nM601", ""])
def test_a_block_line_that_is_not_one_plain_line_is_refused(tmp_path, line):
    src, dst = tmp_path / "a.gcode", tmp_path / "out.gcode"
    src.write_bytes(BODY.encode())
    scan = pf.scan_for_clearing(src, pf.KIND_GCODE)
    with pytest.raises(FileRefused):
        pf.write_prepared_copy(src, dst, pf.KIND_GCODE, scan, [*BLOCK, line])
    assert not dst.exists()


# ------------------------------------------------- archives


def _archive(path, plate=PLATE, sidecar=None, extra=()):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        if sidecar is not None:                                 # before the plate, on purpose
            archive.writestr("Metadata/plate_1.gcode.md5", sidecar)
        archive.writestr("Metadata/plate_1.gcode", plate)
        archive.writestr("Metadata/plate_1.gcode.metadata", b"\x00\x01meta")
        archive.writestr(zipfile.ZipInfo("Metadata/pic.png"), b"\x89PNG" * 100,
                         compress_type=zipfile.ZIP_STORED)
        for name, data in extra:
            archive.writestr(name, data)
    return path


def _members(path):
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        return {info.filename: archive.read(info) for info in archive.infolist()}


def test_the_copy_of_an_archive_is_its_plate_as_plain_gcode_with_the_block(tmp_path):
    # The printer keeps an archive's plate as plain G-code, so the copy is sent as that.
    src = _archive(tmp_path / "a.gcode.3mf", sidecar="0" * 32)
    dst = tmp_path / "out.gcode"
    size, md5 = _copy(src, dst, pf.KIND_3MF)
    out = dst.read_bytes()
    before, after = BODY.split("M104 S0\n")
    assert out == (before + "M104 S0\n" + "\n".join(BLOCK) + "\n" + after).encode()
    assert (size, md5) == (len(out), _md5(out))
    assert _members(src)["Metadata/plate_1.gcode"] == PLATE     # the original is untouched


def test_the_copy_is_held_to_the_size_caps_and_leaves_nothing_behind(tmp_path):
    src = _archive(tmp_path / "a.3mf", plate=(BODY + "; pad\n" * 40_000).encode())
    dst = tmp_path / "out.gcode"
    scan = pf.scan_for_clearing(src, pf.KIND_3MF)
    with pytest.raises(FileRefused) as err:
        pf.write_prepared_copy(src, dst, pf.KIND_3MF, scan, BLOCK, member_cap=100_000)
    assert err.value.key == "archive_too_large"
    assert not dst.exists()


def test_the_copy_can_be_scanned_and_reads_as_a_pause_of_its_own(tmp_path):
    src, dst = _archive(tmp_path / "a.3mf"), tmp_path / "out.gcode"
    _copy(src, dst, pf.KIND_3MF)
    with pytest.raises(FileRefused) as err:                     # so a copy is never prepared twice
        pf.scan_for_clearing(dst, pf.KIND_GCODE)
    assert err.value.key == "clear_own_pause"


def test_every_line_the_sweep_planner_writes_is_a_line_the_copy_accepts(tmp_path):
    from custom_components.anycubic.anycubic_local import sweep

    wide = (((0.0, 0.0), (350.0, 0.0), (350.0, 300.0), (0.0, 300.0)),)
    src = tmp_path / "a.gcode"
    src.write_bytes(BODY.encode())
    scan = pf.scan_for_clearing(src, pf.KIND_GCODE)
    for i, cooling in enumerate((None, sweep.Dwell(900))):
        block = sweep.clearing_block(sweep.S1_MAX, 200.0, wide, cooling=cooling)
        dst = tmp_path / f"out{i}.gcode"
        pf.write_prepared_copy(src, dst, pf.KIND_GCODE, scan, block)
        assert "\n".join(block).encode() in dst.read_bytes()
