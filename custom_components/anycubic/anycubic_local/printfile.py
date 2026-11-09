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
            budget = _Budget(member_cap, total_cap)
            info_text = ""
            slice_info = next((e for e in entries if _SLICE_INFO.fullmatch(e.filename)), None)
            if slice_info is not None:
                with archive.open(slice_info) as fh:
                    info_text = b"".join(budget.chunks(fh)).decode("utf-8", errors="replace")
            window = _Window()
            with archive.open(plates[0]) as fh:
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
