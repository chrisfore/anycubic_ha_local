# Anycubic 3D Printer - Local Control - NO Cloud

A Home Assistant custom integration for the **Anycubic Kobra 3 / 4 / X / S1 series** (and ACE /
ACE 2), talking to the printer's **stock LAN Mode** over its local MQTT broker — **no Anycubic
cloud account, no rooting**.

[![hacs](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://hacs.xyz)
[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=chrisfore&repository=anycubic_ha_local&category=integration)
[![Stars](https://img.shields.io/github/stars/chrisfore/anycubic_ha_local?style=flat&color=f5c518&label=stars)](https://github.com/chrisfore/anycubic_ha_local/stargazers)
[![Buy me a beer](https://img.shields.io/badge/%F0%9F%8D%BA%20Buy%20me%20a%20beer-GitHub%20Sponsors-f9dc46)](https://github.com/sponsors/chrisfore)
[![Venmo](https://img.shields.io/badge/Venmo-%40Chris--Fore--20-f9dc46)](https://venmo.com/u/Chris-Fore-20)

> **Status:** v1.3.0 — validated end-to-end on a Kobra S1 Max and user-validated on a Kobra 4,
> Kobra 3 and Kobra X; the rest of the family shares the identical protocol. Entities adapt to each
> model (see **Supported printers**).

⭐ **If this is useful to you, please star the repo.** It is how other Anycubic owners find it —
and the more printers and firmware versions in use, the faster problems get spotted and fixed.

## Features

### Monitoring
- **Printer:** status / lifecycle, nozzle / bed / chamber temperatures, progress, current & total
  layers, time remaining, file name; *printing* and *paused* binary sensors.
  (Firmware version shows on the device page as its software version.)
- **Why it paused or stopped:** a *pause reason* sensor ("Paused by user", "Filament is stuck", …)
  and a *print event* entity for notifications — see **Print events and pause reasons**.
- **Live camera:** the printer's H.264 stream, started on demand — or the Rinkhals camera feed
  (see **Printers running Rinkhals**).
- **ACE 2:** humidity, box temperature, drying time remaining, loaded slot, and all four
  filament slots (material, color, remaining %).

### Control
- **Print:** pause, resume, stop.
- **Print a file** (Kobra S1 / S1 Max): send a sliced file from Home Assistant and start it, or print the
  last one again — see **Print from Home Assistant**.
- **Temperatures & fans:** nozzle / bed target temperature, part-cooling / auxiliary / chamber fans.
- **Print speed:** silent / standard / sport.

> **Temperatures, fans and print speed can only be changed while a print is running.** They are
> print-*job* settings: the printer applies them to the current job and ignores them when idle
> (confirmed on hardware — it does not even acknowledge the command). The values stay readable
> when idle; only writing is refused. There is no preheat command in Anycubic's LAN protocol.
- **Chamber light:** on / off.
- **ACE 2:** drying on/off (with adjustable temperature & time, kept across Home Assistant
  restarts from v1.2.15), auto-feed.

## How it works

LAN Mode exposes a local MQTT broker (TLS, port 9883). Telemetry arrives as JSON reports; controls
are JSON commands on the same topics. Broker credentials are obtained on the fly and never
persisted — nothing sensitive is stored on the Home Assistant host.

## Supported printers

The Kobra 3 / 4 / X / S1 generations all speak the same signed LAN protocol, so they share one code
path (the newer Kobra 4 / X firmware differs only in how the camera stream is started, handled
automatically). The integration reads the printer's `modelId` and only creates the entities that
model actually has.

| Printer | `modelId` | Status |
| --- | --- | --- |
| Kobra S1 Max | `20029` | ✅ Validated on hardware (enclosed: chamber temp/light, box fan, camera, ACE 2) |
| Kobra S1 | `20025` | ✅ Same enclosed feature set, identical protocol |
| Kobra 3 / 3 V2 / 3 Max | `20024` / `20027` / `20026` | ✅ Kobra 3 validated on hardware by a user (incl. ACE) — open-frame, no chamber temp/light or box fan; camera is the Anycubic add-on |
| Kobra 4 | `20028` | ✅ Validated on hardware by a user — open-frame, built-in camera (newer tokenized stream, v1.2.4+), ACE supported |
| Kobra 2 Pro / Plus / Max | `20021` / `20022` / `20023` | ⚠️ Experimental — older *unsigned* handshake, not yet validated (you'll get a clear "unsupported handshake" message if it can't connect) |
| Kobra X | `20030` | ✅ Validated on hardware by a user — open-frame, built-in camera (tokenized stream, v1.2.4+), built-in 4-colour changer (feeder, no dryer) shown as **Multi-color unit** (v1.2.5+) |
| Photon (resin) | — | ❌ Different platform, no local LAN API |

Enclosure-only entities (chamber temperature, chamber light, box fan) appear **only** on the enclosed
S1 / S1 Max. The camera appears on the Kobra 3 / 4 / X / S1 family (built-in on enclosed and on the
Kobra 4 / X, add-on on Kobra 3).
ACE / ACE 2 entities appear whenever a multi-color box is attached, on any of these printers.
With **two ACE units** attached, each box gets its own device (the second is numbered, e.g.
“ACE Pro #2”) with the full set of sensors and controls.
On the **Kobra X**, whose 4-colour changer is built into the printer rather than being a separate
box, that unit appears as a single device named **Multi-color unit**. It is a feeder with no dry
box, so it gets the slot and auto-feed entities but no drying controls or humidity/box-temperature
sensors; external expansion boxes still show up alongside it as their own ACE devices, dryer
included.

### My printer isn't listed — help me add it

The Kobra 3 / 4 / X / S1 family shares one protocol, so adding a model is usually quick — and the
integration does the detective work for you. There's nothing to install or run by hand:

1. Install the integration and add your printer. It connects for **any** printer in this family,
   even one that isn't listed yet (you'll just get the base entities).
2. On the printer's device page in Home Assistant, choose ⋮ → **Download diagnostics**
   (automatically redacted of addresses and identifiers).
3. Open a **[Request support for my printer](../../issues/new?template=printer_request.yml)** issue
   and **attach that file**.

The diagnostics include a `capabilities` block — model ID & name, the printer's own reported
feature map, the camera / multicolor-box presence inventory, and whether it has a chamber sensor —
which is everything needed to add the model. If the integration can't connect at all (e.g. an
older Kobra 2 on the unsigned protocol), say so in the issue with your model + firmware.

## Requirements

- An Anycubic printer from the table above with **LAN Mode enabled**
  (printer screen → *Settings → Network → LAN Mode*).
- Home Assistant 2024.9 or newer, with HACS.
- The `ffmpeg` add-on / dependency (bundled with most HA installs) for the camera.

## Installation (HACS)

This integration is in the **HACS default store** — no custom-repository step needed.
**Quickest:** click the **"Add to HACS"** badge at the top — it opens the repository page in your
Home Assistant. Or find it manually:

1. HACS → search for **Anycubic 3D Printer - Local Control - NO Cloud** and **Download**.
2. **Restart Home Assistant.**
3. **Settings → Devices & Services → Add Integration → Anycubic 3D Printer - Local Control - NO Cloud**.
4. Enter the printer's **IP address or hostname** (e.g. `192.168.1.50` or `printer.local`).
   Host names are resolved with whatever DNS/mDNS your HA host has.

## Entities

| Domain | Entities |
| --- | --- |
| `sensor` | Status, Pause reason, Nozzle/Bed/Chamber temperature, Progress, Current/Total layer, Time remaining, File name, Firmware |
| `event` | Print event (started / paused / resumed / finished / stopped / error) |
| `binary_sensor` | Printing, Paused, Bed needs clearing (S1 / S1 Max) |
| `camera` | Camera |
| `light` | Chamber light |
| `button` | Pause, Resume, Stop, Print again and Bed is clear (S1 / S1 Max) |
| `number` | Nozzle/Bed target temperature, Part-cooling/Auxiliary/Chamber fan |
| `select` | Print speed (silent / standard / sport) |
| ACE `sensor` | Humidity, Box temperature, Drying time remaining, Loaded slot, Slot 1–4 (material/color/remaining as attributes) |
| ACE `switch` | Drying, Auto-feed |
| ACE `number` | Drying temperature, Drying time |

Chamber temperature, Chamber light, and the box (chamber) fan are created **only on enclosed models**
(S1 / S1 Max); the camera only on models with a local stream; ACE entities only when a box is attached.
See **Supported printers** above.

## Print events and pause reasons

When the printer pauses or stops a job it says why: the `print` report carries a reason code. The
**Pause reason** sensor shows it while the job is paused, and the **Print event** entity fires on each
change of the job — `started`, `paused`, `resumed`, `finished`, `stopped` — with `reason_code` and
`reason` on `paused` and `stopped`. `error` fires once, as soon as the printer reports it, for a pause
or stop the user did not ask for, such as a clog.

Only these three codes have been captured on the wire, from a Kobra 3:

| Code | Reason | Error? |
| --- | --- | --- |
| 10401 | Paused by user (slicer, app or the printer's screen) | no |
| 10601 | Stopped by user | no |
| 11518 | Filament is stuck | yes |

The integration also names the other codes listed on Anycubic's
[Kobra 3 error code pages](https://wiki.anycubic.com/en/error-codes/11518-code/k3) (for example
11519, filament tangle, and 11801, spaghetti detected), kept in `REASON_TEXT` in
`anycubic_local/const.py`. Those names come from the wiki only; none of those codes has been seen
on the wire yet. Any code is an error unless the user caused it, and a code in neither list shows
the printer's own message, which may be in Chinese. If you see one, please open an issue with it.

To have a clog reach your phone (the entity id depends on your printer's name; replace
`event.anycubic_kobra_3_print_event` with the one Home Assistant gave yours):

```yaml
automation:
  - alias: Printer error
    triggers:
      # The event entity's state is the time of the last event, so this fires for every
      # event, a second error in a row included; the condition picks the errors.
      - trigger: state
        entity_id: event.anycubic_kobra_3_print_event
    conditions:
      - condition: state
        entity_id: event.anycubic_kobra_3_print_event
        attribute: event_type
        state: error
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: Printer needs attention
          message: "{{ state_attr('event.anycubic_kobra_3_print_event', 'reason') }}"
```

## Print from Home Assistant

Pick a sliced file on a phone or computer, in Home Assistant, and the printer prints it — no trip to
the printer's screen.

**Only the Kobra S1 and S1 Max can do this for now.** On any other model the entities below are not
created and the actions refuse. To have another model added, see
*[My printer isn't listed](#my-printer-isnt-listed--help-me-add-it)*.

- **Print a file** (action `anycubic.print_file`) — choose a file, and the printer if you have more
  than one. The file is sent to the printer and started.
- **Print again** (button, and action `anycubic.print_again`) — starts the last file sent from Home
  Assistant again, from the copy still on the printer. Home Assistant keeps no copy of the file, only
  its name and a few details, and remembers them across restarts. If the file has since been deleted
  from the printer, the printer refuses and you send the file again.
- **Bed needs clearing** (binary sensor) and **Bed is clear** (button) — see below.

Both actions and both buttons are for Home Assistant **administrators** only. The `printer` field of
both actions is optional: with exactly one Anycubic printer set up it is used automatically, and with
more than one, an action called without it refuses with "Choose a printer."

**Files:** `.gcode`, `.gcode.3mf` and sliced `.3mf`, up to **100 MB**. A `.3mf` must be sliced and hold
a single plate. Slice for the printer you send to: a file that names a different printer is refused (a
file that names none goes ahead). On the printer the file keeps its name, except that anything other
than letters, digits, spaces and `. _ ( ) + -` becomes `_`, and a very long name is shortened.

**Filament:** with an ACE, filament N in the file prints from ACE slot N, and every slot the file
uses must hold a material of the same family the file asks for (PLA, PETG, …). Without an ACE the
file must use one filament, printed from the external spool. Either way the loaded material must be
set on the printer, or the start is refused.

**Start:** the print starts without the printer's auto-leveling step. The start is sent only once: a
start the printer refuses as busy is never tried again. After a confirmed start, Home Assistant shows
one **Started printing** notification per printer, replaced by the next one.

Home Assistant checks everything first and sends nothing if a check fails. Each refusal is one plain
sentence, shown as the error for the action (a script also keeps it in its trace):

| Refusal | What it means |
| --- | --- |
| Printing from Home Assistant is not available for this printer model yet | Not an S1 or S1 Max |
| Choose a printer | No printer was given, and more than one is set up. Pick one |
| Choose an Anycubic printer that is set up in Home Assistant | The device picked is not a printer, an ACE for example |
| Home Assistant has not heard from the printer for over a minute, or is not connected to it | The printer is off or not connected. Try again in a minute |
| The printer is busy | A print is running or paused, the printer is preparing one, or an attached ACE is drying. Wait for it to end; nothing is retried |
| A file is already being sent to this printer | Wait for that upload to finish |
| Clear the bed, then press "Bed is clear" | See **Bed needs clearing** below |
| A problem with the file: no longer available, unreadable, empty, over 100 MB, the wrong type, or an unusable name | Choose or export the file again. A name containing `-ha-pushoff` is reserved: rename the file |
| The 3MF file is not sliced, holds more than one plate, cannot be read, or is too large once unpacked | Slice it, export a single plate, or export it again |
| The file was sliced for a different printer | Slice it for this printer |
| The printer has not reported its filament yet, or a material is not known | Wait a minute, or set the filament type on the printer |
| The file uses more than one filament with no ACE attached, a slot the ACE does not have, or a material other than the one loaded | Attach an ACE, load the material, or slice for what is loaded. With a second ACE attached, only single-filament files print for now |
| The printer did not answer, refused or could not read the upload, could not save it, has no free space, or the upload did not finish | Nothing was started. Check that the printer is on, LAN Mode is on and there is space, then send the file again |
| The printer did not start the print | The file is on the printer. **Print again** tries it again without sending it |
| The printer did not confirm the start | Check the printer before trying again: the job may still start, because large 3MF files take time to unpack |

### Bed needs clearing

Every start makes the printer lower its nozzle onto the bed to find the bed height, so anything left
on the bed gets hit. Home Assistant cannot see the bed, so it asks you.

**Bed needs clearing** turns on whenever the printer reports anything but idle (a print, whoever
started it, or any other busy state, such as after a start it refused), and after any silence of
more than a minute from the printer (it was switched off, say). It also turns on **after every Home
Assistant restart, integration reload or options change**, because nobody was watching the printer
in between. While it is on, **Print a file** and **Print again** refuse. Take the part off the bed,
then press **Bed is clear**; that button works only while the printer is idle.

> **Never automate "Bed is clear".** It is your word that the bed is empty, and nothing checks it. An
> automation or script can press it, so do not write one that does: a press with a part still on the
> bed, followed by a print, drives the nozzle into the part.

### Example script

The **Print a file** form is a script that passes its two fields to the action. Add it to
`configuration.yaml` (or to `scripts.yaml`, without the `script:` line), then open the script from a
dashboard button and pick the file. The printer is optional: leave it empty with one printer, or set
a default for the Printer field in the script editor and the form pre-fills it.

```yaml
script:
  anycubic_print_a_file:
    alias: Print a file
    icon: mdi:file-upload
    fields:
      printer:
        name: Printer
        required: false
        selector:
          device:
            filter:
              integration: anycubic
      file:
        name: File
        required: true
        selector:
          file:
            accept: ".gcode,.3mf"
      allow_clear_bed:
        name: Cool the bed when done
        required: true
        default: false
        selector:
          boolean:
    sequence:
      - action: anycubic.print_file
        data: >-
          {% set data = {'file': file, 'allow_clear_bed': allow_clear_bed | default(false)} %}
          {{ dict(data, printer=printer) if printer is defined and printer else data }}
```

**Cool the bed when done** is for the Kobra S1 Max; see the next section. Leave it out of the
script if you do not use it.

## Cool the bed when done (Kobra S1 Max)

Turn on **Cool the bed when done** when you send a file, and the print ends differently. The head
lifts above the part and parks at the back, the chamber fan and the auxiliary fan run at 80 %, and
the printer waits. Home Assistant watches the bed, and once it has cooled it ends the print and
tells you: "The bed has cooled to 25 °C. Take the print off, then press Bed is clear."

Nothing touches the part, and nothing is ever resumed: the wait is ended with a stop, the same as
pressing Stop.

- **How cool:** 28 °C for PLA, 25 °C for PETG, 30 °C for anything else; the coolest one if a print
  uses several. With the door closed, a bed at 70 °C takes about half an hour.
- **If it does not cool within 90 minutes,** the print is ended anyway and you are told the bed is
  still warm.
- **To take the part sooner,** press **Stop**, wait for the head to stop moving, and take it off.
- **The Bed cool-down sensor** shows where things stand: Print running, Cooling, Cool, Not cooled
  in time, or Stopped. The print event entity fires `clear_ready` when the cooling starts and
  `clear_done` when the bed is cool, so a phone can be told:

```yaml
automation:
  - alias: Print is cool
    triggers:
      - trigger: state
        entity_id: event.anycubic_kobra_s1_max_print_event
        attribute: event_type
        to: clear_done
    actions:
      - action: notify.mobile_app_your_phone
        data:
          message: The print has cooled. Take it off and press Bed is clear.
```

Things to know:

- The nozzle stays at 140 °C while the print waits; the printer holds it there whenever it is
  paused, and takes no temperature or fan commands until the wait ends.
- The bed still needs clearing afterward, as after every print: take the part off, then press
  **Bed is clear**.
- If the print was paused and continued earlier (by hand, or by a filament runout), the printer
  does not report its last pause. You are told when that happens; press Stop when it finishes.
- The file must be sliced for this printer, give its height, and have no pause of its own. A file
  that does not qualify is refused before anything is sent; turn the switch off to print it
  normally.
- Print again never cools: send the file again with the switch on.

## Example dashboard

[`dashboards/anycubic_dashboard.yaml`](dashboards/anycubic_dashboard.yaml) — camera on top, the ACE 2
filament slots (tinted with each slot's real color) below it, then the printer status and all the
controls, including **Print a file**, **Print again**, **Bed is clear** and the bed status for the
S1 / S1 Max, and the bed cool-down status for the S1 Max. The file starts with a commented copy of the script above, which the **Print a file**
button opens. It uses the [Mushroom](https://github.com/piitaya/lovelace-mushroom) and
[card-mod](https://github.com/thomasloven/lovelace-card-mod) frontend cards (install both from HACS),
and assumes the default device names — adjust the entity-ID prefixes if you renamed a device.

## Notes & caveats

- **Stop** cancels the running print (the printer transitions *stopping → stopped*).
- **Target temperatures heat the printer even while idle** (so you can preheat or change filament).
  Set a target back to 0 to turn the heater off.
- **Box fan / chamber fan** is reported as a 0–100 level.
- The printer pushes status while active; the ACE box only answers on request, so it is **polled**
  every 30 s. A freshly idle ACE may briefly show no box data until the next poll.

## Troubleshooting

- **"Re-enable LAN Mode" notification / reauth:** the printer fell back to cloud mode. Turn LAN Mode
  back on at the printer and submit the prompt.
- **Printer unavailable after its IP address changed:** in **Settings → Devices & Services**, open
  the integration, choose ⋮ → **Reconfigure** and enter the new address — no need to delete the
  integration. To stop it changing again, reserve the printer's address in your router.
- **Loaded slot shows nothing / "None":** no filament is loaded into the toolhead (the printer's
  `-1` sentinel is shown as *None*).
- **Camera won't play:** make sure `ffmpeg` is available and the printer is reachable on its stream
  port (18088 on most models — the integration uses the URL the printer itself reports).
- **Camera blank after the printer was switched off and on:** from v1.2.14 the stream is started
  again without reloading the integration. If a card that was already open stays blank, open it
  again.
- **Chamber light turns on by itself after a Home Assistant restart:** the printer firmware switches
  the chamber LED on whenever camera capture starts, and that can't be overridden. v1.2.2 stops HA's
  startup stream probe from triggering capture; also leave **Preload camera stream** (camera →
  settings gear) off, or HA keeps the capture — and the LED — running the whole time HA is up.
- **Bug reports:** download diagnostics from the device page (⋮ → *Download diagnostics*) — it is
  automatically redacted of addresses and identifiers.

## Printers running Rinkhals

On a printer running the Rinkhals firmware overlay with its camera app on, the camera is served
by Rinkhals and the printer's own camera stream cannot work. To use it:

1. In **Settings → Devices & Services**, open the integration and choose **Configure**.
2. Set **Camera source** to **Rinkhals camera** and submit. The integration reloads by itself.

Notes:

- The picture is dark unless the chamber light is on. Reading this feed does not switch the
  light; switch it on at the printer, or with the **Chamber light** entity on enclosed models.
- The feed is a picture stream, shown directly in the camera card. It needs no `ffmpeg`.
- The source is a choice, not a fallback: if the feed does not answer, the camera shows as
  unavailable and is tried again the next time it is displayed. To go back to the printer's own
  camera, set **Camera source** to **Printer camera**. On a printer model with no camera of its
  own, that removes the camera entity, so a card that used it shows nothing until the source is
  set back.

## Feedback, bugs & requests

Everything runs through **[GitHub Issues](../../issues/new/choose)**:

- **Bug report** — include redacted diagnostics (device page → ⋮ → *Download diagnostics*).
- **Feature request / feedback** — ideas and suggestions are welcome.
- **Request support for my printer** — see *[My printer isn't listed](#my-printer-isnt-listed--help-me-add-it)* above.

The most useful thing you can do costs nothing: **[star the repo](https://github.com/chrisfore/anycubic_ha_local/stargazers)**
so more Anycubic owners find it, and tell us which printer and firmware you are running. Every
model someone confirms makes the integration better for everyone on that machine.

## License

Copyright (c) 2026 Chris Fore and contributors.

From v1.2.20 on, this integration is licensed under the GNU Affero General Public License v3.0 or later — see [LICENSE](LICENSE).
