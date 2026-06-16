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
            "printer": asdict(data.printer),
            "ace": [asdict(box) for box in data.ace],
            "light": asdict(data.light),
            "drying_setpoints": {"temp_c": coordinator.drying_set_temp,
                                 "hours": coordinator.drying_set_hours},
        },
        TO_REDACT,
    )
