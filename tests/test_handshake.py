# tests/test_handshake.py
import base64
import hashlib
import json

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from custom_components.anycubic.anycubic_local import handshake


def test_sign_matches_reference_algorithm():
    token = "0123456789abcdefABCDEF0123456789"
    ts = 1781548658398
    nonce = "abc123"
    # sign = md5( md5(token[:16]) + str(ts) + nonce )  (hex; double-urlencode is a no-op on hex)
    expected = hashlib.md5(
        (hashlib.md5(token[:16].encode()).hexdigest() + str(ts) + nonce).encode()
    ).hexdigest()
    assert handshake.sign(token, ts, nonce) == expected
    assert len(handshake.sign(token, ts, nonce)) == 32  # hex digest
    # regression anchor: fixed expected value for these inputs — catches formula changes
    assert handshake.sign(token, ts, nonce) == "3dc9739a6e5de8f075c6999fe3c3aaef"


def _encrypt(plaintext: bytes, token: str, local_token: str) -> str:
    key = token[16:32].encode()
    iv = local_token.encode()[:16].ljust(16, b"\0")
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(enc.update(padded) + enc.finalize()).decode()


def test_decrypt_ctrl_roundtrip():
    token = "0123456789abcdef" + "FEDCBA9876543210"  # 32 chars; [16:32] is the key
    local_token = "localtok12345678"
    payload = {"broker": "mqtts://192.168.1.50:9883", "username": "u", "password": "p",
               "deviceId": "ea42a05c"}
    blob = _encrypt(json.dumps(payload).encode(), token, local_token)
    out = handshake.decrypt_ctrl(blob, token, local_token)
    assert out["broker"] == "mqtts://192.168.1.50:9883"
    assert out["deviceId"] == "ea42a05c"


def _enc(plaintext: bytes, token: str, local_token: str) -> str:
    key = token[16:32].encode(); iv = local_token.encode()[:16].ljust(16, b"\0")
    p = PKCS7(128).padder(); padded = p.update(plaintext) + p.finalize()
    e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(e.update(padded) + e.finalize()).decode()


def test_do_handshake_drives_full_flow():
    token = "0123456789abcdefABCDEF0123456789"; local_token = "localtok12345678"
    info = {"token": token, "cn": "SER-1", "modelId": "20029",
            "modelName": "Anycubic Kobra S1 Max", "deviceType": "fdm",
            "ctrlInfoUrl": "http://1.2.3.4:18910/ctrl", "ctrlType": "lan"}
    decrypted = {"broker": "mqtts://1.2.3.4:9883", "username": "u", "password": "p",
                 "deviceId": "DEV-1"}
    ctrl = {"code": 200, "message": "success",
            "data": {"token": local_token, "info": _enc(json.dumps(decrypted).encode(), token, local_token)}}

    calls = []
    def fake_fetch(method, url, **kw):
        calls.append((method, url))
        return info if url.endswith("/info") else ctrl

    res = handshake.do_handshake("1.2.3.4", fetch=fake_fetch)
    assert res.broker_host == "1.2.3.4" and res.broker_port == 9883
    assert res.username == "u" and res.password == "p"
    assert res.device_id == "DEV-1" and res.model_id == "20029" and res.serial == "SER-1"
    assert res.model_name == "Anycubic Kobra S1 Max" and res.device_type == "fdm"
    assert calls[0] == ("GET", "http://1.2.3.4:18910/info")
    assert calls[1][0] == "POST" and "/ctrl?" in calls[1][1] and "sign=" in calls[1][1]


# ------------------------------------------- an answer is only ever taken from the address entered
#
# /info is answered by whatever is at the address the user typed, and it says where to send
# the second, signed request. Followed as given, that is any device on the network telling
# Home Assistant to POST to a host of its choosing. Every address here is made up.

TOKEN = "0123456789abcdefABCDEF0123456789"
LOCAL_TOKEN = "localtok12345678"
ENTERED = "192.168.1.50"


def _info(ctrl_url, **over):
    return {"token": TOKEN, "cn": "SER-1", "modelId": "20029", "ctrlType": "lan",
            "ctrlInfoUrl": ctrl_url, **over}


def _ctrl(decrypted=None):
    if decrypted is None:
        decrypted = {"broker": f"mqtts://{ENTERED}:9883", "username": "u", "password": "p",
                     "deviceId": "DEV-1"}
    return {"code": 200, "message": "success", "data": {
        "token": LOCAL_TOKEN, "info": _enc(json.dumps(decrypted).encode(), TOKEN, LOCAL_TOKEN)}}


def _second_request(host, ctrl_url):
    calls = []

    def fetch(method, url, **kw):
        calls.append((method, url))
        return _info(ctrl_url) if len(calls) == 1 else _ctrl()

    handshake.do_handshake(host, fetch=fetch)
    assert len(calls) == 2 and calls[1][0] == "POST"
    return calls[1][1]


def test_the_second_request_goes_to_the_entered_address_whatever_the_printer_says():
    import urllib.parse

    for supplied in ("http://203.0.113.9:18910/ctrl", "http://evil.example/ctrl",
                     "http://user:pw@203.0.113.9:18910/ctrl", "https://203.0.113.9:18910/ctrl",
                     "http://203.0.113.9:18910/ctrl#frag", "file:///ctrl", "//203.0.113.9/ctrl",
                     "http://[2001:db8::1]:18910/ctrl"):
        url = urllib.parse.urlsplit(_second_request(ENTERED, supplied))
        assert (url.scheme, url.hostname, url.username) == ("http", ENTERED, None), supplied
        assert url.port == 18910, supplied
        assert url.path == "/ctrl" and not url.fragment, supplied
        assert "203.0.113.9" not in url.geturl() and "evil" not in url.geturl(), supplied


def test_the_second_request_keeps_the_port_path_and_query_the_printer_gave():
    import urllib.parse

    # The usual answer: the printer's own address, which is where the request went anyway.
    assert _second_request(ENTERED, f"http://{ENTERED}:18910/ctrl").startswith(
        f"http://{ENTERED}:18910/ctrl?ts=")
    # Entered by name: the printer answers with its address, the request still uses the name.
    assert _second_request("kobra-s1.local", f"http://{ENTERED}:18910/ctrl").startswith(
        "http://kobra-s1.local:18910/ctrl?ts=")
    url = urllib.parse.urlsplit(_second_request(ENTERED, "http://203.0.113.9:8443/api/v2/ctrl?a=1"))
    assert (url.hostname, url.port, url.path) == (ENTERED, 8443, "/api/v2/ctrl")
    query = urllib.parse.parse_qs(url.query)
    assert query["a"] == ["1"] and {"ts", "nonce", "sign", "did"} <= set(query)
    # No port given: the one the first request used, not whatever the scheme defaults to.
    # The printer answers there, and port 80 on the same host is some other service.
    for portless in ("http://203.0.113.9/ctrl", "https://203.0.113.9/ctrl", "/ctrl", "ctrl"):
        assert _second_request(ENTERED, portless).startswith(f"http://{ENTERED}:18910/ctrl?ts="), \
            portless


def test_a_redirect_is_not_followed(socket_enabled):
    # The same hole by another door: answering /info or /ctrl with a redirect would send
    # the request, and for /ctrl the signature with it, to wherever the answer names.
    import http.server
    import threading

    import pytest

    asked = []

    class Device(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            asked.append(self.path)
            if self.path == "/info":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/elsewhere")
                self.end_headers()
            else:
                body = b'{"token": "not for us"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Device)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(OSError):        # what every caller already treats as unreachable
            handshake._http_fetch("GET", f"http://127.0.0.1:{server.server_port}/info")
    finally:
        server.shutdown()
        server.server_close()
    assert asked == ["/info"]


# ------------------------------------------------- a device that answers, but is not a printer
#
# Port 18910 is not the printer's alone, and an address is easily mistyped. Whatever answers
# there instead must fail as a handshake that failed (HandshakeError, which every caller
# turns into "could not reach the printer"), not as an AttributeError or a KeyError with a
# traceback that quotes the address.

def _wrong_answers():
    good = _info(f"http://{ENTERED}:18910/ctrl")
    return {
        "info is a list": ([], None),
        "info is a string": ("ok", None),
        "info is null": (None, None),
        "token is not text": ({**good, "token": 12345}, None),
        "ctrl url is not text": ({**good, "ctrlInfoUrl": ["http://x/ctrl"]}, None),
        "ctrl url cannot be parsed": ({**good, "ctrlInfoUrl": "http://[::1/ctrl"}, None),
        "ctrl url has a port that is not one": ({**good, "ctrlInfoUrl": "http://x:port/ctrl"}, None),
        "serial is missing": ({k: v for k, v in good.items() if k != "cn"}, _ctrl()),
        "serial is empty": ({**good, "cn": ""}, _ctrl()),
        "serial is a number": ({**good, "cn": 12345}, _ctrl()),
        "serial is a list": ({**good, "cn": ["SER-1"]}, _ctrl()),
        "model id is not an id": ({**good, "modelId": {"id": 20029}}, _ctrl()),
        "ctrl is a list": (good, []),
        "ctrl is null": (good, None),
        "ctrl has no data": (good, {"code": 200}),
        "ctrl data is null": (good, {"code": 200, "data": None}),
        "ctrl data is a list": (good, {"code": 200, "data": ["info", "token"]}),
        "ctrl data is empty": (good, {"code": 200, "data": {}}),
        "ctrl data is not text": (good, {"code": 200, "data": {"info": 1, "token": 2}}),
        "unsealed data is a list": (good, _ctrl(["broker"])),
        "unsealed data has no broker": (good, _ctrl({"username": "u", "password": "p",
                                                    "deviceId": "DEV-1"})),
        "unsealed broker is not a broker": (good, _ctrl({"broker": "somewhere", "username": "u",
                                                        "password": "p", "deviceId": "DEV-1"})),
        "unsealed broker is not text": (good, _ctrl({"broker": 9883, "username": "u",
                                                    "password": "p", "deviceId": "DEV-1"})),
        "unsealed data has no credentials": (good, _ctrl({"broker": f"mqtts://{ENTERED}:9883"})),
    }


def test_an_answer_of_the_wrong_shape_is_a_failed_handshake():
    import pytest

    from custom_components.anycubic.anycubic_local.exceptions import HandshakeError

    for what, (info, ctrl) in _wrong_answers().items():
        answers = iter((info, ctrl))
        with pytest.raises(HandshakeError) as err:
            handshake.do_handshake(ENTERED, fetch=lambda method, url, **kw: next(answers))
        # And it does not say where: the text ends up in Home Assistant's ordinary log.
        assert ENTERED not in str(err.value), what


class _Answer:
    """What the opener hands back for one request: a body, read once."""
    def __init__(self, body): self._body = body
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def _opener_gives(monkeypatch, outcome):
    """Make every request end in `outcome`: bytes for a body, or the exception it raises."""
    import urllib.request

    def open_(self, request, *args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return _Answer(outcome)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_)


def test_a_reply_that_is_not_the_printers_json_is_a_failed_handshake(monkeypatch):
    import http.client

    import pytest

    from custom_components.anycubic.anycubic_local.exceptions import HandshakeError

    for outcome in (b"<html><body>router login</body></html>", b"", b"\xff\xfe\x00not text",
                    http.client.BadStatusLine("SSH-2.0-OpenSSH"), http.client.IncompleteRead(b""),
                    http.client.RemoteDisconnected("closed without an answer"),
                    http.client.InvalidURL(f"nonnumeric port: '{ENTERED}'"),
                    UnicodeError("encoding with 'idna' codec failed (label too long)")):
        _opener_gives(monkeypatch, outcome)
        with pytest.raises((HandshakeError, OSError)) as err:
            handshake.do_handshake(ENTERED)
        assert ENTERED not in str(err.value), outcome


def test_a_model_name_or_device_type_that_is_not_text_is_dropped_not_fatal():
    # The serial becomes the entry's unique id, so one that is not text is refused (above).
    # These two are only ever shown, and a printer is not turned away over a label.
    answers = iter((_info(f"http://{ENTERED}:18910/ctrl", modelName=["Kobra"], deviceType=7,
                          modelId=20029), _ctrl()))
    res = handshake.do_handshake(ENTERED, fetch=lambda method, url, **kw: next(answers))
    assert (res.model_name, res.device_type) == (None, None)
    assert (res.serial, res.model_id) == ("SER-1", "20029")
