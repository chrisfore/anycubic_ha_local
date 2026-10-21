# tests/test_job_reason.py
"""Why a job paused or stopped, and the print events built on it.

The envelopes below are as captured from a Kobra 3 (fw 2.4.1.9): a pause and a stop from
the slicer, and a clog. The reason rides the envelope (`code`, `msg`, `state`), never `data`.
"""
import json
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic.anycubic_local import mqtt as m
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult
from custom_components.anycubic.anycubic_local.models import JobReason, parse_reason
from custom_components.anycubic.const import DOMAIN
from custom_components.anycubic.coordinator import AnycubicCoordinator

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


def _info(state):
    """An info report whose project is in `state`; None for an idle printer."""
    if state is None:
        return {"state": "free", "project": None}
    return {"state": "busy", "project": {"state": state, "progress": 4, "pause": 0,
                                         "filename": "part.gcode"}}


class FakeTransport:
    def __init__(self, hs, on_report, **k): self.on_reason = k.get("on_reason")
    def connect(self): pass
    def disconnect(self): pass
    def query(self, t): pass
    def publish(self, topic, payload): pass


async def _coordinator(hass):
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    events = []
    coord.async_add_event_listener(lambda kind, attrs: events.append((kind, attrs)))
    return coord, events


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


async def test_the_coordinator_hands_the_transport_its_reason_callback(hass):
    coord, _ = await _coordinator(hass)
    assert coord._transport.on_reason == coord._on_reason


# -- coordinator: reason and events ------------------------------------------------------

async def test_a_clog_fires_one_error_then_paused_with_the_reason(hass):
    coord, events = await _coordinator(hass)
    coord._apply("info", _info("printing"))
    coord._apply_reason(parse_reason(CLOG_PAUSING))
    coord._apply_reason(parse_reason(CLOG_PAUSED))     # same fault, reported twice
    coord._apply("info", _info("paused"))
    await hass.async_block_till_done()
    assert [kind for kind, _ in events] == ["error", "paused"]
    for _, attrs in events:
        assert attrs["reason_code"] == 11518 and attrs["reason"] == "Filament is stuck"
        assert attrs["filename"] == "part.gcode"


async def test_a_pause_from_the_slicer_is_no_error(hass):
    coord, events = await _coordinator(hass)
    coord._apply("info", _info("printing"))
    coord._apply_reason(parse_reason(USER_PAUSING))
    coord._apply_reason(parse_reason(USER_PAUSED))
    coord._apply("info", _info("paused"))
    coord._apply("info", _info("resuming"))
    coord._apply("info", _info("printing"))
    await hass.async_block_till_done()
    assert [(kind, attrs.get("reason")) for kind, attrs in events] == [
        ("paused", "Paused by user"), ("resumed", None)]
    assert coord.data.reason is None                    # resumed: no longer applies


async def test_the_reason_survives_the_next_info_report(hass):
    # info rebuilds PrinterState and never carries the reason; it must not wipe it.
    coord, _ = await _coordinator(hass)
    coord._apply("info", _info("printing"))
    coord._apply_reason(parse_reason(CLOG_PAUSED))
    coord._apply("info", _info("paused"))
    coord._apply("info", _info("paused"))
    await hass.async_block_till_done()
    assert coord.data.reason.code == 11518


async def test_stopped_and_finished(hass):
    coord, events = await _coordinator(hass)
    coord._apply("info", _info(None))
    coord._apply("info", _info("preheating"))
    coord._apply("info", _info("printing"))
    coord._apply_reason(parse_reason(USER_STOPPING))
    coord._apply("info", _info("stopping"))
    coord._apply("info", _info("stoped"))
    coord._apply("info", _info(None))
    coord._apply("info", _info("printing"))
    coord._apply("info", _info(None))
    await hass.async_block_till_done()
    assert [(kind, attrs.get("reason")) for kind, attrs in events] == [
        ("started", None), ("stopped", "Stopped by user"), ("started", None),
        ("finished", None)]


async def test_starting_mid_print_is_not_a_new_job(hass):
    coord, events = await _coordinator(hass)
    coord._apply("info", _info("printing"))
    coord._apply("info", _info("printing"))
    await hass.async_block_till_done()
    assert events == []



async def test_a_busy_printer_with_no_job_fires_nothing(hass):
    # Drying in the ACE or loading filament: state busy, but no project or last_project,
    # so the status is the raw "busy". That is not a job starting or finishing.
    coord, events = await _coordinator(hass)
    coord._apply("info", _info(None))
    coord._apply("info", {"state": "busy"})
    coord._apply("info", _info(None))
    await hass.async_block_till_done()
    assert events == []

# -- entities ----------------------------------------------------------------------------

async def test_pause_reason_sensor_and_print_event_entity(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("info", _info("printing"))
        coord._apply_reason(parse_reason(CLOG_PAUSED))
        coord._apply("info", _info("paused"))
        await hass.async_block_till_done()

        reason = hass.states.get("sensor.anycubic_kobra_3_pause_reason")
        assert reason.state == "Filament is stuck"
        assert reason.attributes["code"] == 11518 and reason.attributes["by_user"] is False
        event = hass.states.get("event.anycubic_kobra_3_print_event")
        assert event.attributes["event_type"] == "paused"
        assert event.attributes["reason"] == "Filament is stuck"

        coord._apply("info", _info("printing"))
        await hass.async_block_till_done()
        assert hass.states.get("sensor.anycubic_kobra_3_pause_reason").state == "unknown"
        assert hass.states.get("event.anycubic_kobra_3_print_event").attributes[
            "event_type"] == "resumed"
