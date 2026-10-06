"""LAN-Mode handshake: GET /info -> signed POST /ctrl -> AES-CBC decrypt."""
import base64
import hashlib
import http.client
import json
import random
import re
import string
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .exceptions import CloudModeError, HandshakeError


def sign(token: str, ts: int, nonce: str) -> str:
    """sign = md5(md5(token[:16]) + str(ts) + nonce). Returns the 32-char hex digest."""
    first = hashlib.md5(token[:16].encode()).hexdigest()
    return hashlib.md5((first + str(ts) + nonce).encode()).hexdigest()


def decrypt_ctrl(info_b64: str, token: str, local_token: str) -> dict:
    """AES-CBC decrypt the /ctrl `data.info` blob. key=token[16:32], IV=local_token (pad/trunc 16)."""
    key = token[16:32].encode()
    iv = local_token.encode()[:16].ljust(16, b"\0")
    try:
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = dec.update(base64.b64decode(info_b64)) + dec.finalize()
        unpadder = PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        return json.loads(plaintext.decode())
    except Exception as err:  # noqa: BLE001
        raise HandshakeError(f"ctrl decrypt failed: {err}") from err


@dataclass(frozen=True)
class HandshakeResult:
    broker_host: str
    broker_port: int
    username: str
    password: str
    device_id: str
    model_id: str
    serial: str
    mac: str | None = None          # from /info "usn" (e.g. "uuid:fdm:AA-BB-CC-DD-EE-FF")
    model_name: str | None = None   # from /info "modelName" (e.g. "Anycubic Kobra S1 Max")
    device_type: str | None = None  # from /info "deviceType" (e.g. "fdm")


def _parse_mac(usn) -> str | None:
    if not usn:
        return None
    m = re.search(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", str(usn))
    return m.group(0) if m else None


# What a caller is told when something answered at the address and it was not a printer's
# answer. Fixed text, with nothing of the answer and nothing of the address in it: this ends
# up in Home Assistant's ordinary log.
_NOT_A_PRINTER = "The device at this address did not answer the way a printer does."


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so a request ends at the host it was sent to.

    A redirect is the device at the entered address naming another host, exactly as
    ctrlInfoUrl does (see do_handshake). Returning None makes urllib raise the 3xx as an
    HTTPError, which is an OSError: unreachable, to every caller.
    """
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _http_fetch(method: str, url: str, timeout: float = 6.0) -> dict:
    req = urllib.request.Request(url, method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (http.client.HTTPException, ValueError) as err:
        # Something is listening there and is not a printer: a body that is not JSON or not
        # text (both ValueError), or not HTTP at all. ValueError also covers the UnicodeError
        # an address raises when it cannot be a hostname. None of these is an OSError, so
        # they used to pass every caller's `except` and surface as an unknown error with a
        # traceback. Only these three lines are covered, all of them the library's.
        raise HandshakeError(_NOT_A_PRINTER) from err


def do_handshake(host: str, fetch=_http_fetch) -> HandshakeResult:
    """Run GET /info -> signed POST /ctrl -> AES decrypt. `fetch(method,url)` returns parsed JSON
    (injected for tests). Blocking; call via an executor in HA.

    Both answers come from whatever is at `host`, which may not be a printer at all. Each is
    checked for the shape it must have before it is read, and one that does not have it is a
    HandshakeError. The checks are spelled out one by one, rather than the reading being
    wrapped in an `except` for KeyError, TypeError and AttributeError, so that a mistake in
    this code still fails as itself.
    """
    info = fetch("GET", f"http://{host}:18910/info")
    if not isinstance(info, dict):
        raise HandshakeError(_NOT_A_PRINTER)
    if info.get("ctrlType") == "cloud":
        raise CloudModeError("Printer is in CLOUD mode — enable LAN Mode")
    token = info.get("token")
    if not token or not info.get("ctrlInfoUrl") or not info.get("modelId"):
        # Older Kobra 2 firmware (and some others) use a different unsigned handshake we don't speak.
        raise HandshakeError(
            "This printer doesn't use the signed LAN handshake this integration needs "
            "(Kobra 3 / S1 generation). Kobra 2 / Kobra X aren't supported yet.")
    if not isinstance(token, str):
        raise HandshakeError(_NOT_A_PRINTER)
    ts = int(time.time() * 1000)
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=6))
    did = "".join(random.choices(string.ascii_uppercase + string.digits, k=32))
    qs = urllib.parse.urlencode({"ts": ts, "nonce": nonce, "sign": sign(token, ts, nonce), "did": did})
    ctrl = fetch("POST", _ctrl_url(host, info["ctrlInfoUrl"], qs))
    if not isinstance(ctrl, dict):
        raise HandshakeError(_NOT_A_PRINTER)
    if ctrl.get("code") != 200:
        raise HandshakeError(f"/ctrl failed: {ctrl.get('message')}")
    sealed = ctrl.get("data")
    if not (isinstance(sealed, dict) and isinstance(sealed.get("info"), str)
            and isinstance(sealed.get("token"), str)):
        raise HandshakeError(_NOT_A_PRINTER)
    data = decrypt_ctrl(sealed["info"], token, sealed["token"])
    if not isinstance(data, dict):
        raise HandshakeError(_NOT_A_PRINTER)
    broker = data.get("broker")
    m = re.match(r"mqtts?://([^:]+):(\d+)", broker) if isinstance(broker, str) else None
    if m is None or not all(key in data for key in ("username", "password", "deviceId")):
        raise HandshakeError(_NOT_A_PRINTER)
    return HandshakeResult(
        broker_host=m.group(1), broker_port=int(m.group(2)),
        username=data["username"], password=data["password"],
        device_id=data["deviceId"], model_id=str(info["modelId"]), serial=info.get("cn", ""),
        mac=_parse_mac(info.get("usn")),
        model_name=info.get("modelName"), device_type=info.get("deviceType"))


def _ctrl_url(host: str, supplied, query: str) -> str:
    """Where the signed request goes: the printer's path and port, on the host the user entered.

    ctrlInfoUrl is whatever the device at the entered address chose to send. Used as given,
    any device on the network that answers on port 18910 could have Home Assistant POST to a
    host of its choosing. A printer gives its own address here, so the host is always the one
    the first request went to, and the scheme the one it used. The port, path and any query
    are kept: they say where on that host, and that host already had the first request.
    """
    if not isinstance(supplied, str):
        raise HandshakeError(_NOT_A_PRINTER)
    try:
        parts = urllib.parse.urlsplit(supplied)
        port = parts.port
    except ValueError as err:
        # Not a URL: a malformed host, or a port that is not a number.
        raise HandshakeError(_NOT_A_PRINTER) from err
    path = parts.path if parts.path.startswith("/") else f"/{parts.path}"
    return "".join(("http://", host, "" if port is None else f":{port}", path, "?",
                    f"{parts.query}&{query}" if parts.query else query))
