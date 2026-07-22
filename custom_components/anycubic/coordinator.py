"""Push coordinator: owns the transport, holds merged PrinterState + ACE boxes."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .anycubic_local import mqtt as mqtt_mod
from .anycubic_local.commands import build as build_command
from .anycubic_local.handshake import HandshakeResult
from .anycubic_local.models import (
    AceBox,
    LightState,
    PrinterState,
    merge_boxes,
    parse_info,
    parse_light,
    parse_multicolorbox,
)
from .const import (
    ACE_DRYING_DEFAULT_DURATION_MIN,
    ACE_DRYING_DEFAULT_TEMP,
    ACE_MODEL_NAMES,
    ACE_SUFFIX,
    DEFAULT_QUERY_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Printer status (info/tempature/fan/light) is pushed by the printer during activity, but the ACE
# box (multiColorBox) is NOT pushed — it only answers an on-demand getInfo — so we re-poll on an interval.
_QUERY_TYPES = ("info", "tempature", "fan", "light", "multiColorBox")

# `peripherie` is a static capability inventory ({camera, multiColorBox, udisk} presence flags) — it
# doesn't change, so we ask for it once at connect (for diagnostics / model onboarding) and never poll it.
_CONNECT_ONLY_QUERY_TYPES = ("peripherie",)


@dataclass
class AnycubicData:
    printer: PrinterState = field(default_factory=PrinterState)
    ace: list[AceBox] = field(default_factory=list)
    light: LightState = field(default_factory=LightState)


class AnycubicCoordinator(DataUpdateCoordinator[AnycubicData]):
    def __init__(self, hass: HomeAssistant, hs: HandshakeResult,
                 host: str | None = None, transport_factory=None) -> None:
        super().__init__(hass, logger=_LOGGER, name=DOMAIN,
                         update_interval=timedelta(seconds=DEFAULT_QUERY_INTERVAL))
        self.hs = hs
        # The host the user entered (IP or DNS/mDNS name). Used for the HTTP-facing URLs
        # (camera, device link) so a name is honored and re-resolved; MQTT uses the
        # printer-reported broker. Falls back to the broker host when not supplied.
        self.host = host or hs.broker_host
        # ACE drying setpoints (number entities edit these; the drying switch uses them).
        self.drying_set_temp = ACE_DRYING_DEFAULT_TEMP
        self.drying_set_hours = ACE_DRYING_DEFAULT_DURATION_MIN // 60
        self.data = AnycubicData()
        # Capability data captured for diagnostics / new-model onboarding (see diagnostics.py).
        # Non-sensitive: the printer's reported feature map, the peripheral presence inventory, and
        # which report types this printer actually emits.
        self.raw_features: dict | None = None
        self.peripherie: dict | None = None
        self.seen_report_types: set[str] = set()
        self._factory = transport_factory if transport_factory is not None else mqtt_mod.AnycubicMqtt
        self._transport = None

    def _build_and_connect(self):
        """Construct the transport (paho client + blocking tls_set) and connect.

        Runs in an executor — `tls_set()` loads CA certs from disk, which must not
        happen on the event loop.
        """
        transport = self._factory(self.hs, on_report=self._on_report)
        transport.connect()
        for t in (*_QUERY_TYPES, *_CONNECT_ONLY_QUERY_TYPES):
            transport.query(t)
        return transport

    async def async_start(self) -> None:
        self._transport = await self.hass.async_add_executor_job(self._build_and_connect)

    def _poll(self) -> None:
        for t in _QUERY_TYPES:
            self._transport.query(t)

    async def async_shutdown(self) -> None:
        if self._transport is not None:
            await self.hass.async_add_executor_job(self._transport.disconnect)
        await super().async_shutdown()

    async def _async_update_data(self) -> AnycubicData:
        # Re-poll on the interval so the ACE box (which the printer never pushes) stays fresh;
        # printer status also arrives via push between polls.
        if self._transport is not None:
            await self.hass.async_add_executor_job(self._poll)
        return self.data

    def _on_report(self, msg_type: str, data: dict) -> None:
        """Called on the paho network thread — marshal onto the HA event loop.

        Must be call_soon_threadsafe: add_job with a plain (non-@callback) function
        dispatches to an executor thread, and async_set_updated_data off the event
        loop trips HA's thread-safety check on every report.
        """
        self.hass.loop.call_soon_threadsafe(self._apply, msg_type, data)

    async def async_send_command(self, command: str, **kwargs) -> None:
        """Build a control command and publish it (executor — paho publish is blocking-ish)."""
        if self._transport is None:
            return
        topic, payload = build_command(self.hs.model_id, self.hs.device_id, command, **kwargs)
        await self.hass.async_add_executor_job(
            self._transport.publish, topic, json.dumps(payload))

    @callback
    def _apply(self, msg_type: str, data: dict) -> None:
        self.seen_report_types.add(msg_type)
        if msg_type == "info":
            self.data.printer = parse_info(data)
            features = data.get("features")
            if isinstance(features, dict):
                self.raw_features = features
        elif msg_type == "multiColorBox":
            self.data.ace = merge_boxes(self.data.ace, parse_multicolorbox(data))
            self._sync_ace_device_model()
        elif msg_type == "light":
            self.data.light = parse_light(data)
        elif msg_type == "peripherie" and isinstance(data, dict):
            self.peripherie = data
        self.async_set_updated_data(self.data)

    @callback
    def _sync_ace_device_model(self) -> None:
        """Show the real box model (ACE Pro vs ACE 2) once the box reports it.

        The ACE device registers under the literal name "ACE 2" before the box has
        reported, so entity IDs stay deterministic (ace_2_*); this renames only the
        registry display name/model. A user rename (name_by_user) still wins.
        """
        boxes = self.data.ace
        model_id = boxes[0].model_id if boxes else None
        name = ACE_MODEL_NAMES.get(str(model_id)) if model_id is not None else None
        if not name:
            return
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(
            identifiers={(DOMAIN, f"{self.hs.serial}_{ACE_SUFFIX}")})
        if device is not None and (device.name != name or device.model != name):
            registry.async_update_device(device.id, name=name, model=name)
