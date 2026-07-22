# AnyCubic 3D Printer - Local

A Home Assistant custom integration for the **AnyCubic Kobra 3 / S1 series** (and ACE / ACE 2),
talking to the printer's **stock LAN Mode** over its local MQTT broker — **no AnyCubic cloud account,
no rooting**.

[![hacs](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=chrisfore&repository=anycubic_ha_local&category=integration)

> **Status:** v1.1.4 — validated end-to-end on a Kobra S1 Max; the rest of the Kobra 3 / S1 family
> shares the identical protocol. Entities adapt to each model (see **Supported printers**).

## Features

### Monitoring
- **Printer:** status / lifecycle, nozzle / bed / chamber temperatures, progress, current & total
  layers, time remaining, file name; *printing* and *paused* binary sensors.
  (Firmware version shows on the device page as its software version.)
- **Live camera:** the printer's H.264 stream, started on demand.
- **ACE 2:** humidity, box temperature, loaded slot, and all four filament slots (material, color,
  remaining %).

### Control
- **Print:** pause, resume, stop.
- **Temperatures & fans:** nozzle / bed target temperature, part-cooling / auxiliary / chamber fans.
- **Print speed:** silent / standard / sport.
- **Chamber light:** on / off.
- **ACE 2:** drying on/off (with adjustable temperature & time), auto-feed.

## How it works

LAN Mode exposes a local MQTT broker (TLS, port 9883) reached through a documented handshake
(`GET :18910/info` → signed `POST /ctrl` → AES-decrypt → broker credentials). Telemetry arrives as
JSON reports; controls are JSON commands on the same topics. Crypto keys are derived on the fly and
never persisted — nothing sensitive is stored on the Home Assistant host.

## Supported printers

The Kobra 3 / S1 generation all speak the same signed LAN protocol, so they share one code path. The
integration reads the printer's `modelId` and only creates the entities that model actually has.

| Printer | `modelId` | Status |
| --- | --- | --- |
| Kobra S1 Max | `20029` | ✅ Validated on hardware (enclosed: chamber temp/light, box fan, camera, ACE 2) |
| Kobra S1 | `20025` | ✅ Same enclosed feature set, identical protocol |
| Kobra 3 / 3 V2 / 3 Max | `20024` / `20027` / `20026` | ✅ Open-frame — no chamber temp/light or box fan; camera is the AnyCubic add-on |
| Kobra 2 Pro / Plus / Max | `20021` / `20022` / `20023` | ⚠️ Experimental — older *unsigned* handshake, not yet validated (you'll get a clear "unsupported handshake" message if it can't connect) |
| Kobra X | `20030` | ⚠️ Experimental — different controller; camera is WebRTC (no local stream) so no camera entity |
| Photon (resin) | — | ❌ Different platform, no local LAN API |

Enclosure-only entities (chamber temperature, chamber light, box fan) appear **only** on the enclosed
S1 / S1 Max. The camera appears on the Kobra 3 / S1 family (built-in on enclosed, add-on on Kobra 3).
ACE / ACE 2 entities appear whenever a multi-color box is attached, on any of these printers.

### My printer isn't listed — help me add it

The Kobra 3 / S1 family shares one protocol, so adding a model is usually quick — and the
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
older Kobra 2 or a Kobra X on a different protocol), say so in the issue with your model + firmware.

## Requirements

- An AnyCubic printer from the table above with **LAN Mode enabled**
  (printer screen → *Settings → Network → LAN Mode*).
- Home Assistant 2024.9 or newer, with HACS.
- The `ffmpeg` add-on / dependency (bundled with most HA installs) for the camera.

## Installation (HACS custom repository)

**Quickest:** click the **"Add to HACS"** badge at the top — it opens your Home Assistant and pre-fills
this repository. Then **Download** and **restart**. Or add it manually:

1. HACS → ⋮ → **Custom repositories**.
2. Add this repository's URL, category **Integration**, then **Add**.
3. Find **AnyCubic 3D Printer - Local** in HACS and **Download**.
4. **Restart Home Assistant.**
5. **Settings → Devices & Services → Add Integration → AnyCubic 3D Printer - Local**.
6. Enter the printer's **IP address or hostname** (e.g. `192.168.1.50` or `printer.local`).
   Host names are resolved with whatever DNS/mDNS your HA host has.

## Entities

| Domain | Entities |
| --- | --- |
| `sensor` | Status, Nozzle/Bed/Chamber temperature, Progress, Current/Total layer, Time remaining, File name, Firmware |
| `binary_sensor` | Printing, Paused |
| `camera` | Camera |
| `light` | Chamber light |
| `button` | Pause, Resume, Stop |
| `number` | Nozzle/Bed target temperature, Part-cooling/Auxiliary/Chamber fan |
| `select` | Print speed (silent / standard / sport) |
| ACE `sensor` | Humidity, Box temperature, Loaded slot, Slot 1–4 (material/color/remaining as attributes) |
| ACE `switch` | Drying, Auto-feed |
| ACE `number` | Drying temperature, Drying time |

Chamber temperature, Chamber light, and the box (chamber) fan are created **only on enclosed models**
(S1 / S1 Max); the camera only on models with a local stream; ACE entities only when a box is attached.
See **Supported printers** above.

## Example dashboard

[`dashboards/anycubic_dashboard.yaml`](dashboards/anycubic_dashboard.yaml) — camera on top, the ACE 2
filament slots (tinted with each slot's real color) below it, then the printer status and all the
controls. It uses the [Mushroom](https://github.com/piitaya/lovelace-mushroom) and
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
- **Loaded slot shows nothing / "None":** no filament is loaded into the toolhead (the printer's
  `-1` sentinel is shown as *None*).
- **Camera won't play:** make sure `ffmpeg` is available and the printer is reachable on port 18088.
- **Bug reports:** download diagnostics from the device page (⋮ → *Download diagnostics*) — it is
  automatically redacted of addresses and identifiers.

## Feedback, bugs & requests

Everything runs through **[GitHub Issues](../../issues/new/choose)**:

- **Bug report** — include redacted diagnostics (device page → ⋮ → *Download diagnostics*).
- **Feature request / feedback** — ideas and suggestions are welcome.
- **Request support for my printer** — see *[My printer isn't listed](#my-printer-isnt-listed--help-me-add-it)* above.

## 🍺 Buy me a beer

This is a free, no-cloud labour of love. If it saved you some hassle and you'd like to say thanks:

- **[Sponsor on GitHub](https://github.com/sponsors/chrisfore)** — 0% fees, it all reaches me.
- **[Venmo @Chris-Fore-20](https://venmo.com/u/Chris-Fore-20)**

Totally optional — bug reports and printer info are just as appreciated. 🙌

## License

MIT — see [LICENSE](LICENSE).
