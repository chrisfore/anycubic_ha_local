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
    coord._transport.published.clear()   # drop the connect-time extfilbox probe (issue #12)
    await coord.async_send_command("camera_start")
    topic, payload = coord._transport.published[0]
    assert topic == "anycubic/anycubicCloud/v1/web/printer/20029/DEV/video"
    import json
    assert json.loads(payload)["action"] == "startCapture"

    await coord.async_send_command("light", on=True, brightness=100)
    topic2, payload2 = coord._transport.published[1]
    assert topic2.endswith("/web/printer/20029/DEV/light")
    assert json.loads(payload2)["data"] == {"type": 2, "status": 1, "brightness": 100}


async def test_publish_line_hides_the_file_name_while_the_printer_still_gets_it(hass, caplog):
    # The publish line is the only record of what we put on the wire (issue #10), and it
    # logged the body as sent. A file_details request names the file being printed. The
    # log gets a redacted copy; the printer has to get the real thing or it cannot answer.
    import json
    import logging

    device = "0123456789abcdef0123456789abcdef"          # made up, as long as a real one
    hs = HandshakeResult("192.168.1.50", 9883, "u", "p", device, "20029", "SERIAL-TEST-0001")
    caplog.set_level(logging.DEBUG, logger="custom_components.anycubic.coordinator")
    coord = AnycubicCoordinator(hass, hs, transport_factory=FakeTransport)
    await coord.async_start()
    coord._transport.published.clear()

    await coord.async_send_command("file_details", filename="alice-bracket.gcode")

    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith("publish file_details"))
    assert "alice-bracket" not in line
    assert device not in line
    # Still the record it is there to be: which topic, which action, which arguments.
    assert "anycubic/anycubicCloud/v1/web/printer/20029/**REDACTED**/file" in line
    assert '"action": "fileDetails"' in line
    assert '"root": "local"' in line and '"plate_index": 1' in line

    topic, body = coord._transport.published[0]
    assert topic == f"anycubic/anycubicCloud/v1/web/printer/20029/{device}/file"
    assert json.loads(body)["data"]["filename"] == "alice-bracket.gcode"
