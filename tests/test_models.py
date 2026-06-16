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
