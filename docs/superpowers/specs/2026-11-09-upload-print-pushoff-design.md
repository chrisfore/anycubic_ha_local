# Upload, print, and push the part off

Status: design, revised 2026-11-09 for the owner's "Clear the bed" redesign of Phase 2; Phase 1 is
built and section 6 is unchanged. No owner decision is open. Grounded in a read-only probe and the
first live session on the owner's Kobra S1 Max (firmware 2.7.1.4 with Rinkhals); INFERRED marks
anything unproven, each with a live test (10).

## 1. Intent

The integration can watch and steer a running print but cannot start one. The owner wants to pick a
file on a phone or computer, reach Home Assistant over VPN, and have the printer print it (Phase 1).
Phase 2, in his words: "No auto push. Instead when a print is finished a 'clear the bed' button
appears and user can click it to push the print off the bed. When clicked is when the door switch
and other checks happen."

His worst outcome is pushing a print into a closed door. HA never pushes on its own, and every
failure ends with no push, the part left, the owner told, and no new start until the bed is clear.

## 2. Goals, non-goals, anti-goals

Goals. Phase 1: upload a `.gcode`, `.gcode.3mf` or sliced `.3mf` from HA's own action form and
start it; "Print again" reprints the last file; every refusal is one plain line and sends nothing.
Phase 2: a prepared print ends paused over the chute with the head lifted, and a "Clear the bed"
button appears. On a press, HA checks the job, the door, the temperatures and the motors, and only
then lets the head push the parts forward out of the door.

Non-goals (this update): a custom Lovelace card; slicing; browsing or deleting printer files; a
print queue; a camera check of the bed; multi-plate archives. Phase 1 runs on the Kobra S1 and S1
Max only (20025, 20029); other models stay off until a user validates them with diagnostics, as the
README says. Clearing runs on the S1 Max only.

Anti-goals (must never happen): a push without the owner's press; with a door sensor, a push it does
not show opened after the pause, or reads closed, unknown or unavailable; with none, a push without
the user's confirmation (D34); a sweep in a job HA cannot prove it started and
armed; a resume of a push-off pause by anything in HA but the press path; Z homing, probing or
leveling started by HA with a part on the bed; the upload token anywhere persistent or logged; an
automatic upload retry; anything sent after a guard or a press check has refused.

## 3. Constraints

- Minimum Home Assistant 2024.9.0. AGPL-3.0-or-later, clean-room: prior art is used as patterns only
  (several sources are GPL/AGPL). No new Python dependency.
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
  `async_register_admin_service`, and the "Print again" and "Bed is clear" buttons check in
  `async_press` that the pressing user is an admin.
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
  The stem is cut so that stem, marker and suffix fit 100 characters (T2 kept 150), so the
  marker never truncates. An original that already carries the marker (any case) is refused.
- **Logging.** `filepath` joins `SENSITIVE_KEYS` and `JOB_NAME_KEYS`. The file being sent joins the
  redactor's job names for the submit. Diagnostics may show push-off state and guard outcomes, never
  names.

## 4. Decisions

| # | Decision | By |
|---|---|---|
| D1, D2 | Upload from phone or computer through HA; any slicer with an S1 Max profile; `.gcode` and sliced `.3mf`. UI = HA's own action/script form opened from a "Print a file" button on the printer card, plus "Print again"; no custom card | Owner |
| D6 | Open-frame printers: the door check always passes | Owner |
| D7-D9 | Print-farm cool-down temperatures (now press-time limits); no clearing if the file has a brim; every failure path fails toward "no push" | Owner |
| D10 | After a push that fails or is not made, HA refuses its next start until "Bed is clear" is pressed; persistent notification + event | PM |
| D11 | Brim present or undeterminable: refuse. Body scan; header `brim_type = no_brim` only as a fast "no brim"; `auto_brim` decides nothing; a prime tower's brim counts as a brim | PM |
| D12 | With a door sensor, unknown or unavailable counts as closed | PM |
| D14 | Door source = any HA binary_sensor picked in options (Option B). Option A, the SSH switch reader, is dropped: the switch does not follow the door (step a) | PM |
| D15 | PLA and PETG only, judged on the slots the file uses; strictest limit wins. The cool-down time limit is gone with D31 | PM |
| D16, D17 | Upload via `:18910/gcode_upload`, start via MQTT `print`/`start`; token never logged. Clearing supports `.gcode` and sliced `.3mf`, rewriting the archive consistently | PM |
| D18, D19 | `M601` after the final nozzle-off and before motors-off, then the sweep, then a Z raise; HA resumes only on a press that passes every check; never re-home Z with a part on the bed. A prepared file never sweeps outside HA's control; with no HA it stays paused | PM |
| D20, D21 | Phase 1 then Phase 2, each gated by named live tests, owner present, bed clear. Submit guards: idle (and 10101), suffix, size, model, bed flag, and the 7.1 checks when clearing is asked for explicitly; one-line reason, nothing sent | PM |
| D22 | Bed flag on at every entry load, at any job seen and after any in-session report gap; cleared only by a watched completed sweep or "Bed is clear" | PM call; load rule from review fix I1 |
| D23 | Skirt, raft and any prime or wipe tower also rule out clearing | PM call |
| D24 | Press-time limits: bed 28 C (PLA) / 25 C (PETG); chamber 30 C; nozzle at or below 50 C before any part contact | PM call |
| D25 | "Print again" restarts the printer's copy of the file; HA keeps no copy | PM call |
| D26 | A pause in a job HA cannot prove is its armed job is stopped if its name is marked OR the signature matches, never resumed | PM call; Security |
| D27 | Phase 1 on models 20025 and 20029 only; clearing on 20029 only | PM |
| D28 | "Bed clear" includes the purge line; "done" = sweep block completed with no firmware error or pause | PM |
| D29 | Door evidence (with a sensor): a change to open reported after the push-off pause began, and open at the press; during the sweep any change away from open stops it. Replaces the 30 s `last_reported` bound | PM refinement of D34; Security origin |
| D30 | Resume lock: for any pause in an armed job, in a marked job, or with the bed target 0 or unknown, only the press path may resume; the Resume button goes unavailable | Security |
| D31 | No auto push. A prepared print that finishes shows a "Clear the bed" button; a press runs the door and other checks and pushes only if all pass. Replaces D3 (push-off ticked per print) and the automatic push of D4/D5 (bed and chamber cool, the head pushes the parts out of the open door, closed door means no push); their checks become press checks | Owner |
| D32 | Press checks (7.4) refuse with what to wait for; no retry and no "push when cool" arming; a motor window ends the button; Stop is unchanged | PM |
| D33 | "Tick box": preparation is a per-print toggle, "Allow Clear the bed" (label to be confirmed by the UX reviewer), off by default; Print again follows the same field (7.7) | Owner |
| D34 | "Ignore the door if there is no sensor on the door on the printer. We can just pop up a warning with the button." No sensor: the door check is skipped and the press needs a confirmed warning (7.4). Replaces D13 (no door source, no preparation) | Owner |

## 5. What the user sees

- "Print a file" on the printer card opens a form with the file field (`selector: file`, accepting
  `.gcode` and `.3mf`). The form is an example script whose fields feed the integration's action,
  shipped in the README and the example dashboard. Phase 2 adds the "Allow Clear the bed" tick box.
- "Print again" (button) reprints the last file sent through HA. "Bed needs clearing" (binary
  sensor) and "Bed is clear" (button, available only while the flag is on and no job is active; the
  README warns never to automate it). Both buttons are admin-only.
- Phase 2 adds a "Clear the bed" button (admin-only, same context check), a "Bed clearing" sensor
  (`off`, `armed`, `ready`, `pushing`, `done`, `failed`, `expired`, `stopped`; reason attribute), a
  optional door-sensor option, the `anycubic.clear_bed` action (printer, `door_open_confirmed`), and
  `clear_ready` / `clear_done` / `clear_failed` on the print event entity. The example dashboard's
  "Clear the bed" control calls that action behind a confirmation (7.4). The Resume button goes
  unavailable under the resume lock (7.4).
- Actions: `anycubic.print_file` (printer, file) and `anycubic.print_again` (printer), admin-only;
  Phase 2 gives both the "Allow Clear the bed" tick box (7.7). A refusal is HA's error for the call:
  one plain sentence with the fix ("The printer is busy. Try again when the current job ends.").
- Store (small JSON, no file contents): last-file record; Phase 2 adds the armed-job record.

## 6. Phase 1: upload, start, print again

### 6.1 Guards, in order, before anything is sent

1. The model is 20025 or 20029 ("Printing from Home Assistant is not available for this printer
   model yet.").
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
   refused the upload. Check that LAN Mode is on."). There is no retry, and a failed upload starts
   nothing.
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

The flag is not stored. It is set unconditionally every time the entry loads (HA start, reload,
options change), because a host with no battery-backed clock can misjudge how long it was down. It
also turns on when HA sees any job, whoever started it, and, within a session, after a report gap
longer than the gap bound on the monotonic clock: 60 s (two missed polls), kept below the shortest
job T3 measures. A printer switched off mid-session therefore counts as a gap. It clears only on a
sweep HA watched to completion (7.4) or on "Bed is clear". While it is on, both actions refuse:
"Clear the bed, then press Bed is clear." Reason: auto-leveling probes the nozzle into whatever is
left on the bed, and "Print again" from a phone is exactly when nobody sees it.

## 7. Phase 2: clear the bed

### 7.1 Eligibility at submit (after 6.1)

A print is prepared for clearing (7.2) only when "Allow Clear the bed" is ticked (7.7) and all of
these hold. A ticked print that fails one is refused with one line, and nothing is sent (D21).

- **Model.** The printer is model 20029, and the file's model reads as this printer.
- **Brim, skirt, raft, towers (D11, D23).** A case-insensitive body scan, matching both `;TYPE:` and
  `; FEATURE:` labels, finds no brim, skirt, prime tower or wipe tower. `brim_type = no_brim` may
  answer early, and `raft_layers` must be 0. A scan that cannot finish fails. A skirt is the same
  thin, well-stuck loop as a brim; a tower is an object the footprints do not list.
- **Materials (D15).** Every used slot is PLA or PETG. Used slots come from the used-filament data;
  if that cannot be read, every listed slot counts. The family is the type's leading word, case-
  insensitive: "PLA+", "PLA Silk" and "PETG HF" pass; "PCTG", "TPU for ACE" and empty fail. PETG also
  needs the file to state a textured plate, because PETG can tear smooth PEI.
- **Readable file.** The end code is recognized (7.2). The footprints (`EXCLUDE_OBJECT_DEFINE ...
  POLYGON=`, plus the plate's object boxes in archives) and `max_z_height` can be read. The file
  heats the bed and has no `M600`/`M601` of its own, so in an armed job a pause with the bed target
  at 0 can only be the push-off pause.
- **Geometry.** Every footprint lies on the bed. The tallest part is at least twice the sweep height
  and at most 200 mm. Every coordinate of the park and the sweep (7.3) passes the section 3 limits.

### 7.2 The file change

HA uploads the unmodified original first, because Print again needs it, then a prepared copy under
the marked name (7.5): the original with one block inserted after its last `M104 S0`. That line
must come before the next `M84`/`M18` with no head movement between them; otherwise the end code is
"not recognized". A push is only possible from this pause, because after a normal end the exit
routine releases the motors, and homing Z again would drive the nozzle into the part.

The block, in order: (1) bed and chamber off, repeated so cooling never depends on lines after the
pause; (2) lift Z to the tallest part plus 10 mm and park over the chute, quickly enough that the
nozzle is still above 170 C when PAUSE retracts (T4 measures); (3) an `M117` notice for anyone at
the touchscreen ("HA Clear the bed: do not resume here"); (4) `M601`; (5) the sweep (7.3); (6) a
final Z raise. PAUSE saves the parked position, so RESUME returns there and not to the part.

Archives: only the plate gcode member changes. Its `.md5` sidecar must match it, in the sidecar's
existing case. Every other member stays byte-identical, including `.gcode.metadata`. The result must
be a zip the printer unpacks (T4 runs one). The start message carries the uploaded archive's md5.

### 7.3 Sweep geometry

Computed at submit from the footprints and `max_z_height`: absolute coordinates only, no homing,
probing or extrusion, Z speed within the firmware limit.

1. Start from the park at lift height (the tallest part plus 10 mm).
2. For each X column covering the footprints (spacing no wider than the measured pusher width):
   travel at lift height to a descent point behind the parts and behind the purge line at Y 352;
   lower to sweep height; push toward Y min to the front limit of travel at 1500-2400 mm/min; lift.
   If T5 shows no bed behind the purge line, the descent point sits behind the parts only, and the
   purge line is a known leftover.
3. End with a Z raise of at most 253.

The door side is INFERRED to be low Y (chute, wipe and purge line are at high Y); no sweep runs, not
even in air, before T1 confirms it. Sweep height (1-5 mm), spacing and margins are fixed after T5.

### 7.4 At the end of a prepared print

**Pauses (D26, D30).** At any pause in an armed job (7.5), a marked job, or with the bed target at 0
or unknown, the coordinator refuses `resume` from every caller but the press path, and the Resume
button goes unavailable. In an armed job, a bed target of 0 is the push-off pause (no layer data
needed); above 0 is a mid-print pause, left to the touchscreen; unknown keeps the button unavailable
until a fresh reading. Any other job is stopped if its name is marked OR the signature (bed target 0,
plus `M601`'s reason code if T4 shows one) matches; layer data only spares an unmarked job whose
layers show it mid-print (a cold-bed color change). The flag stays on and the owner is told.

**Ready.** At the push-off pause HA sets the nozzle target to 0 if T4 shows that is accepted and
harmless (PAUSE holds 140 C), polls status every few seconds, sets `ready` and notifies "Print
finished. Ready to clear the bed." It never resumes on its own: no timer, retry or arming.

**Motor window.** The button is available only within the window T4 measures for the motors and
homing to stay on, less a margin, counted on the monotonic clock from when HA saw the pause. If HA
cannot know when the pause began (a restart or a report gap), the window counts as expired. At
expiry the state becomes `expired`, the button goes unavailable, and HA notifies "Press Stop, then
clear the bed by hand." (Stop moves the head, so it comes first.) No drop in T4 means no window.

**On a press**, HA checks in order: (1) the call carries an admin user (a call with no user, such
as an automation, is refused, which is stricter than the Phase 1 buttons); (2) the job is the armed
job, still paused at the signature; (3) the motor window has not expired; (4) the door: with a
sensor, it reported a change to `on` after the push-off pause began and reads `on` now (D29); with
none, the call carries `door_open_confirmed: true` (D34); (5) the bed is at or below its limit, (6)
the chamber at or below 30 °C, and (7) the nozzle at or below 50 °C (D24). Readings must be fresh
and finite. The first failure refuses with one line saying what to wait for or do ("The bed is
34 °C. Wait until it is 28 °C or cooler."; "The door has not been opened since the print finished.
Open it, then press Clear the bed."), sends nothing, and leaves the state at `ready`.

**Where the warning lives (D34).** A button entity cannot show a dialog. The example dashboard's
"Clear the bed" control calls `anycubic.clear_bed` with `door_open_confirmed: true` behind a
tap-action confirmation, "Open the printer door before clearing the bed." With no sensor, a press
from anywhere without that warning is refused with one line ("No door sensor is set up. Use Clear
the bed on the dashboard, which asks you to confirm the door is open."). That covers the button
entity on its more-info page, voice, and an action call without the field. Automations are refused
by check 1, and the README says never to automate the action or put the field in a script. With a
sensor, the button entity and the action both work, and the confirmation is harmless.

**Pushing.** All pass: HA sends `resume` (at most once per job); state `pushing`. From then on, the
door sensor (if any) changing away from `on` (including unavailable or unknown), any further pause,
or an error makes HA send `stop`: `failed`. A
lost connection cannot be acted on; its unconfirmed result counts as `failed`. **Done:** the sweep
block ran to completion with no firmware error or pause, HA watched all of it, and the flag clears.

**Finishing without pushing.** The existing Stop needs no change: it is not a resume, so the lock
does not cover it. It ends the job, leaves the part, and moves at the parked height above the parts
(the runbook kit says the cancel routine homes X/Y at the current height; T4 observes it). The flag
stays on; state `stopped`. A resume HA did not send during `ready` (touchscreen, Mainsail,
Moonraker, slicer, OctoPrint-compatible port) makes HA send `stop` at once (section 9).

### 7.5 Recognizing its own files and jobs (D19, D26)

- **Marker.** The prepared copy is `<stem>-ha-pushoff<original suffix>`, one per original,
  overwritten on each submit.
- **Armed-job record.** HA persists the printer-side name and md5, the time it sent `start`, the
  task id it sent and the clearing state. It binds to the one job that starts within a short window
  after HA's start, with no report gap since, and is retired when that job ends.
- **After an HA restart or report gap (6.4)**, including HA being down at the pause (the printer just
  stays paused): a marked job is HA's only if the printer echoes HA's task id (T3). Otherwise it is
  a marked foreign job, stopped at its first pause.

### 7.6 Telling the owner

A persistent notification names the printer and the reason for `ready`, `expired` and `failed`, for
every foreign job HA stops. The
`ready` notice replaces the old skipped-push notice. Events `clear_ready`, `clear_done` and
`clear_failed` (also used for `expired`) carry `reason`; the README's example automation turns
`clear_ready` into a phone push.

### 7.7 The tick box and Print again (D33)

Preparation is a per-print tick box, "Allow Clear the bed" (the UX reviewer confirms the label with
the Phase 2 strings), off by default, on the form and on both actions. Unticked, the print is a
normal Phase 1 print. A prepared print ends paused at the chute; to clear by hand, press Stop,
remove the part once the head is still, then press "Bed is clear". Print again follows the same
field. Ticked, it restarts the last prepared copy of the same original after re-running the 7.1
checks that do not need the file; with no prepared copy it refuses ("Send the file again with Allow
Clear the bed ticked."). Unticked, and from the "Print again" button, which has no tick, it starts
the original. Restarting a prepared copy ships only if T3 proves the printer checks the md5 at start
(otherwise a same-named file could stand in for HA's copy).

### 7.8 Conditional fallback: cool-down inside the file (only if T4 calls for it)

Applies only if T4 run 1 shows a motor window shorter than a realistic cool-down. If the motors never
drop, or drop later than that, the design above stands. A printer running a file does not idle, so
the cool-down can happen in the file, before the pause, with the motors held (INFERRED; T4 run 3).
The block becomes: (1) bed and chamber off; (2) lift to the tallest part plus 10 mm and park over
the chute; (3) wait for the bed in the file, either with `M190 S<limit - 1>` (the probe notes
GoKlipper's M190 waits to within -2/+1 C in either direction, so the upper edge equals the limit,
and then holds the target) or with a fixed `G4` dwell whose estimate HA shows; (4) `M140 S0` and
`M141 S0`, ending the hold (M190 is also bound to the chamber heater in the config); (5) reheat the
nozzle to a little above 170 C (`M109`) at the park, away from the part, so the PAUSE retract
works; (6) the `M117` notice, `M601`, the sweep and the final raise, as in 7.2. HA sets the nozzle
target to 0 at the pause. The button becomes usable, and `ready` is notified, once the nozzle reads
50 C or less inside the window.

Trade-off: the button appears only after the cool-down, and the job shows as printing until then.
The press still runs every check, including the bed. The window only has to cover the nozzle cooling
(a few minutes) plus the owner's reaction time, which is whatever window remains. M190 has no
timeout: in a room warmer than the target it never ends, so HA stops the job after a limit fixed
from T4 (part stays, flag on, notice). The `G4` dwell avoids that hang but guarantees no
temperature.

## 8. Door sensor (optional, D34)

The user may pick any HA binary_sensor with device class door or opening (`on` = open) in the
options. With one, the press needs a change to `on` reported after the push-off pause began and
`on` at the press (D29). That proves a live report and works with event-only contact sensors (most
Zigbee and Z-Wave), so no reporting interval is required. The 30 s `last_reported` bound is dropped
everywhere. An event-only sensor sends nothing during a sweep of about a minute, so a staleness rule
there would stop every sweep. Instead, HA stops on any change away from `on`, including unavailable
or unknown. Without a sensor, the door check is skipped by the owner's ruling, and the press needs
the user's confirmation (7.4). Option A, reading the printer's own switch over SSH, is dropped: on
the owner's printer GPIO 104 read a constant 1 through open and close, with no edge interrupts in 9
days (step a, PROVEN).

## 9. Risks and unknowns

| Risk / unknown | Until settled | Settled by |
|---|---|---|
| Door side is low Y (INFERRED) | no sweep of any kind | T1 |
| Upload: passed T2 (6 of 6 intact, md5 matched, names of 100 and 150 characters and with spaces or parentheses kept, nothing started); failure codes still unseen | error texts unverified | T2 (passed) |
| Start fields, `web/` sender, mapping, reply codes; task-id echo; md5 check at start; shortest job time; a last-job identity that survives an HA restart | Phase 1 not shipped; with no echo, every marked job after a gap is stopped; with no md5 check, Print again never restarts a prepared copy; the gap rule stays unless the plan's conditional step is ruled in | T3 |
| Busy reply 10101 | idle guard runs first | T6 |
| `M601` pause shape and code; nozzle above 170 C at the pause; nozzle target 0 accepted while paused; what RESUME reheats to; ACE feed or flush on resume with a cold nozzle; how long the motors and homing stay on while paused; what the 140 C nozzle does in a long pause with HA down; what Stop does at the pause; `M117` visible | Phase 2 not shipped. Phase 2 returns to the owner if RESUME reheats above 50 C or does not run clean, or if the motor window is shorter than the bed takes to cool and run 3 does not clear the 7.8 fallback. A nozzle held at 140 C indefinitely with HA down goes to the owner as a risk to accept | T4 |
| Rewritten archive accepted | clearing of archives not shipped | T4 |
| Step-loss, bed-scratch or head-block detection pausing mid-sweep | any later pause means stop | T5 |
| Parts land inside the chamber, not out of the door | Phase 2 returns to the owner before any further build | T5 |
| Purge line not reachable from behind | known leftover; the flag still clears | T5 |
| Script form with a file field on HA 2024.9 (source-checked: the 2024.9 frontend renders script fields and the file selector) | action usable from Developer tools | T7 on the owner's HA |

Owner-accepted risks, in plain terms:
- **Unchecked resume.** A resume from the touchscreen, Mainsail, Moonraker, the slicer or the
  OctoPrint-compatible port, or one while HA is disconnected, sweeps with no door check, and the
  job now waits up to the whole motor window for a press; HA's stop may land too late (`M117` aside).
- **Stop during the sweep** runs the exit routine from sweep height, through parts not yet swept (T9
  records the path). **Nothing ejected:** a sweep that completes but ejects nothing still clears the
  flag. **Misreporting door sensor:** one that reports a change to open while the door stays closed
  (a fallen magnet) passes; one stuck on open fails, since it reports no change. **No door sensor:**
  a closed door is the owner's own responsibility, by his ruling (D34); the confirmation is the only
  guard, and nothing watches the door during the sweep.

## 10. Live tests

The owner is present, and the bed is confirmed clear before every start.

**Phase 1 gate.**
- **T2, upload only. PASSED.** All 6 uploads through 18910 landed intact with md5 matching. The reply
  is HTTP 200 `{"code":200,"message":"success","data":{"gcode":"<name>"}}`. Names of 100 and 150
  characters, and names with spaces or parentheses, were kept exactly. No thumbnail or .acm file
  was created, and nothing started.
- **T3, start a minimal file over MQTT.** Record states and codes, and watch the exit routine. Also
  record whether reports echo HA's task id, whether a start with a wrong md5 is refused, the
  shortest job time, and whether the printer reports a last-job identity that survives an HA
  restart (`last_project`, a task id or a job start time). If it does, the plan may relax the gap
  rule to "flag only if that identity changed across the gap"; that is a conditional plan step, not
  part of this design.
- **T6, start during a print.** Expect 10101. **T7, Print again and refusals.** "Print again"
  starts the original; the refusal paths (busy, wrong suffix, bed flag, wrong model) send nothing.

**Phase 2 gate.**
- **T1, door.** No motion. The owner confirms the door side (if step a did not record it). If a door
  sensor is used, check that it reports a change to open when the door opens.
- **T4, pause.** A 10x10x0.6 mm PLA square with the inserted block. Run 1, with HA's nozzle command
  withheld as if HA were down: hold the pause until the motors or homing drop, or 2 hours at most;
  record when each drops, what the 140 C nozzle does and the bed's cooling curve; end with Stop and
  record its moves. Run 2, once as `.gcode` and once as a rewritten `.gcode.3mf`: nozzle target 0 at
  the pause, then a cold resume. Run 3, only if run 1's window is shorter than a realistic
  cool-down (extends the runbook's `M190 S40` timing): the 7.8 block. Record the in-file M190
  cooling wait and its timing, whether it heats the chamber, whether the reheat at the chute oozes
  onto the bed, how long the nozzle takes from 140 C to 50 C, and that the motors stay held through
  the wait. Pass: every T4 item in section 9 is answered. Optional experiment: does an in-file
  extrusion error end the job (a possible block on unarmed sweeps, used only if it fits the 50 C
  rule)?
- **T5, sweep.** An air sweep 20 mm above the bed, then one light sacrificial PLA part, each started
  by a press with the door open. Pass: parts land in the catch container, not inside the chamber, no
  firmware pause; record the purge-line result.
- **T8, restart and foreign starts.** HA restarted mid-pause: recognized only with the task-id echo,
  else stopped. A prepared file started from the touchscreen, and a Resume during `ready`: stopped.
- **T9, refusals and stops.** Presses with the door closed, a stale sensor, or the bed above its
  limit each refuse and send nothing. Window expiry, if short enough to wait out: button
  unavailable, notice sent. Stop at `ready`: part stays, flag on. Door closed during an air sweep:
  record the stop latency and the exit routine's path.

## 11. Left to the builder

Within the spec: the upload client, streaming and timeouts; start message values (T3); streaming
scans and the archive rewrite (7.2, section 3); sweep tunables (after T5) and the motor-window margin
(after T4); store schema, keys, services.yaml, the `file_upload` dependency and poll cadence; the
docs and automation; offline tests for every guard, the file change, names, press checks and the 7.4
state machine (no test code here).
