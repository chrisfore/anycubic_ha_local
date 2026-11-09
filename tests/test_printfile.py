"""The pure checks on a file before it is sent: names, kinds, archives, and what a file says."""
import hashlib
import warnings
import zipfile

import pytest

from custom_components.anycubic.anycubic_local import printfile as pf
from custom_components.anycubic.anycubic_local.printfile import FileRefused

MARKER = "-ha-pushoff"


def _name(filename, cap=100):
    return pf.printer_name(filename, cap=cap, marker=MARKER)


def _refused(key, call, *args, **kwargs):
    with pytest.raises(FileRefused) as err:
        call(*args, **kwargs)
    assert err.value.key == key
    assert str(err.value) == key          # the text is the key, nothing of the file
    return err.value


# ------------------------------------------------- printer-side names


def test_the_base_name_only_with_the_suffix_in_lower_case():
    name = _name("C:\\Users\\someone\\Desk bracket (2).GCODE.3mf")
    assert name.original == "Desk bracket (2).gcode.3mf"
    assert name.marked == "Desk bracket (2)-ha-pushoff.gcode.3mf"
    assert name.kind == pf.KIND_3MF
    assert _name("/a/b/part.gcode").original == "part.gcode"
    assert _name("part.gcode").kind == pf.KIND_GCODE
    assert _name("part.3mf").kind == pf.KIND_3MF


def test_characters_outside_the_allowed_set_become_underscores():
    assert _name("Désk;brä$ket+v2_[a].gcode").original == "D_sk_br__ket+v2__a_.gcode"
    # Nothing is left that a shell, a path or a URL would read differently.
    for bad in "/\\:*?\"<>|&;%#'`\n\t":
        assert bad not in _name(f"a{bad}b.gcode").original


def test_a_leading_dot_or_space_is_dropped_so_the_file_is_not_hidden():
    assert _name("..hidden.gcode").original == "hidden.gcode"
    assert _name("  spaced  .gcode").original == "spaced.gcode"


def test_the_marker_is_never_truncated_and_both_names_fit_the_cap():
    for cap in (100, 40, 22):
        for length in (1, 50, 99, 100, 150, 400):
            name = _name("x" * length + ".gcode.3mf", cap=cap)
            assert len(name.original) <= cap and len(name.marked) <= cap
            assert name.marked.endswith(MARKER + ".gcode.3mf")
            # Original and copy share a stem, so one can always be found from the other.
            assert name.marked.replace(MARKER, "") == name.original
    # A cap with no room left for a stem beside the marker refuses rather than cutting it.
    _refused("file_name_invalid", _name, "x.gcode.3mf", cap=21)


def test_an_original_already_carrying_the_marker_is_refused_in_any_case():
    for filename in ("part-ha-pushoff.gcode", "PART-HA-PUSHOFF.gcode.3mf",
                     "a-Ha-PushOff-b.3mf", "dir/x-ha-pushoff.gcode"):
        _refused("file_name_marked", _name, filename)
    assert pf.is_marked("a-HA-pushoff.gcode", MARKER)
    assert not pf.is_marked("a-ha-push-off.gcode", MARKER)


def test_unsupported_suffixes_and_empty_names_are_refused():
    for filename in ("part.stl", "part.gcode.zip", "part", "part.bgcode", "part.3mf.txt"):
        _refused("file_type_not_supported", _name, filename)
    for filename in (".gcode", "....gcode.3mf", "   .3mf"):
        _refused("file_name_invalid", _name, filename)


# ------------------------------------------------- numbers


def test_one_finite_number_parser():
    assert pf.finite_number(3) == 3.0
    assert pf.finite_number(" 2.5 ") == 2.5
    for bad in (True, False, None, "nan", "NaN", "inf", "-Infinity", "1e999", float("nan"),
                float("inf"), 10**400, "", "abc", [1], {"a": 1}):
        assert pf.finite_number(bad) is None, bad
    assert pf.whole_number("3", 1, 4) == 3
    for bad in ("3.5", "0", "5", "nan", None, True):
        assert pf.whole_number(bad, 1, 4) is None, bad


def test_material_families_are_the_leading_word():
    assert [pf.material_family(m) for m in ("PLA+", "PLA Silk", "pla high speed", "PETG HF",
                                            "PCTG", "TPU for ACE", "", None, 3)] == [
        "PLA", "PLA", "PLA", "PETG", "PCTG", "TPU", None, None, None]


def test_the_file_model_is_matched_by_name_or_id():
    names = ["Anycubic Kobra S1 Max"]
    assert pf.model_matches(["Anycubic Kobra S1 Max"], "20029", names) is True
    assert pf.model_matches(["Kobra S1 Max"], "20029", names) is True
    assert pf.model_matches(["20029"], "20029", names) is True
    assert pf.model_matches(["Anycubic Kobra S1"], "20029", names) is False
    assert pf.model_matches(["Anycubic Kobra 3"], "20029", names) is False
    assert pf.model_matches(["", "  "], "20029", names) is None
    assert pf.model_matches([], "20029", names) is None
    # Any one source naming this printer is enough.
    assert pf.model_matches(["Something", "Anycubic Kobra S1 Max"], "20029", names) is True


# ------------------------------------------------- G-code files

ORCA_TAIL = (
    "; filament used [g] = 0.00, 0.00, 25.13, 0.35\n"
    "; CONFIG_BLOCK_START\n"
    "; default_filament_colour = ;;;\n"
    "; filament_colour = #212721;#EF60A3;#ffffff;#BA0C2F\n"
    '; filament_type = PETG;"TPU for ACE";PLA;PETG\n'
    "; printer_model = Anycubic Kobra S1 Max\n"
    "; CONFIG_BLOCK_END\n")


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    return path


def test_a_gcode_file_gives_its_model_slots_materials_and_md5(tmp_path):
    body = "; HEADER_BLOCK_START\n; filament: 3,4\n; HEADER_BLOCK_END\nG1 X1\n" + ORCA_TAIL
    path = _write(tmp_path, "a.gcode", body)
    facts = pf.inspect_file(path, pf.KIND_GCODE)
    assert facts.size == len(body.encode())
    assert facts.md5 == hashlib.md5(body.encode()).hexdigest()
    assert facts.models == ("Anycubic Kobra S1 Max",)
    assert facts.used_slots == (3, 4)
    assert facts.materials == {1: "PETG", 2: "TPU for ACE", 3: "PLA", 4: "PETG"}
    assert facts.colors == {1: "#212721", 2: "#EF60A3", 3: "#FFFFFF", 4: "#BA0C2F"}


def test_used_slots_fall_back_to_the_filament_each_slot_used(tmp_path):
    facts = pf.inspect_file(_write(tmp_path, "a.gcode", "G1 X1\n" + ORCA_TAIL), pf.KIND_GCODE)
    assert facts.used_slots == (3, 4)


def test_settings_at_both_ends_of_a_large_file_are_read(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "HEAD_BYTES", 256)
    monkeypatch.setattr(pf, "TAIL_BYTES", 512)
    monkeypatch.setattr(pf, "CHUNK_BYTES", 100)
    body = ("; filament: 2\n" + "G1 X1 Y1\n" * 5000 + ORCA_TAIL)
    facts = pf.inspect_file(_write(tmp_path, "a.gcode", body), pf.KIND_GCODE)
    assert facts.used_slots == (2,)
    assert facts.models == ("Anycubic Kobra S1 Max",)
    assert facts.materials[3] == "PLA"
    # And nothing from the middle was mistaken for a setting.
    assert facts.size == len(body)


def test_a_file_that_says_nothing_has_no_facts(tmp_path):
    body = "; HEADER_BLOCK_START\n; test file: one M117 only\n; HEADER_BLOCK_END\nM117 hi\n"
    facts = pf.inspect_file(_write(tmp_path, "a.gcode", body), pf.KIND_GCODE)
    assert facts.models == () and facts.used_slots is None and facts.materials == {}


def test_non_finite_or_garbled_slot_numbers_say_nothing(tmp_path):
    for line in ("; filament: nan", "; filament: 1,inf", "; filament: 0", "; filament: 1.5",
                 "; filament used [g] = nan, 2", "; filament used [g] = inf",
                 "; filament used [g] = 0, 0"):
        facts = pf.inspect_file(_write(tmp_path, "a.gcode", line + "\n"), pf.KIND_GCODE)
        assert facts.used_slots is None, line


# ------------------------------------------------- archives

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config><plate>
  <metadata key="index" value="1"/>
  <metadata key="printer_model_id" value="Anycubic Kobra S1 Max"/>
  <filament id="1" type="PETG" color="#212721" used_m="0.02" used_g="0.05" />
  <filament id="3" type="PLA" color="#ffffff" used_m="2.86" used_g="8.72" />
  <filament id="4" type="PLA" color="#000000" used_m="0" used_g="0" />
</plate></config>"""


def _archive(tmp_path, members, name="a.gcode.3mf"):
    path = tmp_path / name
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")       # zipfile warns about the duplicates on purpose
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for member, data in members:
                archive.writestr(member, data)
    return path


def _sliced(tmp_path, plate="; filament: 1\nG1 X1\n" + ORCA_TAIL, extra=()):
    return _archive(tmp_path, [("[Content_Types].xml", "<x/>"),
                               ("Metadata/slice_info.config", SLICE_INFO),
                               ("Metadata/plate_1.gcode", plate),
                               ("Metadata/plate_1.gcode.md5", "0" * 32), *extra])


def test_a_sliced_archive_gives_its_facts_with_slice_info_first(tmp_path):
    path = _sliced(tmp_path)
    facts = pf.inspect_file(path, pf.KIND_3MF)
    assert facts.size == path.stat().st_size
    assert facts.md5 == hashlib.md5(path.read_bytes()).hexdigest()   # of the archive itself
    assert facts.models[0] == "Anycubic Kobra S1 Max"
    assert facts.used_slots == (1, 3)              # used_g > 0; slot 4 listed but unused
    assert facts.materials[1] == "PETG" and facts.materials[3] == "PLA"
    assert facts.materials[2] == "TPU for ACE"     # from the plate's own settings
    assert facts.colors[3] == "#FFFFFF"


def test_an_unsliced_archive_is_refused(tmp_path):
    path = _archive(tmp_path, [("3D/3dmodel.model", "<model/>"),
                               ("Metadata/slice_info.config", SLICE_INFO)], "a.3mf")
    _refused("not_sliced", pf.inspect_file, path, pf.KIND_3MF)


def test_more_than_one_plate_is_refused(tmp_path):
    path = _sliced(tmp_path, extra=[("Metadata/plate_2.gcode", "G1\n")])
    _refused("multi_plate", pf.inspect_file, path, pf.KIND_3MF)


def test_duplicate_and_case_variant_member_names_are_refused(tmp_path):
    dup = _sliced(tmp_path, extra=[("Metadata/plate_1.gcode", "G1 X9\n")])
    _refused("archive_invalid", pf.inspect_file, dup, pf.KIND_3MF)
    variant = _sliced(tmp_path, extra=[("metadata/SLICE_INFO.config", "<config/>")])
    _refused("archive_invalid", pf.inspect_file, variant, pf.KIND_3MF)


def test_something_that_is_not_an_archive_is_refused(tmp_path):
    for content in (b"", b"G1 X1\n", b"PK\x03\x04garbage"):
        path = _write(tmp_path, "a.3mf", content)
        _refused("archive_invalid", pf.inspect_file, path, pf.KIND_3MF)


def test_a_damaged_member_is_refused(tmp_path):
    path = _sliced(tmp_path, plate="G1 X1\n" * 2000)
    data = bytearray(path.read_bytes())
    start = data.find(b"Metadata/plate_1.gcode") + 60   # inside its compressed bytes
    data[start:start + 40] = b"\x00" * 40
    path.write_bytes(bytes(data))
    _refused("archive_invalid", pf.inspect_file, path, pf.KIND_3MF)


def test_the_size_caps_trip_while_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "CHUNK_BYTES", 64)
    plate = "G1 X1 Y1\n" * 400                       # 3600 bytes unpacked, far less packed
    path = _sliced(tmp_path, plate=plate)
    read = []
    original = pf._Budget.chunks

    def counting(self, fh):
        for chunk in original(self, fh):
            read.append(len(chunk))
            yield chunk

    monkeypatch.setattr(pf._Budget, "chunks", counting)
    _refused("archive_too_large", pf.inspect_file, path, pf.KIND_3MF, member_cap=1000)
    # Stopped at the cap: it did not read the member through and judge it afterwards.
    assert sum(read) <= len(SLICE_INFO) + 1000
    read.clear()
    _refused("archive_too_large", pf.inspect_file, path, pf.KIND_3MF,
             member_cap=10_000, total_cap=len(SLICE_INFO) + 500)
    assert sum(read) <= len(SLICE_INFO) + 500
    # Within both caps it goes through.
    assert pf.inspect_file(path, pf.KIND_3MF, member_cap=10_000, total_cap=20_000).used_slots


def test_members_are_never_extracted_or_looked_up_by_name(tmp_path, monkeypatch):
    path = _sliced(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("extracted or read by name")

    monkeypatch.setattr(zipfile.ZipFile, "extract", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden)
    original_open = zipfile.ZipFile.open

    def open_by_entry(self, member, *args, **kwargs):
        assert isinstance(member, zipfile.ZipInfo), "opened by name"
        return original_open(self, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", open_by_entry)
    before = sorted(p.name for p in tmp_path.iterdir())
    pf.inspect_file(path, pf.KIND_3MF)
    assert sorted(p.name for p in tmp_path.iterdir()) == before     # nothing written


def test_a_file_that_cannot_be_read_is_refused(tmp_path):
    _refused("file_unreadable", pf.inspect_file, tmp_path / "gone.gcode", pf.KIND_GCODE)
    _refused("file_unreadable", pf.inspect_file, tmp_path / "gone.3mf", pf.KIND_3MF)
