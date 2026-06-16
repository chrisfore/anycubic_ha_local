"""Thin paho-mqtt transport for the printer's local broker."""
from __future__ import annotations

import json
import ssl
import time
import uuid
from collections.abc import Callable

import paho.mqtt.client as mqtt

from .const import query_topic, report_prefix
from .handshake import HandshakeResult


class AnycubicMqtt:
    def __init__(self, hs: HandshakeResult, on_report: Callable[[str, dict], None],
                 client_factory=mqtt.Client) -> None:
        self._hs = hs
        self._on_report = on_report
        self._c = client_factory(client_id=f"ha-{uuid.uuid4().hex[:8]}")
        self._c.username_pw_set(hs.username, hs.password)
        try:
            self._c.tls_set(cert_reqs=ssl.CERT_NONE)
            self._c.tls_insecure_set(True)
        except Exception:  # noqa: BLE001
            pass
        self._c.on_message = self._handle

    def connect(self) -> None:
        self._c.connect(self._hs.broker_host, self._hs.broker_port, keepalive=60)
        self._c.subscribe(f"{report_prefix(self._hs.model_id, self._hs.device_id)}/#")
        self._c.loop_start()

    def disconnect(self) -> None:
        try:
            self._c.loop_stop()
            self._c.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def query(self, msg_type: str) -> None:
        # The ACE box only answers a "getInfo" request; everything else uses "query".
        action = "getInfo" if msg_type == "multiColorBox" else "query"
        body = json.dumps({"type": msg_type, "action": action,
                           "timestamp": int(time.time() * 1000),
                           "msgid": uuid.uuid4().hex, "data": None})
        self._c.publish(query_topic(self._hs.model_id, self._hs.device_id, msg_type), body)

    def publish(self, topic: str, payload: str) -> None:
        self._c.publish(topic, payload)

    def _handle(self, client, userdata, message) -> None:
        try:
            obj = json.loads(message.payload)
        except Exception:  # noqa: BLE001
            return
        if obj.get("action") == "query" and obj.get("data") is None and "state" not in obj:
            return  # our own echoed query
        msg_type = obj.get("type") or message.topic.rsplit("/", 1)[-1]
        data = obj.get("data")
        if data is not None:
            self._on_report(msg_type, data)
