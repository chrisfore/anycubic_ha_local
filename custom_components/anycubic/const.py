"""Integration-wide constants."""
from homeassistant.const import Platform

DOMAIN = "anycubic"
PLATFORMS: list[Platform] = [
    Platform.SENSOR, Platform.BINARY_SENSOR, Platform.CAMERA, Platform.LIGHT, Platform.SWITCH,
    Platform.BUTTON, Platform.NUMBER, Platform.SELECT, Platform.IMAGE,
]

DEFAULT_QUERY_INTERVAL = 30  # seconds; heartbeat poll

# The "Camera source" option: which camera the entry's camera entity shows. The printer's
# own (camera.py) is the default and what every entry had before the option existed. The
# other is the feed of the Rinkhals firmware overlay (rinkhals_camera.py), whose camera app
# takes the camera away from the printer's own service. It is a choice and never a
# fallback: neither camera tries the other when its own does not answer.
CONF_CAMERA_SOURCE = "camera_source"
CAMERA_SOURCE_PRINTER = "printer"
CAMERA_SOURCE_RINKHALS = "rinkhals"
MANUFACTURER = "Anycubic"

# Printer modelId -> name. IDs verified across multiple on-printer sources (Rinkhals api.cfg,
# mann1x, TigerTag, 1coderookie). The Kobra 3 / S1 / X generation (20024-20030) speaks the same
# signed LAN handshake + field schema as the validated S1 Max — Kobra X confirmed from user
# diagnostics (issue #8). Only Kobra 2 (2002x) is experimental (older, unsigned handshake).
MODEL_NAMES: dict[str, str] = {
    "20021": "Anycubic Kobra 2 Pro",
    "20022": "Anycubic Kobra 2 Plus",
    "20023": "Anycubic Kobra 2 Max",
    "20024": "Anycubic Kobra 3",
    "20025": "Anycubic Kobra S1",
    "20026": "Anycubic Kobra 3 Max",
    "20027": "Anycubic Kobra 3 V2",
    "20028": "Anycubic Kobra 4",
    "20029": "Anycubic Kobra S1 Max",
    "20030": "Anycubic Kobra X",
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
# Models with a camera light (light type 3), shown only when peripherie reports a camera.
# Kobra 4 (issue #17), confirmed from a user capture.
CAMERA_LIGHT_MODELS: frozenset[str] = frozenset({"20028"})

# Models with a camera at :18088 (built-in on enclosed and on the Kobra 4 / X, add-on on the
# Kobra 3 family); Kobra 2 has no camera. Kobra 4 (issue #6) and Kobra X (issue #8) both confirmed
# from user diagnostics — peripherie camera=1 plus a stream URL in the info report. The
# new-generation models serve a tokenized path (/live/<token>) instead of /flv; camera.py follows
# whatever URL the printer reports, so no per-model handling is needed here.
CAMERA_MODELS: frozenset[str] = frozenset(
    {"20024", "20025", "20026", "20027", "20028", "20029", "20030"})

# Printers whose multi-material changer is built into the toolhead (Anycubic's "ACE Gen 2" on the
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


def box_has_dryer(box_id: int) -> bool:
    """Whether a multi-material unit has a sealed dry box, or is only a feeder.

    A built-in changer (negative id) is feed-only: the Kobra X owner who reported issue #8
    confirmed the spools "just hang loosely on top of the printer", and it reports a
    constant 0 for humidity and box temperature. Drying controls and dry-box sensors there
    would never do anything. External boxes on the same printer still have their dryer.
    """
    return box_id >= 0


def ace_suffix(box_id: int) -> str:
    """Device-identifier / unique-id suffix for an ACE box ("ace0", "ace1", ...)."""
    return f"ace{box_id}"


# The two below name a unit's device. They live here, and not with the entities that use
# them, because the coordinator renames devices with them too and the entities import the
# coordinator.
def ace_device_name(box_id: int, model_id) -> str:
    """Display name for a multi-material unit's device.

    A negative id is the printer's built-in changer (Kobra X), not a box the user owns —
    naming it "ACE 2" off its reported model_id claims hardware they don't have (issue #8).
    Box 0 keeps the literal "ACE 2" until its model is known, so entity IDs minted at
    registration stay deterministic (ace_2_*). Additional boxes only register after
    they have reported, so they can carry the real model plus a unit number ("ACE Pro #2").
    """
    if box_id < 0:
        return "Multi-color unit"
    model = ACE_MODEL_NAMES.get(str(model_id)) if model_id is not None else None
    if box_id == 0:
        return model or "ACE 2"
    return f"{model or 'ACE'} #{box_id + 1}"


def ace_device_model(box_id: int, model_id) -> str:
    """Hardware model shown on the device page.

    The built-in changer reports the ACE 2 model id because it speaks the same protocol
    (Anycubic ships it as "ACE Gen 2" tech), but it is part of the printer.
    """
    model = ACE_MODEL_NAMES.get(str(model_id)) if model_id is not None else None
    if box_id < 0:
        return f"{model or 'ACE'} (built-in)"
    return model or "ACE 2"


# Defaults used when the drying switch is turned on (the values the Anycubic app sent live).
ACE_DRYING_DEFAULT_TEMP = 45        # °C
ACE_DRYING_DEFAULT_DURATION_MIN = 240
