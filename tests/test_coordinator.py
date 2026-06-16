# tests/test_coordinator.py
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
