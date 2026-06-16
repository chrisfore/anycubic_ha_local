"""Typed state models + pure parsers for AnyCubic LAN reports (no HA imports)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .const import PAUSE_PAUSED, STATE_FREE


@dataclass
class Slot:
    index: int
    material: str | None = None      # "" -> None
    color_hex: str | None = None
    sku: str | None = None           # "" -> None
    status: int | None = None
    remaining: int | None = None
    loaded: bool = False


@dataclass
class AceBox:
    id: int
    model_id: int | None = None
    box_status: int | None = None
    auto_feed: int | None = None
    loaded_slot: int | None = None
    feed_current_status: int | None = None
    temp: float | None = None
    humidity: float | None = None
    drying_active: bool | None = None   # None = unknown (report omitted drying_status)
    drying_target: float | None = None
    drying_remaining: int | None = None
    slots: dict[int, Slot] = field(default_factory=dict)


def _rgb_hex(color) -> str | None:
    if not color or len(color) < 3:
        return None
    return "#{:02X}{:02X}{:02X}".format(color[0], color[1], color[2])


def _opt(s: str | None) -> str | None:
    return s if s not in ("", None) else None


def _parse_slot(s: dict) -> Slot:
    return Slot(
        index=s["index"],
        material=_opt(s.get("type")),
        color_hex=_rgb_hex(s.get("color")),
        sku=_opt(s.get("sku")),
        status=s.get("status"),
        remaining=s.get("consumables_percent"),
        loaded=s.get("status") == 5,
    )


def parse_multicolorbox(data: dict) -> list[AceBox]:
    out = []
    for b in data.get("multi_color_box", []):
        has_dry = b.get("drying_status") is not None
        dry = b.get("drying_status") or {}
        feed = b.get("feed_status") or {}
        humidity = b.get("humidity")
        if humidity is None:
            humidity = dry.get("humidity")
        box = AceBox(
            id=b["id"],
            model_id=b.get("model_id"),
            box_status=b.get("status"),
            auto_feed=b.get("auto_feed"),
            loaded_slot=b.get("loaded_slot"),
            feed_current_status=feed.get("current_status"),
            temp=b.get("temp"),
            humidity=humidity,
            drying_active=(dry.get("status") == 1) if has_dry else None,
            drying_target=dry.get("target_temp"),
            drying_remaining=dry.get("remain_time"),
            slots={s["index"]: _parse_slot(s) for s in b.get("slots", [])},
        )
        out.append(box)
    return out


def merge_boxes(prev: list[AceBox], new: list[AceBox]) -> list[AceBox]:
    """Merge new box readings into prev by box id and slot index; never overwrite a known value with None."""
    by_id = {b.id: b for b in prev}
    for nb in new:
        ob = by_id.get(nb.id)
        if ob is None:
            by_id[nb.id] = nb
            continue
        for f in ("model_id", "box_status", "auto_feed", "loaded_slot", "feed_current_status",
                  "temp", "humidity", "drying_target", "drying_remaining"):
            val = getattr(nb, f)
            if val is not None:
                setattr(ob, f, val)
        if nb.drying_active is not None:   # keep last known state if this report omitted drying_status
            ob.drying_active = nb.drying_active
        for idx, slot in nb.slots.items():
            ob.slots[idx] = slot
    return list(by_id.values())


@dataclass
class PrinterState:
    model: str | None = None
    firmware: str | None = None
    ip: str | None = None
    raw_state: str | None = None          # "free" | "busy"
    status: str | None = None             # derived display status
    nozzle_temp: float | None = None
    nozzle_target: float | None = None
    bed_temp: float | None = None
    bed_target: float | None = None
    chamber_temp: float | None = None
    chamber_target: float | None = None
    fan_speed_pct: int | None = None
    aux_fan_speed_pct: int | None = None
    box_fan_level: int | None = None
    print_speed_mode: int | None = None
    progress: int | None = None
    current_layer: int | None = None
    total_layers: int | None = None
    remain_time: int | None = None
    print_time: int | None = None
    filament_used: int | None = None
    filename: str | None = None
    pause_code: int | None = None
    camera_url: str | None = None
    printing: bool = False
    paused: bool = False


def parse_info(data: dict) -> PrinterState:
    """Parse an `info` report `.data` object into PrinterState."""
    temp = data.get("temp") or {}
    proj = data.get("project") or data.get("last_project") or {}
    raw_state = data.get("state")
    proj_state = proj.get("state")
    pause_code = proj.get("pause")
    status = proj_state if raw_state != STATE_FREE and proj_state else (
        "idle" if raw_state == STATE_FREE else raw_state)
    return PrinterState(
        model=data.get("model"),
        firmware=data.get("version"),
        ip=data.get("ip"),
        raw_state=raw_state,
        status=status,
        nozzle_temp=temp.get("curr_nozzle_temp"),
        nozzle_target=temp.get("target_nozzle_temp"),
        bed_temp=temp.get("curr_hotbed_temp"),
        bed_target=temp.get("target_hotbed_temp"),
        chamber_temp=temp.get("curr_chamber_temp"),
        chamber_target=temp.get("target_chamber_temp"),
        fan_speed_pct=data.get("fan_speed_pct"),
        aux_fan_speed_pct=data.get("aux_fan_speed_pct"),
        box_fan_level=data.get("box_fan_level"),
        print_speed_mode=data.get("print_speed_mode"),
        progress=proj.get("progress"),
        current_layer=proj.get("curr_layer"),
        total_layers=proj.get("total_layers"),
        remain_time=proj.get("remain_time"),
        print_time=proj.get("print_time"),
        filament_used=proj.get("supplies_usage"),
        filename=proj.get("filename"),
        pause_code=pause_code,
        camera_url=(data.get("urls") or {}).get("rtspUrl"),
        printing=raw_state != STATE_FREE and proj_state == "printing",
        paused=pause_code == PAUSE_PAUSED,
    )


@dataclass
class LightState:
    on: bool = False
    brightness: int = 0


def parse_light(data: dict) -> LightState:
    lights = data.get("lights") or []
    if not lights:
        return LightState()
    first = lights[0]
    return LightState(on=first.get("status") == 1, brightness=first.get("brightness", 0))
