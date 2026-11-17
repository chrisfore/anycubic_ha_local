# tests/test_setup_failure.py
#
# A setup that fails says which step failed and why: as one of five form errors, and as one
# line in Home Assistant's log (issue #23, where every cause was "Could not reach the
# printer" and the log was empty). The line is there to be pasted into a bug report, so the
# second half of this file is about what must never be in it.
#
# The REAL handshake runs throughout, against a faked network, so the whole path is the one
# a user takes. Every value below is made up.
import base64
import binascii
import hashlib
import http.client
import io
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anycubic.anycubic_local.exceptions import CloudModeError, HandshakeError
from custom_components.anycubic.anycubic_local.handshake import do_handshake, safe_device_text
from custom_components.anycubic.const import DOMAIN
from custom_components.anycubic.setup_failure import describe_failure

HOST = "192.168.77.61"
TOKEN = "Zq7Lw2Xk9Rt4Vb8NcH3mJ6pYd1Gf5SaE"
SERIAL = "SNKX44556677889900"
MAC = "A4-5B-C6-11-22-33"
USN = f"uuid:fdm:{MAC}"
CTRL_NAME = "printer-kx.lan.example"
SESSION = "sessIV0123456789"                      # the second answer's own token
BROKER_USER, BROKER_PASSWORD, DEVICE_ID = "userQW12ER34TY", "passZX98CV76BN", "devid5566778899aabb"
FIRST_HALF_DIGEST = hashlib.md5(TOKEN[:16].encode()).hexdigest()

FLOW_LOGGER = "custom_components.anycubic.config_flow"


def _info(**changes):
    info = {"token": TOKEN, "cn": SERIAL, "usn": USN, "modelId": "20030",
            "modelName": "Anycubic Kobra X", "deviceType": "fdm", "ctrlType": "lan",
            "ip": HOST, "ctrlInfoUrl": f"http://{CTRL_NAME}:18910/ctrl",
            "fileUploadurl": f"http://{HOST}:18910/upload"}
    info.update(changes)
    return {k: v for k, v in info.items() if v is not None}


def _sealed(plaintext: bytes) -> str:
    key, iv = TOKEN[16:32].encode(), SESSION.encode()[:16].ljust(16, b"\0")
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(enc.update(padded) + enc.finalize()).decode()


def _accepted(plaintext: bytes | None = None):
    if plaintext is None:
        plaintext = json.dumps({"broker": f"mqtts://{HOST}:9883", "username": BROKER_USER,
                                "password": BROKER_PASSWORD, "deviceId": DEVICE_ID}).encode()
    return {"code": 200, "message": "success",
            "data": {"info": _sealed(plaintext), "token": SESSION}}


def _http_error(url, status, phrase):
    return urllib.error.HTTPError(url, status, phrase, http.client.HTTPMessage(), io.BytesIO(b""))


class Net:
    """A faked network: what /info answers and what /ctrl answers.

    Each is a value to send as JSON, an exception to raise, or a function of the request's
    URL returning either. The URLs that were asked are kept, so a test can read the real
    query of the second request and have the device send it back.
    """

    def __init__(self, info, ctrl=None):
        self.info, self.ctrl, self.asked = info, ctrl, []

    @property
    def query(self) -> dict:
        second = [url for url in self.asked if not url.endswith("/info")]
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(second[-1]).query))

    def _answer(self, url):
        self.asked.append(url)
        outcome = self.info if url.endswith("/info") else self.ctrl
        if callable(outcome):
            outcome = outcome(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode()

    @contextmanager
    def up(self):
        net = self

        class Answer:
            def __init__(self, body): self._body = body
            def read(self): return self._body
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        def open_(self, request, *args, **kwargs):
            return Answer(net._answer(request.full_url))

        self.asked = []
        with patch.object(urllib.request.OpenerDirector, "open", open_), \
             patch("custom_components.anycubic.coordinator.mqtt_mod.AnycubicMqtt", _Transport):
            yield self


class _Transport:
    def __init__(self, hs, on_report, **k): pass
    def connect(self): pass
    def disconnect(self): pass
    def query(self, t): pass
    def publish(self, t, p): pass


def _refusal(message, code=10004, key="message"):
    return {"code": code, key: message, "data": None}


KOBRA_X = "(model 20030, Anycubic Kobra X)"

# name -> (the network, the form error, what the log line says after "Printer setup failed at ")
FAILURES = {
    "nothing listening": (
        lambda: Net(ConnectionRefusedError(111, "Connection refused")),
        "cannot_connect", "step info: no connection (ConnectionRefusedError, errno 111)"),
    "no answer in time": (
        lambda: Net(urllib.error.URLError(TimeoutError("timed out"))),
        "cannot_connect", "step info: no connection (TimeoutError)"),
    "a name that does not resolve": (
        lambda: Net(urllib.error.URLError(OSError(-2, "Name or service not known"))),
        "cannot_connect", "step info: no connection (OSError, errno -2)"),
    "the connection drops at the second request": (
        lambda: Net(_info(), ConnectionResetError(104, "Connection reset by peer")),
        "cannot_connect",
        f"step ctrl: no connection (ConnectionResetError, errno 104) {KOBRA_X}"),
    "cloud mode": (
        lambda: Net(_info(ctrlType="cloud")),
        "cloud_mode", f"step info: the printer is in cloud mode {KOBRA_X}"),
    "an HTTP error from the first request": (
        lambda: Net(lambda url: _http_error(url, 404, "Not Found")),
        "unsupported_printer", 'step info: HTTP status 404 "Not Found"'),
    "a web page": (
        lambda: Net(b"<html>router login</html>"),
        "unsupported_printer", "step info: the answer was not a printer's"),
    "a printer with another way in": (
        lambda: Net(_info(token=None, ctrlInfoUrl=None, modelId="20021")),
        "unsupported_printer",
        "step info: the device does not offer the connection this integration uses "
        "(model 20021, Anycubic Kobra 2 Pro)"),
    "no serial": (
        lambda: Net(_info(cn=None)),
        "unsupported_printer", f"step serial: the printer gave no serial number {KOBRA_X}"),
    "an HTTP error from the second request": (
        lambda: Net(_info(), lambda url: _http_error(url, 403, "Forbidden")),
        "handshake_failed", f'step ctrl: HTTP status 403 "Forbidden" {KOBRA_X}'),
    "the printer refuses": (
        lambda: Net(_info(), _refusal("sign error")),
        "handshake_failed",
        f'step ctrl: the printer refused the request, code 10004, message "sign error" {KOBRA_X}'),
    "the printer refuses, spelled msg": (
        lambda: Net(_info(), _refusal("not allowed", code=401, key="msg")),
        "handshake_failed",
        f'step ctrl: the printer refused the request, code 401, message "not allowed" {KOBRA_X}'),
    "the printer refuses without saying why": (
        lambda: Net(_info(), {"code": "nope"}),
        "handshake_failed", f"step ctrl: the printer refused the request {KOBRA_X}"),
    "the second answer is a web page": (
        lambda: Net(_info(), b"<html>"),
        "handshake_failed", f"step ctrl: the answer was not a printer's {KOBRA_X}"),
    "an answer that cannot be unsealed": (
        lambda: Net(_info(), {"code": 200, "data": {"info": "AAAA", "token": SESSION}}),
        "handshake_failed", "step decrypt: the printer's answer could not be read (ValueError) "
        + KOBRA_X),
    "an unsealed answer that is not what a printer sends": (
        lambda: Net(_info(), _accepted(b'{"broker": 5}')),
        "handshake_failed", f"step decrypt: the answer was not a printer's {KOBRA_X}"),
}


async def _start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})


async def _start_for(hass, source, entry):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": source, "entry_id": entry.entry_id}, data=entry.data)


async def _submit(hass, result, data):
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    await hass.async_block_till_done()
    return result


def _entry(hass, host="1.2.3.4"):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, data={"host": host})
    entry.add_to_hass(hass)
    return entry


def _setup_lines(caplog):
    return [r for r in caplog.records if "Printer setup failed" in r.getMessage()]


# ----------------------------------------------------------------- which error, which line

@pytest.mark.parametrize("name", FAILURES)
async def test_each_cause_has_its_own_error_and_log_line_when_adding(hass, caplog, name):
    net, error, said = FAILURES[name]
    with net().up():
        result = await _submit(hass, await _start(hass), {"host": HOST})
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert not hass.config_entries.async_entries(DOMAIN)
    (line,) = [r for r in _setup_lines(caplog) if r.name == FLOW_LOGGER]
    assert line.getMessage() == f"Printer setup failed at {said}"
    # The level Home Assistant's log shows with nothing switched on, and no traceback.
    assert line.levelno == logging.WARNING and line.exc_info is None
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("name", FAILURES)
async def test_each_cause_has_its_own_error_and_log_line_when_reconfiguring(hass, caplog, name):
    net, error, said = FAILURES[name]
    entry = _entry(hass)
    with net().up():
        result = await _submit(
            hass, await _start_for(hass, config_entries.SOURCE_RECONFIGURE, entry), {"host": HOST})
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert entry.data == {"host": "1.2.3.4"}
    assert f"Printer setup failed at {said}" in [r.getMessage() for r in _setup_lines(caplog)]


@pytest.mark.parametrize("name", FAILURES)
async def test_each_cause_has_its_own_error_and_log_line_when_reconnecting(hass, caplog, name):
    net, error, said = FAILURES[name]
    entry = _entry(hass, HOST)
    with net().up():
        result = await _submit(
            hass, await _start_for(hass, config_entries.SOURCE_REAUTH, entry), {})
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": error}
    assert f"Printer setup failed at {said}" in [r.getMessage() for r in _setup_lines(caplog)]


@pytest.mark.parametrize("name", FAILURES)
async def test_a_setup_that_fails_at_start_up_says_the_same_line(hass, caplog, name):
    net, error, said = FAILURES[name]
    entry = _entry(hass, HOST)
    with net().up():
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.reason == f"Printer setup failed at {said}"
    reauth = any(f["context"]["source"] == config_entries.SOURCE_REAUTH
                 for f in hass.config_entries.flow.async_progress())
    assert reauth == (error == "cloud_mode")


async def test_an_address_that_is_not_an_address_has_its_own_error_and_no_log_line(hass, caplog):
    with Net(_info(), _accepted()).up() as net:
        result = await _submit(hass, await _start(hass), {"host": f"http://{HOST}"})
    assert result["errors"] == {"base": "invalid_host"}
    assert not net.asked and not _setup_lines(caplog)


async def test_a_printer_of_a_model_not_known_here_is_still_set_up(hass, caplog):
    # "Not a supported printer" is for an answer that cannot be used. A printer that
    # answers properly is set up whatever its model number, as it always was.
    with Net(_info(modelId="29999", modelName="Anycubic Kobra Next"), _accepted()).up():
        result = await _submit(hass, await _start(hass), {"host": HOST})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Anycubic printer"
    assert result["result"].unique_id == SERIAL
    assert not _setup_lines(caplog)


async def test_a_refusal_from_a_model_not_known_here_names_the_model_the_printer_gave(hass, caplog):
    with Net(_info(modelId=29999, modelName="Anycubic Kobra Next"), _refusal("busy")).up():
        result = await _submit(hass, await _start(hass), {"host": HOST})
    assert result["errors"] == {"base": "handshake_failed"}
    assert [r.getMessage() for r in _setup_lines(caplog)] == [
        'Printer setup failed at step ctrl: the printer refused the request, code 10004, '
        'message "busy" (model 29999, Anycubic Kobra Next)']


def test_the_new_form_errors_are_the_approved_ones_in_both_files():
    import pathlib

    root = pathlib.Path(__file__).parent.parent / "custom_components" / "anycubic"
    text = (root / "strings.json").read_text()
    assert text == (root / "translations" / "en.json").read_text()
    assert json.loads(text)["config"]["error"] == {
        "cannot_connect": "Could not reach the printer. Check the IP and that LAN Mode is on.",
        "invalid_host": "Enter only the IP address or hostname, with no http://, port, or path.",
        "cloud_mode":
            "The printer is in cloud mode. Turn on LAN Mode on the printer, then try again.",
        "unsupported_printer": "Something answered at that address, but not the way a "
                               "supported printer does. Check the IP address.",
        "handshake_failed": "The printer answered but did not accept the setup. The Home "
                            "Assistant log says why. Include that line in a bug report.",
        "wrong_printer": "That address belongs to a different printer.",
    }


def test_an_error_that_carries_no_step_is_still_described():
    assert describe_failure(HandshakeError("anything at all " + TOKEN)) == (
        "handshake_failed", "Printer setup failed at step unknown: the reason was not recorded")
    assert describe_failure(CloudModeError(TOKEN)) == (
        "cloud_mode", "Printer setup failed at step unknown: the printer is in cloud mode")
    assert describe_failure(OSError(f"connect to {HOST} failed")) == (
        "cannot_connect", "Printer setup failed at step unknown: no connection (OSError)")


def test_the_wording_for_a_printer_with_another_way_in_no_longer_names_the_kobra_x():
    with pytest.raises(HandshakeError) as err:
        do_handshake(HOST, fetch=lambda method, url: {"modelId": "20021", "cn": "K2P"})
    assert str(err.value) == ("This device does not offer the LAN connection this integration "
                              "uses. Older Kobra 2 firmware is not supported.")


# ------------------------------------------------------------ what the line never contains
#
# A device can send back anything, including what it was just sent. Each case below is a
# device doing that somewhere its text would otherwise be repeated.

def _windows(secret, size=8):
    secret = secret.lower()
    return {secret[i:i + size] for i in range(len(secret) - size + 1)}


def _must_not_appear(net: Net | None = None) -> set[str]:
    """Every piece of every secret, lower-cased: whole values, 8-character windows of the
    long ones, and the forms a device might re-encode the token into."""
    whole = [TOKEN, SERIAL, MAC, MAC.replace("-", ":"), USN, HOST, CTRL_NAME, SESSION,
             BROKER_USER, BROKER_PASSWORD, DEVICE_ID, FIRST_HALF_DIGEST,
             base64.b64encode(TOKEN.encode()).decode(), binascii.hexlify(TOKEN.encode()).decode(),
             urllib.parse.quote(base64.b64encode(TOKEN.encode()).decode(), safe="")]
    if net is not None and any(not url.endswith("/info") for url in net.asked):
        query = net.query
        whole += [query["sign"], query["nonce"], query["did"], query["ts"]]
    pieces = {w.lower() for w in whole}
    for secret in whole:
        if len(secret) > 8:
            pieces |= _windows(secret)
    return pieces


def _assert_clean(text: str, net: Net | None = None, but=()):
    lowered = text.lower()
    found = sorted(p for p in _must_not_appear(net) if p in lowered and p not in but)
    assert not found, f"{found[:3]} in {text[:300]!r}"


def _echo_query(spaced=False, chunked=False):
    def answer(url):
        query = urllib.parse.urlsplit(url).query
        if spaced:
            query = query.replace("&", " ").replace("=", " ")
        if chunked:
            query = " ".join(query[i:i + 10] for i in range(0, len(query), 10))
        return _refusal(query)
    return answer


def _echo_nonce(url):
    return _refusal("bad nonce " + dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["nonce"])


def _chunks(text, size=10):
    return " ".join(text[i:i + size] for i in range(0, len(text), size))


ECHOES = {
    "the token": lambda: Net(_info(), _refusal(f"bad token {TOKEN}")),
    "the token across the length limit":
        lambda: Net(_info(), _refusal("la " * 23 + " " + TOKEN[:15] + " tail")),
    "the token split by a NUL and a newline":
        lambda: Net(_info(), _refusal(f"x {TOKEN[:6]}\x00{TOKEN[6:11]}\n{TOKEN[11:15]} y")),
    "the token split by an escape":
        lambda: Net(_info(), _refusal(f"x \x1b[31m{TOKEN[:5]}\x1b{TOKEN[5:14]} y")),
    "the token in base64":
        lambda: Net(_info(), _refusal("t " + base64.b64encode(TOKEN.encode()).decode())),
    "the token in hex":
        lambda: Net(_info(), _refusal("t " + binascii.hexlify(TOKEN.encode()).decode())),
    "the token percent-encoded": lambda: Net(_info(), _refusal(
        "t " + urllib.parse.quote(base64.b64encode(TOKEN.encode()).decode(), safe=""))),
    "a piece of the token from the middle":
        lambda: Net(_info(), _refusal(f"err {TOKEN[3:13]} end")),
    "the token in short pieces": lambda: Net(_info(), _refusal(_chunks(TOKEN))),
    "the digest of the token's first half":
        lambda: Net(_info(), _refusal(_chunks(FIRST_HALF_DIGEST))),
    "the whole query it was sent": lambda: Net(_info(), _echo_query()),
    "the query with its punctuation taken out": lambda: Net(_info(), _echo_query(spaced=True)),
    "the query in short pieces": lambda: Net(_info(), _echo_query(chunked=True)),
    "the nonce": lambda: Net(_info(), _echo_nonce),
    "the query in the HTTP reason": lambda: Net(_info(), lambda url: _http_error(
        url, 403, "Forbidden " + urllib.parse.urlsplit(url).query.replace("&", " "))),
    "the token in the HTTP reason":
        lambda: Net(_info(), lambda url: _http_error(url, 500, f"oops {TOKEN[4:20]}")),
    "the address in the HTTP reason of the first request":
        lambda: Net(lambda url: _http_error(url, 404, f"no such page on {HOST}")),
    "the MAC with dashes": lambda: Net(_info(), _refusal(f"device {MAC} busy")),
    "the MAC with colons":
        lambda: Net(_info(), _refusal(f"device {MAC.replace('-', ':').lower()} busy")),
    "the usn": lambda: Net(_info(), _refusal(f"device {USN} busy")),
    "the serial": lambda: Net(_info(), _refusal(f"device {SERIAL} busy")),
    "the address": lambda: Net(_info(), _refusal(f"go away {HOST}")),
    "the name in ctrlInfoUrl": lambda: Net(_info(), _refusal(f"ask {CTRL_NAME} instead")),
    "its own session token": lambda: Net(_info(), {
        "code": 500, "message": f"session {SESSION}", "data": {"token": SESSION}}),
    "the token as the model's name": lambda: Net(
        _info(modelId="29999", modelName=f"Kobra {TOKEN[:12]}"), _refusal("no")),
    "the serial as the model's name of a known model":
        lambda: Net(_info(modelName=SERIAL), _refusal("no")),
    "the token as the model id": lambda: Net(_info(modelId=TOKEN), _refusal("no")),
    "the token as the model id in cloud mode":
        lambda: Net(_info(modelId=TOKEN[:8], ctrlType="cloud")),
    "a code that is the token": lambda: Net(_info(), {"code": TOKEN, "message": "no"}),
    "a ctrlInfoUrl with a space in it": lambda: Net(
        _info(ctrlInfoUrl=f"http://{CTRL_NAME}:18910/ct rl"),
        lambda url: http.client.InvalidURL(f"URL can't contain control characters. {url!r}")),
    "a ctrlInfoUrl that is not a URL": lambda: Net(_info(ctrlInfoUrl=f"http://[{TOKEN}")),
    "an unsealed answer that is not JSON":
        lambda: Net(_info(), _accepted(f"password={BROKER_PASSWORD}".encode())),
    "a full answer whose broker is wrong": lambda: Net(_info(), _accepted(json.dumps({
        "broker": BROKER_PASSWORD, "username": BROKER_USER, "password": BROKER_PASSWORD,
        "deviceId": DEVICE_ID}).encode())),
}


def _chain(err):
    """An exception and everything it was raised from, as far as Python would print it."""
    seen = []
    while err is not None and err not in seen:
        seen.append(err)
        err = err.__cause__ or (None if err.__suppress_context__ else err.__context__)
    return seen


@pytest.mark.parametrize("name", ECHOES)
def test_nothing_the_device_echoes_is_in_the_error_or_its_description(name):
    with ECHOES[name]().up() as net, pytest.raises((HandshakeError, OSError)) as raised:
        do_handshake(HOST)
    err = raised.value
    key, line = describe_failure(err)
    _assert_clean(line, net)
    assert line.startswith("Printer setup failed at step ")
    assert all(" " <= c <= "~" for c in line), line
    if isinstance(err, HandshakeError):
        # Fixed text, and nothing of the device's stored on it outside the cleaned detail.
        _assert_clean(str(err) + repr(err.args) + repr(err.detail), net)
    else:
        _assert_clean(repr(err.handshake_detail), net)


def test_a_failed_unsealing_does_not_keep_what_it_unsealed():
    with Net(_info(), _accepted(f"password={BROKER_PASSWORD}".encode())).up(), \
            pytest.raises(HandshakeError) as raised:
        do_handshake(HOST)
    err = raised.value
    assert (err.step, err.detail["error"]) == ("decrypt", "JSONDecodeError")
    # The JSON error holds the text it could not read. It is not kept, even out of sight.
    assert _chain(err) == [err]


@pytest.mark.parametrize("name", ECHOES)
async def test_nothing_the_device_echoes_reaches_the_log_or_the_form(hass, caplog, name):
    caplog.set_level(logging.DEBUG)                 # every logger, at its most talkative
    entry = _entry(hass)
    with ECHOES[name]().up() as net:
        added = await _submit(hass, await _start(hass), {"host": HOST})
        moved = await _submit(
            hass, await _start_for(hass, config_entries.SOURCE_RECONFIGURE, entry), {"host": HOST})
        again = _entry(hass, HOST)
        back = await _submit(
            hass, await _start_for(hass, config_entries.SOURCE_REAUTH, again), {})
        assert not await hass.config_entries.async_setup(again.entry_id)
        await hass.async_block_till_done()
    for result in (added, moved, back):
        assert result["type"] == FlowResultType.FORM and result["errors"]
        # The form holds the address the user typed, and is where they typed it.
        _assert_clean(repr({k: v for k, v in result.items() if k != "data_schema"}), net,
                      but=_windows(HOST) | {HOST})
    _assert_clean(again.reason, net)
    _assert_clean(caplog.text, net)
    assert "Traceback" not in caplog.text
    lines = _setup_lines(caplog)
    assert len(lines) >= 4
    for record in lines:
        assert all(" " <= c <= "~" for c in record.getMessage()), record.getMessage()


@pytest.mark.parametrize("text", [
    f"bad token {TOKEN}", "la " * 23 + " " + TOKEN[:15], f"{TOKEN[:6]}\x00{TOKEN[6:14]}",
    f"err {TOKEN[3:11].upper()} end", "a1b2c3d4e5f60718", "QUJDREVGR0hJSktMTU5PUA==",
    "%41%42%43%44%45%46", f"for {SERIAL}", "", "\x00\n", 5, None, ["sign error"],
    # An address is not repeated even when it is not one this setup knows about.
    "try 10.0.0.7 instead", "seen from a4:5b:c6:00:00:01", "seen from A4-5B-C6-00-00-01",
])
def test_device_text_that_could_hold_a_secret_is_dropped_whole(text):
    assert safe_device_text(text, [TOKEN, SERIAL]) is None


def test_device_text_that_is_only_words_is_kept_cleaned_and_cut_last():
    assert safe_device_text("sign error", [TOKEN]) == "sign error"
    assert safe_device_text("  not\x1b[31m allowed\r\n", [TOKEN]) == "not[31m allowed"
    assert safe_device_text("no " * 40, [TOKEN]) == ("no " * 40).strip()[:80]
    # Too short to tell from ordinary text, so it is not held against the message.
    assert safe_device_text("device is busy", ["is", "", None]) == "device is busy"


@pytest.mark.parametrize("code", [True, False, 10 ** 9, -(10 ** 9), "10004", 1.5, None, [1]])
def test_a_code_that_is_not_a_plain_number_is_not_printed(code):
    with Net(_info(), {"code": code, "message": "no"}).up(), \
            pytest.raises(HandshakeError) as raised:
        do_handshake(HOST)
    assert describe_failure(raised.value)[1] == (
        f'Printer setup failed at step ctrl: the printer refused the request, message "no" {KOBRA_X}')
