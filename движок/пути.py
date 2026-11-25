# -*- coding: utf-8 -*-
"""
Держит служебные файлы внутри проекта (.cache/), а не в системных папках.

Модуль нужно импортировать раньше всего остального: библиотеки читают
переменные окружения при своём первом импорте. Дочерние процессы
(ffmpeg, yt-dlp) наследуют эти переменные автоматически.
"""
import os
import sys
import tempfile
from pathlib import Path

ПРОЕКТ = Path(__file__).resolve().parent.parent
КЭШ = ПРОЕКТ / ".cache"
ВРЕМЕННЫЕ = КЭШ / "tmp"
МОДЕЛИ = КЭШ / "huggingface"       # сюда скачивается модель распознавания речи
ОКРУЖЕНИЕ = ПРОЕКТ / ".venv"
ПИТОН = ОКРУЖЕНИЕ / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
# Папка с ffmpeg, если его нет в PATH: переменная окружения FFMPEG_DIR.
FFMPEG = Path(os.environ["FFMPEG_DIR"]) if os.environ.get("FFMPEG_DIR") else None

ВРЕМЕННЫЕ.mkdir(parents=True, exist_ok=True)

# Временные файлы: Python и ffmpeg берут папку из этих переменных.
for имя in ("TEMP", "TMP", "TMPDIR"):
    os.environ[имя] = str(ВРЕМЕННЫЕ)
tempfile.tempdir = str(ВРЕМЕННЫЕ)

# Кэши: Hugging Face (модель), yt-dlp и fontconfig (шрифты в ffmpeg).
os.environ["HF_HOME"] = str(МОДЕЛИ)
os.environ["XDG_CACHE_HOME"] = str(КЭШ)

# Эти переменные важнее HF_HOME: если заданы, модель ушла бы мимо проекта.
for имя in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_XET_CACHE"):
    os.environ.pop(имя, None)

# ffmpeg не в системном PATH — добавить папку из FFMPEG_DIR.
if FFMPEG and FFMPEG.is_dir():
    os.environ["PATH"] = str(FFMPEG) + os.pathsep + os.environ.get("PATH", "")


def в_окружении_проекта():
    """True, если скрипт запущен интерпретатором из .venv проекта."""
    return Path(sys.prefix).resolve() == ОКРУЖЕНИЕ.resolve()


def требовать_окружение():
    """Остановить скрипт с понятным сообщением, если запущен не тот Python."""
    if not в_окружении_проекта():
        raise SystemExit(f"Запущен не тот Python. Нужен: {ПИТОН}")
