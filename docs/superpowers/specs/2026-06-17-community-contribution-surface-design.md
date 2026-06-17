# Community & Contribution Surface — Design

**Date:** 2026-06-17
**Status:** Approved (pending written-spec review)
**Scope:** Repo-level additions only. Nothing under `custom_components/` changes, so the
integration runtime and HACS validation are unaffected.

## Goal

Give users of the public `chrisfore/anycubic_ha_local` repo three things:

1. A clear way to send **feedback / bug reports / feature requests** (via GitHub Issues).
2. A guided way to **report an unlisted printer's details** so the maintainer can add model
   support — without shipping any script: users *find and paste* the info themselves.
3. A **"buy me a beer"** donation path with the lowest possible fees.

## Non-goals (deliberately cut)

- **No shipped probe/helper scripts.** Users open a URL in a browser and paste the output.
  (The existing `research/probe/*.py` dev tools stay as-is — research artifacts, not user tools.)
- **No `mailto:`** / no public email address. Issues is the contact channel.
- **No in-HA "button" entity** for feedback/donation — a button entity can't open a URL or send
  mail, so it would be a dead end.
- **No Questions/Discussions page** for now.

## Deliverables

### A. Feedback / requests → GitHub Issue Forms

Add `.github/ISSUE_TEMPLATE/` with three YAML issue forms plus a config:

#### `bug_report.yml` — "Bug report"
- `markdown`: intro + reminder to attach **redacted Download diagnostics** (device page → ⋮ →
  *Download diagnostics*; it is auto-redacted).
- `input` (required): Home Assistant version.
- `input` (required): Integration version (from HACS).
- `dropdown` (required): Printer model — Kobra S1 Max / Kobra S1 / Kobra 3 / 3 V2 / 3 Max /
  Kobra 2 Pro / Plus / Max / Kobra X / Other.
- `textarea` (required): What happened vs. what you expected.
- `textarea`: Steps to reproduce.
- `textarea`: Relevant logs (`custom_components.anycubic`), rendered as a code block.
- `checkboxes`: "I attached redacted diagnostics" / "I searched existing issues".

#### `feature_request.yml` — "Feature request / feedback"
- `markdown`: intro.
- `textarea` (required): The idea or feedback.
- `textarea`: Why it matters / use case.
- `checkboxes`: "I searched existing issues".

#### `printer_request.yml` — "Request support for my printer"
This is the model-onboarding form. No script — pure browser + paste.
- `markdown`: "Help me add your AnyCubic printer. Two quick browser steps below; the `token`
  field is a temporary value and is safe to share. Redact your IP if you prefer."
- `input` (required): Printer model — marketing name, e.g. "Kobra 3 Max".
- `input` (required): Firmware version (printer screen → Settings → about/version).
- `textarea` (required): **Paste `/info`.** Instructions: open
  `http://<your-printer-ip>:18910/info` in a browser on the same network and paste the JSON.
  This yields `modelId` (the key field the maintainer needs) + firmware.
- `textarea` (optional): **Paste any feature endpoint.** Instructions: also try
  `http://<your-printer-ip>:18910/feature` and `http://<your-printer-ip>:18910/features`; paste
  whatever returns JSON (skip if it 404s). *(This crowdsources whether a stock HTTP feature
  endpoint exists — see "Open question" below.)*
- `checkboxes` (required): "Which features does your printer physically have?" — Enclosed/heated
  chamber with door · Built-in camera · Multicolor box (ACE / ACE 2) attached · Touchscreen ·
  Auto-leveling.
- `textarea`: Anything else (quirks, what works, what doesn't).
- `checkboxes`: "I searched existing issues / the Supported printers table".

#### `config.yml`
- `blank_issues_enabled: false` (force people through a form).
- `contact_links`: one entry — **🍺 Buy me a beer** → the README "Buy me a beer" section anchor
  (`https://github.com/chrisfore/anycubic_ha_local#-buy-me-a-beer`), so it shows on the "New
  issue" chooser and always resolves — even before Sponsors is approved, since that section lists
  both the Sponsor button and Venmo. No Questions/Discussions link.

### B. 🍺 Buy me a beer — Sponsors (0% fee) + Venmo

#### `.github/FUNDING.yml`
```yaml
github: [chrisfore]
custom: ["https://venmo.com/u/Chris-Fore-20"]
```
- `github:` renders the native **Sponsor** button at the top of the repo (0% fee on personal
  sponsorships — 100% reaches the maintainer). Requires the maintainer to enable Sponsors
  separately (Stripe Connect onboarding + GitHub approval); the button lights up automatically
  once approved.
- `custom:` adds the Venmo link, which works immediately.

#### README section "🍺 Buy me a beer"
Short paragraph + the two links (Sponsor badge + Venmo). Framed as optional thanks, no pressure.

### C. README documentation

Three additions to `README.md`:
1. **"Feedback & bug reports"** — point to Issues; mention the three forms; reiterate that bug
   reports should include redacted diagnostics.
2. **"My printer isn't listed — help me add it"** — the browser-paste flow: open
   `http://<printer-ip>:18910/info` (try `/feature` / `/features` too), then file a *Request
   support for my printer* issue with the paste + which physical features the printer has.
   Cross-link the **Supported printers** table.
3. **"🍺 Buy me a beer"** — as in (B).

## Open question (resolves itself via the form)

Across four sources — both reference HA cloud integrations, the metheos S1 bridge, the project's
own `PROTOCOL-VALIDATED.md`, and Rinkhals' gkapi documentation — stock firmware exposes only
`:18910/info`, `:18910/gcode_upload`, and `:18088/flv` over LAN HTTP; the `features` capability
map and `peripherie` presence flags ride **MQTT**, not HTTP. A `:18910/feature` endpoint was not
found in any source. The `printer_request.yml` form asks users to try `/feature` / `/features`
anyway and paste anything that returns JSON, so real-world pastes will confirm or deny the
endpoint per model/firmware without us guessing. If pastes prove it exists, a follow-up can
simplify onboarding around it.

## Validation

- **HACS / hassfest:** unaffected — no changes under `custom_components/`, `hacs.json`, or
  `manifest.json`. The existing `.github/workflows/validate.yaml` continues to pass.
- **Issue forms:** GitHub validates issue-form YAML on push; verify the three forms render on the
  repo's "New issue" chooser and the Sponsor/beer link appears.
- **FUNDING.yml:** verify the **Sponsor** button appears at the top of the repo (after Sponsors
  approval) and the Venmo link resolves.

## File manifest

```
.github/FUNDING.yml                         (new)
.github/ISSUE_TEMPLATE/config.yml           (new)
.github/ISSUE_TEMPLATE/bug_report.yml       (new)
.github/ISSUE_TEMPLATE/feature_request.yml  (new)
.github/ISSUE_TEMPLATE/printer_request.yml  (new)
README.md                                   (edited: 3 sections added)
```
```
