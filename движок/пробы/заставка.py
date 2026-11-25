# -*- coding: utf-8 -*-
"""
Prototype: end card ("заставка") for every vertical clip.

Renders several outro designs (Twitch glitch logo + streamer nick, with
a synthesized closing sound) and appends each one to a finished clip, so
the user can compare them.

  python заставка.py <clip.mp4> <out_dir> [--only 5,6] [--dur 2.5]

Variants:
  1  neon    - blurred last frame, logo pops in with a purple glow, nick below
  2  face    - the streamer's screaming face slams in as a round sticker
  3  purple  - Twitch-purple wipe, white logo, nick typed letter by letter
  4  freeze  - last frame freezes and greys out, purple band slides in
  5  meme    - tongue-out photo slams in full width, meme caption below
  6  comic   - pop-art duotone photo in a comic panel, caption box slides in
  7  polaroid- a polaroid of the smiling streamer drops in after a flash
  8  booth   - photo-booth strip, three faces flash in one by one
  9  glitch  - screaming photo with RGB split and slice glitches

Default --only is 5..9 (the photo variants). Photos are small webcam crops
from the raw clips, so full-width variants look soft by nature.
"""
import argparse
import math
import re
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
sys.path.insert(0, str(ENGINE))
import пути  # noqa: E402  must come first: keeps temp files and caches in .cache

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps  # noqa: E402
import config as C  # noqa: E402

W, H, FPS, SR = C.OUT_W, C.OUT_H, C.OUT_FPS, 48000
FONTS = ENGINE / "fonts"
ASSETS = ENGINE / "заставка"
PURPLE = (145, 70, 255)
NICK = C.STREAMER_NICK
if not NICK:
    raise SystemExit("STREAMER_NICK is not set in мои_настройки.py")
# Display fonts (Bungee, Rubik Mono One) have Latin glyphs only.
if not re.fullmatch(r"[A-Za-z0-9_]+", NICK):
    raise SystemExit("outro fonts support Latin nicknames only")
URL = f"twitch.tv/{NICK.lower()}"


# --------------------------------------------------------------------------
# Easing
# --------------------------------------------------------------------------
def clamp01(x):
    return max(0.0, min(1.0, x))


def ease_out_back(x, s=1.70158):
    x = clamp01(x) - 1
    return 1 + (s + 1) * x ** 3 + s * x ** 2


def ease_out_cubic(x):
    return 1 - (1 - clamp01(x)) ** 3


def ease_in_out(x):
    x = clamp01(x)
    return x * x * (3 - 2 * x)


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------
def glitch_logo(size, body=PURPLE + (255,), face=(255, 255, 255, 255)):
    """Twitch glitch icon as an RGBA image, drawn 4x and downscaled."""
    s = 4
    big = size * s
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = big / 24

    def shape(pts):
        return [(x * k, y * k) for x, y in pts]

    d.polygon(shape([(6, 0), (1.714, 4.286), (1.714, 19.714), (6.857, 19.714),
                     (6.857, 24), (11.143, 19.714), (14.571, 19.714),
                     (22.286, 12), (22.286, 0)]), fill=body)
    d.polygon(shape([(6.857, 1.714), (20.571, 1.714), (20.571, 11.143),
                     (17.143, 14.571), (13.714, 14.571), (10.714, 17.571),
                     (10.714, 14.571), (6.857, 14.571)]), fill=face)
    for x0, x1 in ((11.571, 13.286), (16.286, 18.0)):
        d.polygon(shape([(x0, 4.714), (x1, 4.714), (x1, 9.857), (x0, 9.857)]),
                  fill=body)
    return img.resize((size, size), Image.LANCZOS)


def text_layer(text, font_file, size, fill, stroke=0, stroke_fill=(0, 0, 0)):
    """Text rendered tight into its own RGBA image."""
    font = ImageFont.truetype(str(FONTS / font_file), size)
    l, t, r, b = font.getbbox(text, stroke_width=stroke)
    img = Image.new("RGBA", (r - l + 8, b - t + 8), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((4 - l, 4 - t), text, font=font, fill=fill,
                             stroke_width=stroke, stroke_fill=stroke_fill)
    return img


def fit_text(text, font_file, max_w, start, fill, **kw):
    """Largest text layer not wider than max_w."""
    size = start
    while size > 20:
        img = text_layer(text, font_file, size, fill, **kw)
        if img.width <= max_w:
            return img
        size -= 4
    return img


def glow(img, radius, color, strength=2):
    """Coloured blurred halo behind img (same size, with padding)."""
    pad = radius * 3
    base = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
    alpha = Image.new("L", base.size, 0)
    alpha.paste(img.getchannel("A"), (pad, pad))
    halo = Image.new("RGBA", base.size, color + (0,))
    halo.putalpha(alpha.filter(ImageFilter.GaussianBlur(radius)))
    for _ in range(strength):
        base = Image.alpha_composite(base, halo)
    base.alpha_composite(img, (pad, pad))
    return base


def scaled(img, k):
    k = max(k, 0.01)
    return img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))),
                      Image.LANCZOS)


def put(canvas, img, cx, cy, alpha=1.0):
    """Paste img centred at (cx, cy) with overall opacity."""
    if alpha <= 0:
        return
    if alpha < 1:
        img = img.copy()
        img.putalpha(img.getchannel("A").point(lambda v: int(v * alpha)))
    canvas.alpha_composite(img, (int(cx - img.width / 2), int(cy - img.height / 2)))


def round_sticker(path, diameter, border=14):
    """Circle-cropped photo with a white rim, like a sticker."""
    src = Image.open(path).convert("RGBA")
    side = min(src.size)
    # The face sits left of centre in the saved crop.
    cx, cy = int(src.width * 0.41), int(src.height * 0.5)
    box = (max(0, cx - side // 2), max(0, cy - side // 2))
    src = src.crop((box[0], box[1], box[0] + side, box[1] + side))
    src = src.resize((diameter, diameter), Image.LANCZOS)
    # The source frame is small (webcam crop); sharpen after upscaling.
    src = ImageOps.autocontrast(src.convert("RGB"), cutoff=1)
    src = src.filter(ImageFilter.UnsharpMask(radius=3, percent=120, threshold=2))
    src = src.convert("RGBA")
    mask = Image.new("L", (diameter * 4, diameter * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter * 4 - 1, diameter * 4 - 1), fill=255)
    src.putalpha(mask.resize((diameter, diameter), Image.LANCZOS))
    full = diameter + 2 * border
    out = Image.new("RGBA", (full, full), (0, 0, 0, 0))
    rim = Image.new("L", (full * 4, full * 4), 0)
    ImageDraw.Draw(rim).ellipse((0, 0, full * 4 - 1, full * 4 - 1), fill=255)
    out.paste((255, 255, 255, 255), (0, 0), rim.resize((full, full), Image.LANCZOS))
    out.alpha_composite(src, (border, border))
    return out


def last_frame(clip):
    """Final frame of the clip as an RGB image (0.05 s before the end)."""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-sseof", "-0.05", "-i", str(clip), "-frames:v", "1",
         "-vf", f"scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True)
    if r.returncode != 0 or len(r.stdout) < W * H * 3:
        raise SystemExit("could not read the last frame of the clip:\n"
                         + r.stderr.decode("utf-8", "replace"))
    return Image.frombytes("RGB", (W, H), r.stdout[:W * H * 3])


def has_audio(clip):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                          "-show_entries", "stream=index", "-of", "csv=p=0",
                          str(clip)], capture_output=True, text=True).stdout
    return bool(out.strip())


# --------------------------------------------------------------------------
# Variants: each returns a function t -> RGB frame
# --------------------------------------------------------------------------
def v_neon(bg, dur):
    back = bg.filter(ImageFilter.GaussianBlur(30))
    back = Image.blend(back, Image.new("RGB", back.size, (10, 6, 20)), 0.6).convert("RGBA")
    logo = glow(glitch_logo(420), 40, PURPLE, 3)
    nick = glow(fit_text(NICK.upper(), "Bungee-Regular.ttf", W - 120, 150,
                         (255, 255, 255, 255)), 22, PURPLE, 2)
    url = text_layer(URL, "Montserrat-ExtraBold.ttf", 52, (200, 175, 255, 255))

    def frame(t):
        c = back.copy()
        put(c, scaled(logo, ease_out_back(t / 0.45) * (1 + 0.02 * math.sin(t * 6))),
            W / 2, 760)
        k = ease_out_cubic((t - 0.35) / 0.4)
        put(c, nick, W / 2, 1180 + 60 * (1 - k), alpha=k)
        k2 = ease_out_cubic((t - 0.6) / 0.4)
        put(c, url, W / 2, 1330, alpha=k2)
        return c
    return frame


def v_face(bg, dur):
    back = bg.filter(ImageFilter.GaussianBlur(24))
    back = Image.blend(back, Image.new("RGB", back.size, (0, 0, 0)), 0.55).convert("RGBA")
    face = round_sticker(ASSETS / "лицо_ор.png", 640)
    logo = glitch_logo(150)
    nick = fit_text(NICK.upper(), "Bungee-Regular.ttf", W - 300, 130,
                    (255, 255, 255, 255), stroke=6, stroke_fill=(0, 0, 0))
    rng = np.random.default_rng(7)
    shake = rng.uniform(-1, 1, size=(200, 2))

    def frame(t):
        c = back.copy()
        # Slam: comes in huge and lands with a short camera shake.
        k = ease_out_cubic(t / 0.22)
        sc = 2.4 - 1.4 * k
        sh = 26 * max(0.0, 1 - (t - 0.22) / 0.35) if t > 0.22 else 0
        i = int(t * FPS) % len(shake)
        put(c, scaled(face, sc), W / 2 + shake[i][0] * sh, 760 + shake[i][1] * sh,
            alpha=clamp01(t / 0.08))
        k2 = ease_out_back((t - 0.45) / 0.4)
        row_w = logo.width + 24 + nick.width
        x0 = W / 2 - row_w / 2
        if t > 0.45:
            put(c, scaled(logo, k2), x0 + logo.width / 2, 1300)
            put(c, scaled(nick, k2), x0 + logo.width + 24 + nick.width / 2, 1300)
        return c
    return frame


def v_purple(bg, dur):
    back = bg.convert("RGBA")
    panel = Image.new("RGBA", (W, H), PURPLE + (255,))
    logo = glitch_logo(380, body=(255, 255, 255, 255), face=PURPLE + (255,))
    font_file = "RubikMonoOne-Regular.ttf"
    full = fit_text(NICK.upper(), font_file, W - 120, 110, (255, 255, 255, 255))
    size = full.height  # keep letters the same size while typing
    url = text_layer(URL, "Montserrat-ExtraBold.ttf", 50, (235, 225, 255, 255))
    # Typing reveals the rendered word proportionally per letter: this is
    # exact only for a monospaced font such as Rubik Mono One.
    t_type0, per = 0.55, 0.07

    def frame(t):
        c = back.copy()
        wipe = ease_in_out(t / 0.35)
        top = int(H * (1 - wipe))
        c.alpha_composite(panel.crop((0, 0, W, H - top)), (0, top))
        if wipe >= 1:
            put(c, scaled(logo, ease_out_back((t - 0.3) / 0.35)), W / 2, 780)
            n = int(clamp01((t - t_type0) / (per * len(NICK))) * len(NICK) + 1e-6)
            if n:
                part = full.crop((0, 0, int(full.width * n / len(NICK)) + 4, size))
                c.alpha_composite(part, (int(W / 2 - full.width / 2), int(1180 - size / 2)))
            put(c, url, W / 2, 1320, alpha=ease_out_cubic((t - 1.4) / 0.3))
        return c
    return frame


def v_freeze(bg, dur):
    grey = ImageOps.grayscale(bg).convert("RGB")
    band_h = 520
    logo = glitch_logo(220, body=(255, 255, 255, 255), face=PURPLE + (255,))
    nick = fit_text(NICK.upper(), "Bungee-Regular.ttf", W - 380, 120,
                    (255, 255, 255, 255))
    url = text_layer(URL, "Montserrat-ExtraBold.ttf", 46, (225, 210, 255, 255))

    def frame(t):
        g = ease_in_out(t / 0.4)
        img = Image.blend(bg, grey, g)
        z = 1 + 0.06 * ease_in_out(t / dur)          # slow push-in on the freeze
        zw, zh = int(W * z), int(H * z)
        img = img.resize((zw, zh), Image.LANCZOS).crop(
            ((zw - W) // 2, (zh - H) // 2, (zw - W) // 2 + W, (zh - H) // 2 + H))
        img = Image.blend(img, Image.new("RGB", (W, H), (0, 0, 0)), 0.35 * g)
        c = img.convert("RGBA")
        k = ease_out_cubic((t - 0.25) / 0.35)
        x = int(-W + W * k)
        y0 = (H - band_h) // 2
        band = Image.new("RGBA", (W, band_h), PURPLE + (240,))
        c.alpha_composite(band, (x, y0))
        if k > 0:
            put(c, logo, x + 60 + logo.width / 2, y0 + band_h / 2 - 50)
            put(c, nick, x + 60 + logo.width + 40 + nick.width / 2, y0 + band_h / 2 - 50)
            put(c, url, x + W / 2, y0 + band_h - 80)
        return c
    return frame


def photo(name, w, h, cx=0.53, cy=0.45, zoom=1.0):
    """Webcam still cropped to w:h around (cx, cy), scaled to w x h, sharpened."""
    src = Image.open(ASSETS / name).convert("RGB")
    src = src.crop((4, 6, src.width - 4, src.height - 2))   # drop HUD edge pixels
    ar = w / h
    ch = src.height / zoom
    cw = ch * ar
    if cw > src.width / zoom:
        cw = src.width / zoom
        ch = cw / ar
    x0 = min(max(0, src.width * cx - cw / 2), src.width - cw)
    y0 = min(max(0, src.height * cy - ch / 2), src.height - ch)
    img = src.crop((int(x0), int(y0), int(x0 + cw), int(y0 + ch))).resize((w, h), Image.LANCZOS)
    return img.filter(ImageFilter.UnsharpMask(radius=4, percent=110, threshold=2))


def shadowed(img, radius=18, offset=(0, 14), opacity=150):
    """img with a soft drop shadow (returned canvas is padded)."""
    pad = max(radius * 3, abs(offset[0]) + 4, abs(offset[1]) + 4)
    out = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
    sh = Image.new("L", out.size, 0)
    sh.paste(img.getchannel("A").point(lambda v: v * opacity // 255),
             (pad + offset[0], pad + offset[1]))
    shadow = Image.new("RGBA", out.size, (0, 0, 0, 255))
    shadow.putalpha(sh.filter(ImageFilter.GaussianBlur(radius)) if radius else sh)
    out.alpha_composite(shadow)
    out.alpha_composite(img, (pad, pad))
    return out


def top_photo(name, h, cx, cy=0.42, fade=320):
    """Photo across the top h pixels of a black canvas, fading out at its bottom."""
    pic = photo(name, W, h, cx=cx, cy=cy)
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    m = Image.new("L", (W, h), 255)
    d = ImageDraw.Draw(m)
    for i in range(fade):
        d.line((0, h - fade + i, W, h - fade + i), fill=int(255 * (1 - i / fade)))
    canvas.paste(pic, (0, 0), m)
    return canvas


def v_meme(bg, dur):
    """Full-screen freeze of the tongue-out face, meme caption at the bottom."""
    base = top_photo("язык.png", 1400, cx=0.52)
    vign = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vign).ellipse((-300, -200, W + 300, H + 200), fill=255)
    vign = vign.filter(ImageFilter.GaussianBlur(160))
    base = Image.composite(base, Image.new("RGB", (W, H), (0, 0, 0)), vign).convert("RGBA")
    cap = fit_text(NICK.upper(), "Anton-Regular.ttf", W - 120, 230, (255, 255, 255, 255),
                   stroke=12, stroke_fill=(0, 0, 0))
    logo = glitch_logo(170)
    url = text_layer(URL, "Anton-Regular.ttf", 64, (255, 255, 255, 255), stroke=5)
    shake = np.random.default_rng(11).uniform(-1, 1, size=(200, 2))

    def frame(t):
        k = ease_out_cubic(t / 0.18)
        z = 1.35 - 0.35 * k + 0.04 * ease_in_out((t - 0.2) / (dur - 0.2))
        s = 30 * max(0.0, 1 - (t - 0.18) / 0.4) if t > 0.18 else 0
        i = int(t * FPS) % len(shake)
        c = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        put(c, scaled(base, z), W / 2 + shake[i][0] * s, H / 2 + shake[i][1] * s)
        if t > 0.4:
            k2 = ease_out_back((t - 0.4) / 0.35)
            put(c, scaled(logo, k2), W / 2, 1390)
            put(c, scaled(cap, k2), W / 2, 1580)
            put(c, url, W / 2, 1740, alpha=ease_out_cubic((t - 0.8) / 0.3))
        return c
    return frame


def duotone(img, dark, light, levels=5):
    """Posterized two-colour version of a photo (pop-art look)."""
    g = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=2)
    g = g.point(lambda v: int(round(v / 255 * (levels - 1)) * 255 / (levels - 1)))
    return ImageOps.colorize(g, dark, light)


def halftone_mask(w, h, step=18):
    """Grid of dots, used to print a comic-style dot texture."""
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    for y in range(0, h, step):
        off = step // 2 if (y // step) % 2 else 0
        for x in range(-step, w + step, step):
            d.ellipse((x + off - 5, y - 5, x + off + 5, y + 5), fill=255)
    return m


def v_popart(bg, dur):
    """Comic panel: duotone wild-eyed face, yellow burst, nick in a caption box."""
    panel_w, panel_h = 900, 1000
    ph = duotone(photo("глаза.png", panel_w, panel_h, cx=0.55, cy=0.42),
                 (40, 10, 90), (255, 220, 60))
    dots = Image.new("RGB", ph.size, (230, 40, 120))
    ph = Image.composite(dots, ph, halftone_mask(*ph.size).point(lambda v: v // 3))
    panel = Image.new("RGBA", (panel_w + 24, panel_h + 24), (0, 0, 0, 255))
    panel.paste(ph, (12, 12))
    panel = shadowed(panel.rotate(-4, expand=True, resample=Image.BICUBIC), 0, (22, 22), 255)
    back = Image.new("RGBA", (W, H), (255, 214, 0, 255))
    d = ImageDraw.Draw(back)
    for a in range(0, 360, 12):            # radial comic burst
        r1, r2 = math.radians(a), math.radians(a + 6)
        d.polygon([(W / 2, 760), (W / 2 + 2000 * math.cos(r1), 760 + 2000 * math.sin(r1)),
                   (W / 2 + 2000 * math.cos(r2), 760 + 2000 * math.sin(r2))],
                  fill=(255, 170, 0, 255))
    box_txt = fit_text(NICK.upper(), "Bangers-Regular.ttf", W - 360, 170, (0, 0, 0, 255))
    box = Image.new("RGBA", (box_txt.width + box_txt.height + 120, box_txt.height + 60),
                    (255, 255, 255, 255))
    ImageDraw.Draw(box).rectangle((0, 0, box.width - 1, box.height - 1),
                                  outline=(0, 0, 0), width=10)
    box.alpha_composite(glitch_logo(box_txt.height), (40, 30))
    box.alpha_composite(box_txt, (box.width - box_txt.width - 40, 30))
    box = shadowed(box.rotate(3, expand=True, resample=Image.BICUBIC), 0, (16, 16), 255)
    url = text_layer(URL, "Bangers-Regular.ttf", 70, (0, 0, 0, 255))

    def frame(t):
        c = back.copy()
        k = ease_out_back(t / 0.4, 2.2)
        put(c, scaled(panel, 0.2 + 0.8 * k), W / 2, 760)
        if t > 0.45:
            put(c, box, W / 2 + 900 * (1 - ease_out_cubic((t - 0.45) / 0.3)), 1430)
        put(c, url, W / 2, 1640, alpha=ease_out_cubic((t - 0.9) / 0.3))
        return c
    return frame


def polaroid(img, border=34, bottom=230):
    card = Image.new("RGBA", (img.width + 2 * border, img.height + border + bottom),
                     (250, 248, 242, 255))
    card.paste(img, (border, border))
    return card


def v_polaroid(bg, dur):
    """A polaroid of the smiling streamer drops onto a blurred clip frame."""
    back = bg.filter(ImageFilter.GaussianBlur(26))
    back = Image.blend(back, Image.new("RGB", back.size, (25, 10, 45)), 0.55).convert("RGBA")
    card = polaroid(photo("улыбка.png", 800, 800, cx=0.5, cy=0.42))
    hand = fit_text(NICK, "PermanentMarker-Regular.ttf", card.width - 220, 120,
                    (30, 30, 40, 255))
    logo = glitch_logo(hand.height + 10)
    row = logo.width + 20 + hand.width
    y = card.height - 230 + (230 - logo.height) // 2
    card.alpha_composite(logo, ((card.width - row) // 2, y))
    card.alpha_composite(hand, ((card.width - row) // 2 + logo.width + 20,
                                card.height - 230 + (230 - hand.height) // 2))
    card = shadowed(card, 30, (0, 24), 170)
    url = text_layer(URL, "PermanentMarker-Regular.ttf", 62, (255, 255, 255, 255))
    flash = Image.new("RGBA", (W, H), (255, 255, 255, 255))

    def frame(t):
        c = back.copy()
        k = ease_out_cubic(t / 0.45)
        rot = card.rotate(-14 + 10 * k, expand=True, resample=Image.BICUBIC)  # lands at -4
        put(c, scaled(rot, 1.4 - 0.4 * k), W / 2, 880 - 500 * (1 - k))
        put(c, url, W / 2, 1640, alpha=ease_out_cubic((t - 0.8) / 0.3))
        if t < 0.18:                           # camera flash
            put(c, flash, W / 2, H / 2, alpha=1 - t / 0.18)
        return c
    return frame


def v_booth(bg, dur):
    """Photo-booth strip: three faces flash in one by one."""
    # (file, horizontal face position as a fraction of the photo width)
    names = [("гримаса.png", 0.55), ("крик.png", 0.55), ("рожки.png", 0.5)]
    fw, fh = 760, 470
    shots = [photo(n, fw, fh, cx=cx, cy=0.42) for n, cx in names]
    pad, gap, foot = 30, 22, 170
    strip = Image.new("RGBA", (fw + 2 * pad, 3 * fh + 2 * gap + pad + foot), (20, 20, 22, 255))
    nick = fit_text(NICK.upper(), "Bungee-Regular.ttf", fw - 120, 80, (255, 255, 255, 255))
    logo = glitch_logo(nick.height + 6)
    back = bg.filter(ImageFilter.GaussianBlur(26))
    back = Image.blend(back, Image.new("RGB", back.size, PURPLE), 0.55).convert("RGBA")
    url = text_layer(URL, "Montserrat-ExtraBold.ttf", 50, (255, 255, 255, 255))
    t_shot = [0.35, 0.75, 1.15]

    def frame(t):
        c = back.copy()
        s = strip.copy()
        for i, (shot, t0) in enumerate(zip(shots, t_shot)):
            y = pad + i * (fh + gap)
            if t >= t0:
                s.paste(shot, (pad, y))
                fl = 1 - clamp01((t - t0) / 0.15)       # white flash per shot
                if fl > 0:
                    s.alpha_composite(Image.new("RGBA", (fw, fh), (255, 255, 255, int(255 * fl))),
                                      (pad, y))
        if t >= 1.4:
            fy = s.height - foot + (foot - nick.height) // 2
            row = logo.width + 18 + nick.width
            s.alpha_composite(logo, ((s.width - row) // 2, fy - 2))
            s.alpha_composite(nick, ((s.width - row) // 2 + logo.width + 18, fy))
        k = ease_out_cubic(t / 0.35)
        rot = shadowed(s, 24, (0, 20), 160).rotate(3, expand=True, resample=Image.BICUBIC)
        put(c, rot, W / 2, 900 + 1200 * (1 - k))
        put(c, url, W / 2, 1870, alpha=ease_out_cubic((t - 1.5) / 0.3))
        return c
    return frame


def v_glitch(bg, dur):
    """Screaming face with RGB split and sliced glitches, then the nick settles."""
    base = top_photo("крик.png", 1400, cx=0.55)
    base = Image.blend(base, Image.new("RGB", (W, H), (20, 0, 40)), 0.25)
    r, g, b = base.split()
    lines = Image.new("L", (W, H), 0)
    dl = ImageDraw.Draw(lines)
    for y in range(0, H, 6):
        dl.line((0, y, W, y), fill=60)
    black = Image.new("RGB", (W, H), (0, 0, 0))
    logo = glow(glitch_logo(220), 30, PURPLE, 2)
    nick = glow(fit_text(NICK.upper(), "RubikMonoOne-Regular.ttf", W - 120, 120,
                         (255, 255, 255, 255)), 18, PURPLE, 2)
    url = text_layer(URL, "Montserrat-ExtraBold.ttf", 52, (230, 220, 255, 255))
    seeds = np.random.default_rng(5).integers(0, 10 ** 6, size=200)

    def slices(img, amt, seed):
        """Shift random horizontal slices of img sideways."""
        rr = np.random.default_rng(seed)
        a = np.asarray(img).copy()
        for _ in range(int(6 + 10 * amt)):
            y = int(rr.integers(0, a.shape[0] - 60))
            hgt = int(rr.integers(8, 60))
            a[y:y + hgt] = np.roll(a[y:y + hgt], int(rr.integers(-80, 80) * amt), axis=1)
        return Image.fromarray(a)

    def frame(t):
        i = int(t * FPS)
        seed = int(seeds[i % len(seeds)])
        # Strong glitch at the start, short bursts later.
        burst = 1.0 if t < 0.35 else (0.8 if 0.9 < t < 1.0 or 1.6 < t < 1.68 else 0.0)
        sh = int(4 + 22 * burst)
        img = Image.merge("RGB", (r.transform((W, H), Image.AFFINE, (1, 0, sh, 0, 1, 0)), g,
                                  b.transform((W, H), Image.AFFINE, (1, 0, -sh, 0, 1, 0))))
        if burst:
            img = slices(img, burst, seed)
        img = Image.composite(black, img, lines)
        z = 1.0 + 0.05 * ease_in_out(t / dur)
        c = scaled(img.convert("RGBA"), z)
        c = c.crop(((c.width - W) // 2, (c.height - H) // 2,
                    (c.width - W) // 2 + W, (c.height - H) // 2 + H))
        if t > 0.35:
            jx = int(np.random.default_rng(seed).integers(-14, 15)) if burst else 0
            if t > 0.45 or i % 2:
                put(c, logo, W / 2 + jx, 1330)
            if t > 0.55 or i % 2:
                put(c, nick, W / 2 - jx, 1560)
            put(c, url, W / 2, 1700, alpha=ease_out_cubic((t - 0.8) / 0.3))
        return c
    return frame


VARIANTS = {1: ("неон", v_neon), 2: ("ор стримера", v_face),
            3: ("фиолетовый экран", v_purple), 4: ("стоп-кадр", v_freeze),
            5: ("мем-стоп-кадр", v_meme), 6: ("комикс", v_popart),
            7: ("полароид", v_polaroid), 8: ("фотобудка", v_booth),
            9: ("глитч", v_glitch)}


# --------------------------------------------------------------------------
# Sound: synthesized, so there are no rights issues
# --------------------------------------------------------------------------
def env(n, attack, decay):
    t = np.arange(n) / SR
    return np.minimum(1, t / max(attack, 1e-4)) * np.exp(-t / decay)


def tone(freq, dur, attack=0.005, decay=0.3, amp=1.0):
    n = int(dur * SR)
    t = np.arange(n) / SR
    return amp * np.sin(2 * np.pi * freq * t) * env(n, attack, decay)


def thump(dur=0.6):
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 40 + 70 * np.exp(-t / 0.05)                  # pitch drops: kick drum
    ph = 2 * np.pi * np.cumsum(f) / SR
    return np.sin(ph) * env(n, 0.002, 0.18)


def lowpassed_noise(n, cutoff, rng):
    """White noise through a one-pole low-pass with a time-varying cutoff."""
    x = rng.standard_normal(n)
    a = np.exp(-2 * np.pi * np.asarray(cutoff) / SR) * np.ones(n)
    y = np.empty(n)
    acc = 0.0
    for i in range(n):
        acc = a[i] * acc + (1 - a[i]) * x[i]
        y[i] = acc
    return y / (np.abs(y).max() + 1e-9)


def whoosh(dur=0.5, rng=None):
    rng = rng or np.random.default_rng(1)
    n = int(dur * SR)
    t = np.arange(n) / SR
    cut = 300 + 5000 * np.sin(np.pi * t / dur) ** 2
    shape = np.sin(np.pi * np.clip(t / dur, 0, 1)) ** 2
    return lowpassed_noise(n, cut, rng) * shape


def chime(notes, gap=0.08, decay=0.6):
    out = np.zeros(int((gap * len(notes) + decay * 3) * SR))
    for i, f in enumerate(notes):
        s = tone(f, decay * 3, decay=decay) + 0.35 * tone(f * 2, decay * 3, decay=decay / 2)
        a = int(i * gap * SR)
        out[a:a + len(s)] += s[:len(out) - a]
    return out


def tick(rng):
    n = int(0.012 * SR)
    return rng.standard_normal(n) * env(n, 0.0005, 0.003)


def boing(dur=0.7):
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 160 + 140 * np.exp(-t / 0.25) + 25 * np.sin(2 * np.pi * 14 * t) * np.exp(-t / 0.3)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, 0.003, 0.25)


def scratch(dur=0.35, rng=None):
    rng = rng or np.random.default_rng(3)
    n = int(dur * SR)
    t = np.arange(n) / SR
    cut = 800 + 2500 * np.abs(np.sin(2 * np.pi * 9 * t))
    return lowpassed_noise(n, cut, rng) * np.abs(np.sin(2 * np.pi * 9 * t)) * env(n, 0.003, 0.2)


def boom(dur=1.4):
    """Deep distorted boom with a long tail (meme-style hit)."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 45 + 90 * np.exp(-t / 0.04)
    x = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, 0.002, 0.45)
    return np.tanh(3 * x)


def shutter(rng):
    """Camera shutter: two short filtered clicks."""
    n = int(0.12 * SR)
    out = np.zeros(n)
    m = int(0.03 * SR)
    for at in (0.0, 0.055):
        a = int(at * SR)
        out[a:a + m] += lowpassed_noise(m, 3500, rng) * env(m, 0.0005, 0.008)
    return out


def pow_hit(rng):
    n = int(0.25 * SR)
    return 0.7 * thump(0.25) + 0.5 * lowpassed_noise(n, 2500, rng) * env(n, 0.001, 0.04)


def glitch_noise(dur, rng):
    """Digital glitch: chopped square tones and crushed noise."""
    n = int(dur * SR)
    out = np.zeros(n)
    pos = 0
    while pos < n:
        m = int(rng.uniform(0.015, 0.06) * SR)
        t = np.arange(m) / SR
        if rng.random() < 0.5:
            seg = np.sign(np.sin(2 * np.pi * rng.uniform(150, 1800) * t))
        else:
            seg = np.round(rng.standard_normal(m) * 2) / 2
        seg = seg[:n - pos]
        out[pos:pos + len(seg)] = seg * rng.uniform(0.3, 1.0)
        pos += m + int(rng.uniform(0, 0.02) * SR)
    return out * 0.6


def mix(dur, parts):
    out = np.zeros(int(dur * SR))
    for at, sig, gain in parts:
        a = int(at * SR)
        if a >= len(out):
            continue
        seg = sig[:len(out) - a]
        out[a:a + len(seg)] += gain * seg
    # Gentle fade at the very end so the card does not click off.
    f = int(0.08 * SR)
    out[-f:] *= np.linspace(1, 0, f)
    peak = np.abs(out).max()
    return out / peak * 0.6 if peak > 0 else out


def sound_for(v, dur):
    rng = np.random.default_rng(v)
    if v == 5:
        return mix(dur, [(0.0, whoosh(0.2, rng), 0.4), (0.15, boom(), 1.0)])
    if v == 6:
        return mix(dur, [(0.0, whoosh(0.35, rng), 0.45), (0.3, pow_hit(rng), 1.0),
                         (0.45, whoosh(0.3, rng), 0.35), (0.7, pow_hit(rng), 0.7),
                         (0.75, chime([659, 784, 1047]), 0.3)])
    if v == 7:
        return mix(dur, [(0.0, shutter(rng), 0.9), (0.05, tone(2400, 0.4, decay=0.15), 0.08),
                         (0.4, thump(0.4), 0.5), (0.8, chime([784, 1175]), 0.25)])
    if v == 8:
        parts = [(0.0, whoosh(0.35, rng), 0.4)]
        parts += [(t, shutter(rng), 0.9) for t in (0.33, 0.73, 1.13)]
        parts.append((1.4, chime([523, 659, 784, 1047]), 0.3))
        return mix(dur, parts)
    if v == 9:
        return mix(dur, [(0.0, glitch_noise(0.35, rng), 0.7), (0.3, thump(), 0.8),
                         (0.5, chime([440, 659, 880]), 0.2),
                         (0.9, glitch_noise(0.1, rng), 0.5), (1.6, glitch_noise(0.08, rng), 0.5)])
    if v == 1:
        return mix(dur, [(0.0, whoosh(0.45, rng), 0.5), (0.3, thump(), 1.0),
                         (0.4, chime([784, 988, 1175, 1568]), 0.35)])
    if v == 2:
        return mix(dur, [(0.0, whoosh(0.25, rng), 0.5), (0.2, thump(0.8), 1.0),
                         (0.45, boing(), 0.45), (0.5, chime([523, 784]), 0.25)])
    if v == 3:
        parts = [(0.0, whoosh(0.4, rng), 0.45), (0.32, thump(), 0.7)]
        for i in range(len(NICK)):
            parts.append((0.55 + i * 0.07, tick(rng), 0.35))
        parts.append((0.55 + len(NICK) * 0.07, chime([659, 880, 1319]), 0.4))
        return mix(dur, parts)
    return mix(dur, [(0.0, scratch(0.35, rng), 0.6), (0.25, whoosh(0.4, rng), 0.45),
                     (0.6, thump(), 0.8), (0.65, chime([587, 740, 880, 1175]), 0.3)])


def write_wav(path, mono):
    data = (np.clip(mono, -1, 1) * 32767).astype("<i2")
    stereo = np.repeat(data[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(stereo.tobytes())


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------
def render_outro(v, bg, dur, dst, tmp):
    frame = VARIANTS[v][1](bg, dur)
    wav = tmp / f"outro_{v}.wav"
    write_wav(wav, sound_for(v, dur))
    p = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(wav),
         "-c:v", "libx264", "-preset", C.PRESET, "-crf", str(C.CRF),
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", str(SR),
         "-shortest", str(dst)], stdin=subprocess.PIPE)
    try:
        for i in range(int(round(dur * FPS))):
            p.stdin.write(frame(i / FPS).convert("RGB").tobytes())
    except BrokenPipeError:
        pass                      # ffmpeg died; its exit code is checked below
    finally:
        try:
            p.stdin.close()
        except BrokenPipeError:
            pass
    if p.wait() != 0:
        raise SystemExit(f"ffmpeg failed on outro {v}")


def clip_duration(clip):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(clip)], capture_output=True, text=True)
    return float(out.stdout.strip())


def append(clip, outro, dst):
    """Clip + outro, both re-timed to the same frame rate and sample rate."""
    # A clip without sound gets silence, otherwise [0:a] does not exist.
    a0 = (f"[0:a]aresample={SR}[a0];" if has_audio(clip) else
          f"anullsrc=r={SR}:cl=stereo,atrim=0:{clip_duration(clip)}[a0];")
    fc = (f"[0:v]fps={FPS},setsar=1[v0];[1:v]fps={FPS},setsar=1[v1];"
          f"{a0}[1:a]aresample={SR}[a1];"
          f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(clip), "-i", str(outro),
                    "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-preset", C.PRESET, "-crf", str(C.CRF),
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(dst)], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip")
    ap.add_argument("out_dir")
    ap.add_argument("--only", default="5,6,7,8,9")
    ap.add_argument("--dur", type=float, default=2.5)
    args = ap.parse_args()

    clip = Path(args.clip).resolve()
    out_dir = Path(args.out_dir)
    try:
        only = [int(x) for x in args.only.split(",") if x.strip()]
    except ValueError:
        ap.error("--only: numbers separated by commas, e.g. 1,3")
    if not only or any(v not in VARIANTS for v in only):
        ap.error(f"--only: choose from {sorted(VARIANTS)}")
    if args.dur < 2.0:
        ap.error("--dur: at least 2.0 s, otherwise the animations do not finish")
    tmp = ENGINE / "work"
    tmp.mkdir(exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    bg = last_frame(clip)
    for v in only:
        name = VARIANTS[v][0]
        outro = tmp / f"outro_{v}.mp4"
        print(f"outro {v}: {name}")
        render_outro(v, bg, args.dur, outro, tmp)
        append(clip, outro, (out_dir / f"{v} Файт заставка {v} — {name}.mp4").resolve())
    print("done")


if __name__ == "__main__":
    main()
