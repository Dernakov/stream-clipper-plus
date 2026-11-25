# -*- coding: utf-8 -*-
"""
Prototype: animated promo patch that covers the duplicated webcam in the
bottom-left corner of the "full game height" layout (variant 6).

Each style is a 2-second seamless loop rendered to a .mov with alpha
(png codec); the caller loops it for the whole clip.

Styles:
  1  purple   - Twitch-purple panel with moving stripes, logo, nick, call to action
  2  follow   - dark Twitch-style card with a pulsing purple FOLLOW button
  3  live     - black panel, blinking red LIVE badge, nick written vertically
  4  neon     - dark panel with a flickering neon frame and glowing nick
  5  ticker   - purple gradient with a vertical running line of the nick

  python плашка.py <style> <width> <height> <out.mov>
"""
import math
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(HERE))
import пути  # noqa: E402,F401  must come first: keeps temp files and caches in .cache

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402
import заставка as Z  # noqa: E402  shared drawing helpers and the nick check

FPS = Z.FPS
LOOP = 2.0                          # seconds; every animation repeats with this period
N = int(LOOP * FPS)
PURPLE = Z.PURPLE
DARK = (24, 24, 27)                 # Twitch dark UI background
NICK = Z.NICK
CTA = "ЗАХОДИ НА СТРИМ"             # call to action, Cyrillic: Montserrat / Days One


def rounded_mask(w, h, r, corners="tr"):
    """
    Rounded corners of the patch. "tr": only the top-right corner (the patch
    sits in the bottom-left corner of the frame); "left": top-left and
    bottom-left (the patch touches the right edge); "all": every corner.
    """
    m = Image.new("L", (w * 4, h * 4), 0)
    d = ImageDraw.Draw(m)
    R = r * 4
    if corners == "tr":
        box = (-R, 0, w * 4 - 1, h * 4 + R)
    elif corners == "left":
        box = (0, 0, w * 4 - 1 + R, h * 4 - 1)
    elif corners == "all":
        box = (0, 0, w * 4 - 1, h * 4 - 1)
    else:
        raise SystemExit(f"unknown corners mode {corners}")
    d.rounded_rectangle(box, radius=R, fill=255)
    return m.resize((w, h), Image.LANCZOS)


def two_lines(nick):
    """Split the nick at the underscore so it fits a narrow panel."""
    if "_" in nick:
        a, b = nick.split("_", 1)
        return a, "_" + b
    half = len(nick) // 2
    return nick[:half], nick[half:]


def stack(layers, gap):
    """Vertical stack of RGBA layers, centred horizontally."""
    w = max(l.width for l in layers)
    h = sum(l.height for l in layers) + gap * (len(layers) - 1)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    y = 0
    for l in layers:
        out.alpha_composite(l, ((w - l.width) // 2, y))
        y += l.height + gap
    return out


def fit_box(img, w, h):
    """Scale img down (never up) to fit inside w x h."""
    k = min(1.0, w / img.width, h / img.height)
    return Z.scaled(img, k) if k < 1 else img


def cta_block(w, size, color):
    """Call to action on one line if it fits at a readable size, else two lines."""
    one = Z.text_layer(CTA, "Montserrat-ExtraBold.ttf", size, color)
    if one.width <= w:
        return one
    a, b = CTA.split(" ", 1)
    lines = [Z.fit_text(x, "Montserrat-ExtraBold.ttf", w, size, color) for x in (a, b)]
    return stack(lines, 2)


def nick_block(w, font, color, max_size=90):
    a, b = two_lines(NICK.upper())
    la = Z.fit_text(a, font, w, max_size, color)
    lb = Z.fit_text(b, font, w, max_size, color)
    return stack([la, lb], 4)


# --------------------------------------------------------------------------
# Styles: each returns frame(i) -> RGBA w x h, periodic with period N
# --------------------------------------------------------------------------
def s_purple(w, h):
    pad = max(12, w // 14)
    logo = Z.glitch_logo(min(w - 2 * pad, 120), body=(255, 255, 255, 255), face=PURPLE + (255,))
    nick = nick_block(w - 2 * pad, "Bungee-Regular.ttf", (255, 255, 255, 255))
    cta = cta_block(w - 2 * pad, 34, (255, 230, 120, 255))
    body = fit_box(stack([logo, nick, cta], 16), w - 2 * pad, h - 2 * pad)
    step = 40

    def frame(i):
        c = Image.new("RGBA", (w, h), PURPLE + (255,))
        d = ImageDraw.Draw(c)
        off = int(step * 2 * i / N)               # stripes move exactly two widths per loop
        for x in range(-h - 2 * step, w + 2 * step, step):
            d.polygon([(x + off, 0), (x + off + step // 2, 0),
                       (x + off + step // 2 - h, h), (x + off - h, h)], fill=(160, 95, 255, 255))
        k = 1 + 0.04 * math.sin(2 * math.pi * i / N)
        Z.put(c, Z.scaled(body, k), w / 2, h / 2)
        return c
    return frame


def heart(size, color):
    s = 4
    img = Image.new("RGBA", (size * s, size * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size * s / 4
    d.ellipse((0, 0, 2 * r, 2 * r), fill=color)
    d.ellipse((2 * r, 0, 4 * r, 2 * r), fill=color)
    d.polygon([(0.15 * r, 1.35 * r), (3.85 * r, 1.35 * r), (2 * r, 3.9 * r)], fill=color)
    return img.resize((size, size), Image.LANCZOS)


def s_follow(w, h):
    pad = max(12, w // 14)
    logo = Z.glitch_logo(min(70, w // 3))
    nick = nick_block(w - 2 * pad, "Bungee-Regular.ttf", (239, 239, 241, 255), 70)
    label = Z.text_layer("FOLLOW", "Bungee-Regular.ttf", 40, (255, 255, 255, 255))
    hrt = heart(label.height, (255, 255, 255, 255))
    bw = min(w - 2 * pad, hrt.width + 12 + label.width + 40)
    btn = Image.new("RGBA", (hrt.width + 12 + label.width + 40, label.height + 34), (0, 0, 0, 0))
    ImageDraw.Draw(btn).rounded_rectangle((0, 0, btn.width - 1, btn.height - 1),
                                          radius=14, fill=PURPLE + (255,))
    btn.alpha_composite(hrt, (20, 17))
    btn.alpha_composite(label, (20 + hrt.width + 12, 17))
    btn = Z.scaled(btn, bw / btn.width)
    cta = cta_block(w - 2 * pad, 30, (173, 173, 184, 255))
    top = fit_box(stack([logo, nick], 12), w - 2 * pad, h * 0.42)

    def frame(i):
        c = Image.new("RGBA", (w, h), DARK + (255,))
        ImageDraw.Draw(c).rectangle((0, 0, w - 1, 5), fill=PURPLE + (255,))
        Z.put(c, top, w / 2, pad + 8 + top.height / 2)
        p = 0.5 - 0.5 * math.cos(2 * math.pi * i / N)       # 0..1..0 pulse
        # Button sits right above the call to action, whatever its height.
        Z.put(c, Z.scaled(btn, 1 + 0.07 * p), w / 2,
              h - pad - cta.height - 16 - btn.height * 1.07 / 2)
        Z.put(c, cta, w / 2, h - pad - cta.height / 2)
        return c
    return frame


def s_live(w, h):
    pad = max(10, w // 16)
    dot_r = 9
    live = Z.text_layer("LIVE", "Bungee-Regular.ttf", 36, (255, 255, 255, 255))
    pill = Image.new("RGBA", (live.width + 2 * dot_r + 40, live.height + 16), (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle((0, 0, pill.width - 1, pill.height - 1),
                                           radius=pill.height // 2, fill=(235, 4, 0, 255))
    pill.alpha_composite(live, (2 * dot_r + 26, 8))
    pill = fit_box(pill, w - 2 * pad, 80)
    vert = Z.text_layer(NICK.upper(), "Bungee-Regular.ttf", 90, (255, 255, 255, 255))
    vert = vert.rotate(90, expand=True)
    logo_s = min(70, w // 3)
    logo = Z.glitch_logo(logo_s)
    vert = fit_box(vert, w - 2 * pad, h - pill.height - logo_s - 4 * pad)

    def frame(i):
        c = Image.new("RGBA", (w, h), (10, 10, 12, 255))
        Z.put(c, pill, w / 2, pad + pill.height / 2)
        if (i // (N // 2)) % 2 == 0:                          # dot blinks once per second
            r = dot_r * pill.height / (live.height + 16)
            cx = w / 2 - pill.width / 2 + 13 + r
            cy = pad + pill.height / 2
            ImageDraw.Draw(c).ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255, 255))
        Z.put(c, vert, w / 2, pad + pill.height + pad + vert.height / 2)
        Z.put(c, logo, w / 2, h - pad - logo_s / 2)
        return c
    return frame


def s_neon(w, h):
    pad = max(14, w // 12)
    nick = nick_block(w - 3 * pad, "Bungee-Regular.ttf", (245, 235, 255, 255), 80)
    nick = Z.glow(fit_box(nick, w - 3 * pad, h * 0.45), 10, PURPLE, 3)
    tw = Z.glow(Z.fit_text("TWITCH", "Monoton-Regular.ttf", w - 3 * pad, 60,
                           (210, 170, 255, 255)), 8, PURPLE, 2)
    cta = cta_block(w - 3 * pad, 30, (255, 255, 255, 220))
    frame_img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(frame_img).rounded_rectangle((pad // 2, pad // 2, w - pad // 2, h - pad // 2),
                                                radius=18, outline=(200, 140, 255, 255), width=4)
    frame_glow = frame_img.filter(ImageFilter.GaussianBlur(8))
    # Flicker pattern for one loop: mostly on, two short dips.
    flick = [1.0] * N
    for a, b in ((int(N * 0.31), int(N * 0.35)), (int(N * 0.38), int(N * 0.40))):
        for k in range(a, b):
            flick[k] = 0.35

    def frame(i):
        c = Image.new("RGBA", (w, h), (14, 8, 24, 255))
        f = flick[i]
        for layer in (frame_glow, frame_glow, frame_img):
            Z.put(c, layer, w / 2, h / 2, alpha=f)
        Z.put(c, tw, w / 2, pad + 10 + tw.height / 2, alpha=f)
        Z.put(c, nick, w / 2, h * 0.52)
        Z.put(c, cta, w / 2, h - pad - 10 - cta.height / 2)
        return c
    return frame


def s_ticker(w, h):
    unit = Z.text_layer(f" {NICK.upper()}  •  TWITCH  • ", "Bungee-Regular.ttf",
                        int(w * 0.42), (255, 255, 255, 255)).rotate(90, expand=True)
    logo_s = min(84, w // 2)
    logo = Z.glitch_logo(logo_s, body=(255, 255, 255, 255), face=PURPLE + (255,))
    grad = Image.new("RGBA", (w, h))
    gd = ImageDraw.Draw(grad)
    for y in range(h):
        k = y / max(1, h - 1)
        gd.line((0, y, w, y), fill=(int(145 - 60 * k), int(70 - 40 * k), int(255 - 90 * k), 255))
    lane_w = unit.width
    lane_x = (w - lane_w) // 2
    top = logo_s + 30

    def frame(i):
        c = grad.copy()
        lane = Image.new("RGBA", (lane_w, h - top), (0, 0, 0, 0))
        off = int(unit.height * i / N)                 # one full text unit per loop
        y = -off
        while y < lane.height:
            # Crop the unit to the part that is inside the lane.
            top_cut = max(0, -y)
            part = unit.crop((0, top_cut, unit.width,
                              min(unit.height, lane.height - y)))
            lane.alpha_composite(part, (0, max(0, y)))
            y += unit.height
        c.alpha_composite(lane, (lane_x, top))
        ImageDraw.Draw(c).rectangle((0, 0, w, top - 6), fill=(30, 10, 60, 255))
        Z.put(c, logo, w / 2, (top - 6) / 2)
        return c
    return frame


STYLES = {1: ("фиолетовая плашка", s_purple), 2: ("кнопка follow", s_follow),
          3: ("live", s_live), 4: ("неон", s_neon), 5: ("бегущая строка", s_ticker)}


def render(style, w, h, dst, corners="tr"):
    """Write a seamless 2-second loop with alpha to dst (.mov, png codec)."""
    if style not in STYLES:
        raise SystemExit(f"unknown patch style {style}, choose from {sorted(STYLES)}")
    frame = STYLES[style][1](w, h)
    mask = rounded_mask(w, h, 22, corners)
    p = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba",
         "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
         "-c:v", "png", "-pix_fmt", "rgba", str(dst)], stdin=subprocess.PIPE)
    try:
        for i in range(N):
            img = frame(i).convert("RGBA")
            a = Image.composite(img.getchannel("A"), Image.new("L", (w, h), 0), mask)
            img.putalpha(a)
            p.stdin.write(img.tobytes())
    except BrokenPipeError:
        pass
    finally:
        try:
            p.stdin.close()
        except BrokenPipeError:
            pass
    if p.wait() != 0:
        raise SystemExit(f"ffmpeg failed on patch style {style}")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    render(int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4]))
