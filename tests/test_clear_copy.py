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


@pytest.mark.parametrize("upper", [True, False])
def test_only_the_plate_and_its_md5_change_in_an_archive(tmp_path, upper):
    old = _md5(BODY.encode())
    src = _archive(tmp_path / "a.gcode.3mf", sidecar=old.upper() if upper else old)
    dst = tmp_path / "out.gcode.3mf"
    size, md5 = _copy(src, dst, pf.KIND_3MF)
    before, after = _members(src), _members(dst)
    assert list(before) == list(after)                          # same members, same order
    plate = after["Metadata/plate_1.gcode"]
    assert b"M104 S0\n; block start\nM140 S0\nM601\n" in plate
    digest = _md5(plate)
    assert after["Metadata/plate_1.gcode.md5"].decode() == (digest.upper() if upper else digest)
    for name in before:
        if name not in ("Metadata/plate_1.gcode", "Metadata/plate_1.gcode.md5"):
            assert after[name] == before[name], name
    data = dst.read_bytes()
    assert (size, md5) == (len(data), _md5(data))
    with zipfile.ZipFile(dst) as archive:
        assert archive.getinfo("Metadata/pic.png").compress_type == zipfile.ZIP_STORED
        assert archive.getinfo("Metadata/plate_1.gcode").compress_type == zipfile.ZIP_DEFLATED


def test_an_archive_with_no_md5_beside_the_plate_gets_none(tmp_path):
    src, dst = _archive(tmp_path / "a.3mf"), tmp_path / "out.3mf"
    _copy(src, dst, pf.KIND_3MF)
    assert "Metadata/plate_1.gcode.md5" not in _members(dst)


def test_the_copy_is_held_to_the_size_caps_and_leaves_nothing_behind(tmp_path):
    src = _archive(tmp_path / "a.3mf", extra=[("Metadata/big.bin", b"\0" * 200_000)])
    dst = tmp_path / "out.3mf"
    scan = pf.scan_for_clearing(src, pf.KIND_3MF)
    with pytest.raises(FileRefused) as err:
        pf.write_prepared_copy(src, dst, pf.KIND_3MF, scan, BLOCK, member_cap=100_000)
    assert err.value.key == "archive_too_large"
    assert not dst.exists()


def test_the_copy_can_be_scanned_and_reads_as_a_pause_of_its_own(tmp_path):
    src, dst = _archive(tmp_path / "a.3mf"), tmp_path / "out.3mf"
    _copy(src, dst, pf.KIND_3MF)
    with pytest.raises(FileRefused) as err:                     # so a copy is never prepared twice
        pf.scan_for_clearing(dst, pf.KIND_3MF)
    assert err.value.key == "clear_own_pause"
    assert pf.inspect_file(dst, pf.KIND_3MF).kind == pf.KIND_3MF


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


def test_an_md5_file_too_large_to_be_one_is_refused(tmp_path):
    src = _archive(tmp_path / "a.3mf", sidecar="0" * 5000)
    dst = tmp_path / "out.3mf"
    scan = pf.scan_for_clearing(src, pf.KIND_3MF)
    with pytest.raises(FileRefused) as err:
        pf.write_prepared_copy(src, dst, pf.KIND_3MF, scan, BLOCK)
    assert err.value.key == "archive_invalid" and not dst.exists()
