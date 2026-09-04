#!/usr/bin/env python3
"""v2 brand icons: filled, depth-shaded, product-recognizable AnyCubic Kobra S1 Max + ACE 2.

Implements the ICON-DESIGN-BRIEF: dark enclosed cube + glassy front door + wide low ACE
loaf on the roof + clean tube arcs. Filled shapes (no thin line-art), 3-value depth, one
teal accent. Rendered at 2048px (4x supersample) → 512px LANCZOS.
"""
import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops

SS = 2048
OUT = 512
K = SS / 256.0                      # work in 256-space, scale to SS
TEAL = "#00B5A5"
ACE_LID = "#6E747B"


def C(h, a=255):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)


def p(v):       # 256-space -> SS
    return v * K


def vgradient(c1, c2):
    col = Image.new("RGB", (1, SS))
    for y in range(SS):
        t = y / (SS - 1)
        col.putpixel((0, y), tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    return col.resize((SS, SS))


def badge_mask():
    m = Image.new("L", (SS, SS), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, SS - 1, SS - 1], radius=int(SS * 0.225), fill=255)
    return m


def badge(bg, vignette=None):
    grad = vgradient(C(bg[0])[:3], C(bg[1])[:3]).convert("RGBA")
    base = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    base.paste(grad, (0, 0), badge_mask())
    if vignette:
        vg = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
        ImageDraw.Draw(vg).ellipse([SS * 0.05, -SS * 0.05, SS * 0.95, SS * 0.75], fill=C(vignette, 110))
        vg = vg.filter(ImageFilter.GaussianBlur(SS * 0.13))
        vg.putalpha(ImageChops.multiply(vg.split()[3], badge_mask()))
        base = Image.alpha_composite(base, vg)
    return base


def sub():
    return Image.new("RGBA", (SS, SS), (0, 0, 0, 0))


def rr(d, box, rad, **kw):
    d.rounded_rectangle([p(box[0]), p(box[1]), p(box[2]), p(box[3])], radius=p(rad), **kw)


def quad(p0, p1, p2, n=48):
    out = []
    for i in range(n + 1):
        t = i / n
        out.append(((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
                    (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]))
    return [(p(x), p(y)) for x, y in out]


# ----------------------------------------------------------------- front-on
def render_front(cfg):
    base = badge(cfg["bg"], cfg.get("vignette"))
    body = cfg["body"]
    bb = cfg.get("body_box", (70, 56, 186, 196))
    bx0, by0, bx1, by1 = bb
    bw, bh = bx1 - bx0, by1 - by0
    cx = (bx0 + bx1) / 2
    ace_h = cfg.get("ace_h", 34)
    ace_w = bw * cfg.get("ace_w_frac", 0.93)
    abx0, abx1 = cx - ace_w / 2, cx + ace_w / 2
    ace_box = (abx0, by0 - ace_h, abx1, by0)
    door = (bx0 + 0.10 * bw, by0 + 0.085 * bh, bx1 - 0.10 * bw, by1 - 0.17 * bh)
    scr = (bx1 - 0.27 * bw, by1 - 0.135 * bh, bx1 - 0.07 * bw, by1 - 0.03 * bh)

    # ground shadow on badge
    sh = sub()
    ImageDraw.Draw(sh).ellipse([p(cx - bw * 0.62), p(by1 - 2), p(cx + bw * 0.62), p(by1 + 16)], fill=(0, 0, 0, 70))
    sh = sh.filter(ImageFilter.GaussianBlur(p(5)))
    sh.putalpha(ImageChops.multiply(sh.split()[3], badge_mask()))
    base = Image.alpha_composite(base, sh)

    prod = sub()
    d = ImageDraw.Draw(prod)

    # feet
    for fx in (bx0 + 0.10 * bw, bx1 - 0.10 * bw):
        rr(d, (fx - 5, by1 - 3, fx + 5, by1 + 5), 2.5, fill=C(body["dark"]))

    # tubes BEHIND ace (rise from roof, hidden under ace base) -> drawn now, ace covers origins
    tl = sub()
    dt = ImageDraw.Draw(tl)
    tcol = C(TEAL, 235) if cfg.get("tube") == "teal" else C("#E8EAED", 190)
    ntube = cfg.get("tubes", 3)
    for i in range(ntube):
        sx = abx0 + ace_w * (0.28 + 0.46 * (i / max(1, ntube - 1)))
        topY = ace_box[1] - (8 + 5 * math.sin(i))
        dt.line(quad((sx, ace_box[1] + 2), (sx + 6, topY), (sx + 14, ace_box[1] + 4)),
                fill=tcol, width=int(p(2.2)), joint="curve")
    prod = Image.alpha_composite(prod, tl)
    d = ImageDraw.Draw(prod)

    # body: dark frame, base, light right strip, top light band
    rr(d, bb, 10, fill=C(body["dark"]))
    rr(d, (bx0 + 3, by0 + 3, bx1 - 3, by1 - 3), 8, fill=C(body["base"]))
    rr(d, (bx1 - 3 - 0.13 * bw, by0 + 6, bx1 - 5, by1 - 6), 4, fill=C(body["light"]))
    rr(d, (bx0 + 6, by0 + 5, bx1 - 6, by0 + 5 + 0.05 * bh), 3, fill=C(body["light"]))

    # door glass (translucent) + highlight streak
    gl = sub()
    dg = ImageDraw.Draw(gl)
    if cfg.get("door") == "teal":
        dg.rounded_rectangle([p(door[0]), p(door[1]), p(door[2]), p(door[3])], radius=p(7), fill=C("#0E3B3A", 175))
    else:
        dg.rounded_rectangle([p(door[0]), p(door[1]), p(door[2]), p(door[3])], radius=p(7), fill=C("#C8D2DC", 150))
    dw = door[2] - door[0]
    if cfg.get("streak", True):
        dg.polygon([(p(door[0] + 0.10 * dw), p(door[1])), (p(door[0] + 0.42 * dw), p(door[1])),
                    (p(door[0] + 0.18 * dw), p(door[3])), (p(door[0] + 0.02 * dw), p(door[3]))],
                   fill=(255, 255, 255, 46))
    prod = Image.alpha_composite(prod, gl)
    d = ImageDraw.Draw(prod)
    rr(d, door, 7, outline=C("#14151A"), width=int(p(2)))

    # door inner accent: bed line / HA chevron / glow bar
    bedy = door[3] - 0.13 * bh
    bx_a, bx_b = door[0] + 0.10 * dw, door[2] - 0.10 * dw
    if cfg.get("inner") == "bed":
        d.line([(p(bx_a), p(bedy)), (p(bx_b), p(bedy))], fill=C(TEAL), width=int(p(3)))
    elif cfg.get("inner") == "bedglow":
        gw = sub()
        ImageDraw.Draw(gw).line([(p(bx_a), p(bedy)), (p(bx_b), p(bedy))], fill=C(TEAL, 255), width=int(p(4)))
        gw = gw.filter(ImageFilter.GaussianBlur(p(4)))
        prod = Image.alpha_composite(prod, gw)
        d = ImageDraw.Draw(prod)
        d.line([(p(bx_a), p(bedy)), (p(bx_b), p(bedy))], fill=C(TEAL), width=int(p(3.4)))
    elif cfg.get("inner") == "ha":
        mx = (bx_a + bx_b) / 2
        ry = bedy
        d.line([(p(bx_a + 0.16 * (bx_b - bx_a)), p(ry)), (p(mx), p(ry - 0.07 * bh)),
                (p(bx_b - 0.16 * (bx_b - bx_a)), p(ry))], fill=C(TEAL), width=int(p(3)), joint="curve")
        d.line([(p(mx), p(ry - 0.07 * bh)), (p(mx), p(ry + 0.03 * bh))], fill=C(TEAL), width=int(p(3)))

    # screen
    rr(d, scr, 3, fill=C("#15161A"))
    rr(d, (scr[0] + 0.18 * (scr[2] - scr[0]), scr[1] + 0.30 * (scr[3] - scr[1]),
           scr[0] + 0.62 * (scr[2] - scr[0]), scr[1] + 0.52 * (scr[3] - scr[1])), 1.5,
       fill=C(TEAL) if cfg.get("ui") == "teal" else C("#2E7DF0"))

    # LED bar (#10)
    if cfg.get("led"):
        led = sub()
        ImageDraw.Draw(led).rounded_rectangle([p(bx1 - 6), p(by0 + 14), p(bx1 - 3.5), p(by1 - 14)],
                                              radius=p(1.2), fill=C(TEAL, 255))
        led = led.filter(ImageFilter.GaussianBlur(p(2)))
        prod = Image.alpha_composite(prod, led)
        d = ImageDraw.Draw(prod)
        rr(d, (bx1 - 5.5, by0 + 14, bx1 - 4, by1 - 14), 1, fill=C(TEAL))

    # ACE loaf: body + lid + seam
    acol = C("#D9DCE0") if cfg.get("ace") == "light" else C("#4A4D52")
    rr(d, ace_box, 8, fill=acol)
    lid_h = ace_h * 0.42
    rr(d, (ace_box[0], ace_box[1], ace_box[2], ace_box[1] + lid_h), 8, fill=C(ACE_LID))
    rr(d, (ace_box[0], ace_box[1] + lid_h - 2, ace_box[2], ace_box[1] + lid_h + 1), 1, fill=C("#3C4043"))
    # tiny logo dot on ace body
    d.ellipse([p(cx - 2), p(ace_box[1] + lid_h + (ace_h - lid_h) * 0.45 - 2),
               p(cx + 2), p(ace_box[1] + lid_h + (ace_h - lid_h) * 0.45 + 2)],
              fill=C("#9AA0A6") if cfg.get("ace") == "light" else C("#7A8088"))

    # long shadow (#9)
    if cfg.get("long_shadow"):
        alpha = prod.split()[3]
        ls = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
        tint = Image.new("RGBA", (SS, SS), C(cfg["long_shadow"], 255))
        ls = Image.composite(tint, ls, alpha)
        ls = ImageChops.offset(ls, int(p(34)), int(p(34)))
        ls = ls.filter(ImageFilter.GaussianBlur(p(3)))
        a = ls.split()[3].point(lambda v: int(v * 0.55))
        a = ImageChops.multiply(a, badge_mask())
        ls.putalpha(a)
        base = Image.alpha_composite(base, ls)

    out = Image.alpha_composite(base, prod)
    return out.resize((OUT, OUT), Image.LANCZOS)


# ----------------------------------------------------------------- isometric
def render_iso(cfg):
    base = badge(cfg["bg"], cfg.get("vignette"))
    body = cfg["body"]
    fx0, fy0, fx1, fy1 = 64, 86, 150, 196        # front face
    fw, fh = fx1 - fx0, fy1 - fy0
    dx, dy = 40, 26                               # depth offset (up-right)

    sh = sub()
    ImageDraw.Draw(sh).ellipse([p(fx0 - 10), p(fy1 - 2), p(fx1 + dx + 12), p(fy1 + 16)], fill=(0, 0, 0, 70))
    sh = sh.filter(ImageFilter.GaussianBlur(p(5)))
    sh.putalpha(ImageChops.multiply(sh.split()[3], badge_mask()))
    base = Image.alpha_composite(base, sh)

    prod = sub()
    d = ImageDraw.Draw(prod)

    def P(*pts):
        return [(p(x), p(y)) for x, y in pts]

    # printer: right side (dark), top (light), front (mid)
    d.polygon(P((fx1, fy0), (fx1 + dx, fy0 - dy), (fx1 + dx, fy1 - dy), (fx1, fy1)), fill=C(body["dark"]))
    d.polygon(P((fx0, fy0), (fx0 + dx, fy0 - dy), (fx1 + dx, fy0 - dy), (fx1, fy0)), fill=C(body["light"]))
    rr(d, (fx0, fy0, fx1, fy1), 7, fill=C(body["base"]))

    # door on front face
    door = (fx0 + 0.13 * fw, fy0 + 0.10 * fh, fx1 - 0.13 * fw, fy1 - 0.16 * fh)
    gl = sub()
    ImageDraw.Draw(gl).rounded_rectangle([p(door[0]), p(door[1]), p(door[2]), p(door[3])], radius=p(6),
                                         fill=C("#0E3B3A", 175) if cfg.get("door") == "teal" else C("#C8D2DC", 150))
    prod = Image.alpha_composite(prod, gl)
    d = ImageDraw.Draw(prod)
    rr(d, door, 6, outline=C("#14151A"), width=int(p(1.8)))
    if cfg.get("inner") == "bed":
        d.line([(p(door[0] + 0.12 * fw), p(door[3] - 0.12 * fh)), (p(door[2] - 0.12 * fw), p(door[3] - 0.12 * fh))],
               fill=C(TEAL), width=int(p(2.6)))
    # screen lower-right of front
    rr(d, (fx1 - 0.26 * fw, fy1 - 0.14 * fh, fx1 - 0.08 * fw, fy1 - 0.03 * fh), 2.5, fill=C("#15161A"))

    # ACE iso slab on the top face
    ah = 22
    a_fy1 = fy0                       # ACE front-bottom sits on printer top-front edge
    a_fy0 = fy0 - ah
    ax0, ax1 = fx0 + 6, fx1 - 6
    # ACE right + top + front
    d.polygon(P((ax1, a_fy0), (ax1 + dx, a_fy0 - dy), (ax1 + dx, a_fy1 - dy), (ax1, a_fy1)), fill=C("#9AA0A6"))
    d.polygon(P((ax0, a_fy0), (ax0 + dx, a_fy0 - dy), (ax1 + dx, a_fy0 - dy), (ax1, a_fy0)), fill=C("#E6E8EB"))
    rr(d, (ax0, a_fy0, ax1, a_fy1), 5, fill=C("#D9DCE0") if cfg.get("ace", "light") == "light" else C("#5A5E64"))
    rr(d, (ax0, a_fy0, ax1, a_fy0 + ah * 0.40), 5, fill=C(ACE_LID))

    # tubes from ACE back-right down into printer top
    tl = sub()
    dt = ImageDraw.Draw(tl)
    tcol = C(TEAL, 235) if cfg.get("tube") == "teal" else C("#E8EAED", 200)
    for i in range(3):
        s = (ax1 + dx * 0.5 - i * 6, a_fy0 - dy * 0.5 - 2)
        e = (fx1 - 0.3 * fw + i * 6, fy0 - 4)
        dt.line(quad(s, (s[0] + 10, s[1] - 14), e), fill=tcol, width=int(p(2)), joint="curve")
    prod = Image.alpha_composite(prod, tl)

    out = Image.alpha_composite(base, prod)
    return out.resize((OUT, OUT), Image.LANCZOS)


# ----------------------------------------------------------------- mono glyph
def render_mono(cfg):
    base = badge(cfg["bg"])
    glyph = C(cfg["glyph"])
    prod = sub()
    d = ImageDraw.Draw(prod)
    bb = (78, 70, 178, 196)
    bx0, by0, bx1, by1 = bb
    bw = bx1 - bx0
    cx = (bx0 + bx1) / 2
    ace = (bx0 + 6, by0 - 30, bx1 - 6, by0)
    rr(d, bb, 12, fill=glyph)
    rr(d, ace, 8, fill=glyph)
    # door knocked out
    door = (bx0 + 14, by0 + 14, bx1 - 14, by1 - 30)
    rr(d, door, 8, fill=(0, 0, 0, 0))
    cut = sub()
    rr(ImageDraw.Draw(cut), door, 8, fill=(255, 255, 255, 255))
    prod_a = prod.split()[3]
    prod_a = ImageChops.subtract(prod_a, cut.split()[3])
    prod.putalpha(prod_a)
    d = ImageDraw.Draw(prod)
    # tubes
    for i in range(2):
        sx = cx - 10 + i * 20
        d.line(quad((sx, ace[1] + 2), (sx + 4, ace[1] - 12), (sx + 12, ace[1] + 2)),
               fill=glyph, width=int(p(3)), joint="curve")
    out = Image.alpha_composite(base, prod)
    return out.resize((OUT, OUT), Image.LANCZOS)


# ----------------------------------------------------------------- configs
DARKBODY = {"base": "#3A3D42", "light": "#50545B", "dark": "#23252A"}
LITBODY = {"base": "#2B2D31", "light": "#3A3D42", "dark": "#1A1B1E"}
BG1 = ("#F5F6F8", "#E3E7EC")
BG2 = ("#1A1D22", "#0E1116")
BG3 = ("#0F2A2C", "#06181A")
BG4 = ("#E9FBF7", "#CDEFE9")

CONCEPTS = [
    ("01-front-light", render_front, dict(bg=BG1, body=LITBODY, ace="light", door="glass", inner="bed", tubes=3, ui="blue")),
    ("02-front-dark", render_front, dict(bg=BG2, body=DARKBODY, ace="light", door="glass", inner="bedglow", tubes=3, ui="teal")),
    ("03-iso-slate", render_iso, dict(bg=BG1, body=LITBODY, ace="light", door="glass", inner="bed")),
    ("04-iso-teal", render_iso, dict(bg=BG3, body=DARKBODY, ace="light", door="teal", tube="teal", vignette=TEAL)),
    ("05-door-glow", render_front, dict(bg=BG2, body={"base": "#1F2125", "light": "#2A2D33", "dark": "#141519"}, ace="light", door="teal", inner="bedglow", tubes=2, ui="teal")),
    ("06-ace-forward", render_front, dict(bg=BG1, body=LITBODY, ace="light", door="glass", inner=None, tubes=0, ace_h=29.4, ace_w_frac=0.693, ui="blue", streak=False)),
    ("06-ace-forward-dark", render_front, dict(bg=("#2A2F37", "#181C22"), body={"base": "#525761", "light": "#6E7480", "dark": "#383C44"}, ace="light", door="glass", inner=None, tubes=0, ace_h=29.4, ace_w_frac=0.693, ui="blue", streak=False)),
    ("07-mono-glyph", render_mono, dict(bg=("#F5F6F8", "#EBEEF1"), glyph="#123A38")),
    ("08-ha-motif", render_front, dict(bg=BG1, body=LITBODY, ace="light", door="glass", inner="ha", tubes=3, ui="teal")),
    ("09-long-shadow", render_front, dict(bg=BG1, body=LITBODY, ace="light", door="glass", inner="bed", tubes=3, ui="teal", long_shadow="#C4CCD4")),
    ("10-tall-dark", render_front, dict(bg=BG3, body=DARKBODY, ace="light", door="teal", inner=None, tubes=4, led=True, body_box=(76, 44, 180, 196), ace_w_frac=0.80, vignette=TEAL)),
]

OUTDIR = "brand-options"


def contact_sheet(items):
    cols, rows = 5, 2
    cell, pad, gap, lh = OUT, 70, 40, 84
    W = pad * 2 + cols * cell + (cols - 1) * gap
    H = pad * 2 + rows * (cell + lh) + (rows - 1) * gap
    sheet = Image.new("RGBA", (W, H), (0xEE, 0xF1, 0xF4, 255))
    d = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=34)
    except Exception:
        font = ImageFont.load_default()
    for idx, (name, img) in enumerate(items):
        r, c = divmod(idx, cols)
        x = pad + c * (cell + gap)
        y = pad + r * (cell + lh + gap)
        sheet.paste(img, (x, y), img)
        lab = name
        tb = d.textbbox((0, 0), lab, font=font)
        d.text((x + (cell - (tb[2] - tb[0])) / 2, y + cell + 20), lab, fill=(0x1F, 0x29, 0x33, 255), font=font)
    sheet.convert("RGB").save(f"{OUTDIR}/contact-sheet-v2.png")


RENDER_ONLY = {"06-ace-forward", "06-ace-forward-dark"}

if __name__ == "__main__":
    rendered = []
    for name, fn, cfg in CONCEPTS:
        if name not in RENDER_ONLY:
            continue
        fn(cfg).save(f"{OUTDIR}/v2-{name}.png")
        rendered.append(name)
    print("Generated:", ", ".join(rendered))
