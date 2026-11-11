"""The block of G-code that lets a finished print be pushed off the bed.

No Home Assistant here, and nothing is read or written. `clearing_block` returns the lines a
prepared copy of a file gets after its last nozzle-off line, or refuses with a reason:

1. bed and chamber heaters off;
2. lift to the tallest part plus a clearance, park over the purge chute, and turn the
   chamber and auxiliary fans on so the bed and chamber cool while the print waits;
3. `M601`: the printer pauses here, and only a resume carries on;
4. cool the nozzle, with the part fan on, before anything can touch a part;
5. the sweep: for each column, travel at the lifted height to a point behind the parts, come
   down to the sweep height, push straight toward the door, lift;
6. a final raise.

Absolute coordinates only. No homing, no probing, no extrusion and no `M117`: with no
multi-color mapping in the job, a later M117 makes the printer cut, unload and purge at the
height it is at. Every coordinate is checked against the printer's travel before a line is
written, and every height against the ceiling the printer's own end routine sets.
"""
from __future__ import annotations

from dataclasses import dataclass

from .printfile import FileRefused, finite_number


@dataclass(frozen=True)
class CoolTo:
    """Wait in the file until the nozzle has cooled to this temperature (`M109`)."""

    celsius: int


@dataclass(frozen=True)
class Dwell:
    """Wait a fixed time for the nozzle to cool (`G4`), where `M109` does not wait."""

    seconds: float


# The chamber fan (P3) and the auxiliary fan (P2) at 80 % for the wait at the pause, and off
# again after the sweep. They go in the file, before the pause, because the printer takes
# no fan command from outside while it is paused and keeps what the file set. Measured on a
# Kobra S1 Max with the door closed, from a 67 C bed: 28 C after 20 minutes and 25 C after
# 31 with them on, against 60 minutes and never without.
WAIT_FANS_ON = ("M106 P3 S204", "M106 P2 S204")
WAIT_FANS_OFF = ("M106 P2 S0", "M106 P3 S0")

# The longest a timed wait may be.
MAX_DWELL_SECONDS = 3600


@dataclass(frozen=True)
class SweepProfile:
    """One printer model's measurements, and the sweep's settings for it. Millimetres,
    millimetres a minute, degrees Celsius."""

    # How far the head travels.
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    # The highest Z a file may place the bed at: the end routine drops it to here at most.
    z_cap: float
    # What the printer's own pause adds to Z when it parks.
    pause_lift: float
    # The printable area.
    bed_x: tuple[float, float]
    bed_y: tuple[float, float]
    # Over the purge chute, where the head waits while paused.
    park: tuple[float, float]
    # Where the head comes down for a push: behind the parts and behind the purge line.
    descent_y: float
    # How far in front of that point the nearest part may reach, so the head, which is
    # wider than its nozzle, does not come down on it.
    rear_clearance: float
    # Where a push ends: the door side.
    front_y: float
    # The nozzle's height above the bed during a push.
    sweep_height: float
    # The widest gap between two pushes, and how far the pushes reach past the parts.
    column_spacing: float
    side_margin: float
    # Above the tallest part while travelling, and the raise at the very end.
    lift_clearance: float
    final_raise: float
    # A part must be at least this many sweep heights tall, and no taller than this.
    min_height_ratio: float
    max_part_height: float
    push_speed: int
    travel_speed: int
    z_speed: int
    # The nozzle is this cool, or cooler, before the sweep.
    nozzle_cool: int


# The Kobra S1 Max. Travel, the ceiling and the pause's lift are from the printer's own
# configuration and the first live session; the door is on the Y 0 side. The sweep's own
# settings (height, spacing, margins, clearance) start inside what print farms use and are
# fixed after the first live sweeps.
S1_MAX = SweepProfile(
    x_min=-3.0, x_max=358.0, y_min=-2.0, y_max=380.0,
    z_cap=253.0, pause_lift=2.0,
    bed_x=(0.0, 350.0), bed_y=(0.0, 350.0),
    park=(105.0, 352.0),
    descent_y=356.0, rear_clearance=50.0, front_y=-2.0,
    sweep_height=2.0, column_spacing=20.0, side_margin=10.0,
    lift_clearance=10.0, final_raise=10.0,
    min_height_ratio=2.0, max_part_height=200.0,
    push_speed=1800, travel_speed=12000, z_speed=600,
    nozzle_cool=50,
)

_PROFILES = {"20029": S1_MAX}


def profile_for(model_id: str) -> SweepProfile | None:
    """The sweep profile for a printer model, or None if it has none."""
    return _PROFILES.get(model_id)


def _bounds(profile: SweepProfile, footprints) -> tuple[float, float, float]:
    """(lowest x, highest x, highest y) of the outlines, each point checked to be on the bed."""
    xs, ys = [], []
    for outline in footprints:
        if not outline:
            raise FileRefused("clear_part_off_bed")
        for point in outline:
            x, y = (finite_number(v) for v in point)
            if (x is None or y is None
                    or not profile.bed_x[0] <= x <= profile.bed_x[1]
                    or not profile.bed_y[0] <= y <= profile.bed_y[1]):
                raise FileRefused("clear_part_off_bed")
            xs.append(x)
            ys.append(y)
    if not xs:
        raise FileRefused("clear_part_off_bed")
    return min(xs), max(xs), max(ys)


def _columns(profile: SweepProfile, low: float, high: float) -> list[float]:
    """The X of each push: from a margin left of the parts to a margin right of them, kept
    on the bed, evenly spaced and no further apart than the spacing."""
    left = max(profile.bed_x[0], low - profile.side_margin)
    right = min(profile.bed_x[1], high + profile.side_margin)
    gaps = max(1, -int(-(right - left) // profile.column_spacing))
    return [round(left + (right - left) * i / gaps, 2) for i in range(gaps + 1)]


class _Writer:
    """Writes moves, refusing any that leaves the printer's travel or passes the ceiling."""

    def __init__(self, profile: SweepProfile) -> None:
        self._profile = profile
        self.lines: list[str] = []

    def raw(self, line: str) -> None:
        self.lines.append(line)

    def z(self, z: float) -> None:
        profile = self._profile
        if not 0 < z <= profile.z_cap:
            raise FileRefused("clear_out_of_reach")
        self.lines.append(f"G1 Z{z:.2f} F{profile.z_speed}")

    def xy(self, x: float, y: float, speed: int) -> None:
        profile = self._profile
        if not (profile.x_min <= x <= profile.x_max and profile.y_min <= y <= profile.y_max):
            raise FileRefused("clear_out_of_reach")
        self.lines.append(f"G1 X{x:.2f} Y{y:.2f} F{speed}")


def _cooling_lines(profile: SweepProfile, cooling) -> list[str]:
    """The part fan on, each cooling step in turn, the fan off."""
    if cooling is None:
        cooling = CoolTo(profile.nozzle_cool)
    steps = cooling if isinstance(cooling, tuple) else (cooling,)
    if not steps:
        raise FileRefused("clear_out_of_reach")
    lines = ["M106 S255"]
    for step in steps:
        if isinstance(step, CoolTo):
            if type(step.celsius) is not int or not 0 < step.celsius <= profile.nozzle_cool:
                raise FileRefused("clear_out_of_reach")
            # M109 leaves the heater holding that temperature, so it is turned off again.
            lines += [f"M109 S{step.celsius}", "M104 S0"]
        elif isinstance(step, Dwell):
            seconds = None if isinstance(step.seconds, str) else finite_number(step.seconds)
            if seconds is None or not 0 < seconds <= MAX_DWELL_SECONDS:
                raise FileRefused("clear_out_of_reach")
            # In milliseconds: the form every firmware takes. Klipper reads no S on G4.
            lines.append(f"G4 P{int(seconds * 1000)}")
        else:
            raise FileRefused("clear_out_of_reach")
    return [*lines, "M106 S0"]


def _parking_block(profile: SweepProfile, max_z: float) -> list[str]:
    """Lift, park, fans on, pause; then, should the pause ever be resumed, fans off and a
    raise. Nothing in it goes near a part."""
    tallest = finite_number(max_z)
    if tallest is None or tallest <= 0:
        raise FileRefused("clear_no_height")
    lift = round(tallest + profile.lift_clearance, 2)
    if lift + profile.pause_lift > profile.z_cap:
        raise FileRefused("clear_part_too_tall")
    out = _Writer(profile)
    out.raw("; Home Assistant: cool the bed when done.")
    out.raw("M140 S0")
    out.raw("M141 S0")
    out.raw("G90")
    out.z(lift)
    out.xy(*profile.park, profile.travel_speed)
    for line in WAIT_FANS_ON:
        out.raw(line)
    out.raw("M601")
    for line in WAIT_FANS_OFF:
        out.raw(line)
    out.z(min(lift + profile.final_raise, profile.z_cap))
    out.raw("; Home Assistant: end of cool the bed when done.")
    return out.lines


def clearing_block(profile: SweepProfile, max_z: float, footprints, *,
                   cooling: CoolTo | Dwell | tuple[CoolTo | Dwell, ...] | None = None,
                   sweep_height: float | None = None, push: bool = True) -> list[str]:
    """The lines to insert after a file's last nozzle-off line, or a refusal.

    `max_z` is the tallest part and `footprints` each part's outline, as the file gives them.
    `cooling` is how the nozzle is cooled after the resume, one step or several in turn: by
    default a wait for the profile's temperature. `sweep_height` is for a first live test in the air; the product
    never passes it. With `push` off there is no sweep: the block lifts, parks, turns the
    fans on and pauses, and what follows a resume only turns the fans off and raises; only
    the height is needed for that.
    """
    if not push:
        return _parking_block(profile, max_z)
    height = profile.sweep_height if sweep_height is None else finite_number(sweep_height)
    tallest = finite_number(max_z)
    if height is None or height <= 0:
        raise FileRefused("clear_out_of_reach")
    if tallest is None or tallest < profile.min_height_ratio * profile.sweep_height:
        raise FileRefused("clear_part_too_short")
    if tallest > profile.max_part_height:
        raise FileRefused("clear_part_too_tall")
    low, high, back = _bounds(profile, footprints)
    if back > profile.descent_y - profile.rear_clearance:
        raise FileRefused("clear_part_too_far_back")

    lift = round(max(tallest, height) + profile.lift_clearance, 2)
    # The printer's pause lifts a little more from here, and that must fit too.
    if lift + profile.pause_lift > profile.z_cap:
        raise FileRefused("clear_out_of_reach")
    cool = _cooling_lines(profile, cooling)

    out = _Writer(profile)
    out.raw("; Home Assistant: Clear the bed. Do not resume this pause at the printer.")
    out.raw("M140 S0")
    out.raw("M141 S0")
    out.raw("G90")
    out.z(lift)
    out.xy(*profile.park, profile.travel_speed)
    for line in WAIT_FANS_ON:
        out.raw(line)
    out.raw("M601")
    for line in cool:
        out.raw(line)
    out.raw("G90")
    for x in _columns(profile, low, high):
        out.xy(x, profile.descent_y, profile.travel_speed)
        out.z(height)
        out.xy(x, profile.front_y, profile.push_speed)
        out.z(lift)
    for line in WAIT_FANS_OFF:
        out.raw(line)
    out.z(min(lift + profile.final_raise, profile.z_cap))
    out.raw("; Home Assistant: end of Clear the bed.")
    return out.lines
