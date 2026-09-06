# Icon Design Brief — Anycubic Kobra S1 Max + ACE 2 (HA brand icon)

> Synthesized from form-factor + design-language research (2026-06-15). Drives `generate_icons_v2.py`.

## Recognition anchors (only two things matter)
1. **Dark enclosed cube** with a **glassy translucent front door** (the #1 cue).
2. **Wide low ACE "loaf"** perched on the roof (the differentiator).
Everything else (gantry, belts, nozzle, knobs, text) is noise — drop it.

## Canonical silhouette (front-on, 256 canvas)
- Printer body = focal mass, ~62% height, **slightly taller than wide** (S1 Max is the upright one). Body ≈ x[70→186], y[56→196].
- Door = large translucent blue-grey glass rrect inset behind a dark frame, ~70% of front face, with ONE 18%-white highlight streak.
- Touchscreen = small black rounded square, front lower-RIGHT, tiny colored UI bar.
- ACE = wide low rounded "loaf" (≥2.5:1 wider than tall), flush on the roof, ~22% height, two-tone (light body + darker lid + seam line). **No visible spools / colored coils / clear window — that's the Bambu AMS look and is WRONG.**
- Tubes = 3–4 clean thin arcs (the only "mechanism" allowed).
- Feet + ONE soft ground shadow (not per-element drop shadows).

## Style rules
- **Filled, never line-art.** Outlines only as 1–2px seam/frame accents.
- **3 values per surface** (base / light / dark) + glass tone + ONE accent. ≤4 hues on art.
- Depth = flat: 3-face iso tone-step OR slight gradient + one ground shadow. No bevels/reflections.
- **One accent only:** Anycubic-leaning teal `#00B5A5` (pushed away from Bambu green). Used for at most one thing per icon.
- Product is intentionally **monochrome dark grey** — let glass + ACE carry recognition.
- Must read at 48–72px thumbnail. Ship a light `icon.png` + a dark variant.

## Palette
- Body: base `#2B2D31` · light `#3A3D42` · dark `#1A1B1E`
- Glass: `#C8D2DC`@~55% (≈`#5A6470` over dark) · highlight white@18%
- ACE: light body `#D9DCE0` (or matching `#4A4D52`) · lid `#6E747B` · seam `#3C4043`
- Screen `#1A1B1E`, UI bar `#2E7DF0` · Tubes `#E8EAED`@70% or teal · Accent `#00B5A5` · Shadow black@22%
- Dark-variant body: base `#3A3D42` · light `#50545B` · dark `#23252A`
- Grounds: BG-1 cool slate `#F5F6F8→#E3E7EC` · BG-2 charcoal `#1A1D22→#0E1116` · BG-3 teal dusk `#0F2A2C→#06181A` · BG-4 teal-mint `#E9FBF7→#CDEFE9`

## The 10 concepts
1. Front-on hero, **light** (BG-1) — definitive literal icon, teal bed-line.
2. Front-on hero, **dark** (BG-2) — lightened panels, teal under-roof glow; the `dark_icon` mate.
3. **3/4 isometric**, slate (BG-1) — premium product-render feel.
4. 3/4 isometric, **teal-dusk** (BG-3) — moody, one teal tube/door tint.
5. **Door-glow** front (BG-2) — teal-tinted glass + lit-bed bar; "smart/connected".
6. **ACE-forward** stack (BG-4) — ACE slightly larger, 4 clear tubes; "multi-color combo".
7. **Minimal monochrome glyph** — two-box stack, door knocked out; favicon-grade.
8. Printer + **HA motif** (BG-1) — abstract teal house-roofline glowing in the door (no HA logo paste).
9. **Long-shadow flat** (BG-1) — modern app-icon 45° cast shadow.
10. **Tall portrait, dark** (BG-3) — lean into S1 Max upright proportion + vertical teal LED bar.
