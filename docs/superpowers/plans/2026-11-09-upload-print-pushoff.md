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
  6. Option A, optional and pending the owner.

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
| `State.last_reported` (door freshness) | present |
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
   - `coordinator.py`: await that answer with a timeout. 10101 gets one retry after about 1.5 s,
     then is refused as busy. Other codes give their catalog text. No answer means a short status
     watch, then "may still start".
6. **Persistent state.** New `store.py`: a per-entry HA `Store` holding only the last-file record;
   Phase 2 adds the armed-job record. It is loaded in `async_setup_entry` before the platforms.
   The bed flag and report times are never stored.
7. **Bed flag (spec 6.4).** `coordinator.py`. The flag:
   - is set unconditionally every time the entry loads (review fix I1: a host with no
     battery-backed clock can misjudge downtime);
   - is on when any job phase is seen;
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
    - **Start reply:** 10101 is retried once, then refused as busy; other codes give their text;
      no reply gives "may still start".
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

## Phase 1: live tests

Run the local runbook `research/probe/p0b-2026-11-09/RUNBOOK.md`, steps 0 and a-f, then Z, as
written. Before the session, the verification engineer adds these extensions to it:

1. **b (T2).** Also upload names of 100 and 150 characters, and one with spaces and parentheses.
   Record what the printer keeps; this sets the name cap.
2. **c (T3).** Also record:
   - whether `print` reports echo the task id sent;
   - whether a start with a deliberately wrong md5 is refused;
   - the start-to-finish time of the m117 job (the shortest job);
   - any last-job identity (`last_project` fields, task id, job start time), and whether the same
     identity is still reported after an HA restart.
3. **a (T1, part).** Also read the age of the owner's chosen HA door sensor's `last_reported`
   through the open/close sequence.
4. **d (early T4 evidence).** Run as written. Its results feed the Phase 2 gate.
5. **New step T7.** With the Phase 1 build installed on the owner's HA:
   - the owner opens "Print a file" on a phone, sees the file field, and sends the m117 file; the
     job starts and the flag turns on;
   - "Bed is clear" is unavailable during the job and works after it;
   - "Print again" starts the file;
   - busy, wrong suffix and flag-on refusals send nothing.

Pass = the spec's section 10 Phase 1 gate. Failures go back to the Phase 1 implementer, with a delta
review. Checkpoint **C2**.

**Conditional step P1-C**, only if T3 shows a last-job identity that survives an HA restart. It
relaxes the gap rule to "flag only if that identity changed across the gap".
- Files: `coordinator.py`, and `store.py`, which keeps the identity.
- Tests prove: an unchanged identity across a gap leaves the flag as it was; a changed or missing
  identity turns the flag on.

This relaxes a safety rule, so it is built only after the PM rules it in, and it goes through the
security review.

**Release 1.3.0.** Set the version in `manifest.json`, `pyproject.toml` and the README status line
together. **[DOCS]** release notes. Checkpoint **C3**.

## Phase 2 gate

Phase 2 code does not start until both hold:

1. **The owner accepts in writing** the spec's section 9 owner-accepted risks.
2. **T4 passes.** The verification engineer adds runbook step "d2" and runs it with hand-made files:
   the 10x10x0.6 mm PLA square carrying the spec 7.2 block, with no sweep.
   - Run 1 withholds HA's nozzle command, as if HA were down. Hold the pause until the motors or
     homing drop, or for 2 hours at most. Record when each drops, what the 140 C nozzle does, and
     the bed's cooling curve. End with Stop and record its moves.
   - Run 2: nozzle target 0 at the pause, then a cold resume. Run it once as `.gcode` and once as a
     hand-rewritten `.gcode.3mf`.
   - Run 3, only if run 1's window is shorter than a realistic cool-down. Extend the runbook's
     `M190 S40` step (e) into the spec 7.8 block, and record:
     - the in-file M190 cooling wait and how long it takes;
     - whether it heats the chamber;
     - whether the reheat at the chute oozes onto the bed;
     - the nozzle's time from 140 C to 50 C;
     - that the motors stay held through the wait.
   - Record every T4 item in spec section 9.

These outcomes stop the plan and go back to the owner:
- RESUME reheats above 50 C, or does not run clean.
- The motor window ends before the bed cools to its limit, and run 3 does not clear the spec 7.8
  fallback.
- The nozzle holds 140 C indefinitely with HA down: the owner accepts that, or Phase 2 is redesigned.

If the rewritten archive is refused, clearing ships for `.gcode` only.

## Phase 2: code

**Conditional step P2-C.** This step applies only if T4 run 1 shows a short window and run 3 passes.
It builds the spec 7.8 block variant instead of the default block, picking M190 or `G4` from run 3,
in `sweep.py` and `printfile.py`. It also changes `clearing.py`:
- While the in-file cool-down runs, the state is `armed`, and its reason gives HA's estimate.
- HA stops the job if the M190 wait passes the limit set from T4.
- `ready` and the button start only once the nozzle reads 50 C or less.

Its tests prove:
- the block order, including the hold ending before the reheat and the reheat before `M601`;
- the stop when the wait passes the limit;
- the button stays unavailable until the nozzle reaches 50 C.

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
   - block insertion (spec 7.2) and a streaming `.gcode` copy;
   - the `.3mf` rewrite: only the plate member and its same-case md5 sidecar change;
   - the marked name;
   - everything in the executor, under the section 3 archive rules.
4. **Clearing state machine.** New `clearing.py`:
   - the armed-job record, kept in the store;
   - pause classification and the resume lock (spec 7.4);
   - `ready`: nozzle target 0 (per T4), fast polling, notice and `clear_ready`;
   - the motor window: from T4, less a margin, on the monotonic clock from the pause HA saw. An
     unknown start counts as expired. At expiry: `expired`, the button unavailable, and a notice;
   - the press checks in spec order, each with its one-line reason and nothing sent. No timer, no
     retry, no arming;
   - resume at most once, and watching through `pushing`;
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
   - Print again ticked restarts the prepared copy only if T3 proved the md5 check (a constant set
     from T3), and refuses when there is no prepared copy; the button never ticks.
7. **Entities and strings.**
   - `button.py`: "Clear the bed", admin-only through the context check, available only in `ready`
     inside the window.
   - `sensor.py`: "Bed clearing" (enum plus reason).
   - `clear_ready`, `clear_done` and `clear_failed` (`coordinator.py`, `event.py`).
   - `strings.json` and `translations/en.json`: press refusals naming the value to wait for, in °C.
   - Clearing state in `diagnostics.py`.
8. **Offline tests.** They must prove:
   - **Scans, planner and file change:** the scans, limits, block order and archive rules, as above.
   - **Pause handling:**
     - armed with the bed target 0 and no layer data: `ready`;
     - armed with the target above 0: left alone, lock on;
     - target unknown: button unavailable until a fresh reading;
     - marked foreign job: stopped at any pause;
     - unmarked foreign job with the target 0: stopped unless its layers show it mid-print.
   - **No auto push:** HA never resumes without a press.
   - **Press checks:** each refuses in order with its line and sends nothing; stale or non-finite
     readings fail; a non-admin press is refused.
   - **Window and Stop:** expiry, or an unknown start, gives `expired` plus a notice; Stop at
     `ready` leaves the flag on.
   - **Pushing:** resume happens once; door, pause or error means `failed`, and so does a lost
     connection; only a watched completion clears the flag.
   - **Restarts and the lock:** restart with and without the echo; an unexpected resume means stop;
     the Resume button and the actions are refused under the lock.
   - **Actions:** unticked prints normally; ticked and ineligible is refused with nothing sent; the
     original uploads before the copy; Print again ticked picks the copy only with the T3 md5 proof
     and refuses without one; the button starts the original.
9. **[DOCS]**
   - README "Clear the bed" section: how it works; what each refusal means; Stop, then "Bed is
     clear" to clear by hand (Stop first, because it moves the head); the motor window; the
     accepted risks; watch the first ejections; door freshness and the command-line sensor example.
   - The example script gains the "Allow Clear the bed" tick box (label confirmed by the UX
     reviewer).
   - The dashboard's "Clear the bed" button and "Bed clearing" sensor.
   - An example automation that turns `clear_ready` into a phone push.

**Review R2** covers the whole Phase 2 diff. Checkpoint **C4**.

## Phase 2: live tests

In order. A catch container sits in front of the door for every step that can sweep.

1. **T1.** Confirm the door side, if step a has not already. Check the owner's sensor against the
   30 s bound.
2. **T9a.** Make a prepared print of the 10 mm square, reaching `ready`.
   - Press with the door closed, with a stale sensor, and with the bed above its limit. Each must
     refuse and send nothing.
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

## Option A (last, optional, only on the owner's go)

1. **Code:**
   - an options step offered only when SSH answers;
   - credentials stored in the entry;
   - the host key pinned on first use and refused on change, with key authentication preferred;
   - one fixed read command;
   - a "Door" binary_sensor, read once at the press and every 1-2 s during `pushing`, unavailable
     on any error or timeout;
   - the SSH client library added to the manifest `requirements`.
2. **Tests prove:**
   - a changed host key is refused;
   - errors and timeouts make the sensor unavailable;
   - the command is fixed;
   - credentials and output are never logged.
3. Combined review, with security, then **[DOCS]**, then T1 re-run with this sensor, then a release.

## Needed from the owner

1. **Phase 1 session.** A go and about 90 minutes for the Phase 1 live session, with the runbook's
   checklist on hand, plus a phone to try the "Print a file" form.
2. **Door sensor.** One that reports at least every 10 s, chosen before the session. An event-only
   contact fails the 30 s bound.
3. **Risk acceptance.** Written acceptance of the spec's section 9 risks before Phase 2 code starts.
4. **T4 time.** Up to about 2.5 hours for the long pause, plus about an hour for run 3 if it is
   needed. Mostly unattended.
5. **Phase 2 session.** A catch container, the door open for the sweep tests, one light sacrificial
   PLA part, and presence to watch.
6. **Decisions.** Option A, yes or no; the version numbers 1.3.0 and 1.4.0. (D33 is ruled: a tick
   box.)
7. **Close-out.** At runbook Z.4, delete the test files from the printer, or authorize their
   removal.
8. **Awareness.** The flag is on after every HA restart, integration reload or options change, and
   after a printer power-off mid-session, so "Bed is clear" must be pressed before the next HA
   print, unless P1-C is ruled in.
