"""LAN-Mode handshake: GET /info -> signed POST /ctrl -> AES-CBC decrypt."""
import base64
import hashlib
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


def _http_fetch(method: str, url: str, timeout: float = 6.0) -> dict:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def do_handshake(host: str, fetch=_http_fetch) -> HandshakeResult:
    """Run GET /info -> signed POST /ctrl -> AES decrypt. `fetch(method,url)` returns parsed JSON
    (injected for tests). Blocking; call via an executor in HA."""
    info = fetch("GET", f"http://{host}:18910/info")
    if info.get("ctrlType") == "cloud":
        raise CloudModeError("Printer is in CLOUD mode — enable LAN Mode")
    token = info.get("token")
    if not token or not info.get("ctrlInfoUrl") or not info.get("modelId"):
        # Older Kobra 2 firmware (and some others) use a different unsigned handshake we don't speak.
        raise HandshakeError(
            "This printer doesn't use the signed LAN handshake this integration needs "
            "(Kobra 3 / S1 generation). Kobra 2 / Kobra X aren't supported yet.")
    ts = int(time.time() * 1000)
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=6))
    did = "".join(random.choices(string.ascii_uppercase + string.digits, k=32))
    qs = urllib.parse.urlencode({"ts": ts, "nonce": nonce, "sign": sign(token, ts, nonce), "did": did})
    ctrl = fetch("POST", f"{info['ctrlInfoUrl']}?{qs}")
    if ctrl.get("code") != 200:
        raise HandshakeError(f"/ctrl failed: {ctrl.get('message')}")
    data = decrypt_ctrl(ctrl["data"]["info"], token, ctrl["data"]["token"])
    m = re.match(r"mqtts?://([^:]+):(\d+)", data["broker"])
    return HandshakeResult(
        broker_host=m.group(1), broker_port=int(m.group(2)),
        username=data["username"], password=data["password"],
        device_id=data["deviceId"], model_id=str(info["modelId"]), serial=info.get("cn", ""),
        mac=_parse_mac(info.get("usn")),
        model_name=info.get("modelName"), device_type=info.get("deviceType"))
