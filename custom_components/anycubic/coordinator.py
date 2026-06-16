"""Push coordinator: owns the transport, holds merged PrinterState + ACE boxes."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.core import HomeAssistant
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
    DEFAULT_QUERY_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Printer status (info/tempature/fan/light) is pushed by the printer during activity, but the ACE
# box (multiColorBox) is NOT pushed — it only answers an on-demand getInfo — so we re-poll on an interval.
_QUERY_TYPES = ("info", "tempature", "fan", "light", "multiColorBox")


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
        self._factory = transport_factory if transport_factory is not None else mqtt_mod.AnycubicMqtt
        self._transport = None

    def _build_and_connect(self):
        """Construct the transport (paho client + blocking tls_set) and connect.

        Runs in an executor — `tls_set()` loads CA certs from disk, which must not
        happen on the event loop.
        """
        transport = self._factory(self.hs, on_report=self._on_report)
        transport.connect()
        for t in _QUERY_TYPES:
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
        """Called on the paho network thread — marshal onto the HA event loop."""
        self.hass.add_job(self._apply, msg_type, data)

    async def async_send_command(self, command: str, **kwargs) -> None:
        """Build a control command and publish it (executor — paho publish is blocking-ish)."""
        if self._transport is None:
            return
        topic, payload = build_command(self.hs.model_id, self.hs.device_id, command, **kwargs)
        await self.hass.async_add_executor_job(
            self._transport.publish, topic, json.dumps(payload))

    def _apply(self, msg_type: str, data: dict) -> None:
        if msg_type == "info":
            self.data.printer = parse_info(data)
        elif msg_type == "multiColorBox":
            self.data.ace = merge_boxes(self.data.ace, parse_multicolorbox(data))
        elif msg_type == "light":
            self.data.light = parse_light(data)
        self.async_set_updated_data(self.data)
