"""Checks on a file before it is sent to the printer: its name, its kind, and what it says.

No Home Assistant here, and nothing is written anywhere. Everything that reads a file blocks,
so it runs in an executor. Archives are read under the same rules every time:

- a member name that appears twice, or twice in different case, refuses the whole archive,
  because two readers of the same archive could then pick different members;
- members are opened by their entry in the archive's own list, never looked up by name, and
  nothing is extracted to disk;
- the bytes each member unpacks to are counted as they are read, against a cap per member and
  a cap for the archive as a whole, so a small archive cannot unpack to something huge.

Every number read from a file goes through finite_number(), the one parser the checks use.
A value that is missing, not a number, NaN or infinite reads as None, and every check is
written as "passes if within range", so None never passes one.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

# What can be sent, longest first so that ".gcode.3mf" is not taken for ".3mf".
SUFFIXES = (".gcode.3mf", ".gcode", ".3mf")
KIND_GCODE = "gcode"
KIND_3MF = "3mf"

# A printer-side name keeps these and nothing else; anything outside becomes "_".
_NOT_IN_A_NAME = re.compile(r"[^A-Za-z0-9 ._()+\-]")

# How much is read at a time, and how much of the start and the end of a G-code file is kept
# to read its comments from. Slicers write a header block at the start (the slots used, the
# tallest part) and their settings at the very end (the printer model, the filament types).
CHUNK_BYTES = 1 << 20
HEAD_BYTES = 1 << 20
TAIL_BYTES = 1 << 20
# One comment line longer than this is a picture or a settings dump, never a value we read.
_LONGEST_LINE = 64 * 1024

# What an archive may unpack to, counted while reading. Home Assistant takes files of up to
# 100 MB, and G-code packs down to about a fifth of its size, so a real plate stays well
# under these.
MEMBER_CAP = 1 << 30
TOTAL_CAP = 1 << 30
# An archive with more entries than this is not a slicer's.
MAX_MEMBERS = 10_000
# Filament slots a file may name (four per multi-color unit, several units).
MAX_SLOTS = 64

_PLATE = re.compile(r"metadata/plate_\d+\.gcode", re.IGNORECASE)
_SLICE_INFO = re.compile(r"metadata/slice_info\.config", re.IGNORECASE)
# "; key = value" and "; key: value", the two ways slicers write a setting into a comment.
_COMMENT_SETTING = re.compile(r";\s*([A-Za-z_][\w \[\]]{0,63}?)\s*[=:]\s?(.*)")
_TAG = re.compile(r"<([A-Za-z_][\w-]*)\b([^<>]*)>")
_ATTRIBUTE = re.compile(r'([A-Za-z_][\w-]*)\s*=\s*"([^"]*)"')
_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
_WORD = re.compile(r"[A-Za-z0-9]+")


class FileRefused(Exception):
    """The file cannot be sent. `key` says why, and names the message the user is shown.

    The text is the key alone: nothing of the file, its name or its contents is in it.
    """

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def finite_number(value) -> float | None:
    """`value` as a finite float, or None if it is not one.

    The one parser every check uses. A bool is not a number here, though Python counts it
    as one, and neither is NaN, an infinity, or a string that only looks numeric.
    """
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            number = float(value.strip())
        else:
            return None
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def whole_number(value, low: int, high: int) -> int | None:
    """`value` as a whole number from `low` to `high`, or None if it is not one."""
    number = finite_number(value)
    if number is None or number != int(number) or not low <= number <= high:
        return None
    return int(number)


def split_suffix(name: str) -> tuple[str, str] | None:
    """(stem, suffix) for a name with a suffix that can be sent, the suffix in lower case."""
    lowered = name.lower()
    for suffix in SUFFIXES:
        if lowered.endswith(suffix):
            return name[: len(name) - len(suffix)], suffix
    return None


def kind_of(suffix: str) -> str:
    """G-code, or an archive: a .gcode.3mf and a sliced .3mf are both zip archives."""
    return KIND_GCODE if suffix == ".gcode" else KIND_3MF


def is_marked(name: str, marker: str) -> bool:
    """Does this file name carry the push-off marker, in any case?"""
    return marker.casefold() in name.casefold()


@dataclass(frozen=True)
class PrinterName:
    """What a file is called on the printer, and what its push-off copy would be called."""

    original: str
    marked: str
    kind: str


def printer_name(filename: str, *, cap: int, marker: str) -> PrinterName:
    """The printer-side name for a file the user picked.

    The base name only. Any character outside A-Z, a-z, 0-9, space and ._()+- becomes "_".
    The stem is cut so that stem, marker and suffix fit `cap` together. The original is cut
    the same way, so the two always share a stem and the marker is never what gets cut. A
    name that already carries the marker is refused: a copy must always be told from an
    original.
    """
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    parts = split_suffix(base)
    if parts is None:
        raise FileRefused("file_type_not_supported")
    stem, suffix = parts
    stem = _NOT_IN_A_NAME.sub("_", stem).lstrip(". ").rstrip(" ")
    if is_marked(stem, marker):
        raise FileRefused("file_name_marked")
    room = cap - len(marker) - len(suffix)
    stem = stem[:room].rstrip(" ") if room > 0 else ""
    if not stem:
        raise FileRefused("file_name_invalid")
    return PrinterName(original=f"{stem}{suffix}", marked=f"{stem}{marker}{suffix}",
                       kind=kind_of(suffix))


def material_family(material) -> str | None:
    """A material's family: the first word of its name, in upper case.

    "PLA+", "PLA Silk" and "PLA High Speed" are all PLA, "PETG HF" is PETG, and "TPU for
    ACE" is TPU. A name with no word in it has no family.
    """
    if not isinstance(material, str):
        return None
    word = _WORD.search(material)
    return word.group(0).upper() if word else None


def _model_words(text: str) -> str:
    words = _WORD.findall(text.casefold())
    if words and words[0] == "anycubic":
        words = words[1:]
    return " ".join(words)


def model_matches(models: Iterable[str], model_id: str, model_names: Iterable[str]) -> bool | None:
    """Was a file sliced for this printer? None if it does not say.

    `models` is every printer model the file names. Any one of them naming this printer, by
    its model id or by its name with or without the maker's name in front, is a match. A
    file that names only other printers was sliced for another printer.
    """
    named = [m.strip() for m in models if isinstance(m, str) and _model_words(m)]
    if not named:
        return None
    wanted = {_model_words(n) for n in model_names if isinstance(n, str) and n}
    return any(m == model_id or _model_words(m) in wanted for m in named)


@dataclass(frozen=True)
class FileFacts:
    """What the checks need to know about a file, read without keeping any of it."""

    kind: str
    size: int
    md5: str
    # Every printer model the file names; empty if it names none.
    models: tuple[str, ...] = ()
    # The filament slots the file prints with, numbered from 1; None if it does not say.
    used_slots: tuple[int, ...] | None = None
    # Slot number -> the material the file expects there, for every slot it lists.
    materials: dict[int, str] = field(default_factory=dict)
    # Slot number -> "#RRGGBB" the slicer showed for it.
    colors: dict[int, str] = field(default_factory=dict)


class _Budget:
    """Counts what an archive unpacks to as it is read, and stops the read at a cap."""

    def __init__(self, member_cap: int, total_cap: int) -> None:
        self._member_cap, self._total_cap, self._total = member_cap, total_cap, 0

    def chunks(self, fh) -> Iterator[bytes]:
        member = 0
        for chunk in iter(partial(fh.read, CHUNK_BYTES), b""):
            member += len(chunk)
            self._total += len(chunk)
            if member > self._member_cap or self._total > self._total_cap:
                raise FileRefused("archive_too_large")
            yield chunk


@dataclass
class _Window:
    """The start and the end of a stream, kept while it goes past."""

    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    size: int = 0

    def add(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if len(self.head) < HEAD_BYTES:
            self.head += chunk[: HEAD_BYTES - len(self.head)]
        self.tail += chunk
        if len(self.tail) > TAIL_BYTES:
            del self.tail[: len(self.tail) - TAIL_BYTES]

    def settings(self) -> dict[str, str]:
        """Every "; key = value" comment in the kept start and end. The first one wins."""
        whole = self.size <= HEAD_BYTES
        lines = _lines(self.head, drop_first=False, drop_last=not whole)
        if not whole:
            lines += _lines(self.tail, drop_first=self.size > TAIL_BYTES, drop_last=False)
        found: dict[str, str] = {}
        for line in lines:
            if not line.startswith(";") or len(line) > _LONGEST_LINE:
                continue
            match = _COMMENT_SETTING.fullmatch(line.rstrip())
            if match:
                key = " ".join(match.group(1).lower().split())
                found.setdefault(key, match.group(2).strip())
        return found


def _lines(buffer: bytearray, *, drop_first: bool, drop_last: bool) -> list[str]:
    lines = bytes(buffer).decode("utf-8", errors="replace").splitlines()
    # A window cut out of the middle of a file starts and ends partway through a line.
    if drop_first and lines:
        lines = lines[1:]
    if drop_last and lines:
        lines = lines[:-1]
    return lines


def _unquoted(value: str) -> str:
    return value.strip().strip('"').strip()


def _settings_facts(settings: dict[str, str]):
    """(models, used slots, materials, colors) as a G-code file's comments give them."""
    models = tuple(m for m in (_unquoted(settings.get("printer_model", "")),) if m)
    materials = {i: t for i, t in enumerate(
        (_unquoted(t) for t in settings.get("filament_type", "").split(";")), start=1)
        if t and i <= MAX_SLOTS}
    colors = {}
    for i, c in enumerate(settings.get("filament_colour", "").split(";"), start=1):
        color = _COLOR.match(_unquoted(c))
        if color and i <= MAX_SLOTS:
            colors[i] = color.group(0).upper()
    return models, _used_from_settings(settings), materials, colors


def _used_from_settings(settings: dict[str, str]) -> tuple[int, ...] | None:
    """The slots a G-code file prints with, from its own comments; None if they do not say.

    A header line listing the slots wins. Otherwise the filament each slot used, by weight
    or length: a slot with more than none is used. Anything unreadable in a line makes the
    whole line say nothing, rather than half of it being believed.
    """
    listed = settings.get("filament")
    if listed:
        slots = [whole_number(s, 1, MAX_SLOTS) for s in listed.split(",")]
        if slots and None not in slots:
            return tuple(sorted(set(slots)))
    for key in ("filament used [g]", "filament used [mm]"):
        amounts = settings.get(key)
        if not amounts:
            continue
        values = [finite_number(a) for a in amounts.split(",")]
        if values and None not in values and len(values) <= MAX_SLOTS:
            used = tuple(i for i, v in enumerate(values, start=1) if v > 0)
            if used:
                return used
    return None


def _tags(text: str, name: str) -> Iterator[dict[str, str]]:
    for match in _TAG.finditer(text):
        if match.group(1).lower() == name:
            yield {k.lower(): html.unescape(v) for k, v in _ATTRIBUTE.findall(match.group(2))}


def _slice_info_facts(text: str):
    """(models, used slots, materials, colors) from an archive's slice info."""
    models = tuple(tag.get("value", "").strip() for tag in _tags(text, "metadata")
                   if tag.get("key") == "printer_model_id" and tag.get("value", "").strip())
    materials, colors, listed, used = {}, {}, [], []
    for tag in _tags(text, "filament"):
        slot = whole_number(tag.get("id"), 1, MAX_SLOTS)
        if slot is None:
            continue
        listed.append(slot)
        if tag.get("type", "").strip():
            materials[slot] = tag["type"].strip()
        color = _COLOR.match(tag.get("color", "").strip())
        if color:
            colors[slot] = color.group(0).upper()
        amounts = [finite_number(tag.get(k)) for k in ("used_g", "used_m")]
        if any(a is not None and a > 0 for a in amounts):
            used.append(slot)
    slots = used or listed
    return models, (tuple(sorted(set(slots))) if slots else None), materials, colors


def _md5_of(path: Path) -> tuple[int, str]:
    digest, size = hashlib.md5(usedforsecurity=False), 0
    with path.open("rb") as fh:
        for chunk in iter(partial(fh.read, CHUNK_BYTES), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _inspect_gcode(path: Path) -> FileFacts:
    digest, window = hashlib.md5(usedforsecurity=False), _Window()
    with path.open("rb") as fh:
        for chunk in iter(partial(fh.read, CHUNK_BYTES), b""):
            digest.update(chunk)
            window.add(chunk)
    models, used, materials, colors = _settings_facts(window.settings())
    return FileFacts(kind=KIND_GCODE, size=window.size, md5=digest.hexdigest(), models=models,
                     used_slots=used, materials=materials, colors=colors)


# What zipfile and zlib raise for an archive that is damaged, truncated, encrypted or packed
# a way that cannot be read.
_UNREADABLE = (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError,
               EOFError, zlib.error, ValueError, OSError)


def _inspect_archive(path: Path, member_cap: int, total_cap: int) -> FileFacts:
    size, md5 = _md5_of(path)
    try:
        with zipfile.ZipFile(path) as archive:
            plate = _one_plate(archive)
            entries = archive.infolist()
            budget = _Budget(member_cap, total_cap)
            info_text = ""
            slice_info = next((e for e in entries if _SLICE_INFO.fullmatch(e.filename)), None)
            if slice_info is not None:
                with archive.open(slice_info) as fh:
                    info_text = b"".join(budget.chunks(fh)).decode("utf-8", errors="replace")
            window = _Window()
            with archive.open(plate) as fh:
                for chunk in budget.chunks(fh):
                    window.add(chunk)
    except FileRefused:
        raise
    except _UNREADABLE:
        raise FileRefused("archive_invalid") from None
    g_models, g_used, g_materials, g_colors = _settings_facts(window.settings())
    s_models, s_used, s_materials, s_colors = _slice_info_facts(info_text)
    return FileFacts(kind=KIND_3MF, size=size, md5=md5, models=(*s_models, *g_models),
                     used_slots=s_used or g_used, materials={**g_materials, **s_materials},
                     colors={**g_colors, **s_colors})


def inspect_file(path: Path, kind: str, *, member_cap: int = MEMBER_CAP,
                 total_cap: int = TOTAL_CAP) -> FileFacts:
    """Read a file once (twice for an archive) and say what the checks need to know.

    Blocking: run it in an executor. Raises FileRefused for an archive that breaks the rules
    in this module's docstring, has no sliced plate, or has more than one, and for a file
    that cannot be read at all.
    """
    try:
        if kind == KIND_GCODE:
            return _inspect_gcode(path)
        return _inspect_archive(path, member_cap, total_cap)
    except OSError:
        raise FileRefused("file_unreadable") from None


# ------------------------------------------------------------------ Clear the bed: checks
#
# What a file must say before a copy of it may be prepared for Clear the bed. One pass over
# the G-code, line by line, under the same archive rules as above. Every check is "passes if
# it can be read and is within range": anything that cannot be read refuses the file.

# The materials a part may be pushed off in.
CLEARING_FAMILIES = frozenset({"PLA", "PETG"})
# More outlines, or more points in one, than any plate has.
MAX_OUTLINES = 2000
MAX_OUTLINE_POINTS = 5000
MAX_OUTLINE_POINTS_TOTAL = 200_000
# How far outside the outlines, and above the height, a part may be printed and still be
# the part the file describes: a support, the width of a wall, the last layer.
OUTLINE_TOLERANCE = 5.0
HEIGHT_TOLERANCE = 1.0
# The longest an md5 file beside a plate can be.
_SIDECAR_BYTES = 1024

_FEATURE = re.compile(rb";\s*(?:TYPE|FEATURE)\b[\s:=]*(.*)", re.IGNORECASE)
_LINE_NUMBER = re.compile(rb"^N\d+\s*")
_WORD_VALUES = {axis: re.compile(rb"(?<![A-Z])" + axis + rb"\s*(-?\d*\.?\d+)")
                for axis in (b"X", b"Y", b"Z", b"E")}
_POLYGON = re.compile(rb"POLYGON=(\S+)", re.IGNORECASE)
_S_VALUE = re.compile(rb"\bS\s*([-+0-9.]+)")
_BED_TEMP = re.compile(rb"\bBEDTEMP\s*=\s*([-+0-9.]+)")
_AXIS = re.compile(rb"[XYZE]\s*[-+0-9.]")
_XYZ = re.compile(rb"[XYZ]\s*[-+0-9.]")
# G commands that move nothing and leave the coordinates as they are.
_STILL = frozenset({b"G4", b"G21", b"G90", b"G91"})
_LINEAR = frozenset({b"G0", b"G1", b"G2", b"G3"})


@dataclass(frozen=True)
class ClearScan:
    """What the sweep planner and the file change need from a file that passed."""

    # The tallest part, in mm.
    max_z: float
    # Each part's outline on the bed: a closed run of (x, y) points, in mm.
    footprints: tuple[tuple[tuple[float, float], ...], ...]
    # Does the file say it was sliced for a textured plate?
    textured_plate: bool
    # Where the block goes: the offset in the G-code just after the last nozzle-off line.
    insert_at: int
    # The line end that line used, for the block to use too.
    newline: bytes


def _gcode_lines(chunks: Iterable[bytes]) -> Iterator[tuple[bytes, int]]:
    """Each line of a G-code stream with the offset just after it, its line end included.

    A comment too long to be a value is dropped where it stands; anything else that long is
    not G-code, and the file is refused.
    """
    rest, offset, dropping = b"", 0, False
    for chunk in chunks:
        parts = (rest + chunk).split(b"\n")
        rest = parts.pop()
        for part in parts:
            offset += len(part) + 1
            if dropping:
                dropping = False
            elif len(part) <= _LONGEST_LINE:
                yield part, offset
            elif not part.lstrip().startswith(b";"):
                raise FileRefused("clear_unreadable")
        if len(rest) > _LONGEST_LINE:
            if not dropping and not rest.lstrip().startswith(b";"):
                raise FileRefused("clear_unreadable")
            offset += len(rest)
            rest, dropping = b"", True
    if rest and not dropping:
        yield rest, offset + len(rest)


def _first_number(pattern: re.Pattern[bytes], command: bytes) -> float | None:
    match = pattern.search(command)
    return finite_number(match.group(1).decode("ascii", "replace")) if match else None


def _word_value(command: bytes, axis: bytes) -> float | None:
    """The number after one axis letter in a move, or None if the move has none."""
    match = _WORD_VALUES[axis].search(command[2:])
    return finite_number(match.group(1).decode("ascii", "replace")) if match else None


def _outline(command: bytes) -> tuple[tuple[float, float], ...]:
    """The outline an EXCLUDE_OBJECT_DEFINE line gives, or a refusal if it gives none."""
    match = _POLYGON.search(command)
    try:
        points = json.loads(match.group(1)) if match else None
    except (ValueError, RecursionError):
        points = None
    if not isinstance(points, list) or not 3 <= len(points) <= MAX_OUTLINE_POINTS:
        raise FileRefused("clear_no_outline")
    outline = []
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            raise FileRefused("clear_no_outline")
        x, y = (None if isinstance(v, str) else finite_number(v) for v in point)
        if x is None or y is None:
            raise FileRefused("clear_no_outline")
        outline.append((x, y))
    return tuple(outline)


class _ClearScanner:
    """Reads a G-code stream once and refuses at the first thing that rules clearing out.

    It also follows the head, so that what the file says about its parts can be held
    against what it prints: printing outside the outlines it gives, or higher than the
    height it gives, refuses the file, because the sweep is planned from those two.
    """

    def __init__(self) -> None:
        self.window = _Window()
        self.footprints: list[tuple[tuple[float, float], ...]] = []
        self.bed_heated = False
        self.insert_at: int | None = None
        self.newline = b"\n"
        self._moved = False
        self._released = False
        self._moved_after_release = False
        self._points = 0
        # The feature being printed. Start and end code ("custom"), where the purge line
        # is, is not a part; before the first label everything is start code.
        self._in_part = False
        self._labels = 0
        # Where the head is, as far as the file's absolute moves say.
        self._absolute = True
        self._relative_e = False
        self._x: float | None = None
        self._y: float | None = None
        self._z: float | None = None
        self._e = 0.0
        # What was printed as part of a part: its bounds, and how high it went.
        self._printed: list[float] | None = None
        self._printed_z = 0.0

    def scan(self, chunks: Iterable[bytes]) -> ClearScan:
        def kept() -> Iterator[bytes]:
            for chunk in chunks:
                self.window.add(chunk)
                yield chunk

        for raw, end in _gcode_lines(kept()):
            self._line(raw, end)
        return self._result()

    def _line(self, raw: bytes, end: int) -> None:
        text = raw.strip()
        if not text:
            return
        if text.startswith(b";"):
            label = _FEATURE.match(text)
            if label:
                name = label.group(1).lower()
                for word, key in ((b"brim", "clear_brim"), (b"skirt", "clear_skirt"),
                                  (b"tower", "clear_tower")):
                    if word in name:
                        raise FileRefused(key)
                self._in_part = b"custom" not in name
                self._labels += self._in_part
            return
        command = b" ".join(text.split(b";", 1)[0].upper().split())
        # A line number in front changes nothing about what the line does.
        command = _LINE_NUMBER.sub(b"", command)
        if not command:
            return
        word = command.split(b" ", 1)[0]
        if word == b"EXCLUDE_OBJECT_DEFINE":
            if len(self.footprints) >= MAX_OUTLINES:
                raise FileRefused("clear_no_outline")
            # Read from the line as written: the outline's numbers have no upper case.
            outline = _outline(text)
            self._points += len(outline)
            if self._points > MAX_OUTLINE_POINTS_TOTAL:
                raise FileRefused("clear_no_outline")
            self.footprints.append(outline)
        elif word in (b"M600", b"M601"):
            raise FileRefused("clear_own_pause")
        elif word in (b"M140", b"M190"):
            value = _first_number(_S_VALUE, command)
            self.bed_heated |= value is not None and value > 0
        elif word == b"G9111":
            value = _first_number(_BED_TEMP, command)
            self.bed_heated |= value is not None and value > 0
        elif word == b"M104":
            if _first_number(_S_VALUE, command) == 0:
                self.insert_at, self._moved, self._released = end, False, False
                self._moved_after_release = False
                self.newline = b"\r\n" if raw.endswith(b"\r") else b"\n"
        elif word in (b"M84", b"M18"):
            if self.insert_at is not None and not self._moved:
                self._released = True
        elif word == b"M82":
            self._relative_e = False
        elif word == b"M83":
            self._relative_e = True
        elif word.startswith(b"G"):
            self._g(word, command)

    def _g(self, word: bytes, command: bytes) -> None:
        if word == b"G90":
            self._absolute = True
        elif word == b"G91":
            self._absolute = False
        elif word == b"G92":
            if _XYZ.search(command[3:]):
                # The coordinates no longer mean what the sweep takes them to mean.
                raise FileRefused("clear_unreadable")
            value = _word_value(command, b"E")
            if value is not None:
                self._e = value
        moves = (word not in _STILL and word != b"G92"
                 and (word not in _LINEAR or bool(_AXIS.search(command[len(word):]))))
        if moves and self.insert_at is not None:
            if self._released:
                self._moved_after_release = True
            else:
                self._moved = True
        if word in _LINEAR:
            self._follow(command)
        elif moves:
            # Homing, probing and the like: where the head is afterward is not in the file.
            self._x = self._y = self._z = None

    def _follow(self, command: bytes) -> None:
        """Keep track of the head through one G0 to G3, and of what it prints as a part."""
        x, y, z, e = (_word_value(command, axis) for axis in (b"X", b"Y", b"Z", b"E"))
        if e is None:
            extrudes = False
        elif self._relative_e:
            extrudes = e > 0
        else:
            extrudes, self._e = e > self._e, e
        travels = x is not None or y is not None
        if not self._absolute:
            if extrudes and travels and self._in_part:
                raise FileRefused("clear_unreadable")
            if travels:
                self._x = self._y = None
            if z is not None:
                self._z = None
            return
        self._x = self._x if x is None else x
        self._y = self._y if y is None else y
        self._z = self._z if z is None else z
        if not (extrudes and travels and self._in_part):
            return
        if self._x is None or self._y is None:
            raise FileRefused("clear_unreadable")
        if self._printed is None:
            self._printed = [self._x, self._x, self._y, self._y]
        else:
            box = self._printed
            box[0], box[1] = min(box[0], self._x), max(box[1], self._x)
            box[2], box[3] = min(box[2], self._y), max(box[3], self._y)
        if self._z is not None:
            self._printed_z = max(self._printed_z, self._z)

    def _result(self) -> ClearScan:
        settings = self.window.settings()
        if not self._labels:
            # Nothing says what was printed as what: a brim cannot be ruled out.
            raise FileRefused("clear_unreadable")
        if finite_number(settings.get("raft_layers")) != 0:
            raise FileRefused("clear_raft")
        if not self.bed_heated:
            raise FileRefused("clear_bed_not_heated")
        if self.insert_at is None or not self._released or self._moved_after_release:
            raise FileRefused("clear_end_code")
        if not self.footprints:
            raise FileRefused("clear_no_outline")
        max_z = finite_number(settings.get("max_z_height"))
        if max_z is None or max_z <= 0:
            raise FileRefused("clear_no_height")
        if self._printed_z > max_z + HEIGHT_TOLERANCE:
            raise FileRefused("clear_no_height")
        if self._printed is not None:
            xs = [x for outline in self.footprints for x, _ in outline]
            ys = [y for outline in self.footprints for _, y in outline]
            low_x, high_x, low_y, high_y = self._printed
            if (low_x < min(xs) - OUTLINE_TOLERANCE or high_x > max(xs) + OUTLINE_TOLERANCE
                    or low_y < min(ys) - OUTLINE_TOLERANCE
                    or high_y > max(ys) + OUTLINE_TOLERANCE):
                raise FileRefused("clear_no_outline")
        return ClearScan(
            max_z=max_z, footprints=tuple(self.footprints),
            textured_plate="textured" in settings.get("curr_bed_type", "").lower(),
            insert_at=self.insert_at, newline=self.newline)


def _file_chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as fh:
        yield from iter(partial(fh.read, CHUNK_BYTES), b"")


def _one_plate(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    """An archive's one sliced plate, under the rules in this module's docstring."""
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if (len(entries) > MAX_MEMBERS or len(set(names)) != len(names)
            or len({n.casefold() for n in names}) != len(names)):
        raise FileRefused("archive_invalid")
    plates = [e for e in entries if _PLATE.fullmatch(e.filename)]
    if not plates:
        raise FileRefused("not_sliced")
    if len(plates) > 1:
        raise FileRefused("multi_plate")
    return plates[0]


def scan_for_clearing(path: Path, kind: str, *, member_cap: int = MEMBER_CAP,
                      total_cap: int = TOTAL_CAP) -> ClearScan:
    """May a copy of this file be prepared for Clear the bed? What the copy needs, if so.

    Blocking: run it in an executor. Raises FileRefused, with the reason as its key, for a
    brim, a skirt, a raft or a tower, for a pause of the file's own, for a bed that is never
    heated, for an end that is not "nozzle off, then motors off, with no move between", and
    for an outline or a height that cannot be read.
    """
    try:
        if kind == KIND_GCODE:
            return _ClearScanner().scan(_file_chunks(path))
        try:
            with zipfile.ZipFile(path) as archive:
                plate = _one_plate(archive)
                with archive.open(plate) as fh:
                    return _ClearScanner().scan(_Budget(member_cap, total_cap).chunks(fh))
        except FileRefused:
            raise
        except _UNREADABLE as err:
            if isinstance(err, OSError) and not isinstance(err, zipfile.BadZipFile):
                raise
            raise FileRefused("archive_invalid") from None
    except OSError:
        raise FileRefused("file_unreadable") from None


def clearing_families(facts: FileFacts) -> tuple[str, ...]:
    """The material families a file prints with, if a part in each may be pushed off.

    The slots the file uses; if it does not say, every slot it lists. Each must name PLA or
    PETG by its first word. A slot with no material, or a file with no slots, is refused.
    """
    slots = facts.used_slots or tuple(sorted(facts.materials))
    families = {material_family(facts.materials.get(slot)) for slot in slots}
    if not families or not families <= CLEARING_FAMILIES:
        raise FileRefused("clear_material")
    return tuple(sorted(families))


# ------------------------------------------------------------------ Clear the bed: the copy

# What a block line may be: a comment, or one of the commands the sweep planner writes.
# Checked again here, where the block meets a file, so that nothing else is ever inserted.
_BLOCK_LINE = re.compile(
    r"(?:;[^\r\n]*|M140 S0|M141 S0|M104 S0|G90|M601|M106 S(?:0|255)|M106 P[23] S(?:0|204)|M109 S\d{1,2}"
    r"|G4 P\d{1,7}|G1 Z\d{1,3}\.\d\d F\d{1,5}|G1 X-?\d{1,3}\.\d\d Y-?\d{1,3}\.\d\d F\d{1,5})")
_NOZZLE_OFF = re.compile(rb"M104\s+S0+(?:\.0*)?\s*(?:;.*)?", re.IGNORECASE)


def _block_bytes(block: Iterable[str], newline: bytes) -> bytes:
    lines = list(block)
    if not lines or not all(isinstance(line, str) and _BLOCK_LINE.fullmatch(line)
                            for line in lines):
        raise FileRefused("clear_unreadable")
    return b"".join(line.encode("ascii", "replace") + newline for line in lines)


def _insert(chunks: Iterable[bytes], scan: ClearScan, block: bytes) -> Iterator[bytes]:
    """The stream with the block put in at the scan's insert point.

    The line that ends there must still be the nozzle-off line the scan found: if it is
    not, this is not the file that was scanned, and nothing is written past that point.
    """
    seen, tail, done = 0, b"", False
    for chunk in chunks:
        if done:
            yield chunk
            continue
        if seen + len(chunk) < scan.insert_at:
            seen += len(chunk)
            tail = (tail + chunk)[-_LONGEST_LINE:]
            yield chunk
            continue
        cut = scan.insert_at - seen
        head = chunk[:cut]
        last = (tail + head)[-_LONGEST_LINE:]
        if not last.endswith(b"\n") or not _NOZZLE_OFF.fullmatch(
                last[:-1].rsplit(b"\n", 1)[-1].strip()):
            raise FileRefused("clear_end_code")
        yield head
        yield block
        yield chunk[cut:]
        done = True
    if not done:
        raise FileRefused("clear_end_code")


def _copy_gcode(src: Path, dst: Path, scan: ClearScan, block: bytes) -> None:
    with dst.open("wb") as out:
        for chunk in _insert(_file_chunks(src), scan, block):
            out.write(chunk)


def _fresh_entry(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    """A new entry with the old one's name, time, packing and attributes, and nothing else
    of its header: the old one's flags and extras describe bytes that are not copied."""
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise FileRefused("archive_invalid")
    entry = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    entry.compress_type = info.compress_type
    entry.external_attr = info.external_attr
    entry.create_system = info.create_system
    entry.comment = info.comment
    return entry


def _copy_archive(src: Path, dst: Path, scan: ClearScan, block: bytes, member_cap: int,
                  total_cap: int) -> None:
    with zipfile.ZipFile(src) as archive:
        plate = _one_plate(archive)
        budget = _Budget(member_cap, total_cap)
        # The plate's new md5 first: the file that carries it may come before the plate.
        digest = hashlib.md5(usedforsecurity=False)
        with archive.open(plate) as fh:
            for chunk in _insert(budget.chunks(fh), scan, block):
                digest.update(chunk)
        sidecar = (plate.filename + ".md5").casefold()
        with zipfile.ZipFile(dst, "w") as out:
            for info in archive.infolist():
                with archive.open(info) as fh, out.open(_fresh_entry(info), "w",
                                                        force_zip64=True) as to:
                    if info is plate:
                        for chunk in _insert(iter(partial(fh.read, CHUNK_BYTES), b""),
                                             scan, block):
                            to.write(chunk)
                    elif info.filename.casefold() == sidecar:
                        raw = fh.read(_SIDECAR_BYTES + 1)
                        if len(raw) > _SIDECAR_BYTES:
                            raise FileRefused("archive_invalid")
                        old = raw.decode("ascii", "replace")
                        text = old.strip()
                        new = digest.hexdigest()
                        if text and text == text.upper() and not text.isdigit():
                            new = new.upper()
                        to.write((new + old[len(old.rstrip()):]).encode("ascii"))
                    else:
                        for chunk in budget.chunks(fh):
                            to.write(chunk)


def write_prepared_copy(src: Path, dst: Path, kind: str, scan: ClearScan,
                        block: Iterable[str], *, member_cap: int = MEMBER_CAP,
                        total_cap: int = TOTAL_CAP) -> tuple[int, str]:
    """Write `dst`: `src` with the block inserted after its last nozzle-off line.

    Returns the copy's size and md5. Blocking: run it in an executor. In an archive only the
    plate changes, and the md5 file beside it, which keeps its case; every other member
    keeps its bytes, its place and its packing. Whatever goes wrong, no copy is left behind.
    """
    try:
        data = _block_bytes(block, scan.newline)
        try:
            if kind == KIND_GCODE:
                _copy_gcode(src, dst, scan, data)
            else:
                _copy_archive(src, dst, scan, data, member_cap, total_cap)
        except FileRefused:
            raise
        except OSError:
            raise FileRefused("file_unreadable") from None
        except _UNREADABLE:
            raise FileRefused("archive_invalid") from None
        return _md5_of(dst)
    except BaseException:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        raise
