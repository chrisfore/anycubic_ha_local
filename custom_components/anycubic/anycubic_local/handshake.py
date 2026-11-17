"""LAN-Mode handshake: GET /info -> signed POST /ctrl -> AES-CBC decrypt."""
import base64
import hashlib
import http.client
import json
import random
import re
import string
import time
import urllib.error
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
        kind = type(err).__name__
    # Raised out here, not inside the `except`, so the new error is not chained to the old
    # one: a JSON error carries the text it could not read, which is the unsealed answer.
    # The kind of error is all that is kept.
    raise HandshakeError(_UNREADABLE, step=STEP_DECRYPT, reason=REASON_UNREADABLE,
                         detail={"error": kind})


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
# up in Home Assistant's ordinary log. The others are fixed for the same reason.
_NOT_A_PRINTER = "The device at this address did not answer the way a printer does."
_NOT_OFFERED = ("This device does not offer the LAN connection this integration uses. "
                "Older Kobra 2 firmware is not supported.")
_CLOUD_MODE = "The printer is in cloud mode. Turn on LAN Mode on the printer."
_REFUSED = "The printer refused the request."
_UNREADABLE = "The printer's answer could not be read."

# How far setup got when it failed. An error carries one of these as `step`; an OSError
# from a request carries it as `handshake_step`.
STEP_INFO = "info"
STEP_CTRL = "ctrl"
STEP_DECRYPT = "decrypt"
STEP_SERIAL = "serial"

# Which failure it was, as an error's `reason`.
REASON_NOT_A_PRINTER = "not_a_printer"
REASON_NOT_OFFERED = "not_offered"
REASON_CLOUD_MODE = "cloud_mode"
REASON_NO_SERIAL = "no_serial"
REASON_REFUSED = "refused"
REASON_UNREADABLE = "unreadable"

# The /info values that say what kind of machine this is and nothing about which one. Every
# other text value in /info is treated as something that must not be repeated.
_NOT_SECRET_INFO = frozenset(("modelId", "modelName", "deviceType", "ctrlType"))

# Device text is shown only if it reads like words. A run this long of the characters keys,
# digests, base64 and percent-escapes are made of does not.
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9+/=%_-]{16,}")
# Nor does a network address, whichever device's it is: an IPv4 address or a MAC.
_AN_ADDRESS = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}|(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")
_WINDOW = 8             # a piece this long of anything secret is enough to drop the text
_SHORT_SECRET = 4       # below this a value is too short to tell from ordinary text
_TEXT_LIMIT = 80


def safe_device_text(value, secrets=()) -> str | None:
    """Text the device sent, fit to repeat in Home Assistant's ordinary log; None if it is not.

    A printer's refusal says why in words of its own, and that is the most useful thing a
    failed setup can report. It is also whatever the device at that address chose to send,
    and a device can echo back what it was just sent. So, in this order: everything but
    plain printable ASCII is taken out (which also puts back together anything split by a
    control character); text holding a long unbroken run of key-like characters, a network
    address, or any piece of a value in `secrets`, is dropped whole rather than masked; and
    only then is it cut to length, so nothing can hide across the cut.
    """
    if not isinstance(value, str):
        return None
    text = "".join(c for c in value if " " <= c <= "~").strip()
    if not text or _OPAQUE_RUN.search(text) or _AN_ADDRESS.search(text):
        return None
    lowered = text.lower()
    for secret in secrets:
        if not isinstance(secret, str):
            continue
        secret = secret.lower()
        if len(secret) < _SHORT_SECRET:
            continue
        if len(secret) <= _WINDOW:
            if secret in lowered:
                return None
        elif any(secret[i:i + _WINDOW] in lowered for i in range(len(secret) - _WINDOW + 1)):
            return None
    return text[:_TEXT_LIMIT]


def _secrets_of(host: str, info: dict) -> list[str]:
    """Everything known so far that device text must not repeat."""
    found = [host]
    for key, value in info.items():
        if key in _NOT_SECRET_INFO:
            continue
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, int) and not isinstance(value, bool):
            found.append(str(value))
    token = info.get("token")
    if isinstance(token, str):
        found.append(hashlib.md5(token[:16].encode()).hexdigest())
    mac = _parse_mac(info.get("usn"))
    if mac:
        found += [mac.replace(":", "-"), mac.replace("-", ":")]
    supplied = info.get("ctrlInfoUrl")
    if isinstance(supplied, str):
        try:
            name = urllib.parse.urlsplit(supplied).hostname
        except ValueError:
            name = None
        if name:
            found.append(name)
    return found


def _model_of(info: dict, secrets) -> dict:
    """The model, as far as it can be said safely: `model_id` and `model_name`, each optional.

    The id only if it is a short number, which is all a model id ever is. The name is the
    device's own text, so it goes through safe_device_text.
    """
    about = {}
    model_id = info.get("modelId")
    if isinstance(model_id, int) and not isinstance(model_id, bool):
        model_id = str(model_id)
    if isinstance(model_id, str) and model_id.isascii() and model_id.isdigit() \
            and len(model_id) <= 8:
        about["model_id"] = model_id
    name = safe_device_text(info.get("modelName"), secrets)
    if name:
        about["model_name"] = name
    return about


def _small_int(value) -> int | None:
    """A code worth printing: a whole number of ordinary size, and not True or False."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) < 1_000_000:
        return value
    return None


def _ask(fetch, step: str, method: str, url: str, about: dict, secrets):
    """One request, with what failed and where written on whatever it raises.

    An OSError stays the OSError it was, so every caller's `except` means what it did. It is
    given `handshake_step` and `handshake_detail`. For an HTTP status that detail holds the
    status and the reason phrase, cleaned here: the error itself also holds the address
    that was asked, query and all, so nothing reads it after this.
    """
    try:
        return fetch(method, url)
    except HandshakeError as err:
        if err.step is None:
            err.step, err.reason = step, REASON_NOT_A_PRINTER
            err.detail = {**about, **err.detail}
        raise
    except OSError as err:
        detail = dict(about)
        if isinstance(err, urllib.error.HTTPError):
            status = _small_int(err.code)
            if status is not None:
                detail["status"] = status
            phrase = safe_device_text(err.reason, secrets)
            if phrase:
                detail["status_text"] = phrase
        err.handshake_step = step
        err.handshake_detail = detail
        raise

# Where a printer answers /info, and /ctrl unless it says otherwise.
_INFO_PORT = 18910


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
    info = _ask(fetch, STEP_INFO, "GET", f"http://{host}:{_INFO_PORT}/info", {}, (host,))
    if not isinstance(info, dict):
        raise HandshakeError(_NOT_A_PRINTER, step=STEP_INFO, reason=REASON_NOT_A_PRINTER)
    secrets = _secrets_of(host, info)
    about = _model_of(info, secrets)
    if info.get("ctrlType") == "cloud":
        raise CloudModeError(_CLOUD_MODE, step=STEP_INFO, reason=REASON_CLOUD_MODE,
                             detail=about)
    token = info.get("token")
    if not token or not info.get("ctrlInfoUrl") or not info.get("modelId"):
        # Older Kobra 2 firmware (and some others) answer /info without these: they use a
        # different way in, which this integration does not speak.
        raise HandshakeError(_NOT_OFFERED, step=STEP_INFO, reason=REASON_NOT_OFFERED,
                             detail=about)
    # The serial becomes the config entry's unique id and part of every entity's, so it has
    # to be text and it has to be there: 12345 and "12345" would be two different printers,
    # and a printer with no serial cannot be told from any other. The model id goes into
    # every topic. The model's name and the device type are only ever shown, so one that is
    # not text is dropped rather than held against the printer.
    serial, model_id = info.get("cn"), info["modelId"]
    if not (isinstance(token, str)
            and isinstance(model_id, (str, int)) and not isinstance(model_id, bool)):
        raise HandshakeError(_NOT_A_PRINTER, step=STEP_INFO, reason=REASON_NOT_A_PRINTER,
                             detail=about)
    if not (isinstance(serial, str) and serial):
        raise HandshakeError(_NOT_A_PRINTER, step=STEP_SERIAL, reason=REASON_NO_SERIAL,
                             detail=about)
    ts = int(time.time() * 1000)
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=6))
    did = "".join(random.choices(string.ascii_uppercase + string.digits, k=32))
    signed = sign(token, ts, nonce)
    qs = urllib.parse.urlencode({"ts": ts, "nonce": nonce, "sign": signed, "did": did})
    try:
        url = _ctrl_url(host, info["ctrlInfoUrl"], qs)
    except HandshakeError as err:
        # Still the first answer's fault: it is /info that said where to go next.
        err.step, err.reason, err.detail = STEP_INFO, REASON_NOT_A_PRINTER, dict(about)
        raise
    # What this request carries is the first thing a device could echo back.
    secrets += [signed, nonce, did, str(ts)]
    ctrl = _ask(fetch, STEP_CTRL, "POST", url, about, secrets)
    if not isinstance(ctrl, dict):
        raise HandshakeError(_NOT_A_PRINTER, step=STEP_CTRL, reason=REASON_NOT_A_PRINTER,
                             detail=about)
    sealed = ctrl.get("data")
    if ctrl.get("code") != 200:
        if isinstance(sealed, dict):
            secrets += [v for v in sealed.values() if isinstance(v, str)]
        detail = dict(about)
        code = _small_int(ctrl.get("code"))
        if code is not None:
            detail["code"] = code
        # The printer's own words for the refusal. Under "message" in every answer seen;
        # "msg" is read as well in case a firmware spells it that way.
        said = (safe_device_text(ctrl.get("message"), secrets)
                or safe_device_text(ctrl.get("msg"), secrets))
        if said:
            detail["message"] = said
        raise HandshakeError(_REFUSED, step=STEP_CTRL, reason=REASON_REFUSED, detail=detail)
    if not (isinstance(sealed, dict) and isinstance(sealed.get("info"), str)
            and isinstance(sealed.get("token"), str)):
        raise HandshakeError(_NOT_A_PRINTER, step=STEP_CTRL, reason=REASON_NOT_A_PRINTER,
                             detail=about)
    try:
        data = decrypt_ctrl(sealed["info"], token, sealed["token"])
    except HandshakeError as err:
        err.detail = {**about, **err.detail}
        raise
    if not isinstance(data, dict):
        raise HandshakeError(_NOT_A_PRINTER, step=STEP_DECRYPT, reason=REASON_NOT_A_PRINTER,
                             detail=about)
    broker = data.get("broker")
    m = re.match(r"mqtts?://([^:]+):(\d+)", broker) if isinstance(broker, str) else None
    if m is None or not all(key in data for key in ("username", "password", "deviceId")):
        raise HandshakeError(_NOT_A_PRINTER, step=STEP_DECRYPT, reason=REASON_NOT_A_PRINTER,
                             detail=about)
    return HandshakeResult(
        broker_host=m.group(1), broker_port=int(m.group(2)),
        username=data["username"], password=data["password"],
        device_id=data["deviceId"], model_id=str(model_id), serial=serial,
        mac=_parse_mac(info.get("usn")),
        model_name=_text_or_none(info.get("modelName")),
        device_type=_text_or_none(info.get("deviceType")))


def _text_or_none(value) -> str | None:
    return value if isinstance(value, str) else None


def _ctrl_url(host: str, supplied, query: str) -> str:
    """Where the signed request goes: the printer's path and port, on the host the user entered.

    ctrlInfoUrl is whatever the device at the entered address chose to send. Used as given,
    any device on the network that answers on port 18910 could have Home Assistant POST to a
    host of its choosing. A printer gives its own address here, so the host is always the one
    the first request went to, and the scheme the one it used. The port, path and any query
    are kept: they say where on that host, and that host already had the first request.
    Given no port, it is the first request's again, not the scheme's default: the printer is
    known to answer on 18910, and port 80 of the same host is some other service.
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
    return "".join(("http://", host, f":{_INFO_PORT if port is None else port}", path, "?",
                    f"{parts.query}&{query}" if parts.query else query))
