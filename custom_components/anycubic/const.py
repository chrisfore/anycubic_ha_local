"""Integration-wide constants."""
from homeassistant.const import Platform

DOMAIN = "anycubic"
PLATFORMS: list[Platform] = [
    Platform.SENSOR, Platform.BINARY_SENSOR, Platform.CAMERA, Platform.LIGHT, Platform.SWITCH,
    Platform.BUTTON, Platform.NUMBER, Platform.SELECT,
]

DEFAULT_QUERY_INTERVAL = 30  # seconds; heartbeat poll
MANUFACTURER = "AnyCubic"

# Printer modelId -> name. IDs verified across multiple on-printer sources (Rinkhals api.cfg,
# mann1x, TigerTag, 1coderookie). The Kobra 3 / S1 / X generation (20024-20030) speaks the same
# signed LAN handshake + field schema as the validated S1 Max — Kobra X confirmed from user
# diagnostics (issue #8). Only Kobra 2 (2002x) is experimental (older, unsigned handshake).
MODEL_NAMES: dict[str, str] = {
    "20021": "AnyCubic Kobra 2 Pro",
    "20022": "AnyCubic Kobra 2 Plus",
    "20023": "AnyCubic Kobra 2 Max",
    "20024": "AnyCubic Kobra 3",
    "20025": "AnyCubic Kobra S1",
    "20026": "AnyCubic Kobra 3 Max",
    "20027": "AnyCubic Kobra 3 V2",
    "20028": "AnyCubic Kobra 4",
    "20029": "AnyCubic Kobra S1 Max",
    "20030": "AnyCubic Kobra X",
}

# ACE multi-color box model_id (reported inside multiColorBox, not the printer's modelId).
# 40002 is kept as "ACE 2" (the S1 Max bundle name); 40001 is the Kobra 3-era "ACE Pro".
ACE_MODEL_NAMES: dict[str, str] = {
    "40001": "ACE Pro",
    "40002": "ACE 2",
}

# Enclosed printers (KS1 / KS1 Max): chamber temperature, box/chamber fan, and chamber light are
# real hardware only here. On open-frame Kobra models those fields are absent or no-ops.
ENCLOSED_MODELS: frozenset[str] = frozenset({"20025", "20029"})

# Models with a camera at :18088 (built-in on enclosed and on the Kobra 4 / X, add-on on the
# Kobra 3 family); Kobra 2 has no camera. Kobra 4 (issue #6) and Kobra X (issue #8) both confirmed
# from user diagnostics — peripherie camera=1 plus a stream URL in the info report. The
# new-generation models serve a tokenized path (/live/<token>) instead of /flv; camera.py follows
# whatever URL the printer reports, so no per-model handling is needed here.
CAMERA_MODELS: frozenset[str] = frozenset(
    {"20024", "20025", "20026", "20027", "20028", "20029", "20030"})

# Printers whose multi-material changer is built into the toolhead (AnyCubic's "ACE Gen 2" on the
# Kobra X) rather than an attached box. They report it as box id -1 with head_tools_model 1, where
# an external ACE reports id 0 — hardware-confirmed on a Kobra X (issue #8) against a Kobra 4 with
# an external ACE 2. External expansion boxes still report 0+ and get their own devices.
BUILTIN_ACE_MODELS: frozenset[str] = frozenset({"20030"})

ACE_SLOT_COUNT = 4


def primary_ace_box_id(model_id: str) -> int:
    """Box id of the printer's own multi-material unit — the one registered at setup.

    Pre-registering the wrong id creates a second, permanently-dead box device next to
    the real one (issue #8), so this must match what the printer actually reports.
    """
    return -1 if model_id in BUILTIN_ACE_MODELS else 0


def ace_suffix(box_id: int) -> str:
    """Device-identifier / unique-id suffix for an ACE box ("ace0", "ace1", ...)."""
    return f"ace{box_id}"

# Defaults used when the drying switch is turned on (the values the AnyCubic app sent live).
ACE_DRYING_DEFAULT_TEMP = 45        # °C
ACE_DRYING_DEFAULT_DURATION_MIN = 240
