"""Exception hierarchy for the AnyCubic local client."""


class AnycubicError(Exception):
    """Base error."""


class HandshakeError(AnycubicError):
    """GET /info or POST /ctrl handshake failed."""


class CloudModeError(HandshakeError):
    """The printer is in CLOUD mode — LAN Mode must be re-enabled on the printer."""


class ParseError(AnycubicError):
    """A report payload did not match the expected shape."""
