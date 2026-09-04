"""Thin paho-mqtt transport for the printer's local broker."""
from __future__ import annotations

import json
import logging
import ssl
import time
import uuid
from collections.abc import Callable

import paho.mqtt.client as mqtt

from .const import query_topic, redacted, report_prefix
from .handshake import HandshakeResult

_LOGGER = logging.getLogger(__name__)


def _connack_refused(code) -> bool:
    """Did the broker refuse this CONNACK?

    paho v1 hands the callback an int rc (0 accepted); paho v2 hands it a
    ReasonCode. Anything we can't interpret is treated as accepted — a
    misread here would make a healthy printer look dead, which is worse
    than the stale-data bug this check exists to catch.
    """
    is_failure = getattr(code, "is_failure", None)
    if is_failure is not None:
        return bool(is_failure)
    try:
        return int(code) != 0
    except (TypeError, ValueError):
        return False


class AnycubicMqtt:
    def __init__(self, hs: HandshakeResult, on_report: Callable[[str, dict], None],
                 client_factory=mqtt.Client) -> None:
        self._hs = hs
        self._on_report = on_report
        # Whether the broker currently has us. Read by the coordinator, which is the
        # only thing able to do anything about a dead session (re-handshake + rebuild).
        self._connected = False
        self._c = client_factory(client_id=f"ha-{uuid.uuid4().hex[:8]}")
        self._c.username_pw_set(hs.username, hs.password)
        try:
            self._c.tls_set(cert_reqs=ssl.CERT_NONE)
            self._c.tls_insecure_set(True)
        except Exception:  # noqa: BLE001
            pass
        self._c.on_message = self._handle
        # Sessions are clean, so the broker forgets our subscription on every drop
        # (the printer's broker restarts whenever the printer reboots). Subscribing in
        # on_connect covers the initial CONNACK and every paho auto-reconnect; subscribing
        # only once in connect() leaves publishes working but reports silently dead.
        self._c.on_connect = self._on_connect
        self._c.on_disconnect = self._on_disconnect

    def _on_connect(self, *args) -> None:
        # The CONNACK code was previously discarded, so a REFUSED connection looked
        # identical to an accepted one: paho kept retrying, we kept publishing into
        # nothing, and every entity held its last value while reporting healthy.
        code = args[3] if len(args) > 3 else 0
        if _connack_refused(code):
            self._connected = False
            _LOGGER.warning(
                "printer broker refused the connection (code %s); the session "
                "credentials are probably stale", code)
            return
        self._connected = True
        self._c.subscribe(f"{report_prefix(self._hs.model_id, self._hs.device_id)}/#")
        _LOGGER.debug("connected to %s:%s, subscribed to reports",
                      self._hs.broker_host, self._hs.broker_port)

    def _on_disconnect(self, *args) -> None:
        self._connected = False
        _LOGGER.warning("printer broker connection lost; paho will auto-reconnect")

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._c.connect(self._hs.broker_host, self._hs.broker_port, keepalive=60)
        self._c.loop_start()

    def disconnect(self) -> None:
        self._connected = False
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
        self.publish(query_topic(self._hs.model_id, self._hs.device_id, msg_type), body)

    def publish(self, topic: str, payload: str) -> None:
        # paho reports a send it could not make in the RETURN VALUE, not by raising.
        # Discarding it is how a poll could keep "succeeding" against nothing (issue #9),
        # and how a publish that never left the client looked identical to one the printer
        # received and ignored (issue #10). Both need the same return code.
        info = self._c.publish(topic, payload)
        rc = getattr(info, "rc", mqtt.MQTT_ERR_SUCCESS)
        if rc != mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.debug("publish to %s returned rc=%s", topic.rsplit("/", 1)[-1], rc)
        if rc == mqtt.MQTT_ERR_NO_CONN:
            self._connected = False

    def _handle(self, client, userdata, message) -> None:
        try:
            obj = json.loads(message.payload)
        except Exception:  # noqa: BLE001
            return
        if obj.get("action") == "query" and obj.get("data") is None and "state" not in obj:
            return  # our own echoed query
        msg_type = obj.get("type") or message.topic.rsplit("/", 1)[-1]
        # Which report actually arrived, and what it carried. Without this, a printer
        # answering every poll with frozen values (issue #9) looks identical to one
        # answering properly — the coordinator logs "updated" either way, and a report
        # type that quietly stops arriving leaves no trace at all. Redacted because these
        # lines get pasted into issues verbatim.
        _LOGGER.debug("report %s: %s", msg_type, redacted(obj))
        if msg_type == "print":
            # The printer's answer to a control command (code 200 = accepted). Logged here
            # rather than in the coordinator because an ack carries code/state at the TOP
            # level with data usually null — the data-is-None drop below would swallow it.
            # Without this, a command the firmware rejected looked exactly like one that was
            # never sent (issue #10). `print` is never polled, so this is not a per-cycle line.
            _LOGGER.debug("print ack: action=%s code=%s state=%s msg=%s",
                          obj.get("action"), obj.get("code"), obj.get("state"),
                          obj.get("msg") or obj.get("data"))
        data = obj.get("data")
        if data is not None:
            self._on_report(msg_type, data)
        elif msg_type == "video":
            # S1-family video reports answer startCapture with data:null ("initSuccess").
            # They must still reach a capture-kick waiter, or it stalls until timeout.
            self._on_report(msg_type, {})
