#!/usr/bin/env python3
'''
Strip booth - Korean "Life Four Cuts" (인생네컷) & Vintage Photo Booth Strips
----------------------------------------------------------------------------
Tuned for crisp, luminous beauty-flash skin tones, whisper-fine silver grain,
optional 35mm film-edge frame numbers (01 ◀, 02 ◀, LIFE FOUR CUTS) + bottom
brand & date footer directly on the strip, plus live camera & GIF export.
'''
import base64
import datetime
import glob
import io
import math
import os
import random
import socket
import threading
import webbrowser

from flask import Flask, jsonify, request
from PIL import (Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter,
                 ImageFont, ImageOps)

HERE = os.path.dirname(os.path.abspath(__file__))
FW, PAD, GAP = 520, 28, 22                       # frame width, strip padding, gap
SW = FW + 2 * PAD                                # strip width
CARD_W, MARGIN, COLGAP = 1420, 96, 84            # card is 4 in wide (~355 dpi)
TOP, BOTTOM = 100, 240                           # space above / below the strips
CARD_DPI = CARD_W / 4

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 40 * 1024 * 1024


def geo(n):
    '''Sizes for strips of n frames: a 4x6 in card for 3, a 4x8 in card for 4.'''
    ch = 2130 if n == 3 else 2840
    fh = (ch - TOP - BOTTOM - 2 * PAD - (n - 1) * GAP) // n
    sh = n * fh + (n - 1) * GAP + 2 * PAD
    return dict(n=n, ch=ch, fh=fh, sh=sh, sdpi=sh / (6 if n == 3 else 8))


# ----------------------------------------------------------------- film looks
def _lut(r, g, b):
    return [min(255, int(i * k)) for k in (r, g, b) for i in range(256)]


def _soft_beauty_flash(img, glow_amt=0.12, lift=1.07):
    '''Luminous Korean booth flash: lifts midtones/skin without blowing highlights.'''
    # Gentle S-curve / gamma lift for bright, even studio beauty lighting
    img = img.point(lambda v: int(255 * ((v / 255.0) ** 0.88)))
    img = ImageEnhance.Brightness(img).enhance(lift)
    if glow_amt > 0:
        blur = img.filter(ImageFilter.GaussianBlur(7))
        img = Image.blend(img, blur, glow_amt)
    return img


def apply_film(img, film):
    if film == "life4_bw":
        # Crisp, luminous silver B&W like Korean Life Four Cuts (smooth skin, clean greys)
        base = _soft_beauty_flash(img, glow_amt=0.10, lift=1.08)
        g = ImageOps.grayscale(base)
        g = ImageOps.autocontrast(g, cutoff=0.5)
        # Lift shadows slightly so blacks are velvety charcoal rather than crushed
        g = g.point(lambda v: int(16 + v * 0.92))
        g = ImageEnhance.Contrast(g).enhance(1.10)
        return g.convert("RGB")

    if film == "life4_color":
        # Clean, bright studio flash with punchy, slightly cool-pink/red pop
        base = _soft_beauty_flash(img, glow_amt=0.10, lift=1.08)
        base = ImageEnhance.Color(base).enhance(1.16)
        base = ImageEnhance.Contrast(base).enhance(1.06)
        return base.point(_lut(1.02, 1.00, 1.02))

    if film in ("bw", "noir", "sepia", "cyano"):
        g = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=2 if film == "noir" else 1)
        if film == "bw":
            g = ImageEnhance.Contrast(g).enhance(1.15)
        elif film == "noir":
            g = ImageEnhance.Contrast(g).enhance(1.42).point(lambda v: int(255 * (v / 255) ** 1.2))
        if film == "sepia":
            return ImageOps.colorize(g, (34, 20, 10), (255, 240, 208), mid=(158, 112, 72))
        if film == "cyano":
            return ImageOps.colorize(g, (8, 26, 56), (226, 238, 244), mid=(48, 106, 152))
        return g.convert("RGB")

    if film == "vintage":
        img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(0.75)).enhance(1.06)
        return img.point(_lut(1.06, 1.0, 0.90)).point(lambda v: int(v * 0.90 + 22))

    if film == "kodak":
        img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(1.25)).enhance(1.08)
        return img.point(_lut(1.04, 1.0, 0.93))

    return ImageOps.autocontrast(img, cutoff=0.5)


def make_frame(src, zoom, rng, g):
    w, h = src.size
    if zoom > 1:
        cw, ch = w / zoom, h / zoom
        cx = w / 2 + rng.uniform(-.03, .03) * w
        cy = h * 0.42 + rng.uniform(-.03, .03) * h
        l, t = min(max(cx - cw / 2, 0), w - cw), min(max(cy - ch / 2, 0), h - ch)
        src = src.crop((int(l), int(t), int(l + cw), int(t + ch)))
    return ImageOps.fit(src, (FW, g["fh"]), Image.LANCZOS, centering=(0.5, 0.35))


# --------------------------------------------------- analog leaks & 90s stamp
def add_light_leak(f, kind, rng):
    if not kind or kind == "none":
        return f
    W, H = f.size
    layer = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(layer)

    if kind == "amber":
        side = rng.choice(("left", "right", "top", "bottom"))
        for _ in range(rng.randint(2, 3)):
            if side in ("left", "right"):
                cx = rng.randint(-40, 80) if side == "left" else W - rng.randint(-40, 80)
                cy = rng.randint(0, H)
                rx, ry = rng.randint(90, 200), rng.randint(180, 340)
            else:
                cx = rng.randint(0, W)
                cy = rng.randint(-40, 80) if side == "top" else H - rng.randint(-40, 80)
                rx, ry = rng.randint(180, 340), rng.randint(90, 200)
            col = rng.choice([(255, 82, 18), (255, 138, 28), (235, 38, 24)])
            d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=col)
        layer = layer.filter(ImageFilter.GaussianBlur(58))
        return ImageChops.screen(f, ImageEnhance.Brightness(layer).enhance(rng.uniform(0.55, 0.85)))

    if kind == "prism":
        colors = [(255, 65, 105), (255, 185, 45), (55, 215, 175), (85, 135, 255)]
        rng.shuffle(colors)
        ox = rng.randint(-80, W - 80)
        for idx, col in enumerate(colors[:3]):
            x0 = ox + idx * rng.randint(36, 68)
            pts = [(x0, 0), (x0 + 55, 0), (x0 + 185, H), (x0 + 125, H)]
            d.polygon(pts, fill=col)
        layer = layer.filter(ImageFilter.GaussianBlur(42))
        return ImageChops.screen(f, ImageEnhance.Brightness(layer).enhance(0.52))

    if kind == "halation":
        hi = f.convert("L").point(lambda v: max(0, int((v - 210) * 5.0)))
        glow = Image.new("RGB", (W, H), (255, 58, 22))
        halo = Image.composite(glow, Image.new("RGB", (W, H), (0, 0, 0)), hi)
        halo = halo.filter(ImageFilter.GaussianBlur(16))
        return ImageChops.screen(f, ImageEnhance.Brightness(halo).enhance(0.75))

    return f


def _mono_font(size):
    for name in ("DejaVuSansMono-Bold.ttf", "consolab.ttf", "courbd.ttf",
                 "LiberationMono-Bold.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _clean_sans(size, bold=True):
    names = (("DejaVuSans-Bold.ttf", "arialbd.ttf", "Helvetica-Bold.ttf") if bold
             else ("DejaVuSans.ttf", "arial.ttf", "Helvetica.ttf"))
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def add_frame_stamp(f, stamp_text):
    '''Burns a glowing 90s quartz LED date stamp into the lower-right corner.'''
    if not stamp_text:
        return f
    W, H = f.size
    font = _mono_font(22)
    x0, y0, x1, y1 = font.getbbox(stamp_text)
    tw, th = x1 - x0, y1 - y0
    px = W - tw - 24
    py = H - th - 20

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).text((px - x0, py - y0), stamp_text, font=font, fill=(255, 72, 16, 200))
    glow = glow.filter(ImageFilter.GaussianBlur(3.0))

    core = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(core).text((px - x0, py - y0), stamp_text, font=font, fill=(255, 168, 58, 235))

    out = Image.alpha_composite(f.convert("RGBA"), glow)
    out = Image.alpha_composite(out, core)
    return out.convert("RGB")


def finish_frame(f, o, rng):
    '''Subtle flash variation, light leaks, gentle lens falloff, and whisper-fine silver grain.'''
    # Very subtle per-shot flash variance (keep crisp & consistent like real booth)
    f = ImageEnhance.Brightness(f).enhance(rng.uniform(0.985, 1.015))
    f = add_light_leak(f, o.get("leak", "none"), rng)

    v = o["vignette"] / 100
    if v > 0:
        m = Image.radial_gradient("L").resize(f.size, Image.BICUBIC)
        # Much gentler falloff so corners don't turn muddy
        m = m.point(lambda x: int(min(255, 255 * (x / 255) ** 2.6 * v * 0.55)))
        f = Image.composite(Image.new("RGB", f.size, (0, 0, 0)), f, m)

    if o["grain"] > 0:
        # Fine photographic grain instead of heavy rough blotches
        sigma = 2.0 + (o["grain"] / 100.0) * 14.0
        n = Image.effect_noise(f.size, sigma)
        # Slight horizontal/vertical micro-smoothing so grain looks like fine film emulsion
        n = n.filter(ImageFilter.GaussianBlur(0.45))
        # Blend softly around neutral 128 grey
        strength = (o["grain"] / 100.0) * 0.42
        neutral = Image.new("L", f.size, 128)
        n_soft = Image.blend(neutral, n, strength).convert("RGB")
        f = ImageChops.add(f, n_soft, 1, -128)

    if o.get("led_stamp"):
        f = add_frame_stamp(f, o["led_text"])
    return f


# ------------------------------------------- strip builder (with film markings)
def _paste_rotated_text(base_rgba, text, font, fill, cx, cy, angle):
    x0, y0, x1, y1 = font.getbbox(text)
    tw, th = (x1 - x0) + 12, (y1 - y0) + 12
    tmp = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    ImageDraw.Draw(tmp).text((6 - x0, 6 - y0), text, font=font, fill=fill)
    rot = tmp.rotate(angle, resample=Image.BICUBIC, expand=True)
    base_rgba.paste(rot, (int(cx - rot.width // 2), int(cy - rot.height // 2)), rot)


def build_strip(frames, color, o, strip_idx=0):
    n, fh = len(frames), frames[0].height
    is_black = color == "black"
    bg_col = (16, 16, 18) if is_black else (248, 247, 243)
    ink = (235, 235, 238, 230) if is_black else (32, 32, 36, 225)
    sub_ink = (155, 155, 162, 210) if is_black else (110, 110, 118, 210)

    film_marks = o.get("film_marks", False)
    side_pad = 36 if film_marks else PAD
    top_pad = 32 if film_marks else PAD
    bot_pad = 155 if film_marks else PAD
    strip_w = FW + 2 * side_pad
    strip_h = top_pad + n * fh + (n - 1) * GAP + bot_pad

    strip = Image.new("RGB", (strip_w, strip_h), bg_col)
    for i, f in enumerate(frames):
        strip.paste(f, (side_pad, top_pad + i * (fh + GAP)))

    if film_marks:
        rgba = strip.convert("RGBA")
        num_font = _clean_sans(15, bold=True)
        brand_font = _clean_sans(14, bold=True)
        title_font = _clean_sans(32, bold=True)
        date_font = _mono_font(15)

        start_num = 1 if strip_idx == 0 else 40
        for i in range(n):
            y_mid = top_pad + i * (fh + GAP) + fh // 2
            num_str = f"{start_num + i:02d}"
            if strip_idx == 0:
                # Left border markings like "01 ◀", "02 ◀"
                _paste_rotated_text(rgba, f"{num_str} ◀", num_font, ink, side_pad // 2, y_mid, -90)
            else:
                # Right border markings like "40", "LIFE FOUR CUTS", "41"
                if i == 1:
                    _paste_rotated_text(rgba, "LIFE FOUR CUTS", brand_font, ink, strip_w - side_pad // 2, y_mid, -90)
                else:
                    _paste_rotated_text(rgba, num_str, num_font, ink, strip_w - side_pad // 2, y_mid, -90)

        # Bottom brand & date footer directly on the strip (like 인생네컷 / 2026.11.18)
        footer_top = top_pad + n * fh + (n - 1) * GAP
        label = (o.get("strip_brand") or "LIFE FOUR CUTS").strip()
        if label:
            x0, y0, x1, y1 = title_font.getbbox(label)
            ImageDraw.Draw(rgba).text(
                ((strip_w - (x1 - x0)) / 2 - x0, footer_top + 38 - y0),
                label, font=title_font, fill=ink
            )
        stamp_dt = (o.get("strip_date") or "").strip()
        if stamp_dt:
            x0, y0, x1, y1 = date_font.getbbox(stamp_dt)
            ImageDraw.Draw(rgba).text(
                ((strip_w - (x1 - x0)) / 2 - x0, footer_top + 96 - y0),
                stamp_dt, font=date_font, fill=sub_ink
            )
        strip = rgba.convert("RGB")

    return strip


# ---------------------------------------------------------------- paper / wear
def make_paper(W, H, kind, wear, rng):
    w = wear / 100
    base = {"aged": (233, 223, 200), "clean": (249, 248, 244), "dark": (30, 29, 28)}[kind]
    p = Image.new("RGB", (W, H), base)
    grey = Image.new("RGB", (W, H), (128, 128, 128))
    fine = Image.effect_noise((W, H), 22).convert("RGB")
    mott = Image.effect_noise((18, 26), 45).resize((W, H), Image.BICUBIC).convert("RGB")
    p = ImageChops.add(p, Image.blend(grey, fine, 0.06 + 0.10 * w), 1, -128)
    p = ImageChops.add(p, Image.blend(grey, mott, 0.03 + 0.20 * w), 1, -128)
    if kind != "dark":
        edge = Image.radial_gradient("L").resize((W, H), Image.BICUBIC)
        edge = edge.point(lambda x: int(255 * (x / 255) ** 3 * (0.10 + 0.45 * w)))
        tint = (176, 148, 104) if kind == "aged" else (190, 188, 182)
        p = Image.composite(Image.new("RGB", (W, H), tint), p, edge)
    p = p.convert("RGBA")
    if kind == "aged" and w > 0.15:
        L = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(L)
        for _ in range(int(w * 3) + 1):
            cx, cy, r = rng.randrange(W), rng.randrange(H), rng.randrange(80, 260)
            d.ellipse((cx - r, cy - r * .8, cx + r, cy + r * .8),
                      fill=(150, 108, 52, rng.randrange(8, 22)))
        p = Image.alpha_composite(p, L.filter(ImageFilter.GaussianBlur(50)))
    C = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(C)
    lite = (255, 255, 255, 50 if kind == "dark" else 85)
    for _ in range(int(w * 2.5)):
        dx = rng.randrange(-40, 40)
        if rng.random() < .5:
            x = rng.randrange(150, W - 150)
            d.line((x, 0, x + dx, H), fill=(70, 50, 25, 55), width=2)
            d.line((x + 3, 0, x + 3 + dx, H), fill=lite, width=2)
        else:
            y = rng.randrange(150, H - 150)
            d.line((0, y, W, y + dx), fill=(70, 50, 25, 55), width=2)
            d.line((0, y + 3, W, y + 3 + dx), fill=lite, width=2)
    p = Image.alpha_composite(p, C.filter(ImageFilter.GaussianBlur(0.9)))
    return p.convert("RGB")


def add_dust(img, wear, rng):
    if wear <= 2:
        return img
    W, H = img.size
    w = wear / 100
    S = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(S)
    for _ in range(int(10 + 360 * w)):
        x, y, r = rng.randrange(W), rng.randrange(H), rng.choice((1, 1, 1, 2))
        fill = ((40, 30, 20, rng.randrange(45, 130)) if rng.random() < .7
                else (255, 255, 255, rng.randrange(70, 165)))
        d.ellipse((x, y, x + r, y + r), fill=fill)
    for _ in range(int(w * 10)):
        x, y = rng.randrange(W), rng.randrange(H)
        pts = [(x, y)]
        for _ in range(3):
            x += rng.randrange(-26, 26)
            y += rng.randrange(-26, 26)
            pts.append((x, y))
        d.line(pts, fill=(255, 255, 255, 55), width=1)
    return Image.alpha_composite(img.convert("RGBA"), S).convert("RGB")


def rough_edge(img, wear, rng):
    W, H = img.size
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).rounded_rectangle((8, 8, W - 9, H - 9), radius=22, fill=255)
    if wear > 8:
        m = m.filter(ImageFilter.GaussianBlur(4))
        n = Image.effect_noise((W, H), 110).filter(ImageFilter.GaussianBlur(1.5))
        m = ImageChops.add(m, n, 1, -128).point(lambda v: 255 if v > 128 else 0)
        m = m.filter(ImageFilter.GaussianBlur(0.8))
    return Image.composite(img, Image.new("RGB", (W, H), (255, 255, 255)), m)


# --------------------------------------------- scrapbook tape, stamps & cutline
def _draw_washi_piece(card, cx, cy, angle, color, rng):
    tw, th = 195, 48
    tape = Image.new("RGBA", (tw + 20, th + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(tape)
    left_pts = [(10 + rng.randint(-4, 4), 10 + y) for y in range(0, th + 1, 6)]
    right_pts = [(10 + tw + rng.randint(-4, 4), 10 + y) for y in range(th, -1, -6)]
    d.polygon(left_pts + right_pts, fill=color)
    for x in range(14, tw + 6, 5):
        d.line((x, 11, x, 10 + th - 1), fill=(255, 255, 255, 26), width=1)
    tape = tape.rotate(angle, resample=Image.BICUBIC, expand=True)
    card.paste(tape, (int(cx - tape.width // 2), int(cy - tape.height // 2)), tape)


def add_scrapbook_decor(card, xs, strip_w, sh, o, rng):
    W, H = card.size
    rgba = card.convert("RGBA")

    if o.get("tape"):
        palette = [
            (236, 218, 168, 175),
            (218, 108, 96, 165),
            (132, 174, 164, 165),
            (242, 198, 92, 170),
        ]
        c1 = rng.choice(palette)
        c2 = rng.choice(palette)
        for idx, x in enumerate(xs):
            col = c1 if idx == 0 else c2
            _draw_washi_piece(rgba, x + strip_w // 2, TOP - 4, rng.uniform(-5.5, 5.5), col, rng)
            _draw_washi_piece(rgba, x + strip_w // 2, TOP + sh + 4, rng.uniform(-5.5, 5.5), col, rng)

    if o.get("cutline") and len(xs) == 2:
        mid_x = W // 2
        ink = (110, 104, 96, 175) if o["paper"] != "dark" else (185, 180, 170, 175)
        d = ImageDraw.Draw(rgba)
        for y in range(28, H - 28, 26):
            d.line((mid_x, y, mid_x, y + 13), fill=ink, width=3)
        sfont = _mono_font(22)
        d.text((mid_x - 24, 34), "CUT", font=sfont, fill=ink)

    if o.get("retro_stamp"):
        stamp_label = rng.choice(["* ORIGINAL *", "NO RETAKES", "AUTO-PHOTO", "4-POSE BOOTH"])
        sfont = _mono_font(22)
        x0, y0, x1, y1 = sfont.getbbox(stamp_label)
        bw, bh = (x1 - x0) + 28, (y1 - y0) + 18
        st = Image.new("RGBA", (bw + 16, bh + 16), (0, 0, 0, 0))
        sd = ImageDraw.Draw(st)
        ink = (186, 42, 36, 185) if o["paper"] != "dark" else (238, 110, 96, 195)
        sd.rounded_rectangle((8, 8, 8 + bw, 8 + bh), radius=6, outline=ink, width=3)
        sd.text((8 + 14 - x0, 8 + 9 - y0), stamp_label, font=sfont, fill=ink)
        st = st.rotate(rng.uniform(-14, 14), resample=Image.BICUBIC, expand=True)
        rgba.paste(st, (W - st.width - 42, H - st.height - 34), st)

    return rgba.convert("RGB")


# --------------------------------------------------------------------- caption
def load_font(size):
    for folder in (os.path.join(HERE, "fonts"), os.path.join(os.path.dirname(HERE), "fonts")):
        for path in sorted(glob.glob(os.path.join(folder, "*.[to]tf"))):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    for name in ("segoeprb.ttf", "segoepr.ttf", "BRADHITC.TTF", "comicbd.ttf",
                 "Chalkboard.ttc", "Noteworthy.ttc", "MarkerFelt.ttc",
                 "DejaVuSans-Bold.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def draw_caption(card, text, kind, rng, yc):
    if not text:
        return
    W = card.width
    size = 96
    while True:
        font = load_font(size)
        x0, y0, x1, y1 = font.getbbox(text)
        if x1 - x0 <= W - 2 * MARGIN or size <= 30:
            break
        size -= 4
    ink = (24, 22, 20, 235) if kind != "dark" else (225, 220, 208, 235)
    layer = Image.new("RGBA", (W, 260), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(((W - (x1 - x0)) / 2 - x0, 130 - (y1 - y0) / 2 - y0),
                               text, font=font, fill=ink)
    layer = layer.rotate(rng.uniform(-.7, .7), resample=Image.BICUBIC, center=(W / 2, 130))
    card.paste(layer, (0, int(yc - 130)), layer)


# ------------------------------------------------------------------- rendering
def build_card(strips, o, rng, g):
    sw = strips[0].width
    sh = strips[0].height
    ch = max(g["ch"], TOP + sh + 230)
    card_w = max(CARD_W, 2 * MARGIN + 2 * sw + COLGAP)
    card = make_paper(card_w, ch, o["paper"], o["wear"], rng)
    xs = ([MARGIN, MARGIN + sw + COLGAP] if len(strips) == 2 else [(card_w - sw) // 2])
    for st, x in zip(strips, xs):
        card.paste(st, (x, TOP))
    card = add_scrapbook_decor(card, xs, sw, sh, o, rng)
    text = " \u2013 ".join(t for t in (o["caption"].strip(), o["date"].strip()) if t)
    draw_caption(card, text, o["paper"], rng, TOP + sh + (ch - TOP - sh) / 2)
    card = add_dust(card, o["wear"], rng)
    return rough_edge(card, o["wear"], rng)


def render(photos, o, rng):
    n = o["frames"]
    g = geo(n)
    strips, all_frames = [], []
    for s in range(2):
        idx = [i for i in range(s * 4, s * 4 + n) if i in photos]
        if not idx:
            continue
        seq, k = [(i, 1.0) for i in idx], 0
        while o["repeat"] and len(seq) < n:
            seq.append((idx[k % len(idx)], rng.uniform(1.08, 1.24)))
            k += 1
        frames = []
        for i, z in seq:
            f = make_frame(photos[i], z, rng, g)
            processed = finish_frame(apply_film(f, o["film"]), o, rng)
            frames.append(processed)
            all_frames.append(processed)
        strips.append(build_strip(frames, o["strip"], o, strip_idx=s))

    gif_b64 = None
    if all_frames:
        border = (16, 16, 18) if o["strip"] == "black" else (248, 247, 243)
        gif_slides = []
        for fr in all_frames:
            sm = fr.resize((320, int(320 * fr.height / fr.width)), Image.LANCZOS)
            slide = Image.new("RGB", (sm.width + 32, sm.height + 32), border)
            slide.paste(sm, (16, 16))
            gif_slides.append(slide.convert("P", palette=Image.ADAPTIVE, colors=256))
        gbuf = io.BytesIO()
        gif_slides[0].save(
            gbuf,
            format="GIF",
            save_all=True,
            append_images=gif_slides[1:],
            duration=480,
            loop=0,
            optimize=True,
        )
        gif_b64 = f"data:image/gif;base64,{base64.b64encode(gbuf.getvalue()).decode('ascii')}"

    if o["layout"] == "card":
        return [(build_card(strips, o, rng, g), CARD_DPI)], gif_b64
    return [(add_dust(st, o["wear"], rng), g["sdpi"]) for st in strips], gif_b64


# ---------------------------------------------------------------------- routes
@app.get("/")
def index():
    return PAGE


@app.post("/generate")
def generate():
    photos = {}
    for i in range(8):
        f = request.files.get(f"slot{i}")
        if f and f.filename:
            try:
                im = ImageOps.exif_transpose(Image.open(f.stream)).convert("RGB")
            except Exception:
                return jsonify(error="One of the photos couldn't be read. Use JPG, PNG or WebP files."), 400
            im.thumbnail((2000, 2000))
            photos[i] = im
    if not photos:
        return jsonify(error="Add at least one photo first."), 400
    g = request.form.get

    def num(k, d):
        try:
            return max(0, min(100, int(g(k, d))))
        except ValueError:
            return d

    today_led = datetime.date.today().strftime("%m '%d '%y")
    today_dot = datetime.date.today().strftime("%Y.%m.%d")
    o = dict(
        film=g("film", "life4_bw"),
        strip=g("strip", "black"),
        paper=g("paper", "clean"),
        layout=g("layout", "strips"),
        grain=num("grain", 16),
        vignette=num("vignette", 12),
        wear=num("wear", 0),
        caption=g("caption", "")[:60],
        date=g("date", "")[:30],
        repeat=g("repeat") == "1",
        frames=4 if g("frames") == "4" else 3,
        leak=g("leak", "none"),
        led_stamp=g("led_stamp") == "1",
        led_text=(g("led_text", "") or today_led)[:18],
        film_marks=g("film_marks") == "1",
        strip_brand=g("strip_brand", "LIFE FOUR CUTS")[:24],
        strip_date=g("strip_date", today_dot)[:20],
        tape=g("tape") == "1",
        cutline=g("cutline") == "1",
        retro_stamp=g("retro_stamp") == "1",
    )

    rendered, gif_b64 = render(photos, o, random.Random(random.randrange(1 << 30)))
    out = []
    for n, (img, dpi) in enumerate(rendered, 1):
        jpg_buf = io.BytesIO()
        img.save(jpg_buf, "JPEG", quality=93, dpi=(dpi, dpi))
        jpg_b64 = base64.b64encode(jpg_buf.getvalue()).decode("ascii")

        out.append(dict(
            name=f"photobooth-{n}",
            w=round(img.width / dpi, 2),
            h=round(img.height / dpi, 2),
            jpg=f"data:image/jpeg;base64,{jpg_b64}"
        ))
    return jsonify(prints=out, gif=gif_b64)


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Strip booth</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
<style>
:root{--ink:#131a33;--mist:#f2f4f9;--line:#dce1ee;--mute:#5d6685;--cherry:#d8362c;--butter:#ffd966;--curtain:#9e1a22;
 --thumb:grayscale(1) contrast(1.08) brightness(1.08);
 --head:'Bricolage Grotesque','Trebuchet MS',system-ui,sans-serif;--body:'Instrument Sans',system-ui,-apple-system,'Segoe UI',sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--mist);color:var(--ink);font:500 16px/1.5 var(--body)}
.app{display:grid;grid-template-columns:minmax(0,1fr) minmax(400px,42%);min-height:100vh}
.studio{width:100%;max-width:800px;margin-left:auto;padding:44px 52px 0}
h1{margin:0;font:800 clamp(40px,6vw,64px)/.95 var(--head);letter-spacing:-.035em}
.lede{margin:12px 0 34px;color:var(--mute);font-size:18px;max-width:44ch}
.sec{padding:30px 0 34px;border-top:1px solid var(--line)}
h2{margin:0 0 18px;font:700 22px/1.1 var(--head);letter-spacing:-.01em;display:flex;align-items:center;gap:12px}
h2 .n{width:30px;height:30px;border-radius:50%;background:var(--butter);display:grid;place-items:center;font-size:15px}
.hint{color:var(--mute);font-size:14px;margin:10px 0 0}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.row{display:grid;grid-template-columns:138px 1fr;gap:10px 16px;align-items:center;margin-bottom:16px}
.row>label,.row>.l{font-weight:600;font-size:15px}
.opt{position:relative;display:inline-block}
.opt input{position:absolute;inset:0;opacity:0;margin:0;cursor:pointer;width:100%;height:100%}
.pills{display:flex;flex-wrap:wrap;gap:8px}
.pill{display:block;padding:8px 15px;border-radius:999px;border:1px solid var(--line);background:#fff;font-weight:600;font-size:15px}
input:checked+.pill{background:var(--ink);border-color:var(--ink);color:#fff}
input:focus-visible+.pill,input:focus-visible+.film,.slot:focus-visible,.btn:focus-visible,.go:focus-visible{outline:3px solid var(--cherry);outline-offset:2px}
.btn{font:600 15px var(--body);padding:9px 16px;border-radius:999px;border:1px solid var(--ink);background:#fff;color:var(--ink);cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:6px}
.btn:hover{background:var(--butter)}
.btn.cam-btn{background:var(--ink);color:#fff}
.btn.cam-btn:hover{background:var(--cherry);border-color:var(--cherry);color:#fff}
.strips{display:grid;gap:12px}
.strip-row{display:flex;align-items:center;gap:14px}
.strip-row>b{width:56px;font-size:14px;color:var(--mute);font-weight:600}
.slots{display:flex;gap:8px;background:#12141c;padding:8px;border-radius:4px}
.slot{position:relative;width:88px;aspect-ratio:520/560;background:#262a38;overflow:hidden;cursor:pointer;display:grid;place-items:center;color:#7b84a3;font:500 26px var(--head);border-radius:2px}
.slot img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;filter:var(--thumb)}
.slot.over{outline:3px solid var(--butter);outline-offset:-3px}
.slot .x{position:absolute;top:4px;right:4px;width:22px;height:22px;border:0;border-radius:50%;background:#000c;color:#fff;font:600 15px/1 var(--body);cursor:pointer;display:none}
.slot.filled .x{display:block}.slot.filled span{display:none}
body[data-frames="3"] .slot[data-k="3"]{display:none}
.zone{margin-top:14px;padding:14px 16px;border:1.5px dashed #b4bcd6;border-radius:12px;color:var(--mute);font-size:15px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.zone.over{background:#fff;border-color:var(--ink)}
.zone-acts{display:flex;gap:8px;flex-wrap:wrap}
.films{display:grid;grid-template-columns:repeat(auto-fill,minmax(106px,1fr));gap:10px}
.films .opt{display:block}
.film{display:block;padding:8px 8px 10px;border:1px solid var(--line);border-radius:14px;background:#fff;font-weight:600;font-size:14px}
.film .sw{display:block;height:46px;border-radius:8px;margin-bottom:8px}
input:checked+.film{border-color:var(--ink);box-shadow:0 0 0 2px var(--ink)}
.switch{display:flex;align-items:center;gap:12px;font-weight:600;cursor:pointer}
.switch input{position:absolute;opacity:0}
.track{width:46px;height:26px;border-radius:13px;background:#c5cbe0;position:relative;transition:background .2s;flex:none}
.track::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:transform .2s}
.switch input:checked+.track{background:var(--ink)}.switch input:checked+.track::after{transform:translateX(20px)}
.switch input:focus-visible+.track{outline:3px solid var(--cherry);outline-offset:2px}
input[type=text]{font:inherit;padding:10px 14px;border:1px solid var(--line);border-radius:12px;background:#fff;width:100%;color:var(--ink)}
.inline{display:flex;gap:8px}
input[type=range]{width:100%;accent-color:var(--cherry)}
.bar{position:sticky;bottom:0;background:var(--mist);border-top:1px solid var(--line);padding:16px 0 20px;margin-top:6px}
.go{width:100%;height:58px;border:0;border-radius:999px;background:var(--cherry);color:#fff;font:700 20px var(--head);cursor:pointer;letter-spacing:-.005em}
.go:hover{background:#bf2c23}.go:active{transform:translateY(1px)}.go:disabled{opacity:.7;cursor:wait}
#msg{color:#b3261e;font-weight:600;margin:10px 4px 0;min-height:1.3em}
.stage{position:sticky;top:0;height:100vh;overflow:auto;background-color:var(--curtain);color:#fff;transition:background-color .4s;
 background-image:linear-gradient(#0000008c,#0000 150px),linear-gradient(90deg,#0000004d,#ffffff14 22%,#0000003d 50%,#ffffff12 76%,#0000004d);background-size:100% 100%,170px 100%}
.rod{position:sticky;top:0;height:12px;background:linear-gradient(#eceef1,#8a9098);box-shadow:0 6px 10px #0007;z-index:2}
.stage-in{padding:30px 34px 44px}
.stage h2{color:#fff;font-size:20px}
.empty{max-width:30ch;opacity:.9}
.ghost{width:150px;aspect-ratio:1/3.2;border:2px dashed #ffffff80;border-radius:4px;margin:0 0 22px}
.print{margin:0 0 30px;text-align:center}
.print img{max-width:100%;height:auto;background:#fff;box-shadow:0 18px 30px -10px #000c}
.print:nth-of-type(odd) img{transform:rotate(-.8deg)}.print:nth-of-type(even) img{transform:rotate(.7deg)}
.acts{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:18px}
.stage .btn{background:#fff;border-color:#fff}.stage .btn:hover{background:var(--butter);border-color:var(--butter)}
.tools{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:22px;font-size:15px}
.tools select{font:inherit;padding:8px 10px;border-radius:10px;border:0;color:var(--ink)}
.dev{font-weight:600;font-size:18px}
.prog{height:6px;border-radius:3px;margin-top:14px;background:#ffffff40;overflow:hidden}
.prog::after{content:"";display:block;width:35%;height:100%;background:#fff;animation:slide 1.1s ease-in-out infinite}
@keyframes slide{from{transform:translateX(-100%)}to{transform:translateX(290%)}}

/* Live Booth Camera Modal */
.modal{position:fixed;inset:0;background:#0d101ad9;backdrop-filter:blur(6px);z-index:50;display:grid;place-items:center;padding:20px}
.modal[hidden]{display:none}
.booth-box{background:#181c2b;color:#fff;border-radius:20px;padding:22px;width:100%;max-width:520px;box-shadow:0 24px 60px #000a;border:1px solid #2e3550}
.booth-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.booth-top h3{margin:0;font:700 20px var(--head)}
.cam-wrap{position:relative;aspect-ratio:4/3;background:#000;border-radius:12px;overflow:hidden}
.cam-wrap video{width:100%;height:100%;object-fit:cover;transform:scaleX(-1);filter:var(--thumb)}
.cam-wrap.no-mirror video{transform:none}
.count{position:absolute;inset:0;display:grid;place-items:center;font:800 96px var(--head);color:#fff;text-shadow:0 6px 24px #000c;pointer-events:none}
.flash{position:absolute;inset:0;background:#fff;opacity:0;pointer-events:none;transition:opacity .25s}
.flash.on{opacity:1;transition:none}
.cam-status{position:absolute;bottom:10px;left:12px;background:#000a;padding:4px 10px;border-radius:999px;font-size:13px;font-weight:600}
.booth-acts{display:flex;gap:10px;justify-content:space-between;align-items:center;flex-wrap:wrap;margin-top:16px}
.booth-acts .btn{border-color:#fff;background:#fff;color:var(--ink)}
.booth-acts .btn.primary{background:var(--cherry);border-color:var(--cherry);color:#fff}
.booth-acts .btn.primary:hover{background:#bf2c23}

@media (prefers-reduced-motion:reduce){.prog::after{animation:none;width:100%}.track,.track::after,.stage{transition:none}}
@media (max-width:980px){.app{grid-template-columns:1fr}.studio{padding:30px 20px 0}.stage{position:static;height:auto;min-height:70vh}.row{grid-template-columns:1fr}.slot{width:calc((100vw - 120px)/4)}}
</style></head><body data-frames="4">
<div class="app">
<main class="studio">
 <h1>Strip booth</h1>
 <p class="lede">Turn your photos—or snap live booth shots—into clean Korean 4-cut or vintage photo strips.</p>

 <section class="sec">
  <h2><span class="n">1</span>Add or snap your photos</h2>
  <div class="top"><div class="row" style="margin:0;grid-template-columns:auto auto"><span class="l">Frames per strip</span><div class="pills" id="frames"></div></div></div>
  <div class="strips" id="strips"></div>
  <div class="zone" id="zone">
   <span>Drop photos here or use your camera.</span>
   <div class="zone-acts">
    <button class="btn cam-btn" id="openCam" type="button">Live booth camera</button>
    <button class="btn" id="pick" type="button">Choose photos</button>
   </div>
  </div>
  <p class="hint">Click a frame to replace its photo, or drag one frame onto another to swap. The second strip is optional.</p>
  <input type="file" id="multi" accept="image/*" multiple hidden><input type="file" id="single" accept="image/*" hidden>
 </section>

 <section class="sec">
  <h2><span class="n">2</span>Choose a film</h2>
  <div class="films" id="films"></div>
 </section>

 <section class="sec">
  <h2><span class="n">3</span>Film border markings &amp; extras</h2>
  <div class="row">
   <span class="l">4-Cut markings</span>
   <div>
    <label class="switch"><input type="checkbox" id="filmMarks" checked><span class="track"></span>Add 35mm edge numbers (01 ◀) &amp; strip footer</label>
    <div id="marksRow" style="margin-top:10px;display:grid;grid-template-columns:1fr 140px;gap:8px">
     <input type="text" id="stripBrand" value="LIFE FOUR CUTS" maxlength="24" placeholder="LIFE FOUR CUTS">
     <input type="text" id="stripDate" maxlength="20" placeholder="2026.11.18">
    </div>
   </div>
  </div>
  <div class="row"><span class="l">Light leaks</span><div class="pills" id="leak"></div></div>
  <div class="row">
   <span class="l">90s LED date</span>
   <div>
    <label class="switch"><input type="checkbox" id="ledStamp"><span class="track"></span>Burn orange timestamp into each frame</label>
    <div id="ledRow" style="margin-top:10px" hidden><input type="text" id="ledText" maxlength="18" placeholder="11 '18 '26"></div>
   </div>
  </div>
  <div class="row"><span class="l">Washi tape</span><label class="switch" style="font-weight:500"><input type="checkbox" id="tape"><span class="track"></span>Tape strips onto the card</label></div>
  <div class="row"><span class="l">Retro stamp</span><label class="switch" style="font-weight:500"><input type="checkbox" id="retroStamp"><span class="track"></span>Add vintage booth ink stamp</label></div>
  <div class="row"><span class="l">Cut line</span><label class="switch" style="font-weight:500"><input type="checkbox" id="cutline"><span class="track"></span>Scissor cut-line down the middle (2 strips)</label></div>
 </section>

 <section class="sec">
  <h2><span class="n">4</span>Finish the print</h2>
  <div class="row"><span class="l">Layout</span><div class="pills" id="layout"></div></div>
  <div class="row"><span class="l">Strip colour</span><div class="pills" id="strip"></div></div>
  <div class="row"><label for="grain">Film grain</label><input type="range" id="grain" min="0" max="100" value="16"></div>
  <div class="row"><label for="vignette">Dark corners</label><input type="range" id="vignette" min="0" max="100" value="12"></div>
  <div class="row"><label for="wear">Paper wear</label><input type="range" id="wear" min="0" max="100" value="0"></div>
  <div class="row"><span class="l">Card paper</span><div class="pills" id="paper"></div></div>
  <div class="row"><label for="caption">Card caption</label><input type="text" id="caption" value="" placeholder="Optional card caption" maxlength="60"></div>
  <div class="row"><label for="date">Card date</label><div class="inline"><input type="text" id="date" maxlength="30"><button class="btn" id="today" type="button">Today</button></div></div>
  <div class="row"><span class="l">Short strips</span><label class="switch" style="font-weight:500"><input type="checkbox" id="repeat" checked><span class="track"></span>Repeat photos to fill every frame</label></div>
  <p class="hint">Tip: "Strips on their own" with "Life 4-Cut B&amp;W" or "Life 4-Cut Color" matches classic Korean photo booth strips.</p>
 </section>

 <div class="bar"><button class="go" id="go" type="button">Develop prints</button><p id="msg" role="alert"></p></div>
</main>

<aside class="stage" aria-live="polite">
 <div class="rod"></div>
 <div class="stage-in">
  <h2>Your prints</h2>
  <div id="out"><div class="ghost"></div><p class="empty">Press Develop prints and your strips will show up here.</p></div>
 </div>
</aside>
</div>

<!-- Live Webcam Booth Modal -->
<div class="modal" id="camModal" hidden>
 <div class="booth-box">
  <div class="booth-top">
   <h3>Photo Booth Camera</h3>
   <button class="btn" id="closeCam" type="button" style="padding:5px 12px">Close</button>
  </div>
  <div class="cam-wrap" id="camWrap">
   <video id="video" autoplay playsinline muted></video>
   <div class="count" id="countdown"></div>
   <div class="flash" id="flash"></div>
   <div class="cam-status" id="camStatus">Ready</div>
  </div>
  <div class="booth-acts">
   <div style="display:flex;gap:8px">
    <button class="btn" id="flipCam" type="button">Mirror</button>
    <button class="btn" id="snapOne" type="button">Snap 1</button>
   </div>
   <button class="btn primary" id="startSession" type="button">Start 4-Pose Session</button>
  </div>
 </div>
</div>

<script>
const $=s=>document.querySelector(s),$$=s=>document.querySelectorAll(s);
const N=8,slots=Array(N).fill(null);
const FILMS=[
 ['life4_bw','Life 4-Cut B&W','Crisp silver monochrome with smooth beauty flash','#1a1a1d,#b8b8be,#f4f4f6','grayscale(1) contrast(1.08) brightness(1.08)'],
 ['life4_color','Life 4-Cut Color','Luminous studio flash with punchy clean colors','#e85a71,#f7b2bd,#ffffff','contrast(1.06) brightness(1.07) saturate(1.16)'],
 ['bw','Classic B&W','Bright flash, soft contrast','#000,#fff','grayscale(1) contrast(1.15) brightness(1.05)'],
 ['noir','Noir','Deep blacks, hard contrast','#000,#777,#fff','grayscale(1) contrast(1.5) brightness(.92)'],
 ['sepia','Sepia','Brown tone of an old print','#22140a,#9e7048,#fff0d0','sepia(1) contrast(1.05)'],
 ['vintage','Faded colour','Washed out, warm shadows','#3a2a1c,#b08a6a,#f3e6cf','saturate(.75) sepia(.25) contrast(1.05)'],
 ['kodak','Warm colour','Rich colour, golden cast','#4a1d10,#d9822b,#fff1c4','saturate(1.28) sepia(.15) contrast(1.08)'],
 ['cyano','Cyanotype','Blue print tone','#081a38,#306a98,#e2eef4','grayscale(1) sepia(1) hue-rotate(185deg) saturate(2.2) brightness(.95)'],
 ['clean','Natural','Colour as shot','#25405a,#6a9a5a,#f1d9a0','none']
];

function pills(id,name,items,sel){$('#'+id).innerHTML=items.map((it,i)=>`<label class="opt"><input type="radio" name="${name}" value="${it[0]}" ${it[0]===sel||(!sel&&!i)?'checked':''}><span class="pill">${it[1]}</span></label>`).join('')}
pills('frames','frames',[['3','3'],['4','4']],'4');
pills('leak','leak',[['none','None'],['amber','Warm amber'],['prism','Prism flare'],['halation','Red halation']],'none');
pills('layout','layout',[['strips','Strips on their own'],['card','Both strips on one card']],'strips');
pills('strip','strip',[['black','Black'],['white','White']],'black');
pills('paper','paper',[['clean','Clean white'],['aged','Aged cream'],['dark','Black card']],'clean');
$('#films').innerHTML=FILMS.map((f,i)=>`<label class="opt" title="${f[2]}"><input type="radio" name="film" value="${f[0]}" ${i?'':'checked'}><span class="film"><span class="sw" style="background:linear-gradient(90deg,${f[3]})"></span>${f[1]}</span></label>`).join('');

const val=n=>document.querySelector(`input[name=${n}]:checked`).value;
const frames=()=>+val('frames'),visible=i=>(i%4)<frames();
function today(){
 const d=new Date();
 $('#date').value=d.toLocaleString('en',{month:'short'})+' '+d.getDate()+" '"+String(d.getFullYear()).slice(2);
 const mm=String(d.getMonth()+1).padStart(2,'0'),dd=String(d.getDate()).padStart(2,'0'),yy=String(d.getFullYear()).slice(2);
 $('#ledText').value=`${mm} '${dd} '${yy}`;
 $('#stripDate').value=`${d.getFullYear()}.${mm}.${dd}`;
}
today();$('#today').onclick=today;
$('#ledStamp').onchange=()=>$('#ledRow').hidden=!$('#ledStamp').checked;
$('#filmMarks').onchange=()=>$('#marksRow').hidden=!$('#filmMarks').checked;

let pending=null;
for(let r=0;r<2;r++){const row=document.createElement('div');row.className='strip-row';row.innerHTML=`<b>Strip ${r+1}</b>`;
 const box=document.createElement('div');box.className='slots';
 for(let k=0;k<4;k++){const i=r*4+k,s=document.createElement('div');s.className='slot';s.tabIndex=0;s.draggable=true;s.dataset.i=i;s.dataset.k=k;
  s.setAttribute('role','button');s.setAttribute('aria-label','Strip '+(r+1)+', frame '+(k+1));
  s.innerHTML='<span>+</span><img alt="" hidden><button class="x" type="button" aria-label="Remove photo">×</button>';
  s.onclick=e=>{if(e.target.classList.contains('x')){e.stopPropagation();clearSlot(i);return}pending=i;$('#single').click()};
  s.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();pending=i;$('#single').click()}};
  s.ondragstart=e=>e.dataTransfer.setData('text/slot',i);
  s.ondragover=e=>{e.preventDefault();s.classList.add('over')};s.ondragleave=()=>s.classList.remove('over');
  s.ondrop=e=>{e.preventDefault();e.stopPropagation();s.classList.remove('over');
   if(e.dataTransfer.files.length){addFiles(e.dataTransfer.files,i);return}
   const from=e.dataTransfer.getData('text/slot');if(from!==''){const a=+from;[slots[a],slots[i]]=[slots[i],slots[a]];paint()}};
  box.append(s)}
 row.append(box);$('#strips').append(row)}

function clearSlot(i){if(slots[i])URL.revokeObjectURL(slots[i].url);slots[i]=null;paint()}
function paint(){$$('.slot').forEach(s=>{const d=slots[s.dataset.i],img=s.querySelector('img');
 s.classList.toggle('filled',!!d);img.hidden=!d;if(d)img.src=d.url;else img.removeAttribute('src')})}
function addFiles(list,at){let skipped=0;$('#msg').textContent='';
 [...list].filter(f=>f.type.startsWith('image/')).forEach((f,n)=>{
  const i=(n===0&&at!=null)?at:slots.findIndex((s,j)=>!s&&visible(j));
  if(i<0){skipped++;return}
  if(slots[i])URL.revokeObjectURL(slots[i].url);
  slots[i]={file:f,url:URL.createObjectURL(f)}});
 paint();if(skipped)$('#msg').textContent='Every frame is full, so '+skipped+' photo'+(skipped>1?'s were':' was')+' skipped.'}

$('#pick').onclick=()=>$('#multi').click();
$('#multi').onchange=e=>{addFiles(e.target.files);e.target.value=''};
$('#single').onchange=e=>{if(e.target.files.length)addFiles(e.target.files,pending);e.target.value=''};
const zone=$('#zone');
zone.ondragover=e=>{e.preventDefault();zone.classList.add('over')};zone.ondragleave=()=>zone.classList.remove('over');
zone.ondrop=e=>{e.preventDefault();zone.classList.remove('over');addFiles(e.dataTransfer.files)};
addEventListener('dragover',e=>e.preventDefault());addEventListener('drop',e=>e.preventDefault());

$('#frames').onchange=()=>{
 document.body.dataset.frames=frames();
 $('#startSession').textContent=`Start ${frames()}-Pose Session`;
};
$('#films').onchange=()=>{const f=FILMS.find(x=>x[0]===val('film'));document.documentElement.style.setProperty('--thumb',f[4])};

// ------------------------------------------------ Live Webcam Booth Logic
let camStream=null, mirrored=true, sessionRunning=false;
const video=$('#video'), camModal=$('#camModal');

async function startCamera(){
 try{
  camStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'user',width:{ideal:1280},height:{ideal:960}},audio:false});
  video.srcObject=camStream;
  $('#startSession').textContent=`Start ${frames()}-Pose Session`;
  $('#camStatus').textContent='Strike a pose!';
  camModal.hidden=false;
 }catch(err){
  $('#msg').textContent='Camera access denied or unavailable. Check browser permissions.';
 }
}
function stopCamera(){
 sessionRunning=false;
 if(camStream){camStream.getTracks().forEach(t=>t.stop());camStream=null}
 video.srcObject=null;
 camModal.hidden=true;
 $('#countdown').textContent='';
}
$('#openCam').onclick=startCamera;
$('#closeCam').onclick=stopCamera;
$('#flipCam').onclick=()=>{mirrored=!mirrored;$('#camWrap').classList.toggle('no-mirror',!mirrored)};

function captureFrameBlob(){
 return new Promise(resolve=>{
  const w=video.videoWidth||1280, h=video.videoHeight||960;
  const c=document.createElement('canvas');
  c.width=w; c.height=h;
  const ctx=c.getContext('2d');
  if(mirrored){ctx.translate(w,0);ctx.scale(-1,1)}
  ctx.drawImage(video,0,0,w,h);
  const fl=$('#flash');fl.classList.add('on');setTimeout(()=>fl.classList.remove('on'),120);
  c.toBlob(blob=>{
   const file=new File([blob],`booth-${Date.now()}.jpg`,{type:'image/jpeg'});
   resolve(file);
  },'image/jpeg',0.9);
 });
}

const wait=ms=>new Promise(r=>setTimeout(r,ms));

$('#snapOne').onclick=async()=>{
 if(sessionRunning)return;
 const file=await captureFrameBlob();
 addFiles([file]);
 $('#camStatus').textContent='Captured 1 photo!';
};

$('#startSession').onclick=async()=>{
 if(sessionRunning)return;
 sessionRunning=true;
 const total=frames();
 for(let step=0;step<total;step++){
  if(!sessionRunning)return;
  $('#camStatus').textContent=`Pose ${step+1} of ${total} — get ready!`;
  for(let n=3;n>=1;n--){
   if(!sessionRunning)return;
   $('#countdown').textContent=n;
   await wait(850);
  }
  $('#countdown').textContent='';
  const file=await captureFrameBlob();
  let target=slots.findIndex((s,j)=>!s&&visible(j));
  if(target<0)target=step%total;
  addFiles([file],target);
  await wait(550);
 }
 sessionRunning=false;
 $('#camStatus').textContent='All poses captured!';
 await wait(450);
 stopCamera();
};

// ------------------------------------------------ Client-side Image Compression
function compressImage(file, maxDim=1400, quality=0.86){
 return new Promise(resolve=>{
  const img=new Image();
  const url=URL.createObjectURL(file);
  img.onload=()=>{
   URL.revokeObjectURL(url);
   let w=img.width, h=img.height;
   if(w>maxDim || h>maxDim){
    if(w>h){h=Math.round(h*(maxDim/w));w=maxDim}
    else{w=Math.round(w*(maxDim/h));h=maxDim}
   }
   const c=document.createElement('canvas');
   c.width=w; c.height=h;
   c.getContext('2d').drawImage(img,0,0,w,h);
   c.toBlob(blob=>resolve(blob || file),'image/jpeg',quality);
  };
  img.onerror=()=>{URL.revokeObjectURL(url);resolve(file)};
  img.src=url;
 });
}

$('#go').onclick=async()=>{
 const msg=$('#msg');msg.textContent='';
 if(!slots.some((s,i)=>s&&visible(i))){msg.textContent='Add or snap at least one photo first.';return}
 const fd=new FormData();
 const go=$('#go');go.disabled=true;go.textContent='Developing…';
 $('#out').innerHTML='<p class="dev">Developing your prints &amp; animated reel…</p><div class="prog"></div>';
 if(innerWidth<=980)$('.stage').scrollIntoView({behavior:'smooth'});

 for(let i=0;i<slots.length;i++){
  if(slots[i]&&visible(i)){
   const compressed=await compressImage(slots[i].file);
   fd.append('slot'+i,compressed,'photo'+i+'.jpg');
  }
 }
 ['film','layout','strip','paper','frames','leak'].forEach(n=>fd.append(n,val(n)));
 ['caption','date','grain','vignette','wear'].forEach(n=>fd.append(n,$('#'+n).value));
 fd.append('film_marks',$('#filmMarks').checked?'1':'0');
 fd.append('strip_brand',$('#stripBrand').value);
 fd.append('strip_date',$('#stripDate').value);
 fd.append('led_text',$('#ledText').value);
 fd.append('led_stamp',$('#ledStamp').checked?'1':'0');
 fd.append('tape',$('#tape').checked?'1':'0');
 fd.append('retro_stamp',$('#retroStamp').checked?'1':'0');
 fd.append('cutline',$('#cutline').checked?'1':'0');
 fd.append('repeat',$('#repeat').checked?'1':'0');
 try{
  const r=await fetch('/generate',{method:'POST',body:fd});
  if(r.status===413)throw new Error('Photos are too large even after compression. Try fewer photos.');
  const j=await r.json();
  if(!r.ok)throw new Error(j.error||'Something went wrong.');
  show(j.prints, j.gif);
 }catch(err){$('#out').innerHTML='<div class="ghost"></div><p class="empty">Nothing was printed.</p>';msg.textContent=err.message}
 go.disabled=false;go.textContent='Develop prints';
};

// ------------------------------------------------ Client-side PNG, PDF, GIF & Print
function triggerDownload(url, filename){
 const a=document.createElement('a');
 a.href=url; a.download=filename;
 document.body.appendChild(a); a.click(); a.remove();
}

function downloadPNG(jpgDataUrl, filename){
 const img=new Image();
 img.onload=()=>{
  const c=document.createElement('canvas');
  c.width=img.width; c.height=img.height;
  c.getContext('2d').drawImage(img,0,0);
  c.toBlob(blob=>{
   const u=URL.createObjectURL(blob);
   triggerDownload(u,filename);
   setTimeout(()=>URL.revokeObjectURL(u),5000);
  },'image/png');
 };
 img.src=jpgDataUrl;
}

function downloadPDF(p){
 if(!window.jspdf){alert('PDF library still loading, try again in a second.');return}
 const {jsPDF}=window.jspdf;
 const orient=p.w>p.h?'l':'p';
 const pdf=new jsPDF({orientation:orient,unit:'in',format:[p.w,p.h]});
 pdf.addImage(p.jpg,'JPEG',0,0,p.w,p.h);
 pdf.save(p.name+'.pdf');
}

function openPrint(p, fit){
 const win=window.open('','_blank');
 if(!win){alert('Please allow pop-ups to print.');return}
 const css=fit==='1'
  ?'img{max-width:100vw;max-height:100vh;display:block;margin:0 auto}'
  :`img{width:${p.w}in;height:auto;display:block}`;
 win.document.write(`<!doctype html><title>Print</title><style>@page{margin:0}body{margin:0}${css}</style><img src="${p.jpg}" onload="setTimeout(()=>window.print(),200)">`);
 win.document.close();
}

function show(prints, gifUrl){
 const multi=prints.length>1;
 const gifBtn=gifUrl?`<a class="btn" href="${gifUrl}" download="photobooth-reel.gif">Animated GIF</a>`:'';
 $('#out').innerHTML=`<div class="tools"><button class="btn" id="again" type="button">Develop again</button>
  <label>Print size <select id="fit"><option value="0">Actual size</option><option value="1">Fit to page</option></select></label>
  ${gifBtn}</div>`+
 prints.map((p,n)=>`<figure class="print"><img src="${p.jpg}" alt="Photo booth ${multi?'strip '+(n+1):'card'}" width="${p.w*100}" height="${p.h*100}">
  <div class="acts">
   <button class="btn" type="button" data-png="${n}">PNG</button>
   <a class="btn" href="${p.jpg}" download="${p.name}.jpg">JPG</a>
   <button class="btn" type="button" data-pdf="${n}">PDF</button>
   <button class="btn" type="button" data-print="${n}">Print</button>
  </div></figure>`).join('');
 $('#again').onclick=()=>$('#go').click();
 $$('[data-png]').forEach(b=>b.onclick=()=>{const p=prints[b.dataset.png];downloadPNG(p.jpg,p.name+'.png')});
 $$('[data-pdf]').forEach(b=>b.onclick=()=>downloadPDF(prints[b.dataset.pdf]));
 $$('[data-print]').forEach(b=>b.onclick=()=>openPrint(prints[b.dataset.print],$('#fit').value));
}
</script></body></html>
'''


def free_port(start=5000):
    for port in range(start, start + 50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


if __name__ == "__main__":
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    print(f"Strip booth is running at {url}  (Ctrl+C to stop)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
