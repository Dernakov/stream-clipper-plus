# -*- coding: utf-8 -*-
"""
Скачать запись стрима по ссылке. Twitch, Kick, YouTube.

    python скачать.py "https://www.twitch.tv/videos/123456789" --папка "../СТРИМЫ"
    python скачать.py "https://kick.com/КАНАЛ/videos/<uuid>"
    python скачать.py "https://kick.com/КАНАЛ" --список
    python скачать.py "D:\\уже-скачанный.mp4"      просто показать, что за файл

Флаги:
    --инфо            ничего не качать, только показать доступные форматы
    --список          последние трансляции канала (Kick)
    --папка ПУТЬ      куда класть, ОБЯЗАТЕЛЬНО: СТРИМЫ (стрим для мемов),
                      СЫРЫЕ МАТЧИ (матч), СЫРЫЕ КЛИПЫ (клип Twitch для файтов)
    --качество X      1080p60 (по умолчанию), 720p60, 480p...
    --имя X           имя выходного файла без расширения
    --скорость X      лимит скорости закачки, например 900K или 2M

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ, а не голый yt-dlp: у Kick сломан адрес записи.
Ссылка вида kick.com/КАНАЛ/videos/<uuid> содержит id НОВОГО формата (UUIDv7,
начинается на 01...), а API, в который стучится yt-dlp, знает только старый.
Получается 404 и падение в generic-экстрактор. Обход: спросить у API v2
список трансляций канала, найти нужную и взять оттуда video.uuid — это
старый формат, с ним yt-dlp отрабатывает штатно. Проверено вживую.

Здесь же проверяется is_live: пока стрим идёт, скачается только огрызок,
и понять это постфактум трудно.
"""
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import пути  # первым: переносит кэши и временные файлы в .cache проекта
пути.требовать_окружение()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ДВИЖОК = Path(__file__).resolve().parent
ПРОЕКТ = ДВИЖОК.parent
СТРИМЫ = ПРОЕКТ / "СТРИМЫ"

БРАУЗЕР = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")


def запрос(url, таймаут=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": БРАУЗЕР,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=таймаут) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def чч_мм(секунд):
    секунд = int(секунд or 0)
    if секунд < 60:
        return "%d сек" % секунд
    if секунд < 3600:
        return "%d мин %02d сек" % (секунд // 60, секунд % 60)
    return "%d ч %02d мин" % (секунд // 3600, секунд % 3600 // 60)


def вес(байт):
    гб = байт / 1024 ** 3
    return "%.2f ГБ" % гб if гб >= 1 else "%.0f МБ" % (байт / 1024 ** 2)


# ------------------------------------------------------------------ Kick

def kick_канал(url):
    m = re.search(r"kick\.com/([^/?#]+)", url)
    return m.group(1) if m else None


def kick_видео_канала(канал):
    try:
        return запрос("https://kick.com/api/v2/channels/%s/videos" % канал)
    except urllib.error.HTTPError as e:
        print("  Kick ответил %s. Обычно это защита Cloudflare." % e.code)
        print("  Тогда остаётся скачать вручную и положить файл в СТРИМЫ/.")
    except Exception as e:
        print("  Не достучался до Kick: %s" % e)
    return None


def kick_показать_список(канал, записи):
    print("")
    print("  Последние трансляции канала %s:" % канал)
    print("")
    for i, з in enumerate(записи[:15], 1):
        видео = з.get("video") or {}
        дата = (з.get("start_time") or з.get("created_at") or "")[:16].replace("T", " ")
        живой = "  [ИДЁТ ПРЯМО СЕЙЧАС]" if з.get("is_live") else ""
        название = (з.get("session_title") or "")[:48]
        print("  %2d. %s  %10s  %s%s" % (
            i, дата, чч_мм((з.get("duration") or 0) / 1000), название, живой))
        print("      https://kick.com/%s/videos/%s" % (канал, видео.get("uuid", "?")))
    print("")


def kick_разрешить(url):
    """
    Вернуть ссылку, которую поймёт yt-dlp, и заготовку имени файла.
    Новый id из адресной строки подменяем на старый video.uuid из API.
    """
    канал = kick_канал(url)
    if not канал:
        return url, None
    записи = kick_видео_канала(канал)
    if not записи:
        print("  Пробую ссылку как есть — вдруг повезёт.")
        return url, канал

    m = re.search(r"/videos/([0-9a-fA-F-]{20,})", url)
    новый_id = m.group(1) if m else None

    нужная = None
    if новый_id:
        # Не гадаем, в каком поле лежит новый id, — ищем его во всей записи.
        for з in записи:
            if новый_id.lower() in json.dumps(з).lower():
                нужная = з
                break
    if нужная is None:
        if новый_id:
            print("  Этой записи нет в свежем списке канала (%d шт.) — "
                  "видимо, она старая." % len(записи))
            kick_показать_список(канал, записи)
            print("  Возьми ссылку из списка выше или скачай вручную.")
            return None, канал
        нужная = записи[0]
        print("  Ссылка без номера записи — беру самую свежую трансляцию.")

    видео = нужная.get("video") or {}
    uuid = видео.get("uuid")
    дата = (нужная.get("start_time") or нужная.get("created_at") or "")[:10]

    if нужная.get("is_live"):
        print("")
        print("  ВНИМАНИЕ: трансляция ИДЁТ ПРЯМО СЕЙЧАС (is_live = true).")
        print("  Скачается только уже записанная часть, то есть обрубок.")
        print("  Лучше дождаться конца стрима — тогда duration станет реальным.")
        print("")
    else:
        print("  Запись от %s, длина %s: %s" % (
            дата, чч_мм((нужная.get("duration") or 0) / 1000),
            (нужная.get("session_title") or "")[:60]))

    if not uuid:
        print("  В ответе API нет video.uuid — пробую ссылку как есть.")
        return url, канал
    return ("https://kick.com/%s/videos/%s" % (канал, uuid),
            "%s-%s" % (канал, дата or "kick"))


# ----------------------------------------------------------------- общее

def площадка(url):
    u = url.lower()
    if "kick.com" in u:
        return "kick"
    if "twitch.tv" in u:
        return "twitch"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    return "другое"


def формат(пл, качество):
    if пл == "youtube":
        выс = re.sub(r"\D", "", качество.split("p")[0]) or "1080"
        return "bv*[height<=%s]+ba/b[height<=%s]/b" % (выс, выс)
    # Twitch и Kick отдают именованные форматы: 1080p60, 720p60, ...
    база = качество.replace("p60", "p")
    return "%s/%s60/%s/best" % (качество, база, база)


def запустить_ytdlp(ссылка, выход, качество, скорость=None):
    cmd = [sys.executable, "-m", "yt_dlp",
           "-f", формат(площадка(ссылка), качество),
           "--concurrent-fragments", "8",
           "--retries", "20", "--fragment-retries", "20",
           "--no-playlist",
           "-o", str(выход), ссылка]
    if скорость:
        # Лимит на эту закачку, формат yt-dlp: 900K, 2M. Чтобы не забивать канал.
        cmd += ["--limit-rate", скорость]
    print("")
    print("  Качаю в %s" % выход)
    print("")
    return subprocess.call(cmd)


def показать_файл(путь):
    путь = Path(путь)
    if not путь.exists():
        print("  Файла нет: %s" % путь)
        return 1
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(путь)],
            capture_output=True, text=True, errors="replace", timeout=120)
        d = json.loads(r.stdout)
        видео = next((s for s in d["streams"] if s["codec_type"] == "video"), {})
        дл = float(d["format"].get("duration", 0))
        fps = видео.get("r_frame_rate", "0/1")
        try:
            a, b = fps.split("/")
            fps = "%.0f" % (float(a) / float(b))
        except Exception:
            pass
        print("")
        print("  %s" % путь.name)
        print("  %sx%s @ %s fps, %s, %s" % (
            видео.get("width"), видео.get("height"), fps, чч_мм(дл),
            вес(путь.stat().st_size)))
        print("  Путь: %s" % путь)
        print("")
        return 0
    except Exception as e:
        print("  ffprobe не прочитал файл: %s" % e)
        print("  Если файл ещё качается — это нормально: подожди и повтори.")
        print("  Признак недокачанного файла — ошибка moov atom not found.")
        return 1


def главное():
    арг = sys.argv[1:]
    if not арг:
        print(__doc__)
        return 1

    цель = арг[0]
    инфо = "--инфо" in арг
    список = "--список" in арг

    def значение(флаг, по_умолчанию=None):
        if флаг in арг and len(арг) > арг.index(флаг) + 1:
            return арг[арг.index(флаг) + 1]
        return по_умолчанию

    # Папка обязательна (с 2026-11-20): задачи идут в соседних чатах параллельно,
    # и матч, скачанный «по умолчанию» в СТРИМЫ/, попал бы к стримам для мемов.
    папка_арг = значение("--папка")
    качество = значение("--качество", "1080p60")
    имя = значение("--имя")
    скорость = значение("--скорость")   # лимит скорости, например 900K
    if "--скорость" in арг and not (скорость and re.fullmatch(r"\d+(\.\d+)?[KkMm]?", скорость)):
        print("  --скорость: нужно число с K или M, например 900K или 2M.")
        return 1

    # Локальный файл — просто рассказать, что это такое.
    if not цель.lower().startswith("http"):
        return показать_файл(цель)

    пл = площадка(цель)
    ссылка, подпись = цель, None

    if пл == "kick":
        канал = kick_канал(цель)
        if список or not re.search(r"/videos/", цель):
            записи = kick_видео_канала(канал)
            if not записи:
                return 1
            kick_показать_список(канал, записи)
            if список:
                return 0
        ссылка, подпись = kick_разрешить(цель)
        if ссылка is None:
            return 1
    elif пл == "twitch":
        # В имени — номер записи или клипа: у двух разных ссылок одного дня
        # не будет одинаковых имён (иначе кэши и папки партий смешаются).
        m = re.search(r"/videos/(\d+)", цель)
        клип = re.search(r"(?:clips\.twitch\.tv/|/clip/)([A-Za-z0-9_-]+)", цель)
        if m:
            подпись = "twitch-" + m.group(1)
        elif клип:
            подпись = "twitch-clip-" + клип.group(1)
        else:
            подпись = "twitch-" + datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    elif пл == "youtube":
        m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/)([A-Za-z0-9_-]{6,})", цель)
        подпись = "youtube-" + (m.group(1) if m
                                else datetime.now().strftime("%Y-%m-%d-%H-%M-%S"))

    if инфо:
        print("")
        print("  Площадка: %s" % пл)
        print("  Ссылка для yt-dlp: %s" % ссылка)
        print("")
        return subprocess.call([sys.executable, "-m", "yt_dlp", "-F", ссылка])

    if not папка_арг:
        print("")
        print("  Укажи, куда класть: --папка \"%s\" (стрим для мемов)," % СТРИМЫ)
        print("  --папка \"%s\" (матч) или" % (СТРИМЫ.parent / "СЫРЫЕ МАТЧИ"))
        print("  --папка \"%s\" (клип для файтов)." % (СТРИМЫ.parent / "СЫРЫЕ КЛИПЫ"))
        return 1
    папка = Path(папка_арг)
    папка.mkdir(parents=True, exist_ok=True)
    основа = имя or подпись or datetime.now().strftime("стрим-%Y-%m-%d-%H-%M-%S")
    выход = папка / (основа + ".%(ext)s")

    # Уже скачанное не перекачиваем и не перезаписываем. Готовым считается
    # только файл с точным именем, который ffprobe читает целиком: после
    # обрыва yt-dlp оставляет куски вроде «основа.f1080.mp4» и «.part».
    def целый(p):
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(p)], capture_output=True, text=True)
        try:
            return r.returncode == 0 and float(r.stdout.strip()) > 0
        except ValueError:
            return False
    уже = [p for p in папка.glob(основа + ".*")
           if p.stem == основа and целый(p)
           and p.suffix.lower() in (".mp4", ".mkv", ".ts", ".webm", ".mov", ".flv")]
    if уже:
        print("")
        print("  Уже скачано, повторно не качаю:")
        показать_файл(уже[0])
        return 0

    # Стрим на 8 часов — 15–25 ГБ, плюс рядом идут другие задачи.
    import shutil
    свободно = shutil.disk_usage(папка).free / 1e9
    клип = основа.startswith("twitch-clip-")
    if свободно < 5 and not клип:     # клип Twitch весит мегабайты
        print("  На диске свободно %.1f ГБ — не хватит даже на матч. Освободи место." % свободно)
        return 1
    if свободно < 30 and not клип:
        print("  ВНИМАНИЕ: на диске свободно %.1f ГБ. Длинный стрим может не влезть." % свободно)

    код = запустить_ytdlp(ссылка, выход, качество, скорость)
    if код != 0:
        print("")
        print("  yt-dlp вернул ошибку. Что обычно помогает:")
        print("    1. %s -m pip install --no-cache-dir -U yt-dlp   — экстракторы ломаются и чинятся часто" % пути.ПИТОН)
        print("    2. качество пониже:  --качество 720p60")
        print("    3. скачать вручную и положить файл в СТРИМЫ/")
        return код

    готовые = sorted(папка.glob(основа + ".*"), key=lambda p: p.stat().st_mtime)
    if готовые:
        показать_файл(готовые[-1])
    return 0


if __name__ == "__main__":
    sys.exit(главное())
