"""What printing from Home Assistant remembers about each printer, across restarts.

A small JSON file per config entry, with no file contents in it: the last file Home Assistant
sent. Anything missing or malformed in it is read the safe way, as no last file.

The bed flag is not kept. Every load of the entry starts with the bed needing clearing:
whatever the printer did while Home Assistant was not running, nobody saw it, and a stored
time cannot prove otherwise on a host whose clock may be wrong until it syncs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .anycubic_local.printfile import MAX_SLOTS, whole_number
from .const import DOMAIN

STORAGE_VERSION = 1
# A change is written this long after it is made.
SAVE_DELAY = 1.0

_MD5 = re.compile(r"[0-9a-f]{32}")
_COLOR = re.compile(r"#[0-9A-F]{6}")
_LONGEST_TEXT = 255


@dataclass(frozen=True)
class LastFile:
    """The last file Home Assistant sent to the printer: enough to start it again."""

    name: str
    size: int
    md5: str
    filetype: int
    # The filament slots it prints with (from 1), and what it expects in each.
    used_slots: tuple[int, ...] | None = None
    materials: dict[int, str] = field(default_factory=dict)
    colors: dict[int, str] = field(default_factory=dict)
    # Whether the file named this printer (True) or named no printer at all (False).
    model_named: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name, "size": self.size, "md5": self.md5, "filetype": self.filetype,
            "used_slots": None if self.used_slots is None else list(self.used_slots),
            "materials": {str(k): v for k, v in self.materials.items()},
            "colors": {str(k): v for k, v in self.colors.items()},
            "model_named": self.model_named,
        }

    @classmethod
    def from_dict(cls, data) -> LastFile | None:
        """The record as stored, or None if any part of it is not what was written."""
        if not isinstance(data, dict):
            return None
        name, md5 = data.get("name"), data.get("md5")
        size = whole_number(data.get("size"), 1, 1 << 40)
        filetype = whole_number(data.get("filetype"), 0, 1000)
        if (not isinstance(name, str) or not 0 < len(name) <= _LONGEST_TEXT
                or not isinstance(md5, str) or not _MD5.fullmatch(md5)
                or size is None or filetype is None):
            return None
        used = data.get("used_slots")
        if used is not None:
            if not isinstance(used, list) or not used:
                return None
            slots = [whole_number(s, 1, MAX_SLOTS) for s in used]
            if None in slots:
                return None
            used = tuple(slots)
        materials = _slot_map(data.get("materials"), lambda v: 0 < len(v) <= _LONGEST_TEXT)
        colors = _slot_map(data.get("colors"), _COLOR.fullmatch)
        if materials is None or colors is None:
            return None
        return cls(name=name, size=size, md5=md5, filetype=filetype, used_slots=used,
                   materials=materials, colors=colors,
                   model_named=data.get("model_named") is True)


def _slot_map(data, valid) -> dict[int, str] | None:
    if not isinstance(data, dict):
        return None
    out = {}
    for key, value in data.items():
        slot = whole_number(key, 1, MAX_SLOTS)
        if slot is None or not isinstance(value, str) or not valid(value):
            return None
        out[slot] = value
    return out


@dataclass
class PrintState:
    """The state, as it is in memory. The defaults are the safe ones."""

    # Not stored, and on whenever this is made: see the module's docstring.
    bed_flag: bool = True
    last_file: LastFile | None = None

    def as_dict(self) -> dict:
        return {"last_file": None if self.last_file is None else self.last_file.as_dict()}

    @classmethod
    def from_dict(cls, data) -> PrintState:
        """The stored state, with the bed needing clearing whatever was stored before."""
        last_file = data.get("last_file") if isinstance(data, dict) else None
        return cls(last_file=None if last_file is None else LastFile.from_dict(last_file))


class PrintStore:
    """One entry's file under .storage."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.print.{entry_id}",
                                         private=True)

    async def async_load(self) -> PrintState:
        """The stored state. With no file, everything is at its safe default."""
        return PrintState.from_dict(await self._store.async_load())

    @callback
    def async_schedule_save(self, state: PrintState) -> None:
        self._store.async_delay_save(state.as_dict, SAVE_DELAY)

    async def async_save(self, state: PrintState) -> None:
        await self._store.async_save(state.as_dict())

    async def async_remove(self) -> None:
        await self._store.async_remove()
