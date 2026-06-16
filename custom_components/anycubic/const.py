"""Integration-wide constants."""
from homeassistant.const import Platform

DOMAIN = "anycubic"
PLATFORMS: list[Platform] = [
    Platform.SENSOR, Platform.BINARY_SENSOR, Platform.CAMERA, Platform.LIGHT, Platform.SWITCH,
    Platform.BUTTON, Platform.NUMBER, Platform.SELECT,
]

DEFAULT_QUERY_INTERVAL = 30  # seconds; heartbeat poll
MANUFACTURER = "AnyCubic"

MODEL_NAMES: dict[str, str] = {"20029": "AnyCubic Kobra S1 Max"}

ACE_SUFFIX = "ace0"   # box 0; multi-ACE later
ACE_SLOT_COUNT = 4

# Defaults used when the drying switch is turned on (the values the AnyCubic app sent live).
ACE_DRYING_DEFAULT_TEMP = 45        # °C
ACE_DRYING_DEFAULT_DURATION_MIN = 240
