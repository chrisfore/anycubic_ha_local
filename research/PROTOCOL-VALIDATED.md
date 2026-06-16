# LAN-Mode Protocol — VALIDATED on real hardware (2026-06-15)

> Captured live from the user's **AnyCubic Kobra S1 Max**, firmware **2.6.9.6**, at `<printer-ip>`.
> Read-only (GET /info, signed POST /ctrl, MQTT subscribe + `action:"query"` status requests). No printer state changed.
> Fixtures: `research/probe/*.json`. Capture tools: `research/probe/capture_*.py`, `probe_peripherie.py`.
>
> **CORRECTION (supersedes earlier drafts):** ACE **humidity, box temperature, and drying status ARE available locally**
> (box-level fields in `multiColorBox/report`). They are **activity-gated** — they only populate when the ACE is actively
> feeding during a print; idle/standalone load-unload reports omit them, which is why the first captures missed them.
> There is **NO cloud requirement** for any value.

## Device identity (/info + /ctrl)
| Field | Value |
|-------|-------|
| modelName | `Anycubic Kobra S1 Max` |
| **modelId** | **`20029`** |
| firmware | `2.6.9.6` |
| serial (cn) | `<serial>` |
| MAC (usn) | `<mac>` |
| **deviceId** (MQTT) | `<device-id>` |
| ctrlType | **`lan`** (LAN mode ON) |
| video stream | `http://<printer-ip>:18088/flv` (FLV, likely on-demand) |
| ACE hardware model_id | `40002` |

Ports: `18910` info/ctrl · `9883` local MQTT (TLS) · `18088` FLV · `80` gkapi (OctoPrint 1.8.7 compat). `7125`+`22` **closed** → stock, not rooted. `/webcam/?action=snapshot` 404s on this fw.

## Handshake (VALIDATED end-to-end)
1. `GET http://IP:18910/info` → `{ token, cn, ctrlInfoUrl, modelId, … }`
2. `POST {ctrlInfoUrl}?ts=&nonce=&sign=&did=` — `ts`=epoch ms, `nonce`=rand 6 alnum, `did`=rand 32 (A-Z0-9), `sign`=`md5(md5(token[:16])+str(ts)+nonce)` (hex; the documented "double url-encode" is a no-op on a hex digest).
3. Response `{code:200, message:"success", data:{token: local_token, info:<b64>}}`.
4. AES-CBC decrypt `data.info`: key=`token[16:32]` (ascii), IV=`local_token` ascii right-padded/truncated to 16 B, PKCS7 → `{broker:"mqtts://IP:9883", username, password, deviceId, devicecrt, devicepk, …}`.
5. MQTT over TLS to `IP:9883` (self-signed — server cert NOT verified; **no client cert needed**), `username_pw_set(username,password)` → CONNACK accepted.

## MQTT topology (VALIDATED)
- **Query (read):** publish `anycubic/anycubicCloud/v1/web/printer/{modelId}/{deviceId}/{type}` body `{type,action:"query",timestamp,msgid,data:null}` — a read-only status request, NOT control.
- **Report:** `anycubic/anycubicCloud/v1/printer/public/{modelId}/{deviceId}/{type}/report`.
- **Ack:** `…/{deviceId}/response` → `{msgid}` (acks every received msgid — NOT a recognition signal).
- **Report `action` field varies:** `query`/`report`/`refresh`/`workReport`/`setInfo`. **Parsers MUST key off report TYPE/topic, not `action`**, or most box updates are dropped.
- **Report types observed:** `info`, `tempature` (sic — wire string literally misspelled; do not normalize), `fan`, `light`, `multiColorBox` (ACE), `print`, `status` (workReport), `file`, `peripherie`, `video`. `print`/`multiColorBox` are **activity-gated** (emit during print/load/feed; idle returns nothing).

## `info` report `.data` (master object)
```
printerName, model, ip, version (fw)
state : "free" (idle) | "busy" (any activity)
temp  : { curr_hotbed_temp, curr_nozzle_temp, curr_chamber_temp, target_hotbed_temp, target_nozzle_temp, target_chamber_temp }   # chamber = enclosure, NOT the ACE box
print_speed_mode, fan_speed_pct, aux_fan_speed_pct, box_fan_level
project / last_project : { progress %, curr_layer, total_layers, remain_time MINUTES, print_time s,
                           supplies_usage mm, state, print_status int, filename, pause, project_type }
urls : { rtspUrl, fileUploadurl }
features : { auto_leveling_support, drying_first_support, camera_timelapse_support, gcode_3mf_support,
             preheating_support, pre_cancel_support, fod_support, … }   # capability map for feature-gating
```
`tempature` and `fan` reports mirror the temp/fan sub-objects (fan adds `taskid` during activity).

## Status / lifecycle model — VALIDATED (live print + pause/resume/stop)
- Top-level `info.data.state`: `free` / `busy`.
- `info.data.project.state` machine: `preheating → auto_leveling → vibrating → flow_calibrating → printing → pausing → paused → resuming → resumed → stopping → stoped` (+ `updated`); idle job ends `finished` (via `last_project`). **Note `stoped` is the wire spelling (single-p).** Parse unknown states pass-through.
- `info.data.project.pause` int (authoritative pause flag): `0`=running, `1`=paused, `2`=pausing, `3`=resuming, `4`=stopping. `print_status` ints observed: `{1,2,6,9,10,11}` (don't rely on these; use `project.state` + `pause`).

## ACE 2 (`multiColorBox`) — VALIDATED full box object (during active print)
Report topic `…/printer/public/20029/{deviceId}/multiColorBox/report`. `data.multi_color_box` is an **array** (multi-ACE ready). Full box object:
```
{ id           : 0,                       # box id (0 = primary)
  status       : 1,                       # BOX status (distinct from slot.status)
  model_id     : 40002,                   # ACE hardware id (use for device registry)
  auto_feed    : 0,
  loaded_slot  : -1,                       # active slot (-1 = none)
  feed_status  : { code:200, type:-1, current_status:-1, slot_index:-1 },   # OBJECT, not int
  temp         : 35,                       # ACE BOX temperature °C   ← LOCAL
  humidity     : 24,                       # ACE BOX humidity %        ← LOCAL
  drying_status: { status:0, target_temp:0, duration:0, remain_time:0 },    # ← LOCAL
  slots        : [ …per-slot… ] }
```
Per slot:
```
{ index:1, type:"PETG" ("" = empty), color:[R,G,B], color_group:[[R,G,B,A]],
  consumables_percent:100, status:5 (5=loaded,4=ready, empty→type ""), edit_status:0,
  sku:"AHPEFG-102" (CAN be "" on a loaded slot → treat as Optional/None), icon_type:0 }
```
Sibling `data.head_tools_model` also present (toolhead id) — handling TBD.

### Parsing rules (important)
- **Activity-gated:** box-level `temp`/`humidity`/`drying_status` appear only during active ACE feed; idle/load-unload reports omit them. Don't conclude "absent" from an idle capture.
- **Humidity dual-shape:** full reports carry `box.humidity`; slim reports carry `drying_status.humidity` with the other null. Parse `box.get("humidity") or box.get("drying_status",{}).get("humidity")`; **never overwrite a known value with None.**
- **On-demand action = `getInfo` (NOT `query`):** publishing `multiColorBox` with `action:"query"` returns nothing; `action:"getInfo"` returns the full box (validated live mid-print). The printer also does **not push** multiColorBox autonomously during a steady print (only on slot load/unload/feed events) — so the integration must **poll** it on an interval to keep ACE state fresh. (Printer status info/tempature/fan/light *are* pushed autonomously.)
- **Incremental:** reports may carry 0–1 slots or the full set. **Merge by `(box id, slot index)`** into cached state; track `loaded_slot`; don't assume a full array per message.
- **Initial state:** idle returns nothing — accumulate from events; persist via `restore_state`. (Test whether a `getInfo`/full-state query returns idle state — reference uses a `getInfo` action.)

## Control commands — VALIDATED (captured live; exact actions)
Publish to `anycubic/anycubicCloud/v1/web/printer/{modelId}/{deviceId}/{type}` with `{type, action, timestamp, msgid, data}`:
| Function | type | action | data |
|----------|------|--------|------|
| Pause print | `print` | `pause` | `{taskid}` |
| Resume print | `print` | `resume` | `{taskid}` |
| **Stop print** | `print` | `stop` (INFERRED, near-certain — confirm on first use) | `{taskid:"-1"}` |
| Set temps/fans/speed | `print` | `update` | `{taskid, settings:{ target_nozzle_temp, target_hotbed_temp, fan_speed_pct, aux_fan_speed_pct, box_fan_level, print_speed_mode }}` (any subset) |
| Light | `light` | `control` | `{type:2, status, brightness}` |
| ACE auto-feed | `multiColorBox` | `setAutoFeed` | `{multi_color_box:[{id, auto_feed}]}` |
| ACE drying start | `multiColorBox` | `setDry` | `{multi_color_box:[{id, drying_status:{status:1, target_temp, duration}}]}` |
| ACE drying stop | `multiColorBox` | `setDry` | `{multi_color_box:[{id, drying_status:{status:0}}]}` |
| Camera start/stop | `video` | `startCapture`/`stopCapture` | `null` |
Printer acks on `…/{type}/report` with `code:200`. `taskid` observed as `"-1"` (current job). `print_speed_mode` 1/2/3 = silent/standard/sport. Drying example: `target_temp:45, duration:240` (min). **Stop action = `"stop"` (inferred from the captured state machine `stopping→stoped`, mirroring pause→pausing / resume→resuming; not directly captured because the recording script dropped the `action` field on identical-data commands — confirm on first use).**

## Chamber light — both states captured
`light.data.lights[]` = `[{type:2, status, brightness}]`. Off `{status:0,brightness:0}`, On `{status:1,brightness:100}` → v1.0 light *state*; v1.1 light *control*.

## Camera — VALIDATED (live stream is HA-compatible)
On-demand. **Start/stop are benign commands** (camera only; no print impact) — captured live:
- **Start:** publish `…/web/printer/{modelId}/{deviceId}/video` `{type:"video", action:"startCapture", timestamp, msgid, data:null}` → printer reports `…/video/report` `state:"initSuccess", code:200`.
- **Stop:** same topic, `action:"stopCapture"` → report `state:"pushStopped"`.
- **Stream:** after `initSuccess`, `info.urls.rtspUrl` = `http://IP:18088/flv` goes live. **Probed: HTTP-FLV, video codec id 7 = H.264 (AVC).** HA `stream`/go2rtc/ffmpeg consume this directly as a `stream_source`.
- `/webcam/?action=snapshot` 404s on this fw (no still-snapshot endpoint).

→ **v1.0 Camera entity:** issue `startCapture` when HA requests the stream, expose `http://IP:18088/flv` as `stream_source` (ffmpeg), `stopCapture` when idle/closed. Add `ffmpeg` to manifest dependencies. (Although it publishes a command, it's camera-only and safe — consistent with monitor-only.)

## peripherie report
`{camera, multiColorBox, udisk}` (1/0 presence flags) — peripheral inventory only, NOT environment data.
