# tests/test_job_reason.py
"""Why a job paused or stopped, and the print events built on it.

The envelopes below are as captured from a Kobra 3 (fw 2.4.1.9): a pause and a stop from
the slicer, and a clog. The reason rides the envelope (`code`, `msg`, `state`), never `data`.
"""
import json

from custom_components.anycubic.anycubic_local import mqtt as m
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult
from custom_components.anycubic.anycubic_local.models import JobReason, parse_reason

HS = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20024", "SER-1")
PROGRESS = {"curr_layer": 2, "filename": "part.gcode", "print_time": 2, "progress": 4,
            "remain_time": 19, "taskid": "-1", "total_layers": 70}


def _print(action, state, code, msg):
    return {"type": "print", "action": action, "timestamp": 1, "msgid": "x",
            "state": state, "code": code, "msg": msg, "data": dict(PROGRESS)}


USER_PAUSING = _print("pause", "pausing", 10401, "用户发起")
USER_PAUSED = _print("pause", "paused", 10401, "用户发起")
USER_STOPPING = _print("stop", "stopping", 10601, "用户发起")
CLOG_PAUSING = _print("pause", "pausing", 11518, "Filament is stuck")
CLOG_PAUSED = _print("pause", "paused", 11518, "Filament is stuck")


# -- parse_reason ------------------------------------------------------------------------

def test_user_pause_and_stop_are_named_and_marked_as_the_users():
    assert parse_reason(USER_PAUSED) == JobReason(10401, "Paused by user", "paused", True)
    assert parse_reason(USER_STOPPING) == JobReason(10601, "Stopped by user", "stopping", True)


def test_a_clog_is_a_fault():
    reason = parse_reason(CLOG_PAUSED)
    assert reason == JobReason(11518, "Filament is stuck", "paused", False)
    assert reason.paused


def test_codes_from_anycubics_wiki_are_named_and_are_faults():
    # The printer's msg is the Chinese text; the catalogue names it in English.
    tangle = parse_reason(_print("pause", "paused", 11519, "缠料"))
    assert tangle == JobReason(11519, "Filament tangle detected", "paused", False)
    assert parse_reason(_print("pause", "paused", 11801, "x")).reason == (
        "Spaghetti detected, print paused")


def test_an_uncatalogued_code_passes_the_printers_message_through():
    assert parse_reason(_print("pause", "paused", 10999, "堵料")).reason == "堵料"
    assert parse_reason(_print("stop", "stoped", 10998, "")).reason == "code 10998"


def test_reports_that_give_no_reason():
    # A progress push, a command ack, a phase that is no pause or stop, a code that is not
    # a number (True is an int to Python, and no code).
    assert parse_reason(_print("start", "printing", 200, "done")) is None
    assert parse_reason(_print("pause", "paused", 200, "done")) is None
    assert parse_reason(_print("start", "printing", 10401, "用户发起")) is None
    assert parse_reason(_print("pause", "paused", "10401", "x")) is None
    assert parse_reason(_print("pause", "paused", True, "x")) is None
    assert parse_reason({"type": "print", "state": "paused"}) is None


# -- transport ---------------------------------------------------------------------------

class FakeClient:
    def __init__(self, *a, **k): self.on_message = None; self.on_connect = None
    def username_pw_set(self, u, p): pass
    def tls_set(self, **k): pass
    def tls_insecure_set(self, v): pass


def _msg(payload):
    class M:
        topic = "anycubic/anycubicCloud/v1/printer/public/20024/DEV/print/report"
        def __init__(s): s.payload = json.dumps(payload).encode()
    return M()


def test_the_transport_hands_over_the_reason_and_still_the_report():
    reasons, reports = [], []
    t = m.AnycubicMqtt(HS, on_report=lambda kind, data: reports.append(kind),
                       client_factory=FakeClient, on_reason=reasons.append)
    t._handle(None, None, _msg(CLOG_PAUSED))
    t._handle(None, None, _msg(_print("start", "printing", 200, "done")))
    assert reasons == [JobReason(11518, "Filament is stuck", "paused", False)]
    assert reports == ["print", "print"]


def test_a_transport_with_no_reason_callback_works_as_before():
    reports = []
    t = m.AnycubicMqtt(HS, on_report=lambda kind, data: reports.append(kind),
                       client_factory=FakeClient)
    t._handle(None, None, _msg(CLOG_PAUSED))
    assert reports == ["print"]
