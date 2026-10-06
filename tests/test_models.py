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


def test_parse_light_reads_the_answer_to_a_control_command():
    # Issue #14: a light `control` is answered with the bare light object carrying the NEW
    # state, not a `lights` list. Read as "no lights", every answer said off, so a light
    # just switched on looked like the command had not taken. `data` verbatim from a
    # Kobra S1 Max capture.
    light = models.parse_light({"type": 2, "status": 1, "brightness": 100})
    assert light.on is True
    assert light.brightness == 100


def test_parse_light_reads_a_control_answer_that_switched_the_light_off():
    # This one was right before the fix, but only because "not understood" and "off" were
    # the same value. It has to stay a real reading now that they are not.
    light = models.parse_light({"type": 2, "status": 0, "brightness": 0})
    assert light is not None
    assert light.on is False
    assert light.brightness == 0


def test_parse_light_still_reads_the_lights_list_when_it_says_off():
    # The answer to a query is the shape this parser always understood. test_parse_light
    # pins it on; this pins it off, so both readings come through the fix untouched.
    light = models.parse_light({"lights": [{"type": 2, "status": 0, "brightness": 0}]})
    assert light.on is False
    assert light.brightness == 0


def test_parse_light_does_not_read_an_unrecognised_payload_as_off():
    # Unknown is not "off" (issue #14), the same rule parse_extfilbox applies to a partial
    # answer. None tells the coordinator to keep the state it already has.
    for payload in ({}, {"lights": None}, {"type": 2}, {"status": 1, "brightness": 100}, []):
        assert models.parse_light(payload) is None, payload


def test_parse_light_does_not_read_a_list_or_object_that_says_nothing_as_off():
    # The same rule, for the shapes that still slipped through: a `lights` list with no
    # light in it, or with one that does not say how it is, and a bare object whose status
    # is null. Each used to read as "off"; a list holding null raised.
    for payload in ({"lights": []}, {"lights": [{}]}, {"lights": [None]},
                    {"lights": [{"type": 2, "brightness": 100}]},
                    {"lights": [{"type": 2, "status": None, "brightness": 0}]},
                    {"lights": ["on"]},
                    {"type": 2, "status": None, "brightness": 0}):
        assert models.parse_light(payload) is None, payload


def test_parse_light_ignores_a_bare_object_for_another_light():
    # A bare object names the lamp it is about. Only the chamber light's own type, the one
    # the command builder sends, may move the chamber light.
    assert models.parse_light({"type": 1, "status": 1, "brightness": 100}) is None


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


def test_display_filename_strips_the_printer_side_directory_prefix():
    # Reported on #12: a job started from the printer's own screen names the file
    # ".3mf_temp/<name>.gcode", while the same job from the Slicer is just "<name>.gcode".
    # Users should see one name, not two spellings of it.
    assert models.display_filename(
        ".3mf_temp/0907-2001-Plant wall clip_plate(01)_PLA_0.28_5m39s.gcode"
    ) == "0907-2001-Plant wall clip_plate(01)_PLA_0.28_5m39s.gcode"
    assert models.display_filename(
        "0907-2001-Plant wall clip_plate(01)_PLA_0.28_5m39s.gcode"
    ) == "0907-2001-Plant wall clip_plate(01)_PLA_0.28_5m39s.gcode"
    assert models.display_filename(None) is None


def test_queried_extfilbox_marks_unknown_fields_rather_than_claiming_empty():
    # The QUERIED answer is partial: same physical spool that pushes
    # loaded=1/status_type=3/current_status=10 comes back as loaded=0 with -1 sentinels
    # (issue #12). Reading that 0 as "no filament" would blank a loaded spool on reload.
    spool = models.parse_extfilbox({"type": "PETG", "color": [117, 120, 123],
                                    "loaded": 0, "status_type": -1, "current_status": -1})
    assert spool.material == "PETG"
    assert spool.loaded is None            # unknown, not False
    assert spool.status_type is None
    assert spool.current_status is None


def test_merge_external_spool_keeps_known_values_over_unknown():
    known = models.ExternalSpool(material="PETG", color_hex="#75787B", loaded=True,
                                 status_type=3, current_status=10)
    partial = models.parse_extfilbox({"type": "PETG", "color": [117, 120, 123],
                                      "loaded": 0, "status_type": -1, "current_status": -1})
    merged = models.merge_external_spool(known, partial)
    assert merged.loaded is True           # a partial answer must not clobber this
    assert merged.current_status == 10
    assert merged.material == "PETG"


def test_merge_external_spool_accepts_a_real_change():
    known = models.ExternalSpool(material="PETG", loaded=True, current_status=10)
    pushed = models.parse_extfilbox({"type": "PLA", "color": [255, 0, 0], "loaded": 0,
                                     "status_type": 3, "current_status": 4})
    merged = models.merge_external_spool(known, pushed)
    assert merged.material == "PLA"
    assert merged.loaded is False          # a real 0 alongside real statuses must land
    assert merged.current_status == 4


def test_parse_file_details_decodes_both_images(load_fixture):
    # Issue #13: the printer answers a fileDetails request with base64 renders of the job.
    images = models.parse_file_details(load_fixture("file_details.json"))
    assert images.thumbnail.startswith(b"\x89PNG")
    assert images.top_view.startswith(b"\x89PNG")
    assert images.filename.endswith("_5m39s.gcode")
    assert images.paint_infos[0]["material_type"] == "PETG"
    assert images.skip_parts == ["Plant_wall_clip.stl_id_0_copy_0"]


def test_parse_file_details_tolerates_missing_images(load_fixture):
    # A firmware that omits a render must give None, not raise — the other image
    # and the metadata are still worth having.
    data = load_fixture("file_details.json")
    del data["file_details"]["png_image"]
    images = models.parse_file_details(data)
    assert images.thumbnail is not None
    assert images.top_view is None


def test_parse_file_details_rejects_an_oversized_blob(load_fixture):
    # The payload comes from a device we do not control; a runaway blob must not be
    # held in memory just because it decoded.
    data = load_fixture("file_details.json")
    data["file_details"]["thumbnail"] = "QUFB" * (models.MAX_IMAGE_BYTES // 2)
    images = models.parse_file_details(data)
    assert images.thumbnail is None
    assert images.top_view is not None      # the sane one still lands


def test_parse_file_details_ignores_a_non_details_file_report(load_fixture):
    # listLocal / deleteLocal / listUdisk share the `file` topic and carry no renders.
    assert models.parse_file_details({"root": "local", "records": [{"name": "a.gcode"}]}) is None
