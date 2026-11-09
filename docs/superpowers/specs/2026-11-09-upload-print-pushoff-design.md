# Upload, print, and push the part off

Status: design, revised 2026-11-09 after the builder check and the security review. Phase 1 ships
first and is useful alone. One owner decision is open: the optional built-in door reader (section 8).
Grounded in a read-only probe of the owner's Kobra S1 Max (firmware 2.7.1.4 with Rinkhals); stock
behavior is known from disassembly. INFERRED marks anything unproven; each has a named live test
(section 10).

## 1. Intent

The integration can watch and steer a running print but cannot start one. The owner wants to pick a
file on a phone or computer, reach Home Assistant over VPN, and have the printer print it. For
back-to-back prints with nobody at the printer, he also wants the printer to clear its own bed.

His worst outcome is pushing a print into a closed door. Every failure path below ends the same way:
no push, the part stays, the owner is told, and HA starts nothing until he says the bed is clear.

## 2. Goals, non-goals, anti-goals

Goals. Phase 1: upload a `.gcode`, `.gcode.3mf` or sliced `.3mf` from HA's own action form and
start it; "Print again" reprints the last file; every refusal is one plain line and sends nothing.
Phase 2: an opt-in push-off, ticked per print: bed and chamber cool, the door is checked, and the
head pushes the parts forward out of the door.

Non-goals (this update): a custom Lovelace card; slicing; browsing or deleting printer files; a
print queue; a camera check of the bed; multi-plate archives. Phase 1 runs on the Kobra S1 and S1
Max only (models 20025 and 20029, one firmware family). Other models stay off until a user validates
them with diagnostics, and the README says so. Push-off runs on the S1 Max only.

Anti-goals (must never happen): a push with the door closed or its reading unknown, unavailable,
restored or too old; a sweep in a job HA cannot prove it started and armed; a resume of a push-off
pause by anything in HA but the push-off state machine; Z homing, probing or leveling started by HA
with a part on the bed; the upload token anywhere persistent or logged; an automatic upload retry (a
retried multipart upload can corrupt the file); anything sent after a guard has refused.

## 3. Constraints

- Minimum Home Assistant 2024.9.0. AGPL-3.0-or-later, clean-room: prior art is used as patterns only
  (several sources are GPL/AGPL). No new Python dependency outside the optional part of section 8.
- HA's file upload caps a file at 100 MB, and its file id is usable once ("Print again" covers
  repeats). Transfer is the printer's `:18910/gcode_upload` (stock and Rinkhals; it never starts a
  print), then the MQTT `print`/`start` message. Not the OctoPrint-compatible port, not Moonraker.
- No harmless start exists. Every start ends in the firmware exit routine: filament cut, head to the
  purge chute at the current Z, bed drops 100 mm, motors off.
- Firmware facts (PROVEN): in a file only M, G and T lines run, so `PAUSE`, `RESUME` and
  `TEMPERATURE_WAIT` are skipped there while `M601` pauses. PAUSE saves the position, sets the
  nozzle to 140 C, retracts (fails below 170 C), lifts Z 2 mm and parks at the chute. RESUME returns
  to the saved position. `M84` clears homing. The Z probe is the nozzle, so homing or leveling Z
  drives it into any part left on the bed.
- Travel limits (S1 Max): X -3 to 358, Y -2 to 380. The exit routine drops the bed 100 mm, so no Z
  that HA places in a file may exceed 253, PAUSE's own 2 mm lift included.

Security rules (from the security review; they apply everywhere below)
- **Numbers and access.** Only finite numbers are accepted, and every guard is "pass if within
  range", so a missing, NaN, infinite or non-numeric value fails. Both actions are registered with
  `async_register_admin_service`.
- **Upload token.** While held, it is masked in every log line and error by `redacted_error`'s
  whole-word pass (`redacted()` skips identifiers under 7 characters, and the token may be short).
  Errors carry fixed text and are raised `from None`. Requests use `allow_redirects=False`. The host
  is pinned to the entered address, the way `_ctrl_url` pins it: the port, path and query (which
  carries the token) come from the printer, never the host.
- **Archives.** Duplicate or case-variant member names are refused. Uncompressed bytes are capped
  per member and in total, counted as they are read. Nothing is extracted by member name, and
  temporary files get generated names and are deleted in `finally`. Scans, rewrites and md5 run in
  the executor.
- **Printer-side names.** Base name only. Any character outside `A-Za-z0-9 ._()+-` becomes `_`.
  The stem is cut so that stem, marker and suffix fit 100 characters (T2 may lower this), so the
  marker never truncates. An original that already carries the marker (any case) is refused.
- **Logging.** `filepath` joins `SENSITIVE_KEYS` and `JOB_NAME_KEYS`. The file being sent joins the
  redactor's job names for the submit. Diagnostics may show push-off state and guard outcomes, never
  names.

## 4. Decisions

| # | Decision | By |
|---|---|---|
| D1, D2 | Upload from phone or computer through HA; any slicer with an S1 Max profile; `.gcode` and sliced `.3mf`. UI = HA's own action/script form (file field + push-off toggle) opened from a "Print a file" button on the printer card, plus "Print again"; no custom card | Owner |
| D3 | Push-off is opt-in and ticked each print | Owner |
| D4, D5 | Bed cools, chamber cools, head pushes the parts forward out of the open door; door closed at push time: skip that push, no waiting | Owner |
| D6 | Open-frame printers: the door check always passes | Owner |
| D7-D9 | Cool-down temperatures use print-farm values; push-off refused if the file has a brim; every failure path fails toward "no push" | Owner |
| D10 | After a skipped or failed push, HA refuses its next start until "Bed is clear" is pressed; persistent notification + event | PM |
| D11 | Brim present or undeterminable: refuse. Body scan; header `brim_type = no_brim` only as a fast "no brim"; `auto_brim` decides nothing; a prime tower's brim counts as a brim | PM |
| D12 | Door unknown, unavailable or stale counts as closed | PM |
| D13 | Enclosed printer with no usable door sensor: push-off refused at submit | PM |
| D14 | Door source baseline = any HA binary_sensor picked in options (Option B); built-in SSH reader optional (Option A) | PM; Option A pending owner |
| D15 | Push-off materials PLA and PETG only, judged on the slots the file uses; strictest target wins; a cool-down time limit, then skip | PM |
| D16, D17 | Upload via `:18910/gcode_upload`, start via MQTT `print`/`start`; token never logged. Push-off supports `.gcode` and sliced `.3mf`, rewriting the archive consistently | PM |
| D18, D19 | `M601` after the final nozzle-off and before motors-off, then the sweep, then a Z raise; HA cools, re-checks, then resumes or stops; never re-home Z with a part on the bed. A modified file never sweeps outside HA's control; with no HA it stays paused | PM |
| D20, D21 | Phase 1 then Phase 2, each gated by named live tests, owner present, bed clear. Submit guards: idle (and 10101), suffix, size, model, bed flag, push-off guards; one-line reason, nothing sent | PM |
| D22 | Bed flag on at any job seen and after any report gap; cleared only by a watched completed sweep or "Bed is clear" | PM call |
| D23 | Skirt, raft and any prime or wipe tower also refuse push-off | PM call |
| D24 | Targets: bed 28 C (PLA) / 25 C (PETG); chamber 30 C; limit 60 min; nozzle at or below 50 C before any part contact | PM call |
| D25 | "Print again" restarts the printer's copy of the unmodified original; HA keeps no copy | PM call |
| D26 | A pause in a job HA cannot prove is its armed job is stopped if its name is marked OR the signature matches, never resumed | PM call; Security |
| D27 | Phase 1 on models 20025 and 20029 only; push-off on 20029 only | PM |
| D28 | "Bed clear" includes the purge line; "done" = sweep block completed with no firmware error or pause | PM |
| D29 | Door freshness: `last_reported` within 30 s, or the door counts as closed | Security; bound chosen here |
| D30 | Resume lock: for any pause in an armed job, in a marked job, or with the bed target 0 or unknown, only the push-off state machine may resume; the Resume button goes unavailable | Security |

## 5. What the user sees

- "Print a file" on the printer card opens a form with two fields: the file (`selector: file`,
  accepting `.gcode` and `.3mf`) and "Push the part off the bed when done" (off by default; Phase
  2). The form is an example script whose fields feed the integration's action. It ships in the
  README and the example dashboard.
- "Print again" (button) reprints the last file sent through HA, never with push-off. "Bed needs
  clearing" (binary sensor) and "Bed is clear" (button, available only while the flag is on and no
  job is active; the README warns never to automate it).
- Phase 2 adds a "Push-off" sensor (`off`, `armed`, `cooling`, `pushing`, `done`, `skipped`,
  `failed`, reason as an attribute), a door-sensor option, and `push_off_done` / `_skipped` /
  `_failed` on the existing print event entity. The Resume button goes unavailable for any pause
  under the resume lock (7.4).
- Actions: `anycubic.print_file` (printer, file, push_off) and `anycubic.print_again` (printer, plus
  push_off in Phase 2; 7.7), both admin-only. A refusal is HA's error for the call: one plain
  sentence with the fix if there is one ("The printer is busy. Try again when the current job
  ends.").
- Per-entry store (small JSON, no file contents): bed flag, last report time, last-file and
  armed-job records.

## 6. Phase 1: upload, start, print again

### 6.1 Guards, in order, before anything is sent

1. The model is 20025 or 20029 ("Printing from Home Assistant is not enabled for this model yet.").
2. The printer is reachable: a report has arrived within the gap bound (6.4).
3. No other submission to this printer is in flight. One lock per printer covers upload and start.
4. The printer is idle: `info` state `free` and no job phase. Drying or loading counts as busy.
5. The bed flag is off (6.4).
6. The suffix is `.gcode`, `.gcode.3mf` or `.3mf`, and the printer-side name follows section 3. A
   `.3mf` holds exactly one plate gcode. None means "not sliced", and more than one is refused
   ("Export a single plate").
7. Size is within the cap, which HA enforces first at 100 MB. Disk space is the printer's own check
   (it reads `X-File-Length`), and its refusal is passed on.
8. The file's model, from the gcode header (`printer_model`) or the archive's slice info
   (`printer_model_id`). If it names a different printer, refuse. If it is missing or unreadable
   (PrusaSlicer often leaves it empty), Phase 1 goes ahead and push-off is refused (7.1).
9. Filament mapping. A single-filament file with no multi-color unit prints from the external
   spool. With a unit, file slot N maps to unit slot N, the slicer's default. Refuse if a used
   slot's loaded material, as last reported, is another family or not known. Refuse multi-filament
   files with no unit. Second units take single-filament files only until tested.

### 6.2 Transfer and start

1. **Upload.** The upload address and token are read fresh from the printer's info data for each
   upload, kept in memory only, and handled under the section 3 token rules. The file is POSTed as
   multipart (`filename`, `gcode`) with `X-File-Length`, streamed from disk.
2. **Upload errors.** The printer's 19001-19011 errors map to one-line reasons ("The printer
   refused the upload. Is LAN Mode on?"). There is no retry, and a failed upload starts nothing.
3. **Start.** Re-check guard 4, then publish `print`/`start` on the integration's existing `web/`
   sender path (INFERRED; T3). The message carries a fresh task id, the printer-side name, size, md5
   and file type, and the 6.1 mapping.
4. **Task settings.** Auto-leveling on. Vibration compensation, flow calibration, timelapse, the
   printer's camera detection settings (`ai_settings`) and drying off.
5. **Reply.** 10101 (previous task still active) gets one retry after about 1.5 s, as Rinkhals
   does, then is refused as busy. Other codes give the existing reason catalog's text. No reply and
   no job seen: "The printer did not confirm the start. The job may still start, because large 3MF
   files take time to unpack."

### 6.3 Print again (D25)

HA records the last file it sent: printer-side name, size, md5, type, mapping, the 6.1 results and
the 7.1 facts. "Print again" re-runs guards 1-5 and 9, then starts that printer-side name. It never
re-uploads, and it refuses any marked name (7.5). This keeps files of up to 100 MB out of HA
backups. A file since deleted or overwritten on the printer gets the printer's refusal, passed on as
"Send the file again".

### 6.4 Bed needs clearing (D10, D22)

The flag is persisted, and it is on if its history is missing. It turns on as soon as HA sees any
job, whoever started it, and after any report gap longer than the gap bound: 60 s (two missed
polls), which must stay below the shortest possible job that T3 measures, and shrinks if that job is
shorter. The time of the last report is stored, so an HA restart or a printer switched off counts as
a gap. It clears only on a sweep HA watched to completion (7.4) or on "Bed is clear". While it is
on, both actions refuse: "Clear the bed, then press Bed is clear." Reason: auto-leveling probes the
nozzle into whatever is left on the bed, and "Print again" from a phone is exactly when nobody sees
it.

## 7. Phase 2: push-off

### 7.1 Push-off guards at submit (after 6.1)

Push-off is refused, with one line and nothing sent, unless all of these hold:

- **Model.** The printer is model 20029, and the file's model reads as this printer.
- **Door.** On an enclosed model, a door sensor is configured and reads `on` or `off` within the
  freshness bound (section 8), so an event-only sensor is refused here and not at the end of a long
  print. Open-frame models pass (D6), but none has a profile yet.
- **Brim, skirt, raft, towers (D11, D23).** A case-insensitive body scan, matching both `;TYPE:`
  and `; FEATURE:` labels, finds no brim, skirt, prime tower or wipe tower. `brim_type = no_brim`
  may answer early, and `raft_layers` must be 0. A scan that cannot finish refuses. A skirt is the
  same thin, well-stuck loop as a brim. A tower is an object the footprints do not list, so the
  sweep would hit it blind.
- **Materials (D15).** Every used slot is PLA or PETG. Used slots come from the file's
  used-filament data; if that cannot be read, every listed slot counts as used. The family is the
  type's leading word, case-insensitive: "PLA+", "PLA Silk" and "PETG HF" pass; "PCTG", "TPU for
  ACE" and empty fail. PETG also needs the file to state a textured plate, because PETG can tear
  smooth PEI. Unknown plate: PETG refused, PLA allowed.
- **Readable file.** The end code is recognized (7.2). The footprints (`EXCLUDE_OBJECT_DEFINE ...
  POLYGON=`, plus the plate's object boxes in archives) and `max_z_height` can be read. The file
  heats the bed (bed temperature above 0) and has no `M600`/`M601` of its own, so in an armed job a
  pause with the bed target at 0 can only be the push-off pause.
- **Geometry.** Every footprint lies on the bed. The tallest part is at least twice the sweep height
  and at most 200 mm. Every coordinate of the park and the sweep (7.3) is within the section 3
  travel and Z limits.

### 7.2 The file change

HA uploads the unmodified original first, because Print again needs it, then a copy under the marked
name (7.5). The copy is the original with one block inserted and nothing else changed.

**Insertion point.** Directly after the file's last `M104 S0`. That line must come before the next
`M84`/`M18` with no head movement between them. Otherwise the end code is "not recognized" and
push-off is refused.

**The block, in order:** (1) bed and chamber off, repeated so the cool-down never depends on lines
after the pause; (2) lift Z to the tallest part plus 10 mm and park over the chute, quickly enough
that the nozzle is still above 170 C when PAUSE retracts (T4 measures); (3) an `M117` notice for
anyone at the touchscreen ("HA push-off: do not resume here"); (4) `M601`; (5) the sweep (7.3); (6)
a final Z raise. PAUSE saves the parked position, so RESUME returns there and not to the part. The
file's own end lines and the exit routine follow.

**Archives.** Only the plate gcode member changes. Its `.md5` sidecar must match the new member, in
the sidecar's existing case. Every other member stays byte-identical, including the header copy in
`.gcode.metadata`, since the header does not change. The result must be a zip the printer unpacks
(T4 runs one). The md5 in the start message is the md5 of the uploaded archive.

### 7.3 Sweep geometry

Computed at submit from the footprints and `max_z_height`: absolute coordinates only, no homing,
probing or extrusion, Z speed within the firmware limit.

1. Start from the park at lift height (the tallest part plus 10 mm).
2. For each X column covering the footprints, with spacing no wider than the measured pusher width:
   travel at lift height to a descent point behind the parts and behind the purge line at Y 352;
   lower to sweep height; push toward Y min to the front limit of travel at 1500-2400 mm/min; lift.
   If T5 shows there is no bed behind the purge line, the descent point sits behind the parts only,
   and the purge line becomes a known leftover (section 9).
3. End with a Z raise of at most 253.

The door side is INFERRED to be low Y, because the chute, the wipe and the purge line are all at
high Y. No sweep runs, not even in air, before T1 confirms it. Sweep height (research: 1-5 mm),
column spacing, margins and the minimum part height are fixed by the plan after T5.

### 7.4 At the end of the print

**Pauses (D26, D30).** At any pause in an armed job (7.5), in a job with a marked name, or with
the bed target at 0 or unknown, the coordinator refuses `resume` from every caller but the push-off
state machine, and the Resume button goes unavailable. Then:
- **Armed job.** A bed target of 0 is the push-off pause and starts the sequence below; no layer
  data is needed (`current_layer` and `total_layers` can be missing). A bed target above 0 is a
  mid-print pause, left to the touchscreen. Unknown: HA waits for a fresh reading and stops the job
  if none comes before the timer runs out.
- **Any other job.** HA stops it if its name is marked OR the signature matches; never both are
  required. The signature is a bed target of 0 plus, if T4 shows one, `M601`'s own reason code.
  Layer data only narrows the stop of an unmarked job: one whose layers show it mid-print (a
  cold-bed print paused for a color change) is left alone. The flag stays on and the owner is told.

**The armed job's sequence:**
1. **Cooling.** State `cooling`, and the 60-minute timer starts. HA sets the nozzle target to 0
   (PAUSE holds 140 C), may run the chamber fan if T4 shows the printer obeys while paused, and
   polls status every few seconds until the job ends (`info` alone is too slow).
2. **Cool-down target.** Wait until fresh, finite readings show the bed at or below its target, the
   chamber at or below 30 C (enclosed models), and the nozzle at or below 50 C. A stale reading
   never counts as cool.
3. **Push time (D5, D12, D29).** Read the door once. It must read `on` (open) within the freshness
   bound. Restored, unknown, unavailable or too old counts as closed. Read the temperatures again.
4. **Pushing.** If everything passes, HA sends `resume`, at most once per job, and the state is
   `pushing`. From then on, the door leaving `on` or going stale, any further pause, or an error
   makes HA send `stop`, with the result `failed`. A lost connection cannot be acted on, so the
   result is unconfirmed, which counts as failed.
5. **Skipped.** If any check fails, or the timer runs out, HA sends `stop`, with the result
   `skipped`. The part stays, and the exit routine moves at the parked height.
6. **Done.** The sweep block ran to completion with no firmware error or pause, and HA watched all
   of it. The bed flag clears.
7. **Any other ending.** The flag stays on, and HA sends a persistent notification and an event.

**Unexpected resume.** A resume HA did not send during `cooling` (from the touchscreen, Mainsail,
Moonraker, the slicer or the OctoPrint-compatible port) makes HA send `stop` at once. Whether that
stop lands in time is in section 9.

### 7.5 Recognizing its own files and jobs (D19, D26)

- **Marker.** The copy is named `<stem>-ha-pushoff<original suffix>`, visible on the touchscreen;
  one per original, overwritten on each submit, so printer storage does not fill.
- **Armed-job record.** HA persists the printer-side name and md5, the time it sent `start`, the
  task id it sent, and the push-off phase. The record binds to the one job that starts within a
  short window after HA's start, with no report gap since, and is retired when that job ends.
- **After an HA restart or report gap (6.4)**, including HA being down at the pause (the printer
  then just stays paused): a marked job is HA's only if the printer echoes the task id HA sent,
  which T3 establishes. Otherwise it is a marked foreign job, stopped at its first pause (7.4).
- **"Print again" without push-off** always starts the unmarked original (6.3).

### 7.6 Telling the owner

For `skipped` and `failed`, and for every foreign job HA stops at a pause, HA sends a persistent
notification naming the printer and the reason, and fires the matching `push_off_*` event with
`reason`. The README gets an example automation that turns the event into a phone push.

### 7.7 Print again with push-off

`anycubic.print_again` takes `push_off` (default off). When it is on, HA re-runs every 7.1 guard
that does not need the file (door, model, bed flag, idle), then restarts the marked copy from the
last push-off submit of the same original. If that submit had no push-off, it refuses: "Send the
file again with push-off ticked." The option ships only if T3 proves the printer checks the md5 at
start; otherwise a same-named file could stand in for HA's copy, and the option refuses. The button
never sets it: D3 means one tick per print, and a button has no tick.

## 8. Door sensor source

**Baseline (Option B, D14).** The user picks any HA binary_sensor with device class door or opening
(`on` = open) in the integration's options. The integration holds no SSH code and no credentials.

**Freshness (D29).** A reading counts only if its `last_reported` (present on every supported HA
version) is within 30 s. Why 30 s: the door is read once at push time, then watched through a sweep
of about a minute; a silent sensor is caught within half a minute, and one polled every 10 s gets
three chances per window. Event-only contact sensors (most Zigbee and Z-Wave door contacts) usually
fail this check, because they write state only on change. A polled sensor meets it; the README
documents one, a command-line binary sensor in the user's own configuration that reads the
printer's door switch every 10 s. T1 checks the chosen sensor against the bound.

**OPTIONAL, pending owner decision (Option A): built-in SSH reader.** The printer's door switch
(GPIO 104) is reported only to its touchscreen (polarity INFERRED: 1 = open; T1) and is readable over
SSH on Rinkhals or rooted printers only. Option A is an options step, offered only when SSH answers,
that takes SSH credentials and creates a polled "Door" binary_sensor; any SSH error or timeout makes
it unavailable, which counts as closed. Costs the owner must accept: credentials for a root shell
on the printer stored in the config entry, in plain text on disk and in every HA backup, so anyone
with HA admin rights or a backup has root on the printer; and a new dependency (an SSH client
library) with its update and supply-chain burden. Mitigations it still needs: host key pinned on
first use and refused on change, key authentication preferred, one fixed read command with nothing
user-supplied, credentials and output never logged. Dropping Option A changes nothing else.

## 9. Risks and unknowns

| Risk / unknown | Until settled | Settled by |
|---|---|---|
| Door side is low Y (INFERRED) | no sweep of any kind | T1 |
| Upload behaves as disassembled; error codes; name-length limit | Phase 1 not shipped | T2 |
| Start fields, `web/` sender, mapping, reply codes; task-id echo; md5 check at start; shortest job time; a last-job identity that survives an HA restart | Phase 1 not shipped; with no echo, every marked job after a gap is stopped; with no md5 check, 7.7 refuses; the gap rule stays as written unless the plan's conditional step is ruled in | T3 |
| Busy reply 10101 | idle guard runs first | T6 |
| `M601` pause shape and code; nozzle above 170 C at the pause; nozzle target 0 accepted while paused; what RESUME reheats to; ACE feed or flush on resume with a cold nozzle; motors on and homed through a full-length pause; `M117` visible on the touchscreen | Phase 2 not shipped. RESUME reheating above 50 C or not running clean returns Phase 2 to the owner. Lost motors or homing means the cool-down limit must sit below the idle timeout, or Phase 2 returns to the owner | T4 |
| Rewritten archive accepted | push-off on archives not shipped | T4 |
| Step-loss, bed-scratch or head-block detection pausing mid-sweep | any later pause means stop | T5 |
| Parts land inside the chamber, not out of the door | Phase 2 returns to the owner before any further build | T5 |
| Purge line not reachable from behind | known leftover; the flag still clears | T5 |
| Script form with a file field on HA 2024.9 (source-checked: the 2024.9 frontend renders script fields and the file selector) | action usable from Developer tools | T7 on the owner's HA |

Owner-accepted risks, in plain terms:
- **Unchecked resume.** A resume from the touchscreen, Mainsail, Moonraker, the slicer or the
  OctoPrint-compatible port, or one that arrives while HA is disconnected, runs the sweep with no
  door check. HA's stop may land too late; the `M117` notice is the only other guard.
- **Stop during the sweep** runs the exit routine from sweep height, through parts not yet swept
  (T9 records the path). **Nothing ejected:** a sweep that completes but ejects nothing still clears
  the flag. **Stuck door sensor:** one stuck on "open" that keeps reporting in time passes.

## 10. Live tests

The owner is present, and the bed is confirmed clear before every start.

**Phase 1 gate.**
- **T2, upload only.** The file lands; nothing starts or is deleted without the owner's OK. Record
  the name-length limit.
- **T3, start a minimal file over MQTT.** Record states and codes, and watch the exit routine. Also
  record whether reports echo HA's task id, whether a start with a wrong md5 is refused, the
  shortest job time, and whether the printer reports a last-job identity that survives an HA
  restart (`last_project`, a task id or a job start time). If it does, the plan may relax the gap
  rule to "flag only if that identity changed across the gap"; that is a conditional plan step, not
  part of this design.
- **T6, start during a print.** Expect 10101. **T7, Print again and refusals.** "Print again"
  starts the original; the refusal paths (busy, wrong suffix, bed flag, wrong model) send nothing.

**Phase 2 gate.**
- **T1, door.** No motion. The owner confirms the door side. Check that the chosen sensor tracks the
  door and reports within 30 s, and record the switch polarity.
- **T4, pause.** A 10x10x0.6 mm PLA square with the inserted block, run once as `.gcode` and once
  as a rewritten `.gcode.3mf`, with the pause held for the full 60 minutes. Pass: every item in the
  T4 row of section 9 is answered, and the motors stay on and homed. Optional experiment, not a
  commitment: does an extrusion error inside the file end the job? If so, a cold-extrusion check
  might block a sweep HA did not arm; used only if T4 proves it and it fits the 50 C contact rule.
- **T5, sweep.** A sweep 20 mm above the bed, then one light sacrificial PLA part. Pass: the parts
  land out of the door in the catch container, not inside the chamber, with no firmware pause.
  Record the purge-line result.
- **T8, restart and foreign starts.** HA restarted mid-pause: the job is recognized only with the
  task-id echo, otherwise stopped. A marked file started from the touchscreen: stopped, never
  swept. An unexpected Resume during `cooling`: stopped.
- **T9, skips and stops.** Door closed at push time, and cool-down timer expiry: skipped, stopped,
  notified, flag on. Door closed during an air sweep: record the stop latency and the exit routine's
  path from sweep height.

## 11. Left to the builder

The upload HTTP client (no new dependency), streaming and timeouts; exact start message values
(T3); scanning files of up to 100 MB without loading them whole; reading header keys, archive
configs and used slots; how the archive is rewritten, within 7.2 and section 3; sweep height, column
spacing, margins, speeds and minimum part height (7.3), fixed after T5; store schema, entity and
translation keys, services.yaml, the `file_upload` dependency and poll cadence; the example script,
dashboard button, README and automation; offline tests for every guard, the file change, the name
rules and the 7.4 state machine (no test code here).
