"""Topic templates, message types, and status enums (validated — see PROTOCOL-VALIDATED.md)."""
import ipaddress
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

# The keys above whose value is the job's file name or a path to it. A payload that has one
# has said what is printing, and redacted() then scrubs that name by VALUE out of the rest
# of the payload, under whatever key. The list above was extended four times for this one
# text turning up under one more key; a fifth key needs no fifth release.
#
# `name` and `objects_skip_parts` are masked by key but are not read as a name here. `name`
# is as broad as a key gets, and one that held a model or a material would have that word
# scrubbed out of the whole payload. The model name they carry is found from the file name
# instead (see _job_patterns).
JOB_NAME_KEYS: frozenset[str] = frozenset({
    "filename", "display_filename", "origin3mf", "temp_gcode", "plate_name"})

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

# What a job file's name ends in. Taken off, repeatedly, to leave the stem: the extension is
# not part of what the user called the print, and "<name>.gcode.3mf" and "<name>.gcode" are
# the same job (the printer unpacks one into the other). Listed rather than "whatever
# follows the last dot", because the stem itself has dots in it: a layer height of 0.2.
_JOB_EXTENSIONS = re.compile(r"(?:\.(?:gcode|bgcode|gco|g|3mf|stl|obj|step|stp|amf))+\Z",
                             re.IGNORECASE)
# What the slicer puts round the model's name to make the file's:
# <date>-<time>-<model name>_plate(NN)_<material>_<layer>_<duration>. Every job captured so
# far is named this way. A name that is not simply has nothing taken off.
_SLICER_PREFIX = re.compile(r"\A\d{4}-\d{4}-")
_SLICER_SUFFIX = re.compile(r"_plate\(\d+\).*\Z", re.IGNORECASE)
# A run of letters and digits, in any script. Everything else is a separator.
_WORD = re.compile(r"[^\W_]+")
_SEPARATORS = r"[\W_]+"

# A path segment that says what a URL is for rather than who may use it: a word in lower
# case ("flv", "gcode_upload", "live"), with a file extension if it has one ("index.m3u8"),
# or a short number such as a channel ("1"). See _mask_url.
_PATH_WORD = re.compile(r"(?:[a-z][a-z_-]{0,23}(?:\.[a-z0-9]{1,5})?|\d{1,3})\Z")


def runtime_identifiers(hs, *also: str | None) -> tuple[str, ...]:
    """The values that identify one printer, for redacted() to scrub out of any string.

    A key list only covers keys someone has already seen, and a new firmware can put the
    device id or the address under any name. These are the identifiers a session already
    holds: the handshake's device id, broker host, serial and MAC, plus the address the
    user entered (`also`), which no handshake reports.

    The MAC is given with dashes and with colons, because the handshake reports one form
    and Home Assistant's device registry holds the other. The broker username and password
    are left out on purpose: they are never in a payload, and scrubbing a short username
    out of every string would mangle text for nothing. Their KEYS stay in SENSITIVE_KEYS.

    An entered address that is one bare word is left out for the same reason. A printer
    called "anycubic" on the user's network would have that word scrubbed out of every
    model name and every topic, and the entered address never appears in a printer payload
    in the first place. An IP address or a dotted name is specific enough to keep.
    """
    mac = hs.mac or ""
    return tuple(v for v in (hs.device_id, hs.broker_host, hs.serial,
                             mac.replace(":", "-"), mac.replace("-", ":"),
                             *(a for a in also if a and not _is_one_word(a))) if v)


def _is_one_word(address: str) -> bool:
    """Is this entered address a single-label hostname, rather than an IP or a dotted name?"""
    if "." in address:
        return False
    try:
        ipaddress.ip_address(address)
    except ValueError:
        return True
    return False        # an IPv6 literal has no dot either


def redacted(value, identifiers=(), job_names=()):
    """Deep-copy `value` with everything that identifies the user or the printer masked.

    The one redactor. The debug log and the diagnostics download both go through it, because
    both get pasted into public issues. It never mutates what it is given, so a payload can
    be logged as a redacted copy while the original is still on its way to the printer.
    Four layers, each catching what the others cannot:

    - key names: a value under a SENSITIVE_KEYS name is masked whole.
    - URLs: a string that parses as scheme://host keeps its scheme, port and the words of
      its path, and loses its userinfo, host, query, fragment and any opaque path segment.
    - exact values: `identifiers` (see runtime_identifiers) are scrubbed out of every string
      and every key they appear in, whatever their case.
    - the job's name: the file being printed, as `value` itself gives it under a
      JOB_NAME_KEYS key and as the caller gives it in `job_names` (the coordinator knows
      what is printing when a report does not say), is scrubbed the same way.

    Huge strings are truncated last, after masking: the head that survives a truncation is
    exactly where a URL keeps its host.
    """
    if isinstance(identifiers, str):
        # One identifier handed over bare. Iterated as it stands it would be a run of single
        # characters, all too short to count, and nothing would be scrubbed at all.
        identifiers = (identifiers,)
    if isinstance(job_names, str):
        job_names = (job_names,)
    wanted = {i.lower(): re.escape(i.lower()) for i in identifiers
              if isinstance(i, str) and len(i) >= _MIN_IDENTIFIER}
    for name in (*job_names, *_jobs_named_in(value)):
        if isinstance(name, str):
            wanted.update(_job_patterns(name))
    # Longest first. Where one identifier begins another (192.168.1.5 and 192.168.1.50) the
    # shorter must not match first and leave the tail of the longer in the clear. The same
    # goes for a model's name, which the file's name contains.
    patterns = [wanted[text] for text in sorted(wanted, key=len, reverse=True)]
    exact = re.compile("|".join(patterns), re.IGNORECASE) if patterns else None
    return _redact(value, exact)


def redacted_error(err: BaseException, identifiers=()) -> str:
    """An exception's text, fit for Home Assistant's ordinary log.

    UpdateFailed and ConfigEntryNotReady are logged at the normal level, with no debug
    logging on, and those lines are pasted into issues like any other. An exception's text
    is whatever the library, or the device that answered, put in it. So it goes through the
    redactor, with two things added that only make sense for an error.

    An identifier the redactor leaves alone in a payload for being short is masked here
    wherever it stands as a word of its own: a printer entered as "kobra" is an ordinary
    word in a model name and the address in "cannot resolve kobra". And the result is
    checked. If any identifier is still in it, or it is empty, or it could not be made at
    all, the kind of error is all that is said. Never empty: Home Assistant fills an empty
    message in from the exception it was raised from, which is the text this keeps out.
    """
    try:
        wanted = sorted({i.lower() for i in identifiers if isinstance(i, str) and i},
                        key=len, reverse=True)
        text = redacted(str(err), wanted)
        if wanted:
            # Not inside a longer run of letters and digits: "pi" is not in "expired".
            text = re.sub(rf"(?<![^\W_])(?:{'|'.join(map(re.escape, wanted))})(?![^\W_])",
                          REDACTED, text, flags=re.IGNORECASE)
        lowered = text.lower()
        if text and not any(i in lowered for i in wanted):
            return text
    except Exception:  # noqa: BLE001
        pass
    return type(err).__name__


def _jobs_named_in(value):
    """Every job file `value` names: the strings it holds under a JOB_NAME_KEYS key."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, Mapping):
            for key, held in item.items():
                if isinstance(held, str):
                    if key in JOB_NAME_KEYS:
                        yield held
                else:
                    pending.append(held)
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)


def _job_patterns(name: str) -> dict[str, str]:
    """What to scrub for one job file, as {the text: a pattern that finds it}.

    Two texts. The stem: the base name with the directory and the extension taken off, so
    that ".3mf_temp/<name>.gcode" and "<name>.gcode.3mf" are one job. And the model's name:
    the stem without what the slicer wrapped round it. The printer repeats the model's name
    on its own, as the name of each object on the plate, with the spaces of the file's name
    turned into underscores. So each text is matched with ANY separators between its words,
    and "Desk bracket", "Desk_bracket" and "desk-bracket" are all found. Changed separators
    are the only variation the printer has been seen to make, so that is as far as this
    goes: the words must be the same words in the same order, with something between them.

    A text shorter than _MIN_IDENTIFIER is not scrubbed by value, for the reason an
    identifier is not: "cube" is a word. The key list still masks it where it is known to be.
    """
    stem = _JOB_EXTENSIONS.sub("", name.replace("\\", "/").rsplit("/", 1)[-1])
    model = _SLICER_SUFFIX.sub("", _SLICER_PREFIX.sub("", stem))
    found = {}
    for text in (stem, model):
        words = _WORD.findall(text)
        if words:
            # From the first word to the last: separators hanging off either end are not
            # part of the name, and the slicer leaves one where it joined the pieces.
            text = text[text.index(words[0]):text.rindex(words[-1]) + len(words[-1])]
        if words and len(text) >= _MIN_IDENTIFIER:
            found[text.lower()] = _SEPARATORS.join(re.escape(w.lower()) for w in words)
    return found


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

    The path keeps its shape and loses what is opaque in it. Newer printers serve the camera
    at /live/<per-session token>, and that token is all it takes to watch. A token can be as
    short as eight characters, so it cannot be told from a word by length. A segment is
    kept only when it reads as a word or a short number (_PATH_WORD): /flv, /gcode_upload
    and /streaming/live/1 stay whole, and anything with a capital, with letters and digits
    mixed, or longer than a word comes out as /live/**REDACTED**.
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
    path = "/".join(segment if not segment or _PATH_WORD.match(segment) else REDACTED
                    for segment in parts.path.split("/"))
    return "".join((parts.scheme, "://", REDACTED, "" if port is None else f":{port}",
                    path, f"?{REDACTED}" if parts.query else "",
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
