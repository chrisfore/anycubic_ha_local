"""Printer sensors and ACE 2 box + slot sensors."""
from __future__ import annotations

import math
import re

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfMass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ACE_SLOT_COUNT, ENCLOSED_MODELS, box_has_dryer
from .coordinator import AnycubicCoordinator
from .definitions import (
    ACE_SENSORS,
    PRINTER_SENSORS,
    AceSensorEntityDescription,
    AnycubicSensorEntityDescription,
    slot_attributes,
)
from .entity import AnycubicAceEntity, AnycubicEntity, async_setup_ace_entities


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coord: AnycubicCoordinator = entry.runtime_data
    enclosed = coord.hs.model_id in ENCLOSED_MODELS
    add([AnycubicSensor(coord, d) for d in PRINTER_SENSORS if enclosed or not d.enclosed_only])
    add([AnycubicPauseReasonSensor(coord), AnycubicFilamentUsedSensor(coord)])

    def ace_sensors(box_id: int) -> list:
        dryer = box_has_dryer(box_id)
        return [AnycubicAceBoxSensor(coord, d, box_id)
                for d in ACE_SENSORS if dryer or not d.dry_box_only] + [
            AnycubicAceSlotSensor(coord, i, box_id) for i in range(1, ACE_SLOT_COUNT + 1)]

    async_setup_ace_entities(entry, coord, add, ace_sensors)
    _async_setup_external_spool(entry, coord, add)


@callback
def _async_setup_external_spool(entry: ConfigEntry, coord: AnycubicCoordinator,
                                add: AddEntitiesCallback) -> None:
    """Create the external-spool sensor the first time the printer reports one (issue #12).

    The printer sends `extfilbox` only while no ACE unit is attached, so creating this
    up front would leave every ACE owner with an entity that never has a value. Created
    on first report instead, which also covers unplugging the ACE mid-session.
    """
    created: set[bool] = set()

    @callback
    def _scan() -> None:
        if coord.data.external_spool is not None and not created:
            created.add(True)
            add([AnycubicExternalSpoolSensor(coord)])

    _scan()
    entry.async_on_unload(coord.async_add_listener(_scan))


class AnycubicSensor(AnycubicEntity, SensorEntity):
    entity_description: AnycubicSensorEntityDescription

    def __init__(self, coordinator: AnycubicCoordinator, description: AnycubicSensorEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.data.printer)


# Filament used (issue #22). The printer reports the job's filament as millimetres fed —
# measured on a Kobra S1 Max: a job the slicer put at 7.27 m ended at 8177, the rest being
# purge at colour changes and the prime. Shown in grams, the way filament is bought, from
# 1.75 mm filament and the material's density. Densities in g/cm³, by the word a material
# name starts with ("PETG", "TPU for ACE", "PLA+").
FILAMENT_DIAMETER_MM = 1.75
MATERIAL_DENSITY = {"PLA": 1.24, "PETG": 1.27, "PET": 1.27, "ABS": 1.04, "ASA": 1.07,
                    "TPU": 1.21, "PA": 1.14, "PC": 1.20, "HIPS": 1.04, "PVA": 1.23}
DEFAULT_DENSITY = MATERIAL_DENSITY["PLA"]
_WORDS = re.compile(r"[A-Za-z]+")


def material_density(name: str | None) -> float | None:
    """The density for a material name, or None if it names none known here."""
    for word in _WORDS.findall(name or ""):
        if word.upper() in MATERIAL_DENSITY:
            return MATERIAL_DENSITY[word.upper()]
    return None


def job_density(data) -> tuple[float, str]:
    """The density to weigh the job's filament by, and where it was found.

    The file name first: Anycubic's slicer puts the material in it ("…_PETG_0.2_55m.gcode").
    Then the ACE slot feeding, while one is; the printer forgets it once the job ends.
    Then the ACE slots holding filament, if they agree. PLA otherwise.
    """
    density = material_density(data.printer.filename)
    if density is not None:
        return density, "file name"
    for box in data.ace:
        slot = box.slots.get(box.loaded_slot)
        density = material_density(slot.material) if slot else None
        if density is not None:
            return density, "loaded slot"
    found = {material_density(s.material) for box in data.ace for s in box.slots.values() if s.loaded}
    found.discard(None)
    if len(found) == 1:
        return found.pop(), "ACE slots"
    return DEFAULT_DENSITY, "default"


class AnycubicFilamentUsedSensor(AnycubicEntity, SensorEntity):
    """Filament the job has fed, in grams. Per job, so no long-term statistics: it starts
    over with each job and can dip during one (retractions)."""

    _attr_translation_key = "filament_used"
    _attr_icon = "mdi:printer-3d-nozzle"
    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.GRAMS
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "filament_used")

    @property
    def native_value(self):
        mm = self.coordinator.data.printer.filament_used
        if mm is None:
            return None
        density, _ = job_density(self.coordinator.data)
        area_cm2 = math.pi * (FILAMENT_DIAMETER_MM / 20) ** 2
        return round(mm / 10 * area_cm2 * density, 2)

    @property
    def extra_state_attributes(self):
        mm = self.coordinator.data.printer.filament_used
        density, source = job_density(self.coordinator.data)
        return {"length_mm": mm, "density": density, "density_from": source}


class AnycubicPauseReasonSensor(AnycubicEntity, SensorEntity):
    """Why the print is paused: "Paused by user", "Filament is stuck", … Unknown otherwise.

    Read from the coordinator's reason, not from PrinterState: info never carries it.
    """

    _attr_translation_key = "pause_reason"
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "pause_reason")

    @property
    def native_value(self):
        reason = self.coordinator.data.reason
        return reason.reason if reason is not None and reason.paused else None

    @property
    def extra_state_attributes(self):
        reason = self.coordinator.data.reason
        if reason is None or not reason.paused:
            return None
        return {"code": reason.code, "by_user": reason.by_user}


class AnycubicAceBoxSensor(AnycubicAceEntity, SensorEntity):
    entity_description: AceSensorEntityDescription

    def __init__(self, coordinator, description, box_id: int = 0):
        super().__init__(coordinator, description.key, box_id)
        self.entity_description = description

    @property
    def native_value(self):
        return None if self._box is None else self.entity_description.value_fn(self._box)


class AnycubicAceSlotSensor(AnycubicAceEntity, SensorEntity):
    _attr_translation_key = "ace_slot"

    def __init__(self, coordinator, slot_number: int, box_id: int = 0):
        # Display "Slot 1..4" with entity-ids slot_1..slot_4, but the printer reports slots
        # 0-indexed (0..3) — so look up slot_number - 1.
        super().__init__(coordinator, f"slot_{slot_number}", box_id)
        self._box_index = slot_number - 1
        self._attr_translation_placeholders = {"n": str(slot_number)}

    @property
    def _slot(self):
        return None if self._box is None else self._box.slots.get(self._box_index)

    @property
    def native_value(self):
        s = self._slot
        return None if s is None else (s.material or "Empty")

    @property
    def extra_state_attributes(self):
        return slot_attributes(self._slot)


class AnycubicExternalSpoolSensor(AnycubicEntity, SensorEntity):
    """The bare spool holder used in place of an ACE unit.

    State is the material, matching the ACE slot sensors so the two read the same way
    on a dashboard; the colour the reporter asked for rides along as an attribute.
    """

    _attr_translation_key = "external_spool"
    _attr_icon = "mdi:printer-3d-nozzle"

    def __init__(self, coordinator: AnycubicCoordinator) -> None:
        super().__init__(coordinator, "external_spool")

    @property
    def _spool(self):
        return self.coordinator.data.external_spool

    @property
    def available(self) -> bool:
        # Cleared once an ACE takes over. Unavailable is the honest reading — "unknown"
        # would suggest we are still watching a holder that is no longer in the filament path.
        return super().available and self._spool is not None

    @property
    def native_value(self):
        spool = self._spool
        return None if spool is None else (spool.material or "Empty")

    @property
    def extra_state_attributes(self):
        spool = self._spool
        if spool is None:
            return {}
        return {"material": spool.material, "color": spool.color_hex,
                "loaded": spool.loaded, "status": spool.current_status}
