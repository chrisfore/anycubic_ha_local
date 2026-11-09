"""The upload client, against a real HTTP server on this machine standing in for the printer."""
import json
import logging

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.anycubic.anycubic_local import upload
from custom_components.anycubic.anycubic_local.upload import UploadError

# Made up, one as long as a real upload token and one far shorter than the redactor's
# usual minimum. Neither may ever show in a log line or an error.
TOKEN = "Tk9dX2pQ7mN4vB8cL1sR6wY3zA5hJ0eF"
SHORT = "ab1"
# What the stand-in printer names as its own host. Never connected to: the client must keep
# the host it was given (and the test harness refuses any address but this machine's).
NAMED_HOST = "198.51.100.7"


class Printer:
    """The printer's upload port: /info says where to upload, /gcode_upload takes the file."""

    def __init__(self) -> None:
        self.port = 0
        self.token = TOKEN
        self.info_redirect = False
        self.info = None
        self.answer: tuple[int, str] = (200, json.dumps({"code": 200, "msg": "ok"}))
        self.redirect_upload = False
        self.uploads: list[dict] = []
        self.elsewhere = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/info", self._info)
        app.router.add_post("/gcode_upload", self._upload)
        app.router.add_route("*", "/elsewhere", self._elsewhere)
        return app

    async def _info(self, request):
        if self.info_redirect:
            raise web.HTTPFound(f"http://127.0.0.1:{self.port}/elsewhere")
        body = self.info or {
            "fileUploadurl": f"http://{NAMED_HOST}:{self.port}/gcode_upload?s={self.token}",
            "ctrlType": "lan"}
        return web.json_response(body)

    async def _upload(self, request):
        form = await request.post()
        part = form.get("gcode")
        self.uploads.append({
            "query": dict(request.query), "headers": dict(request.headers),
            "filename": form.get("filename"),
            "part_filename": getattr(part, "filename", None),
            "content": part.file.read() if part is not None else None})
        if self.redirect_upload:
            raise web.HTTPFound(f"http://127.0.0.1:{self.port}/elsewhere")
        status, text = self.answer
        return web.Response(status=status, text=text, content_type="application/json")

    async def _elsewhere(self, request):
        self.elsewhere += 1
        return web.json_response({"code": 200})


@pytest.fixture
async def printer(socket_enabled):
    stand_in = Printer()
    server = TestServer(stand_in.app(), host="127.0.0.1")
    await server.start_server()
    stand_in.port = server.port
    session = aiohttp.ClientSession()
    yield stand_in, session
    await session.close()
    await server.close()


@pytest.fixture
def gcode(tmp_path):
    path = tmp_path / "part.gcode"
    path.write_bytes(b"; test\n" + b"G1 X1 Y1\n" * 5000)
    return path


async def _upload(stand_in, session, path, name="part.gcode"):
    target = await upload.async_fetch_target(session, "127.0.0.1", port=stand_in.port)
    await upload.async_send_file(session, target, path, name, path.stat().st_size)
    return target


async def test_the_host_stays_the_entered_one_and_the_printers_query_is_kept(printer, gcode):
    stand_in, session = printer
    target = await _upload(stand_in, session, gcode, "Desk bracket (2).gcode")
    assert len(stand_in.uploads) == 1, "the file went somewhere else"
    sent = stand_in.uploads[0]
    assert sent["query"] == {"s": TOKEN}
    assert target.secrets == (TOKEN,)
    assert TOKEN not in repr(target) and NAMED_HOST not in repr(target)
    # The request the printer expects: its name as text, the file as "gcode", the size ahead.
    assert sent["filename"] == "Desk bracket (2).gcode"
    assert sent["part_filename"] == "Desk bracket (2).gcode"     # not percent-encoded
    assert sent["content"] == gcode.read_bytes()
    assert sent["headers"]["X-File-Length"] == str(gcode.stat().st_size)
    # A length up front, not a chunked body.
    assert "Content-Length" in sent["headers"]
    assert "chunked" not in sent["headers"].get("Transfer-Encoding", "")


async def test_a_short_token_is_held_as_a_secret_too(printer, gcode):
    stand_in, session = printer
    stand_in.token = SHORT
    target = await _upload(stand_in, session, gcode)
    assert target.secrets == (SHORT,)
    assert stand_in.uploads[0]["query"] == {"s": SHORT}


async def test_redirects_are_never_followed(printer, gcode):
    stand_in, session = printer
    stand_in.info_redirect = True
    with pytest.raises(UploadError) as err:
        await upload.async_fetch_target(session, "127.0.0.1", port=stand_in.port)
    assert err.value.key == "printer_not_answering"
    stand_in.info_redirect = False
    stand_in.redirect_upload = True
    with pytest.raises(UploadError) as err:
        await _upload(stand_in, session, gcode)
    assert err.value.key == "upload_refused"
    assert stand_in.elsewhere == 0


async def test_each_printer_error_code_maps_to_its_reason(printer, gcode):
    stand_in, session = printer
    assert set(upload.UPLOAD_ERRORS) == set(range(19001, 19012))
    for code, key in upload.UPLOAD_ERRORS.items():
        stand_in.answer = (200, json.dumps({"code": code, "msg": "the printer's own words"}))
        with pytest.raises(UploadError) as err:
            await _upload(stand_in, session, gcode)
        assert err.value.key == key, code
        assert str(err.value) == key           # fixed text: nothing the printer said


async def test_anything_but_a_plain_yes_is_a_refusal(printer, gcode):
    stand_in, session = printer
    for status, text in ((500, "{}"), (200, '{"code": 1}'), (200, '{"code": "nan"}'),
                         (200, '{"code": NaN}'), (200, '{"code": Infinity}'),
                         (404, "not json")):
        stand_in.answer = (status, text)
        with pytest.raises(UploadError) as err:
            await _upload(stand_in, session, gcode)
        assert err.value.key == "upload_refused", (status, text)
    for text in ('{"code": 200}', '{"code": 0}', "{}", "not json"):
        stand_in.answer = (200, text)
        await _upload(stand_in, session, gcode)


async def test_a_printer_in_cloud_mode_or_with_no_upload_address_is_refused(printer):
    stand_in, session = printer
    for info in ({"ctrlType": "cloud", "fileUploadurl": "http://x:1/gcode_upload?s=a"},
                 {"ctrlType": "lan"}, {"fileUploadurl": 5}):
        stand_in.info = info
        with pytest.raises(UploadError) as err:
            await upload.async_fetch_target(session, "127.0.0.1", port=stand_in.port)
        assert err.value.key == "upload_refused", info


# ------------------------------------------------- failures that quote the URL


class _Answer:
    def __init__(self, status, body):
        self.status, self._body = status, body
        self.content = self

    async def read(self, limit=-1):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class QuotingSession:
    """Answers /info like a printer, then fails the upload with an error that quotes the URL."""

    def __init__(self, token, error):
        self.token, self.error = token, error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", kwargs))
        return _Answer(200, json.dumps({
            "fileUploadurl": f"http://{NAMED_HOST}:18910/gcode_upload?s={self.token}"}).encode())

    def post(self, url, **kwargs):
        self.calls.append(("POST", kwargs))
        raise self.error(f"Connection reset while sending to {url}")


def _chain(err):
    seen = []
    while err is not None and err not in seen:
        seen.append(err)
        err = err.__cause__ or err.__context__
    return seen


@pytest.mark.parametrize("token", [TOKEN, SHORT])
@pytest.mark.parametrize("error", [aiohttp.ClientConnectionError, aiohttp.InvalidURL,
                                   RuntimeError])
async def test_the_token_is_never_in_a_log_line_or_an_error(gcode, caplog, token, error):
    caplog.set_level(logging.DEBUG, logger="custom_components.anycubic")
    session = QuotingSession(token, error)
    target = await upload.async_fetch_target(session, "printer.example", identifiers=())
    with pytest.raises(UploadError) as err:
        await upload.async_send_file(session, target, gcode, "part.gcode", 10)
    assert err.value.key == "upload_failed"
    # No chain at all: not even the replaced error rides along as context.
    assert _chain(err.value) == [err.value]
    assert token not in str(err.value) and token not in repr(err.value)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "upload failed" in logged, "the kind of failure is still in the debug log"
    assert token not in logged
    assert "printer.example" not in logged
    # Neither request followed a redirect, and the failed upload was not tried again.
    assert [m for m, _ in session.calls] == ["GET", "POST"]
    assert all(kwargs["allow_redirects"] is False for _, kwargs in session.calls)


async def test_a_file_that_cannot_be_opened_sends_nothing(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.anycubic")
    session = QuotingSession(TOKEN, RuntimeError)
    target = await upload.async_fetch_target(session, "printer.example")
    with pytest.raises(UploadError) as err:
        await upload.async_send_file(session, target, tmp_path / "Alice part.gcode",
                                     "Alice part.gcode", 1)
    assert err.value.key == "upload_failed"
    assert [m for m, _ in session.calls] == ["GET"]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "FileNotFoundError" in logged
    assert "Alice" not in logged and str(tmp_path) not in logged    # the kind of error only


def test_query_values_are_held_as_written_and_as_decoded():
    target = upload._target("printer.example",
                            "http://198.51.100.7:18910/gcode_upload?s=a%2Bb%3D&t=x+y", 18910)
    assert set(target.secrets) == {"a%2Bb%3D", "a+b=", "x+y", "x y"}
    assert target.url == "http://printer.example:18910/gcode_upload?s=a%2Bb%3D&t=x+y"
