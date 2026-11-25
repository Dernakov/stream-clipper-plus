# -*- coding: utf-8 -*-
"""
Строка состояния Claude Code: этапы всех идущих задач и заполнение контекста.

Задачи могут идти одновременно в соседних чатах (мемы, файты, матч), поэтому
у каждой свой файл этапа: .cache/этап-<команда>.txt (этап-мем.txt,
этап-файт.txt, этап-матч.txt). Строка показывает их все, например
«мем 2/5 Выбор моментов · матч 1/6 Анализ». Старый общий .cache/этап.txt
тоже читается — для прочей работы. Файлы со словом «Готово» и забытые
(старше 7 суток) не показываются.
"""
import json
import sys
import time
from pathlib import Path

ШИРИНА = 10    # число делений в полоске
КЭШ = Path(__file__).resolve().parent.parent / ".cache"
МАКС_ДЛИНА_ЭТАПА = 60    # на одну задачу, чтобы длинный текст не ломал строку
СРОК = 7 * 86400         # дольше этого этап считается забытым


def этапы():
    """Список «задача этап» по всем свежим файлам этапа."""
    итог = []
    файлы = sorted(КЭШ.glob("этап-*.txt")) + [КЭШ / "этап.txt"]
    for файл in файлы:
        try:
            if time.time() - файл.stat().st_mtime > СРОК:
                continue
            строки = файл.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        текст = строки[0].strip() if строки else ""
        if not текст or текст.lower().startswith("готово"):
            continue
        задача = файл.stem[len("этап-"):] if файл.stem.startswith("этап-") else ""
        итог.append(f"{задача} {текст}".strip()[:МАКС_ДЛИНА_ЭТАПА])
    return итог


def main():
    # Claude Code передаёт данные сессии в stdin в формате JSON.
    try:
        данные = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        процент = round(float(данные["context_window"]["used_percentage"]))
    except (ValueError, KeyError, TypeError, OverflowError):
        процент = None

    sys.stdout.reconfigure(encoding="utf-8")
    # Этапы показываются первыми, если они есть.
    текущие = этапы()
    префикс = f"Этап: {' · '.join(текущие)} | " if текущие else ""

    # Пока модель не ответила ни разу, значения ещё нет.
    if процент is None:
        print(f"{префикс}Контекст: нет данных")
        return

    процент = max(0, min(100, процент))
    занято = процент * ШИРИНА // 100
    print(f"{префикс}Контекст: {'█' * занято}{'░' * (ШИРИНА - занято)} {процент}%")


if __name__ == "__main__":
    main()
