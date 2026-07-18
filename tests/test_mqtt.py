# tests/test_mqtt.py
from custom_components.anycubic.anycubic_local import mqtt as m
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult


class FakeClient:
    def __init__(self, *a, **k):
        self.subs = []; self.pubs = []
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
    def publish(self, t, payload): self.pubs.append((t, payload))


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
