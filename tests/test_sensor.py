# tests/test_sensor.py
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic.const import DOMAIN
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult

HS = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER-1")


async def test_sensors_created_and_valued(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)

    class FakeTransport:
        def __init__(s, hs, on_report, **k): s.on_report = on_report
        def connect(s): pass
        def disconnect(s): pass
        def query(s, t): pass
        def publish(s, t, p): pass

    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("info", {"state": "busy", "temp": {"curr_nozzle_temp": 211},
                              "project": {"state": "printing", "progress": 55, "pause": 0}})
        await hass.async_block_till_done()

    st = hass.states.get("sensor.anycubic_kobra_s1_max_nozzle_temperature")
    assert st is not None and st.state == "211"
    prog = hass.states.get("sensor.anycubic_kobra_s1_max_progress")
    assert prog.state == "55"
    printing = hass.states.get("binary_sensor.anycubic_kobra_s1_max_printing")
    assert printing.state == "on"


async def test_unload(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-2", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)

    class FT:
        def __init__(s, hs, on_report, **k): pass
        def connect(s): pass
        def disconnect(s): pass
        def query(s, t): pass
        def publish(s, t, p): pass

    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", FT):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)


class _FakeTransport:
    def __init__(s, hs, on_report, **k): s.on_report = on_report
    def connect(s): pass
    def disconnect(s): pass
    def query(s, t): pass
    def publish(s, t, p): pass


async def test_external_spool_appears_only_once_the_printer_reports_one(hass):
    # Issue #12: the printer sends `extfilbox` only while no ACE is attached, so the
    # entity must be created on first report — pre-creating it would leave every ACE
    # owner with a permanently unavailable entity.
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", _FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        assert hass.states.get("sensor.anycubic_kobra_s1_max_external_spool") is None

        coord._apply("extfilbox", {"type": "PETG", "color": [117, 120, 123],
                                   "loaded": 1, "status_type": 3, "current_status": 10})
        await hass.async_block_till_done()

    st = hass.states.get("sensor.anycubic_kobra_s1_max_external_spool")
    assert st is not None
    assert st.state == "PETG"
    assert st.attributes["color"] == "#75787B"
    assert st.attributes["loaded"] is True
    assert st.attributes["status"] == 10


async def test_external_spool_shows_empty_when_nothing_is_loaded(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", _FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entry.runtime_data._apply("extfilbox", {"type": "", "color": [], "loaded": 0,
                                                "status_type": 0, "current_status": 0})
        await hass.async_block_till_done()

    st = hass.states.get("sensor.anycubic_kobra_s1_max_external_spool")
    assert st.state == "Empty"
    assert st.attributes["loaded"] is False


async def test_external_spool_clears_when_the_ace_comes_back(hass):
    # Issue #12: reconnecting the ACE sends NO final extfilbox — reports just stop, so the
    # sensor would hold a stale spool forever. Confirmed from the reporter's log: switching
    # the ACE on produced a non-empty multiColorBox and nothing else. That report is the signal.
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", _FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("extfilbox", {"type": "PETG", "color": [117, 120, 123], "loaded": 1,
                                   "status_type": 3, "current_status": 10})
        await hass.async_block_till_done()
        coord._apply("multiColorBox", {"multi_color_box": [
            {"id": 0, "model_id": 40001, "humidity": 20, "temp": 28, "slots": []}]})
        await hass.async_block_till_done()

    assert coord.data.external_spool is None
    st = hass.states.get("sensor.anycubic_kobra_s1_max_external_spool")
    assert st.state == "unavailable"


async def test_empty_ace_report_leaves_the_external_spool_alone(hass):
    # An empty multi_color_box is the NO-ACE case — exactly when the bare spool is in use.
    # Clearing on it would blank the sensor on every 30s poll.
    entry = MockConfigEntry(domain=DOMAIN, unique_id="SER-1", data={"host": "1.2.3.4"})
    entry.add_to_hass(hass)
    with patch("custom_components.anycubic.do_handshake", return_value=HS), \
         patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", _FakeTransport):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data
        coord._apply("extfilbox", {"type": "PETG", "color": [117, 120, 123], "loaded": 1,
                                   "status_type": 3, "current_status": 10})
        coord._apply("multiColorBox", {"multi_color_box": []})
        await hass.async_block_till_done()

    assert coord.data.external_spool is not None
    assert hass.states.get("sensor.anycubic_kobra_s1_max_external_spool").state == "PETG"
