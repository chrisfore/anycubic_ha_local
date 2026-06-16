# tests/test_commands.py
import pytest

from custom_components.anycubic.anycubic_local import commands

M, D = "20029", "devA"
BASE = "anycubic/anycubicCloud/v1/web/printer/20029/devA"


def test_pause_resume_stop():
    t, p = commands.build(M, D, "pause")
    assert t == f"{BASE}/print"
    assert p["type"] == "print" and p["action"] == "pause" and p["data"] == {"taskid": "-1"}
    assert commands.build(M, D, "resume")[1]["action"] == "resume"
    assert commands.build(M, D, "stop")[1]["action"] == "stop"  # inferred; confirm on first use


def test_set_settings():
    t, p = commands.build(M, D, "set_nozzle_temp", value=210)
    assert t == f"{BASE}/print"
    assert p["action"] == "update"
    assert p["data"] == {"taskid": "-1", "settings": {"target_nozzle_temp": 210}}


def test_light():
    t, p = commands.build(M, D, "light", on=True, brightness=100)
    assert t == f"{BASE}/light"
    assert p["action"] == "control"
    assert p["data"] == {"type": 2, "status": 1, "brightness": 100}
    _, p_off = commands.build(M, D, "light", on=False)
    assert p_off["data"] == {"type": 2, "status": 0, "brightness": 0}


def test_ace_drying_and_autofeed():
    _, p = commands.build(M, D, "drying_start", target_temp=45, duration=240)
    assert p["type"] == "multiColorBox" and p["action"] == "setDry"
    assert p["data"] == {"multi_color_box": [{"id": 0, "drying_status": {"status": 1, "target_temp": 45, "duration": 240}}]}
    _, ps = commands.build(M, D, "drying_stop")
    assert ps["data"] == {"multi_color_box": [{"id": 0, "drying_status": {"status": 0}}]}
    _, pa = commands.build(M, D, "auto_feed", on=False)
    assert pa["action"] == "setAutoFeed"
    assert pa["data"] == {"multi_color_box": [{"id": 0, "auto_feed": 0}]}


def test_camera():
    assert commands.build(M, D, "camera_start")[1]["action"] == "startCapture"
    assert commands.build(M, D, "camera_stop")[1]["action"] == "stopCapture"


def test_all_have_required_envelope():
    t, p = commands.build(M, D, "pause")
    assert set(["type", "action", "timestamp", "msgid", "data"]).issubset(p.keys())


def test_unknown_command_raises():
    with pytest.raises(ValueError):
        commands.build(M, D, "bogus")
