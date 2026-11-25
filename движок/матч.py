# -*- coding: utf-8 -*-
"""
Шаблон /матч: горизонтальный ролик для YouTube (16:9) из записи одной игры Dota 2.

    python матч.py analyze "<матч>"
        расшифровка речи, сигналы Доты и таблица по 10 секунд:
        work/<матч>/матч/таблица.tsv и речь блоками по 10 минут (речь_NN.txt)

    python матч.py sheet "<матч>" --from 600 --to 720 [--step 10]
        лист кадров отрезка (до 16 кадров) — смотреть глазами спорные места

    python матч.py render "<матч>" --plan "<match_plan.json>" --draft
        быстрая проба 480p для себя (пользователю не отдаётся):
        work/<матч>/матч/проба.mp4

    python матч.py notes "<матч>" --plan "<match_plan.json>"
        таймкоды ГОТОВОГО ролика для пометок «спорное» из плана

    python матч.py render "<матч>" --plan "<match_plan.json>" --batch "<имя>"
        чистовик 1080p с заставкой: Clips Ready/<имя>/Матч <матч> YouTube.mp4

    python матч.py check "<готовый ролик>" "<матч>" --plan "<match_plan.json>"
        длина, совпадение звука и картинки в начале, середине и конце ролика

match_plan.json — решения Claude, что оставить:

    {"источник": "twitch-123456789.mp4",
     "куски": [{"с": 35.0, "по": 160.0, "что": "драфт: выбирает героя"}, ...],
     "вырезано": [{"с": 160.0, "по": 300.0, "почему": "загрузка и пустое ожидание"}]}

Как собирается ролик. Видео каждого куска кодируется отдельно, точно по сетке
кадров, и куски склеиваются без перекодирования. Звук собирается ОТДЕЛЬНО одной
непрерывной дорожкой: каждый кусок в WAV ровно под длину его видео (до сэмпла),
с микрофейдами на стыках, затем один раз кодируется в AAC. Если кодировать звук
каждого куска в AAC и склеивать, на сотне стыков набегает рассинхрон в десятки
и сотни миллисекунд к концу ролика. Куски кэшируются: после сбоя рендер
продолжается с того места, где остановился.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import wave
from fractions import Fraction
from pathlib import Path

sys.dont_write_bytecode = True     # чтобы не плодить папки __pycache__

import пути                        # первым: переносит кэши и временные файлы на D
пути.требовать_окружение()

import numpy as np

ДВИЖОК = Path(__file__).resolve().parent
sys.path.insert(0, str(ДВИЖОК))
import clipper                     # noqa: E402
import config as C                 # noqa: E402
import игра                        # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ЧАСТОТА = 48000                    # частота звука ролика


def мм_сс(с):
    с = int(round(с))
    return f"{с // 3600}:{с % 3600 // 60:02d}:{с % 60:02d}" if с >= 3600 else f"{с // 60}:{с % 60:02d}"


def папка_матча(src):
    """work/<запись>/матч/ — всё, что относится к этому матчу."""
    d = clipper.stream_cache(src) / "матч"
    d.mkdir(parents=True, exist_ok=True)
    return d


def свойства(src):
    """Длительность, размер кадра и частота кадров исходника."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,r_frame_rate:format=duration",
                        "-of", "json", str(src)], capture_output=True, text=True, check=True)
    д = json.loads(r.stdout)
    # Частота кадров — точной дробью (60/1, 60000/1001): от неё считается
    # число кадров и сэмплов, и десятичное приближение копило бы ошибку.
    return dict(dur=float(д["format"]["duration"]), w=int(д["streams"][0]["width"]),
                h=int(д["streams"][0]["height"]),
                fps=Fraction(д["streams"][0]["r_frame_rate"]))


# ───────────────────────────── АНАЛИЗ ─────────────────────────────

def серость(src, папка):
    """
    Насыщенность цвета игровой части кадра по опорным кадрам. Экран смерти
    в Доте обесцвечен: насыщенность резко падает.
    """
    файл = папка / "цвет.npz"
    if файл.exists():
        д = np.load(файл)
        return д["t"], д["нас"]
    ш, в = 192, 108
    cmd = ["ffmpeg", "-v", "info", "-nostats", "-skip_frame", "nokey", "-i", str(src),
           "-vf", f"scale={ш}:{в},showinfo", "-an", "-sn", "-fps_mode", "passthrough",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    размер = ш * в * 3
    нас = []
    лог = clipper.job_dir() / "цвет.log"
    with open(лог, "wb") as ош:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ош)
        try:
            while True:
                буфер = p.stdout.read(размер)
                if len(буфер) < размер:
                    break
                к = np.frombuffer(буфер, dtype=np.uint8).reshape(в, ш, 3).astype(np.float32)
                # Игровая часть: без верхней панели, нижней панели героя и краёв.
                к = к[int(в * 0.08):int(в * 0.72), int(ш * 0.2):int(ш * 0.8)]
                мх, мн = к.max(axis=2), к.min(axis=2)
                нас.append(float(((мх - мн) / np.maximum(мх, 1.0)).mean()))
        finally:
            p.stdout.close()
        код = p.wait()
    текст = лог.read_text(encoding="utf-8", errors="replace")
    if код != 0:
        raise SystemExit("ffmpeg не смог прочитать видео:\n" + текст[-1500:])
    t = [float(x) for x in re.findall(r"pts_time:([\d.]+)", текст)]
    n = min(len(t), len(нас))
    t, нас = np.array(t[:n], dtype=np.float32), np.array(нас[:n], dtype=np.float32)
    tmp = clipper.job_dir() / "цвет.npz"
    np.savez(tmp, t=t, нас=нас)
    clipper.finish_file(tmp, файл)
    return t, нас


def cmd_analyze(args):
    src = Path(args.src)
    папка = папка_матча(src)
    св = свойства(src)
    print(f"  матч: {src.name}, {мм_сс(св['dur'])}, {св['w']}x{св['h']} @ {float(св['fps']):g}")

    print("  речь...")
    clipper.cmd_asr(argparse.Namespace(src=str(src), force=False))
    речь = clipper.jload(clipper.stream_cache(src) / "transcript.json")

    print("  сигналы Доты (таймер, счёт, движение)...")
    t, лево, право, таймер, движение = игра.экранные_сигналы(src, clipper.stream_cache(src))
    # Таймер идёт только в самой игре: так отсекаются драфт, загрузка, пауза и меню.
    # Та же логика, что в игра.найти_кандидатов: цифры счёта при этом спокойны.
    тихо = (лево < игра.ПОРОГ_ТИШИНЫ) & (право < игра.ПОРОГ_ТИШИНЫ)
    идёт = np.convolve(((таймер > игра.ПОРОГ_ТАЙМЕРА) & тихо).astype(np.float32),
                       np.ones(9) / 9, mode="full")[4:4 + len(таймер)] > 0.6
    смена_счёта = ((лево > игра.ПОРОГ_СЧЁТА) | (право > игра.ПОРОГ_СЧЁТА)) & идёт
    п = np.percentile(движение[идёт], [50, 95]) if идёт.any() else (1.0, 2.0)
    дв = np.clip((движение - п[0]) / max(п[1] - п[0], 0.1), 0, 1)

    print("  цвет кадра (экран смерти)...")
    tц, нас = серость(src, папка)
    в_игре_нас = нас[np.interp(tц, t, идёт.astype(np.float32)) > 0.5]
    норма = float(np.median(в_игре_нас)) if len(в_игре_нас) else float(np.median(нас))

    db, шаг = clipper.audio_rms(clipper.extract_audio(src), 0.5)

    ш = C.MATCH_BIN
    строки = []
    for i in range(int(np.ceil(св["dur"] / ш))):
        a, b = i * ш, min((i + 1) * ш, св["dur"])
        м = (t >= a) & (t < b)
        мц = (tц >= a) & (tц < b)
        мз = db[int(a / шаг):int(b / шаг)]
        фразы = [s for s in речь if a <= s["start"] < b]
        говорит = sum(max(0.0, min(s["end"], b) - max(s["start"], a))
                      for s in речь if s["end"] > a and s["start"] < b)
        игра_доля = float(идёт[м].mean()) if м.any() else 0.0
        убийств = int(смена_счёта[м].sum())
        серый = bool(мц.any() and float(нас[мц].mean()) < норма * 0.45)
        if игра_доля < 0.5:
            метка = "вне игры"
        elif убийств:
            метка = "файт"
        elif серый:
            метка = "смерть"
        elif говорит / (b - a) < 0.2:
            метка = "игра, молча"
        else:
            метка = "игра"
        строки.append(dict(
            с=a, по=b, метка=метка, игра=round(игра_доля, 2), убийств=убийств,
            движение=round(float(дв[м].mean()) if м.any() else 0.0, 2),
            громк=round(float(мз.mean()) if len(мз) else -80.0, 1),
            пик=round(float(мз.max()) if len(мз) else -80.0, 1),
            речь=round(говорит / (b - a), 2), серый=серый,
            текст=" ".join(s["text"] for s in фразы)))

    clipper.jdump(строки, папка / "таблица.json")
    with open(папка / "таблица.tsv", "w", encoding="utf-8") as f:
        # Текст речи в таблицу не пишется: он есть в речь_NN.txt.
        f.write("время\tметка\tигра\tубийств\tдвиж\tгромк\tпик\n")
        for с in строки:
            f.write(f"{мм_сс(с['с'])}\t{с['метка']}\t{с['игра']}\t{с['убийств']}\t"
                    f"{с['движение']}\t{с['громк']}\t{с['пик']}\n")
    # Речь целиком блоками по 10 минут: так её читают по частям, а решения
    # по каждому блоку сразу пишут в match_plan.json.
    for n in range(int(np.ceil(св["dur"] / 600))):
        a, b = n * 600, (n + 1) * 600
        with open(папка / f"речь_{n + 1:02d}.txt", "w", encoding="utf-8") as f:
            f.write(f"# {мм_сс(a)}–{мм_сс(min(b, св['dur']))}\n")
            for s in речь:
                if a <= s["start"] < b:
                    f.write(f"{мм_сс(s['start'])}  {s['text']}\n")

    # Сразу кандидаты в вырез и листы для их проверки. Это помощь, а не
    # основная работа: её сбой не должен ронять анализ.
    try:
        cmd_suggest(argparse.Namespace(src=str(src)))
    except Exception as e:
        print(f"  вырезы не предложены: {e}")
    в_игре = [с for с in строки if с["метка"] != "вне игры"]
    print(f"\n  таблица: {папка / 'таблица.tsv'} ({len(строки)} строк по {ш} с)")
    print(f"  речь блоками: {папка}/речь_NN.txt")
    if в_игре:
        print(f"  игра идёт: {мм_сс(в_игре[0]['с'])} – {мм_сс(в_игре[-1]['по'])}")
    print(f"  файтов (смена счёта): {sum(с['метка'] == 'файт' for с in строки)} отрезков, "
          f"смертей (серый экран): {sum(с['метка'] == 'смерть' for с in строки)}")


# ───────────────────────────── ПРЕДЛОЖЕНИЕ ВЫРЕЗОВ ─────────────────────────────

def плотность_речи(речь, строки):
    """
    Символов речи на строку таблицы. Сегменты расшифровки бывают длиной
    до полутора минут и перекрывают паузы, поэтому доля «говорит» почти
    всегда 1.0; плотность текста различает тишину и разговор лучше.
    """
    пл = np.zeros(len(строки))
    for s in речь:
        длина = max(s["end"] - s["start"], 0.5)
        n = len(s["text"].strip())
        for i, с in enumerate(строки):
            о = min(s["end"], с["по"]) - max(s["start"], с["с"])
            if о > 0:
                пл[i] += n * о / длина
    return пл


def предложить_вырезы(строки, пл):
    """
    Кандидаты в вырез для формата «игра без мёртвого времени»: подряд идущие
    строки без речи и без смены счёта рядом, плюс паузы посреди игры. Это
    только кандидаты: каждый Claude проверяет по кадрам и речи.
    """
    n = len(строки)
    в_игре = [i for i, с in enumerate(строки) if с["метка"] != "вне игры"]
    if not в_игре:
        return []            # игра не распознана: предлагать нечего
    начало, конец = в_игре[0], в_игре[-1]
    def драка_рядом(i):
        return any(строки[j]["убийств"] for j in range(max(0, i - 1), min(n, i + 2)))
    мёртвая = []
    for i, с in enumerate(строки):
        тихо = пл[i] < C.MATCH_QUIET_CHARS
        if начало <= i <= конец and с["метка"] == "вне игры" and not драка_рядом(i):
            мёртвая.append("пауза" if not тихо else "пауза, тишина")
        elif тихо and not драка_рядом(i) and начало <= i <= конец:
            мёртвая.append("смерть, тишина" if с["метка"] == "смерть" else "тишина")
        elif тихо and (i < начало or i > конец):
            мёртвая.append("меню, тишина")
        else:
            мёртвая.append(None)
    кандидаты, i = [], 0
    while i < n:
        if not мёртвая[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and мёртвая[j + 1]:
            j += 1
        a, b = строки[i]["с"], строки[j]["по"]
        if b - a >= C.MATCH_MIN_CUT:
            почему = sorted({м for м in мёртвая[i:j + 1]}, key=мёртвая[i:j + 1].index)
            кандидаты.append(dict(с=a, по=b, почему=", ".join(почему)))
        i = j + 1
    return кандидаты


def cmd_suggest(args):
    src = Path(args.src)
    папка = папка_матча(src)
    строки = clipper.jload(папка / "таблица.json")
    речь = clipper.jload(clipper.stream_cache(src) / "transcript.json")
    пл = плотность_речи(речь, строки)
    кандидаты = предложить_вырезы(строки, пл)
    всего = sum(к["по"] - к["с"] for к in кандидаты)
    with open(папка / "вырезы.txt", "w", encoding="utf-8") as f:
        f.write(f"# кандидаты в вырез: {len(кандидаты)}, всего {мм_сс(всего)}\n"
                f"# проверить каждый по кадрам (sheet) и речи вокруг; спорное — оставить\n")
        for к in кандидаты:
            рядом = " ".join(s["text"].strip() for s in речь
                             if s["end"] > к["с"] - 10 and s["start"] < к["по"] + 10)
            f.write(f"\n{мм_сс(к['с'])}–{мм_сс(к['по'])}  ({к['по'] - к['с']:.0f} с)  {к['почему']}\n")
            if рядом:
                f.write(f"  речь рядом: {рядом[:300]}\n")
    print(f"  кандидатов в вырез: {len(кандидаты)}, всего {мм_сс(всего)} -> {папка / 'вырезы.txt'}")
    # Листы проверки: строка = кандидат, 4 кадра от начала до конца отрезка,
    # 4 кандидата на лист. Если src нет (запись удалена), листы не рисуются.
    for старый in (папка / "листы").glob("вырезы_*.jpg"):
        старый.unlink()          # листы прошлого запуска сбили бы проверку
    if src.is_file():
        for n in range(0, len(кандидаты), 4):
            ряды = [[к["с"] + (к["по"] - к["с"]) * д for д in (0.1, 0.37, 0.63, 0.9)]
                    for к in кандидаты[n:n + 4]]
            print(нарисовать_лист(src, [м for р in ряды for м in р],
                                  папка / "листы" / f"вырезы_{n // 4 + 1}.jpg"))


# ───────────────────────────── ЛИСТ КАДРОВ ─────────────────────────────

def cmd_sheet(args):
    src = Path(args.src)
    шаг = max(1.0, float(args.step))
    моменты = list(np.arange(float(args.from_), float(args.to) + 0.01, шаг))[:16]
    if not моменты:
        raise SystemExit("пустой отрезок: --from должен быть меньше --to")
    print(нарисовать_лист(src, моменты, папка_матча(src) / "листы"
                          / f"лист_{int(моменты[0])}-{int(моменты[-1])}.jpg"))


def нарисовать_лист(src, моменты, итог):
    """Лист кадров 4 в ряд с таймкодом исходника на каждом кадре."""
    from PIL import Image, ImageDraw, ImageFont
    ш, в, колонок = 480, 270, 4
    лист = Image.new("RGB", (ш * колонок, в * ((len(моменты) + колонок - 1) // колонок)))
    шрифт = ImageFont.truetype(str(clipper.FONTS / C.FONT_FILE), 26)
    for i, м in enumerate(моменты):
        кадр = clipper.job_dir() / f"кадр_{i}.jpg"
        clipper.sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{м:.2f}", "-i", str(src),
                    "-frames:v", "1", "-vf", f"scale={ш}:{в}", "-q:v", "4", str(кадр)])
        if not кадр.exists():
            continue
        к = Image.open(кадр).convert("RGB")
        р = ImageDraw.Draw(к)
        р.rectangle([0, 0, 110, 36], fill=(0, 0, 0))
        р.text((6, 3), мм_сс(м), font=шрифт, fill=(255, 230, 0))
        лист.paste(к, ((i % колонок) * ш, (i // колонок) * в))
    итог.parent.mkdir(exist_ok=True)
    лист.save(итог, quality=85)
    return итог


# ───────────────────────────── СБОРКА ─────────────────────────────

def прочитать_план(путь, src, длина, fps):
    """Куски из плана: внутри записи, по порядку, без наложений, по сетке кадров."""
    if not Path(путь).is_file():
        raise SystemExit(f"нет файла плана: {путь}")
    try:
        план = json.loads(Path(путь).read_text(encoding="utf-8"))
    except ValueError as e:
        raise SystemExit(f"план не читается как JSON: {e}")
    if not isinstance(план, dict):
        raise SystemExit("план должен быть JSON-объектом с ключом «куски»")
    # План от другого матча собрал бы ролик молча из чужих таймкодов.
    источник = план.get("источник")
    if источник and источник != Path(src).name:
        raise SystemExit(f"план составлен для «{источник}», а исходник «{Path(src).name}»")
    куски = []
    for к in план.get("куски", []):
        try:
            a, b = (к["с"], к["по"]) if isinstance(к, dict) else к
            a, b = float(a), float(b)
        except (KeyError, TypeError, ValueError):
            raise SystemExit(f"кусок плана без чисел «с» и «по»: {к}")
        if b <= a:
            raise SystemExit(f"кусок {мм_сс(a)}: конец раньше начала")
        if a < 0 or a >= длина:
            raise SystemExit(f"кусок {мм_сс(a)} за пределами записи")
        # Начало и длина — целое число кадров.
        кадр_а = round(Fraction(a) * fps)
        n = int(round((Fraction(min(b, длина)) - Fraction(кадр_а) / fps) * fps))
        if n / fps < C.MATCH_MIN_KEEP:
            raise SystemExit(f"кусок {мм_сс(a)} короче {C.MATCH_MIN_KEEP} с — дёрганый монтаж")
        куски.append((кадр_а, n))
    if not куски:
        raise SystemExit("в плане нет кусков")
    for (к1, n1), (к2, _) in zip(куски, куски[1:]):
        if к2 < к1 + n1:
            raise SystemExit(f"куски идут не по порядку или налезают: "
                             f"{мм_сс(к1 / fps)} и {мм_сс(к2 / fps)}")
    return куски


def сэмплы_до(кадров, fps):
    """Сколько сэмплов звука приходится на первые `кадров` кадров ролика (точно)."""
    return int(round(Fraction(кадров) * ЧАСТОТА / fps))


def кадров_в(файл):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                        "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(файл)],
                       capture_output=True, text=True, check=True)
    return int(r.stdout.strip().split(",")[0])


def записать_звук(сырой, итог, начало_кадра, кадров, fps):
    """
    Звук куска ровно под его видео: обрезать или дополнить тишиной до нужного
    числа сэмплов (с учётом накопленного округления) и сгладить края.
    """
    нужно = сэмплы_до(начало_кадра + кадров, fps) - сэмплы_до(начало_кадра, fps)
    with wave.open(str(сырой), "rb") as w:
        x = np.frombuffer(w.readframes(min(w.getnframes(), нужно)), dtype=np.int16).reshape(-1, 2)
    if len(x) < нужно:
        x = np.vstack([x, np.zeros((нужно - len(x), 2), dtype=np.int16)])
    x = x.astype(np.float32)
    ф = min(int(C.MATCH_FADE * ЧАСТОТА), len(x) // 2)
    if ф > 0:
        k = np.linspace(0.0, 1.0, ф, dtype=np.float32)[:, None]
        x[:ф] *= k
        x[-ф:] *= k[::-1]
    with wave.open(str(итог), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(ЧАСТОТА)
        w.writeframes(np.clip(x, -32768, 32767).astype(np.int16).tobytes())


def видео_параметры(режим, fps):
    if режим == "draft":
        д = C.MATCH_DRAFT
        return dict(w=д["w"], h=д["h"], fps=Fraction(д["fps"]), preset=д["preset"], crf=д["crf"])
    return dict(w=C.MATCH_W, h=C.MATCH_H, fps=fps, preset=C.MATCH_PRESET, crf=C.MATCH_CRF)


def дробь(fps):
    return f"{fps.numerator}/{fps.denominator}"


def кодек(п):
    # Одинаковые параметры у всех кусков и заставки: только так их можно
    # склеить без перекодирования. Шкала времени кратна частоте кадров,
    # чтобы у каждого кадра была целая длительность (и при 60000/1001).
    шкала = п["fps"].numerator * (100 if п["fps"].denominator == 1 else 1)
    return ["-c:v", "libx264", "-preset", п["preset"], "-crf", str(п["crf"]),
            "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-video_track_timescale", str(шкала)]


def вписать(п):
    """Масштаб в кадр ролика без растяжения: запись не 16:9 получит поля."""
    W, H = п["w"], п["h"]
    return (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1")


def собрать_кусок(src, кадр_а, кадров, п, кэш):
    """
    Один кусок: видео .mp4 без звука и сырой звук .wav. Кэшируется по началу
    и длине, поэтому после правки плана пересобираются только новые куски.
    """
    имя = f"{кадр_а}_{кадров}"
    видео, звук = кэш / f"{имя}.mp4", кэш / f"{имя}.wav"
    if видео.exists() and звук.exists():
        return видео, звук
    a = float(Fraction(кадр_а) / п["fps"])
    L = float(Fraction(кадров) / п["fps"])
    pad = min(float(C.SEEK_PAD), a)
    tmp_v, tmp_a = clipper.job_dir() / "кусок.mp4", clipper.job_dir() / "кусок.wav"
    # Перемотка на SEEK_PAD раньше и резка по меткам — как в clipper.render_clip:
    # на записях Twitch перемотка прыгает к опорному кадру, и без запаса
    # картинка уезжает относительно звука.
    граф = (f"[0:v]trim=start={pad:.6f}:end={pad + L:.6f},setpts=PTS-STARTPTS,"
            f"fps={дробь(п['fps'])},{вписать(п)}[v];"
            f"[0:a]atrim=start={pad:.6f}:end={pad + L + 0.5:.6f},asetpts=PTS-STARTPTS,"
            f"aresample={ЧАСТОТА},aformat=sample_fmts=s16:channel_layouts=stereo[a]")
    clipper.sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{a - pad:.6f}", "-t", f"{pad + L + 2:.6f}",
                "-i", str(Path(src).resolve()), "-filter_complex", граф,
                "-map", "[v]", "-frames:v", str(кадров), *кодек(п), "-an", str(tmp_v),
                "-map", "[a]", "-c:a", "pcm_s16le", str(tmp_a)])
    вышло = кадров_в(tmp_v)
    if вышло < кадров - 1:
        print(f"    ВНИМАНИЕ: кусок {мм_сс(a)} дал {вышло} кадров из {кадров} (конец записи?)")
    clipper.finish_file(tmp_v, видео)
    clipper.finish_file(tmp_a, звук)
    return видео, звук


def собрать_заставку(п, кэш):
    """
    Заставка в кадр ролика. Горизонтальная вписывается целиком; узкая
    (вертикальная) встаёт по центру на своём же размытом тёмном фоне.
    """
    исходник = ДВИЖОК / C.MATCH_OUTRO
    if not исходник.exists():
        raise SystemExit(f"нет файла заставки: {исходник}")
    # Имя в кэше зависит от файла заставки: после замены заставки старая
    # копия из кэша не подхватится.
    ст = исходник.stat()
    метка = hashlib.sha1(f"{исходник.name}|{ст.st_size}|{ст.st_mtime_ns}".encode()).hexdigest()[:8]
    видео, звук = кэш / f"заставка_{метка}.mp4", кэш / f"заставка_{метка}.wav"
    if видео.exists() and звук.exists():
        return видео, звук
    W, H = п["w"], п["h"]
    tmp_v, tmp_a = clipper.job_dir() / "заставка.mp4", clipper.job_dir() / "заставка.wav"
    кадр = свойства(исходник)
    if кадр["w"] * H >= кадр["h"] * W:
        граф = f"[0:v]fps={дробь(п['fps'])},{вписать(п)}[v]"
    else:
        граф = (f"[0:v]fps={дробь(п['fps'])},split=2[b][f];"
                f"[b]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"gblur=sigma={C.BLUR_SIGMA},eq=brightness=-{C.BLUR_DARKEN},setsar=1[bb];"
                f"[f]scale=-2:{H}:flags=lanczos,setsar=1[ff];"
                f"[bb][ff]overlay=(W-w)/2:0[v]")
    if clipper.has_audio(исходник):
        граф += f";[0:a]aresample={ЧАСТОТА},aformat=sample_fmts=s16:channel_layouts=stereo[a]"
        звуковой = ["-map", "[a]", "-c:a", "pcm_s16le", str(tmp_a)]
    else:
        звуковой = []
    clipper.sh(["ffmpeg", "-y", "-v", "error", "-i", str(исходник), "-filter_complex", граф,
                "-map", "[v]", *кодек(п), "-an", str(tmp_v), *звуковой])
    if not звуковой:
        # Немая заставка — тишина; нужная длина добьётся при сборке дорожки.
        with wave.open(str(tmp_a), "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(ЧАСТОТА)
    clipper.finish_file(tmp_v, видео)
    clipper.finish_file(tmp_a, звук)
    return видео, звук


def усиление(wav):
    """
    Постоянное усиление на весь ролик (не компрессия): соотношение голоса,
    игры и музыки остаётся как на стриме. Цель -14 LUFS; если на такой
    громкости лимитер срезал бы заметную долю звука (файты «плющит»),
    цель снижается до -16, затем -18.
    """
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(wav), "-af",
                        "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    txt = r.stderr or ""
    try:
        вход = float(json.loads(txt[txt.rfind("{"):txt.rfind("}") + 1])["input_i"])
    except (ValueError, KeyError):
        print("    замер громкости не удался, громкость оставляю как есть")
        return 0.0
    if вход < -70:
        return 0.0
    # Усиление для каждой цели, зажатое в разумные пределы.
    цели = {ц: round(min(30.0, max(-10.0, ц - вход)), 2) for ц in C.MATCH_LOUDNESS}
    # Сколько сэмплов после усиления упрётся в потолок лимитера.
    счёт, всего = dict.fromkeys(цели, 0), 0
    with wave.open(str(wav), "rb") as w:
        while True:
            кусок = w.readframes(ЧАСТОТА * 60)
            if not кусок:
                break
            а = np.abs(np.frombuffer(кусок, dtype=np.int16).astype(np.int32))
            всего += len(а)
            for ц, g in цели.items():
                счёт[ц] += int((а > C.MATCH_LIMIT * 32768 / 10 ** (g / 20)).sum())
    for ц in C.MATCH_LOUDNESS:
        доля = счёт[ц] / max(всего, 1)
        if доля <= C.MATCH_LIMIT_SHARE or ц == C.MATCH_LOUDNESS[-1]:
            print(f"    громкость: было {вход:.1f} LUFS, цель {ц}, усиление {цели[ц]:+.1f} дБ, "
                  f"лимитер срежет {доля * 100:.2f} % звука")
            return цели[ц]


def cmd_render(args):
    import shutil
    src = Path(args.src)
    if not src.is_file():
        raise SystemExit(f"нет файла: {src}")
    св = свойства(src)
    режим = "draft" if args.draft else "final"
    п = видео_параметры(режим, св["fps"])
    куски = прочитать_план(args.plan, src, св["dur"], п["fps"])
    # Кэш кусков — один на режим и параметры кадра: после правки плана
    # пересобираются только новые куски, а старые, которых нет в плане,
    # удаляются после успешной сборки (иначе диск забьётся копиями).
    ключ = hashlib.sha1(json.dumps([режим, п["w"], п["h"], дробь(п["fps"]), п["preset"],
                                    п["crf"]]).encode()).hexdigest()[:10]
    кэш = папка_матча(src) / f"куски_{режим}_{ключ}"
    кэш.mkdir(exist_ok=True)

    итог_кадров = sum(n for _, n in куски)
    print(f"  {режим}: {len(куски)} кусков, {мм_сс(итог_кадров / п['fps'])} без заставки, "
          f"{п['w']}x{п['h']} @ {float(п['fps']):g}")
    t0 = time.time()
    jd = clipper.job_dir()
    части, начало = [], 0          # (видео, сырой звук, первый кадр, кадров)
    for i, (кадр_а, n) in enumerate(куски):
        видео, сырой = собрать_кусок(src, кадр_а, n, п, кэш)
        кадров = кадров_в(видео)
        части.append((видео, сырой, начало, кадров))
        начало += кадров
        if (i + 1) % 10 == 0 or i + 1 == len(куски):
            print(f"    кусков {i + 1}/{len(куски)}, {time.time() - t0:.0f} с", flush=True)
    # Заставка и в пробе: она должна выглядеть как готовый ролик.
    заставка = bool(C.MATCH_OUTRO and not args.nooutro)
    if заставка:
        видео, сырой = собрать_заставку(п, кэш)
        кадров = кадров_в(видео)
        части.append((видео, сырой, начало, кадров))

    # Видео — склейка без перекодирования, звук — одна непрерывная дорожка,
    # в которой у каждого куска ровно столько сэмплов, сколько у него кадров.
    список = jd / "список.txt"
    список.write_text("".join(
        "file '" + v.resolve().as_posix().replace("'", "'\\''") + "'\n"
        for v, _, _, _ in части), encoding="utf-8")
    клей = jd / "видео.mp4"
    clipper.sh(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", str(список), "-c", "copy", str(клей)])
    # Усиление считается только по игре: заставка уже выровнена по громкости
    # (MATCH_OUTRO), поэтому её звук ослабляется ровно на усиление ролика —
    # после общего volume=g она звучит на своём уровне, а не на +15–20 дБ.
    игра = части[:-1] if заставка else части
    дорожка, звук_куска = jd / "звук.wav", jd / "звук_куска.wav"
    игра_wav = jd / "звук_игры.wav"
    with wave.open(str(игра_wav), "wb") as out:
        out.setnchannels(2); out.setsampwidth(2); out.setframerate(ЧАСТОТА)
        for _, сырой, нач, кадров in игра:
            записать_звук(сырой, звук_куска, нач, кадров, п["fps"])
            with wave.open(str(звук_куска), "rb") as w:
                out.writeframes(w.readframes(w.getnframes()))
    g = усиление(игра_wav)
    if заставка:
        _, сырой, нач, кадров = части[-1]
        записать_звук(сырой, звук_куска, нач, кадров, п["fps"])
        with wave.open(str(звук_куска), "rb") as w:
            x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        x *= 10 ** (-g / 20)
        with wave.open(str(дорожка), "wb") as out, wave.open(str(игра_wav), "rb") as src_w:
            out.setnchannels(2); out.setsampwidth(2); out.setframerate(ЧАСТОТА)
            while кусок := src_w.readframes(ЧАСТОТА * 60):
                out.writeframes(кусок)
            # Ослабленная заставка в int16 теряет младшие биты, но после
            # обратного усиления это ниже уровня шума исходника (-18 LUFS).
            out.writeframes(np.clip(np.round(x), -32768, 32767).astype(np.int16).tobytes())
    else:
        дорожка = игра_wav
    готовый = jd / "ролик.mp4"
    clipper.sh(["ffmpeg", "-y", "-v", "error", "-i", str(клей), "-i", str(дорожка),
                "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                "-af", f"volume={g}dB,alimiter=limit={C.MATCH_LIMIT}:level=disabled",
                "-c:a", "aac", "-b:a", f"{C.MATCH_AUDIO_KBPS}k", "-ar", str(ЧАСТОТА),
                "-movflags", "+faststart", str(готовый)])

    if режим == "draft":
        # Проба только для себя: черновики пользователю не отдаются
        # (решение 2026-11-20 — сразу чистовик, правит стример сам).
        итог = папка_матча(src) / "проба.mp4"
    else:
        имя = args.batch or clipper.batch_name("Матч", src)
        итог = clipper.batch_dir(src, имя) / f"Матч {src.stem} YouTube.mp4"
    try:
        clipper.finish_file(готовый, итог)
    except PermissionError:
        # Старый файл открыт в плеере или пересылается: не терять
        # готовую работу, положить рядом под другим именем.
        n = 2
        while (запасной := итог.with_name(f"{итог.stem} {n}.mp4")).exists():
            n += 1
        clipper.finish_file(готовый, запасной)
        print(f"  ВНИМАНИЕ: «{итог.name}» занят другой программой, сохранил как «{запасной.name}»")
        итог = запасной
    # Убрать из кэша куски, которых в этом плане уже нет, и кэши других
    # параметров того же режима.
    нужные = {p.name for v, s, _, _ in части for p in (v, s)}
    for f in кэш.iterdir():
        if f.name not in нужные:
            f.unlink(missing_ok=True)
    for d in папка_матча(src).glob(f"куски_{режим}_*"):
        if d != кэш:
            shutil.rmtree(d, ignore_errors=True)
    print(f"\n  готово за {(time.time() - t0) / 60:.1f} мин: {итог}")
    print(f"  длина {мм_сс(длина_файла(итог))}, {итог.stat().st_size / 1e9:.2f} ГБ")


# ───────────────────────────── ПРОВЕРКА ─────────────────────────────

def длина_файла(f, поток=None):
    cmd = ["ffprobe", "-v", "error"]
    if поток:
        cmd += ["-select_streams", поток, "-show_entries", "stream=duration"]
    else:
        cmd += ["-show_entries", "format=duration"]
    r = subprocess.run(cmd + ["-of", "csv=p=0", str(f)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip().split("\n")[0])
    except ValueError:
        return 0.0


def звук_отрезка(f, a, d):
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{a:.3f}", "-t", f"{d:.3f}", "-i", str(f),
                        "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32)


def кадр(f, a):
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{a:.3f}", "-i", str(f), "-frames:v", "1",
                        "-vf", "scale=160:90", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype=np.uint8).astype(np.float32)


def сдвиг_звука(x, y, запас):
    """Где в y лучше всего совпадает x (нормированная корреляция), мс от «запаса»."""
    x = (x - x.mean()) / (x.std() + 1e-6)
    лучшее, где = -2.0, 0
    for i in range(0, len(y) - len(x) + 1, 4):
        окно = y[i:i + len(x)]
        с = float(np.dot(x, (окно - окно.mean()) / (окно.std() + 1e-6))) / len(x)
        if с > лучшее:
            лучшее, где = с, i
    return (где / 8000 - запас) * 1000


def cmd_check(args):
    ролик, src = Path(args.video), Path(args.src)
    св = свойства(src)
    fps = свойства(ролик)["fps"]
    куски = прочитать_план(args.plan, src, св["dur"], fps)
    дв, дз = длина_файла(ролик, "v:0"), длина_файла(ролик, "a:0")
    print(f"  видео {дв:.3f} с, звук {дз:.3f} с, разница {abs(дв - дз) * 1000:.0f} мс")
    ок = abs(дв - дз) < 2.0 / float(fps) + 0.05
    # Сверка с исходником в начале, середине и конце ролика.
    точки, t = [], 0.0
    for кадр_а, n in куски:
        точки.append((float(кадр_а / fps), t, float(n / fps)))
        t += float(n / fps)
    for a, t_out, L in [точки[0], точки[len(точки) // 2], точки[-1]]:
        м = min(1.0, L / 3)
        # Звук: где в исходнике лучше всего совпадает отрезок ролика.
        x = звук_отрезка(ролик, t_out + м, 1.0)
        y = звук_отрезка(src, a + м - 0.2, 1.4)
        if len(x) < 4000 or len(y) < len(x) or x.std() < 1:
            print(f"    {мм_сс(t_out)}: тихо, звук не сверить")
            сдвиг_з = None
        else:
            сдвиг_з = сдвиг_звука(x, y, 0.2)
        # Картинка: какой кадр исходника рядом ближе всего к кадру ролика.
        к = кадр(ролик, t_out + м)
        отличия = [(abs(кадр(src, a + м + d / float(fps)) - к).mean(), d) for d in range(-6, 7)]
        сдвиг_в = min(отличия)[1] * 1000 / float(fps)
        сз = "?" if сдвиг_з is None else f"{сдвиг_з:+.0f} мс"
        print(f"    {мм_сс(t_out)} ролика (исходник {мм_сс(a)}): звук {сз}, картинка {сдвиг_в:+.0f} мс")
        if сдвиг_з is not None and abs(сдвиг_з - сдвиг_в) > 45:
            ок = False
    print("  ИТОГ: " + ("звук и картинка совпадают" if ок else "ЕСТЬ РАСХОЖДЕНИЕ, смотреть глазами"))
    if not ок:
        sys.exit(1)


def в_ролике(t, куски, fps):
    """
    Секунда исходника -> секунда готового ролика. Если t попала в вырезанное,
    берётся начало следующего оставленного куска; после последнего — конец.
    """
    позиция = 0
    for кадр_а, n in куски:
        a, b = кадр_а / fps, (кадр_а + n) / fps
        if t < a:
            return float(позиция / fps)
        if t <= b:
            return float((позиция + (Fraction(t) - a) * fps) / fps)
        позиция += n
    return float(позиция / fps)


def cmd_notes(args):
    """
    Пометки для стримера: спорные места остаются в ролике, а пользователь
    получает их таймкоды В ГОТОВОМ ролике, чтобы стример сам решил,
    вырезать ли (решение пользователя 2026-11-20).
    """
    src = Path(args.src)
    св = свойства(src)
    fps = св["fps"]
    куски = прочитать_план(args.plan, src, св["dur"], fps)
    план = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    спорное = план.get("спорное", [])
    if not спорное:
        print("  спорных мест в плане нет")
        return
    for с in спорное:
        try:
            a = в_ролике(float(с["с"]), куски, fps)
            b = в_ролике(float(с["по"]), куски, fps)
        except (KeyError, TypeError, ValueError):
            raise SystemExit(f"спорное: нужны числа «с» и «по»: {с}")
        if b - a < 1:
            print(f"  ({мм_сс(float(с['с']))}–{мм_сс(float(с['по']))} исходника не вошло в ролик)")
            continue
        пояснение = " — ".join(x for x in (с.get("что"), с.get("почему")) if x)
        print(f"  {мм_сс(a)}–{мм_сс(b)}  {пояснение}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("analyze", help="речь, сигналы Доты, таблица по 10 с")
    p.add_argument("src")
    p = sub.add_parser("suggest", help="кандидаты в вырез: тишина, паузы, меню")
    p.add_argument("src")
    p = sub.add_parser("sheet", help="лист кадров отрезка")
    p.add_argument("src")
    p.add_argument("--from", dest="from_", type=float, required=True, metavar="СЕК")
    p.add_argument("--to", type=float, required=True, metavar="СЕК")
    p.add_argument("--step", type=float, default=10.0, metavar="СЕК")
    p = sub.add_parser("render", help="собрать ролик по плану")
    p.add_argument("src")
    p.add_argument("--plan", required=True, metavar="match_plan.json")
    p.add_argument("--draft", action="store_true",
                   help="быстрая проба 480p для себя, в work/<матч>/матч/проба.mp4")
    p.add_argument("--batch", default=None, metavar="ИМЯ",
                   help="папка в Clips Ready (из clipper.py batchname --kind Матч)")
    p.add_argument("--nooutro", action="store_true", help="без заставки в конце")
    p = sub.add_parser("notes", help="таймкоды готового ролика для спорных мест")
    p.add_argument("src")
    p.add_argument("--plan", required=True, metavar="match_plan.json")
    p = sub.add_parser("check", help="длина и синхрон готового ролика")
    p.add_argument("video")
    p.add_argument("src")
    p.add_argument("--plan", required=True, metavar="match_plan.json")
    args = ap.parse_args()
    {"analyze": cmd_analyze, "sheet": cmd_sheet, "render": cmd_render,
     "check": cmd_check, "notes": cmd_notes, "suggest": cmd_suggest}[args.cmd](args)


if __name__ == "__main__":
    main()
