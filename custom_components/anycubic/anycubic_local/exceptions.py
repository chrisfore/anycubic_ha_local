"""Exception hierarchy for the Anycubic local client."""


class AnycubicError(Exception):
    """Base error."""


class HandshakeError(AnycubicError):
    """GET /info or POST /ctrl handshake failed.

    The message is always fixed text: nothing the device sent and nothing of the address is
    ever in it, because it ends up in Home Assistant's ordinary log. What went wrong is said
    by three attributes instead:

    - `step`: how far setup got (see the STEP_ names in handshake.py), or None if unknown.
    - `reason`: which of that step's failures this is, a fixed word.
    - `detail`: the few facts worth logging (a status, a code, the model). Anything in it
      that came from the device was cleaned and checked before it was put there.
    """

    def __init__(self, message: str = "", *, step: str | None = None,
                 reason: str | None = None, detail: dict | None = None) -> None:
        super().__init__(message)
        self.step = step
        self.reason = reason
        self.detail = dict(detail or {})


class CloudModeError(HandshakeError):
    """The printer is in CLOUD mode — LAN Mode must be re-enabled on the printer."""


class ParseError(AnycubicError):
    """A report payload did not match the expected shape."""
