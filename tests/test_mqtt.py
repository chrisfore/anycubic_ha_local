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


def test_the_answer_to_stop_capture_is_not_forwarded():
    """The capture kick sends stopCapture, pauses, then startCapture, and waits for the
    printer's video report. A slow printer answers the STOP after the start has gone out,
    and that answer ended the wait: before capture was running and, on firmware that puts
    the stream URL in the start answer, before there was a URL (issue #15). Only the
    envelope says which command a video report answers, and only the transport sees it."""
    hs = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER")
    seen = []
    client = m.AnycubicMqtt(hs, on_report=lambda t, d: seen.append((t, d)), client_factory=FakeClient)
    client.connect()
    topic = "anycubic/anycubicCloud/v1/printer/public/20029/DEV/video/report"
    for envelope in (
        {"type": "video", "action": "stopCapture", "state": "pushStopped", "code": 200,
         "data": None},
        # Whatever action it is filed under, a report that says pushing stopped is not
        # the start answer.
        {"type": "video", "action": "report", "state": "pushStopped", "code": 200,
         "data": {"urls": {"rtspUrl": "http://1.2.3.4:18088/live/old"}}},
    ):
        client._c.on_message(client._c, None, _msg(topic, envelope))
    assert seen == []
    # The start answer still arrives, with a URL (Kobra 4) or without one (S1 family),
    # and so does one that reports a failure: the wait has its answer either way.
    client._c.on_message(client._c, None, _msg(topic, {
        "type": "video", "action": "startCapture", "state": "initSuccess", "code": 200,
        "data": {"urls": {"rtspUrl": "http://1.2.3.4:18088/live/new"}}}))
    client._c.on_message(client._c, None, _msg(topic, {
        "type": "video", "action": "startCapture", "state": "initFailed", "code": 11402,
        "data": None}))
    assert seen == [("video", {"urls": {"rtspUrl": "http://1.2.3.4:18088/live/new"}}),
                    ("video", {})]


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


def test_redaction_covers_plate_name_not_just_filename():
    # Reported on #12: masking `filename` while leaving `plate_name` in the clear was
    # decorative — they carry the same text, and the full path sat next to a **REDACTED**
    # filename in the reporter's own paste. Either both go or neither does.
    from custom_components.anycubic.anycubic_local.const import redacted

    out = redacted({"data": {
        "filename": "0907-2001-Plant wall clip.gcode",
        "plate_name": "/useremain/app/gk/gcodes/0907-2001-Plant wall clip_plate(01).gcode"}})
    assert out["data"]["filename"] == "**REDACTED**"
    assert out["data"]["plate_name"] == "**REDACTED**"


# ------------------------------------------- what the transport writes to the debug log
#
# The tests above call the redactor directly. These read the log lines themselves, because
# a redactor that works is no use to a line that does not go through it: the print-ack
# line logged its payload raw, and the connect line printed the address outright.
# Every identifier here is made up.

LAN = "192.168.1.50"
DEVICE = "0123456789abcdef0123456789abcdef"
SERIAL = "SERIAL-TEST-0001"
TOKEN = "feedfacefeedfacefeedfacefeedface"
SECRET_HS = HandshakeResult(LAN, 9883, "u", "p", DEVICE, "20029", SERIAL, mac="AA-BB-CC-DD-EE-FF")
REPORTS = f"anycubic/anycubicCloud/v1/printer/public/20029/{DEVICE}"


def _logging_client(caplog, **kwargs):
    import logging
    caplog.set_level(logging.DEBUG, logger=m.__name__)
    return m.AnycubicMqtt(SECRET_HS, on_report=lambda t, d: None, client_factory=FakeClient,
                          **kwargs)


def _logged(caplog):
    """Everything the transport logged, as a user would paste it."""
    return "\n".join(r.getMessage() for r in caplog.records if r.name == m.__name__)


def test_a_logged_info_report_carries_neither_the_address_nor_the_upload_token(caplog):
    # The `urls` block is not on the key list and must not be: its port and path are how a
    # camera gets debugged. So the address in rtspUrl and the `s=` token in fileUploadurl
    # went out in every `info` report line.
    client = _logging_client(caplog)
    client._c.on_message(client._c, None, _msg(f"{REPORTS}/info/report", {
        "type": "info", "action": "report", "timestamp": 1700000000000,
        "msgid": "made-by-the-printer", "state": "done", "code": 200, "msg": "done",
        "data": {"printerName": "Alice's Kobra", "model": "Anycubic Kobra S1 Max",
                 "version": "2.7.1.4", "ip": LAN, "state": "free",
                 "temp": {"curr_nozzle_temp": 27, "target_nozzle_temp": 0},
                 "urls": {"rtspUrl": f"http://{LAN}:18088/flv",
                          "fileUploadurl": f"http://{LAN}:18910/gcode_upload?s={TOKEN}"},
                 "features": {"fod_support": True}}}))
    log = _logged(caplog)
    assert "report info:" in log
    assert LAN not in log
    assert TOKEN not in log
    assert "Alice" not in log
    # What triage needs is still there. The version looks exactly like an IPv4 address.
    assert "'version': '2.7.1.4'" in log
    assert "'model': 'Anycubic Kobra S1 Max'" in log
    assert "'curr_nozzle_temp': 27" in log
    assert "http://**REDACTED**:18088/flv" in log
    assert "http://**REDACTED**:18910/gcode_upload?**REDACTED**" in log


def test_a_logged_report_hides_this_printers_identifiers_under_unknown_keys(caplog):
    # A key nobody has seen cannot be on the list. The transport knows its own handshake,
    # so those values are scrubbed wherever a new firmware puts them.
    client = _logging_client(caplog)
    client._c.on_message(client._c, None, _msg(f"{REPORTS}/info/report", {
        "type": "info", "action": "report", "data": {
            "state": "free", "cn": SERIAL, "bind": f"bound to {DEVICE}",
            "usn": "uuid:fdm:aa:bb:cc:dd:ee:ff", "note": f"ssh root@{LAN}"}}))
    log = _logged(caplog).lower()
    for secret in (SERIAL, DEVICE, "aa:bb:cc:dd:ee:ff", LAN):
        assert secret.lower() not in log, secret
    assert "'state': 'free'" in log


def test_a_report_labelled_from_its_topic_does_not_log_the_device_id(caplog):
    # A message with no `type` is labelled with the last piece of its topic, and every
    # topic has the device id in it. The label goes through the redactor like the payload.
    client = _logging_client(caplog)
    client._c.on_message(client._c, None, _msg(REPORTS, {"state": "free"}))
    log = _logged(caplog)
    assert "report **REDACTED**:" in log
    assert DEVICE not in log


def test_the_transport_scrubs_extra_identifiers_it_is_handed(caplog):
    # The address the user typed is known to the coordinator, not to the handshake.
    client = _logging_client(caplog, identifiers=(DEVICE, "kobra-s1.local"))
    client._c.on_message(client._c, None, _msg(f"{REPORTS}/info/report", {
        "type": "info", "action": "report", "data": {"seen_as": "kobra-s1.local:18910"}}))
    assert "kobra-s1.local" not in _logged(caplog)


def test_a_print_ack_with_no_msg_does_not_log_the_file_name(caplog):
    # The ack line printed `msg`, or the whole of `data` when `msg` was empty, without
    # passing either through the redactor. A progress report has an empty msg and a
    # filename in its data.
    client = _logging_client(caplog)
    client._c.on_message(client._c, None, _msg(f"{REPORTS}/print/report", {
        "type": "print", "action": "start", "timestamp": 1700000000000,
        "msgid": "made-by-the-printer", "state": "printing", "code": 200, "msg": "",
        "data": {"taskid": "-1", "progress": 5, "filename": "alice-bracket.gcode"}}))
    log = _logged(caplog)
    assert "print ack: action=start code=200 state=printing" in log
    assert "alice-bracket" not in log


def test_a_logged_print_report_does_not_name_the_print(caplog, load_fixture):
    # A progress report carries the job's name under filename and, beside it, under
    # display_filename, in the paths of the stored job and of the gcode unpacked from it,
    # and as the model's name in source_info. With only `filename` on the key list, every
    # `report print:` line of a job still named the job.
    client = _logging_client(caplog)
    client._c.on_message(client._c, None, _msg(f"{REPORTS}/print/report", {
        "type": "print", "action": "start", "timestamp": 1700000000000,
        "msgid": "made-by-the-printer", "state": "printing", "code": 200, "msg": "",
        "data": load_fixture("print_progress.json")}))
    log = _logged(caplog)
    assert "report print:" in log
    for part in ("alice", "bracket"):
        assert part not in log.lower(), part
    # What a stalled job is debugged from is still on the line.
    for kept in ("'state': 'printing'", "'taskid': '-1'", "'progress': 42", "'curr_layer': 120",
                 "'total_layers': 900", "'print_time': 600", "'remain_time': 1800",
                 "'supplies_usage': 39832", "'software_version': '1.3.7'"):
        assert kept in log, kept


def test_a_print_ack_still_says_what_the_printer_answered(caplog):
    # The ack line is the instrument for issue #10: accepted or not, in one greppable line.
    client = _logging_client(caplog)
    client._c.on_message(client._c, None, _msg(f"{REPORTS}/print/report", {
        "type": "print", "action": "update", "state": "updated", "code": 200, "msg": "done",
        "data": {"taskid": "-1"}}))
    assert "print ack: action=update code=200 state=updated msg=done" in _logged(caplog)


def test_the_connect_line_does_not_name_the_host(caplog):
    client = _logging_client(caplog)
    client.connect()
    log = _logged(caplog)
    assert "connected" in log and "9883" in log
    assert LAN not in log


async def test_the_coordinator_hands_the_transport_the_entered_address(hass, caplog):
    # End to end across the seam: coordinator -> transport factory -> report line. The
    # user typed a name; the printer's handshake only ever reports an address.
    import logging
    from functools import partial

    from custom_components.anycubic.coordinator import AnycubicCoordinator

    caplog.set_level(logging.DEBUG, logger=m.__name__)
    coord = AnycubicCoordinator(
        hass, SECRET_HS, host="kobra-s1.local",
        transport_factory=partial(m.AnycubicMqtt, client_factory=FakeClient))
    await coord.async_start()
    paho = coord._transport._c
    paho.on_message(paho, None, _msg(f"{REPORTS}/info/report", {
        "type": "info", "action": "report", "data": {"state": "free",
                                                     "seen_as": "kobra-s1.local:18910"}}))
    await hass.async_block_till_done()
    log = _logged(caplog)
    assert "report info:" in log
    assert "kobra-s1.local" not in log


# ------------------------------------------------------- the running job, known by value

JOB = "0907-2001-Alice desk bracket _plate(01)_PLA_0.2_1h12m.gcode.3mf"
JOB_STEM = "0907-2001-Alice desk bracket _plate(01)_PLA_0.2_1h12m"


async def test_a_report_that_does_not_name_the_job_is_still_scrubbed_of_it(hass, caplog):
    # A `print` report names the job, and its own line is scrubbed from what it says itself.
    # A report of another type does not name it, so the line for one could only be scrubbed
    # by key, and a key nobody had seen let the name through. The coordinator knows what is
    # printing, and the transport asks it.
    import logging
    from functools import partial

    from custom_components.anycubic.coordinator import AnycubicCoordinator

    caplog.set_level(logging.DEBUG, logger=m.__name__)
    coord = AnycubicCoordinator(
        hass, SECRET_HS, transport_factory=partial(m.AnycubicMqtt, client_factory=FakeClient))
    await coord.async_start()
    paho = coord._transport._c
    paho.on_message(paho, None, _msg(f"{REPORTS}/print/report", {
        "type": "print", "action": "start", "state": "printing", "code": 200, "msg": "",
        "data": {"taskid": "-1", "progress": 5, "filename": JOB,
                 "job_label": "Alice_desk_bracket_.stl_id_0_copy_0"}}))
    await hass.async_block_till_done()
    assert coord.data.printer.filename == JOB
    paho.on_message(paho, None, _msg(f"{REPORTS}/fan/report", {
        "type": "fan", "action": "report", "data": {
            "fan_speed_pct": 40, "for_job": f"{JOB_STEM}.gcode", "part": "alice-desk-bracket"}}))
    await hass.async_block_till_done()
    log = _logged(caplog)
    assert "report print:" in log and "report fan:" in log
    assert "'fan_speed_pct': 40" in log and "'progress': 5" in log
    for part in ("alice", "bracket", "0907-2001"):
        assert part not in log.lower(), part


# ------------------------------------------ a line that cannot be written costs only itself
#
# The debug lines are written on paho's network thread, between a report arriving and its
# being applied. An exception there ends the thread: no more reports until the watchdog
# notices the silence and rebuilds the session.

def _deepest_payload_the_redactor_cannot_walk():
    """JSON that parses but nests deeper than the redactor can follow, or None."""
    import json

    from custom_components.anycubic.anycubic_local.const import redacted

    for depth in range(300, 3000, 25):
        text = '{"a":' * depth + "1" + "}" * depth
        try:
            nested = json.loads(text)
        except RecursionError:
            return None
        try:
            redacted(nested)
        except RecursionError:
            return nested
    return None


def test_a_report_nested_too_deep_to_redact_is_still_applied(caplog):
    import pytest

    nested = _deepest_payload_the_redactor_cannot_walk()
    if nested is None:
        pytest.skip("this interpreter parses no JSON deeper than the redactor can walk")
    seen = []
    client = _logging_client(caplog)
    client._on_report = lambda t, d: seen.append((t, d))
    client._c.on_message(client._c, None, _msg(f"{REPORTS}/info/report", {
        "type": "info", "action": "report", "data": {"state": "free", "deep": nested}}))
    # The report after it arrives too: the thread that delivers them is still running.
    client._c.on_message(client._c, None, _msg(f"{REPORTS}/fan/report", {
        "type": "fan", "action": "report", "data": {"fan_speed_pct": 40}}))
    assert [t for t, _ in seen] == ["info", "fan"]
    assert seen[0][1]["state"] == "free"
    log = _logged(caplog)
    assert "a report could not be logged" in log
    assert "report fan:" in log


def test_a_redactor_that_fails_logs_none_of_the_payload(caplog, monkeypatch):
    # Whatever goes wrong while a line is being made, the fallback line is fixed text.
    def broken(*args, **kwargs):
        raise ValueError(f"cannot redact {args!r}")

    monkeypatch.setattr(m, "redacted", broken)
    seen = []
    client = _logging_client(caplog)
    client._on_report = lambda t, d: seen.append((t, d))
    client._c.on_message(client._c, None, _msg(f"{REPORTS}/print/report", {
        "type": "print", "action": "start", "state": "printing", "code": 200, "msg": "",
        "data": {"taskid": "-1", "progress": 5, "filename": JOB, "ip": LAN}}))
    assert seen == [("print", {"taskid": "-1", "progress": 5, "filename": JOB, "ip": LAN})]
    log = _logged(caplog)
    assert "a report could not be logged" in log
    for secret in ("alice", LAN, DEVICE, "taskid", "print"):
        assert secret not in log.lower().replace("a report could not be logged", ""), secret


# ---------------------------------------------- a light command the printer did not carry out

def _light_answer(action, code, data):
    return {"type": "light", "action": action, "timestamp": 1700000000000,
            "msgid": "made-by-the-printer", "state": "failed" if code != 200 else "done",
            "code": code, "msg": "", "data": data}


def test_a_light_control_answer_that_reports_failure_is_not_passed_on(caplog):
    # A control answer carries the light object, and the coordinator reads that as the state
    # the light took. It is only handed `data`, so it cannot see that the envelope said the
    # command failed: a refused "off" would have been believed. No failure has been captured;
    # every answer seen has code 200, which is what the other acks use for "accepted".
    seen = []
    client = _logging_client(caplog)
    client._on_report = lambda t, d: seen.append((t, d))
    off = {"type": 2, "status": 0, "brightness": 0}
    for code in (500, 0, 400):
        client._c.on_message(client._c, None, _msg(f"{REPORTS}/light/report",
                                                   _light_answer("control", code, off)))
    assert seen == []
    assert "light command was not accepted" in _logged(caplog)
    # The answer to a command that worked is passed on as it always was...
    client._c.on_message(client._c, None, _msg(f"{REPORTS}/light/report",
                                               _light_answer("control", 200, off)))
    # ...and so is one that carries no code at all.
    bare = _light_answer("control", 200, off)
    del bare["code"]
    client._c.on_message(client._c, None, _msg(f"{REPORTS}/light/report", bare))
    assert seen == [("light", off), ("light", off)]


def test_only_a_light_control_answer_is_judged_by_its_code():
    # Every other report reaches the coordinator exactly as before, whatever its code: a
    # light QUERY answer, a `print` ack the firmware rejected, an `info`.
    seen = []
    client = m.AnycubicMqtt(SECRET_HS, on_report=lambda t, d: seen.append((t, d)),
                            client_factory=FakeClient)
    lights = {"lights": [{"type": 2, "status": 1, "brightness": 100}]}
    for tail, envelope in (
        ("light/report", _light_answer("query", 500, lights)),
        ("light/report", _light_answer("report", 0, lights)),
        ("print/report", {"type": "print", "action": "update", "state": "failed", "code": 500,
                          "msg": "", "data": {"taskid": "-1"}}),
        ("info/report", {"type": "info", "action": "report", "code": 500,
                         "data": {"state": "free"}}),
        ("video/report", {"type": "video", "action": "startCapture", "code": 500, "data": None}),
    ):
        client._c.on_message(client._c, None, _msg(f"{REPORTS}/{tail}", envelope))
    assert seen == [("light", lights), ("light", lights), ("print", {"taskid": "-1"}),
                    ("info", {"state": "free"}), ("video", {})]


def test_a_payload_that_is_not_a_json_object_is_ignored(caplog):
    # Valid JSON, and not a report: a list, a number, a string, null. Reading it as one
    # raised on paho's network thread, with or without debug logging, and ended the thread.
    import logging

    for level in (logging.DEBUG, logging.WARNING):
        caplog.set_level(level, logger=m.__name__)
        seen = []
        client = m.AnycubicMqtt(SECRET_HS, on_report=lambda t, d: seen.append((t, d)),
                                client_factory=FakeClient)
        for payload in ([1, 2, 3], [{"type": "info", "data": {"state": "free"}}], 7, "info", None,
                        True):
            client._c.on_message(client._c, None, _msg(f"{REPORTS}/info/report", payload))
        assert seen == []
        # The report after them is delivered: the thread is still there.
        client._c.on_message(client._c, None, _msg(f"{REPORTS}/info/report", {
            "type": "info", "action": "report", "data": {"state": "free"}}))
        assert seen == [("info", {"state": "free"})]


# --------------------------------------------------- the three paho constructors there are

def test_the_client_is_built_the_way_each_paho_wants_it(monkeypatch):
    # paho 1.x has no callback API version. 2.0.0 demands one, first and without a default,
    # and the transport could not be constructed on it at all. 2.1 made it optional again.
    made = []
    version_1 = paho.CallbackAPIVersion.VERSION1

    class Paho1(FakeClient):
        def __init__(self, client_id="", clean_session=None, userdata=None):
            made.append(("1.x", client_id)); super().__init__()

    class Paho200(FakeClient):
        def __init__(self, callback_api_version, client_id="", clean_session=None):
            made.append(("2.0.0", callback_api_version, client_id)); super().__init__()

    class Paho21(FakeClient):
        def __init__(self, callback_api_version=version_1, client_id=""):
            made.append(("2.1", callback_api_version, client_id)); super().__init__()

    for factory in (Paho200, Paho21):
        m.AnycubicMqtt(SECRET_HS, on_report=lambda t, d: None, client_factory=factory)
    monkeypatch.delattr(m.mqtt, "CallbackAPIVersion")           # what importing 1.x looks like
    m.AnycubicMqtt(SECRET_HS, on_report=lambda t, d: None, client_factory=Paho1)
    # Version 1 callbacks everywhere: what 1.x has, and what 2.1 has given us until now.
    assert [call[:-1] for call in made] == [
        ("2.0.0", version_1), ("2.1", version_1), ("1.x",)]
    assert all(call[-1].startswith("ha-") for call in made)


def test_the_real_paho_client_can_be_constructed():
    # Whichever paho is installed, with nothing faked.
    client = m.AnycubicMqtt(SECRET_HS, on_report=lambda t, d: None)
    assert client._c.on_message == client._handle


# --------------------------------------------------- a disconnect we asked for is not news

def _warnings(caplog):
    import logging
    return [r.getMessage() for r in caplog.records
            if r.name == m.__name__ and r.levelno >= logging.WARNING]


def test_our_own_disconnect_does_not_warn_that_the_connection_was_lost(caplog):
    # Every reload of the entry, and every recovery, closes the session on purpose. paho
    # then calls on_disconnect like for any other, and the log said the connection was lost.
    class Paho(FakeClient):
        def disconnect(self):
            super().disconnect()
            self.on_disconnect(self, None, 0)       # as paho does, from its own thread

    client = m.AnycubicMqtt(SECRET_HS, on_report=lambda t, d: None, client_factory=Paho)
    client.connect()
    client.disconnect()
    assert client.connected is False
    assert _warnings(caplog) == []


def test_a_disconnect_nobody_asked_for_still_warns(caplog):
    client = m.AnycubicMqtt(SECRET_HS, on_report=lambda t, d: None, client_factory=FakeClient)
    client.connect()
    client._c.on_disconnect(client._c, None, 7)
    assert _warnings(caplog) == ["printer broker connection lost; paho will auto-reconnect"]
    # A session closed on purpose and then opened again is back to warning: recovery
    # builds a new transport, but nothing here relies on that.
    client.disconnect()
    client.connect()
    caplog.clear()
    client._c.on_disconnect(client._c, None, 7)
    assert _warnings(caplog) == ["printer broker connection lost; paho will auto-reconnect"]
