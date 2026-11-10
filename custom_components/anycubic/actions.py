"""The print_file and print_again actions: the guards, then the upload, then the start.

All are for admins only. The guards run in a fixed order, under one lock per printer, and
every refusal is one plain line raised before anything is sent: no upload and no message to
the printer. A failure after that point says what failed, in fixed text. A start the printer
confirms is said in one notification per printer.

Guards, in order: 1 the model may print from Home Assistant; 2 the printer has reported
within the gap bound; 3 nothing else is being sent to it; 4 it is idle; 5 the bed is clear;
6 the file's kind, name and (for an archive) structure; 7 its size; 8 the printer it was
sliced for; 9 the filament it needs against what is loaded. Print again runs 1 to 5 and 9.

With "Allow Clear the bed" ticked, the file must also pass the Clear the bed checks, all of
them before anything is sent. The original is then uploaded, then a prepared copy of it, and
the copy is what starts. The clear_bed action is the checked press that pushes the parts off
once such a print has ended (see clearing.py).
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service import async_register_admin_service

from .anycubic_local import sweep, upload
from .anycubic_local.commands import FILETYPE_GCODE
from .anycubic_local.models import display_filename
from .anycubic_local.printfile import (
    FileRefused,
    clearing_families,
    inspect_file,
    is_marked,
    material_family,
    model_matches,
    printer_name,
    scan_for_clearing,
    write_prepared_copy,
)
from .clearing import bed_limit_for
from .const import (
    ACE_SLOT_COUNT,
    DOMAIN,
    MODEL_NAMES,
    PRINT_MODELS,
    PRINTER_NAME_CAP,
    PUSH_OFF_MARKER,
    PUSH_OFF_MODELS,
    primary_ace_box_id,
)
from .coordinator import AnycubicCoordinator
from .store import LastFile

SERVICE_PRINT_FILE = "print_file"
SERVICE_PRINT_AGAIN = "print_again"
SERVICE_CLEAR_BED = "clear_bed"
ATTR_PRINTER = "printer"
ATTR_FILE = "file"
ATTR_ALLOW_CLEAR_BED = "allow_clear_bed"
ATTR_DOOR_OPEN_CONFIRMED = "door_open_confirmed"

# How a prepared copy cools the nozzle after the resume, before the sweep. PROVISIONAL: the
# wait for 50 C is what the design asks for, but whether this printer's M109 waits while
# cooling has not been measured, so a fixed dwell follows it. Set from the pause test before
# Clear the bed is released.
NOZZLE_COOLING = (sweep.CoolTo(50), sweep.Dwell(300))

# Home Assistant's own cap on an uploaded file. Checked again here, before the file is read,
# so a file over it is never read and the refusal says why.
MAX_FILE_BYTES = 100 * 1024 * 1024
# Longest material name kept in the last-file record.
_LONGEST_MATERIAL = 64
_WHITE = (255, 255, 255)

# What diagnostics show for a submission that got all the way to a running job.
OUTCOME_STARTED = "started"

# The confirmation of a start the printer confirmed: one notification per printer, each
# replacing the one before, titled with the printer's name. A constant, as strings.json has
# no place for a notification's text.
STARTED_MESSAGE = "Started printing {name}"
_STARTED_NOTIFICATION_ID = DOMAIN + "_print_started_{entry_id}"


def _one_device(value) -> str:
    """One device id. A target picked in the UI can arrive as a one-item list."""
    if isinstance(value, list):
        if len(value) != 1:
            raise vol.Invalid("Choose one printer.")
        value = value[0]
    return cv.string(value)


PRINT_FILE_SCHEMA = vol.Schema({
    vol.Optional(ATTR_PRINTER): _one_device,
    vol.Required(ATTR_FILE): cv.string,
    vol.Optional(ATTR_ALLOW_CLEAR_BED, default=False): cv.boolean,
})
PRINT_AGAIN_SCHEMA = vol.Schema({
    vol.Optional(ATTR_PRINTER): _one_device,
    vol.Optional(ATTR_ALLOW_CLEAR_BED, default=False): cv.boolean,
})
CLEAR_BED_SCHEMA = vol.Schema({
    vol.Optional(ATTR_PRINTER): _one_device,
    vol.Optional(ATTR_DOOR_OPEN_CONFIRMED, default=False): cv.boolean,
})


def _refusal(key: str, **placeholders) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=key,
        translation_placeholders={k: str(v) for k, v in placeholders.items()} or None)


def _failure(key: str) -> HomeAssistantError:
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=key)


@callback
def async_setup_actions(hass: HomeAssistant) -> None:
    """Register the actions, once, for every printer."""

    async def _print_file(call: ServiceCall) -> None:
        await async_print_file(hass, call.data.get(ATTR_PRINTER), call.data[ATTR_FILE],
                               allow_clear=call.data[ATTR_ALLOW_CLEAR_BED])

    async def _print_again(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call.data.get(ATTR_PRINTER))
        if call.data[ATTR_ALLOW_CLEAR_BED]:
            # The printer does not check a file's md5 at a start, so a file put there under
            # the prepared copy's name would run in its place.
            coordinator.last_guard_outcome = "print_again_no_clear"
            raise _refusal("print_again_no_clear")
        await async_print_again(hass, coordinator)

    async def _clear_bed(call: ServiceCall) -> None:
        # Stricter than the other actions: a call with nobody behind it, an automation's
        # or a script's, is refused. The admin check itself is the registration's.
        if not call.context.user_id:
            raise _refusal("clear_needs_user")
        await async_clear_bed(_coordinator(hass, call.data.get(ATTR_PRINTER)),
                              door_open_confirmed=call.data[ATTR_DOOR_OPEN_CONFIRMED])

    async_register_admin_service(hass, DOMAIN, SERVICE_PRINT_FILE, _print_file,
                                 schema=PRINT_FILE_SCHEMA)
    async_register_admin_service(hass, DOMAIN, SERVICE_PRINT_AGAIN, _print_again,
                                 schema=PRINT_AGAIN_SCHEMA)
    async_register_admin_service(hass, DOMAIN, SERVICE_CLEAR_BED, _clear_bed,
                                 schema=CLEAR_BED_SCHEMA)


@callback
def _coordinator(hass: HomeAssistant, device_id: str | None) -> AnycubicCoordinator:
    """The printer behind a device: a loaded entry of this integration, and its printer.

    A multi-color unit is a device of this integration too, but not a printer. With no
    device named, the one printer set up, if there is exactly one: with none or several,
    which one is meant is for the user to say. Every enabled printer counts, connected or
    not, so that a print meant for one that is offline never starts on another. A printer
    that is set up but not loaded is refused as not connected, named or not.
    """
    if device_id is None:
        entries = [entry for entry in hass.config_entries.async_entries(DOMAIN)
                   if entry.disabled_by is None and entry.source != SOURCE_IGNORE]
        if len(entries) != 1:
            raise _refusal("printer_not_chosen")
        return _loaded(entries[0])
    device = dr.async_get(hass).async_get(device_id)
    if device is not None:
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.domain != DOMAIN:
                continue
            if entry.state is not ConfigEntryState.LOADED:
                # Its printer's device carries the entry's unique id, the serial.
                if entry.unique_id and (DOMAIN, entry.unique_id) in device.identifiers:
                    raise _refusal("printer_not_connected")
                continue
            coordinator: AnycubicCoordinator = entry.runtime_data
            if (DOMAIN, coordinator.hs.serial) in device.identifiers:
                return coordinator
    raise _refusal("unknown_printer")


def _loaded(entry: ConfigEntry) -> AnycubicCoordinator:
    if entry.state is not ConfigEntryState.LOADED:
        raise _refusal("printer_not_connected")
    return entry.runtime_data


async def async_clear_bed(coordinator: AnycubicCoordinator, *,
                          door_open_confirmed: bool = False) -> None:
    """The clear_bed action, and the Clear the bed button: the checked press."""
    if coordinator.hs.model_id not in PUSH_OFF_MODELS:
        raise _refusal("clear_model")
    await coordinator.clearing.async_press(door_open_confirmed=door_open_confirmed)


async def async_print_file(hass: HomeAssistant, device_id: str | None, file_id: str, *,
                           allow_clear: bool = False) -> None:
    """The print_file action.

    The uploaded file is Home Assistant's, usable once, and deleted at the end of this
    whatever the outcome, a printer that cannot be found included.
    """
    holder = process_uploaded_file(hass, file_id)
    try:
        path: Path | None = holder.__enter__()
    except ValueError:
        holder, path = None, None
    try:
        coordinator = _coordinator(hass, device_id)
        await _with_outcome(coordinator,
                            _async_submit_file(hass, coordinator, path, allow_clear))
    finally:
        if holder is not None:
            await hass.async_add_executor_job(holder.__exit__, None, None, None)


async def async_print_again(hass: HomeAssistant, coordinator: AnycubicCoordinator) -> None:
    """The print_again action, and the Print again button: start the last file again."""
    await _with_outcome(coordinator, _async_start_again(coordinator))


async def _with_outcome(coordinator: AnycubicCoordinator, submission) -> None:
    try:
        await submission
    except HomeAssistantError as err:
        coordinator.last_guard_outcome = err.translation_key or "error"
        raise
    coordinator.last_guard_outcome = OUTCOME_STARTED


def _check_model(coordinator: AnycubicCoordinator) -> None:
    if coordinator.hs.model_id not in PRINT_MODELS:
        raise _refusal("model_not_enabled")


def _check_reachable(coordinator: AnycubicCoordinator) -> None:
    if not coordinator.reachable:
        raise _refusal("printer_unreachable")


def _check_idle(coordinator: AnycubicCoordinator) -> None:
    if not coordinator.idle:
        raise _refusal("printer_busy")


def _check_bed(coordinator: AnycubicCoordinator) -> None:
    if coordinator.bed_needs_clearing:
        raise _refusal("bed_not_clear")


def _check_printer(coordinator: AnycubicCoordinator) -> None:
    """Guards 2, 4 and 5 again, just before the start: an upload can take minutes."""
    _check_reachable(coordinator)
    _check_idle(coordinator)
    _check_bed(coordinator)


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        raise FileRefused("file_unreadable") from None


@dataclass(frozen=True)
class _Prepared:
    """A prepared copy of a file, ready to send: where it is, and what the start says."""

    path: Path
    size: int
    md5: str
    bed_limit: int


def _prepare(path: Path, kind: str, model_id: str, facts, model_named) -> _Prepared:
    """Check a file for Clear the bed and write its prepared copy. Executor.

    Every check comes before the copy is written, and all of it before anything is sent.
    The copy has a generated name in the system's temporary folder; the caller deletes it.
    """
    profile = sweep.profile_for(model_id)
    if profile is None:
        raise FileRefused("clear_model")
    if model_named is not True:
        raise FileRefused("clear_file_model")
    families = clearing_families(facts)
    scan = scan_for_clearing(path, kind)
    if "PETG" in families and not scan.textured_plate:
        raise FileRefused("clear_petg_plate")
    block = sweep.clearing_block(profile, scan.max_z, scan.footprints, cooling=NOZZLE_COOLING)
    handle, name = tempfile.mkstemp(prefix="anycubic-", suffix=".tmp")
    os.close(handle)
    copy = Path(name)
    size, md5 = write_prepared_copy(path, copy, kind, scan, block)
    return _Prepared(path=copy, size=size, md5=md5, bed_limit=bed_limit_for(families))


def _delete(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


async def _async_submit_file(hass: HomeAssistant, coordinator: AnycubicCoordinator,
                             path: Path | None, allow_clear: bool = False) -> None:
    _check_model(coordinator)
    _check_reachable(coordinator)
    if coordinator.submit_lock.locked():
        raise _refusal("submit_in_progress")
    async with coordinator.submit_lock:
        _check_idle(coordinator)
        _check_bed(coordinator)
        if path is None:
            raise _refusal("file_missing")
        try:
            name = printer_name(path.name, cap=PRINTER_NAME_CAP, marker=PUSH_OFF_MARKER)
            size = await hass.async_add_executor_job(_size_of, path)
            if size <= 0:
                raise FileRefused("file_empty")
            if size > MAX_FILE_BYTES:
                raise FileRefused("file_too_large")
            facts = await hass.async_add_executor_job(inspect_file, path, name.kind)
        except FileRefused as err:
            raise _refusal(err.key) from None
        model_named = _check_file_model(coordinator, facts.models)
        ams_settings = _filament_mapping(coordinator, facts.used_slots, facts.materials,
                                         facts.colors)
        prepared: _Prepared | None = None
        if allow_clear:
            try:
                prepared = await hass.async_add_executor_job(
                    _prepare, path, name.kind, coordinator.hs.model_id, facts, model_named)
            except FileRefused as err:
                raise _refusal(err.key) from None
        try:
            await _async_send_and_start(hass, coordinator, path, name, facts, model_named,
                                        ams_settings, prepared)
        finally:
            if prepared is not None:
                await hass.async_add_executor_job(_delete, prepared.path)


async def _async_send_and_start(hass: HomeAssistant, coordinator: AnycubicCoordinator,
                                path: Path, name, facts, model_named, ams_settings: dict,
                                prepared: _Prepared | None) -> None:
    """Upload the file, and its prepared copy if there is one, then start what was asked."""
    session = async_get_clientsession(hass)
    identifiers = (*coordinator.redaction_identifiers, coordinator.host)
    with coordinator.submitting(name.original, name.marked, path.name):
        try:
            target = await upload.async_fetch_target(session, coordinator.host,
                                                     identifiers=identifiers)
            with coordinator.holding(target.secrets):
                # The original first, and always: Print again starts it, never a copy.
                await upload.async_send_file(session, target, path, name.original,
                                             facts.size, identifiers=identifiers)
                if prepared is not None:
                    await upload.async_send_file(
                        session, target, prepared.path, name.marked, prepared.size,
                        identifiers=identifiers)
        except upload.UploadError as err:
            raise _failure(err.key) from None
        # Recorded as soon as the file is on the printer, so that a start the printer
        # turns down, or does not confirm, can be tried again with Print again.
        coordinator.async_set_last_file(LastFile(
            name=name.original, size=facts.size, md5=facts.md5, filetype=FILETYPE_GCODE,
            used_slots=facts.used_slots,
            materials={k: v[:_LONGEST_MATERIAL] for k, v in facts.materials.items()},
            colors=dict(facts.colors), model_named=model_named is True))
        _check_printer(coordinator)
        if prepared is None:
            started = await coordinator.async_start_print(
                name=name.original, size=facts.size, md5=facts.md5,
                filetype=FILETYPE_GCODE, ams_settings=ams_settings)
        else:
            started = await coordinator.async_start_print(
                name=name.marked, size=prepared.size, md5=prepared.md5,
                filetype=FILETYPE_GCODE, ams_settings=ams_settings,
                bed_limit=prepared.bed_limit)
        if started:
            _notify_started(coordinator, name.original)


async def _async_start_again(coordinator: AnycubicCoordinator) -> None:
    _check_model(coordinator)
    _check_reachable(coordinator)
    if coordinator.submit_lock.locked():
        raise _refusal("submit_in_progress")
    async with coordinator.submit_lock:
        _check_idle(coordinator)
        _check_bed(coordinator)
        record = coordinator.last_file
        if record is None:
            raise _refusal("nothing_to_print_again")
        if is_marked(record.name, PUSH_OFF_MARKER):
            raise _refusal("print_again_marked")
        ams_settings = _filament_mapping(coordinator, record.used_slots, record.materials,
                                         record.colors)
        with coordinator.submitting(record.name):
            if await coordinator.async_start_print(
                    name=record.name, size=record.size, md5=record.md5,
                    filetype=record.filetype, ams_settings=ams_settings, again=True):
                _notify_started(coordinator, record.name)


@callback
def _notify_started(coordinator: AnycubicCoordinator, name: str) -> None:
    """Say that the printer confirmed the start of `name`, replacing the last such word.

    The file is named as the user reads it, with no path. An underscore is escaped, as the
    notification is shown as Markdown and a name like "_part_.gcode" would turn italic.
    """
    device = coordinator.registered_device((DOMAIN, coordinator.hs.serial))
    title = (device and (device.name_by_user or device.name)) or MODEL_NAMES.get(
        coordinator.hs.model_id, "Anycubic printer")
    shown = (display_filename(name) or "").replace("_", "\\_")
    persistent_notification.async_create(
        coordinator.hass, STARTED_MESSAGE.format(name=shown), title=title,
        notification_id=_STARTED_NOTIFICATION_ID.format(
            entry_id=coordinator.config_entry.entry_id))


def _check_file_model(coordinator: AnycubicCoordinator, models) -> bool | None:
    """Guard 8. A file that names another printer is refused; one naming none goes ahead."""
    names = [n for n in (MODEL_NAMES.get(coordinator.hs.model_id), coordinator.hs.model_name)
             if n]
    matched = model_matches(models, coordinator.hs.model_id, names)
    if matched is False:
        raise _refusal("wrong_printer_model")
    return matched


def _filament_mapping(coordinator: AnycubicCoordinator, used_slots, materials: dict[int, str],
                      colors: dict[int, str]) -> dict:
    """Guard 9, and the filament mapping the start carries.

    The slots a file uses are what it says it uses; if it does not say, every slot it lists;
    if it lists none, slot 1. With a multi-color unit, file slot N is the unit's slot N, as
    slicers map it by default. With none, a single-filament file prints from the external
    spool. What is loaded must be known, as last reported, and of the family the file asks
    for wherever the file names one.
    """
    slots = tuple(used_slots or sorted(materials) or (1,))
    present = coordinator.ace_present
    if present is None:
        raise _refusal("filament_unknown")
    if not present:
        if len(slots) > 1:
            raise _refusal("multi_filament_needs_unit")
        spool = coordinator.data.external_spool
        _compare_material(None, materials.get(slots[0]), spool.material if spool else None)
        return {"use_ams": False, "ams_box_mapping": []}

    primary = primary_ace_box_id(coordinator.hs.model_id)
    if len(slots) > 1 and coordinator.attached_box_ids - {primary}:
        raise _refusal("second_unit_single_only")
    box = next((b for b in coordinator.data.ace if b.id == primary), None)
    if box is None or primary not in coordinator.attached_box_ids:
        raise _refusal("filament_unknown")
    mapping = []
    for number in slots:
        if not 1 <= number <= ACE_SLOT_COUNT:
            raise _refusal("slot_out_of_range", slot=number)
        slot = box.slots.get(number - 1)
        loaded = slot.material if slot is not None else None
        wanted = materials.get(number)
        _compare_material(number, wanted, loaded)
        mapping.append({
            "paint_index": number - 1,
            "ams_index": number - 1,
            "paint_color": _rgba(colors.get(number) or slot.color_hex),
            "ams_color": _rgba(slot.color_hex),
            "material_type": wanted or loaded,
        })
    return {"use_ams": True, "ams_box_mapping": mapping}


def _compare_material(slot: int | None, wanted: str | None, loaded: str | None) -> None:
    """Refuse a slot (None: the external spool) whose material is unknown or another family."""
    loaded_family = material_family(loaded)
    if loaded_family is None:
        if slot is None:
            raise _refusal("spool_material_unknown")
        raise _refusal("slot_material_unknown", slot=slot)
    wanted_family = material_family(wanted)
    if wanted_family is not None and wanted_family != loaded_family:
        if slot is None:
            raise _refusal("spool_material_mismatch", loaded=loaded_family,
                           wanted=wanted_family)
        raise _refusal("slot_material_mismatch", slot=slot, loaded=loaded_family,
                       wanted=wanted_family)


def _rgba(color: str | None) -> list[int]:
    """"#RRGGBB" as the four numbers the printer takes; white if there is no color."""
    try:
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)) if color else _WHITE
    except (TypeError, ValueError):
        rgb = _WHITE
    return [*rgb, 255]
