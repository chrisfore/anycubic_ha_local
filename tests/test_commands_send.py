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


async def test_a_publish_line_that_cannot_be_written_does_not_stop_the_command(hass, caplog, monkeypatch):
    # The line is made before the command is published. A redactor that raised while making
    # it took the command with it, and only with debug logging on.
    import json
    import logging

    from custom_components.anycubic import coordinator as coord_mod

    def broken(*args, **kwargs):
        raise RecursionError(f"cannot redact {args!r}")

    caplog.set_level(logging.DEBUG, logger="custom_components.anycubic.coordinator")
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._transport.published.clear()
    monkeypatch.setattr(coord_mod, "redacted", broken)

    await coord.async_send_command("file_details", filename="alice-bracket.gcode")

    topic, body = coord._transport.published[0]
    assert json.loads(body)["data"]["filename"] == "alice-bracket.gcode"
    lines = [r.getMessage() for r in caplog.records if "file_details" in r.getMessage()]
    assert lines == ["publish file_details: the line could not be written; sent as usual"]
    assert "alice" not in caplog.text and "DEV" not in "".join(lines)


async def test_the_publish_line_is_scrubbed_of_the_running_job(hass, caplog):
    # A command that names the job under a key the list does not have. The coordinator
    # knows what is printing, so the name goes by its value.
    import logging
    from unittest.mock import patch

    from custom_components.anycubic import coordinator as coord_mod

    job = "0907-2001-Alice desk bracket _plate(01)_PLA_0.2_1h12m.gcode.3mf"
    caplog.set_level(logging.DEBUG, logger="custom_components.anycubic.coordinator")
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._apply("print", {"taskid": "-1", "progress": 5, "filename": job})
    await hass.async_block_till_done()
    caplog.clear()

    real_build = coord_mod.build_command

    def build(model_id, device_id, command, **kwargs):
        topic, payload = real_build(model_id, device_id, command, **kwargs)
        payload["data"] = {"taskid": "-1", "target": "Alice_desk_bracket_.stl_id_0_copy_0"}
        return topic, payload

    with patch.object(coord_mod, "build_command", build):
        await coord.async_send_command("pause")
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("publish pause"))
    assert "alice" not in line.lower() and "bracket" not in line.lower()
    assert '"taskid": "-1"' in line
