# tests/test_coordinator.py
import threading

from custom_components.anycubic.coordinator import AnycubicCoordinator
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult

HS = HandshakeResult("1.2.3.4", 9883, "u", "p", "DEV", "20029", "SER-1")


class FakeTransport:
    def __init__(self, hs, on_report, **k): self.on_report = on_report; self.queries = []
    def connect(self): self.connected = True
    def disconnect(self): self.connected = False
    def query(self, t): self.queries.append(t)


async def test_coordinator_applies_info_report(hass):
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._on_report("info", {"state": "busy", "model": "AnyCubic Kobra S1 Max",
                              "temp": {"curr_nozzle_temp": 210},
                              "project": {"state": "printing", "progress": 42, "pause": 0}})
    await hass.async_block_till_done()
    assert coord.data.printer.nozzle_temp == 210
    assert coord.data.printer.progress == 42
    assert coord.data.printer.printing is True


async def test_coordinator_merges_ace(hass):
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._on_report("multiColorBox", {"multi_color_box": [{"id": 0, "humidity": 24, "temp": 35, "slots": []}]})
    await hass.async_block_till_done()
    assert coord.data.ace[0].humidity == 24


async def test_tempature_push_does_not_blank_printer(hass):
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._apply("info", {"state": "busy", "temp": {"curr_nozzle_temp": 210},
                          "project": {"state": "printing", "progress": 42, "pause": 0}})
    await hass.async_block_till_done()
    coord._apply("tempature", {"curr_nozzle_temp": 230})   # raw temp dict, not an info envelope
    await hass.async_block_till_done()
    assert coord.data.printer.nozzle_temp == 210            # unchanged, NOT None
    assert coord.data.printer.progress == 42


async def test_on_report_applies_on_the_event_loop_thread(hass):
    # Reports arrive on the paho network thread. _apply calls async_set_updated_data,
    # which HA only allows on the event loop — running it anywhere else (e.g. an
    # executor thread) raises RuntimeError on every report and floods the log.
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    loop_thread = threading.current_thread()
    applied_on = []
    orig_apply = coord._apply

    def spy(msg_type, data):
        applied_on.append(threading.current_thread())
        orig_apply(msg_type, data)

    coord._apply = spy
    worker = threading.Thread(target=coord._on_report, args=("info", {"state": "free"}))
    worker.start()
    worker.join()
    await hass.async_block_till_done()
    assert applied_on, "_apply never ran"
    assert applied_on[0] is loop_thread
    assert coord.data.printer is not None


async def test_coordinator_queries_peripherie_once_at_connect(hass):
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    # peripherie (the capability inventory) is asked for at connect, alongside the normal poll types.
    assert "peripherie" in coord._transport.queries
    assert "info" in coord._transport.queries


async def test_coordinator_captures_capabilities(hass):
    coord = AnycubicCoordinator(hass, HS, transport_factory=FakeTransport)
    await coord.async_start()
    coord._on_report("info", {"state": "free", "model": "AnyCubic Kobra S1 Max",
                              "temp": {"curr_chamber_temp": 36},
                              "features": {"camera_timelapse_support": True, "fod_support": True}})
    coord._on_report("peripherie", {"camera": 1, "multiColorBox": 1, "udisk": 0})
    await hass.async_block_till_done()
    # The raw feature map and peripheral inventory are stashed verbatim for diagnostics / onboarding.
    assert coord.raw_features == {"camera_timelapse_support": True, "fod_support": True}
    assert coord.peripherie == {"camera": 1, "multiColorBox": 1, "udisk": 0}
    assert {"info", "peripherie"} <= coord.seen_report_types
