from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic.const import DOMAIN
from custom_components.anycubic.diagnostics import async_get_config_entry_diagnostics
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult

HS = HandshakeResult("1.2.3.4", 9883, "u", "secretpw", "DEV", "20029", "SER-1",
                     model_name="Anycubic Kobra S1 Max", device_type="fdm")


class FakeTransport:
    def __init__(self, hs, on_report, **k): pass
    def connect(self): pass
    def disconnect(self): pass
    def query(self, t): pass
    def publish(self, t, p): pass


async def test_diagnostics_redacts_identifiers(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "10.0.0.5"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("info", {"model": "Kobra S1 Max", "ip": "10.0.0.5", "state": "free",
                              "temp": {"curr_chamber_temp": 36},
                              "features": {"camera_timelapse_support": True, "fod_support": True},
                              "urls": {"rtspUrl": "http://10.0.0.5:18088/flv"},
                              "project": {"filename": "JaneDoe_secret_part.gcode.3mf"}})
        coord._apply("peripherie", {"camera": 1, "multiColorBox": 1, "udisk": 0})
        await hass.async_block_till_done()

        diag = await async_get_config_entry_diagnostics(hass, entry)

    # Non-secret diagnostic context survives.
    assert diag["model_id"] == "20029"
    assert diag["update_success"] is True
    # The capability block carries everything needed to add a new model — and nothing sensitive.
    caps = diag["capabilities"]
    assert caps["model_id"] == "20029"
    assert caps["model_name"] == "Anycubic Kobra S1 Max"
    assert caps["device_type"] == "fdm"
    assert caps["has_chamber_temp"] is True
    assert caps["features"] == {"camera_timelapse_support": True, "fod_support": True}
    assert caps["peripherie"] == {"camera": 1, "multiColorBox": 1, "udisk": 0}
    assert {"info", "peripherie"} <= set(caps["report_types_seen"])
    # Addresses and identifiers are redacted everywhere they appear.
    assert diag["host"] == "**REDACTED**"
    assert diag["entry_data"]["host"] == "**REDACTED**"
    assert diag["printer"]["ip"] == "**REDACTED**"
    # And no raw secret/identifier value leaks anywhere in the blob.
    blob = str(diag)
    for secret in ("10.0.0.5", "secretpw", "SER-1", "DEV", "JaneDoe"):
        assert secret not in blob


async def test_diagnostics_includes_raw_multicolorbox_report(hass):
    """The last raw multiColorBox payload is captured verbatim so protocol issues
    (unknown fields, model-specific key differences) can be triaged from a
    diagnostics attachment alone — including keys our parser doesn't know about."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "10.0.0.5"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        raw = {"multi_color_box": [{
            "id": 0, "status": 1, "model_id": 40001, "auto_feed": 0, "loaded_slot": 1,
            "slots": [{"index": 0, "sku": "AHHSGY-106", "type": "PLA High Speed",
                       "color": [117, 120, 123], "status": 5, "consumables_percent": 0,
                       "some_future_field": 87}],
        }]}
        coord._apply("multiColorBox", raw)
        await hass.async_block_till_done()

        diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["raw_multicolorbox"] == raw
    # Unknown/unparsed wire keys survive verbatim — that's the whole point.
    assert diag["raw_multicolorbox"]["multi_color_box"][0]["slots"][0]["some_future_field"] == 87


async def test_diagnostics_camera_url_keeps_shape_hides_host(hass):
    """camera_url must show the protocol shape (scheme/port/path) with only the host
    redacted. Full-value redaction erased the one datum needed to debug a camera
    that won't stream on an unvalidated model (issue #6, Kobra 4 "no feed")."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "10.0.0.5"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("info", {"model": "Kobra S1 Max", "state": "free",
                              "urls": {"rtspUrl": "http://10.0.0.5:18088/flv"}})
        await hass.async_block_till_done()
        diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["printer"]["camera_url"] == "http://**REDACTED**:18088/flv"
    assert "10.0.0.5" not in str(diag)


async def test_diagnostics_camera_url_absent_stays_none(hass):
    """No info report yet -> camera_url is None and the masking must not blow up."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "10.0.0.5"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["printer"]["camera_url"] is None


async def test_diagnostics_hides_identifiers_under_keys_nobody_has_seen(hass):
    """features, peripherie and raw_multicolorbox go out verbatim on purpose, so a firmware
    key the parser does not know can be triaged from the attachment. That also meant a new
    key carrying a URL or an id went straight out. Every identifier here is made up."""
    import json

    device = "0123456789abcdef0123456789abcdef"
    serial = "SERIAL-TEST-0001"
    token = "feedfacefeedfacefeedfacefeedface"
    hs = HandshakeResult("192.168.1.50", 9883, "u", "secretpw", device, "20029", serial,
                         mac="AA-BB-CC-DD-EE-FF", model_name="Anycubic Kobra S1 Max",
                         device_type="fdm")
    # The user typed a name, so the entered address and the broker address differ.
    entry = MockConfigEntry(domain=DOMAIN, unique_id=serial, data={"host": "kobra-s1.local"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=hs), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("info", {"state": "free", "version": "2.7.1.4", "features": {
            "fod_support": True, "bound_serial": serial,
            "cloud_bind": f"https://cloud.example.com/bind?device={device}&s={token}"}})
        coord._apply("peripherie", {"camera": 1, "multiColorBox": 1, "udisk": 0,
                                    "camera_host": "kobra-s1.local"})
        coord._apply("multiColorBox", {
            "owner_device": device, "upload": f"http://192.168.1.50:18910/gcode_upload?s={token}",
            "multi_color_box": [{"id": 0, "temp": 30, "slots": [{
                "index": 0, "sku": "AHPEFG-102", "type": "PETG", "color": [67, 82, 59],
                "status": 5, "consumables_percent": 95, "reader_mac": "aa:bb:cc:dd:ee:ff"}]}]})
        await hass.async_block_till_done()

        diag = await async_get_config_entry_diagnostics(hass, entry)

    blob = json.dumps(diag).lower()
    for secret in (device, serial, "aa:bb:cc:dd:ee:ff", "aa-bb-cc-dd-ee-ff", "192.168.1.50",
                   "kobra-s1.local", token, "secretpw"):
        assert secret.lower() not in blob, secret
    # What the attachment is for is still there, unknown keys included.
    assert diag["capabilities"]["firmware"] == "2.7.1.4"
    assert diag["capabilities"]["features"]["fod_support"] is True
    assert diag["capabilities"]["peripherie"]["camera"] == 1
    slot = diag["raw_multicolorbox"]["multi_color_box"][0]["slots"][0]
    assert (slot["sku"], slot["type"], slot["color"], slot["status"],
            slot["consumables_percent"]) == ("AHPEFG-102", "PETG", [67, 82, 59], 5, 95)
    assert diag["raw_multicolorbox"]["upload"] == \
        "http://**REDACTED**:18910/gcode_upload?**REDACTED**"


async def test_diagnostics_hides_the_running_job_under_keys_nobody_has_seen(hass):
    """The verbatim blocks again: a firmware that names the job, or an object on the plate,
    under a new key. The printer state in the same download says what is printing, so the
    name is recognised by its value wherever it is. Every name here is made up."""
    import json

    job = "0907-2001-Alice desk bracket _plate(01)_PLA_0.2_1h12m.gcode.3mf"
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "192.168.1.50"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("info", {"state": "busy", "version": "2.7.1.4",
                              "project": {"filename": job, "progress": 42}})
        coord._apply("peripherie", {"camera": 1, "last_job": "Alice desk bracket, plate 1"})
        coord._apply("multiColorBox", {
            "feeding_for": "Alice_desk_bracket_.stl_id_0_copy_0",
            "multi_color_box": [{"id": 0, "temp": 30, "slots": [{
                "index": 0, "sku": "AHPEFG-102", "type": "PLA", "color": [67, 82, 59],
                "status": 5, "consumables_percent": 95}]}]})
        await hass.async_block_till_done()

        diag = await async_get_config_entry_diagnostics(hass, entry)

    blob = json.dumps(diag).lower()
    for part in ("alice", "bracket", "0907-2001"):
        assert part not in blob, part
    assert diag["printer"]["progress"] == 42
    assert diag["capabilities"]["peripherie"]["camera"] == 1
    assert diag["raw_multicolorbox"]["multi_color_box"][0]["slots"][0]["type"] == "PLA"
