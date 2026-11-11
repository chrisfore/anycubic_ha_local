"""Printing from Home Assistant: the actions, every guard, the start and its answer, Print again."""
import dataclasses
import hashlib
import io
import json
import logging
import re
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import persistent_notification as pn
from homeassistant.components.file_upload import FileUploadData
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic import (
    actions as actions_mod,
    config_flow as config_flow_mod,
    coordinator as coord_mod,
    diagnostics as diagnostics_mod,
)
from custom_components.anycubic.anycubic_local import upload as upload_mod
from custom_components.anycubic.anycubic_local.const import REDACTED, redacted
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult
from custom_components.anycubic.const import DOMAIN

HS = HandshakeResult("192.168.1.50", 9883, "u", "p", "DEVICE-ID-0001", "20029", "SER-1",
                     model_name="Anycubic Kobra S1 Max")
HS_K3 = HandshakeResult("192.168.1.51", 9883, "u", "p", "DEVICE-ID-0002", "20024", "SER-K3",
                        model_name="Anycubic Kobra 3")
HOST = "printer.example"
# Made up, and shorter than the redactor's usual minimum on purpose.
TOKEN = "Qx9Tk"
TARGET = upload_mod.UploadTarget(url=f"http://{HOST}:18910/gcode_upload?s={TOKEN}",
                                 secrets=(TOKEN,), host=HOST)
PLA_GCODE = ("; HEADER_BLOCK_START\n; filament: 1\n; HEADER_BLOCK_END\nG1 X1\n"
             "; filament_type = PLA\n; printer_model = Anycubic Kobra S1 Max\n")


class Transport:
    """Records every publish. Answers a print start with the next item in `replies`: a code,
    None for no answer, or a list of raw answers. Reports a job when asked for `info` if
    `job_on_query` is set."""

    replies: list = []
    job_on_query = False
    last = None

    def __init__(self, hs, on_report, **kwargs):
        self.published, self.queries = [], []
        self._on_report, self._on_start_reply = on_report, kwargs.get("on_start_reply")
        type(self).last = self

    def connect(self): pass
    def disconnect(self): pass

    def query(self, msg_type):
        self.queries.append(msg_type)
        if msg_type == "info" and type(self).job_on_query:
            self._on_report("info", {"state": "busy", "project": {"state": "preheating"}})

    def publish(self, topic, payload):
        body = json.loads(payload)
        self.published.append((topic, body))
        if body.get("action") == "start" and type(self).replies:
            reply = type(self).replies.pop(0)
            if isinstance(reply, list):
                for envelope in reply:
                    self._on_start_reply(envelope)
            elif reply is not None:
                self._on_start_reply({"action": "start", "code": reply,
                                      "state": "printing" if reply == 200 else "failed",
                                      "msg": "the printer's own words"})

    def starts(self):
        return [(topic, body) for topic, body in self.published if body.get("action") == "start"]


@pytest.fixture(autouse=True)
def quick_start(monkeypatch):
    monkeypatch.setattr(coord_mod, "START_REPLY_TIMEOUT", 0.2)
    monkeypatch.setattr(coord_mod, "START_WATCH_TIMEOUT", 0.3)
    monkeypatch.setattr(coord_mod, "START_WATCH_QUERY_EVERY", 0.05)
    Transport.replies, Transport.job_on_query = [200], False


async def _setup(hass, hs=HS, options=None, entry_id=None):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=hs.serial, data={"host": HOST},
                            options=options or {}, **({"entry_id": entry_id} if entry_id else {}))
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=hs), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", Transport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _boxes(*slots, box_id=0, drying=0):
    return {"id": box_id, "drying_status": {"status": drying},
            "slots": [{"index": i, "type": t, "color": c, "status": 5}
                      for i, (t, c) in enumerate(slots)]}


def _ready(coord, *boxes):
    """Idle, reporting, a unit with PLA in slot 1 and PETG in slot 2, and the bed clear."""
    coord._apply("info", {"state": "free", "project": None})
    boxes = boxes or (_boxes(("PLA", [255, 0, 0]), ("PETG", [0, 0, 0])),)
    coord._apply("multiColorBox", {"multi_color_box": list(boxes)})
    coord.async_mark_bed_clear()
    assert not coord.bed_needs_clearing


@pytest.fixture
async def env(hass, tmp_path):
    entry = await _setup(hass)
    coord = entry.runtime_data
    _ready(coord)
    uploads = FileUploadData(tmp_path, {})
    hass.data["file_upload"] = uploads
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "SER-1")})
    fetch, send = AsyncMock(return_value=TARGET), AsyncMock()
    with patch.object(upload_mod, "async_fetch_target", fetch), \
         patch.object(upload_mod, "async_send_file", send):
        yield SimpleNamespace(hass=hass, entry=entry, coord=coord, tmp=tmp_path,
                              uploads=uploads, fetch=fetch, send=send, device=device.id)


def _stage(env, name, content) -> str:
    """Put a file where Home Assistant keeps an upload, as its upload view would."""
    file_id = uuid.uuid4().hex
    folder = env.tmp / file_id
    folder.mkdir()
    (folder / name).write_bytes(content.encode() if isinstance(content, str) else content)
    env.uploads.files[file_id] = name
    return file_id


def _gone(env, file_id) -> bool:
    return not (env.tmp / file_id).exists() and file_id not in env.uploads.files


async def _print_file(env, file_id, **kwargs):
    await env.hass.services.async_call(DOMAIN, "print_file",
                                       {"printer": env.device, "file": file_id},
                                       blocking=True, **kwargs)


async def _print_again(env, **kwargs):
    await env.hass.services.async_call(DOMAIN, "print_again", {"printer": env.device},
                                       blocking=True, **kwargs)


def _nothing_sent(env):
    env.fetch.assert_not_awaited()
    env.send.assert_not_awaited()
    assert Transport.last.starts() == []


def _zip(members) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


# ------------------------------------------------- the whole way through


async def test_a_file_is_checked_uploaded_started_and_recorded(env):
    file_id = _stage(env, "Desk bracket.gcode", PLA_GCODE)
    await _print_file(env, file_id)

    env.fetch.assert_awaited_once()
    assert env.fetch.await_args.args[1] == HOST            # asked of the entered address
    env.send.assert_awaited_once()
    session, target, path, name, size = env.send.await_args.args
    assert target is TARGET and name == "Desk bracket.gcode" and size == len(PLA_GCODE)

    (topic, start), = Transport.last.starts()
    assert topic == "anycubic/anycubicCloud/v1/web/printer/20029/DEVICE-ID-0001/print"
    data = start["data"]
    assert data["filename"] == "Desk bracket.gcode" and data["filepath"] == "/"
    assert data["filesize"] == len(PLA_GCODE)
    assert data["md5"] == hashlib.md5(PLA_GCODE.encode()).hexdigest()
    assert data["filetype"] == 1
    assert data["taskid"].isdigit() and len(data["taskid"]) == 9
    assert data["task_settings"] == {
        "auto_leveling": 0, "vibration_compensation": 0, "flow_calibration": 0,
        "timelapse": {"status": 0}, "ai_settings": {"status": 0}, "dry_mode": 0}
    assert data["ams_settings"] == {"use_ams": True, "ams_box_mapping": [{
        "paint_index": 0, "ams_index": 0, "paint_color": [255, 0, 0, 255],
        "ams_color": [255, 0, 0, 255], "material_type": "PLA"}]}

    record = env.coord.last_file
    assert (record.name, record.size, record.md5) == (
        "Desk bracket.gcode", len(PLA_GCODE), data["md5"])
    assert record.used_slots == (1,) and record.materials == {1: "PLA"}
    assert record.model_named is True
    assert env.coord.bed_needs_clearing, "the printer took a job: the bed is not clear"
    assert env.coord.last_guard_outcome == "started"
    assert _gone(env, file_id)


async def test_a_3mf_is_sent_under_its_own_name(env):
    archive = _zip([("Metadata/plate_1.gcode", PLA_GCODE),
                    ("Metadata/slice_info.config",
                     '<config><plate><metadata key="printer_model_id" '
                     'value="Anycubic Kobra S1 Max"/><filament id="2" type="PETG" '
                     'color="#00FF00" used_g="3"/></plate></config>')])
    file_id = _stage(env, "Cube.gcode.3mf", archive)
    await _print_file(env, file_id)
    (_, start), = Transport.last.starts()
    assert start["data"]["filename"] == "Cube.gcode.3mf"
    assert start["data"]["md5"] == hashlib.md5(archive).hexdigest()
    assert start["data"]["ams_settings"]["ams_box_mapping"] == [{
        "paint_index": 1, "ams_index": 1, "paint_color": [0, 255, 0, 255],
        "ams_color": [0, 0, 0, 255], "material_type": "PETG"}]


async def test_a_file_that_names_no_filament_maps_to_slot_1(env):
    file_id = _stage(env, "m117.gcode", "; HEADER_BLOCK_START\n; note\n; HEADER_BLOCK_END\nM117 hi\n")
    await _print_file(env, file_id)
    (_, start), = Transport.last.starts()
    assert start["data"]["ams_settings"]["ams_box_mapping"][0]["ams_index"] == 0
    assert start["data"]["ams_settings"]["ams_box_mapping"][0]["material_type"] == "PLA"
    assert env.coord.last_file.model_named is False


async def test_with_no_unit_a_single_filament_file_prints_from_the_external_spool(env):
    env.coord._apply("multiColorBox", {"multi_color_box": []})
    env.coord._apply("extfilbox", {"type": "PLA", "loaded": 1})
    await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    (_, start), = Transport.last.starts()
    assert start["data"]["ams_settings"] == {"use_ams": False, "ams_box_mapping": []}


# ------------------------------------------------- every guard refuses and sends nothing


def _busy(coord):
    coord._apply("info", {"state": "busy", "project": {"state": "printing", "pause": 0}})


def _drying(coord):
    coord._apply("multiColorBox", {"multi_color_box": [
        _boxes(("PLA", [255, 0, 0]), ("PETG", [0, 0, 0]), drying=1)]})


def _no_unit(coord):
    coord._apply("multiColorBox", {"multi_color_box": []})


def _two_units(coord):
    coord._apply("multiColorBox", {"multi_color_box": [
        _boxes(("PLA", [255, 0, 0]), ("PETG", [0, 0, 0])), _boxes(("PLA", [1, 1, 1]), box_id=1)]})


def _unit_slot1_unknown(coord):
    coord._apply("multiColorBox", {"multi_color_box": [
        _boxes(("", [0, 0, 0]), ("PETG", [0, 0, 0]))]})


def _set(attr, value):
    return lambda coord: setattr(coord, attr, value)


def _silent(coord):
    coord._report_at -= 61


ONE_PLATE = [("Metadata/plate_1.gcode", PLA_GCODE)]
REFUSALS = [
    # (key, file name, contents, how the printer is first)
    ("printer_unreachable", "a.gcode", PLA_GCODE, _silent),
    ("printer_busy", "a.gcode", PLA_GCODE, _busy),
    ("printer_busy", "a.gcode", PLA_GCODE,
     # As seen live after a refused start: busy, no job, the last project "failed".
     lambda c: c._apply("info", {"state": "busy", "project": None,
                                 "last_project": {"state": "failed"}})),
    ("printer_busy", "a.gcode", PLA_GCODE, _drying),
    ("bed_not_clear", "a.gcode", PLA_GCODE, lambda c: c._raise_bed_flag("test")),
    ("file_type_not_supported", "a.stl", PLA_GCODE, None),
    ("file_type_not_supported", "a.bgcode", PLA_GCODE, None),
    ("file_name_marked", "a-HA-PushOff.gcode", PLA_GCODE, None),
    ("file_name_invalid", ".gcode", PLA_GCODE, None),
    ("file_empty", "a.gcode", b"", None),
    ("archive_invalid", "a.gcode.3mf", b"not a zip", None),
    ("not_sliced", "a.3mf", _zip([("3D/3dmodel.model", "<m/>")]), None),
    ("multi_plate", "a.3mf", _zip([*ONE_PLATE, ("Metadata/plate_2.gcode", "G1\n")]), None),
    ("archive_invalid", "a.3mf", _zip([*ONE_PLATE, ("METADATA/Plate_1.gcode", "G1\n")]), None),
    ("wrong_printer_model", "a.gcode", "; printer_model = Anycubic Kobra 3\n", None),
    ("slot_material_mismatch", "a.gcode", "; filament_type = PETG\n", None),
    ("slot_material_unknown", "a.gcode", PLA_GCODE, _unit_slot1_unknown),
    ("slot_out_of_range", "a.gcode", "; filament: 5\n; filament_type = PLA;PLA;PLA;PLA;PLA\n",
     None),
    ("filament_unknown", "a.gcode", PLA_GCODE, _set("ace_present", None)),
    ("multi_filament_needs_unit", "a.gcode", "; filament: 1,2\n", _no_unit),
    ("spool_material_unknown", "a.gcode", PLA_GCODE, _no_unit),
    ("spool_material_mismatch", "a.gcode", PLA_GCODE,
     lambda c: (_no_unit(c), c._apply("extfilbox", {"type": "PETG"}))),
    ("second_unit_single_only", "a.gcode", "; filament: 1,2\n; filament_type = PLA;PETG\n",
     _two_units),
    # A count that is not a finite number says nothing, so every listed slot is checked,
    # and slot 2 holds PETG where the file wants PLA.
    ("slot_material_mismatch", "a.gcode",
     "; filament used [g] = nan, inf\n; filament_type = PLA;PLA\n", None),
]


@pytest.mark.parametrize(("key", "name", "contents", "prepare"), REFUSALS,
                         ids=[f"{r[0]}-{i}" for i, r in enumerate(REFUSALS)])
async def test_every_guard_refuses_with_one_line_and_sends_nothing(env, key, name, contents,
                                                                   prepare):
    if prepare is not None:
        prepare(env.coord)
    file_id = _stage(env, name, contents)
    with pytest.raises(ServiceValidationError) as err:
        await _print_file(env, file_id)
    assert err.value.translation_key == key
    _nothing_sent(env)
    assert env.coord.last_file is None
    assert env.coord.last_guard_outcome == key
    assert _gone(env, file_id), "the uploaded file was left behind"


async def test_a_second_submission_while_one_is_running_is_refused(env):
    file_id = _stage(env, "a.gcode", PLA_GCODE)
    async with env.coord.submit_lock:
        with pytest.raises(ServiceValidationError) as err:
            await _print_file(env, file_id)
    assert err.value.translation_key == "submit_in_progress"
    _nothing_sent(env)
    assert _gone(env, file_id)


async def test_an_unknown_or_used_file_id_is_refused(env):
    with pytest.raises(ServiceValidationError) as err:
        await _print_file(env, uuid.uuid4().hex)
    assert err.value.translation_key == "file_missing"
    _nothing_sent(env)


async def test_a_file_over_the_cap_is_refused_before_it_is_read(env, monkeypatch):
    monkeypatch.setattr(actions_mod, "MAX_FILE_BYTES", 10)
    read = AsyncMock()
    monkeypatch.setattr(actions_mod, "inspect_file", read)
    with pytest.raises(ServiceValidationError) as err:
        await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    assert err.value.translation_key == "file_too_large"
    read.assert_not_called()
    _nothing_sent(env)


async def test_a_multi_color_unit_is_not_a_printer(env):
    env.coord._apply("multiColorBox", {"multi_color_box": [_boxes(("PLA", [1, 2, 3]))]})
    await env.hass.async_block_till_done()
    unit = dr.async_get(env.hass).async_get_device(identifiers={(DOMAIN, "SER-1_ace0")})
    file_id = _stage(env, "a.gcode", PLA_GCODE)
    for device_id in (unit.id, "no-such-device"):
        with pytest.raises(ServiceValidationError) as err:
            await env.hass.services.async_call(DOMAIN, "print_file",
                                               {"printer": device_id, "file": file_id},
                                               blocking=True)
        assert err.value.translation_key == "unknown_printer"
    _nothing_sent(env)


async def test_with_one_printer_the_printer_can_be_left_out(env):
    Transport.replies = [200, 200]
    file_id = _stage(env, "a.gcode", PLA_GCODE)
    await env.hass.services.async_call(DOMAIN, "print_file", {"file": file_id}, blocking=True)
    assert len(Transport.last.starts()) == 1
    assert _gone(env, file_id)
    env.coord._apply("info", {"state": "free", "project": None})
    env.coord.async_mark_bed_clear()
    await env.hass.services.async_call(DOMAIN, "print_again", {}, blocking=True)
    assert len(Transport.last.starts()) == 2


async def _refused_unnamed(env, key, file_id=None):
    for service, data in (("print_file", {"file": file_id or _stage(env, "a.gcode", PLA_GCODE)}),
                          ("print_again", {})):
        with pytest.raises(ServiceValidationError) as err:
            await env.hass.services.async_call(DOMAIN, service, data, blocking=True)
        assert err.value.translation_key == key


async def test_the_one_printer_not_loaded_is_not_connected_and_none_must_be_chosen(env):
    s1 = Transport.last
    assert await env.hass.config_entries.async_unload(env.entry.entry_id)
    file_id = _stage(env, "a.gcode", PLA_GCODE)
    await _refused_unnamed(env, "printer_not_connected", file_id)
    assert _gone(env, file_id), "the uploaded file was left behind"
    # With no printer set up at all, there is none to use.
    assert await env.hass.config_entries.async_remove(env.entry.entry_id)
    await _refused_unnamed(env, "printer_not_chosen")
    env.fetch.assert_not_awaited()
    assert s1.starts() == []


async def test_an_offline_second_printer_still_means_one_must_be_chosen(env):
    """A print meant for B, which is retrying its setup, must not start on A."""
    s1 = Transport.last
    other = MockConfigEntry(domain=DOMAIN, unique_id="SER-B", data={"host": "printer-b.example"})
    other.add_to_hass(env.hass)
    with patch("custom_components.anycubic.do_handshake", side_effect=OSError("refused")):
        await env.hass.config_entries.async_setup(other.entry_id)
        await env.hass.async_block_till_done()
        assert other.state is ConfigEntryState.SETUP_RETRY
        await _refused_unnamed(env, "printer_not_chosen")
        assert await env.hass.config_entries.async_unload(other.entry_id)
    env.fetch.assert_not_awaited()
    assert s1.starts() == []


async def test_a_disabled_printer_does_not_count(env):
    MockConfigEntry(domain=DOMAIN, unique_id="SER-B", data={"host": "printer-b.example"},
                    disabled_by=ConfigEntryDisabler.USER).add_to_hass(env.hass)
    await env.hass.services.async_call(DOMAIN, "print_file",
                                       {"file": _stage(env, "a.gcode", PLA_GCODE)}, blocking=True)
    assert len(Transport.last.starts()) == 1


async def test_a_chosen_printer_that_is_not_loaded_is_not_connected(env):
    env.coord._apply("multiColorBox", {"multi_color_box": [_boxes(("PLA", [1, 2, 3]))]})
    await env.hass.async_block_till_done()
    unit = dr.async_get(env.hass).async_get_device(identifiers={(DOMAIN, "SER-1_ace0")})
    s1 = Transport.last
    assert await env.hass.config_entries.async_unload(env.entry.entry_id)
    for device_id, key in ((env.device, "printer_not_connected"), (unit.id, "unknown_printer")):
        file_id = _stage(env, "a.gcode", PLA_GCODE)
        with pytest.raises(ServiceValidationError) as err:
            await _print_file(SimpleNamespace(hass=env.hass, device=device_id), file_id)
        assert err.value.translation_key == key
        assert _gone(env, file_id)
        with pytest.raises(ServiceValidationError) as err:
            await _print_again(SimpleNamespace(hass=env.hass, device=device_id))
        assert err.value.translation_key == key
    env.fetch.assert_not_awaited()
    assert s1.starts() == []


async def test_with_several_printers_one_must_be_chosen(env):
    s1 = Transport.last
    await _setup(env.hass, HS_K3)
    file_id = _stage(env, "a.gcode", PLA_GCODE)
    for service, data in (("print_file", {"file": file_id}), ("print_again", {})):
        with pytest.raises(ServiceValidationError) as err:
            await env.hass.services.async_call(DOMAIN, service, data, blocking=True)
        assert err.value.translation_key == "printer_not_chosen"
    assert _gone(env, file_id)
    env.fetch.assert_not_awaited()
    assert s1.starts() == [] and Transport.last.starts() == []
    # Naming one still works.
    await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    assert len(s1.starts()) == 1


async def test_a_refusal_after_the_upload_starts_nothing(env):
    """Guards 2, 4 and 5 are asked again after the upload, which can take minutes."""
    async def a_job_starts_meanwhile(*args, **kwargs):
        _busy(env.coord)

    env.send.side_effect = a_job_starts_meanwhile
    with pytest.raises(ServiceValidationError) as err:
        await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    assert err.value.translation_key == "printer_busy"
    assert Transport.last.starts() == []
    # The file is on the printer, so it is on record for Print again.
    assert env.coord.last_file is not None


async def test_an_upload_failure_is_one_line_and_starts_nothing(env):
    env.send.side_effect = upload_mod.UploadError("upload_no_space")
    file_id = _stage(env, "a.gcode", PLA_GCODE)
    with pytest.raises(HomeAssistantError) as err:
        await _print_file(env, file_id)
    assert err.value.translation_key == "upload_no_space"
    assert err.value.__cause__ is None
    assert Transport.last.starts() == []
    assert env.coord.last_file is None
    assert _gone(env, file_id)


# ------------------------------------------------- other models


async def test_other_models_get_no_new_entities_and_a_refusal(hass, tmp_path):
    entry = await _setup(hass, HS_K3)
    coord = entry.runtime_data
    _ready(coord)
    names = [s.entity_id for s in hass.states.async_all()]
    assert not [n for n in names if "print_again" in n or "bed_is_clear" in n
                or "bed_needs_clearing" in n]
    assert "button.anycubic_kobra_3_stop" in names            # the rest is as it was
    hass.data["file_upload"] = FileUploadData(tmp_path, {})
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "SER-K3")})
    for service, data in (("print_file", {"printer": device.id, "file": uuid.uuid4().hex}),
                          ("print_again", {"printer": device.id})):
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(DOMAIN, service, data, blocking=True)
        assert err.value.translation_key == "model_not_enabled"
    assert Transport.last.starts() == []


async def test_the_s1_max_has_the_new_entities(env):
    names = {s.entity_id for s in env.hass.states.async_all()}
    assert {"button.anycubic_kobra_s1_max_print_again", "button.anycubic_kobra_s1_max_bed_is_clear",
            "binary_sensor.anycubic_kobra_s1_max_bed_needs_clearing"} <= names


# ------------------------------------------------- the start and its answer


async def test_a_busy_answer_is_refused_and_never_tried_again(env):
    """Each refused start damages the printer's record of the job that is running."""
    Transport.replies = [10101, 200]          # a second start would have been taken
    with pytest.raises(HomeAssistantError) as err:
        await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    assert err.value.translation_key == "printer_busy"
    await env.hass.async_block_till_done()
    assert len(Transport.last.starts()) == 1
    assert Transport.replies == [200], "a second start was sent"
    assert not env.coord.bed_needs_clearing
    assert env.coord.last_guard_outcome == "printer_busy"


async def test_print_again_on_a_busy_answer_is_not_tried_again_either(env):
    await _sent_once(env)
    Transport.replies = [10101, 200]
    with pytest.raises(HomeAssistantError) as err:
        await _print_again(env)
    assert err.value.translation_key == "printer_busy"
    assert len(Transport.last.starts()) == 1


async def test_another_code_gives_its_catalog_text(env, caplog):
    Transport.replies = [10115]
    with pytest.raises(HomeAssistantError) as err:
        await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    assert err.value.translation_key == "start_refused"
    # Read after a colon, so it starts in lower case.
    assert err.value.translation_placeholders == {"reason": "the device cannot parse the file"}
    assert len(Transport.last.starts()) == 1, "only a busy answer is tried again"
    # A code nobody has catalogued: no number for the user, the number in the debug log.
    caplog.set_level(logging.DEBUG, logger="custom_components.anycubic")
    Transport.replies = [12345]
    with pytest.raises(HomeAssistantError) as err:
        await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    assert err.value.translation_placeholders == {"reason": "no reason given"}
    assert any("12345" in r.getMessage() and r.levelno == logging.DEBUG for r in caplog.records)


def test_a_reason_after_a_colon_keeps_a_name_in_capitals():
    assert coord_mod._after_colon("Filament broken") == "filament broken"
    assert coord_mod._after_colon("ACE Pro response timeout") == "ACE Pro response timeout"
    assert coord_mod._after_colon("X-axis homing failed") == "X-axis homing failed"
    assert coord_mod._after_colon("") == ""


def test_more_than_one_printer_is_refused_in_plain_words():
    import voluptuous as vol

    with pytest.raises(vol.Invalid, match=r"^Choose one printer\.$"):
        actions_mod._one_device(["a", "b"])
    assert actions_mod._one_device(["a"]) == "a"


async def test_no_answer_and_no_job_says_it_may_still_start(env):
    Transport.replies = [None]
    with pytest.raises(HomeAssistantError) as err:
        await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    assert err.value.translation_key == "start_unconfirmed"
    assert len(Transport.last.starts()) == 1, "a start is never sent twice unasked"
    assert "info" in Transport.last.queries
    assert env.coord.last_file is not None, "Print again can try the same file"


async def test_no_answer_but_a_job_seen_is_a_start(env):
    Transport.replies, Transport.job_on_query = [None], True
    await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    assert env.coord.last_guard_outcome == "started"
    assert env.coord.bed_needs_clearing


async def test_an_answer_with_no_code_is_not_an_answer(env):
    Transport.replies = [[{"action": "start", "state": "printing"},
                          {"action": "start", "code": float("nan")},
                          {"action": "start", "code": True},
                          {"action": "start", "code": "200"}]]
    with pytest.raises(HomeAssistantError) as err:
        await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    assert err.value.translation_key == "start_unconfirmed"


# ------------------------------------------------- Print again


async def _sent_once(env):
    await _print_file(env, _stage(env, "Desk bracket.gcode", PLA_GCODE))
    env.coord._apply("info", {"state": "free", "project": None})
    env.coord.async_mark_bed_clear()
    env.fetch.reset_mock()
    env.send.reset_mock()
    Transport.last.published.clear()
    Transport.replies = [200]


async def test_print_again_starts_the_recorded_name_and_never_uploads(env):
    await _sent_once(env)
    first = env.coord.last_file
    await _print_again(env)
    env.fetch.assert_not_awaited()
    env.send.assert_not_awaited()
    (_, start), = Transport.last.starts()
    assert start["data"]["filename"] == first.name
    assert start["data"]["md5"] == first.md5 and start["data"]["filesize"] == first.size
    assert env.coord.bed_needs_clearing


async def test_print_again_runs_the_guards_again(env):
    await _sent_once(env)
    env.coord._raise_bed_flag("test")
    with pytest.raises(ServiceValidationError) as err:
        await _print_again(env)
    assert err.value.translation_key == "bed_not_clear"
    env.coord.async_mark_bed_clear()
    _busy(env.coord)
    with pytest.raises(ServiceValidationError) as err:
        await _print_again(env)
    assert err.value.translation_key == "printer_busy"
    env.coord._apply("info", {"state": "free", "project": None})
    env.coord.async_mark_bed_clear()
    # Slot 1 now holds PETG; the file wants PLA.
    env.coord._apply("multiColorBox", {"multi_color_box": [_boxes(("PETG", [0, 0, 0]))]})
    with pytest.raises(ServiceValidationError) as err:
        await _print_again(env)
    assert err.value.translation_key == "slot_material_mismatch"
    assert Transport.last.starts() == []


async def test_print_again_refuses_a_marked_name_and_an_empty_record(env):
    with pytest.raises(ServiceValidationError) as err:
        await _print_again(env)
    assert err.value.translation_key == "nothing_to_print_again"
    await _sent_once(env)
    env.coord.print_state.last_file = dataclasses.replace(
        env.coord.last_file, name="Desk bracket-ha-pushoff.gcode")
    with pytest.raises(ServiceValidationError) as err:
        await _print_again(env)
    assert err.value.translation_key == "print_again_marked"
    assert Transport.last.starts() == []


async def test_print_again_that_the_printer_refuses_says_to_send_the_file_again(env):
    await _sent_once(env)
    Transport.replies = [10115]
    with pytest.raises(HomeAssistantError) as err:
        await _print_again(env)
    assert err.value.translation_key == "print_again_refused"


async def test_the_print_again_button_follows_the_record_and_the_printer(env):
    entity = "button.anycubic_kobra_s1_max_print_again"
    assert env.hass.states.get(entity).state == "unavailable"     # nothing on record
    await _sent_once(env)
    await env.hass.async_block_till_done()
    assert env.hass.states.get(entity).state != "unavailable"
    await env.hass.services.async_call("button", "press", {"entity_id": entity}, blocking=True)
    assert len(Transport.last.starts()) == 1
    _busy(env.coord)
    await env.hass.async_block_till_done()
    assert env.hass.states.get(entity).state == "unavailable"


# ------------------------------------------------- the start confirmation


def _notes(hass):
    return {key: (note["title"], note["message"])
            for key, note in pn._async_get_or_create_notifications(hass).items()}


async def test_a_confirmed_start_is_said_in_one_notification_per_printer(env):
    note_id = f"{DOMAIN}_print_started_{env.entry.entry_id}"
    await _sent_once(env)
    assert _notes(env.hass) == {
        note_id: ("Anycubic Kobra S1 Max", "Started printing Desk bracket.gcode")}
    # Print again replaces it, under the name the user gave the printer.
    dr.async_get(env.hass).async_update_device(env.device, name_by_user="Garage printer")
    env.coord.print_state.last_file = dataclasses.replace(
        env.coord.last_file, name=".3mf_temp/_Desk_ bracket.gcode")
    await _print_again(env)
    assert _notes(env.hass) == {
        note_id: ("Garage printer", "Started printing \\_Desk\\_ bracket.gcode")}
    # And so does the Print again button.
    pn.async_dismiss(env.hass, note_id)
    env.coord._apply("info", {"state": "free", "project": None})
    env.coord.async_mark_bed_clear()
    Transport.replies = [200]
    await env.hass.async_block_till_done()
    await env.hass.services.async_call(
        "button", "press", {"entity_id": "button.anycubic_kobra_s1_max_print_again"},
        blocking=True)
    assert len(Transport.last.starts()) == 2
    assert _notes(env.hass) == {
        note_id: ("Garage printer", "Started printing \\_Desk\\_ bracket.gcode")}


async def test_a_start_the_printer_did_not_confirm_is_not_said(env):
    env.coord._raise_bed_flag("test")                        # a guard refuses
    with pytest.raises(ServiceValidationError):
        await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    env.coord.async_mark_bed_clear()
    for reply in (10101, 10115, None):                       # busy, refused, no answer
        Transport.replies = [reply]
        with pytest.raises(HomeAssistantError):
            await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    # Taken, but not in the words of a running job.
    Transport.replies = [[{"action": "start", "code": 200, "state": "failed"}]]
    await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    env.coord._apply("info", {"state": "free", "project": None})
    env.coord.async_mark_bed_clear()
    # No answer, and a job seen: it started, but the printer did not say so.
    Transport.replies, Transport.job_on_query = [None], True
    await _print_file(env, _stage(env, "a.gcode", PLA_GCODE))
    assert env.coord.last_guard_outcome == "started"
    assert len(Transport.last.starts()) == 5
    assert _notes(env.hass) == {}


# ------------------------------------------------- access and redaction


async def test_a_non_admin_is_refused_both_actions(env, hass_read_only_user, hass_admin_user):
    file_id = _stage(env, "a.gcode", PLA_GCODE)
    for call in (lambda c: _print_file(env, file_id, context=c),
                 lambda c: _print_again(env, context=c)):
        with pytest.raises(Unauthorized):
            await call(Context(user_id=hass_read_only_user.id))
    _nothing_sent(env)
    await _print_file(env, file_id, context=Context(user_id=hass_admin_user.id))
    assert len(Transport.last.starts()) == 1


async def test_print_again_and_bed_is_clear_are_for_admins_only(
        env, hass_read_only_user, hass_admin_user):
    again, clear = ("button.anycubic_kobra_s1_max_print_again",
                    "button.anycubic_kobra_s1_max_bed_is_clear")

    async def press(entity_id, user=None):
        context = Context(user_id=user.id) if user is not None else None
        await env.hass.services.async_call("button", "press", {"entity_id": entity_id},
                                           blocking=True, context=context)

    await _sent_once(env)
    env.coord._raise_bed_flag("test")
    await env.hass.async_block_till_done()
    for entity_id in (again, clear):
        with pytest.raises(Unauthorized):
            await press(entity_id, hass_read_only_user)
    assert env.coord.bed_needs_clearing and Transport.last.starts() == []
    await press(clear, hass_admin_user)               # an admin may
    assert not env.coord.bed_needs_clearing
    env.coord._raise_bed_flag("test")
    await env.hass.async_block_till_done()
    await press(clear)                                # so may an automation, with no user
    assert not env.coord.bed_needs_clearing
    await env.hass.async_block_till_done()
    await press(again, hass_admin_user)
    assert len(Transport.last.starts()) == 1


def test_filepath_is_masked_and_its_name_scrubbed_everywhere():
    report = {"filepath": "/useremain/app/gk/gcodes/Alice_Desk_Bracket.gcode",
              "note": "now printing Alice Desk Bracket"}
    safe = redacted(report)
    assert safe["filepath"] == REDACTED
    assert "Alice" not in json.dumps(safe)


async def test_the_file_being_sent_and_the_token_are_scrubbed_while_held(env, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.anycubic")
    seen = {}

    async def while_sending(*args, **kwargs):
        coord = env.coord
        seen["names"] = coord._job_names()
        seen["line"] = redacted({"what": "Desk bracket.gcode is uploading"},
                                coord.redaction_identifiers, coord._job_names())
        seen["error"] = coord._error_text(RuntimeError(
            f"reset by peer at http://{HOST}:18910/gcode_upload?s={TOKEN} (s={TOKEN})"))

    env.send.side_effect = while_sending
    await _print_file(env, _stage(env, "Desk bracket.gcode", PLA_GCODE))
    assert "Desk bracket.gcode" in seen["names"]
    assert "Desk bracket" not in json.dumps(seen["line"])
    assert TOKEN not in seen["error"] and HOST not in seen["error"]
    # Held only while sending.
    assert env.coord._held_secrets == () and env.coord._sending == ()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "publish print_start" in logged
    assert "Desk bracket" not in logged and TOKEN not in logged


async def test_diagnostics_say_how_it_went_and_name_nothing(env):
    from custom_components.anycubic.diagnostics import async_get_config_entry_diagnostics

    await _print_file(env, _stage(env, "Desk bracket.gcode", PLA_GCODE))
    diag = await async_get_config_entry_diagnostics(env.hass, env.entry)
    assert diag["print_from_ha"] == {
        "enabled": True, "bed_needs_clearing": True, "last_guard_outcome": "started",
        "has_last_file": True, "door_sensor_configured": False, "door_reading_age_s": None,
        "clearing": {"state": "off", "reason": None, "armed": False, "press_possible": False,
                     "motor_window_s": 5400.0}}
    blob = json.dumps(diag)
    assert "Desk bracket" not in blob and TOKEN not in blob


# ------------------------------------------------- the door sensor option


async def test_with_the_option_off_the_door_sensor_is_not_offered_and_one_kept_is_inert(
        hass, monkeypatch):
    from custom_components.anycubic import clearing as clearing_mod
    from custom_components.anycubic.diagnostics import async_get_config_entry_diagnostics

    for module in (config_flow_mod, diagnostics_mod, clearing_mod):
        monkeypatch.setattr(module, "DOOR_SENSOR_OPTION_ENABLED", False)

    hass.states.async_set("binary_sensor.printer_door", "off", {"device_class": "door"})
    entry = await _setup(hass, options={"door_sensor": "binary_sensor.printer_door"})
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert "door_sensor" not in flow["data_schema"].schema
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", Transport):
        await hass.config_entries.options.async_configure(flow["flow_id"],
                                                          {"camera_source": "printer"})
        await hass.async_block_till_done()
    assert entry.options["door_sensor"] == "binary_sensor.printer_door"     # kept, unused
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["print_from_ha"]["door_sensor_configured"] is False
    assert diag["print_from_ha"]["door_reading_age_s"] is None
    assert entry.runtime_data.clearing.door_entity is None


async def test_the_door_sensor_is_offered_on_enclosed_printers_only_once_enabled(
        hass, monkeypatch):
    monkeypatch.setattr(config_flow_mod, "DOOR_SENSOR_OPTION_ENABLED", True)
    monkeypatch.setattr(diagnostics_mod, "DOOR_SENSOR_OPTION_ENABLED", True)
    hass.states.async_set("binary_sensor.printer_door", "off", {"device_class": "door"})
    entry = await _setup(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert "door_sensor" in flow["data_schema"].schema
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", Transport):
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"camera_source": "printer", "door_sensor": "binary_sensor.printer_door"})
        await hass.async_block_till_done()
    assert entry.options["door_sensor"] == "binary_sensor.printer_door"
    from custom_components.anycubic.diagnostics import async_get_config_entry_diagnostics
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["print_from_ha"]["door_sensor_configured"] is True
    assert isinstance(diag["print_from_ha"]["door_reading_age_s"], float)
    assert "printer_door" not in json.dumps(diag)

    k3 = await _setup(hass, HS_K3)
    flow = await hass.config_entries.options.async_init(k3.entry_id)
    assert "door_sensor" not in flow["data_schema"].schema


# ------------------------------------------------- strings


def test_every_refusal_and_failure_has_one_line_of_text():
    base = Path(actions_mod.__file__).parent
    strings = json.loads((base / "strings.json").read_text())
    assert strings == json.loads((base / "translations" / "en.json").read_text())
    messages = {k: v["message"] for k, v in strings["exceptions"].items()}
    used = set(upload_mod.UPLOAD_ERRORS.values())
    for source in ("actions.py", "coordinator.py", "anycubic_local/printfile.py",
                   "anycubic_local/upload.py"):
        text = (base / source).read_text()
        used |= set(re.findall(
            r'\b(?:_refusal|_failure|FileRefused|UploadError)\(\s*"([a-z0-9_]+)"', text))
        used |= set(re.findall(r'translation_key\s*=\s*"([a-z0-9_]+)"', text))
        used |= set(re.findall(r'"([a-z0-9_]+)" if again else "([a-z0-9_]+)"', text)[0]
                    if "if again" in text else ())
    # The scan found the keys it should, so an empty result below means something.
    assert {"bed_not_clear", "archive_too_large", "printer_not_answering", "start_unconfirmed",
            "print_again_refused", "start_refused", "clear_while_busy"} <= used
    missing = used - set(messages)
    assert not missing, missing
    for key, message in messages.items():
        assert "\n" not in message and message.endswith((".", "?")), key
    for service in ("print_file", "print_again"):
        assert strings["services"][service]["fields"]["printer"]["name"] == "Printer"
