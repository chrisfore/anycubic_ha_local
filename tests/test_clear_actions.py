"""Clear the bed from the outside: the tick box, the clear_bed action, the button, the sensor."""
# ruff: noqa: F811  (the fixtures imported below are named again as test arguments)
import hashlib
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from test_print_actions import (  # noqa: F401  (env and quick_start are fixtures)
    HS,
    HS_K3,
    Transport,
    _boxes,
    _gone,
    _nothing_sent,
    _ready,
    _setup,
    _stage,
    env,
    quick_start,
)

from custom_components.anycubic import (
    actions as actions_mod,
    clearing as clearing_mod,
    diagnostics as diagnostics_mod,
)
from custom_components.anycubic.anycubic_local import sweep, upload as upload_mod
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult
from custom_components.anycubic.const import DOMAIN

CLEAR = "button.anycubic_kobra_s1_max_clear_the_bed"
STATE = "sensor.anycubic_kobra_s1_max_bed_clearing"


def _part(*, material="PLA", slot=1, plate="Textured PEI Plate", extra="", max_z="12.40",
          model="Anycubic Kobra S1 Max"):
    types = ";".join(["PLA"] * (slot - 1) + [material])
    return (f"; HEADER_BLOCK_START\n; filament: {slot}\n; max_z_height: {max_z}\n"
            "; HEADER_BLOCK_END\nM140 S60\nM190 S60\n"
            "EXCLUDE_OBJECT_DEFINE NAME=a CENTER=175,175 "
            "POLYGON=[[170,170],[180,170],[180,180],[170,180]]\n"
            f"G1 Z.2\n;TYPE:Outer wall\nG1 X170 Y170 E.4\n{extra}"
            "M140 S0\nM104 S0\nM141 S0\nM84\n"
            f"; raft_layers = 0\n; curr_bed_type = {plate}\n; filament_type = {types}\n"
            + (f"; printer_model = {model}\n" if model else ""))


async def _print_file(env, file_id, allow=None, **kwargs):
    data = {"printer": env.device, "file": file_id}
    if allow is not None:
        data["allow_clear_bed"] = allow
    await env.hass.services.async_call(DOMAIN, "print_file", data, blocking=True, **kwargs)


async def _refused(key, call):
    with pytest.raises(ServiceValidationError) as err:
        await call
    assert err.value.translation_key == key
    return err.value


def _sent(env):
    """(printer-side name, the bytes sent) for each upload, in order."""
    return [(call.args[3], call.args[4]) for call in env.send.await_args_list]


# ------------------------------------------------- the tick box on print_file


@pytest.mark.parametrize("allow", [None, False])
async def test_unticked_is_an_ordinary_print(env, allow):
    await _print_file(env, _stage(env, "part.gcode", _part()), allow)
    assert [name for name, _ in _sent(env)] == ["part.gcode"]
    (_, start), = Transport.last.starts()
    assert start["data"]["filename"] == "part.gcode"
    assert env.coord.print_state.armed is None and env.coord.clearing.state == "off"


async def test_ticked_sends_the_original_then_the_copy_starts_the_copy_and_arms(env):
    copies = []

    async def send(session, target, path, name, size, **kwargs):
        copies.append((name, Path(path).read_bytes(), Path(path)))

    env.send.side_effect = send
    file_id = _stage(env, "part.gcode", _part())
    await _print_file(env, file_id, True)

    (first, original, _), (second, copy, copy_path) = copies
    assert first == "part.gcode" and original == _part().encode()      # the original, first
    assert second == "part-ha-pushoff.gcode"
    block = "\n".join(sweep.clearing_block(
        sweep.S1_MAX, 12.4, (((170.0, 170.0), (180.0, 170.0), (180.0, 180.0), (170.0, 180.0)),),
        cooling=actions_mod.NOZZLE_COOLING))
    assert copy == _part().replace("M104 S0\n", "M104 S0\n" + block + "\n").encode()
    assert "M601" in block and "M117" not in block

    (_, start), = Transport.last.starts()
    data = start["data"]
    assert data["filename"] == "part-ha-pushoff.gcode"
    assert data["filesize"] == len(copy) and data["md5"] == hashlib.md5(copy).hexdigest()

    armed = env.coord.print_state.armed
    assert (armed.name, armed.md5, armed.task_id, armed.bed_limit) == (
        "part-ha-pushoff.gcode", data["md5"], data["taskid"], 28)
    assert env.coord.clearing.state == "armed"
    # Print again is for the original, never the copy.
    assert env.coord.last_file.name == "part.gcode"
    assert env.coord.last_file.md5 == hashlib.md5(original).hexdigest()
    assert not copy_path.exists() and _gone(env, file_id)                # no copy left behind


@pytest.mark.parametrize(("content", "key"), [
    (_part(extra=";TYPE:Brim\nG1 X1 Y1 E1\n"), "clear_brim"),
    (_part(extra=";TYPE:Skirt\nG1 X1 Y1 E1\n"), "clear_skirt"),
    (_part(extra="; FEATURE: Prime tower\n"), "clear_tower"),
    (_part(extra="M601\n"), "clear_own_pause"),
    (_part(max_z="3.0"), "clear_part_too_short"),
    (_part(max_z="250"), "clear_part_too_tall"),
    (_part(model=None), "clear_file_model"),
    (_part().replace("M84\n", ""), "clear_end_code"),
    (_part().replace("; raft_layers = 0\n", ""), "clear_raft"),
])
async def test_ticked_and_not_eligible_is_refused_and_nothing_is_sent(env, content, key):
    file_id = _stage(env, "part.gcode", content)
    await _refused(key, _print_file(env, file_id, True))
    _nothing_sent(env)
    assert env.coord.print_state.armed is None and _gone(env, file_id)
    assert env.coord.last_guard_outcome == key


async def test_petg_needs_a_textured_plate_and_gets_the_cooler_limit(env):
    await _refused("clear_petg_plate", _print_file(env, _stage(
        env, "a.gcode", _part(material="PETG", slot=2, plate="Smooth PEI Plate")), True))
    _nothing_sent(env)
    await _print_file(env, _stage(env, "b.gcode", _part(material="PETG", slot=2)), True)
    assert env.coord.print_state.armed.bed_limit == 25


async def test_a_material_that_is_not_pla_or_petg_is_refused(env):
    _ready(env.coord, _boxes(("ABS", [1, 2, 3])))
    await _refused("clear_material",
                   _print_file(env, _stage(env, "a.gcode", _part(material="ABS")), True))
    _nothing_sent(env)


async def test_clearing_is_for_the_s1_max_only(hass, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from homeassistant.components.file_upload import FileUploadData
    from homeassistant.helpers import device_registry as dr

    s1 = HandshakeResult("192.168.1.52", 9883, "u", "p", "DEVICE-ID-0003", "20025", "SER-S1",
                         model_name="Anycubic Kobra S1")
    entry = await _setup(hass, s1)
    _ready(entry.runtime_data)
    uploads = FileUploadData(tmp_path, {})
    hass.data["file_upload"] = uploads
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "SER-S1")})
    fetch, send = AsyncMock(), AsyncMock()
    with patch.object(upload_mod, "async_fetch_target", fetch), \
         patch.object(upload_mod, "async_send_file", send):
        s1_env = SimpleNamespace(hass=hass, tmp=tmp_path, uploads=uploads, device=device.id,
                                 fetch=fetch, send=send)
        await _refused("clear_model", _print_file(
            s1_env, _stage(s1_env, "a.gcode", _part(model="Anycubic Kobra S1")), True))
        _nothing_sent(s1_env)
    assert hass.states.get(CLEAR.replace("s1_max", "s1")) is None
    assert hass.states.get(STATE.replace("s1_max", "s1")) is None


async def test_a_start_the_printer_turns_down_leaves_nothing_armed(env):
    Transport.replies = [10101]
    with pytest.raises(HomeAssistantError):
        await _print_file(env, _stage(env, "part.gcode", _part()), True)
    assert env.coord.print_state.armed is None and env.coord.clearing.state == "off"


async def test_a_failed_second_upload_starts_nothing(env):
    env.send.side_effect = [None, upload_mod.UploadError("upload_failed")]
    with pytest.raises(HomeAssistantError):
        await _print_file(env, _stage(env, "part.gcode", _part()), True)
    assert Transport.last.starts() == [] and env.coord.print_state.armed is None


# ------------------------------------------------- Print again


async def test_print_again_ticked_is_refused_and_unticked_starts_the_original(env):
    await _print_file(env, _stage(env, "part.gcode", _part()), True)
    env.coord.clearing.async_disarm()
    _ready(env.coord)
    await _refused("print_again_no_clear", env.hass.services.async_call(
        DOMAIN, "print_again", {"printer": env.device, "allow_clear_bed": True}, blocking=True))
    assert len(Transport.last.starts()) == 1
    Transport.replies = [200]
    await env.hass.services.async_call(DOMAIN, "print_again", {"printer": env.device},
                                       blocking=True)
    assert Transport.last.starts()[-1][1]["data"]["filename"] == "part.gcode"
    assert env.coord.print_state.armed is None


# ------------------------------------------------- the clear_bed action, and the button


async def _to_ready(env):
    await _print_file(env, _stage(env, "part.gcode", _part()), True)
    task = int(env.coord.print_state.armed.task_id)
    temps = {"curr_hotbed_temp": 24, "target_hotbed_temp": 0, "curr_chamber_temp": 25}
    project = {"state": "printing", "pause": 0, "filename": "part-ha-pushoff.gcode",
               "task_id": task}
    env.coord._apply("info", {"state": "busy", "project": project, "temp": temps})
    env.coord._apply("info", {"state": "busy", "temp": temps,
                              "project": {**project, "state": "paused", "pause": 1}})
    await env.hass.async_block_till_done()
    assert env.coord.clearing.state == "ready"


async def _clear_bed(env, hass_admin_user=None, confirmed=None, user=...):
    data = {"printer": env.device}
    if confirmed is not None:
        data["door_open_confirmed"] = confirmed
    context = Context(user_id=None if user is None else user.id)
    await env.hass.services.async_call(DOMAIN, "clear_bed", data, blocking=True,
                                       context=context)


def _resumes():
    return [body for _, body in Transport.last.published if body.get("action") == "resume"]


async def test_clear_bed_with_the_confirmation_from_an_admin_sends_the_resume(env, hass_admin_user):
    await _to_ready(env)
    await _clear_bed(env, confirmed=True, user=hass_admin_user)
    assert len(_resumes()) == 1 and env.coord.clearing.state == "pushing"
    assert env.hass.states.get(STATE).state == "pushing"


async def test_clear_bed_without_the_confirmation_points_to_the_dashboard(env, hass_admin_user):
    await _to_ready(env)
    await _refused("clear_no_door_sensor", _clear_bed(env, user=hass_admin_user))
    await _refused("clear_no_door_sensor", _clear_bed(env, confirmed=False, user=hass_admin_user))
    assert _resumes() == []


async def test_clear_bed_with_no_user_behind_it_is_refused(env):
    await _to_ready(env)
    await _refused("clear_needs_user", _clear_bed(env, confirmed=True, user=None))
    assert _resumes() == []


async def test_clear_bed_from_a_user_who_is_not_an_admin_is_refused(env, hass_read_only_user):
    await _to_ready(env)
    with pytest.raises(Unauthorized):
        await _clear_bed(env, confirmed=True, user=hass_read_only_user)
    assert _resumes() == []


async def test_clear_bed_on_another_model_is_refused(hass, hass_admin_user):
    from homeassistant.helpers import device_registry as dr

    await _setup(hass, HS_K3)
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "SER-K3")})
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "clear_bed", {"printer": device.id, "door_open_confirmed": True},
            blocking=True, context=Context(user_id=hass_admin_user.id))
    assert err.value.translation_key == "clear_model"


async def test_the_button_is_there_only_while_a_press_is_possible(env):
    assert env.hass.states.get(CLEAR).state == "unavailable"
    assert env.hass.states.get(STATE).state == "off"
    await _to_ready(env)
    assert env.hass.states.get(CLEAR).state != "unavailable"
    assert env.hass.states.get(STATE).state == "ready"


async def test_the_button_with_no_door_sensor_points_to_the_dashboard(env, hass_admin_user):
    await _to_ready(env)
    await _refused("clear_no_door_sensor", env.hass.services.async_call(
        "button", "press", {"entity_id": CLEAR}, blocking=True,
        context=Context(user_id=hass_admin_user.id)))
    assert _resumes() == []


async def test_the_button_refuses_a_press_with_no_user_and_one_from_a_non_admin(
        env, hass_read_only_user):
    await _to_ready(env)
    await _refused("clear_needs_user", env.hass.services.async_call(
        "button", "press", {"entity_id": CLEAR}, blocking=True))
    with pytest.raises(Unauthorized):
        await env.hass.services.async_call(
            "button", "press", {"entity_id": CLEAR}, blocking=True,
            context=Context(user_id=hass_read_only_user.id))
    assert _resumes() == []


async def test_the_button_works_with_a_door_sensor_that_saw_the_door_open(hass, hass_admin_user,
                                                                         tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from homeassistant.components.file_upload import FileUploadData
    from homeassistant.helpers import device_registry as dr

    door = "binary_sensor.printer_door"
    hass.states.async_set(door, "off")
    entry = await _setup(hass, HS, options={"door_sensor": door})
    _ready(entry.runtime_data)
    uploads = FileUploadData(tmp_path, {})
    hass.data["file_upload"] = uploads
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "SER-1")})
    with patch.object(upload_mod, "async_fetch_target", AsyncMock(
            return_value=upload_mod.UploadTarget(url="http://h:18910/u?s=t", secrets=("t",),
                                                 host="h"))), \
         patch.object(upload_mod, "async_send_file", AsyncMock()):
        door_env = SimpleNamespace(hass=hass, coord=entry.runtime_data, tmp=tmp_path,
                                   uploads=uploads, device=device.id)
        await _to_ready(door_env)
    hass.states.async_set(door, "on")
    await hass.async_block_till_done()
    await hass.services.async_call("button", "press", {"entity_id": CLEAR}, blocking=True,
                                   context=Context(user_id=hass_admin_user.id))
    assert len(_resumes()) == 1


# ------------------------------------------------- diagnostics and strings


async def test_diagnostics_show_the_clearing_state_and_no_names(env):
    await _to_ready(env)
    shown = (await diagnostics_mod.async_get_config_entry_diagnostics(
        env.hass, env.entry))["print_from_ha"]
    assert shown["clearing"] == {"state": "ready", "reason": None, "armed": True,
                                 "press_possible": True,
                                 "motor_window_s": clearing_mod.MOTOR_WINDOW}
    assert "part" not in json.dumps(shown)


def test_every_clearing_refusal_event_and_field_has_its_text():
    base = Path(actions_mod.__file__).parent
    strings = json.loads((base / "strings.json").read_text())
    assert strings == json.loads((base / "translations" / "en.json").read_text())
    messages = strings["exceptions"]
    used = set()
    for source in ("clearing.py", "actions.py", "button.py", "anycubic_local/sweep.py",
                   "anycubic_local/printfile.py"):
        used |= set(re.findall(r'\b(?:_refusal|FileRefused)\(\s*"([a-z0-9_]+)"',
                               (base / source).read_text()))
        used |= set(re.findall(r'translation_key\s*=\s*"([a-z0-9_]+)"',
                               (base / source).read_text()))
    assert {"clear_bed_hot", "clear_door_not_opened", "clear_raft", "clear_part_too_short",
            "clear_needs_user", "resume_locked"} <= used | {"resume_locked"}
    # button.py also names its entities with translation keys; those are not messages.
    missing = (used | {"resume_locked"}) - set(messages) - set(strings["entity"]["button"])
    assert not missing, missing
    for key in ("clear_bed_hot", "clear_chamber_hot"):
        assert "{temp} °C" in messages[key]["message"] and "{limit} °C" in messages[key]["message"]
    events = strings["entity"]["event"]["print_event"]["state_attributes"]["event_type"]["state"]
    assert {"clear_ready", "clear_done", "clear_failed"} <= set(events)
    assert set(strings["entity"]["sensor"]["bed_clearing"]["state"]) == set(clearing_mod.STATES)
    assert strings["entity"]["button"]["clear_bed"]["name"] == "Clear the bed"
    services = strings["services"]
    for service in ("print_file", "print_again"):
        assert services[service]["fields"]["allow_clear_bed"]["name"] == "Allow Clear the bed"
    assert set(services["clear_bed"]["fields"]) == {"printer", "door_open_confirmed"}
    assert strings["options"]["step"]["init"]["data"]["door_sensor"] == "Door sensor"


async def test_a_ticked_archive_is_sent_whole_and_its_copy_as_plain_gcode(env):
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Metadata/plate_1.gcode", _part())
    sent = []

    async def send(session, target, path, name, size, **kwargs):
        sent.append((name, Path(path).read_bytes()))
        return name[:-len(".3mf")] if name.endswith(".gcode.3mf") else name

    env.send.side_effect = send
    await _print_file(env, _stage(env, "part.gcode.3mf", buffer.getvalue()), True)
    (first, original), (second, copy) = sent
    assert first == "part.gcode.3mf" and original == buffer.getvalue()
    assert second == "part-ha-pushoff.gcode" and copy.startswith(b"; HEADER_BLOCK_START")
    assert b"M601" in copy
    (_, start), = Transport.last.starts()
    assert start["data"]["filename"] == "part-ha-pushoff.gcode"
    assert start["data"]["md5"] == hashlib.md5(copy).hexdigest()
    assert env.coord.print_state.armed.name == "part-ha-pushoff.gcode"
    assert env.coord.last_file.name == "part.gcode"
