# tests/test_mqtt.py
import paho.mqtt.client as paho

from custom_components.anycubic.anycubic_local import mqtt as m
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult


class FakeClient:
    def __init__(self, *a, **k):
        self.subs = []; self.pubs = []
        self.publish_rc = paho.MQTT_ERR_SUCCESS
        self.on_message = None; self.on_connect = None; self.on_disconnect = None
    def username_pw_set(self, u, p): self.u, self.p = u, p
    def tls_set(self, **k): self.tls = True
    def tls_insecure_set(self, v): self.insecure = v
    def connect(self, h, port, keepalive=60):
        self.conn = (h, port)
        # paho fires on_connect after CONNACK (initial connect AND every auto-reconnect)
        if self.on_connect:
            self.on_connect(self, None, {}, 0)
    def simulate_reconnect(self):
        """Broker dropped us (e.g. printer reboot); paho's loop thread reconnected."""
        if self.on_disconnect:
            self.on_disconnect(self, None, 1)
        if self.on_connect:
            self.on_connect(self, None, {}, 0)
    def loop_start(self): self.started = True
    def loop_stop(self): self.started = False
    def disconnect(self): self.conn = None
    def subscribe(self, t): self.subs.append(t)
    def publish(self, t, payload):
        self.pubs.append((t, payload))
        # paho hands back an MQTTMessageInfo whose rc reports a send into a dead socket
        return type("Info", (), {"rc": self.publish_rc})()


def _msg(topic, payload):
    import json
    class M:  # paho MQTTMessage-ish
        def __init__(s): s.topic = topic; s.payload = json.dumps(payload).encode()
    return M()


def test_connect_subscribe_and_route_report():
    hs = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER")
    seen = []
    client = m.AnycubicMqtt(hs, on_report=lambda t, d: seen.append((t, d)), client_factory=FakeClient)
    client.connect()
    assert client._c.conn == ("1.2.3.4", 9883)
    assert any("printer/public/20029/DEV/#" in s for s in client._c.subs)
    client.query("info")
    assert client._c.pubs[0][0].endswith("/web/printer/20029/DEV/info")
    # deliver a report -> on_report called with (type, data)
    client._c.on_message(client._c, None, _msg(
        "anycubic/anycubicCloud/v1/printer/public/20029/DEV/info/report",
        {"type": "info", "action": "report", "data": {"state": "free"}}))
    assert seen == [("info", {"state": "free"})]
    # our own query echo (no data, action query) is ignored
    client._c.on_message(client._c, None, _msg(
        "anycubic/anycubicCloud/v1/web/printer/20029/DEV/info",
        {"type": "info", "action": "query", "data": None}))
    assert len(seen) == 1


def test_resubscribes_after_broker_reconnect():
    """Printer reboots restart its broker; paho auto-reconnects with a clean session,
    so the subscription MUST be re-established in on_connect or reports stop forever
    (publishes keep working — the failure is silent)."""
    hs = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER")
    client = m.AnycubicMqtt(hs, on_report=lambda t, d: None, client_factory=FakeClient)
    client.connect()
    subs_after_connect = len(client._c.subs)
    assert subs_after_connect >= 1
    client._c.simulate_reconnect()
    assert len(client._c.subs) == subs_after_connect + 1, (
        "subscription not re-established after reconnect")
    assert all("printer/public/20029/DEV/#" in s for s in client._c.subs)


def _client():
    hs = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER")
    c = m.AnycubicMqtt(hs, on_report=lambda t, d: None, client_factory=FakeClient)
    c.connect()
    return c


def test_accepted_connack_reports_connected():
    assert _client().connected is True


def test_refused_connack_is_not_treated_as_connected():
    """A refused CONNACK used to be indistinguishable from an accepted one: the code
    subscribed anyway and reported healthy while receiving nothing (issue #9)."""
    c = _client()
    subs_before = len(c._c.subs)
    c._c.on_connect(c._c, None, {}, 5)          # 5 = not authorised (stale credentials)
    assert c.connected is False
    assert len(c._c.subs) == subs_before, "subscribed on a refused connection"


def test_refused_connack_is_detected_through_a_paho_v2_reason_code():
    """paho v2 passes a ReasonCode object, not an int rc."""
    c = _client()
    c._c.on_connect(c._c, None, {}, type("RC", (), {"is_failure": True})(), None)
    assert c.connected is False


def test_an_unreadable_connack_code_is_treated_as_accepted():
    """Failing open here is deliberate: misreading the code would take a healthy
    printer offline, which is worse than the stale-data bug being fixed."""
    c = _client()
    c._c.on_connect(c._c, None, {}, object())
    assert c.connected is True


def test_dropped_connection_reports_disconnected():
    c = _client()
    c._c.on_disconnect(c._c, None, 1)
    assert c.connected is False
    c._c.on_connect(c._c, None, {}, 0)          # paho auto-reconnected
    assert c.connected is True


def test_publish_into_a_dead_socket_reports_disconnected():
    """paho reports this in the return value rather than raising, so discarding it
    let a poll keep 'succeeding' against a session the printer had already dropped."""
    c = _client()
    c._c.publish_rc = paho.MQTT_ERR_NO_CONN
    c.query("info")
    assert c.connected is False


def test_forwards_video_report_with_null_data():
    """S1-family video reports carry data:null (state:"initSuccess") — they must still
    reach the coordinator so a waiter knows the printer answered startCapture."""
    hs = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER")
    seen = []
    client = m.AnycubicMqtt(hs, on_report=lambda t, d: seen.append((t, d)), client_factory=FakeClient)
    client.connect()
    client._c.on_message(client._c, None, _msg(
        "anycubic/anycubicCloud/v1/printer/public/20029/DEV/video/report",
        {"type": "video", "action": "startCapture", "state": "initSuccess",
         "code": 200, "data": None}))
    assert seen == [("video", {})]


def test_inbound_reports_are_logged_with_secrets_redacted(caplog):
    """The report log is the instrument for issue #9 — it must name the type that arrived
    and must not leak the address or filename into a log a user pastes into an issue."""
    import logging
    from custom_components.anycubic.anycubic_local.const import redacted

    payload = {"type": "info", "action": "report",
               "data": {"ip": "192.168.1.50", "filename": "alice-bracket.gcode",
                        "temp": {"curr_nozzle_temp": 93}}}
    out = redacted(payload)
    assert out["data"]["ip"] == "**REDACTED**"
    assert out["data"]["filename"] == "**REDACTED**"
    # The values we actually need for triage must survive untouched.
    assert out["data"]["temp"]["curr_nozzle_temp"] == 93
    assert out["type"] == "info"
    # And the original is not mutated.
    assert payload["data"]["ip"] == "192.168.1.50"


def test_redacted_leaves_absent_values_alone():
    from custom_components.anycubic.anycubic_local.const import redacted
    assert redacted({"ip": None, "slots": [{"filename": "x", "index": 1}]}) == {
        "ip": None, "slots": [{"filename": "**REDACTED**", "index": 1}]}


def test_redacted_truncates_huge_base64_blobs():
    # Issue #13: a `file`/fileDetails report carries base64 thumbnail + png_image + svg_image.
    # Logged verbatim that is hundreds of KB per print, and the reporter had to strip them by
    # hand before pasting. Keep enough to identify the field, drop the payload.
    from custom_components.anycubic.anycubic_local.const import redacted

    out = redacted({"type": "file", "data": {"file_details": {
        "thumbnail": "A" * 40000, "png_image": "B" * 9000, "root": "local"}}})
    thumb = out["data"]["file_details"]["thumbnail"]
    assert len(thumb) < 200
    assert thumb.startswith("AAAA")
    assert "40000" in thumb                      # says how much was dropped
    assert out["data"]["file_details"]["root"] == "local"   # short values untouched
