import json
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic.const import DOMAIN
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult
from custom_components.anycubic.anycubic_local.mqtt import AnycubicMqtt

HS = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER-1")
ENTITY = "light.anycubic_kobra_s1_max_chamber_light"


class FakeTransport:
    def __init__(self, hs, on_report, **k):
        pass

    def connect(self):
        pass

    def disconnect(self):
        pass

    def query(self, t):
        pass

    def publish(self, t, p):
        pass


async def test_light_state_and_control(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("light", {"lights": [{"type": 2, "status": 1, "brightness": 100}]})
        await hass.async_block_till_done()

        st = hass.states.get(ENTITY)
        assert st is not None and st.state == "on"
        # On/off light (chamber LED is not dimmable) — no brightness control.
        assert st.attributes.get("supported_color_modes") == ["onoff"]
        assert "brightness" not in st.attributes

        # Turn off: the command is sent AND the state flips immediately (optimistic),
        # ahead of the printer's own confirmation about 0.2s later (issue #14).
        coord.async_send_command = AsyncMock()
        await hass.services.async_call(
            "light", "turn_off", {"entity_id": ENTITY}, blocking=True)
        coord.async_send_command.assert_awaited_with("light", on=False)
        await hass.async_block_till_done()
        assert hass.states.get(ENTITY).state == "off"

        # Turn back on: optimistic state returns to on without a new report.
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": ENTITY}, blocking=True)
        coord.async_send_command.assert_awaited_with("light", on=True)
        await hass.async_block_till_done()
        assert hass.states.get(ENTITY).state == "on"


# ------------------------------------------- the printer's own confirmation (issue #14)
#
# The test above never delivers a report after a command, which is how this went unseen:
# about 0.2s after a light command the printer sends a `light` report of its own, and the
# entity has to survive it. Everything below therefore runs on the REAL transport with
# only the paho client faked, so each report takes the path it takes live: full envelope
# into AnycubicMqtt._handle, `data` on to the coordinator, state out to the entity.

REPORTS = "anycubic/anycubicCloud/v1/printer/public/20029/DEV"
# The light object as the printer sends it, in a command's `data` and in reports alike.
ON = {"type": 2, "status": 1, "brightness": 100}
OFF = {"type": 2, "status": 0, "brightness": 0}


class FakePaho:
    """The paho client and nothing above it. `pubs` is what we put on the wire."""
    def __init__(self, *a, **k):
        self.pubs = []
        self.on_message = None; self.on_connect = None; self.on_disconnect = None
    def username_pw_set(self, u, p): pass
    def tls_set(self, **k): pass
    def tls_insecure_set(self, v): pass
    def connect(self, h, port, keepalive=60):
        self.on_connect(self, None, {}, 0)      # paho fires this once the CONNACK is in
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): pass
    def subscribe(self, t): pass
    def publish(self, t, payload): self.pubs.append((t, json.loads(payload)))


async def _setup(hass):
    """Set the integration up and return the fake paho client the real transport drives."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt",
               partial(AnycubicMqtt, client_factory=FakePaho)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry.runtime_data._transport._c


def _printer_sends(paho, tail, payload):
    """Deliver one message from the printer the way paho does: into AnycubicMqtt._handle."""
    paho.on_message(paho, None, SimpleNamespace(
        topic=f"{REPORTS}/{tail}", payload=json.dumps(payload).encode()))


def _light_report(action, data):
    """A `light` report envelope as captured. The msgid is the printer's own: a report
    never carries the msgid of the command it answers, only .../response does."""
    return {"type": "light", "action": action, "timestamp": 1700000000000,
            "msgid": "made-by-the-printer", "state": "done", "code": 200, "msg": "done",
            "data": data}


async def test_light_stays_on_when_the_printer_confirms_the_command(hass):
    # The bug as reported: on, off again 0.2s later, then on at the next poll 30s after.
    paho = await _setup(hass)
    await hass.services.async_call("light", "turn_on", {"entity_id": ENTITY}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "on"            # optimistic: no answer yet

    sent = [p for _, p in paho.pubs if p["action"] == "control"]
    assert [p["data"] for p in sent] == [ON]
    # The printer acks our msgid on .../response, then answers on light/report with the
    # bare light object carrying the state the light took. Not a `lights` list.
    _printer_sends(paho, "response", {"msgid": sent[0]["msgid"]})
    _printer_sends(paho, "light/report", _light_report("control", ON))
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY).state == "on"


async def test_light_follows_a_command_sent_by_another_client(hass):
    # Report topics are shared between clients: the answer to a light command from the
    # Slicer or the phone app reaches our subscription too, with nothing sent by us.
    paho = await _setup(hass)
    assert hass.states.get(ENTITY).state == "off"

    _printer_sends(paho, "light/report", _light_report("control", ON))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "on"

    _printer_sends(paho, "light/report", _light_report("control", OFF))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "off"


async def test_an_unrecognised_light_report_leaves_the_state_alone(hass):
    # Unknown is not "off": a `light` report in neither shape, or about another lamp, must
    # not move an entity whose last real reading said the light is on.
    paho = await _setup(hass)
    _printer_sends(paho, "light/report", _light_report("query", {"lights": [ON]}))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "on"

    for data in ({}, {"type": 2}, {"type": 1, "status": 0, "brightness": 0}):
        _printer_sends(paho, "light/report", _light_report("report", data))
        await hass.async_block_till_done()
        assert hass.states.get(ENTITY).state == "on", data
