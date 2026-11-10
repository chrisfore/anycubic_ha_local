"""Build the exact LAN write-commands captured from the printer (see PROTOCOL-VALIDATED.md).

All commands are validated against real hardware, including `stop`: the app sends
{type:print, action:stop, data:{taskid:-1}} and the printer transitions stopping->stoped.
"""
import time
import uuid

from .const import LIGHT_TYPE_CAMERA, LIGHT_TYPE_CHAMBER, query_topic

_TASKID = "-1"


def _box(payload: dict, box_id: int = 0) -> dict:
    return {"multi_color_box": [{"id": box_id, **payload}]}


def build(model_id: str, device_id: str, command: str, *, value=None, on=None,
          brightness=None, target_temp=None, duration=None, box_id: int = 0,
          filename=None, ts=None, msgid=None):
    """Return (topic, payload_dict) for a control command.

    ts/msgid default to a real epoch-ms clock and a fresh uuid; they are parameters only
    so tests can pin them. They used to default to 0 and were documented as "injected by
    the caller/transport" — but nothing ever injected them, so every control command went
    out with timestamp 0 while queries carried a real clock (issue #10).
    """
    if command in ("pause", "resume", "stop"):
        mtype, action, data = "print", command, {"taskid": _TASKID}
    elif command in ("set_nozzle_temp", "set_bed_temp", "set_fan_speed", "set_aux_fan",
                      "set_box_fan", "set_speed_mode"):
        key = {"set_nozzle_temp": "target_nozzle_temp", "set_bed_temp": "target_hotbed_temp",
               "set_fan_speed": "fan_speed_pct", "set_aux_fan": "aux_fan_speed_pct",
               "set_box_fan": "box_fan_level", "set_speed_mode": "print_speed_mode"}[command]
        mtype, action, data = "print", "update", {"taskid": _TASKID, "settings": {key: value}}
    elif command in ("light", "camera_light"):
        mtype, action = "light", "control"
        # On/off light: when no brightness is given, turn fully on (100). status 0 -> off.
        # "camera_light" is the Kobra 4's camera light: the same command for another lamp.
        light_type = LIGHT_TYPE_CAMERA if command == "camera_light" else LIGHT_TYPE_CHAMBER
        data = {"type": light_type, "status": 1 if on else 0,
                "brightness": (100 if brightness is None else brightness) if on else 0}
    elif command == "auto_feed":
        mtype, action, data = "multiColorBox", "setAutoFeed", _box({"auto_feed": 1 if on else 0}, box_id)
    elif command == "drying_start":
        mtype, action = "multiColorBox", "setDry"
        data = _box({"drying_status": {"status": 1, "target_temp": target_temp, "duration": duration}}, box_id)
    elif command == "drying_stop":
        mtype, action, data = "multiColorBox", "setDry", _box({"drying_status": {"status": 0}}, box_id)
    elif command == "file_details":
        # UNVERIFIED SHAPE. The printer never pushes `file` on its own — it only answers the
        # AnyCubic Slicer (issue #13) — so the integration has to ask. No capture of the
        # REQUEST exists, and neither cloud fork ever builds one, so these keys are inferred
        # from the fields the response echoes back: root, filename, plate_index. A wrong guess
        # is ignored the same way a polled `extfilbox` query is, so this is safe to try.
        mtype, action = "file", "fileDetails"
        data = {"root": "local", "filename": filename, "plate_index": 1}
    elif command == "camera_start":
        mtype, action, data = "video", "startCapture", None
    elif command == "camera_stop":
        mtype, action, data = "video", "stopCapture", None
    else:
        raise ValueError(f"unknown command: {command}")

    payload = {"type": mtype, "action": action,
               "timestamp": int(time.time() * 1000) if ts is None else ts,
               "msgid": msgid or str(uuid.uuid4()), "data": data}
    return query_topic(model_id, device_id, mtype), payload


# `filetype` in a print start. 1 is what every start seen so far has sent, for G-code; it is
# sent for archives too until T3 says otherwise (INFERRED).
FILETYPE_GCODE = 1


def build_print_start(model_id: str, device_id: str, *, taskid: str, filename: str,
                      filesize: int, md5: str, ams_settings: dict,
                      filetype: int = FILETYPE_GCODE, ts=None, msgid=None):
    """Return (topic, payload) for print/start: print a file that is already on the printer.

    UNVERIFIED SHAPE (T3). It follows the start this printer has been sent by other clients:
    the printer looks the base name up in its own file folder, so `filepath` is only "/".
    `taskid` is fresh for each start, so the printer's reports can be matched to it.
    `ams_settings` maps the file's filament slots to the multi-color unit's, or says to use
    the external spool. Task settings: auto-leveling, vibration compensation, flow
    calibration, timelapse, the printer's camera detection and drying all off.
    """
    data = {
        "taskid": taskid,
        "filename": filename,
        "filepath": "/",
        "filetype": filetype,
        "filesize": filesize,
        "md5": md5,
        "ams_settings": ams_settings,
        "task_settings": {
            # Off: a start with leveling on hung on the printer, waiting on a 10 C bed preheat.
            "auto_leveling": 0,
            "vibration_compensation": 0,
            "flow_calibration": 0,
            "timelapse": {"status": 0},
            "ai_settings": {"status": 0},
            "dry_mode": 0,
        },
    }
    payload = {"type": "print", "action": "start",
               "timestamp": int(time.time() * 1000) if ts is None else ts,
               "msgid": msgid or str(uuid.uuid4()), "data": data}
    return query_topic(model_id, device_id, "print"), payload
