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
    assert calls[0] == ("GET", "http://1.2.3.4:18910/info")
    assert calls[1][0] == "POST" and "/ctrl?" in calls[1][1] and "sign=" in calls[1][1]
