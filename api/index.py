#!/usr/bin/env python3
"""
Strip booth - turn photos into photo booth strips (Vercel-ready)
----------------------------------------------------------------
Deploy on Vercel:
  - Place this file at api/index.py
  - Include requirements.txt (Flask, Pillow) and vercel.json
  - Optional: put a handwriting-style .ttf/.otf in api/fonts/
"""
import base64
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

_rm = {}


def have_rembg():
    """Optional background removal. Disabled gracefully when rembg isn't installed."""
    if _rm.get("ok"):
        return True
    try:
        import importlib
        importlib.invalidate_caches()
        from rembg import new_session, remove
        _rm.update(ok=True, new_session=new_session, remove=remove)
        return True
    except Exception:
        return False


HERE = os.path.dirname(os.path.abspath(__file__))
FW, PAD, GAP = 520, 26, 22                       # frame width, strip padding, gap
SW = FW + 2 * PAD                                # strip width
CARD_W, MARGIN, COLGAP = 1420, 96, 84            # card is 4 in wide (~355 dpi)
TOP, BOTTOM = 100, 240                           # space above / below the strips
CARD_DPI = CARD_W / 4

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 40 * 1024 * 1024


def geo(n):
    """Sizes for strips of n frames: a 4x6 in card for 3, a 4x8 in card for 4."""
    ch = 2130 if n == 3 else 2840
    fh = (ch - TOP - BOTTOM - 2 * PAD - (n - 1) * GAP) // n
    sh = n * fh + (n - 1) * GAP + 2 * PAD
    return dict(n=n, ch=ch, fh=fh, sh=sh, sdpi=sh / (6 if n == 3 else 8))


# ----------------------------------------------------------------- film looks
def _lut(r, g, b):
    return [min(255, int(i * k)) for k in (r, g, b) for i in range(256)]


def apply_film(img, film):
    if film in ("bw", "noir", "sepia", "cyano"):
        g = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=3 if film == "noir" else 1)
        if film == "bw":
            g = ImageEnhance.Contrast(g).enhance(1.18)
        elif film == "noir":
            g = ImageEnhance.Contrast(g).enhance(1.5).point(lambda v: int(255 * (v / 255) ** 1.25))
        if film == "sepia":
            return ImageOps.colorize(g, (34, 20, 10), (255, 240, 208), mid=(158, 112, 72))
        if film == "cyano":
            return ImageOps.colorize(g, (8, 26, 56), (226, 238, 244), mid=(48, 106, 152))
        return g.convert("RGB")
    if film == "vintage":
        img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(0.7)).enhance(1.08)
        return img.point(_lut(1.07, 1.0, 0.88)).point(lambda v: int(v * 0.88 + 26))
    if film == "kodak":
        img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(1.3)).enhance(1.12)
        return img.point(_lut(1.05, 1.0, 0.92))
    return ImageOps.autocontrast(img, cutoff=0.5)


def make_frame(src, zoom, rng, g):
    w, h = src.size
    if zoom > 1:
        cw, ch = w / zoom, h / zoom
        cx = w / 2 + rng.uniform(-.04, .04) * w
        cy = h * 0.42 + rng.uniform(-.04, .04) * h
        l, t = min(max(cx - cw / 2, 0), w - cw), min(max(cy - ch / 2, 0), h - ch)
        src = src.crop((int(l), int(t), int(l + cw), int(t + ch)))
    return ImageOps.fit(src, (FW, g["fh"]), Image.LANCZOS, centering=(0.5, 0.35))


def finish_frame(f, o, rng):
    """Flash glow, lens falloff and film grain, slightly different per frame."""
    f = ImageEnhance.Brightness(f).enhance(rng.uniform(0.94, 1.06))
    f = Image.blend(f, f.filter(ImageFilter.GaussianBlur(10)), 0.18)
    v = o["vignette"] / 100
    if v > 0:
        m = Image.radial_gradient("L").resize(f.size, Image.BICUBIC)
        m = m.point(lambda x: int(min(255, 255 * (x / 255) ** 2 * v * 0.95)))
        f = Image.composite(Image.new("RGB", f.size, (0, 0, 0)), f, m)
    if o["grain"] > 0:
        n = Image.effect_noise(f.size, 10 + o["grain"] * 0.55)
        n = n.filter(ImageFilter.GaussianBlur(0.6)).convert("RGB")
        f = ImageChops.add(f, n, 1, -128)
    return f


def build_strip(frames, color):
    n, fh = len(frames), frames[0].height
    col = (14, 14, 14) if color == "black" else (246, 244, 238)
    strip = Image.new("RGB", (SW, n * fh + (n - 1) * GAP + 2 * PAD), col)
    for i, f in enumerate(frames):
        strip.paste(f, (PAD, PAD + i * (fh + GAP)))
    return strip


# ------------------------------------------------------- backdrops (optional)
COLORS = {"red": (158, 26, 34), "navy": (30, 46, 104), "green": (34, 86, 66),
          "mustard": (206, 152, 38), "pink": (222, 150, 166), "grey": (128, 132, 138)}
_session, _lock = None, threading.Lock()


def mix(c, t, k):
    return tuple(int(c[i] * (1 - k) + t[i] * k) for i in range(3))


def _noisy(img, amount):
    grey = Image.new("RGB", img.size, (128, 128, 128))
    n = Image.effect_noise(img.size, 40).convert("RGB")
    return ImageChops.add(img, Image.blend(grey, n, amount), 1, -128)


def backdrop(kind, color, size, rng):
    """A fresh wall / curtain / studio backdrop the size of one frame."""
    W, H = size
    c = COLORS.get(color, COLORS["red"])
    dark = Image.new("RGB", size, (0, 0, 0))
    edge = Image.radial_gradient("L").resize(size, Image.BICUBIC)
    if kind == "curtain":
        img = Image.new("RGB", size)
        d = ImageDraw.Draw(img)
        ph, folds = rng.uniform(0, 6.28), W / rng.uniform(62, 84)
        for x in range(W):
            t = x / W * folds * 6.283 + ph
            b = 0.62 + 0.38 * (0.62 * math.sin(t) + 0.38 * math.sin(t * 2.3 + 1.1))
            sheen = max(0, math.sin(t)) ** 8 * 0.28
            d.line((x, 0, x, H), fill=tuple(min(255, int(ch * b + 255 * sheen)) for ch in c))
        img = Image.composite(dark, img, edge.point(lambda v: int(v * 0.45)))
        return _noisy(img.filter(ImageFilter.GaussianBlur(1.2)), 0.06)
    if kind == "brick":
        img = Image.new("RGB", size, (184, 172, 158))
        d = ImageDraw.Draw(img)
        bw, bh, m = 118, 44, 6
        ox, oy = rng.randrange(bw + m), rng.randrange(bh + m)
        for r in range(-1, H // (bh + m) + 2):
            y, off = r * (bh + m) - oy, ((bw + m) // 2 if r % 2 else 0)
            for x in range(-(bw + m) - ox + off, W + bw, bw + m):
                v = rng.randrange(-22, 22)
                d.rectangle((x, y, x + bw, y + bh), fill=(150 + v, 66 + v // 2, 50 + v // 2))
        img = Image.composite(dark, img, edge.point(lambda v: int(v * 0.35)))
        return _noisy(img.filter(ImageFilter.GaussianBlur(1.4)), 0.12)
    if kind == "stripes":
        light = mix(c, (255, 255, 255), 0.55)
        img = Image.new("RGB", size, light)
        d = ImageDraw.Draw(img)
        sw = rng.choice((46, 54, 62))
        for x in range(-rng.randrange(sw * 2), W, sw * 2):
            d.rectangle((x, 0, x + sw, H), fill=mix(c, (255, 255, 255), 0.15))
        img = Image.composite(dark, img, edge.point(lambda v: int(v * 0.3)))
        return _noisy(img.filter(ImageFilter.GaussianBlur(1.0)), 0.08)
    if kind == "studio":                          # seamless paper with a spotlight
        img = Image.composite(Image.new("RGB", size, mix(c, (0, 0, 0), 0.4)),
                              Image.new("RGB", size, mix(c, (255, 255, 255), 0.3)),
                              edge.point(lambda v: int(v * 0.85)))
        return _noisy(img, 0.05)
    base = mix(c, (255, 255, 255), 0.45)          # painted wall
    img = Image.new("RGB", size, base)
    n = Image.effect_noise((10, 14), 50).resize(size, Image.BICUBIC).convert("RGB")
    img = ImageChops.add(img, Image.blend(Image.new("RGB", size, (128,) * 3), n, 0.28), 1, -128)
    img = Image.composite(Image.new("RGB", size, mix(base, (0, 0, 0), 0.45)), img,
                          edge.point(lambda v: int(v * 0.3)))
    return _noisy(img, 0.05)


def cutout(im):
    """Person on a transparent background (needs the optional rembg package)."""
    global _session
    with _lock:
        if _session is None:
            _session = _rm["new_session"]("u2net_human_seg")
    small = im.copy()
    small.thumbnail((1200, 1200))
    return _rm["remove"](small, session=_session).convert("RGBA")


def place(person, zoom, rng, g, o):
    f = make_frame(person, zoom, rng, g)
    a = f.getchannel("A").filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.1))
    bg = backdrop(o["bg_kind"], o["bg_color"], f.size, rng)
    bg.paste(f.convert("RGB"), (0, 0), a)
    return bg


# ---------------------------------------------------------------- paper / wear
def make_paper(W, H, kind, wear, rng):
    w = wear / 100
    base = {"aged": (233, 223, 200), "clean": (249, 248, 244), "dark": (30, 29, 28)}[kind]
    p = Image.new("RGB", (W, H), base)
    grey = Image.new("RGB", (W, H), (128, 128, 128))
    fine = Image.effect_noise((W, H), 30).convert("RGB")
    mott = Image.effect_noise((18, 26), 60).resize((W, H), Image.BICUBIC).convert("RGB")
    p = ImageChops.add(p, Image.blend(grey, fine, 0.10 + 0.12 * w), 1, -128)
    p = ImageChops.add(p, Image.blend(grey, mott, 0.05 + 0.25 * w), 1, -128)
    if kind != "dark":
        edge = Image.radial_gradient("L").resize((W, H), Image.BICUBIC)
        edge = edge.point(lambda x: int(255 * (x / 255) ** 3 * (0.15 + 0.5 * w)))
        tint = (176, 148, 104) if kind == "aged" else (190, 188, 182)
        p = Image.composite(Image.new("RGB", (W, H), tint), p, edge)
    p = p.convert("RGBA")
    if kind == "aged" and w > 0.1:                      # soft stains
        L = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(L)
        for _ in range(int(w * 4) + 1):
            cx, cy, r = rng.randrange(W), rng.randrange(H), rng.randrange(80, 260)
            d.ellipse((cx - r, cy - r * .8, cx + r, cy + r * .8),
                      fill=(150, 108, 52, rng.randrange(10, 26)))
        p = Image.alpha_composite(p, L.filter(ImageFilter.GaussianBlur(50)))
    C = Image.new("RGBA", (W, H), (0, 0, 0, 0))          # fold creases
    d = ImageDraw.Draw(C)
    lite = (255, 255, 255, 60 if kind == "dark" else 95)
    for _ in range(int(w * 3.2)):
        dx = rng.randrange(-40, 40)
        if rng.random() < .5:
            x = rng.randrange(150, W - 150)
            d.line((x, 0, x + dx, H), fill=(70, 50, 25, 70), width=2)
            d.line((x + 3, 0, x + 3 + dx, H), fill=lite, width=2)
        else:
            y = rng.randrange(150, H - 150)
            d.line((0, y, W, y + dx), fill=(70, 50, 25, 70), width=2)
            d.line((0, y + 3, W, y + 3 + dx), fill=lite, width=2)
    p = Image.alpha_composite(p, C.filter(ImageFilter.GaussianBlur(0.9)))
    return p.convert("RGB")


def add_dust(img, wear, rng):
    W, H = img.size
    w = wear / 100
    S = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(S)
    for _ in range(int(40 + 500 * w)):
        x, y, r = rng.randrange(W), rng.randrange(H), rng.choice((1, 1, 1, 2, 2, 3))
        fill = ((40, 30, 20, rng.randrange(60, 160)) if rng.random() < .7
                else (255, 255, 255, rng.randrange(90, 200)))
        d.ellipse((x, y, x + r, y + r), fill=fill)
    for _ in range(int(w * 14)):                         # hairs and scratches
        x, y = rng.randrange(W), rng.randrange(H)
        pts = [(x, y)]
        for _ in range(3):
            x += rng.randrange(-30, 30)
            y += rng.randrange(-30, 30)
            pts.append((x, y))
        d.line(pts, fill=(255, 255, 255, 70), width=1)
    return Image.alpha_composite(img.convert("RGBA"), S).convert("RGB")


def rough_edge(img, wear, rng):
    """Cut the card out of a white sheet; worn cards get uneven, torn edges."""
    W, H = img.size
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).rounded_rectangle((8, 8, W - 9, H - 9), radius=22, fill=255)
    if wear > 4:
        m = m.filter(ImageFilter.GaussianBlur(4))
        n = Image.effect_noise((W, H), 120).filter(ImageFilter.GaussianBlur(1.5))
        m = ImageChops.add(m, n, 1, -128).point(lambda v: 255 if v > 128 else 0)
        m = m.filter(ImageFilter.GaussianBlur(0.8))
    return Image.composite(img, Image.new("RGB", (W, H), (255, 255, 255)), m)


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
    card = make_paper(CARD_W, g["ch"], o["paper"], o["wear"], rng)
    xs = ([MARGIN, MARGIN + SW + COLGAP] if len(strips) == 2 else [(CARD_W - SW) // 2])
    for st, x in zip(strips, xs):
        card.paste(st, (x, TOP))
    text = " \u2013 ".join(t for t in (o["caption"].strip(), o["date"].strip()) if t)
    draw_caption(card, text, o["paper"], rng, TOP + g["sh"] + (g["ch"] - TOP - g["sh"]) / 2)
    card = add_dust(card, o["wear"], rng)
    return rough_edge(card, o["wear"], rng)


def render(photos, o, rng):
    n = o["frames"]
    g = geo(n)
    strips, cuts = [], {}
    for s in range(2):
        idx = [i for i in range(s * 4, s * 4 + n) if i in photos]
        if not idx:
            continue
        seq, k = [(i, 1.0) for i in idx], 0
        while o["repeat"] and len(seq) < n:               # fill from what we have
            seq.append((idx[k % len(idx)], rng.uniform(1.12, 1.32)))
            k += 1
        frames = []
        for i, z in seq:
            if o["bg"]:
                if i not in cuts:
                    cuts[i] = cutout(photos[i])
                f = place(cuts[i], z, rng, g, o)
            else:
                f = make_frame(photos[i], z, rng, g)
            frames.append(finish_frame(apply_film(f, o["film"]), o, rng))
        strips.append(build_strip(frames, o["strip"]))
    if o["layout"] == "card":
        return [(build_card(strips, o, rng, g), CARD_DPI)]
    return [(add_dust(st, o["wear"], rng), g["sdpi"]) for st in strips]


# ---------------------------------------------------------------------- routes
@app.get("/")
def index():
    return PAGE


@app.get("/status")
def status():
    return jsonify(cutout=have_rembg())


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

    o = dict(film=g("film", "bw"), strip=g("strip", "black"), paper=g("paper", "aged"),
             layout=g("layout", "card"), grain=num("grain", 50), vignette=num("vignette", 40),
             wear=num("wear", 55), caption=g("caption", "")[:60], date=g("date", "")[:30],
             repeat=g("repeat") == "1", frames=4 if g("frames") == "4" else 3,
             bg=g("bg") == "1", bg_kind=g("bg_kind", "curtain"), bg_color=g("bg_color", "red"))
    if o["bg"] and not have_rembg():
        return jsonify(error="Background swapping needs rembg/onnxruntime, which isn't available on this server. Turn off background swap to continue."), 400
    try:
        out = []
        for n, (img, dpi) in enumerate(render(photos, o, random.Random(random.randrange(1 << 30))), 1):
            # Return high-quality JPEG data URL directly (stateless for Vercel, stays well under 4.5MB limit)
            jpg_buf = io.BytesIO()
            img.save(jpg_buf, "JPEG", quality=92, dpi=(dpi, dpi))
            jpg_b64 = base64.b64encode(jpg_buf.getvalue()).decode("ascii")

            out.append(dict(
                name=f"photobooth-{n}",
                w=round(img.width / dpi, 2),
                h=round(img.height / dpi, 2),
                px_w=img.width,
                px_h=img.height,
                jpg=f"data:image/jpeg;base64,{jpg_b64}"
            ))
    except Exception as e:
        if o["bg"]:
            return jsonify(error=f"Background swap failed: {e}"), 500
        raise
    return jsonify(prints=out)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Strip booth</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
<style>
:root{--ink:#131a33;--mist:#f2f4f9;--line:#dce1ee;--mute:#5d6685;--cherry:#d8362c;--butter:#ffd966;--curtain:#9e1a22;
 --thumb:grayscale(1) contrast(1.15) brightness(1.05);
 --head:'Bricolage Grotesque','Trebuchet MS',system-ui,sans-serif;--body:'Instrument Sans',system-ui,-apple-system,'Segoe UI',sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--mist);color:var(--ink);font:500 16px/1.5 var(--body)}
.app{display:grid;grid-template-columns:minmax(0,1fr) minmax(400px,42%);min-height:100vh}
.studio{width:100%;max-width:800px;margin-left:auto;padding:44px 52px 0}
h1{margin:0;font:800 clamp(40px,6vw,64px)/.95 var(--head);letter-spacing:-.035em}
.lede{margin:12px 0 34px;color:var(--mute);font-size:18px;max-width:40ch}
.sec{padding:30px 0 34px;border-top:1px solid var(--line)}
h2{margin:0 0 18px;font:700 22px/1.1 var(--head);letter-spacing:-.01em;display:flex;align-items:center;gap:12px}
h2 .n{width:30px;height:30px;border-radius:50%;background:var(--butter);display:grid;place-items:center;font-size:15px}
.hint{color:var(--mute);font-size:14px;margin:10px 0 0}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.row{display:grid;grid-template-columns:132px 1fr;gap:10px 16px;align-items:center;margin-bottom:16px}
.row>label,.row>.l{font-weight:600;font-size:15px}
.opt{position:relative;display:inline-block}
.opt input{position:absolute;inset:0;opacity:0;margin:0;cursor:pointer;width:100%;height:100%}
.pills{display:flex;flex-wrap:wrap;gap:8px}
.pill{display:block;padding:8px 15px;border-radius:999px;border:1px solid var(--line);background:#fff;font-weight:600;font-size:15px}
input:checked+.pill{background:var(--ink);border-color:var(--ink);color:#fff}
input:focus-visible+.pill,input:focus-visible+.film,input:focus-visible+.dot,.slot:focus-visible,.btn:focus-visible,.go:focus-visible{outline:3px solid var(--cherry);outline-offset:2px}
.btn{font:600 15px var(--body);padding:9px 16px;border-radius:999px;border:1px solid var(--ink);background:#fff;color:var(--ink);cursor:pointer;text-decoration:none;display:inline-block}
.btn:hover{background:var(--butter)}
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
.films{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:10px}
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
.warn{margin-top:14px;padding:14px 16px;border-radius:12px;background:#fff3d6;border:1px solid #e8c766;font-size:15px}
.warn code{display:block;margin:8px 0 10px;padding:8px 10px;background:#fff;border-radius:8px;font:500 14px ui-monospace,Consolas,monospace;user-select:all}
.dots{display:flex;gap:10px}
.dot{display:block;width:32px;height:32px;border-radius:50%;border:2px solid #fff;box-shadow:0 0 0 1px var(--line)}
input:checked+.dot{box-shadow:0 0 0 3px var(--ink)}
#bgOpts{margin-top:18px}
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
@media (prefers-reduced-motion:reduce){.prog::after{animation:none;width:100%}.track,.track::after,.stage{transition:none}}
@media (max-width:980px){.app{grid-template-columns:1fr}.studio{padding:30px 20px 0}.stage{position:static;height:auto;min-height:70vh}.row{grid-template-columns:1fr}.slot{width:calc((100vw - 120px)/4)}}
</style></head><body data-frames="4">
<div class="app">
<main class="studio">
 <h1>Strip booth</h1>
 <p class="lede">Turn your photos into photo booth strips you can print.</p>

 <section class="sec">
  <h2><span class="n">1</span>Add your photos</h2>
  <div class="top"><div class="row" style="margin:0;grid-template-columns:auto auto"><span class="l">Frames per strip</span><div class="pills" id="frames"></div></div></div>
  <div class="strips" id="strips"></div>
  <div class="zone" id="zone"><span>Drop photos here. They fill the empty frames in order.</span><button class="btn" id="pick" type="button">Choose photos</button></div>
  <p class="hint">Click a frame to replace its photo, or drag one frame onto another to swap. The second strip is optional.</p>
  <input type="file" id="multi" accept="image/*" multiple hidden><input type="file" id="single" accept="image/*" hidden>
 </section>

 <section class="sec">
  <h2><span class="n">2</span>Choose a film</h2>
  <div class="films" id="films"></div>
 </section>

 <section class="sec">
  <h2><span class="n">3</span>Swap the background</h2>
  <label class="switch"><input type="checkbox" id="bgOn"><span class="track"></span>Put my photos in front of a wall or curtain</label>
  <p class="hint" id="bgHint">Off keeps your original backgrounds. (Requires local server with rembg installed).</p>
  <div class="warn" id="bgWarn" hidden><b>Not available on serverless hosting.</b> Background removal requires <code>rembg</code> and <code>onnxruntime</code> on a local server. Turn off background swap to continue.</div>
  <div id="bgOpts" hidden>
   <div class="row"><span class="l">Backdrop</span><div class="pills" id="bgKind"></div></div>
   <div class="row" id="colorRow"><span class="l">Colour</span><div class="dots" id="bgColor"></div></div>
  </div>
 </section>

 <section class="sec">
  <h2><span class="n">4</span>Finish the print</h2>
  <div class="row"><label for="caption">Caption</label><input type="text" id="caption" value="i luv her so much" maxlength="60"></div>
  <div class="row"><label for="date">Date stamp</label><div class="inline"><input type="text" id="date" maxlength="30"><button class="btn" id="today" type="button">Today</button></div></div>
  <div class="row"><span class="l">Layout</span><div class="pills" id="layout"></div></div>
  <div class="row"><span class="l">Strip colour</span><div class="pills" id="strip"></div></div>
  <div class="row"><span class="l">Card paper</span><div class="pills" id="paper"></div></div>
  <div class="row"><label for="grain">Film grain</label><input type="range" id="grain" min="0" max="100" value="50"></div>
  <div class="row"><label for="vignette">Dark corners</label><input type="range" id="vignette" min="0" max="100" value="40"></div>
  <div class="row"><label for="wear">Paper wear</label><input type="range" id="wear" min="0" max="100" value="55"></div>
  <div class="row"><span class="l">Short strips</span><label class="switch" style="font-weight:500"><input type="checkbox" id="repeat" checked><span class="track"></span>Repeat photos to fill every frame</label></div>
  <p class="hint">Caption, date and paper apply to the card layout. Separate strips come out as plain strips.</p>
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
<script>
const $=s=>document.querySelector(s),$$=s=>document.querySelectorAll(s);
const N=8,slots=Array(N).fill(null);
const FILMS=[['bw','Black and white','Bright flash, soft contrast','#000,#fff','grayscale(1) contrast(1.15) brightness(1.05)'],
['noir','Noir','Deep blacks, hard contrast','#000,#777,#fff','grayscale(1) contrast(1.6) brightness(.9)'],
['sepia','Sepia','Brown tone of an old print','#22140a,#9e7048,#fff0d0','sepia(1) contrast(1.05)'],
['vintage','Faded colour','Washed out, warm shadows','#3a2a1c,#b08a6a,#f3e6cf','saturate(.7) sepia(.3) contrast(1.05)'],
['kodak','Warm colour','Rich colour, golden cast','#4a1d10,#d9822b,#fff1c4','saturate(1.35) sepia(.18) contrast(1.1)'],
['cyano','Cyanotype','Blue print tone','#081a38,#306a98,#e2eef4','grayscale(1) sepia(1) hue-rotate(185deg) saturate(2.2) brightness(.95)'],
['clean','Natural','Colour as shot','#25405a,#6a9a5a,#f1d9a0','none']];
const COLORS={red:['#9e1a22','Red'],navy:['#1d2c66','Navy'],green:['#1f5642','Green'],mustard:['#c4931f','Mustard'],pink:['#d4849a','Pink'],grey:['#6a7078','Grey']};
function pills(id,name,items,sel){$('#'+id).innerHTML=items.map((it,i)=>`<label class="opt"><input type="radio" name="${name}" value="${it[0]}" ${it[0]===sel||(!sel&&!i)?'checked':''}><span class="pill">${it[1]}</span></label>`).join('')}
pills('frames','frames',[['3','3'],['4','4']],'4');
pills('layout','layout',[['card','Both strips on one card'],['strips','Strips on their own']]);
pills('strip','strip',[['black','Black'],['white','White']]);
pills('paper','paper',[['aged','Aged cream'],['clean','Clean white'],['dark','Black card']]);
pills('bgKind','bg_kind',[['curtain','Velvet curtain'],['wall','Painted wall'],['brick','Brick'],['stripes','Retro stripes'],['studio','Studio paper']]);
$('#bgColor').innerHTML=Object.entries(COLORS).map(([k,v],i)=>`<label class="opt"><input type="radio" name="bg_color" value="${k}" ${i?'':'checked'}><span class="dot" title="${v[1]}" style="background:${v[0]}"></span></label>`).join('');
$('#films').innerHTML=FILMS.map((f,i)=>`<label class="opt" title="${f[2]}"><input type="radio" name="film" value="${f[0]}" ${i?'':'checked'}><span class="film"><span class="sw" style="background:linear-gradient(90deg,${f[3]})"></span>${f[1]}</span></label>`).join('');
const val=n=>document.querySelector(`input[name=${n}]:checked`).value;
const frames=()=>+val('frames'),visible=i=>(i%4)<frames();
function today(){const d=new Date();$('#date').value=d.toLocaleString('en',{month:'short'})+' '+d.getDate()+" '"+String(d.getFullYear()).slice(2)}
today();$('#today').onclick=today;

let pending=null,canCut=false;
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

$('#frames').onchange=()=>{document.body.dataset.frames=frames()};
$('#films').onchange=()=>{const f=FILMS.find(x=>x[0]===val('film'));document.documentElement.style.setProperty('--thumb',f[4])};
function stageColor(){const on=$('#bgOn').checked;document.documentElement.style.setProperty('--curtain',on?COLORS[val('bg_color')][0]:COLORS.red[0])}
function bgUI(){$('#bgOpts').hidden=!$('#bgOn').checked;$('#colorRow').hidden=val('bg_kind')==='brick';stageColor()}
$('#bgOn').onchange=bgUI;$('#bgOpts').onchange=bgUI;
async function checkCut(){try{canCut=(await (await fetch('/status')).json()).cutout}catch(e){canCut=false}
 $('#bgWarn').hidden=canCut||!$('#bgOn').checked;return canCut}
$('#bgOn').addEventListener('change',()=>{if($('#bgOn').checked)checkCut();else $('#bgWarn').hidden=true});

// Compress images client-side before upload so 8 photos stay safely under Vercel's 4.5MB limit
function compressImage(file, maxDim=1400, quality=0.84){
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
 if(!slots.some((s,i)=>s&&visible(i))){msg.textContent='Add at least one photo first.';return}
 const bg=$('#bgOn').checked,fd=new FormData();
 if(bg&&!await checkCut()){msg.textContent='Background swap is not available on this server. Turn it off to continue.';return}
 const go=$('#go');go.disabled=true;go.textContent='Developing…';
 $('#out').innerHTML='<p class="dev">'+(bg?'Cutting out backgrounds and developing…':'Developing your prints…')+'</p><div class="prog"></div>';
 if(innerWidth<=980)$('.stage').scrollIntoView({behavior:'smooth'});

 for(let i=0;i<slots.length;i++){
  if(slots[i]&&visible(i)){
   const compressed=await compressImage(slots[i].file);
   fd.append('slot'+i,compressed,'photo'+i+'.jpg');
  }
 }
 ['film','layout','strip','paper','frames','bg_kind','bg_color'].forEach(n=>fd.append(n,val(n)));
 ['caption','date','grain','vignette','wear'].forEach(n=>fd.append(n,$('#'+n).value));
 fd.append('repeat',$('#repeat').checked?'1':'0');fd.append('bg',bg?'1':'0');
 try{
  const r=await fetch('/generate',{method:'POST',body:fd});
  if(r.status===413)throw new Error('Photos are too large even after compression. Try fewer photos.');
  const j=await r.json();
  if(!r.ok)throw new Error(j.error||'Something went wrong.');
  show(j.prints);
 }catch(err){$('#out').innerHTML='<div class="ghost"></div><p class="empty">Nothing was printed.</p>';msg.textContent=err.message}
 go.disabled=false;go.textContent='Develop prints';
};

// Client-side PNG, PDF, and Print helpers (stateless, no server storage needed)
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

function show(prints){
 const multi=prints.length>1;
 $('#out').innerHTML=`<div class="tools"><button class="btn" id="again" type="button">Develop again</button>
  <label>Print size <select id="fit"><option value="0">Actual size</option><option value="1">Fit to page</option></select></label></div>`+
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
"""


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
