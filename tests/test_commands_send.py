from custom_components.anycubic.coordinator import AnycubicCoordinator
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult

HS = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER-1")


class FakeTransport:
    def __init__(self, hs, on_report, **k): self.published = []
    def connect(self): pass
    def disconnect(self): pass
    def query(self, t): pass
    def publish(self, topic, payload): self.published.append((topic, payload))


async def test_send_command_publishes_built_payload(hass):
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    await coord.async_send_command("camera_start")
    topic, payload = coord._transport.published[0]
    assert topic == "anycubic/anycubicCloud/v1/web/printer/20029/DEV/video"
    import json
    assert json.loads(payload)["action"] == "startCapture"

    await coord.async_send_command("light", on=True, brightness=100)
    topic2, payload2 = coord._transport.published[1]
    assert topic2.endswith("/web/printer/20029/DEV/light")
    assert json.loads(payload2)["data"] == {"type": 2, "status": 1, "brightness": 100}
