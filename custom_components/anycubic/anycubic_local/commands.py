"""Build the exact LAN write-commands captured from the printer (see PROTOCOL-VALIDATED.md).

Note: `stop` action is inferred (state machine stopping->stoped, parallel to pause/resume) —
confirm on first real use before exposing the stop button by default.
"""
import uuid

from .const import query_topic

_TASKID = "-1"


def _box(payload: dict, box_id: int = 0) -> dict:
    return {"multi_color_box": [{"id": box_id, **payload}]}


def build(model_id: str, device_id: str, command: str, *, value=None, on=None,
          brightness=None, target_temp=None, duration=None, box_id: int = 0,
          ts=None, msgid=None):
    """Return (topic, payload_dict) for a control command. ts/msgid injected by the caller/transport."""
    if command in ("pause", "resume", "stop"):
        mtype, action, data = "print", command, {"taskid": _TASKID}
    elif command in ("set_nozzle_temp", "set_bed_temp", "set_fan_speed", "set_aux_fan",
                      "set_box_fan", "set_speed_mode"):
        key = {"set_nozzle_temp": "target_nozzle_temp", "set_bed_temp": "target_hotbed_temp",
               "set_fan_speed": "fan_speed_pct", "set_aux_fan": "aux_fan_speed_pct",
               "set_box_fan": "box_fan_level", "set_speed_mode": "print_speed_mode"}[command]
        mtype, action, data = "print", "update", {"taskid": _TASKID, "settings": {key: value}}
    elif command == "light":
        mtype, action = "light", "control"
        # On/off light: when no brightness is given, turn fully on (100). status 0 -> off.
        data = {"type": 2, "status": 1 if on else 0,
                "brightness": (100 if brightness is None else brightness) if on else 0}
    elif command == "auto_feed":
        mtype, action, data = "multiColorBox", "setAutoFeed", _box({"auto_feed": 1 if on else 0}, box_id)
    elif command == "drying_start":
        mtype, action = "multiColorBox", "setDry"
        data = _box({"drying_status": {"status": 1, "target_temp": target_temp, "duration": duration}}, box_id)
    elif command == "drying_stop":
        mtype, action, data = "multiColorBox", "setDry", _box({"drying_status": {"status": 0}}, box_id)
    elif command == "camera_start":
        mtype, action, data = "video", "startCapture", None
    elif command == "camera_stop":
        mtype, action, data = "video", "stopCapture", None
    else:
        raise ValueError(f"unknown command: {command}")

    payload = {"type": mtype, "action": action, "timestamp": ts or 0,
               "msgid": msgid or str(uuid.uuid4()), "data": data}
    return query_topic(model_id, device_id, mtype), payload
