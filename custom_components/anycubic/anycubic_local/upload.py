"""Send a file to the printer's own upload port. Upload only: nothing here starts a print.

Two requests, both through the caller's aiohttp session and neither ever following a
redirect. The first asks the printer where to upload. The second sends the file there.

Where to upload is a URL the device at the entered address chooses, and its query carries the
printer's upload token. So the URL is never used as given: the host is always the address the
user entered, and only the port, path and query are taken from the printer, the way the
integration already treats the printer's other URLs. The token is never logged, stored or
put in an error. Every failure is raised as an UploadError with fixed text, `from None` and
outside the handler that caught the original, so the exception it replaces, whose text can
quote the URL, travels with it neither as cause nor as context. There is no retry: a
multipart upload sent twice can leave a damaged file on the printer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import aiohttp
from yarl import URL

from .const import redacted_error
from .printfile import whole_number

_LOGGER = logging.getLogger(__name__)

# Where the printer says where to upload.
INFO_PORT = 18910
INFO_TIMEOUT = 10.0
# A whole upload, from the first byte to the printer's answer. The printer checks a 3MF
# before it answers, which takes a while for a large one.
UPLOAD_TIMEOUT = 900.0
# Most of an answer that is read. Both answers are a small JSON object.
_MAX_ANSWER = 64 * 1024

# The printer's own upload errors, each to the reason the user is shown. INFERRED from the
# firmware's upload handler, where each code sits beside the check that raises it; T2 is
# where they are confirmed. 19001 to 19004 are not raised in that handler, so they are read
# as the printer refusing the request itself.
UPLOAD_ERRORS: dict[int, str] = {
    19001: "upload_refused",
    19002: "upload_refused",
    19003: "upload_refused",
    19004: "upload_refused",
    19005: "upload_not_understood",    # not a POST
    19006: "upload_not_understood",    # the form could not be read
    19007: "upload_file_refused",      # the file's name or type
    19008: "upload_not_saved",         # the file could not be opened for writing
    19009: "upload_not_saved",         # the file could not be moved into place
    19010: "upload_no_space",          # not enough free space for the file
    19011: "upload_3mf_refused",       # the printer's own check of a 3MF
}


class UploadError(Exception):
    """An upload that did not happen. `key` names the reason; the text is only the key."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class UploadTarget:
    """Where to send the file. Neither field is ever shown, in a log line or anywhere else."""

    url: str = field(repr=False)
    # The values in the URL's query: the upload token. The caller holds these for redaction
    # for as long as it holds the target.
    secrets: tuple[str, ...] = field(repr=False)
    # The entered address the URL was moved onto, masked in a log line like the token.
    host: str = field(repr=False, default="")


def _target(host: str, supplied, default_port: int) -> UploadTarget:
    """The printer's upload URL, moved onto the host the user entered."""
    if not isinstance(supplied, str) or not supplied:
        raise UploadError("upload_refused")
    parts = port = None
    try:
        parts = urlsplit(supplied)
        port = parts.port
    except ValueError:
        parts = None
    # Outside the handler, so nothing of the URL travels with the error as its context.
    if parts is None:
        raise UploadError("printer_not_answering") from None
    path = parts.path if parts.path.startswith("/") else f"/{parts.path}"
    url = f"http://{host}:{default_port if port is None else port}{path}"
    if parts.query:
        url = f"{url}?{parts.query}"
    # Each value as it is written in the URL and as it reads decoded: an error can quote
    # either one.
    raw = (pair.partition("=")[2] for pair in re.split(r"[&;]", parts.query))
    decoded = (value for _, value in parse_qsl(parts.query))
    secrets = tuple(dict.fromkeys(value for value in (*raw, *decoded) if value))
    return UploadTarget(url=url, secrets=secrets, host=host)


def _log_failure(what: str, err: BaseException, identifiers) -> None:
    # The kind of failure is worth having in a debug log; its text goes through the same
    # redactor as every other error, with the token handed in, so the token is masked
    # whatever its length.
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug("%s failed: %s", what, redacted_error(err, tuple(identifiers)))


async def _read_answer(response) -> tuple[int, dict]:
    body = await response.content.read(_MAX_ANSWER)
    try:
        answer = json.loads(body)
    except ValueError:
        answer = None
    return response.status, answer if isinstance(answer, dict) else {}


async def async_fetch_target(session: aiohttp.ClientSession, host: str, *,
                             port: int = INFO_PORT, identifiers=()) -> UploadTarget:
    """Ask the printer where to upload. Asked fresh for every upload and kept by nobody."""
    status, info = 0, {}
    try:
        async with session.get(
                URL(f"http://{host}:{port}/info"), allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=INFO_TIMEOUT)) as response:
            status, info = await _read_answer(response)
    except Exception as err:  # noqa: BLE001 - whatever went wrong, the caller gets fixed text
        _log_failure("asking the printer where to upload", err, (host, *identifiers))
    # Raised here, outside the handler, so the error is not chained to the one it replaces.
    if status != 200 or not info:
        raise UploadError("printer_not_answering")
    if info.get("ctrlType") == "cloud":
        raise UploadError("upload_refused")
    return _target(host, info.get("fileUploadurl"), port)


async def async_send_file(session: aiohttp.ClientSession, target: UploadTarget, path: Path,
                          name: str, size: int, *, identifiers=()) -> None:
    """Send one file, streamed from disk, under the printer-side name `name`.

    The printer checks its free space against X-File-Length before it takes the file. The
    file goes in a part called "gcode", whatever its kind, beside a text part naming it.
    """
    masked = (*identifiers, target.host, *target.secrets)
    loop = asyncio.get_running_loop()
    status, answer, sent = 0, {}, False
    try:
        handle = await loop.run_in_executor(None, path.open, "rb")
    except OSError as err:
        # The kind of error only: its text is the path, which ends in the user's file name.
        _LOGGER.debug("opening the file to upload failed: %s", type(err).__name__)
        handle = None
    if handle is not None:
        try:
            # The names are already reduced to plain ASCII with no quotes in them, so they
            # are sent as they are rather than percent-encoded.
            form = aiohttp.FormData(quote_fields=False)
            form.add_field("filename", name)
            form.add_field("gcode", handle, filename=name,
                           content_type="application/octet-stream")
            async with session.post(
                    URL(target.url, encoded=True), data=form,
                    headers={"X-File-Length": str(size)}, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT)) as response:
                status, answer = await _read_answer(response)
            sent = True
        except Exception as err:  # noqa: BLE001 - its text can quote the URL, and the token
            _log_failure("upload", err, masked)
        finally:
            await loop.run_in_executor(None, handle.close)
    # Raised here, outside the handler, so the error is not chained to the one it replaces.
    if not sent:
        raise UploadError("upload_failed") from None
    code = whole_number(answer.get("code"), 0, 10**9)
    if code in UPLOAD_ERRORS:
        raise UploadError(UPLOAD_ERRORS[code])
    # The printer's answer to a good upload is not captured yet (T2). Taken as good: status
    # 200 with no code, or with 0 or 200. Anything else, a redirect included, is a refusal.
    if status != 200 or ("code" in answer and code not in (0, 200)):
        raise UploadError("upload_refused")
