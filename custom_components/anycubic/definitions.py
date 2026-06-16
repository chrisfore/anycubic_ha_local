"""EntityDescription catalogs with value_fn lambdas over the coordinator's PrinterState."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import BinarySensorEntityDescription
from homeassistant.components.number import NumberDeviceClass, NumberEntityDescription
from homeassistant.components.sensor import SensorDeviceClass, SensorEntityDescription, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature, UnitOfTime

from .anycubic_local.models import AceBox, PrinterState, Slot


@dataclass(frozen=True, kw_only=True)
class AnycubicSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[PrinterState], object]
    enclosed_only: bool = False   # only create on enclosed printers (KS1 / KS1 Max)


@dataclass(frozen=True, kw_only=True)
class AnycubicBinaryEntityDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[PrinterState], bool]


_T = SensorDeviceClass.TEMPERATURE
_C = UnitOfTemperature.CELSIUS  # printer reports Celsius; default the display to C too
PRINTER_SENSORS: tuple[AnycubicSensorEntityDescription, ...] = (
    AnycubicSensorEntityDescription(key="status", translation_key="status", value_fn=lambda p: p.status),
    AnycubicSensorEntityDescription(key="nozzle_temperature", translation_key="nozzle_temperature",
        device_class=_T, native_unit_of_measurement=_C, suggested_unit_of_measurement=_C,
        state_class=SensorStateClass.MEASUREMENT, value_fn=lambda p: p.nozzle_temp),
    AnycubicSensorEntityDescription(key="bed_temperature", translation_key="bed_temperature",
        device_class=_T, native_unit_of_measurement=_C, suggested_unit_of_measurement=_C,
        state_class=SensorStateClass.MEASUREMENT, value_fn=lambda p: p.bed_temp),
    AnycubicSensorEntityDescription(key="chamber_temperature", translation_key="chamber_temperature",
        device_class=_T, native_unit_of_measurement=_C, suggested_unit_of_measurement=_C,
        state_class=SensorStateClass.MEASUREMENT, value_fn=lambda p: p.chamber_temp, enclosed_only=True),
    AnycubicSensorEntityDescription(key="progress", translation_key="progress",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: p.progress),
    AnycubicSensorEntityDescription(key="current_layer", translation_key="current_layer",
        value_fn=lambda p: p.current_layer),
    AnycubicSensorEntityDescription(key="total_layers", translation_key="total_layers",
        value_fn=lambda p: p.total_layers),
    AnycubicSensorEntityDescription(key="time_remaining", translation_key="time_remaining",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_unit_of_measurement=UnitOfTime.HOURS, suggested_display_precision=1,
        value_fn=lambda p: p.remain_time),
    AnycubicSensorEntityDescription(key="filename", translation_key="filename", icon="mdi:file",
        value_fn=lambda p: p.filename),
    AnycubicSensorEntityDescription(key="firmware", translation_key="firmware", icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda p: p.firmware),
)

PRINTER_BINARY_SENSORS: tuple[AnycubicBinaryEntityDescription, ...] = (
    AnycubicBinaryEntityDescription(key="printing", translation_key="printing", is_on_fn=lambda p: p.printing),
    AnycubicBinaryEntityDescription(key="paused", translation_key="paused", is_on_fn=lambda p: p.paused),
)


@dataclass(frozen=True, kw_only=True)
class AnycubicNumberEntityDescription(NumberEntityDescription):
    command: str        # commands.build() command name
    attr: str           # PrinterState field to read and optimistically update
    enclosed_only: bool = False   # only create on enclosed printers (KS1 / KS1 Max)


# Live printer setpoints — read the printer's reported target, write via print/update settings.
# Ranges/units from the validated capture (nozzle 251, bed 71, box_fan_level 40 all seen).
PRINTER_NUMBERS: tuple[AnycubicNumberEntityDescription, ...] = (
    AnycubicNumberEntityDescription(key="nozzle_target", translation_key="nozzle_target",
        command="set_nozzle_temp", attr="nozzle_target", device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS, native_min_value=0,
        native_max_value=320, native_step=5, icon="mdi:printer-3d-nozzle-heat"),
    AnycubicNumberEntityDescription(key="bed_target", translation_key="bed_target",
        command="set_bed_temp", attr="bed_target", device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS, native_min_value=0,
        native_max_value=120, native_step=5, icon="mdi:radiator"),
    AnycubicNumberEntityDescription(key="fan_speed", translation_key="fan_speed",
        command="set_fan_speed", attr="fan_speed_pct", native_unit_of_measurement=PERCENTAGE,
        native_min_value=0, native_max_value=100, native_step=1, icon="mdi:fan"),
    AnycubicNumberEntityDescription(key="aux_fan", translation_key="aux_fan",
        command="set_aux_fan", attr="aux_fan_speed_pct", native_unit_of_measurement=PERCENTAGE,
        native_min_value=0, native_max_value=100, native_step=1, icon="mdi:fan-auto"),
    AnycubicNumberEntityDescription(key="box_fan", translation_key="box_fan",
        command="set_box_fan", attr="box_fan_level", native_unit_of_measurement=PERCENTAGE,
        native_min_value=0, native_max_value=100, native_step=1, icon="mdi:fan-chevron-up",
        enclosed_only=True),
)


@dataclass(frozen=True, kw_only=True)
class AceSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[AceBox], object]


@dataclass(frozen=True, kw_only=True)
class AceBinaryEntityDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[AceBox], bool]


ACE_SENSORS: tuple[AceSensorEntityDescription, ...] = (
    AceSensorEntityDescription(key="humidity", translation_key="ace_humidity",
        device_class=SensorDeviceClass.HUMIDITY, native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT, value_fn=lambda b: b.humidity),
    AceSensorEntityDescription(key="box_temperature", translation_key="ace_box_temperature",
        device_class=_T, native_unit_of_measurement=_C, suggested_unit_of_measurement=_C,
        state_class=SensorStateClass.MEASUREMENT, value_fn=lambda b: b.temp),
    AceSensorEntityDescription(key="loaded_slot", translation_key="ace_loaded_slot",
        # Printer slots are 0-indexed; show 1-4 to match the "Slot N" labels. -1/None = nothing loaded.
        value_fn=lambda b: "None" if b.loaded_slot in (None, -1) else b.loaded_slot + 1),
)

# Drying is exposed as a controllable switch (see switch.py), not a read-only binary sensor.
ACE_BINARY_SENSORS: tuple[AceBinaryEntityDescription, ...] = ()


def slot_attributes(slot: Slot | None) -> dict:
    if slot is None:
        return {}
    return {"material": slot.material, "color": slot.color_hex, "sku": slot.sku,
            "remaining": slot.remaining, "loaded": slot.loaded}
