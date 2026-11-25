# -*- coding: utf-8 -*-
"""
Prototype: Counter-Strike fight clip in several horizontal crop widths.

Same idea as variant 6 of варианты_файта.py (the Dota template): the game is
kept at full height (score bar on top, health/ammo HUD at the bottom), the
sides are cut, the webcam fills the space above. In CS the webcam sits on the
right side of the frame, so when a crop catches it, the webcam (and the
channel logo above it) is covered by the promo patch from плашка.py.

All geometry lives in a json file (source pixels):
  {"sw": 1664, "sh": 936,
   "sub_y": 860,                     # captions end above this source y (bottom HUD)
   "variants": [
     {"name": "...", "x0": 429, "x1": 1235,
      "cam": [w, h, x, y],           # webcam crop with the proportion of the top strip
      "cam_full": [w, h, x, y],      # optional, replaces "cam": the whole webcam
                                     # window, fitted by height, blurred sides
      "title_size": 80,              # optional cap of the title font size
      "cover": [x0, y0, x1, y1]}     # optional: area to hide under the patch
   ]}

The title must never cover the webcam image of the streamer's face. With
"cam_full" pass --chin (lowest chin y in source px): the title then shrinks
to fit between the chin and the bottom of the strip, or the run stops.

Usage:
  python варианты_cs.py <clip.mp4> <out_dir> --geo geo.json --title "..."
       --trim A-B [--words words.json] [--only 1,2] [--patch 2] [--chin Y]
       [--name "file name.mp4"]
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # sibling probes
import пути  # noqa: E402  must come first: keeps temp files and caches on D

import clipper  # noqa: E402
import config as C  # noqa: E402
import варианты_файта as VF  # noqa: E402  subtitles / title builder
import плашка  # noqa: E402

FPS = C.OUT_FPS
W, H = C.OUT_W, C.OUT_H


MIN_PATCH_W = 80                     # output px; narrower patches are unreadable
MIN_TITLE_SIZE = 48                  # px; a smaller title is hard to read on a phone
CHIN_GAP = 12                        # px between the chin and the top of the title


def even(v):
    return int(round(v / 2) * 2)


def layout(geo, v):
    """Filter graph for one variant plus title/caption/patch positions."""
    sw, sh = geo["sw"], geo["sh"]
    x0, x1 = v["x0"], v["x1"]
    if not 0 <= x0 < x1 <= sw:
        raise SystemExit(f"bad crop {x0}-{x1} for frame width {sw}")
    cw = x1 - x0
    k = W / cw                                   # source px -> output px of the game
    game_h = even(sh * k)
    cam_h = H - game_h
    if v.get("cam_full"):
        # Whole webcam window by height: not zoomed in, room left under the chin.
        strip = VF.cam_full("a", "cam", v["cam_full"], cam_h, v.get("face_x"))
    else:
        cw_, ch_, cx_, cy_ = v["cam"]
        ck = W / cw_                             # source px -> output px of the webcam
        if abs(ch_ * ck - cam_h) > 12:
            print(f"  note: webcam crop {cw_}x{ch_} gives {ch_ * ck:.0f}px, strip is {cam_h}px")
        strip = VF.cam_strip("a", "cam", v["cam"], cam_h)

    fc = ("[jv]split=2[a][b];"
          + strip
          + f"[b]crop={cw}:{sh}:{x0}:0,scale={W}:{game_h}:flags=lanczos,setsar=1[game];"
          + "[cam][game]vstack=inputs=2[st];")

    # Title sits at the bottom of the webcam strip: off the game, as low under
    # the face as the strip allows. A short strip gets a smaller title.
    size_max = min(v.get("title_size", C.WIDE_TITLE_SIZE_MAX), C.WIDE_TITLE_SIZE_MAX)
    size_min = None
    chin = geo.get("chin")
    if chin and v.get("cam_full"):
        # Rule: the title never covers the face. It must fit between the chin
        # and the bottom of the strip; the font shrinks to a readable minimum.
        # Same wrapping as build_ass (censored), so the line count matches.
        t1, t2 = clipper.wrap2_top_heavy(clipper.esc_ass(geo["title"].strip().upper()), 20)
        t1, t2 = clipper.censor(t1), clipper.censor(t2)
        _, fh, _, fy = v["cam_full"]
        if not fy <= chin <= fy + fh:
            raise SystemExit(f"chin={chin} is outside the webcam window {fy}..{fy + fh}")
        chin_out = int((chin - fy) * cam_h / fh)
        room = cam_h - VF.TITLE_BOTTOM_PAD - (chin_out + CHIN_GAP)
        lines_k = VF.TITLE_LINE_K + (VF.TITLE_STEP_K if t2 else 0)
        fit = int(room / lines_k)
        if fit < MIN_TITLE_SIZE:
            raise SystemExit(f"only {room}px under the chin (fits size {fit}, minimum "
                             f"{MIN_TITLE_SIZE}): the title would cover the face")
        size_max = min(size_max, fit)
        # fit_size_px never goes below its minimum (62 by default), so the
        # minimum is lowered here; otherwise a small fit would be ignored.
        size_min = MIN_TITLE_SIZE
    elif chin:
        raise SystemExit("--chin needs cam_full in the geometry")
    sub_top = cam_h + int(geo["sub_y"] * k) - 115
    # title_top is ignored by build_ass when title_bottom is set.
    pos = dict(title_top=0, sub_top=sub_top, step_k=VF.TITLE_STEP_K,
               title_bottom=cam_h - VF.TITLE_BOTTOM_PAD, size_min=size_min)

    cover = None
    if v.get("cover"):
        a, b, c, d = v["cover"]
        a, c = max(a, x0), min(c, x1)            # only the part inside the crop
        b, d = max(b, 0), min(d, sh)
        # A sliver of the webcam is too narrow for a readable patch: skip it.
        if (c - a) * k >= MIN_PATCH_W and d > b:
            px = int((a - x0) * k)
            pw = W - px if c >= x1 else int((c - x0) * k) - px
            py = cam_h + int(b * k)
            ph = int((d - b) * k)
            cover = (px, py, even(pw), even(ph), "left" if c >= x1 else "all")
    return fc, pos, size_max, cover


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out_dir")
    ap.add_argument("--geo", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--trim", required=True, help="A-B seconds to keep")
    ap.add_argument("--words", default="", help="cached words json for the trimmed part")
    ap.add_argument("--only", default="")
    ap.add_argument("--patch", type=int, default=2, help="patch style, see плашка.py")
    ap.add_argument("--chin", type=int, default=0,
                    help="lowest chin y in the webcam (source px); needs cam_full")
    ap.add_argument("--name", default="",
                    help="output file name (only with a single rendered variant)")
    args = ap.parse_args()

    src = Path(args.src).resolve()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    geo = json.loads(Path(args.geo).read_text(encoding="utf-8"))
    geo["title"] = args.title
    if args.chin:
        geo["chin"] = args.chin
    try:
        t0, t1 = map(float, args.trim.split("-"))
    except ValueError:
        raise SystemExit(f"bad --trim {args.trim!r}, expected A-B in seconds")
    dur = t1 - t0
    if t0 < 0 or dur <= 0:
        raise SystemExit(f"bad --trim {args.trim!r}: need 0 <= A < B")
    only = {int(i) for i in args.only.split(",") if i}
    picked = [n for n in range(1, len(geo["variants"]) + 1) if not only or n in only]
    if args.name and len(picked) != 1:
        raise SystemExit("--name needs exactly one variant to render")
    if args.name and Path(args.name).name != args.name:
        raise SystemExit("--name must be a bare file name, not a path")
    # Check every picked variant before the slow work starts.
    for n in picked:
        layout(geo, geo["variants"][n - 1])
    # Временные файлы — в личной папке процесса (параллельные задачи).
    work = clipper.job_dir()

    wav = work / "cs_variants.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t0), "-t", str(dur),
                    "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(wav)], check=True)
    if args.words:
        data = json.loads(Path(args.words).read_text(encoding="utf-8"))
        # Accept both a plain list and the {"key", "words"} cache of варианты_файта.py.
        words = data["words"] if isinstance(data, dict) else data
    else:
        print("transcribing...")
        words = clipper.clip_words(wav)
    gain = clipper.linear_gain_db(wav)
    again = f"volume={gain}dB," if gain else ""

    if not C.STREAMER_NICK:
        raise SystemExit("STREAMER_NICK не задан в мои_настройки.py: плашке с ником нечего показать")
    clipper.render_nick_badge(C.STREAMER_NICK, work / "clip_nick.png")
    for n in picked:
        v = geo["variants"][n - 1]
        fc, pos, size_max, cover = layout(geo, v)
        extra_in = []
        if cover:
            px, py, pw, ph, corners = cover
            mov = work / f"cs_patch_{n}.mov"
            плашка.render(args.patch, pw, ph, mov, corners)
            extra_in = ["-stream_loop", "-1", "-i", mov.name]
            # The patch carries the nick, so the separate nick badge is skipped.
            if fc.count("[st];") != 1:
                raise SystemExit("filter layout changed: expected one [st] label")
            fc = fc.replace("[st];", f"[st0];[st0][1:v]overlay={px}:{py}:shortest=1[st];")
            badge = "[st]null[stn];"
        else:
            badge = (f"movie=clip_nick.png[nick];[st][nick]overlay="
                     f"{C.NICK_BADGE_X}:{C.NICK_BADGE_Y}[stn];")
        ass_text = VF.build_ass(words, args.title, dur, size_max=size_max, **pos)
        ass = work / f"cs_variant_{n}.ass"
        ass.write_text(ass_text, encoding="utf-8")
        # The input is seeked with -ss/-t, so the graph starts at zero.
        full = ("[0:v]setpts=PTS-STARTPTS[jv];"
                "[0:a]asetpts=PTS-STARTPTS[ja];" + fc + badge
                + f"[stn]subtitles={ass.name}:fontsdir={clipper.FONTS_REL}[v];"
                  f"[ja]{again}alimiter=limit=0.85:level=disabled,aresample=48000[a]")
        script = work / f"cs_variant_{n}.filter"
        script.write_text(full, encoding="utf-8")
        fname = args.name or f"{n} Файт CS вариант {n} — {v['name']}.mp4"
        dst = (out_dir / fname).resolve()
        tmp_dst = work / f"render_{n}.mp4"
        print(f"rendering {dst.name}...")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t0), "-t", str(dur),
                        "-i", str(src), *extra_in,
                        "-/filter_complex", script.name, "-map", "[v]", "-map", "[a]",
                        "-r", str(FPS), "-c:v", "libx264", "-preset", C.PRESET,
                        "-crf", str(C.CRF), "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                        str(tmp_dst)], check=True, cwd=str(work))
        clipper.finish_file(tmp_dst, dst)
    print("done")


if __name__ == "__main__":
    main()
