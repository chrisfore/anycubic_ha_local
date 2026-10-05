"""Topic templates, message types, and status enums (validated — see PROTOCOL-VALIDATED.md)."""
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

PREFIX = "anycubic/anycubicCloud/v1"

# Identifiers / addresses that must never leave the user's machine in something they
# share. Diagnostics redacts these, and so does the inbound-report debug log — users
# paste those straight into issues. filename can embed the user's own name.
#
# `plate_name` carries the same text as `filename` — a `file` report puts the full
# /useremain/app/gk/gcodes/... path in it. Masking only `filename` was decorative: the
# path sat in the clear beside a **REDACTED** filename in the very report that revealed
# it (issue #12). Any key that can hold a print's name belongs here or none of them do.
#
# By that rule `objects_skip_parts` is here too: a `file` report lists the objects on the
# plate under names built from the model file's name. `printerName` is whatever the owner
# typed into the printer.
#
# A `print` progress report says the name four more times beside `filename`: under
# `display_filename`, in the paths of the stored job and of the gcode unpacked from it
# (`origin3mf`, `temp_gcode`), and as `name` in each entry of source_info.models, the
# model's own name, which the file name is built from. `name` is as broad as a key gets.
# It is here bare because a key list cannot say "only under models", and masking `models`
# whole would also hide the fields beside it, which name nothing; no report seen so far
# has another `name`. `temp_dir` sits with these, is a staging directory rather than a
# name, and is deliberately NOT here.
#
# `urls` is deliberately NOT here. Its values hold the printer's address, and fileUploadurl
# an `s=` token besides, but masking the key would also throw away the scheme, port and
# path that make a camera debuggable (issue #6). redacted() masks the parts of a URL that
# identify, under any key, and keeps the rest.
SENSITIVE_KEYS: frozenset[str] = frozenset({
    "host", "ip", "filename", "plate_name", "username", "password", "device_id",
    "serial", "broker_host", "deviceId", "mac", "objects_skip_parts", "printerName",
    "display_filename", "origin3mf", "temp_gcode", "name"})

REDACTED = "**REDACTED**"

# Longest string kept whole in a redacted payload. A `file`/fileDetails report carries
# base64 thumbnail, png_image and svg_image blobs (issue #13) — hundreds of KB per print.
# Logged verbatim they bury the line that matters and are unpastable into an issue; the
# reporter had to strip them by hand. Keep enough of the head to recognise the field.
MAX_LOGGED_STR = 120
_KEEP_HEAD = 32

# Shortest runtime identifier worth scrubbing out of free text. Below this a value is
# ordinary text as often as it is an identifier, and replacing every accidental match
# mangles the line it was meant to protect. Seven is the shortest an IPv4 address can be.
_MIN_IDENTIFIER = 7


def runtime_identifiers(hs, *also: str | None) -> tuple[str, ...]:
    """The values that identify one printer, for redacted() to scrub out of any string.

    A key list only covers keys someone has already seen, and a new firmware can put the
    device id or the address under any name. These are the identifiers a session already
    holds: the handshake's device id, broker host, serial and MAC, plus whatever the caller
    adds (the address the user entered, which no handshake reports).

    The MAC is given with dashes and with colons, because the handshake reports one form
    and Home Assistant's device registry holds the other. The broker username and password
    are left out on purpose: they are never in a payload, and scrubbing a short username
    out of every string would mangle text for nothing. Their KEYS stay in SENSITIVE_KEYS.
    """
    mac = hs.mac or ""
    return tuple(v for v in (hs.device_id, hs.broker_host, hs.serial,
                             mac.replace(":", "-"), mac.replace("-", ":"), *also) if v)


def redacted(value, identifiers=()):
    """Deep-copy `value` with everything that identifies the user or the printer masked.

    The one redactor. The debug log and the diagnostics download both go through it, because
    both get pasted into public issues. It never mutates what it is given, so a payload can
    be logged as a redacted copy while the original is still on its way to the printer.
    Three layers, each catching what the others cannot:

    - key names: a value under a SENSITIVE_KEYS name is masked whole.
    - URLs: a string that parses as scheme://host keeps its scheme, port and path, and
      loses its userinfo, host, query and fragment.
    - exact values: `identifiers` (see runtime_identifiers) are scrubbed out of every string
      and every key they appear in, whatever their case.

    Huge strings are truncated last, after masking: the head that survives a truncation is
    exactly where a URL keeps its host.
    """
    if isinstance(identifiers, str):
        # One identifier handed over bare. Iterated as it stands it would be a run of single
        # characters, all too short to count, and nothing would be scrubbed at all.
        identifiers = (identifiers,)
    wanted = sorted({i.lower() for i in identifiers
                     if isinstance(i, str) and len(i) >= _MIN_IDENTIFIER}, key=len, reverse=True)
    # Longest first. Where one identifier begins another (192.168.1.5 and 192.168.1.50) the
    # shorter must not match first and leave the tail of the longer in the clear.
    exact = re.compile("|".join(map(re.escape, wanted)), re.IGNORECASE) if wanted else None
    return _redact(value, exact)


def _redact(value, exact):
    # Every container type is walked, not only dict and list: one that is passed through
    # untouched is one whose contents nobody looked at.
    if isinstance(value, Mapping):
        return {(_scrub(k, exact) if isinstance(k, str) else k):
                (REDACTED if k in SENSITIVE_KEYS and v is not None else _redact(v, exact))
                for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(v, exact) for v in value]
    if isinstance(value, str):
        text = _scrub(_mask_url(value), exact)
        if len(text) > MAX_LOGGED_STR:
            return f"{text[:_KEEP_HEAD]}...<{len(text)} chars truncated>"
        return text
    return value


def _scrub(text: str, exact) -> str:
    return text if exact is None else exact.sub(REDACTED, text)


def _mask_url(text: str) -> str:
    """Mask the parts of a URL that identify: scheme://**REDACTED**:port/path?**REDACTED**.

    Recognised by parsing, never by pattern: a firmware version ("2.7.1.4") has exactly the
    shape of an IPv4 address and has to survive. The result is rebuilt from the parsed
    pieces rather than edited in place, so nothing in the host position can be carried
    across by accident.
    """
    if ":" not in text:
        # No colon means no scheme, so this is not a URL. Asking first also keeps the base64
        # blobs of a `file` report (hundreds of KB, no colon in the alphabet) out of the
        # cache urlsplit keeps of everything it is shown.
        return text
    try:
        parts = urlsplit(text)
    except ValueError:
        # urlsplit only objects to what sits in the host position (a malformed IPv6
        # literal, say). No part of such a string can be called safe.
        return REDACTED
    if not (parts.scheme and parts.netloc):
        return text
    try:
        port = parts.port
    except ValueError:
        # Not a number: an unbracketed IPv6 host reads as host "fe80", port ":1". Keeping
        # whatever is in the port position would hand back a piece of the address.
        port = None
    return "".join((parts.scheme, "://", REDACTED, "" if port is None else f":{port}",
                    parts.path, f"?{REDACTED}" if parts.query else "",
                    f"#{REDACTED}" if parts.fragment else ""))

QUERY_TYPES = ["info", "tempature", "fan", "light", "multiColorBox", "print"]
# note: "tempature" is the printer firmware's actual (misspelled) wire string and must NOT be corrected.
# report `action` varies (query/report/refresh/workReport/setInfo) — key off TYPE, never action.

# project.pause int -> human state
PAUSE_STATE = {0: "printing", 1: "paused", 2: "pausing", 3: "resuming", 4: "stopping"}
PAUSE_PAUSED = 1  # project.pause int for the paused state

# top-level info.data.state
STATE_FREE = "free"
STATE_BUSY = "busy"

# `type` of the chamber light inside a `light` object. Shared by the command builder and the
# report parser on purpose: the printer answers a light command with a bare light object,
# and the parser believes one only for the lamp the builder commands (issue #14).
LIGHT_TYPE_CHAMBER = 2


def query_topic(model_id: str, device_id: str, msg_type: str) -> str:
    return f"{PREFIX}/web/printer/{model_id}/{device_id}/{msg_type}"


def report_prefix(model_id: str, device_id: str) -> str:
    return f"{PREFIX}/printer/public/{model_id}/{device_id}"
