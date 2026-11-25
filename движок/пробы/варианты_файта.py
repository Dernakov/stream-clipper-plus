# -*- coding: utf-8 -*-
"""
Prototype: render one raw 16:9 fight clip in several vertical framings
so the user can compare them side by side.

Variants:
  1  close    - webcam strip on top, tight game crop that follows the action
  2  full     - webcam on top, whole 16:9 frame in the middle, blurred fill
  3  medium   - webcam strip on top, square game crop that follows the action
  4  punch    - same layout as "full", but the frame zooms in on the action
                at key moments and zooms back out
  5  double   - whole 16:9 frame on top, tracked close-up below
  6  tall     - webcam on top; game at full height (score bar to HUD), sides
                cut at the right edge of the inventory and symmetrically left

Usage:
  python варианты_файта.py <clip.mp4> <out_dir> --title "..." [--only 1,3]
       [--zooms START-END:FACTOR,...] [--trim START-END]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # sibling probes (плашка)
import пути  # noqa: E402  must come first: keeps temp files and caches on D

import numpy as np  # noqa: E402
import clipper  # noqa: E402
import config as C  # noqa: E402

FPS = C.OUT_FPS
W, H = C.OUT_W, C.OUT_H


# --------------------------------------------------------------------------
# Action tracking
# --------------------------------------------------------------------------
def read_gray(src, w, h, fps, t0, dur):
    """Decode the clip as small grayscale frames: array [n, h, w] uint8."""
    cmd = ["ffmpeg", "-v", "error", "-ss", str(t0), "-t", str(dur), "-i", str(src),
           "-vf", f"fps={fps},scale={w}:{h},format=gray",
           "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w)


def action_track(src, sw, sh, t0, dur, mask_rects, fps=10, smooth_s=0.9):
    """
    Where is the action on screen, second by second.

    Motion between neighbouring frames is weighted towards the strongest
    changes; HUD, webcam, logo and kill-feed are masked out. Frames where
    almost the whole picture changes (camera jump) are skipped, the gaps are
    filled by interpolation, then the path is smoothed so the virtual camera
    does not jitter. Returns times (from clip start) and centres in source px.
    """
    lw, lh = 416, int(round(416 * sh / sw))
    fr = read_gray(src, lw, lh, fps, t0, dur)
    k = lw / sw
    mask = np.ones((lh, lw), np.float32)
    for x0, y0, x1, y1 in mask_rects:
        mask[int(y0 * k):int(y1 * k), int(x0 * k):int(x1 * k)] = 0
    ys, xs = np.mgrid[0:lh, 0:lw]

    times, cx, cy = [], [], []
    for i in range(1, len(fr)):
        d = np.abs(fr[i].astype(np.float32) - fr[i - 1]) * mask
        if (d > 25).mean() > 0.45:          # camera jump or full-screen flash
            continue
        thr = np.percentile(d[mask > 0], 97)
        wgt = np.clip(d - thr, 0, None)
        s = wgt.sum()
        if s < 1e-3:
            continue
        times.append(i / fps)
        cx.append((wgt * xs).sum() / s / k)
        cy.append((wgt * ys).sum() / s / k)

    grid = np.arange(0, dur + 1e-6, 1.0 / fps)
    if len(times) < 2:
        return grid, np.full_like(grid, sw / 2), np.full_like(grid, sh / 2)
    gx = np.interp(grid, times, cx)
    gy = np.interp(grid, times, cy)
    # Gaussian smoothing, edges padded so the path does not sag to zero.
    sig = smooth_s * fps
    kern = np.exp(-0.5 * (np.arange(-3 * sig, 3 * sig + 1) / sig) ** 2)
    kern /= kern.sum()
    pad = len(kern) // 2
    gx = np.convolve(np.pad(gx, pad, mode="edge"), kern, mode="valid")[:len(grid)]
    gy = np.convolve(np.pad(gy, pad, mode="edge"), kern, mode="valid")[:len(grid)]
    return grid, gx, gy


def piecewise(times, vals, var, step=0.5):
    """ffmpeg expression: linear interpolation of vals over time variable var."""
    knots = np.arange(times[0], times[-1] + 1e-6, step)
    if times[-1] - knots[-1] > 1e-3:
        knots = np.append(knots, times[-1])   # keep the final point of the path
    vs = np.interp(knots, times, vals)
    terms = [f"lt({var},{knots[0]:.2f})*{vs[0]:.1f}"]
    for (a, va), (b, vb) in zip(zip(knots, vs), zip(knots[1:], vs[1:])):
        terms.append(f"gte({var},{a:.2f})*lt({var},{b:.2f})*"
                     f"({va:.1f}+({vb - va:.1f})*({var}-{a:.2f})/{b - a:.2f})")
    terms.append(f"gte({var},{knots[-1]:.2f})*{vs[-1]:.1f}")
    return "+".join(terms)


def crop_path(grid, gx, gy, cw, ch, box):
    """Top-left corner of a cw x ch crop centred on the track, kept inside box."""
    x0, y0, x1, y1 = box
    lx = np.clip(gx - cw / 2, x0, x1 - cw)
    ly = np.clip(gy - ch / 2, y0, y1 - ch)
    return lx, ly


# --------------------------------------------------------------------------
# Subtitles and title
# --------------------------------------------------------------------------
def ts(t):
    t = round(max(0.0, t), 2)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


TITLE_STEP_K = 0.78      # line spacing of the two-line title, in font sizes
TITLE_LINE_K = 1.0       # height of one title line box, in font sizes (tuned by eye)
TITLE_BOTTOM_PAD = 12    # gap between the title and the bottom edge of the webcam strip


def build_ass(words, title, dur, title_top, sub_top, sub_size=90, size_max=None,
              step_k=None, title_bottom=None, size_min=None):
    """
    Two-line caps title (red / white) and one-word captions, censored.
    size_max caps the title font size (default: WIDE_TITLE_SIZE_MAX).
    step_k: line spacing in font sizes (default: WIDE_TITLE_STEP_K).
    title_bottom: if set, the title is pushed down so its last line ends
    there; title_top is then ignored.
    """
    t1, t2 = clipper.wrap2_top_heavy(clipper.esc_ass(title.strip().upper()), 20)
    t1, t2 = clipper.censor(t1), clipper.censor(t2)
    cap = C.WIDE_TITLE_SIZE_MAX if size_max is None else min(size_max, C.WIDE_TITLE_SIZE_MAX)
    tsz = clipper.fit_size_px([t1, t2], W * C.WIDE_TITLE_FIT, size_max=cap, size_min=size_min)
    step = int(tsz * (C.WIDE_TITLE_STEP_K if step_k is None else step_k))
    if title_bottom is not None:
        title_top = title_bottom - (step if t2 else 0) - int(tsz * TITLE_LINE_K)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: T1,{C.FONT_NAME},{tsz},{C.LIFE_COLOR_RED},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,5,0,8,40,40,{title_top},204
Style: T2,{C.FONT_NAME},{tsz},{C.COLOR_LINE2},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,5,0,8,40,40,{title_top + step},204
Style: SUB,{C.FONT_NAME},{sub_size},{C.COLOR_LINE2},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,6,0,8,40,40,{sub_top},204

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = [f"Dialogue: 1,{ts(0)},{ts(dur)},T1,,0,0,0,,{t1}"]
    if t2:
        ev.append(f"Dialogue: 1,{ts(0)},{ts(dur)},T2,,0,0,0,,{t2}")
    for i, w in enumerate(words):
        st = w["s"]
        nxt = words[i + 1]["s"] if i + 1 < len(words) else w["e"] + 0.4
        en = min(max(w["e"], st + 0.25), nxt - 0.01, dur)
        txt = w["w"].upper().lstrip("-— ").strip()
        if en <= st or not txt:
            continue
        ev.append(f"Dialogue: 0,{ts(st)},{ts(en)},SUB,,0,0,0,,"
                  f"{clipper.esc_ass(clipper.censor(txt))}")
    return head + "\n".join(ev) + "\n"


# --------------------------------------------------------------------------
# Layouts
# --------------------------------------------------------------------------
def blur_bg(label_in, label_out, crop=None):
    """Full canvas background: the game, enlarged, blurred and darkened."""
    c = f"crop={crop}," if crop else ""
    return (f"[{label_in}]{c}scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},gblur=sigma=28,eq=brightness=-0.18,setsar=1[{label_out}];")


def cam_strip(label_in, label_out, cam, h):
    w_, h_, x_, y_ = cam
    return (f"[{label_in}]crop={w_}:{h_}:{x_}:{y_},"
            f"scale={W}:{h}:flags=lanczos,setsar=1[{label_out}];")


def cam_full(label_in, label_out, cam, h, face_x=None):
    """
    Webcam at its full height, not zoomed in more than the strip needs.

    The whole webcam window (cam = w, h, x, y in source px) is scaled to the
    strip height h. If it comes out wider than the frame, the sides are cut,
    keeping face_x (source px) in the middle; if narrower, the sides are
    filled with a blurred, darkened copy of the same webcam.
    """
    w_, h_, x_, y_ = cam
    fw = int(round(w_ * h / h_ / 2) * 2)
    src = f"[{label_in}]crop={w_}:{h_}:{x_}:{y_},setsar=1"
    if fw >= W:
        fx = (x_ + w_ / 2) if face_x is None else face_x
        off = int(min(max((fx - x_) * h / h_ - W / 2, 0), fw - W))
        return f"{src},scale={fw}:{h}:flags=lanczos,crop={W}:{h}:{off}:0,setsar=1[{label_out}];"
    return (f"{src},split=2[{label_out}_b][{label_out}_f];"
            f"[{label_out}_b]scale={W}:{h}:force_original_aspect_ratio=increase,"
            f"crop={W}:{h},gblur=sigma=24,eq=brightness=-0.12,setsar=1[{label_out}_bg];"
            f"[{label_out}_f]scale={fw}:{h}:flags=lanczos,setsar=1[{label_out}_fg];"
            f"[{label_out}_bg][{label_out}_fg]overlay={(W - fw) // 2}:0[{label_out}];")


def tracked_crop(label_in, label_out, track, cw, ch, box, out_w, out_h):
    grid, gx, gy = track
    lx, ly = crop_path(grid, gx, gy, cw, ch, box)
    ex, ey = piecewise(grid, lx, "t"), piecewise(grid, ly, "t")
    return (f"[{label_in}]crop={cw}:{ch}:'{ex}':'{ey}',"
            f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[{label_out}];")


def punch_zoom(label_in, label_out, track, zooms, sw, sh, out_w, out_h, dur,
               game_bottom=None):
    """
    Whole 16:9 frame with smooth punch-ins: zoom factor z(t) eases from 1 to
    the target over 0.5 s at the start of each window and back at the end;
    while zoomed, the view is centred on the action track.

    The zoom curve is written as an exact smoothstep formula (short and
    perfectly smooth); x/y use the already smoothed track sampled every 0.5 s
    and read the current zoom from zoompan's own `zoom` variable. A long
    piecewise expression for all three overflows ffmpeg's expression parser.
    """
    grid, gx, gy = track
    ramp = 0.5
    t = f"(on/{FPS})"
    z_terms = []
    for a, b, f in zooms:
        e = (f"clip(min(({t}-{a:.2f})/{ramp},({b:.2f}-{t})/{ramp}),0,1)")
        z_terms.append(f"{f - 1:.3f}*{e}*{e}*(3-2*{e})")
    ez = "1+" + "+".join(z_terms)
    fmax = max(f for _, _, f in zooms)
    # zoompan works on the upscaled input; express everything in its pixels.
    up_w, up_h = out_w * 2, out_h * 2
    s = up_w / sw
    gxs = piecewise(grid, gx * s, t)
    gys = piecewise(grid, gy * s, t)
    # k: 0 at no zoom (frame centre), 1 at full zoom (action centre).
    k = f"clip((zoom-1)/{max(1e-6, fmax - 1):.3f},0,1)"
    ex = f"clip(iw/2+(({gxs})-iw/2)*{k}-iw/zoom/2,0,iw-iw/zoom)"
    # While zoomed, keep the bottom HUD and webcam out of view as far as the
    # zoom allows; at zoom 1 the bound collapses to 0 and the full frame shows.
    ybot = "ih" if game_bottom is None else f"{game_bottom / sh:.4f}*ih"
    ey = (f"clip(ih/2+(({gys})-ih/2)*{k}-ih/zoom/2,0,"
          f"max(0,{ybot}-ih/zoom))")
    return (f"[{label_in}]fps={FPS},scale={up_w}:{up_h}:flags=lanczos,"
            f"zoompan=z='{ez}':x='{ex}':y='{ey}':d=1:s={out_w}x{out_h}:fps={FPS},"
            f"setsar=1[{label_out}];")


def build_filter(variant, track, geo, zooms, dur):
    sw, sh = geo["sw"], geo["sh"]
    cam = geo["cam"]
    game_box = geo["game_box"]          # x0, y0, x1, y1 of the playable area
    full_h = int(round(W * sh / sw / 2) * 2)   # 16:9 frame at 1080 wide

    if variant == 1:     # close: webcam 540 on top, tight tracked crop 1080x1380
        game_h = H - 540
        ch = game_box[3] - game_box[1]
        cw = int(round(ch * W / game_h / 2) * 2)
        f = ("[jv]split=2[a][b];" + cam_strip("a", "cam", cam, 540)
             + tracked_crop("b", "game", track, cw, ch, game_box, W, game_h)
             + "[cam][game]vstack=inputs=2[st];")
        return f, dict(title_top=480, sub_top=1560)

    if variant == 2:     # full: webcam on top, whole frame in the middle
        frame_y = 820
        f = ("[jv]split=3[a][b][c];" + blur_bg("a", "bg", geo["bg_crop"])
             + cam_strip("b", "cam", cam, 540)
             + f"[c]scale={W}:{full_h}:flags=lanczos,setsar=1[full];"
             + f"[bg][cam]overlay=0:0[t1];[t1][full]overlay=0:{frame_y}[st];")
        return f, dict(title_top=480, sub_top=frame_y + full_h + 60)

    if variant == 3:     # medium: webcam 540 on top, square tracked crop
        side = game_box[3] - game_box[1]
        f = ("[jv]split=3[a][b][c];" + blur_bg("a", "bg", geo["bg_crop"])
             + cam_strip("b", "cam", cam, 540)
             + tracked_crop("c", "game", track, side, side, game_box, W, W)
             + f"[bg][cam]overlay=0:0[t1];[t1][game]overlay=0:540[st];")
        return f, dict(title_top=480, sub_top=540 + W + 50)

    if variant == 4:     # punch: like "full", with zoom-ins on key moments
        frame_y = 820
        f = ("[jv]split=3[a][b][c];" + blur_bg("a", "bg", geo["bg_crop"])
             + cam_strip("b", "cam", cam, 540)
             + punch_zoom("c", "full", track, zooms, sw, sh, W, full_h, dur,
                          game_box[3])
             + f"[bg][cam]overlay=0:0[t1];[t1][full]overlay=0:{frame_y}[st];")
        return f, dict(title_top=480, sub_top=frame_y + full_h + 60)

    if variant == 5:     # double: whole frame on top, tracked close-up below
        low_h = H - full_h - 230
        ch = game_box[3] - game_box[1]
        cw = int(round(ch * W / low_h / 2) * 2)
        f = ("[jv]split=3[a][b][c];" + blur_bg("a", "bg", geo["bg_crop"])
             + f"[b]scale={W}:{full_h}:flags=lanczos,setsar=1[full];"
             + tracked_crop("c", "game", track, cw, ch, game_box, W, low_h)
             + f"[bg][full]overlay=0:0[t1];[t1][game]overlay=0:{full_h + 230}[st];")
        return f, dict(title_top=full_h + 10, sub_top=H - 330)

    if variant == 6:     # full height: score bar to HUD, sides cut symmetrically
        # geo["hud_right"]: right edge of the inventory block in source px;
        # the left edge mirrors it, so the HUD stays centred.
        if "hud_right" not in geo or ("cam6" not in geo and "cam_full" not in geo):
            raise SystemExit("variant 6 needs hud_right and cam6 or cam_full in the geometry json")
        x1 = geo["hud_right"]
        if not sw / 2 < x1 <= sw:
            raise SystemExit(f"hud_right={x1} is outside the right half of the frame")
        x0 = sw - x1
        cw = x1 - x0
        game_h = int(round(sh * W / cw / 2) * 2)
        cam_h = H - game_h
        cam6 = geo.get("cam6")       # webcam crop with the cam_h:W proportion
        hud_top = geo.get("hud_top", game_box[3])
        sub_top = cam_h + int(hud_top * W / cw) - 190
        if "cam_full" in geo:
            strip = cam_full("a", "cam", geo["cam_full"], cam_h, geo.get("face_x"))
        else:
            strip = cam_strip("a", "cam", cam6, cam_h)
        f = ("[jv]split=2[a][b];" + strip
             + f"[b]crop={cw}:{sh}:{x0}:0,scale={W}:{game_h}:flags=lanczos,setsar=1[game];"
             + "[cam][game]vstack=inputs=2[st];")
        # Title sits at the very bottom of the webcam strip, lines close together.
        pos = dict(title_top=cam_h - 260, sub_top=sub_top, step_k=TITLE_STEP_K,
                   title_bottom=cam_h - TITLE_BOTTOM_PAD)
        if "cam_full" in geo and geo.get("chin"):
            # Title goes right under the chin; the font shrinks to fit the strip.
            _, ch_, _, cy_ = geo["cam_full"]
            if not cy_ <= geo["chin"] <= cy_ + ch_:
                raise SystemExit(f"chin={geo['chin']} is outside the webcam window {cy_}..{cy_ + ch_}")
            chin_out = int((geo["chin"] - cy_) * cam_h / ch_)
            room = cam_h - TITLE_BOTTOM_PAD - (chin_out + 12)
            fit = int(room / (TITLE_STEP_K + TITLE_LINE_K))
            # Rule (2026-11-15): the title never covers the face.
            if fit < 48:
                raise SystemExit(f"only {room}px under the chin (fits size {fit}, minimum 48): "
                                 "the title would cover the face")
            # The font shrinks only if two lines do not fit under the chin;
            # size_min lets fit_size_px go below its default minimum of 62.
            pos["size_max"] = fit
            pos["size_min"] = 48
        if "cam_cover" in geo:
            # Box over the duplicated webcam inside the game crop, output px.
            right, top = geo["cam_cover"]
            k = W / cw
            pos["cover"] = (int(round((right - x0) * k / 2) * 2),
                            H - (cam_h + int(top * k)), cam_h + int(top * k))
        return f, pos

    raise SystemExit(f"unknown variant {variant}")


NAMES = {1: "крупно с ведением", 2: "весь экран", 3: "средний план с ведением",
         4: "весь экран с наездами", 5: "общий план и крупный",
         6: "вся высота игры"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out_dir")
    ap.add_argument("--title", required=True)
    ap.add_argument("--only", default="1,2,3,4,5")
    ap.add_argument("--zooms", default="", help="A-B:F,... seconds from clip start")
    ap.add_argument("--trim", default="", help="A-B seconds to keep")
    ap.add_argument("--geo", required=True, help="json file with geometry")
    ap.add_argument("--chin", type=int, default=0,
                    help="lowest chin y in the webcam (source px) for this clip; "
                         "overrides geo['chin']")
    ap.add_argument("--patch", type=int, default=0,
                    help="variant 6: cover the duplicated webcam with promo patch style N "
                         "(see плашка.py); the nick badge moves into the patch")
    args = ap.parse_args()

    src = Path(args.src).resolve()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    geo = json.loads(Path(args.geo).read_text(encoding="utf-8"))
    if args.chin:
        geo["chin"] = args.chin
    # Временные файлы — в личной папке процесса: параллельные задачи
    # в соседних чатах не перетирают их друг у друга.
    work = clipper.job_dir()
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(src)], capture_output=True, check=True).stdout)
    total = float(probe["format"]["duration"])
    t0, t1 = (map(float, args.trim.split("-")) if args.trim else (0.0, total))
    dur = t1 - t0

    print("tracking the action...")
    track = action_track(src, geo["sw"], geo["sh"], t0, dur, geo["mask"])
    np.savetxt(work / "variants_track.csv", np.c_[track], delimiter=",", fmt="%.2f")

    # Words: cached per clip so every variant shows the same captions.
    wav = work / "variants_tmp.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t0), "-t", str(dur),
                    "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(wav)], check=True)
    # Cache is valid only for the same clip, size and trim window.
    key = {"src": str(src), "size": src.stat().st_size, "t0": t0, "t1": t1}
    # Кэш слов — в кэше этого клипа (work/<имя клипа>/), он переживает перезапуск.
    words_cache = clipper.stream_cache(src) / "variants_words.json"
    cached = (json.loads(words_cache.read_text(encoding="utf-8"))
              if words_cache.exists() else {})
    if cached.get("key") == key:
        words = cached["words"]
    else:
        print("transcribing...")
        words = clipper.clip_words(wav)
        words_cache.write_text(json.dumps({"key": key, "words": words},
                                          ensure_ascii=False, indent=1),
                               encoding="utf-8")
    gain = clipper.linear_gain_db(wav)
    again = f"volume={gain}dB," if gain else ""

    if not C.STREAMER_NICK:
        raise SystemExit("STREAMER_NICK не задан в мои_настройки.py: плашке с ником нечего показать")
    clipper.render_nick_badge(C.STREAMER_NICK, work / "clip_nick.png")
    zooms = []
    for part in filter(None, args.zooms.split(",")):
        rng, f = part.split(":")
        a, b = map(float, rng.split("-"))
        zooms.append((a, b, float(f)))

    for v in map(int, args.only.split(",")):
        fc, pos = build_filter(v, track, geo, zooms or [(0, dur, 1.5)], dur)
        cover = pos.pop("cover", None)
        extra_in, name_tail = [], ""
        if args.patch and v == 6:
            if not cover:
                raise SystemExit("--patch needs cam_cover in the geometry json")
            import плашка
            pw, ph, py = cover
            if pw <= 0 or ph <= 0:
                raise SystemExit("cam_cover lies outside the game crop")
            if "[st];" not in fc:
                raise SystemExit("filter layout changed: no [st] label to put the patch on")
            mov = work / f"patch_{args.patch}.mov"
            плашка.render(args.patch, pw, ph, mov)
            extra_in = ["-stream_loop", "-1", "-i", mov.name]
            # Patch replaces the badge: overlay it and skip the nick overlay below.
            fc = fc.replace("[st];", f"[st0];[st0][1:v]overlay=0:{py}:shortest=1[st];")
            name_tail = f" плашка {args.patch} {плашка.STYLES[args.patch][0]}"
        ass = work / f"variant_{v}.ass"
        ass.write_text(build_ass(words, args.title, dur, **pos), encoding="utf-8")
        full = (f"[0:v]trim={t0}:{t1},setpts=PTS-STARTPTS[jv];"
                f"[0:a]atrim={t0}:{t1},asetpts=PTS-STARTPTS[ja];" + fc
                + (f"movie=clip_nick.png[nick];[st][nick]overlay="
                   f"{C.NICK_BADGE_X}:{C.NICK_BADGE_Y}[stn];" if not extra_in
                   else "[st]null[stn];")
                + f"[stn]subtitles={ass.name}:fontsdir={clipper.FONTS_REL}[v];"
                  f"[ja]{again}alimiter=limit=0.85:level=disabled,aresample=48000[a]")
        dst = (out_dir / f"{v} Файт вариант {v} — {NAMES[v]}{name_tail}.mp4").resolve()
        tmp_dst = work / f"render_{v}.mp4"
        print(f"rendering {dst.name}...")
        script = work / f"variant_{v}.filter"
        script.write_text(full, encoding="utf-8")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), *extra_in,
                        "-/filter_complex", script.name, "-map", "[v]", "-map", "[a]",
                        "-r", str(FPS), "-c:v", "libx264", "-preset", C.PRESET,
                        "-crf", str(C.CRF), "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                        str(tmp_dst)], check=True, cwd=str(work))
        # Готовый ролик появляется в папке партии целиком, без недописанного файла.
        clipper.finish_file(tmp_dst, dst)
    print("done")


if __name__ == "__main__":
    main()
