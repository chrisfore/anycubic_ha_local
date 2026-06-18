# Community & Contribution Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add GitHub Issue forms, funding links, and README docs so users can report bugs/feedback, submit unlisted-printer details (no script — they paste browser output), and optionally donate (GitHub Sponsors + Venmo).

**Architecture:** Repo-level files only — `.github/FUNDING.yml`, four `.github/ISSUE_TEMPLATE/*.yml` files, and three new `README.md` sections. Nothing under `custom_components/` changes, so the integration runtime and HACS/hassfest validation are untouched.

**Tech Stack:** GitHub YAML issue forms, GitHub `FUNDING.yml`, Markdown. Local validation via Python `yaml.safe_load` (syntax); GitHub validates the issue-form schema on push.

---

## File Structure

```
.github/FUNDING.yml                         (new)  — Sponsor button + Venmo
.github/ISSUE_TEMPLATE/config.yml           (new)  — disable blank issues + beer contact link
.github/ISSUE_TEMPLATE/bug_report.yml       (new)  — bug form
.github/ISSUE_TEMPLATE/feature_request.yml  (new)  — feature/feedback form
.github/ISSUE_TEMPLATE/printer_request.yml  (new)  — unlisted-printer onboarding form (paste /info)
README.md                                   (edit) — 3 sections: feedback, printer onboarding, beer
```

One responsibility per file. The four `ISSUE_TEMPLATE` files are independent forms; `config.yml`
governs the chooser. `FUNDING.yml` is standalone. README ties them together with user-facing prose.

**Note on local YAML check:** if `python3 -c "import yaml"` fails (PyYAML not installed), either
`pip install pyyaml` in a throwaway step or skip the local check and rely on GitHub's on-push
schema validation. Do **not** add PyYAML as a project dependency — it is only a dev convenience.

---

## Task 1: Funding links

**Files:**
- Create: `.github/FUNDING.yml`

- [ ] **Step 1: Create `.github/FUNDING.yml`**

```yaml
# Shown as the repo "Sponsor" button and in the sidebar.
# github: 0% platform fee on personal sponsorships — 100% reaches the maintainer.
#   (The button only lights up once GitHub Sponsors is enabled + approved for this account.)
# custom: Venmo works immediately, no approval needed.
github: [chrisfore]
custom: ["https://venmo.com/u/Chris-Fore-20"]
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/FUNDING.yml')); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add .github/FUNDING.yml
git commit -m "chore: add FUNDING.yml (GitHub Sponsors + Venmo)"
```

---

## Task 2: Issue chooser config + bug & feature forms

**Files:**
- Create: `.github/ISSUE_TEMPLATE/config.yml`
- Create: `.github/ISSUE_TEMPLATE/bug_report.yml`
- Create: `.github/ISSUE_TEMPLATE/feature_request.yml`

- [ ] **Step 1: Create `.github/ISSUE_TEMPLATE/config.yml`**

```yaml
blank_issues_enabled: false
contact_links:
  - name: 🍺 Buy me a beer
    url: https://github.com/chrisfore/anycubic_ha_local#-buy-me-a-beer
    about: If this integration saved you some hassle, you can shout me a beer. Totally optional.
```

- [ ] **Step 2: Create `.github/ISSUE_TEMPLATE/bug_report.yml`**

```yaml
name: Bug report
description: Something isn't working with the AnyCubic 3D Printer - Local integration.
title: "[Bug]: "
labels: ["bug"]
body:
  - type: markdown
    attributes:
      value: |
        Thanks for reporting a bug! Please include **redacted diagnostics** — on the printer's
        device page in Home Assistant choose ⋮ → **Download diagnostics** (automatically redacted
        of addresses and identifiers) and attach it in the "Logs / diagnostics" box below.
  - type: input
    id: ha_version
    attributes:
      label: Home Assistant version
      placeholder: e.g. 2026.6.1
    validations:
      required: true
  - type: input
    id: integration_version
    attributes:
      label: Integration version
      description: Shown in HACS, or on the integration's page.
      placeholder: e.g. 1.1.2
    validations:
      required: true
  - type: dropdown
    id: model
    attributes:
      label: Printer model
      options:
        - Kobra S1 Max
        - Kobra S1
        - Kobra 3
        - Kobra 3 V2
        - Kobra 3 Max
        - Kobra 2 Pro
        - Kobra 2 Plus
        - Kobra 2 Max
        - Kobra X
        - Other (tell us below)
    validations:
      required: true
  - type: textarea
    id: what_happened
    attributes:
      label: What happened?
      description: What did you expect, and what happened instead?
    validations:
      required: true
  - type: textarea
    id: steps
    attributes:
      label: Steps to reproduce
      placeholder: |
        1. ...
        2. ...
  - type: textarea
    id: logs
    attributes:
      label: Logs / diagnostics
      description: Paste relevant `custom_components.anycubic` logs and/or attach the redacted diagnostics file.
      render: shell
  - type: checkboxes
    id: checks
    attributes:
      label: Before submitting
      options:
        - label: I attached redacted diagnostics (or explained why not).
        - label: I searched existing issues and didn't find a duplicate.
          required: true
```

- [ ] **Step 3: Create `.github/ISSUE_TEMPLATE/feature_request.yml`**

```yaml
name: Feature request / feedback
description: Suggest an improvement or share feedback.
title: "[Feature]: "
labels: ["enhancement"]
body:
  - type: markdown
    attributes:
      value: |
        Thanks for the idea! Describe what you'd like and why it would help.
  - type: textarea
    id: idea
    attributes:
      label: The idea or feedback
    validations:
      required: true
  - type: textarea
    id: why
    attributes:
      label: Why it matters / your use case
  - type: checkboxes
    id: checks
    attributes:
      label: Before submitting
      options:
        - label: I searched existing issues and didn't find a duplicate.
          required: true
```

- [ ] **Step 4: Validate YAML syntax for all three files**

Run:
```bash
python3 -c "import yaml; [yaml.safe_load(open(f)) for f in ['.github/ISSUE_TEMPLATE/config.yml','.github/ISSUE_TEMPLATE/bug_report.yml','.github/ISSUE_TEMPLATE/feature_request.yml']]; print('ok')"
```
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add .github/ISSUE_TEMPLATE/config.yml .github/ISSUE_TEMPLATE/bug_report.yml .github/ISSUE_TEMPLATE/feature_request.yml
git commit -m "feat: add issue chooser config + bug and feature/feedback forms"
```

---

## Task 3: Printer support request form (no-script onboarding)

**Files:**
- Create: `.github/ISSUE_TEMPLATE/printer_request.yml`

- [ ] **Step 1: Create `.github/ISSUE_TEMPLATE/printer_request.yml`**

```yaml
name: Request support for my printer
description: Help add support for an AnyCubic printer that isn't listed yet.
title: "[Printer]: "
labels: ["printer-support"]
body:
  - type: markdown
    attributes:
      value: |
        Help me add your AnyCubic printer! Two quick browser steps below — no software to install.

        > The `token` value in `/info` is **temporary** and safe to share. You can redact your IP
        > address if you prefer; the key field the maintainer needs is `modelId`.
  - type: input
    id: model_name
    attributes:
      label: Printer model (marketing name)
      placeholder: e.g. Kobra 3 Max
    validations:
      required: true
  - type: input
    id: firmware
    attributes:
      label: Firmware version
      description: Printer screen → Settings → (About / Version).
      placeholder: e.g. 2.5.9.9
    validations:
      required: true
  - type: textarea
    id: info_json
    attributes:
      label: Output of http://<printer-ip>:18910/info
      description: >
        On a device on the same network, open http://YOUR-PRINTER-IP:18910/info in a web browser
        (replace YOUR-PRINTER-IP — e.g. http://192.168.1.50:18910/info). Copy the JSON it shows
        and paste it here.
      render: json
    validations:
      required: true
  - type: textarea
    id: feature_json
    attributes:
      label: Output of http://<printer-ip>:18910/feature (optional)
      description: >
        Also try http://YOUR-PRINTER-IP:18910/feature and http://YOUR-PRINTER-IP:18910/features.
        If either shows JSON, paste it here. If you get "404" / "Not Found" / a blank page, skip it.
      render: json
  - type: checkboxes
    id: hardware
    attributes:
      label: Which features does your printer physically have?
      options:
        - label: Enclosed / heated chamber with a door
        - label: Built-in camera
        - label: Multicolor box attached (ACE / ACE 2)
        - label: Touchscreen
        - label: Auto-leveling
  - type: textarea
    id: notes
    attributes:
      label: Anything else?
      description: Quirks, what works, what doesn't, link to the product page, etc.
  - type: checkboxes
    id: checks
    attributes:
      label: Before submitting
      options:
        - label: I checked the "Supported printers" table and my model isn't already listed.
          required: true
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/ISSUE_TEMPLATE/printer_request.yml')); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add .github/ISSUE_TEMPLATE/printer_request.yml
git commit -m "feat: add 'Request support for my printer' issue form"
```

---

## Task 4: README sections

**Files:**
- Modify: `README.md`

Three additions. Use the exact anchors so cross-links resolve:
the beer section heading must be `## 🍺 Buy me a beer` (anchor `#-buy-me-a-beer`, referenced by
`config.yml`), and the onboarding heading must be `### My printer isn't listed — help me add it`.

- [ ] **Step 1: Add the printer-onboarding subsection at the end of the "Supported printers" section**

Insert immediately **before** the `## Requirements` heading in `README.md`:

```markdown
### My printer isn't listed — help me add it

The Kobra 3 / S1 family shares one protocol, so adding a model is usually quick. To help:

1. On a device on the same network as the printer, open **`http://<printer-ip>:18910/info`** in a
   web browser (for example `http://192.168.1.50:18910/info`). Copy the JSON it returns.
2. Open a **[Request support for my printer](../../issues/new?template=printer_request.yml)** issue,
   paste that JSON, and tick which features your printer physically has (chamber, camera, ACE box).

The `token` field is temporary and safe to share; redact your IP if you like — the key field is
`modelId`. (Curious folks can also try `http://<printer-ip>:18910/feature`; paste it too if it
returns JSON.)
```

- [ ] **Step 2: Add the "Feedback, bugs & requests" section immediately before `## License`**

```markdown
## Feedback, bugs & requests

Everything runs through **[GitHub Issues](../../issues/new/choose)**:

- **Bug report** — include redacted diagnostics (device page → ⋮ → *Download diagnostics*).
- **Feature request / feedback** — ideas and suggestions are welcome.
- **Request support for my printer** — see *[My printer isn't listed](#my-printer-isnt-listed--help-me-add-it)* above.
```

- [ ] **Step 3: Add the "Buy me a beer" section immediately before `## License`** (after the section from Step 2)

```markdown
## 🍺 Buy me a beer

This is a free, no-cloud labour of love. If it saved you some hassle and you'd like to say thanks:

- **[Sponsor on GitHub](https://github.com/sponsors/chrisfore)** — 0% fees, it all reaches me.
- **[Venmo @Chris-Fore-20](https://venmo.com/u/Chris-Fore-20)**

Totally optional — bug reports and printer info are just as appreciated. 🙌
```

- [ ] **Step 4: Verify the new headings and anchors exist**

Run:
```bash
grep -nE "^### My printer isn't listed|^## Feedback, bugs & requests|^## 🍺 Buy me a beer" README.md
```
Expected: three matching lines printed.

- [ ] **Step 5: Verify internal links point at real targets**

Run:
```bash
grep -nE "issues/new\?template=printer_request.yml|issues/new/choose|#my-printer-isnt-listed--help-me-add-it|sponsors/chrisfore|venmo.com/u/Chris-Fore-20" README.md
```
Expected: the printer-request link, the chooser link, the in-page onboarding anchor, the Sponsor link, and the Venmo link all appear.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: README — feedback/issues, printer onboarding, and 'buy me a beer'"
```

---

## Task 5: Final verification pass (no new commit)

**Files:** none (read-only checks)

- [ ] **Step 1: Re-validate every new YAML file at once**

Run:
```bash
python3 -c "import yaml, glob; [yaml.safe_load(open(f)) for f in glob.glob('.github/**/*.yml', recursive=True)]; print('all yaml ok')"
```
Expected: `all yaml ok`

- [ ] **Step 2: Confirm nothing under `custom_components/` changed (HACS-safe)**

Run: `git diff --stat 22ccae0..HEAD -- custom_components/`
Expected: empty output (no integration files touched).

- [ ] **Step 3: Confirm the full set of new/changed files**

Run: `git diff --stat 22ccae0..HEAD -- .github/ README.md`
Expected: `FUNDING.yml`, the four `ISSUE_TEMPLATE/*.yml`, and `README.md` listed.

- [ ] **Step 4: Manual post-push checklist (do after pushing to GitHub)**

  - On the repo's **Issues → New issue** page, the three forms appear and the **🍺 Buy me a beer**
    contact link shows; blank issues are not offered.
  - Each form renders without a "There is an error in your template" banner (GitHub schema check).
  - The **Sponsor** button shows at the top of the repo once Sponsors is approved; the Venmo link
    in the sidebar resolves to `https://venmo.com/u/Chris-Fore-20`.
  - In the rendered README, the **Request support for my printer** link opens that form, and the
    **My printer isn't listed** in-page link jumps to the right section.

---

## Self-Review notes

- **Spec coverage:** A (issues + 3 forms + config, no Discussions link) → Tasks 2–3 + config.yml;
  B (FUNDING Sponsors+Venmo + README beer) → Task 1 + Task 4 Step 3; C (no-script browser-paste
  onboarding + README) → Task 3 + Task 4 Step 1. Non-goals respected: no scripts, no `mailto:`,
  no button entity, no Discussions link.
- **Open `/feature` question:** carried as the optional `feature_json` field in Task 3 — pastes
  will reveal whether the endpoint exists per model/firmware.
- **Anchor consistency:** `config.yml` link `#-buy-me-a-beer` matches the `## 🍺 Buy me a beer`
  heading (Task 4 Step 3); the README Feedback link `#my-printer-isnt-listed--help-me-add-it`
  matches the `### My printer isn't listed — help me add it` heading (Task 4 Step 1).
- **No push step:** committing is in-scope; pushing to origin is the maintainer's call.
```
