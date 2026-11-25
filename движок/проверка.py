# -*- coding: utf-8 -*-
"""
Проверка окружения: что стоит, чего не хватает и чем это доставить.

    python проверка.py             полный отчёт
    python проверка.py --кратко    только итоговая строка

Запускается командой /начинаем при первом запуске и в начале каждой партии.
Ничего не ставит сама — только показывает точные команды установки,
чтобы решение ставить осталось за человеком.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import пути  # первым: переносит кэши и временные файлы в .cache проекта

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ДВИЖОК = Path(__file__).resolve().parent
ПРОЕКТ = ДВИЖОК.parent
PIP = f"{пути.ПИТОН} -m pip install --no-cache-dir"   # только в окружение проекта

ЕСТЬ, НЕТ, ВНИМ = "[ есть ]", "[ НЕТ  ]", "[  !   ]"

проблемы = []      # то, без чего работать нельзя
замечания = []     # то, что стоит знать, но не блокирует


def строка(метка, статус, текст=""):
    print(f"  {статус} {метка:<22} {текст}")


def версия_программы(имя, аргумент="-version"):
    путь = shutil.which(имя)
    if not путь:
        return None, None
    try:
        r = subprocess.run([путь, аргумент], capture_output=True, text=True,
                           timeout=20, errors="replace")
        первая = (r.stdout or r.stderr or "").strip().splitlines()
        return путь, (первая[0][:60] if первая else "")
    except Exception:
        return путь, ""


def проверить_python():
    v = sys.version_info
    текст = f"{v.major}.{v.minor}.{v.micro}"
    if v < (3, 9):
        проблемы.append("Нужен Python 3.9 или новее — python.org/downloads")
        строка("Python", НЕТ, текст + " — слишком старый, нужен 3.9+")
    elif not пути.в_окружении_проекта():
        проблемы.append(f"Запускать нужно через окружение проекта: {пути.ПИТОН}")
        строка("Python", НЕТ, текст + " — запущен не из окружения проекта")
    else:
        строка("Python", ЕСТЬ, текст + " (окружение проекта)")


def проверить_ffmpeg():
    for имя in ("ffmpeg", "ffprobe"):
        путь, вер = версия_программы(имя)
        if путь:
            строка(имя, ЕСТЬ, вер)
        else:
            строка(имя, НЕТ, "не найден в PATH")
            проблемы.append(
                "ffmpeg — весь монтаж на нём. Поставить и добавить в PATH\n"
                "        (Windows: сборка с gyan.dev, macOS: brew install ffmpeg,\n"
                "        Linux: пакет ffmpeg) или указать папку в FFMPEG_DIR.")
            return


def проверить_ytdlp():
    если_модуль = importlib.util.find_spec("yt_dlp") is not None
    путь = shutil.which("yt-dlp")
    if если_модуль:
        try:
            import yt_dlp
            строка("yt-dlp", ЕСТЬ, f"модуль, версия {yt_dlp.version.__version__}")
        except Exception:
            строка("yt-dlp", ЕСТЬ, "модуль")
    elif путь:
        строка("yt-dlp", ЕСТЬ, "программа в PATH (модуля нет)")
        замечания.append("yt-dlp стоит как программа, но не как модуль. "
                         "Скрипт скачивания зовёт `python -m yt_dlp` — "
                         f"поставь модулем: {PIP} -U yt-dlp")
    else:
        строка("yt-dlp", НЕТ, "нужен, чтобы качать стримы по ссылке")
        проблемы.append("yt-dlp — качает стримы с Twitch/Kick/YouTube:\n"
                        f"        {PIP} -U yt-dlp")


def проверить_пакеты():
    нужные = {
        "faster_whisper": ("faster-whisper", "распознавание речи в стриме"),
        "numpy":          ("numpy",          "поиск моментов, замеры"),
        "PIL":            ("pillow",         "подбор кегля заголовка"),
    }
    нет = []
    for модуль, (пакет, зачем) in нужные.items():
        if importlib.util.find_spec(модуль):
            строка(пакет, ЕСТЬ, зачем)
        else:
            строка(пакет, НЕТ, зачем)
            нет.append(пакет)
    if нет:
        проблемы.append(f"Пакеты Python:\n        {PIP} " + " ".join(нет))


def проверить_шрифт():
    import glob
    шрифты = sorted(glob.glob(str(ДВИЖОК / "fonts" / "*.ttf")))
    if not шрифты:
        строка("шрифт", НЕТ, "папка движок/fonts пуста")
        проблемы.append("Шрифт заголовков: положи .ttf в движок/fonts "
                        "и укажи его в мои_настройки.py")
        return
    имена = ", ".join(Path(ш).name for ш in шрифты)
    строка("шрифт", ЕСТЬ, имена)


def проверить_модель():
    кэш = пути.МОДЕЛИ / "hub"
    найдено = []
    if кэш.exists():
        найдено = [d.name for d in кэш.iterdir()
                   if d.is_dir() and "whisper" in d.name.lower()]
    if найдено:
        строка("модель речи", ЕСТЬ, f"скачана: {len(найдено)} шт.")
    else:
        строка("модель речи", ВНИМ, "скачается сама при первом asr (~1.5 ГБ)")
        замечания.append("Модель распознавания ещё не скачана. Первый `asr` "
                         "потратит несколько минут на загрузку ~1.5 ГБ — "
                         "это нормально и бывает один раз.")


def проверить_место():
    try:
        всего, занято, свободно = shutil.disk_usage(str(ПРОЕКТ))
    except Exception:
        return
    гб = свободно / 1024 ** 3
    if гб < 15:
        строка("место на диске", ВНИМ, f"{гб:.1f} ГБ свободно")
        замечания.append(f"Свободно всего {гб:.1f} ГБ. Стрим на 3 часа в 1080p — "
                         "это 6-10 ГБ, плюс кэш распознавания. Освободи место.")
    else:
        строка("место на диске", ЕСТЬ, f"{гб:.0f} ГБ свободно")


def проверить_ядра():
    n = os.cpu_count() or 4
    рек = max(2, n - 2)
    строка("ядер CPU", ЕСТЬ, f"{n} (в настройках стоит держать ASR_THREADS = {рек})")
    try:
        sys.path.insert(0, str(ДВИЖОК))
        import config as C
        if abs(getattr(C, "ASR_THREADS", рек) - рек) > 4:
            замечания.append(
                f"ASR_THREADS = {C.ASR_THREADS}, а ядер {n}. "
                f"Для этой машины разумнее {рек} — поправь в мои_настройки.py.")
    except Exception:
        pass


def проверить_настройки():
    личные = ДВИЖОК / "мои_настройки.py"
    if личные.exists():
        строка("мои настройки", ЕСТЬ, "движок/мои_настройки.py")
        return True
    строка("мои настройки", ВНИМ, "ещё не создан — это первый запуск")
    return False


def главное():
    кратко = "--кратко" in sys.argv
    первый_запуск = None
    if not кратко:
        print()
        print("  ПРОВЕРКА ОКРУЖЕНИЯ")
        print("  " + "-" * 68)
        проверить_python()
        проверить_ffmpeg()
        проверить_ytdlp()
        проверить_пакеты()
        проверить_шрифт()
        проверить_модель()
        проверить_место()
        проверить_ядра()
        первый_запуск = проверить_настройки()
        print()
    else:
        # тихий режим: те же проверки, но без печати
        import io, contextlib
        буфер = io.StringIO()
        with contextlib.redirect_stdout(буфер):
            проверить_python(); проверить_ffmpeg(); проверить_ytdlp()
            проверить_пакеты(); проверить_шрифт(); проверить_модель()
            проверить_место(); проверить_ядра()
            первый_запуск = проверить_настройки()

    if проблемы and not кратко:
        print("  НЕ ХВАТАЕТ — без этого работать нельзя:")
        for i, п in enumerate(проблемы, 1):
            print(f"    {i}. {п}")
        print()

    if замечания and not кратко:
        print("  ОБРАТИ ВНИМАНИЕ:")
        for i, з in enumerate(замечания, 1):
            print(f"    {i}. {з}")
        print()

    if проблемы:
        print("ИТОГ: не хватает " + str(len(проблемы)) + " шт. — см. выше")
        return 1
    if первый_запуск is False:
        print("ИТОГ: окружение готово, НАСТРОЙКИ ЕЩЁ НЕ СОЗДАНЫ (первый запуск)")
        return 0
    print("ИТОГ: всё на месте, можно работать")
    return 0


if __name__ == "__main__":
    sys.exit(главное())
