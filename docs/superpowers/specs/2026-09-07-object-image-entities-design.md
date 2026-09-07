# Object image entities (issue #13)

Expose the printer's own render of the job being printed as Home Assistant `image`
entities: the slicer thumbnail and the 512x512 top view.

## Why this needs a design at all

The obvious version — "read the images out of the report and show them" — does not
work, because the printer does not send the report. Three facts, all established
empirically on a Kobra 3 V2 (firmware 1.1.2.8) by the reporter, drive the whole design:

1. **`file` is never pushed.** Starting a print produces no `file` report. The message
   arrives only when the AnyCubic Slicer's Workbench is open, because the Slicer asks
   for it. An integration that waits for a push waits forever.
2. **The request works.** Publishing `fileDetails` ourselves does produce the report,
   with no Slicer running. Confirmed 2026-09-07.
3. **The payload is large.** Three base64 blobs — `thumbnail`, `png_image`,
   `svg_image` — in one message, per job.

Fact 1 is the same trap that made issue #9 take three wrong diagnoses: a report that
only appears while the Slicer is open looks like a report the printer sends.

## Wire protocol

Request, published to `.../web/printer/{modelId}/{deviceId}/file`:

```json
{"type": "file", "action": "fileDetails", "timestamp": 1757270000000,
 "msgid": "<uuid>", "data": {"root": "local", "filename": "<name>", "plate_index": 1}}
```

`filename` must be the printer's own spelling. A job started from the printer's screen
is named `.3mf_temp/<name>.gcode`; from the Slicer it is `<name>.gcode`. The prefixed
form is confirmed working, so the request quotes `PrinterState.filename` verbatim.
`display_filename()` exists for the UI and must not be used here.

Response (`data.file_details`), abridged:

| Field | Use |
|---|---|
| `thumbnail` | base64 PNG, ~230x110 — the slicer preview. **Entity.** |
| `png_image` | base64 PNG, 512x512 — top view of the plate. **Entity.** |
| `svg_image` | base64 SVG. **Dropped.** |
| `paint_infos` | per-object colour, material, `filament_used`. Attributes. |
| `objects_skip_parts` | object names. Attributes. |
| `plate_name`, `filename` | correlation only; both redacted in logs. |

`svg_image` is dropped deliberately: it does not render even for the reporter who
supplied it, an HA `image` entity would have to declare `image/svg+xml` for it, and it
is a third view of something already covered by two working images.

## Components

**`models.py` — `ObjectImages` dataclass + `parse_file_details(data)`.** Pure, no HA
imports, matching every other parser in that file. Decodes base64 to `bytes` at parse
time so a malformed blob fails in one place. Fields: `thumbnail: bytes | None`,
`top_view: bytes | None`, `paint_infos: list[dict]`, `skip_parts: list[str]`,
`filename: str | None` (which job these belong to).

**`coordinator.py`** already requests `fileDetails` once per job via
`_request_file_details()`. Add a `file` branch to `_apply` that parses the response into
`self.data.object_images`, guarded three ways:

- Ignore reports whose `action` is not `fileDetails` — the `file` topic also carries
  `listLocal`, `deleteLocal`, `listUdisk` and `cloudRecommendList` in the reference
  implementations.
- Ignore a response whose `filename` no longer matches the current job, so a slow
  answer for the previous print cannot overwrite the new one.
- Reject any blob over `MAX_IMAGE_BYTES` (2 MiB) and log it, rather than holding
  unbounded memory from a device we do not control.

**`image.py` — new platform.** Two `AnycubicImageEntity` instances (`thumbnail`,
`top_view`) on the printer device, created on the first successful parse, using the
same created-on-report pattern as the external spool sensor. `async_image()` returns
the held bytes; `image_last_updated` is set when a new job's images land, which is what
drives the frontend to re-fetch. `_attr_content_type = "image/png"`.

Add `Platform.IMAGE` to `PLATFORMS`, and `thumbnail` / `top_view` to `strings.json` and
`translations/en.json`.

## Lifecycle

| Event | Effect |
|---|---|
| `print` report names a new file | Request sent (already implemented) |
| `fileDetails` response, filename matches | Images replaced, `image_last_updated` bumped |
| `fileDetails` response, filename stale | Discarded |
| Job ends | Images retained — the last print's preview stays, matching how `progress` and `filename` already behave at idle |
| Integration reload | Nothing held; images return on the next job |

Images are **not** persisted across restarts. They are re-requestable, and writing
hundreds of KB per job into the HA state machine or restore-state to save one MQTT
round-trip is a bad trade.

## Memory

Two blobs, one job at a time: ~50 KB thumbnail plus a 512x512 PNG. Bounded by
`MAX_IMAGE_BYTES` per blob and by only ever holding the current job's set.

The existing `MAX_LOGGED_STR` truncation already keeps these out of debug logs.
Diagnostics shares `redacted()` and so is covered too, but `parse_file_details` output
must not be added to the diagnostics dump regardless — bytes do not belong in a JSON
attachment users post publicly.

## Testing

- `test_models.py` — parse a captured payload; base64 decodes to real PNG bytes
  (`\x89PNG` magic); missing keys give `None` rather than raising; oversized blob rejected.
- `test_coordinator.py` — a `fileDetails` response populates `object_images`; a
  `listLocal` response on the same topic does not; a response for a stale filename is
  discarded.
- `test_image.py` — both entities appear after the first response and serve the right
  bytes; they do not exist before one arrives; `image_last_updated` advances on a new job.

Fixture: `tests/fixtures/file_details.json`, with tiny real PNGs rather than the
reporter's full payload — the shape is what matters and a fixture should stay readable.

## Not doing

- **`svg_image`** — see above.
- **Persisting images across restarts** — cost far exceeds one round-trip.
- **Requesting images for jobs that are not running** (browsing `listLocal` and
  fetching previews for stored files). A plausible future feature, unrelated to this
  issue, and it would turn a per-job request into a per-file crawl.
- **A camera entity for the top view.** `image` is the right platform; `camera` implies
  a stream and this repo already has a real one.

## Open question for implementation

`plate_index` is hardcoded to 1. Every capture so far is single-plate. Whether a
multi-plate 3MF needs the running plate's index is unknown, and there is no capture of
one. Left as 1 until a multi-plate job proves otherwise; the field is already in the
request, so the fix would be sourcing the value rather than changing the protocol.
