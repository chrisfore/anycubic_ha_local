"""Topic templates, message types, and status enums (validated — see PROTOCOL-VALIDATED.md)."""

PREFIX = "anycubic/anycubicCloud/v1"

QUERY_TYPES = ["info", "tempature", "fan", "light", "multiColorBox", "print"]
# note: "tempature" is the printer firmware's actual (misspelled) wire string and must NOT be corrected.
# report `action` varies (query/report/refresh/workReport/setInfo) — key off TYPE, never action.

# project.pause int -> human state
PAUSE_STATE = {0: "printing", 1: "paused", 2: "pausing", 3: "resuming", 4: "stopping"}
PAUSE_PAUSED = 1  # project.pause int for the paused state

# top-level info.data.state
STATE_FREE = "free"
STATE_BUSY = "busy"


def query_topic(model_id: str, device_id: str, msg_type: str) -> str:
    return f"{PREFIX}/web/printer/{model_id}/{device_id}/{msg_type}"


def report_prefix(model_id: str, device_id: str) -> str:
    return f"{PREFIX}/printer/public/{model_id}/{device_id}"
