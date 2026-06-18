"""Diagnostics — a redacted snapshot of the entry + coordinator state for bug reports."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import AnycubicCoordinator

# Identifiers / addresses that should never leave the user's machine in a shared report.
# camera_url embeds the printer's IP; filename can embed the user's name.
TO_REDACT = {"host", "ip", "camera_url", "filename", "username", "password", "device_id",
             "serial", "broker_host", "deviceId", "mac"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator: AnycubicCoordinator = entry.runtime_data
    data = coordinator.data
    return async_redact_data(
        {
            "entry_data": dict(entry.data),
            "model_id": coordinator.hs.model_id,
            "host": coordinator.host,
            "update_success": coordinator.last_update_success,
            # Capability snapshot for adding a new printer model — everything the maintainer needs to
            # support it, and nothing sensitive (model IDs, the printer's own feature/peripheral
            # inventory, whether a chamber sensor / ACE box is present). See README "My printer isn't
            # listed". Attach the whole diagnostics file to a "Request support for my printer" issue.
            "capabilities": {
                "model_id": coordinator.hs.model_id,
                "model_name": coordinator.hs.model_name,
                "device_type": coordinator.hs.device_type,
                "firmware": data.printer.firmware,
                "has_chamber_temp": data.printer.chamber_temp is not None,
                "ace_attached": bool(data.ace),
                "features": coordinator.raw_features,
                "peripherie": coordinator.peripherie,
                "report_types_seen": sorted(coordinator.seen_report_types),
            },
            "printer": asdict(data.printer),
            "ace": [asdict(box) for box in data.ace],
            "light": asdict(data.light),
            "drying_setpoints": {"temp_c": coordinator.drying_set_temp,
                                 "hours": coordinator.drying_set_hours},
        },
        TO_REDACT,
    )
