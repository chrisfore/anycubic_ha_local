# tests/test_models.py
from custom_components.anycubic.anycubic_local import models


def test_parse_info(load_fixture):
    p = models.parse_info(load_fixture("info_report.json"))
    assert p.model == "Anycubic Kobra S1 Max"
    assert p.firmware == "2.6.9.6"
    assert p.nozzle_temp == 45
    assert p.chamber_temp == 43
    assert p.progress == 42
    assert p.current_layer == 120
    assert p.total_layers == 900
    assert p.printing is True
    assert p.paused is False
    assert p.status == "printing"          # project.state surfaced while busy
    assert p.camera_url == "http://192.168.1.50:18088/flv"


def test_parse_multicolorbox_full(load_fixture):
    box = models.parse_multicolorbox(load_fixture("multicolorbox_full.json"))[0]
    assert box.id == 0
    assert box.humidity == 24
    assert box.temp == 35
    assert box.model_id == 40002
    assert box.feed_current_status == -1
    assert box.drying_active is False
    assert box.drying_target == 0   # idle sentinel 0 must be preserved, not coerced to None
    assert box.slots[1].material == "PETG"
    assert box.slots[1].color_hex == "#43523B"
    assert box.slots[1].remaining == 100
    assert box.slots[1].loaded is True


def test_merge_dual_humidity_and_no_none_clobber(load_fixture):
    full = models.parse_multicolorbox(load_fixture("multicolorbox_full.json"))
    slim = models.parse_multicolorbox(load_fixture("multicolorbox_slim.json"))
    merged = models.merge_boxes(full, slim)[0]
    # slim has humidity under drying_status, full under box.humidity -> latest (slim) wins, 30
    assert merged.humidity == 30
    # slim omits temp -> must NOT clobber the known 35
    assert merged.temp == 35
    assert merged.loaded_slot == 1
    assert merged.drying_active is True


def test_parse_light(load_fixture):
    light = models.parse_light(load_fixture("light_report.json"))
    assert light.on is True
    assert light.brightness == 100


def test_apply_temperature_folds_a_tempature_report():
    state = models.PrinterState(nozzle_temp=210, nozzle_target=210, bed_temp=60,
                                chamber_temp=41, progress=42, printing=True)
    models.apply_temperature(state, {"taskid": "", "curr_nozzle_temp": 39,
                                     "target_nozzle_temp": 0, "curr_hotbed_temp": 28,
                                     "target_hotbed_temp": 0})
    assert state.nozzle_temp == 39
    assert state.nozzle_target == 0        # a real setpoint of 0 must land, not read as absent
    assert state.bed_temp == 28
    assert state.chamber_temp == 41        # key omitted -> keep the last known value
    assert state.progress == 42            # job fields are not this report's business
    assert state.printing is True


def test_apply_fan_folds_a_fan_report():
    state = models.PrinterState(fan_speed_pct=100, aux_fan_speed_pct=50, box_fan_level=1,
                                progress=42)
    models.apply_fan(state, {"taskid": "-1", "fan_speed_pct": 0, "box_fan_level": 3})
    assert state.fan_speed_pct == 0
    assert state.box_fan_level == 3
    assert state.aux_fan_speed_pct == 50   # omitted -> unchanged
    assert state.progress == 42


def test_apply_progress_folds_a_print_report():
    state = models.PrinterState(progress=5, current_layer=1, printing=True, status="printing")
    applied = models.apply_progress(state, {
        "taskid": "-1", "progress": 42, "curr_layer": 120, "total_layers": 900,
        "remain_time": 31, "print_time": 610, "supplies_usage": 1200,
        "filename": "plate.gcode.3mf"})
    assert applied is True
    assert state.progress == 42
    assert state.current_layer == 120
    assert state.total_layers == 900
    assert state.remain_time == 31
    assert state.filament_used == 1200
    assert state.filename == "plate.gcode.3mf"
    assert state.printing is True          # lifecycle stays info's job
    assert state.status == "printing"


def test_apply_progress_ignores_the_command_ack_and_settings_shapes():
    # All three share the `print` topic; only the progress shape carries `progress`.
    # Folding an ack would wipe progress/layer/remaining-time on every button press.
    state = models.PrinterState(progress=42, current_layer=120, remain_time=31)
    for ack in ({"taskid": "-1"},
                {"taskid": "-1", "settings": {"target_nozzle_temp": 210}},
                {"taskid": "-1", "localtask": "", "curr_nozzle_temp": 210,
                 "curr_hotbed_temp": 60, "settings": {"fan_speed_pct": 100}}):
        assert models.apply_progress(state, ack) is False
    assert state.progress == 42
    assert state.current_layer == 120
    assert state.remain_time == 31


def test_apply_progress_lands_a_genuine_zero():
    # progress 0 / layer 0 at the start of a job are real values, not "absent".
    state = models.PrinterState(progress=99, current_layer=900)
    assert models.apply_progress(state, {"progress": 0, "curr_layer": 0}) is True
    assert state.progress == 0
    assert state.current_layer == 0


def test_parse_extfilbox_reads_the_external_spool():
    # Issue #12: with the ACE unplugged the printer reports the bare spool on its own
    # topic. Payload verbatim from the reporter's debug log.
    spool = models.parse_extfilbox({"type": "PETG", "color": [117, 120, 123],
                                    "loaded": 1, "status_type": 3, "current_status": 10})
    assert spool.material == "PETG"
    assert spool.color_hex == "#75787B"
    assert spool.loaded is True
    assert spool.status_type == 3
    assert spool.current_status == 10


def test_parse_extfilbox_reports_an_empty_holder():
    # No filament loaded: the printer still reports, with the material blank and loaded 0.
    spool = models.parse_extfilbox({"type": "", "color": [], "loaded": 0,
                                    "status_type": 0, "current_status": 0})
    assert spool.material is None
    assert spool.color_hex is None
    assert spool.loaded is False
