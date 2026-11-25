# -*- coding: utf-8 -*-
"""
Второй шаблон игрового ролика: хук в начале.

    python хук.py "<готовый ролик>" --hook A-B [--out "<новый файл>"]

Берёт готовый вертикальный ролик (уже с заголовком, субтитрами и заставкой)
и собирает из него:

    1. хук — отрезок A..B этого же ролика, самый яркий момент с реакцией;
    2. перемотку назад от B до начала, сжатую в HOOK_REWIND_S секунд:
       картинка выцветает, идут помехи, по центру значок ◀◀, звук перемотки плёнки;
    3. ролик целиком с самого начала.

A и B — секунды готового ролика (можно с запятой: 7,6-13,1). Без --out ролик
заменяется на месте. В готовый файл пишется метка, и повторный запуск на нём
отказывается работать, чтобы не получилось двух хуков.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True     # чтобы не плодить папки __pycache__

import пути  # первым: переносит кэши и временные файлы в .cache проекта
пути.требовать_окружение()

from PIL import Image, ImageDraw  # noqa: E402

import config as C  # noqa: E402

for _поток in (sys.stdout, sys.stderr):
    try:
        _поток.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ДВИЖОК = Path(__file__).resolve().parent
МЕТКА = "stream-clipper:hook"        # пишется в metadata comment готового файла

# Фрагменты записи перемотки (секунды исходного файла): разгон с самого начала
# и естественная остановка с щелчком. Подобраны под авторскую запись
# перемотки плёнки (HOOK_SOUND); для своей записи числа подобрать заново.
ЗВУК_НАЧАЛО = 0.05
ЗВУК_ОСТАНОВКА = (2.10, 2.70)
ЗВУК_СКЛЕЙКА = 0.06

# Значок ◀◀: размер картинки, ширина одного треугольника, отступ, обводка.
ЗНАЧОК_W, ЗНАЧОК_H = 300, 170
ТРЕУГОЛЬНИК_W, ЗНАЧОК_ОТСТУП, ЗНАЧОК_ОБВОДКА = 140, 10, 8

СТЕРЕО = "aformat=sample_rates=48000:channel_layouts=stereo"


def запустить(cmd):
    """Запускает ffmpeg/ffprobe и падает с понятной ошибкой."""
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        sys.stderr.write((r.stderr or "")[-2000:] + "\n")
        raise SystemExit(f"{cmd[0]} завершился с ошибкой, исходник не тронут")
    return r.stdout + r.stderr


def свойства(файл):
    """Длительность, есть ли звук и метка хука."""
    out = запустить(["ffprobe", "-v", "error", "-show_entries",
                     "format=duration:format_tags=comment:stream=codec_type",
                     "-of", "json", str(файл)])
    данные = json.loads(out)
    формат = данные["format"]
    есть_звук = any(п.get("codec_type") == "audio" for п in данные.get("streams", []))
    метка = (формат.get("tags") or {}).get("comment", "")
    return float(формат["duration"]), есть_звук, метка == МЕТКА


def значок_перемотки(путь):
    """Рисует ◀◀ белыми треугольниками с чёрной обводкой (без шрифтов)."""
    img = Image.new("RGBA", (ЗНАЧОК_W, ЗНАЧОК_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    шаг = ТРЕУГОЛЬНИК_W - 5            # треугольники слегка заходят друг на друга
    for x0 in (ЗНАЧОК_ОТСТУП, ЗНАЧОК_ОТСТУП + шаг):
        tri = [(x0, ЗНАЧОК_H // 2),
               (x0 + ТРЕУГОЛЬНИК_W, ЗНАЧОК_ОТСТУП),
               (x0 + ТРЕУГОЛЬНИК_W, ЗНАЧОК_H - ЗНАЧОК_ОТСТУП)]
        d.polygon(tri, fill=(255, 255, 255, 255), outline=(0, 0, 0, 255),
                  width=ЗНАЧОК_ОБВОДКА)
    img.save(путь)


def средняя_громкость(файл):
    """Средний уровень звука в дБ по volumedetect."""
    замер = запустить(["ffmpeg", "-hide_banner", "-i", str(файл),
                       "-af", "volumedetect", "-f", "null", "-"])
    for строка in замер.splitlines():
        if "mean_volume:" in строка:
            значение = строка.split("mean_volume:")[1].split("dB")[0].strip()
            if значение not in ("-inf", "inf", "nan"):
                return float(значение)
            break
    raise SystemExit("звук перемотки получился пустым, проверьте HOOK_SOUND")


def звук_перемотки(длина, папка):
    """
    Готовит звук перемотки ровно на `длина` секунд или возвращает None.

    Разгон берётся с начала записи такой длины, чтобы вместе с остановкой
    и после замедления хватило на всю перемотку. Громкость приводится
    к HOOK_SOUND_DB по среднему уровню.
    """
    if not C.HOOK_SOUND:
        return None
    исходник = ДВИЖОК / C.HOOK_SOUND
    if not исходник.exists():
        raise SystemExit(f"нет файла звука перемотки: {исходник}")
    медленно = C.HOOK_SOUND_SLOW
    ост0, ост1 = ЗВУК_ОСТАНОВКА
    # Длина исходного звука до замедления = длина перемотки * коэффициент.
    разгон = длина * медленно - (ост1 - ост0) + ЗВУК_СКЛЕЙКА
    if разгон <= ЗВУК_СКЛЕЙКА or ЗВУК_НАЧАЛО + разгон > ост0:
        raise SystemExit(f"перемотка {длина} с не помещается в запись звука, "
                         "поменяйте HOOK_REWIND_S")
    сырой = папка / "звук_сырой.wav"
    готовый = папка / "звук.wav"
    fc = (f"[0:a]aresample=44100,asplit[x][y];"
          f"[x]atrim={ЗВУК_НАЧАЛО}:{ЗВУК_НАЧАЛО + разгон:.3f},asetpts=PTS-STARTPTS[s];"
          f"[y]atrim={ост0}:{ост1},asetpts=PTS-STARTPTS[e];"
          f"[s][e]acrossfade=d={ЗВУК_СКЛЕЙКА},asetrate={44100 * медленно:.0f},"
          f"aresample=48000,aformat=channel_layouts=stereo,afade=t=in:d=0.02,"
          f"apad,atrim=0:{длина}[o]")
    запустить(["ffmpeg", "-y", "-v", "error", "-i", str(исходник),
               "-filter_complex", fc, "-map", "[o]", "-c:a", "pcm_s16le", str(сырой)])
    поправка = C.HOOK_SOUND_DB - средняя_громкость(сырой)
    запустить(["ffmpeg", "-y", "-v", "error", "-i", str(сырой),
               "-af", f"volume={поправка:.2f}dB", "-c:a", "pcm_s16le", str(готовый)])
    return готовый


def собрать(ролик, a, b, итог, папка):
    перемотка = float(C.HOOK_REWIND_S)
    хук = b - a
    значок = папка / "значок.png"
    значок_перемотки(значок)
    звук = звук_перемотки(перемотка, папка)

    # Ролик открывается тремя отдельными входами, а не через split: иначе,
    # пока reverse ждёт конец перемотки, ветка «ролик с начала» копила бы
    # в памяти все кадры (около 3 МБ на кадр 1080x1920).
    входы = ["-ss", str(a), "-t", str(хук), "-i", str(ролик),     # 0: хук
             "-t", str(b), "-i", str(ролик),                       # 1: для перемотки
             "-i", str(ролик),                                     # 2: ролик целиком
             "-loop", "1", "-i", str(значок)]                      # 3: значок ◀◀
    if звук:
        входы += ["-i", str(звук)]                                 # 4: звук перемотки
        fc_звук = f"[4:a]{СТЕРЕО},apad,atrim=0:{перемотка}[ra];"
    else:
        fc_звук = f"anullsrc=r=48000:cl=stereo,atrim=0:{перемотка}[ra];"
    # Звук в ролике есть всегда: main() отказывает роликам без звука.
    fc_хук = (f"[0:a]{СТЕРЕО},asetpts=PTS-STARTPTS,afade=t=in:d=0.03,"
              f"afade=t=out:st={хук - 0.08:.3f}:d=0.08[ha];")
    fc_ролик = f"[2:a]{СТЕРЕО},asetpts=PTS-STARTPTS,afade=t=in:d=0.05[fa];"

    fc = (
        # 1. Хук: короткие затухания звука на краях, чтобы не щёлкало на стыках.
        f"[0:v]setpts=PTS-STARTPTS,setsar=1[hv];" + fc_хук +
        # 2. Перемотка: ролик от B до начала задом наперёд, сжатый во времени.
        # Ускорение и прореживание кадров идут до reverse, поэтому в памяти
        # копится только кадров на HOOK_REWIND_S секунд.
        f"[1:v]setpts=PTS-STARTPTS,setpts=PTS*{перемотка}/{b},fps={C.OUT_FPS},"
        "reverse,hue=s=0.25,eq=contrast=1.15:brightness=0.03,noise=alls=28:allf=t,"
        "drawbox=x=0:y=0:w=iw:h=ih:color=white@0.08:t=fill,"
        f"trim=0:{перемотка},setpts=PTS-STARTPTS[rv0];"
        "[rv0][3:v]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1[rv];" + fc_звук +
        # 3. Ролик с начала.
        "[2:v]setpts=PTS-STARTPTS,setsar=1[fv];" + fc_ролик +
        "[hv][ha][rv][ra][fv][fa]concat=n=3:v=1:a=1[v][a]"
    )
    запустить(["ffmpeg", "-y", "-v", "error", *входы, "-filter_complex", fc,
               "-map", "[v]", "-map", "[a]", "-r", str(C.OUT_FPS),
               "-c:v", "libx264", "-preset", C.PRESET, "-crf", str(C.CRF),
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
               "-metadata", f"comment={МЕТКА}", "-movflags", "+faststart", str(итог)])


def положить(черновик, итог):
    """
    Кладёт готовый файл на место итогового.

    Сначала копия рядом с итогом (в той же папке), затем os.replace: замена
    атомарная, поэтому при обрыве исходный ролик остаётся целым.
    """
    рядом = итог.with_name(итог.name + ".part")
    try:
        shutil.copyfile(черновик, рядом)
        os.replace(рядом, итог)
    except PermissionError:
        raise SystemExit(f"файл занят другой программой, закройте его: {итог.name}")
    finally:
        рядом.unlink(missing_ok=True)


def main():
    разбор = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    разбор.add_argument("ролик", help="готовый вертикальный ролик")
    разбор.add_argument("--hook", required=True, metavar="A-B",
                        help="отрезок хука, секунды готового ролика")
    разбор.add_argument("--out", help="куда сохранить .mp4; без него ролик заменяется")
    арг = разбор.parse_args()

    ролик = Path(арг.ролик).resolve()
    if not ролик.is_file():
        raise SystemExit(f"нет ролика: {ролик}")
    try:
        a, b = (float(x) for x in арг.hook.replace(",", ".").split("-"))
    except ValueError:
        raise SystemExit("--hook пишется как A-B, например 7.6-13.1")
    всего, есть_звук, уже_с_хуком = свойства(ролик)
    if уже_с_хуком:
        raise SystemExit("в этом ролике хук уже есть, второй не добавляю")
    if not есть_звук:
        raise SystemExit("в ролике нет звука: хук собирается только для роликов со звуком")
    if not 0 <= a < b <= всего:
        raise SystemExit(f"отрезок хука {a}-{b} вне ролика длиной {всего:.1f} с")
    if not C.HOOK_MIN_S <= b - a <= C.HOOK_MAX_S:
        raise SystemExit(f"хук {b - a:.1f} с, допустимо от {C.HOOK_MIN_S} "
                         f"до {C.HOOK_MAX_S} с")

    if арг.out:
        итог = Path(арг.out).resolve()
        if итог.suffix.lower() != ".mp4":
            raise SystemExit("--out должен быть файлом .mp4")
        итог.parent.mkdir(parents=True, exist_ok=True)
    else:
        итог = ролик

    # Всё промежуточное — во временной папке проекта, она удаляется сама,
    # в том числе при ошибке. У каждого запуска своя папка.
    with tempfile.TemporaryDirectory(dir=пути.ВРЕМЕННЫЕ, prefix="хук_") as tmp:
        папка = Path(tmp)
        черновик = папка / "ролик.mp4"
        собрать(ролик, a, b, черновик, папка)
        ожидается = (b - a) + C.HOOK_REWIND_S + всего
        получилось = свойства(черновик)[0]
        if abs(получилось - ожидается) > 0.3:
            raise SystemExit(f"ролик с хуком {получилось:.1f} с вместо {ожидается:.1f} с, "
                             "исходник не тронут")
        положить(черновик, итог)
    print(f"хук {a}-{b} с + перемотка {C.HOOK_REWIND_S} с -> {итог.name}")


if __name__ == "__main__":
    main()
