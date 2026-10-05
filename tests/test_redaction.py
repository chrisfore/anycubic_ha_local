# tests/test_redaction.py
#
# The one redactor. The debug log and the diagnostics download both get pasted straight into
# public issues, so every case below is something that must not be in either, or something
# triage cannot do without. Every identifier here is made up.
import copy

from custom_components.anycubic.anycubic_local import const
from custom_components.anycubic.anycubic_local.handshake import HandshakeResult

MASK = "**REDACTED**"
HOST = "192.168.1.50"
ENTERED = "kobra-s1.local"
DEVICE = "0123456789abcdef0123456789abcdef"
SERIAL = "SERIAL-TEST-0001"
MAC = "AA-BB-CC-DD-EE-FF"
TOKEN = "feedfacefeedfacefeedfacefeedface"
HS = HandshakeResult(HOST, 9883, "mqtt-user-1", "mqtt-pass-1", DEVICE, "20029", SERIAL, mac=MAC)
IDS = (DEVICE, HOST, SERIAL, MAC, "AA:BB:CC:DD:EE:FF", ENTERED)


# ---------------------------------------------------------------------------- URL values

def test_a_url_keeps_scheme_port_and_path_and_loses_everything_else():
    # The `info` report's `urls` block: rtspUrl is the printer's LAN address, fileUploadurl
    # is the address plus an `s=` token. Scheme, port and path are what make a camera that
    # will not stream debuggable (issue #6), so those three stay.
    for url, safe in (
        (f"http://{HOST}:18088/flv", f"http://{MASK}:18088/flv"),
        (f"http://{HOST}:18910/gcode_upload?s={TOKEN}", f"http://{MASK}:18910/gcode_upload?{MASK}"),
        (f"rtsp://user:pass@{HOST}:8554/streaming/live/1", f"rtsp://{MASK}:8554/streaming/live/1"),
        (f"mqtts://{HOST}:9883", f"mqtts://{MASK}:9883"),
        ("http://printer.local/x#s=abc", f"http://{MASK}/x#{MASK}"),
        ("http://[fe80::1]:18088/flv", f"http://{MASK}:18088/flv"),
    ):
        assert const.redacted({"u": url}) == {"u": safe}, url


def test_a_port_that_is_not_a_port_is_dropped_rather_than_kept():
    # An unbracketed IPv6 host reads as host "fe80" with port ":1". Keeping whatever sits
    # in the port position would hand back a piece of the address.
    assert const.redacted("http://fe80::1/flv") == f"http://{MASK}/flv"
    assert const.redacted(f"http://{HOST}:99999/flv") == f"http://{MASK}/flv"


def test_a_url_that_cannot_be_parsed_is_hidden_whole():
    # It looked enough like a URL for the parser to object to its host. There is no safe
    # part to keep.
    assert const.redacted("http://[fe80::1/flv") == MASK


def test_a_firmware_version_is_not_mistaken_for_an_address():
    # "2.7.1.4" is a firmware version and has exactly the shape of an IPv4 address, which
    # is why URLs are recognised by parsing and never by a dotted-number pattern.
    plain = {"version": "2.7.1.4", "time": "12:30:05", "note": "PLA: 0.28", "msg": "done",
             "path": "/useremain/app/gk", "usn": "uuid:fdm:unit"}
    assert const.redacted(plain) == plain


def test_the_urls_block_keeps_its_shape():
    # Masking the whole `urls` key would be simpler and would throw away the port and path.
    out = const.redacted({"urls": {"rtspUrl": f"http://{HOST}:18088/flv",
                                   "fileUploadurl": f"http://{HOST}:18910/gcode_upload?s={TOKEN}"}})
    assert out == {"urls": {"rtspUrl": f"http://{MASK}:18088/flv",
                            "fileUploadurl": f"http://{MASK}:18910/gcode_upload?{MASK}"}}


def test_a_long_url_is_masked_before_it_is_cut_short():
    # Truncation keeps the head of a string, and the head of a URL is its host.
    url = f"http://{HOST}:18088/live/{'a' * 200}?s={TOKEN}"
    out = const.redacted({"u": url})["u"]
    assert HOST not in out
    assert out.startswith(f"http://{MASK}:18088/live/")
    assert "truncated" in out


# ------------------------------------------------------------------------------ key names

def test_printer_name_and_skipped_object_names_are_masked_by_key():
    # printerName is whatever the owner typed into the printer. objects_skip_parts are
    # object names derived from the model file's name, the same class as a file name.
    out = const.redacted({"printerName": "Alice's Kobra", "model": "Anycubic Kobra S1 Max",
                          "file_details": {"objects_skip_parts": ["alice_bracket.stl_id_0_copy_0"],
                                           "root": "local"}})
    assert out == {"printerName": MASK, "model": "Anycubic Kobra S1 Max",
                   "file_details": {"objects_skip_parts": MASK, "root": "local"}}


# --------------------------------------------------------------------------- exact values

def test_runtime_identifiers_are_scrubbed_wherever_they_appear():
    # None of these keys is on the list, and none of the values is a URL. A new firmware
    # can put an identifier anywhere; the ones we already hold are the ones we can catch.
    out = const.redacted({"cn": SERIAL, "bind": f"bound to {DEVICE} ok",
                          "note": f"ssh root@{HOST}", "seen_as": f"{ENTERED}:18910",
                          "nested": [{"deep": f"<{SERIAL}>"}]}, IDS)
    assert out == {"cn": MASK, "bind": f"bound to {MASK} ok", "note": f"ssh root@{MASK}",
                   "seen_as": f"{MASK}:18910", "nested": [{"deep": f"<{MASK}>"}]}


def test_identifiers_are_matched_whatever_their_case_and_the_mac_in_both_forms():
    # The handshake gives the MAC upper-case with dashes; Home Assistant's device registry
    # holds it lower-case with colons.
    out = const.redacted({"a": "uuid:fdm:AA-BB-CC-DD-EE-FF", "b": "aa:bb:cc:dd:ee:ff",
                          "c": SERIAL.lower(), "d": DEVICE.upper()}, IDS)
    assert out == {"a": f"uuid:fdm:{MASK}", "b": MASK, "c": MASK, "d": MASK}


def test_an_identifier_used_as_a_key_is_scrubbed_too():
    out = const.redacted({"devices": {DEVICE: {"temp": 30}}}, IDS)
    assert out == {"devices": {MASK: {"temp": 30}}}


def test_a_candidate_shorter_than_seven_characters_is_left_alone():
    # A value that short is ordinary text as often as it is an identifier, and scrubbing it
    # mangles every accidental match. Seven is the shortest an IPv4 address can be.
    text = {"msg": "DEV board SER-1 ok", "addr": "1.2.3.4"}
    assert const.redacted(text, ("DEV", "SER-1", "1.2.3.4")) == {
        "msg": "DEV board SER-1 ok", "addr": MASK}


def test_a_single_identifier_handed_over_bare_still_counts():
    # A string is iterable. Taken one character at a time, every piece would be too short
    # to scrub and the call would silently do nothing.
    assert const.redacted({"note": f"at {ENTERED}"}, ENTERED) == {"note": f"at {MASK}"}


def test_the_longer_of_two_overlapping_identifiers_wins():
    # The entered address and the broker address can share a prefix. Tried shortest first,
    # 192.168.1.50 would come out as "**REDACTED**0".
    out = const.redacted({"a": "192.168.1.50", "b": "192.168.1.5"},
                         ("192.168.1.5", "192.168.1.50"))
    assert out == {"a": MASK, "b": MASK}


def test_an_identifier_is_scrubbed_before_a_long_string_is_cut_short():
    out = const.redacted({"blob": f"id={SERIAL};" + "x" * 300}, IDS)["blob"]
    assert SERIAL not in out
    assert "truncated" in out


def test_runtime_identifiers_are_the_handshake_ids_and_the_entered_address():
    ids = const.runtime_identifiers(HS, ENTERED)
    assert {DEVICE, HOST, SERIAL, "AA-BB-CC-DD-EE-FF", "AA:BB:CC:DD:EE:FF", ENTERED} <= set(ids)
    # The broker credentials are never in a payload, and scrubbing a short username out of
    # every string would mangle text for nothing. Their KEYS stay masked.
    assert "mqtt-user-1" not in ids and "mqtt-pass-1" not in ids
    assert const.redacted({"username": "mqtt-user-1", "password": "mqtt-pass-1"}, ids) == {
        "username": MASK, "password": MASK}


def test_runtime_identifiers_skip_what_the_handshake_did_not_give():
    bare = HandshakeResult(HOST, 9883, "u", "p", DEVICE, "20029", "")      # no serial, no MAC
    assert all(const.runtime_identifiers(bare, None))


# ------------------------------------------------------------------- copy, never mutation

def test_redaction_returns_a_deep_copy_and_leaves_its_input_alone():
    payload = {"type": "info", "data": {"ip": HOST, "cn": SERIAL, "temp": {"curr_nozzle_temp": 93},
                                        "urls": {"rtspUrl": f"http://{HOST}:18088/flv"},
                                        "slots": [{"index": 0, "color": [1, 2, 3]}]}}
    before = copy.deepcopy(payload)
    out = const.redacted(payload, IDS)
    assert payload == before
    assert out["data"]["temp"] == {"curr_nozzle_temp": 93}
    # Nothing is shared: a caller that edits the copy cannot reach the original.
    assert out["data"]["temp"] is not payload["data"]["temp"]
    assert out["data"]["slots"][0]["color"] is not payload["data"]["slots"][0]["color"]


def test_every_container_is_walked_not_only_dict_and_list():
    # A container passed through untouched is one whose contents nobody looked at.
    from types import MappingProxyType

    out = const.redacted({"seen": {f"unit-{SERIAL}"}, "pair": (HOST, 1),
                          "frozen": MappingProxyType({"ip": HOST, "cn": SERIAL})}, IDS)
    assert out == {"seen": [f"unit-{MASK}"], "pair": [MASK, 1],
                   "frozen": {"ip": MASK, "cn": MASK}}


# --------------------------------------------------------------- what triage still needs

def test_an_info_report_stays_readable_apart_from_what_identifies(load_fixture):
    data = load_fixture("info_report.json")
    expected = copy.deepcopy(data)
    expected["printerName"] = MASK
    expected["ip"] = MASK
    expected["project"]["filename"] = MASK
    expected["urls"]["rtspUrl"] = f"http://{MASK}:18088/flv"
    # Everything else is untouched: model, version, state, every temperature, fan, speed,
    # progress, layer and time field, and the feature booleans.
    assert const.redacted(data, IDS) == expected


def test_an_ace_report_stays_readable_in_full(load_fixture):
    # Slot sku, type, colour, status and percent are the point of the ACE diagnostics.
    data = load_fixture("multicolorbox_full.json")
    assert const.redacted(data, IDS) == data


def test_a_file_report_keeps_root_and_plate_index(load_fixture):
    data = load_fixture("file_details.json")
    expected = copy.deepcopy(data)
    expected["filename"] = MASK
    expected["plate_name"] = MASK
    expected["file_details"]["objects_skip_parts"] = MASK
    assert const.redacted(data, IDS) == expected


def test_a_print_report_loses_the_prints_name_and_nothing_else(load_fixture):
    # A `print` progress report says what is printing five times: under filename, under
    # display_filename, in the paths of the stored job and of the gcode unpacked from it,
    # and as the model's own name in source_info, which the file name is built from. Only
    # `filename` was masked, so the name went out beside it in every `report print:` line.
    data = load_fixture("print_progress.json")
    out = const.redacted(data, IDS)
    for part in ("alice", "bracket"):
        assert part not in str(out).lower(), part
    expected = copy.deepcopy(data)
    for key in ("filename", "display_filename", "origin3mf", "temp_gcode"):
        expected[key] = MASK
    expected["source_info"]["models"][0]["name"] = MASK
    # Everything else is untouched: task id, progress, layers, times, filament used, the
    # slicer and its version, the plate, the rest of the model entry, and temp_dir, which is
    # a staging directory and names nothing.
    assert out == expected


def test_an_ack_envelope_stays_readable_in_full():
    ack = {"type": "print", "action": "update", "timestamp": 1700000000000,
           "msgid": "made-by-the-printer", "state": "updated", "code": 200, "msg": "done",
           "data": {"taskid": "-1", "settings": {"target_nozzle_temp": 210, "print_speed_mode": 2}}}
    assert const.redacted(ack, IDS) == ack
