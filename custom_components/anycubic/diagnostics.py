"""Diagnostics — a redacted snapshot of the entry + coordinator state for bug reports."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import AnycubicCoordinator

# Identifiers / addresses that should never leave the user's machine in a shared report.
# filename can embed the user's name. camera_url is handled by _mask_url_host instead:
# scheme/port/path must survive (they're how an unvalidated model's camera gets debugged
# from a diagnostics attachment — issue #6), only the address is secret.
TO_REDACT = {"host", "ip", "filename", "username", "password", "device_id",
             "serial", "broker_host", "deviceId", "mac"}


def _mask_url_host(url: str | None) -> str | None:
    """Redact only the host (and any query, which could carry a token) of a URL."""
    if not url:
        return url
    parts = urlsplit(url)
    if not parts.hostname:
        return "**REDACTED**"
    netloc = parts.netloc.replace(parts.hostname, "**REDACTED**")
    return parts._replace(netloc=netloc, query="**REDACTED**" if parts.query else "").geturl()


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
            "printer": asdict(data.printer) | {"camera_url": _mask_url_host(data.printer.camera_url)},
            "ace": [asdict(box) for box in data.ace],
            # Verbatim last multiColorBox payload — carries wire keys the parser may not
            # know about (e.g. model-specific slot fields), for protocol triage from a
            # diagnostics attachment alone.
            "raw_multicolorbox": coordinator.raw_multicolorbox,
            "light": asdict(data.light),
            "drying_setpoints": {
                str(box_id): {"temp_c": coordinator.drying_temp(box_id),
                              "hours": coordinator.drying_hours(box_id)}
                for box_id in sorted({0, *(box.id for box in data.ace)})
            },
        },
        TO_REDACT,
    )
