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
# mann1x, TigerTag, 1coderookie). The Kobra 3 / S1 generation (20024-20029) speaks the same
# signed LAN handshake + field schema as the validated S1 Max. Kobra 2 (2002x) and Kobra X (20030)
# are experimental (different/older handshake) — named here, but their handshake needs validation.
MODEL_NAMES: dict[str, str] = {
    "20021": "AnyCubic Kobra 2 Pro",
    "20022": "AnyCubic Kobra 2 Plus",
    "20023": "AnyCubic Kobra 2 Max",
    "20024": "AnyCubic Kobra 3",
    "20025": "AnyCubic Kobra S1",
    "20026": "AnyCubic Kobra 3 Max",
    "20027": "AnyCubic Kobra 3 V2",
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

# Models with an FLV camera at :18088 (built-in on enclosed, add-on on the Kobra 3 family).
# Kobra 2 has no camera; Kobra X uses WebRTC (no local FLV) — both excluded.
CAMERA_MODELS: frozenset[str] = frozenset({"20024", "20025", "20026", "20027", "20029"})

ACE_SUFFIX = "ace0"   # box 0; multi-ACE later
ACE_SLOT_COUNT = 4

# Defaults used when the drying switch is turned on (the values the AnyCubic app sent live).
ACE_DRYING_DEFAULT_TEMP = 45        # °C
ACE_DRYING_DEFAULT_DURATION_MIN = 240
