# Plan: upload, print, and push the part off

Implements `docs/superpowers/specs/2026-11-09-upload-print-pushoff-design.md` ("spec"; section
numbers below refer to it).

## How this plan runs

- **Branch.** `feature/upload-print-pushoff`, from `main`.
- **Commits.** The PM commits and pushes at each checkpoint (C1-C5). Nothing is committed by the
  implementer, reviewer or docs writer.
- **Phases.** One implementer runs each code phase start to finish. One combined code and security
  review then covers the whole phase diff. The same implementer fixes the findings, and the reviewer
  re-checks only the fix delta.
- **Docs.** Steps marked **[DOCS]** are pure documentation and go to the docs writer: README, the
  example dashboard and its script, and release notes.
- **Tests.** No test code appears here. Each phase lists what its offline tests must prove. The
  implementer writes them in `tests/`, on the existing pytest-homeassistant-custom-component harness.
- **Live tests.** The verification engineer runs them. The owner is present, and the bed is
  confirmed clear before every start.
- **Order.**
  1. Phase 1 code.
  2. Phase 1 live tests.
  3. Phase 2 gate.
  4. Phase 2 code.
  5. Phase 2 live tests.
  6. Option A (the SSH switch reader) is dropped: step a showed the switch does not follow the door.

## Versions and minimum Home Assistant

The version currently disagrees across files: `manifest.json` says 1.2.21, while `pyproject.toml` and
the README status line still say 1.2.15. Every bump sets all three together: 1.3.0 at the Phase 1
release, 1.4.0 at the Phase 2 release.

The minimum stays **2024.9.0**. Each API below was checked in the 2024.9.0 core sources and in its
frontend (20240904.0):

| API the code relies on | 2024.9.0 |
|---|---|
| `selector: file` (`accept` is required) and `file_upload.process_uploaded_file`, 100 MB cap | present |
| Script more-info dialog renders script fields, including the file selector (the "Print a file" form) | present |
| `State.last_reported` | present, though no longer needed: the door rule (spec D29) now uses state changes |
| `async_register_admin_service` | present. It takes no `supports_response`, so the actions return nothing and report by raising |
| `helpers.storage.Store` | present |
| `ServiceValidationError` with translation keys (one-line refusals) | present |

`file_upload` joins the manifest `dependencies`.

## Phase 1: code

1. **Gating constants.** `const.py`:
   - Phase 1 models {20025, 20029};
   - push-off models {20029};
   - gap bound 60 s;
   - name cap 100;
   - the `-ha-pushoff` marker;
   - freshness bound 30 s.

   `manifest.json`: add `file_upload`.
2. **Redaction.** `anycubic_local/const.py`: add `filepath` to `SENSITIVE_KEYS` and `JOB_NAME_KEYS`.
   Provide a way for a held upload token to be masked by `redacted_error`'s whole-word pass, which
   also catches tokens shorter than 7 characters. `coordinator.py`: the file being sent joins
   `_job_names()` for the duration of the submit.
3. **File inspection (pure, no HA).** New `anycubic_local/printfile.py`:
   - printer-side name rules (section 3);
   - suffix check;
   - archive validation: duplicate or case-variant names refused, per-member and total
     uncompressed caps counted while reading, no extraction by name, exactly one plate gcode;
   - header reads for Phase 1: model, filament types, used slots;
   - streaming size and md5;
   - one finite-number parser that every guard uses.
4. **Upload client.** New `anycubic_local/upload.py`:
   - the upload URL keeps the entered host; port, path and query come from the printer;
   - multipart `filename` + `gcode` with `X-File-Length`, streamed from disk;
   - 19001-19011 mapped to fixed reasons;
   - fixed-text errors raised `from None`.

   Sent through HA's shared aiohttp session with `allow_redirects=False`, so there is no new
   dependency. No retry.
5. **Start command and reply.**
   - `anycubic_local/commands.py`: a `print_start` builder (spec 6.2). Its shape mirrors the staged
     start command in the local runbook kit (`research/probe/`, git-ignored; never committed).
   - `anycubic_local/mqtt.py`: hand the envelope of a `print`/`start` answer (action, code, state,
     msg) to a new callback, the way `on_reason` is handed its envelope today.
   - `coordinator.py`: send the start once and await the answer with a timeout. 10101 is refused
     as busy with no retry, because a refused start corrupts the printer's job record (T6). Other
     codes give their catalog text. No answer means a short status watch, then "may still start".
     The start sends `auto_leveling` 0, because 1 hung on a 10 C preheat (T7.3). (As built in
     1.3.0b2.)
6. **Persistent state.** New `store.py`: a per-entry HA `Store` holding only the last-file record;
   Phase 2 adds the armed-job record. It is loaded in `async_setup_entry` before the platforms.
   The bed flag and report times are never stored.
7. **Bed flag (spec 6.4).** `coordinator.py`. The flag:
   - is set unconditionally every time the entry loads (review fix I1: a host with no
     battery-backed clock can misjudge downtime);
   - is on for any info state other than `free`, and for a current job in any status;
   - is on after any in-session report gap over the bound, measured on the monotonic clock in the
     poll cycle;
   - clears only through the "Bed is clear" press, or Phase 2's watched completed sweep.
8. **Actions.**
   - New `actions.py` and `services.yaml`. `__init__.py` gains `async_setup`, which registers
     `print_file` and `print_again` once with `async_register_admin_service`. The target is the
     printer device, through a device selector filtered to this integration.
   - Guards run in spec 6.1 order, under one lock per printer; then upload, then start; then the
     last-file record is written.
   - `print_again` re-runs guards 1-5 and 9, starts the recorded name, and refuses marked names.
   - No `push_off` field yet.
9. **Entities, created on Phase 1 models only.**
   - `button.py`: "Print again" (available when a record exists and the printer is idle) and "Bed
     is clear" (available when the flag is on and no job is active). Both are admin-only through a
     context user check in `async_press`.
   - `binary_sensor.py`: "Bed needs clearing".
10. **Options and diagnostics.**
    - `config_flow.py`: the door-sensor picker (binary_sensor, device class door or opening),
      enclosed models only. Nothing uses it before Phase 2, but it lets T1 check the owner's sensor
      in the first live session.
    - `diagnostics.py`: bed flag, the last guard outcome, and the door reading's age. No names.
11. **Strings.** `strings.json` and `translations/en.json`: action names and fields, every refusal
    as a translated exception, the new entities, and the option label.
12. **Offline tests.** They must prove:
    - **Names:** the name rules hold, the marker is never truncated, and an original already
      carrying the marker is refused.
    - **Archives:** duplicate and case-variant names are refused; the size caps trip while reading;
      unsliced and multi-plate files are refused; temp files are gone after both success and
      failure.
    - **Refusals:** every guard refuses with its one line and makes zero upload or MQTT calls;
      non-finite values fail; other models get no new entities and a refusal.
    - **Upload:** the host stays pinned when the printer names another; the query (token) is kept;
      redirects are not followed; each printer error code maps to its text; there is no retry.
    - **Token:** the token never appears in logs, diagnostics or exception chains, including a
      token under 7 characters.
    - **Start reply:** 10101 is refused as busy with no retry; other codes give their text; no
      reply gives "may still start".
    - **Bed flag:** on at every entry load (start, reload, options change); on for a job from any
      source; on after an in-session gap on the monotonic clock; never stored; clears only on the
      button; the button is unavailable during a job; both actions refuse while the flag is on.
    - **Print again:** re-runs guards, starts the recorded name, never uploads, and refuses marked
      names.
    - **Access and redaction:** a non-admin action call and a non-admin press of either button are
      refused; `filepath` is masked; the file being sent is scrubbed from log lines.
13. **[DOCS]** README section "Print from Home Assistant":
    - enabled models (S1, S1 Max), and that others stay off until validated with diagnostics;
    - actions and both buttons are admin-only, and the 100 MB cap;
    - the bed flag, with the warning never to automate "Bed is clear";
    - the door-sensor option and its freshness need.

    The example script (a file field with `accept: ".gcode,.3mf"`) goes in the README and in
    `dashboards/anycubic_dashboard.yaml`. So do the "Print a file" button (it opens the script's
    more-info form), "Print again", "Bed is clear" and the flag tile.

**Review R1** covers the whole Phase 1 diff. Checkpoint **C1**.

## Phase 1: live tests (done, 1.3.0b2)

These were run from the local runbook `research/probe/p0b-2026-11-09/RUNBOOK.md`. The results are
in spec section 10:
- T2: PASS.
- T3: PASS, with the task id echoed and a wrong md5 accepted.
- T6: the 10101 refusal corrupts the job record, so the retry was removed.
- T7.1-7.4, 7.6 and 7.7: PASS. 7.5 was skipped, because the picker hides `.stl`.

Step a showed the door switch is dead, and the door side is confirmed as Y 0 by camera. Checkpoint
**C2** is done (c2e6cfb, 9cf5c29).

**Step P1-U, success confirmation, for the UX reviewer.** A successful send shows nothing today.
`actions.py` adds one persistent notification per printer on a confirmed start ("Printing <file>."),
replaced by the next one; it covers both actions and the Print again button. Tests prove that it
appears only after a confirmed start, and never on a refusal.

**Conditional step P1-C**, restricted. `last_project` survives an HA restart but is cleared by a
printer reboot (T3). P1-C therefore applies to HA restarts and reloads only: HA stores the flag and
the last task id, and at load keeps the stored flag only while the printer still reports the same
`last_project` task id and is free.
- Files: `coordinator.py`, and `store.py`.
- A missing identity (a printer reboot), a changed one, or a printer that is not free turns the
  flag on.
- Tests prove all three cases, plus that an unchanged identity keeps the stored flag.

This step partly reverses fix I1 and relaxes a safety rule. It is built only after the PM rules it
in, and it goes through the security review.

**Release 1.3.0.** Set the version in `manifest.json`, `pyproject.toml` and the README status line
together. **[DOCS]** release notes. Checkpoint **C3**.

## Phase 2 gate

Phase 2 code does not start until both hold:

1. **The owner accepts in writing** the spec's section 9 owner-accepted risks.
2. **T4 passes.** The verification engineer adds runbook step "d2" and runs it with hand-made files:
   the 10x10x0.6 mm PLA square carrying the spec 7.2 block.
   - Run 1: hold the pause until the motors or homing drop, or for 2 hours at most. Record when each
     drops, what the 140 C nozzle does, and the bed's cooling curve. End with Stop and record its
     moves.
   - Run 2: once as `.gcode` and once as a hand-rewritten `.gcode.3mf`. Resume, and record:
     - the nozzle temperature at PAUSE's retract;
     - whether in-file `M109 S50` waits for cooling, or else the `G4` time from 140 to 50 C;
     - that nothing touches the part above 50 C.
   - Run 3, only if run 1's window is shorter than a realistic cool-down. Extend the runbook's
     `M190 S40` step into the spec 7.8 block, and record whether the reheat at the chute oozes onto
     the bed, and that the motors stay held through the wait.

These outcomes stop the plan and go back to the owner:
- The motor window ends before the bed cools to its limit, and run 3 does not clear the 7.8
  fallback.
- The nozzle holds 140 C indefinitely: the owner accepts that, or Phase 2 is redesigned.

If the rewritten archive is refused, clearing ships for `.gcode` only.

## Phase 2: code

**Conditional step P2-C.** This step applies only if T4 run 1 shows a short window and run 3
passes. It builds the spec 7.8 block variant (M190 or `G4`, chosen from run 3) in `sweep.py` and
`printfile.py`. In `clearing.py`, while the in-file cool-down runs the state is `armed`, with HA's
estimate as the reason, and HA stops the job if the M190 wait passes the limit set from T4.

Its tests prove:
- the block order: the hold ends, then the reheat, then `M104 S0`, then `M601`;
- the job is stopped when the wait passes the limit.

If T4 shows the motors never drop, or drop later than a realistic cool-down, skip this step.

1. **File checks.** `anycubic_local/printfile.py`:
   - brim, skirt and tower scan (case-insensitive, `;TYPE:` and `; FEATURE:`);
   - `brim_type` fast path and `raft_layers`;
   - the bed is heated, and the file has no `M600`/`M601` of its own;
   - materials by used slot and leading word, and PETG only on a textured plate;
   - footprints and `max_z_height`;
   - end-code recognition.
2. **Sweep planner.** New `anycubic_local/sweep.py`:
   - the S1 Max profile: travel limits, chute park, purge line at Y 352, bed area, door side from
     T1;
   - park, columns, descent points, sweep and final raise;
   - every coordinate checked, and Z at most 253 including PAUSE's 2 mm;
   - it returns the block or a refusal reason.

   The tunables start inside the research ranges and are fixed after T5.
3. **File change.** `anycubic_local/printfile.py`:
   - block insertion (spec 7.2) and a streaming `.gcode` copy. The block has no `M117`. It cools the
     nozzle in the file after `M601` (`M109 S50` or a `G4` dwell, per T4 run 2);
   - the `.3mf` rewrite: only the plate member and its same-case md5 sidecar change;
   - the marked name;
   - everything in the executor, under the section 3 archive rules.
4. **Clearing state machine.** New `clearing.py`:
   - the armed-job record, kept in the store;
   - pause classification and the resume lock (spec 7.4);
   - `ready`: fast polling, notice and `clear_ready`. HA sends no nozzle command, because a target
     sent while paused is ignored;
   - the second-pause limit (spec 7.4): notify once when an armed job resumes from an earlier pause;
   - the motor window: from T4, less a margin, on the monotonic clock from the pause HA saw. An
     unknown start counts as expired. At expiry: `expired`, the button unavailable, and a notice;
   - the press checks in spec order, each with its one-line reason and nothing sent. No timer, no
     retry, no arming;
   - the door rule (D29, D34): with a sensor, a change to `on` after the pause began and `on` at the
     press; with none, `door_open_confirmed: true`;
   - a call with no user (an automation) refused;
   - resume at most once, and watching through `pushing`, stopping on any change of the sensor away
     from `on`;
   - the results; the flag clears only on `done`;
   - stopping foreign jobs and unexpected resumes;
   - restart and gap handling through the task-id echo (T3).
5. **Resume lock.** `coordinator.py`: `resume` is refused under the lock unless the press path sends
   it. `button.py`: Resume follows the lock. Stop is unchanged.
6. **Actions.** `actions.py` and `services.yaml`:
   - The "Allow Clear the bed" tick box (D33, off by default) on `print_file` and `print_again`;
     ticked and ineligible: refused, nothing sent.
   - A prepared submit uploads the original, then the prepared copy, starts the copy, and arms the
     record.
   - Print again ticked is refused, because T3 showed md5 is not checked at start; unticked, and the
     button, restart the original.
   - New `anycubic.clear_bed` (printer, `door_open_confirmed`), registered admin-only and refusing
     calls with no user. The field is required when no door sensor is configured.
7. **Entities and strings.**
   - `button.py`: "Clear the bed", admin-only through the context check, available only in `ready`
     inside the window. With no door sensor, its press is refused with the "use the dashboard" line
     (spec 7.4).
   - `sensor.py`: "Bed clearing" (enum plus reason).
   - `clear_ready`, `clear_done` and `clear_failed` (`coordinator.py`, `event.py`).
   - `strings.json` and `translations/en.json`: press refusals naming the value to wait for, in °C.
   - Clearing state in `diagnostics.py`.
8. **Offline tests.** They must prove:
   - **Scans, planner and file change:** the scans, limits and archive rules, as above. The block
     has no `M117`, and the nozzle cooling comes before any move that can touch a part.
   - **Pause handling:**
     - armed with the bed target 0 and no layer data: `ready`;
     - armed with the target above 0: left alone, lock on;
     - target unknown: button unavailable until a fresh reading;
     - marked foreign job: stopped at any pause;
     - unmarked foreign job with the target 0: stopped unless its layers show it mid-print;
     - an armed job resuming from an earlier pause: one notice, and no `ready` afterwards.
   - **No auto push:** HA never resumes without a press.
   - **Press checks:**
     - each refuses in order with its line and sends nothing;
     - stale or non-finite readings fail;
     - a non-admin press, and a call with no user, are refused.
   - **Door, with a sensor:**
     - a door left open since before the pause, with no change, is refused;
     - an event-only sensor passes after a fresh open;
     - unavailable or unknown counts as closed.
   - **Door, with none:** the button entity press, and the action without the field, are refused;
     the action with the field passes the door check.
   - **Window and Stop:** expiry, or an unknown start, gives `expired` plus a notice; Stop at
     `ready` leaves the flag on.
   - **Pushing:** resume happens once; a sensor change away from `on` (including unavailable),
     a pause or an error means `failed`, and so does a lost connection; only a watched completion
     clears the flag.
   - **Restarts and the lock:** restart with and without the echo; an unexpected resume means stop;
     the Resume button and the actions are refused under the lock.
   - **Actions:** unticked prints normally; ticked and ineligible is refused with nothing sent; the
     original uploads before the copy; Print again ticked is refused; the button starts the
     original.
9. **[DOCS]**
   - README "Clear the bed" section: how it works; what each refusal means; Stop, then "Bed is
     clear" to clear by hand (Stop first, because it moves the head); the motor window; the
     accepted risks, including that with no door sensor a closed door is the user's responsibility;
     watch the first ejections. Also: the door sensor is optional, and event-only contacts work;
     never automate `clear_bed` or put `door_open_confirmed` in a script.
   - The example script gains the "Allow Clear the bed" tick box (label confirmed by the UX
     reviewer).
   - The dashboard's "Clear the bed" control: a tap action calling `anycubic.clear_bed` with
     `door_open_confirmed: true` behind the confirmation "Open the printer door before clearing the
     bed." Plus the "Bed clearing" sensor.
   - An example automation that turns `clear_ready` into a phone push.

**Review R2** covers the whole Phase 2 diff. Checkpoint **C4**.

## Phase 2: live tests

In order. A catch container sits in front of the door for every step that can sweep.

1. **T1.** The door side is settled: Y 0, seen on camera. If a sensor is used, check that it reports
   a change to open.
2. **T9a.** Make a prepared print of the 10 mm square, reaching `ready`. These presses must each
   refuse and send nothing:
   - with a sensor: the door closed, the door left open since before the pause;
   - with none: the button entity, and the action without the field;
   - the bed above its limit.

   The dashboard's confirmation must pass the door check.
   - Then press Stop. Pass: the part stays and the flag stays on.
3. **T8.**
   - HA restarted mid-pause: recognized only with the task-id echo, otherwise stopped.
   - A prepared file started from the touchscreen: stopped, never swept.
   - A touchscreen Resume during `ready`, with the door open: stopped.
4. **T5 air sweep, with T9b.** A press with the door open runs a sweep 20 mm above an empty bed. The
   file is built offline by the verification engineer, calling the planner with that height; the
   product has no such setting. T9b closes the door mid-sweep and records the stop latency and the
   exit routine's path.
5. **T5 sacrificial part.** One light sacrificial PLA part, a press with the door open, and the
   owner watching. Pass: the part lands in the container, not inside the chamber; no firmware
   pause; the purge-line result is recorded. Landing inside the chamber sends Phase 2 back to the
   owner.
6. **Window expiry**, if the window is short enough to wait out: the button goes unavailable and the
   notice is sent.

Tune the sweep constants from T5. The implementer makes the fixes, with a delta review. **Release
1.4.0**: all three version files together, and **[DOCS]** release notes. Checkpoint **C5**.

## Option A: dropped

Step a showed that GPIO 104 reads a constant 1 through open and close on the owner's printer, so
there is nothing to read.

## Needed from the owner

1. **Door sensor, optional.** Any door contact works, event-only ones included. Without one, the
   dashboard confirmation stands in, and a closed door is the owner's responsibility (D34).
2. **Risk acceptance.** Written acceptance of the spec's section 9 risks before Phase 2 code starts.
3. **T4 time.** Up to about 2.5 hours for the long pause, plus about an hour for run 3 if it is
   needed. Mostly unattended.
4. **Phase 2 session.** A catch container, the door open for the sweep tests, one light sacrificial
   PLA part, and presence to watch.
5. **Decisions.** The version numbers 1.3.0 and 1.4.0.
6. **Close-out.** At runbook Z.4, delete the test files from the printer, or authorize their
   removal.
7. **Awareness.** The flag is on after every HA restart, integration reload or options change, and
   after a printer power-off mid-session, so "Bed is clear" must be pressed before the next HA
   print, unless P1-C is ruled in.
