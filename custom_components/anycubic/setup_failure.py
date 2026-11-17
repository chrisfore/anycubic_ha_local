"""Why setting a printer up failed: which form error to show, and what to say in the log."""
from __future__ import annotations

import urllib.error

from .anycubic_local import handshake as hs_mod
from .anycubic_local.exceptions import CloudModeError, HandshakeError
from .const import MODEL_NAMES

# The form errors (strings.json, config.error).
ERROR_INVALID_HOST = "invalid_host"
ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_CLOUD_MODE = "cloud_mode"
ERROR_UNSUPPORTED_PRINTER = "unsupported_printer"
ERROR_HANDSHAKE_FAILED = "handshake_failed"

_STEPS = frozenset((hs_mod.STEP_INFO, hs_mod.STEP_CTRL, hs_mod.STEP_DECRYPT,
                    hs_mod.STEP_SERIAL))
_UNKNOWN_STEP = "unknown"
# Before the printer has answered /info properly, what is at the address is in doubt.
# After, it is a printer, and the fault is in what it did next.
_BEFORE_A_GOOD_INFO = frozenset((hs_mod.STEP_INFO, hs_mod.STEP_SERIAL))


def describe_failure(err: BaseException, doing: str = "setup") -> tuple[str, str]:
    """The form error for a failed handshake, and one line for the log saying why.

    `doing` is the word for what was being done: "setup", or "reconnect" when a running
    session is being got back.

    For the two things a device can do to fail one: a HandshakeError and an OSError. The
    line is built from the error's step, reason and detail and from fixed words here. It
    never uses the error's own text, its arguments or its repr: an HTTP error's holds the
    address that was asked, with the whole query. Everything from the device that reaches
    the line was cleaned where it was read (handshake.safe_device_text), and is checked
    again here for being the plain kind of value it should be.
    """
    if isinstance(err, HandshakeError):
        step, detail = err.step, err.detail
    else:
        step = getattr(err, "handshake_step", None)
        detail = getattr(err, "handshake_detail", None)
    if step not in _STEPS:
        step = _UNKNOWN_STEP
    if not isinstance(detail, dict):
        detail = {}
    key, why = _key_and_why(err, step, detail)
    return key, f"Printer {doing} failed at step {step}: {why}{_model(detail)}"


def is_described(err: BaseException) -> bool:
    """Is this a failure describe_failure should word, rather than the error's own text?

    Everything the handshake raises: its own errors, an OSError it marked with the step it
    came from, and any HTTP status error at all, marked or not, because that one's text is
    the device's reason phrase.
    """
    return (isinstance(err, (HandshakeError, urllib.error.HTTPError))
            or hasattr(err, "handshake_step"))


def _key_and_why(err: BaseException, step: str, detail: dict) -> tuple[str, str]:
    after_info = step not in _BEFORE_A_GOOD_INFO and step != _UNKNOWN_STEP
    answered = ERROR_HANDSHAKE_FAILED if after_info else ERROR_UNSUPPORTED_PRINTER
    if isinstance(err, CloudModeError):
        return ERROR_CLOUD_MODE, "the printer is in cloud mode"
    if isinstance(err, HandshakeError):
        reason = err.reason
        if reason == hs_mod.REASON_NOT_OFFERED:
            return answered, "the device does not offer the connection this integration uses"
        if reason == hs_mod.REASON_NO_SERIAL:
            return answered, "the printer gave no serial number"
        if reason == hs_mod.REASON_REFUSED:
            why = "the printer refused the request"
            code, said = detail.get("code"), detail.get("message")
            if _is_plain_int(code):
                why += f", code {code}"
            if _is_plain_text(said):
                why += f', message "{said}"'
            return answered, why
        if reason == hs_mod.REASON_UNREADABLE:
            kind = detail.get("error")
            why = "the printer's answer could not be read"
            if isinstance(kind, str) and kind.isidentifier() and len(kind) <= 40:
                why += f" ({kind})"
            return answered, why
        if step == _UNKNOWN_STEP:
            # Not one of this integration's own: nothing is known about it but its kind.
            return ERROR_HANDSHAKE_FAILED, "the reason was not recorded"
        return answered, "the answer was not a printer's"
    if isinstance(err, urllib.error.HTTPError):
        why = "the answer was an HTTP error"
        status, said = detail.get("status"), detail.get("status_text")
        if _is_plain_int(status):
            why = f"HTTP status {status}"
            if _is_plain_text(said):
                why += f' "{said}"'
        return answered, why
    return ERROR_CANNOT_CONNECT, f"no connection ({_kind_of(err)})"


def _kind_of(err: BaseException) -> str:
    """What kind of network error, by class and error number: nothing that is free text."""
    inner = getattr(err, "reason", None) if isinstance(err, urllib.error.URLError) else None
    if isinstance(inner, BaseException):
        err = inner
    kind = type(err).__name__
    number = getattr(err, "errno", None)
    return f"{kind}, errno {number}" if _is_plain_int(number) else kind


def _model(detail: dict) -> str:
    """ " (model 20030, Anycubic Kobra X)", as far as it is known; else nothing."""
    model_id = detail.get("model_id")
    if not (isinstance(model_id, str) and model_id.isascii() and model_id.isdigit()
            and len(model_id) <= 8):
        return ""
    # This integration's own name for a model it knows; the device's only for one it does not.
    name = MODEL_NAMES.get(model_id) or detail.get("model_name")
    return f" (model {model_id}, {name})" if _is_plain_text(name) else f" (model {model_id})"


def _is_plain_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) < 1_000_000


def _is_plain_text(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 80
            and all(" " <= c <= "~" for c in value) and '"' not in value)
