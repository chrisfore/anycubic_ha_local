"""Diagnostics — a redacted snapshot of the entry + coordinator state for bug reports."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .anycubic_local.const import SENSITIVE_KEYS, redacted
from .coordinator import AnycubicCoordinator

# Shared with the inbound-report debug log, so a key only has to be classified once.
TO_REDACT = SENSITIVE_KEYS


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator: AnycubicCoordinator = entry.runtime_data
    data = coordinator.data
    # The whole dict goes through the SAME redactor as the debug log, not through a key list
    # alone. features, peripherie and raw_multicolorbox are passed on verbatim so that a key
    # this integration does not know can be triaged from the attachment, and a key nobody
    # has seen is exactly the one a key list cannot cover: a new firmware putting a URL or
    # an id under one would have gone straight out. That same function is what keeps
    # camera_url's scheme, port and path (how an unvalidated model's camera gets debugged —
    # issue #6) while masking its host. Home Assistant's own key redaction stays on top.
    #
    # The running job's name is scrubbed by value too, out of those same verbatim blocks.
    # The redactor finds it for itself, under `printer` below.
    return async_redact_data(redacted(
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
        coordinator.redaction_identifiers,
    ), TO_REDACT)
