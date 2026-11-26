# Anycubic 3D Printer - Local Control - NO Cloud

A Home Assistant integration for the **Anycubic Kobra 3 / 4 / X / S1 series** and the ACE / ACE 2
multi-color box. It talks to the printer's own **LAN Mode** on your network: **no Anycubic cloud
account, no rooting, no custom firmware.**

[![hacs](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://hacs.xyz)
[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=chrisfore&repository=anycubic_ha_local&category=integration)
[![Stars](https://img.shields.io/github/stars/chrisfore/anycubic_ha_local?style=flat&color=f5c518&label=stars)](https://github.com/chrisfore/anycubic_ha_local/stargazers)
[![Buy me a beer](https://img.shields.io/badge/%F0%9F%8D%BA%20Buy%20me%20a%20beer-GitHub%20Sponsors-f9dc46)](https://github.com/sponsors/chrisfore)
[![Venmo](https://img.shields.io/badge/Venmo-%40Chris--Fore--20-f9dc46)](https://venmo.com/u/Chris-Fore-20)

⭐ **If this is useful to you, please star the repo.** It is how other Anycubic owners find it.

## What you get

- **Print from Home Assistant** *(Kobra S1 / S1 Max)*: pick a sliced file on your phone or
  computer and the printer starts it. Print the last file again with one button. Optionally let
  the bed **cool down by itself** when the print is done, and **skip the purge line** so there is
  nothing to scrape off the back of the bed. See [Print from Home Assistant](#print-from-home-assistant).
- **Live status:** state, temperatures, progress, layers, time elapsed and remaining, filament
  used in grams, the file name, and the printer's own preview of the part.
- **Why it paused:** a *Pause reason* sensor ("Filament is stuck", …) and a *Print event* entity
  that fires on start, pause, resume, finish, stop and error. Good for phone notifications.
- **Camera:** the printer's live stream, started only while you watch.
- **Controls:** pause, resume, stop, print speed, target temperatures, fans, chamber light.
- **ACE / ACE 2:** all four slots (material, color, remaining), humidity, box temperature, and
  drying with its own temperature and time.

## Supported printers

All of these speak the same LAN protocol. The integration reads the printer's model and creates
only the entities that model has.

| Printer | Status | Print from HA |
| --- | --- | --- |
| Kobra S1 Max | ✅ Validated on hardware | ✅ incl. cool-down |
| Kobra S1 | ✅ Same feature set as the S1 Max | ✅ (cool-down is S1 Max only) |
| Kobra 3 / 3 V2 / 3 Max | ✅ Kobra 3 validated by a user | — |
| Kobra 4 | ✅ Validated by a user | — |
| Kobra X | ✅ Validated by a user | — |
| Kobra 2 Pro / Plus / Max | ⚠️ Experimental, older protocol, not validated | — |
| Photon (resin) | ❌ No local LAN control | — |

- Chamber temperature, chamber light and the box fan exist only on the enclosed **S1 / S1 Max**.
- The **Kobra X** built-in 4-color changer shows up as a device named **Multi-color unit**
  (slots and auto-feed; it has no dryer).
- With **two ACE units**, each gets its own device (for example "ACE Pro #2").

### My printer isn't listed

The integration connects to any printer in this family, even one not in the table, and gives
you the basic entities. To get it fully supported:

1. Add the printer in Home Assistant.
2. On its device page, choose ⋮ → **Download diagnostics** (addresses and identifiers are removed
   automatically).
3. Open a **[Request support for my printer](../../issues/new?template=printer_request.yml)** issue
   and attach the file.

If it cannot connect at all, say so in the issue with your model and firmware version. Older
firmware is the usual cause: update the printer first.

## Installation

You need **LAN Mode** turned on (printer screen → *Settings → Network → LAN Mode*) and Home
Assistant 2024.9 or newer with HACS.

1. Click **Add to HACS** at the top, or search HACS for
   **Anycubic 3D Printer - Local Control - NO Cloud**, then **Download**.
2. **Restart Home Assistant.**
3. **Settings → Devices & Services → Add Integration →** search **Anycubic**.
4. Enter the printer's IP address or host name (for example `192.168.1.50`).

Tip: reserve the printer's IP address in your router so it does not change. If it does change,
open the integration and choose ⋮ → **Reconfigure**.

## Print from Home Assistant

*Kobra S1 and S1 Max. On other models these entities are not created.*

### Quick start

1. Slice the file for your printer in your slicer and export it (`.gcode`, `.gcode.3mf` or a
   sliced single-plate `.3mf`, up to 100 MB).
2. Add the [example script](#example-script) below. It gives you a **Print a file** form.
3. Open the form from a dashboard button, pick the file, choose the switches you want, and run it.

Home Assistant checks the file and the printer first. If anything is wrong it sends nothing and
tells you why in one sentence. If all is well it sends the file, starts the print and shows a
**Started printing** notification.

### The two switches

| Switch | What it does | Printers |
| --- | --- | --- |
| **Cool the bed when done** | At the end, the head lifts and parks at the back, the fans run, and the printer waits. Once the bed has cooled, Home Assistant ends the print and tells you the part is ready to take off. | S1 Max |
| **Skip the purge line** | Leaves out the strip of filament the slicer lays along the back of the bed before each print, so it does not need scraping off. | S1, S1 Max |

Both switches change only the copy that is sent to the printer. Your file is never changed.

**Cool the bed when done, in detail**

- **How cool:** 28 °C for PLA, 25 °C for PETG, 30 °C for anything else (the coolest, if a print
  uses several). With the door closed, a 70 °C bed takes about half an hour.
- **Time limit:** if the bed has not cooled within 90 minutes, the print is ended anyway and you
  are told the bed is still warm.
- **Want the part sooner?** Press **Stop**, wait for the head to stop moving, and take it off.
- The **Bed cool-down** sensor shows *Print running, Cooling, Cool, Not cooled in time* or
  *Stopped*. The *Print event* entity fires `clear_ready` when cooling starts and `clear_done`
  when the bed is cool.
- While it waits, the nozzle is held at 140 °C and the printer takes no temperature or fan changes.
- Nothing touches the part, and the print is never resumed: the wait is ended with a stop.
- The file must be sliced for this printer, give its height, and have no pause of its own.
  Otherwise it is refused; turn the switch off to print it normally.

**Skip the purge line, in detail**

- The head makes the same moves; only the filament is left out. The printer has already purged
  into its chute by then.
- The very start of the first layer can come out a little thin. Try it on a part where that does
  not matter first.
- Works on files from the Anycubic slicer. A file whose purge line is not recognized is refused
  before anything is sent.

### Print again

The **Print again** button (or action `anycubic.print_again`) starts the last file sent from Home
Assistant again, from the copy still on the printer. It prints your original file, without either
switch: to cool down or skip the purge line, send the file again. Home Assistant keeps only the
file's name and a few details, across restarts. If the file was deleted from the printer, send it
again.

### Bed needs clearing

Every print starts by lowering the nozzle onto the bed to measure it, so a part left on the bed
gets hit. Home Assistant cannot see the bed, so it asks you.

**Bed needs clearing** turns on after every print (whoever started it), after the printer has been
silent for over a minute, and after every Home Assistant restart or integration reload. While it
is on, Print a file and Print again refuse. Take the part off, then press **Bed is clear**.

> **Never automate "Bed is clear".** It is your word that the bed is empty, and nothing checks it.

### Files and filament

- **Slice for the printer you send to.** A file that names a different printer is refused.
- **With an ACE**, filament N in the file prints from ACE slot N, and each slot must hold the same
  type of material the file asks for (PLA, PETG, …). With a second ACE attached, only
  single-filament files print for now.
- **Without an ACE**, the file must use one filament, printed from the external spool.
- The loaded material must be set on the printer.
- The print starts without the printer's auto-leveling step.
- On the printer, characters other than letters, digits, spaces and `. _ ( ) + -` in the name
  become `_`. Names containing `-ha-pushoff` are reserved.

Only Home Assistant **administrators** can print. With one printer set up, the printer field is
optional.

### Example script

Add this to `scripts.yaml` (or to `configuration.yaml` under `script:`), then point a dashboard
button at it. Leave out a switch you do not use.

```yaml
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
    skip_purge_line:
      name: Skip the purge line
      required: true
      default: false
      selector:
        boolean:
  sequence:
    - action: anycubic.print_file
      data: >-
        {% set data = {'file': file, 'allow_clear_bed': allow_clear_bed | default(false),
                       'skip_purge_line': skip_purge_line | default(false)} %}
        {{ dict(data, printer=printer) if printer is defined and printer else data }}
```

Notify your phone when the part is cool (use your own entity IDs):

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

<details>
<summary><b>Why was my print refused?</b></summary>

| Message | What to do |
| --- | --- |
| Printing from Home Assistant is not available for this printer model yet | Only the S1 and S1 Max can print from Home Assistant |
| Choose a printer | More than one printer is set up: pick one |
| Home Assistant has not heard from the printer… | The printer is off or disconnected. Try again in a minute |
| The printer is busy | A print is running or paused. Nothing is retried. ACE drying does not count |
| A file is already being sent to this printer | Wait for that upload to finish |
| Clear the bed, then press "Bed is clear" | See [Bed needs clearing](#bed-needs-clearing) |
| A problem with the file (unreadable, empty, over 100 MB, wrong type, bad name) | Export it from the slicer again, or rename it |
| The 3MF file is not sliced / holds more than one plate | Slice it and export a single plate |
| The file was sliced for a different printer | Slice it for this printer |
| A slot or material problem | Load the material the file asks for, set its type on the printer, or slice for what is loaded |
| No purge line was found in this file to skip | Turn Skip the purge line off |
| An upload problem (no answer, refused, no space, did not finish) | Nothing was started. Check the printer, LAN Mode and free space, and send it again |
| The printer did not start the print | The file is on the printer: **Print again** retries without sending it |
| The printer did not confirm the start | Check the printer first: large 3MF files take time to unpack, and the job may still start |

</details>

## Entities

| Type | Entities |
| --- | --- |
| Sensor | Status, Pause reason, Nozzle / Bed / Chamber temperature, Progress, Current / Total layers, Time remaining, Elapsed time, Filament used, File name, Firmware, Bed cool-down |
| Image | Object thumbnail, Object top view (the printer's own render of the current job) |
| Binary sensor | Printing, Paused, Bed needs clearing |
| Event | Print event |
| Camera | Camera |
| Light | Chamber light, Camera light |
| Button | Pause, Resume, Stop, Print again, Bed is clear |
| Number | Nozzle / Bed target temperature, Part cooling / Auxiliary / Chamber fan |
| Select | Print speed (silent / standard / sport) |
| ACE | Humidity, Box temperature, Drying time remaining, Loaded slot, Slot 1–4, External spool; Drying and Auto-feed switches; Drying temperature and time |

**Temperatures, fans and print speed can be changed only during a print.** The printer applies
them to the running job and ignores them when idle. There is no preheat command in LAN Mode.

The ACE is checked every 30 seconds; everything else updates as soon as the printer reports it.

## Print events and notifications

The **Print event** entity fires `started`, `paused`, `resumed`, `finished`, `stopped` and `error`.
`paused` and `stopped` carry a `reason_code` and `reason`. `error` fires once for a pause or stop
you did not ask for, such as a clog.

Get a clog on your phone (replace the entity ID with your printer's):

```yaml
automation:
  - alias: Printer error
    triggers:
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

Reason codes seen on real printers so far: 10401 *Paused by user*, 10601 *Stopped by user*, 11518
*Filament is stuck*. Other names come from Anycubic's
[error code pages](https://wiki.anycubic.com/en/error-codes/11518-code/k3). If you see a code with
no English name, please open an issue with it.

## Example dashboard

[`dashboards/anycubic_dashboard.yaml`](dashboards/anycubic_dashboard.yaml) has the camera, the ACE
slots in their real filament colors, printer status, all controls, and the Print a file, Print
again, Bed is clear and Cool-down tiles. It needs the
[Mushroom](https://github.com/piitaya/lovelace-mushroom) and
[card-mod](https://github.com/thomasloven/lovelace-card-mod) cards from HACS, and assumes the
default device names.

## Troubleshooting

- **"Re-enable LAN Mode" prompt:** the printer went back to cloud mode. Turn LAN Mode on at the
  printer and submit the prompt.
- **Cannot connect during setup:** the error names the step that failed. Check the IP address,
  that LAN Mode is on, and that the printer firmware is up to date.
- **Printer unavailable after its IP changed:** ⋮ → **Reconfigure** on the integration.
- **Loaded slot shows "None":** no filament is in the toolhead.
- **Camera will not play:** make sure `ffmpeg` is available and the printer's stream port is
  reachable from Home Assistant (18088 on most models).
- **Chamber light turns on by itself:** the printer switches its light on whenever the camera
  runs. Leave **Preload camera stream** (camera settings) off.
- **Bug reports:** attach diagnostics from the device page (⋮ → **Download diagnostics**).

<details>
<summary><b>Printers running Rinkhals</b></summary>

With the Rinkhals firmware overlay and its camera app on, the printer's own camera stream does not
work. Open the integration → **Configure** → set **Camera source** to **Rinkhals camera**. The feed
is dark unless the chamber light is on, and needs no `ffmpeg`. Set it back to **Printer camera**
to return to the stock stream.

</details>

## How it works

LAN Mode runs a local MQTT broker on the printer (TLS, port 9883). The printer sends its status
there and takes commands there. The login is fetched each time the integration connects and is
never saved, so nothing sensitive is stored in Home Assistant. Files are sent straight to the
printer over your network.

## Feedback and requests

Everything goes through **[GitHub Issues](../../issues/new/choose)**: bug reports (with
diagnostics), feature ideas, and requests for new printers. Telling us your printer and firmware
helps everyone on that model.

## License

Copyright (c) 2026 Chris Fore and contributors.

From v1.2.20 on, this integration is licensed under the GNU Affero General Public License v3.0 or
later. See [LICENSE](LICENSE).
