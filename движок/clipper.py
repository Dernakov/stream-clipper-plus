# -*- coding: utf-8 -*-
"""
Стрим 1.5 часа -> вертикальные клипы под TikTok и Instagram Reels.

Порядок:
  python clipper.py scan  <стрим.mp4>   аудио, громкость, 30 кандидатов
  python clipper.py asr   <стрим.mp4>   транскрипт целиком (large-v3-turbo)
  python clipper.py pick  <стрим.mp4>   свести с речью и движением
  python clipper.py cut   <стрим.mp4> --start 575 --dur 24 --title "..." --out "имя"
  python clipper.py check <клип.mp4>    нет ли чужой графики в кадре

  python clipper.py auto  <стрим.mp4>   всё подряд, отбор формулой (хуже)

Кэш у каждого стрима свой: work/<имя стрима>/ — чужой не подхватится.
Результат: Clips Ready/<дата> <время> <имя стрима> <длительность>/<имя клипа>.mp4


Работает без GPU и без интернета. Нужен только ffmpeg в PATH.
"""
import argparse, io, json, os, re, subprocess, sys, time, wave
from pathlib import Path

sys.dont_write_bytecode = True     # чтобы не плодить папки __pycache__

import пути                        # первым: переносит кэши и временные файлы в .cache проекта
пути.требовать_окружение()

import numpy as np

# Консоль Windows по умолчанию cp1251 и роняет вывод на кириллице.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).parent
WORK = HERE / "work"
OUT = HERE.parent / "Clips Ready"  # результат лежит рядом с движком, не внутри
FONTS = HERE / "fonts"
for d in (WORK, OUT):
    d.mkdir(exist_ok=True)

sys.path.insert(0, str(HERE))
import config as C

# Временные файлы рендера у каждого процесса свои: work/jobs/<случайное имя>/.
# Раньше clip_tmp.wav, clip_tmp.ass, probe.wav и clip_nick.png лежали прямо
# в work/, и два одновременных рендера (например, мемы и файты в соседних
# чатах) перетирали их друг у друга: ролик получал чужие субтитры без ошибки.
JOBS = WORK / "jobs"
# Путь к шрифтам из папки задачи. Относительный и без буквы диска: libass
# на Windows спотыкается о двоеточие в "D:\...".
FONTS_REL = "../../../fonts"
_JOB_DIR = None


def job_dir():
    """Личная временная папка этого процесса; удаляется при выходе."""
    global _JOB_DIR
    if _JOB_DIR is None:
        import atexit, shutil, tempfile
        JOBS.mkdir(parents=True, exist_ok=True)
        # Папки процессов, которые упали и не прибрали за собой.
        старше = time.time() - 2 * 86400
        for p in JOBS.iterdir():
            try:
                if p.is_dir() and p.stat().st_mtime < старше:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
        _JOB_DIR = Path(tempfile.mkdtemp(prefix=f"p{os.getpid()}_", dir=JOBS))
        atexit.register(shutil.rmtree, _JOB_DIR, True)
    return _JOB_DIR


def finish_file(tmp, dst):
    """
    Кладёт готовый файл на место одним переименованием. Пока рендер идёт,
    в папке результата нет недописанного ролика, и приёмка его не заберёт.
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(tmp), str(dst))


def sh(cmd, **kw):
    """Запустить процесс, упасть с понятной ошибкой."""
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", **kw)
    if r.returncode != 0:
        sys.stderr.write((r.stderr or "")[-3000:] + "\n")
        raise SystemExit(f"команда упала: {' '.join(map(str, cmd[:4]))} ...")
    return r.stdout


def jdump(obj, path):
    # Через временный файл и переименование: соседний процесс не прочитает
    # недописанный json (transcript, candidates), даже если взял ту же запись.
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def jload(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def hhmmss(t):
    t = int(t)
    return f"{t//60:02d}:{t%60:02d}"


# ────────────────────────────── ЭТАП 1: SCAN ──────────────────────────────

def stream_cache(src):
    """
    Кэш на КАЖДЫЙ стрим свой: work/<имя файла>/.

    Раньше кэш был общий, и это была мина: на новом стриме scan/asr видели
    старые audio16k.wav и transcript.json, писали «уже есть» и пропускали
    работу. Дальше моменты выбирались по транскрипту ПРЕДЫДУЩЕГО стрима,
    а резались из нового — на выходе случайные куски с чужими заголовками.
    Ошибка молчаливая, ловится только просмотром готовых файлов.

    31.08.2026 мина сработала снова, уже по-другому: пользователь прислал новую партию,
    и один из файлов опять назывался «запись-1.mp4» — ровно как разобранный
    накануне. Имя то же, содержимое другое. Поэтому теперь в кэше лежит штамп
    source.json с размером исходника, и при несовпадении заводится ОТДЕЛЬНАЯ
    папка, а старый кэш не трогается.
    """
    src = Path(src)
    d = WORK / src.stem
    try:
        size = src.stat().st_size
    except OSError:
        d.mkdir(parents=True, exist_ok=True)
        return d

    sf = d / "source.json"
    if d.exists():
        # 31.08.2026, второй заход. Штамп добавили ПОЗЖЕ, чем завели первые
        # папки кэша, поэтому у старых папок source.json нет вовсе — и проверка
        # ниже их молча пропускала. Хуже: она потом штамповала чужой кэш
        # размером НОВОГО файла, и подмену уже нельзя было заметить.
        # Теперь кэш без штампа, но с содержимым, считается ЧУЖИМ.
        нажито = [f for f in ("audio16k.wav", "transcript.json", "peaks.json")
                  if (d / f).exists()]
        if sf.exists():
            old = jload(sf)
            if old.get("size") != size:
                print(f"  ВНИМАНИЕ: кэш «{src.stem}» принадлежит ДРУГОМУ файлу "
                      f"({old.get('size')} байт против {size}).")
                d = WORK / f"{src.stem}__{size % 100000}"
                sf = d / "source.json"
                print(f"  Беру отдельную папку work/{d.name}, старый кэш не трогаю.")
        elif нажито:
            print(f"  ВНИМАНИЕ: в кэше «{src.stem}» лежит {', '.join(нажито)} "
                  f"БЕЗ штампа — чей это, неизвестно.")
            d = WORK / f"{src.stem}__{size % 100000}"
            sf = d / "source.json"
            print(f"  Беру отдельную папку work/{d.name}, старый кэш не трогаю.")
    d.mkdir(parents=True, exist_ok=True)
    if not sf.exists():
        jdump({"file": str(src), "size": size}, sf)
    return d


LAYOUT_KEYS = re.compile(r"(CAM_SRC|CAM_SAFE|MEME_FILL_AREA|[A-Z0-9_]*CROP[A-Z0-9_]*)")


def apply_layout(src):
    """
    Геометрия раскладки (окно вебки и кропы стилей) для КОНКРЕТНОЙ записи.

    Раньше замер вписывался в мои_настройки.py, общий для всех. Если в одном
    чате замеряли новый стрим, параллельный рендер в соседнем чате
    подхватывал чужие кропы посреди работы. Теперь принятый замер лежит
    в кэше записи: work/<имя>/layout.json. Нет файла — работают числа
    из мои_настройки.py, как раньше.
    """
    if not src:
        return
    f = stream_cache(src) / "layout.json"
    if not f.exists():
        return
    data = jload(f)
    чужие = [k for k in data if not LAYOUT_KEYS.fullmatch(k) or not hasattr(C, k)]
    if чужие:
        raise SystemExit(f"{f}: неизвестные ключи {', '.join(чужие)} "
                         "— в layout.json только геометрия раскладки")
    for k, v in data.items():
        setattr(C, k, tuple(v) if isinstance(v, list) else v)
    print(f"  раскладка этой записи: {f.parent.name}/layout.json ({len(data)} чисел)")


def has_audio(src):
    """Есть ли в файле звуковая дорожка."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=index", "-of", "csv=p=0",
                        str(src)], capture_output=True, text=True,
                       errors="replace")
    return bool(r.stdout.strip())


def extract_audio(src):
    wav = stream_cache(src) / "audio16k.wav"
    if wav.exists() and wav.stat().st_size > 1000:
        print(f"  аудио уже есть: {wav.name}")
        return wav
    if not has_audio(src):
        # Без этой проверки ffmpeg падал невнятным «Output file does not
        # contain any stream», и понять причину было нельзя.
        sys.exit("\n  В файле НЕТ звуковой дорожки, а на звуке стоит весь "
                 "конвейер:\n  по нему ищутся моменты, режутся паузы и "
                 "снимаются субтитры.\n  Скорее всего, скачалось только "
                 "видео — перекачай стрим целиком.\n")
    print("  извлекаю аудио 16 кГц моно...")
    # Пишем во временную папку и кладём на место целиком: иначе соседний
    # процесс (asr) мог прочитать недописанный файл, раз он «больше 1000 байт».
    tmp = job_dir() / "audio16k.wav"
    sh(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)])
    finish_file(tmp, wav)
    return wav


def stub_av():
    """
    Windows Smart App Control (включён 30.08.2026) блокирует DLL пакета `av`
    (PyAV) — он не подписан. faster_whisper тянет av ТОЛЬКО ради своей функции
    decode_audio, то есть чтобы превратить файл в массив сэмплов.

    Нам это не нужно: аудио уже вытащено ffmpeg-ом в 16 кГц моно 16 бит.
    Поэтому подсовываем пустой модуль `av` — импорт проходит, а decode_audio
    никогда не вызывается, потому что в transcribe() мы передаём массив,
    а не путь к файлу (см. read_wav_f32 ниже).

    Если политику однажды отключат и настоящий av заработает — заглушка
    не поставится, ветка сама себя выключит.
    """
    import types
    try:
        import av  # noqa: F401
        return False
    except Exception:
        m = types.ModuleType("av")
        m.__doc__ = "заглушка вместо заблокированного PyAV, см. stub_av()"
        sys.modules.setdefault("av", m)
        return True


def read_wav_f32(path):
    """wav 16 кГц моно 16 бит -> float32 -1..1, как ждёт faster_whisper."""
    import wave
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != 16000:
            raise RuntimeError(f"{path}: ожидался wav 16 кГц моно 16 бит")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def loudness_curve(wav):
    """Посекундная громкость: линейный RMS и он же в dBFS."""
    npy = wav.parent / "rms_lin.npy"
    if npy.exists():
        lin = np.load(npy)
        return lin, np.clip(20 * np.log10(lin + 1e-9), -80, 0)
    print("  считаю кривую громкости...")
    w = wave.open(str(wav))
    sr, n = w.getframerate(), w.getnframes()
    data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    cnt = len(data) // sr
    lin = np.sqrt((data[:cnt * sr].reshape(cnt, sr) ** 2).mean(axis=1)).astype(np.float32)
    np.save(npy, lin)
    return lin, np.clip(20 * np.log10(lin + 1e-9), -80, 0)


def _smooth(x, w):
    # Окно нельзя брать длиннее самих данных: np.convolve с mode="same"
    # вернёт тогда массив длиной с ОКНО, а не с данные, и следующая же
    # операция упадёт на несовпадении размеров. Ловится только на коротких
    # файлах — например когда шаблон пробуют на минутном ролике.
    w = max(1, min(int(w), len(x)))
    return np.convolve(x, np.ones(w) / w, mode="same")


def find_peaks(lin, db, k=C.N_CANDIDATES, mindist=45):
    """
    Отбираем по АБСОЛЮТНОЙ громкости окна, а не по «всплеску после тишины».

    Это не первая версия. Сначала здесь был всплеск относительно фона —
    и он систематически приводил в меню: между матчами стример молчит,
    поэтому любой возврат звука выглядел как гигантский всплеск. Проверка
    глазами показала, что топ по всплеску — это лобби и таблицы результатов.
    Абсолютная громкость такого перекоса не даёт.

    Усредняем в ЛИНЕЙНОЙ шкале: в dB усреднение занижает короткие крики.
    """
    win = 21
    loud = _smooth(lin, win)
    bg = _smooth(db, 61)
    burst = _smooth(db, 3) - bg        # оставляем как справочный признак

    order = np.argsort(loud)[::-1]
    picked = []
    for i in order:
        if all(abs(i - j) >= mindist for j in picked):
            picked.append(int(i))
        if len(picked) >= k:
            break
    picked.sort()
    lo, hi = float(loud.min()), float(loud.max())
    return [{"t": t,
             "loud": round((float(loud[t]) - lo) / max(hi - lo, 1e-9), 3),
             "abs_db": round(float(db[t]), 1),
             "burst": round(float(burst[t]), 1)} for t in picked]


def cmd_scan(args):
    print("[1/4] SCAN")
    wav = extract_audio(args.src)
    if wav.stat().st_size < 16000 * 2 * 5:
        sys.exit("\n  Файл короче 5 секунд — искать в нём моменты нечего.\n"
                 "  Конвейер рассчитан на запись стрима целиком.\n")
    lin, db = loudness_curve(wav)
    peaks = find_peaks(lin, db)
    jdump(peaks, stream_cache(args.src) / "peaks.json")
    print(f"  длина стрима: {len(db)} сек ({len(db)/60:.0f} мин)")
    print(f"  найдено кандидатов: {len(peaks)}")
    for p in peaks[:12]:
        print(f"    {hhmmss(p['t'])}  громкость {p['loud']:.2f}  ({p['abs_db']:.0f} dB)")
    print(f"  -> {stream_cache(args.src)/'peaks.json'}")


# ────────────────────────────── ЭТАП 2: ASR ───────────────────────────────

def load_model(size):
    stub_av()
    from faster_whisper import WhisperModel
    print(f"  гружу модель {size} (первый раз — скачается, ~1-3 ГБ)...")
    return WhisperModel(size, device="cpu", compute_type="int8",
                        cpu_threads=C.ASR_THREADS)


def cmd_asr(args):
    print("[2/4] ASR — транскрипт всего стрима")
    wav = extract_audio(args.src)
    dst = stream_cache(args.src) / "transcript.json"
    if dst.exists() and not args.force:
        print(f"  уже есть: {dst.name} (--force чтобы перезаписать)")
        return
    m = load_model(C.ASR_MODEL_SCAN)
    t0 = time.time()
    segs, _ = m.transcribe(read_wav_f32(wav), language="ru", beam_size=1,
                           vad_filter=True,
                           vad_parameters=dict(min_silence_duration_ms=500,
                                               speech_pad_ms=200),
                           condition_on_previous_text=False)
    out, mark = [], 0
    for s in segs:
        out.append({"start": round(s.start, 2), "end": round(s.end, 2),
                    "text": s.text.strip()})
        if s.start - mark > 300:
            mark = s.start
            el = time.time() - t0
            print(f"    {s.start/60:4.0f} мин | {s.start/max(el,1):.1f}x", flush=True)
    jdump(out, dst)
    words = sum(len(x["text"].split()) for x in out)
    print(f"  готово за {(time.time()-t0)/60:.1f} мин | "
          f"{len(out)} реплик | {words} слов -> {dst}")


# ────────────────────────────── ЭТАП 3: PICK ──────────────────────────────

# Слова, которые у стримера маркируют эмоцию/поворот.
# Это грубая эвристика — она нужна, только если ты не отдаёшь отбор Claude.
HOT = re.compile(
    r"\b(бля|блин|нах|пизд|ебa|ёб|еба|сук|офиг|ахуе|охуе|капец|жесть|"
    r"я в шоке|не понял|что это|как так|стоп|смотри|прикинь|вот это|"
    r"убил|убива|killed|топ-?1|выигр|победа|проигр|слил|катка|"
    r"донат|подписч|réальн|реально|серьёзно|ору|ржу|смешно)", re.I)


def words_in_window(tr, t0, t1):
    return [s for s in tr if s["end"] > t0 and s["start"] < t1]


def motion_score(src, t, span=6.0, n=10):
    """
    Насколько «живая» картинка в области геймплея.
    Это важно: стример часто орёт, разбирая ПОСЛЕМАТЧЕВУЮ ТАБЛИЦУ —
    эмоция есть, а на экране статика, и клип получается мёртвый.
    Считаем среднюю разницу между соседними кадрами: меню ~0.5, бой ~15-50.
    """
    from PIL import Image
    import tempfile, shutil
    td = tempfile.mkdtemp()
    try:
        g = game_crop_for(src)
        sh(["ffmpeg", "-y", "-v", "error", "-ss", str(max(0, t - span / 2)),
            "-t", str(span), "-i", str(src),
            "-vf", f"crop={g['w']}:{g['h']}:{g['x']}:{g['y']},"
                   f"scale=160:-1,fps={n/span:.2f}",
            "-q:v", "5", os.path.join(td, "f_%03d.jpg")])
        fs = sorted(f for f in os.listdir(td) if f.endswith(".jpg"))
        if len(fs) < 3:
            return 0.0
        ar = [np.asarray(Image.open(os.path.join(td, f)).convert("L"),
                         dtype=np.float32) for f in fs]
        return float(np.mean([np.abs(ar[i + 1] - ar[i]).mean()
                              for i in range(len(ar) - 1)]))
    finally:
        shutil.rmtree(td, ignore_errors=True)


def cmd_pick(args):
    print("[3/4] PICK — свожу кандидатов с речью")
    src = getattr(args, "src", None)
    if not src:
        raise SystemExit("укажи файл стрима: python clipper.py pick <стрим.mp4>")
    cache = stream_cache(src)
    for need, step in (("peaks.json", "scan"), ("transcript.json", "asr")):
        if not (cache / need).exists():
            raise SystemExit(
                f"нет {need} для этого стрима.\n"
                f"  Сначала прогони: python clipper.py {step} \"{src}\"")
    peaks = jload(cache / "peaks.json")
    tr = jload(cache / "transcript.json")

    cands = []
    for p in peaks:
        t = p["t"]
        a, b = max(0, t - C.PRE_ROLL - 6), t + C.POST_ROLL + 6
        segs = words_in_window(tr, a, b)
        text = " ".join(s["text"] for s in segs).strip()
        if len(text) < 15:          # молчит — скорее всего просто шум игры
            continue
        wps = len(text.split()) / max(b - a, 1)
        hot = len(HOT.findall(text))
        cands.append({"t": t, "time": hhmmss(t), "loud": p.get("loud", 0),
                      "burst": p["burst"], "hot_words": hot,
                      "words_per_sec": round(wps, 2), "text": text[:600]})

    if src:
        print(f"  меряю движение в кадре для {len(cands)} кандидатов...")
        for c in cands:
            c["motion"] = round(motion_score(src, c["t"]), 2)

    # Движение — самый надёжный из трёх сигналов. Порог именно 2.5, а не 4:
    # при 4 под нож попадают спокойные, но живые сцены (стоит и целится,
    # идёт по полю). Статичные экраны — рабочий стол, лобби, таблица итогов —
    # лежат ниже 2.3, между 2.5 и 6 честная серая зона.
    for c in cands:
        m = c.get("motion")
        if m is None:
            mv = 0.0
        elif m < 2.5:
            mv = -40            # статика, клип будет мёртвый
        elif m < 6:
            mv = -8             # серая зона: может быть и меню, и вялый бой
        else:
            mv = min(m, 30) * 1.2
        c["score"] = round(c["loud"] * 40 + mv + c["hot_words"] * 3
                           + min(c["words_per_sec"], 4) * 3, 1)

    cands.sort(key=lambda x: -x["score"])
    jdump(cands, cache / "candidates.json")
    print(f"  кандидатов с речью: {len(cands)} -> {cache/'candidates.json'}")
    print("\n  ТОП-10:\n")
    for c in cands[:10]:
        m = c.get("motion")
        ms = f" motion={m:5.1f}" if m is not None else ""
        print(f"  {c['time']}  score={c['score']:5.1f}{ms}  {c['text'][:90]}")
    print(f"\n  Дальше: отдай {cache/'candidates.json'} Claude Code и попроси")
    print("  выбрать лучшие моменты — он читает смысл, а не только громкость.")


# ────────────────────────────── ЭТАП 4: CUT ───────────────────────────────

def esc_ass(s):
    return s.replace("{", "(").replace("}", ")").replace("\n", " ")


def wrap2_top_heavy(text, width):
    """
    Перенос с уклоном в ДЛИННУЮ первую строку — так сделано в образце
    второго аккаунта: «СТРИМЕР ПОДШАРИЛ» / «ЗА КВАС ТАРАС», 83% и 53%
    ширины кадра. Обычный балансирующий перенос даёт другую картинку.
    """
    words = text.split()
    if len(words) < 2:
        return text, ""
    # 31.08.2026: тот же штраф за висящий предлог, что и в wrap2. Без него
    # «СКОЛЬКО ХИНКАЛИ В / НЕГО ВЛЕЗАЕТ» — «В» болтается в конце строки.
    ВИСЯЧИЕ = {"и", "а", "но", "в", "на", "с", "у", "к", "по", "за", "из", "до",
               "что", "как", "не", "он", "то", "же", "бы", "о", "от", "для",
               "про", "со", "во", "их", "ему", "мне"}
    best, best_cost = None, None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        over = max(0, len(a) - width) + max(0, len(b) - width)
        # целимся в соотношение примерно 1.6 : 1
        cost = over * 10 + abs(len(a) - len(b) * 1.6)
        if words[i - 1].strip(".,!?—-").lower() in ВИСЯЧИЕ:
            cost += 12
        if best_cost is None or cost < best_cost:
            best, best_cost = (a, b), cost
    return best


def wrap2(text, width=C.MAX_CHARS_PER_LINE):
    """
    Разбить фразу на 2 строки, сохраняя порядок слов.
    Перебираем все точки разреза и берём ту, где длинная строка короче всего —
    так строки выходят примерно равными, как в референсах.
    """
    words = text.split()
    if not words:
        return "", ""
    if len(" ".join(words)) <= width:
        return " ".join(words), ""

    # короткие слова, которые некрасиво висят в конце строки
    HANG = {"и", "а", "но", "в", "на", "с", "у", "к", "по", "за", "из", "до",
            "что", "как", "не", "он", "то", "же", "бы", "о", "от", "для"}

    best, best_cost = None, None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        # штраф за превышение ширины намного тяжелее, чем за перекос
        over = max(0, len(a) - width) + max(0, len(b) - width)
        hang = 6 if words[i - 1].lower().strip(",.!?") in HANG else 0
        cost = over * 10 + abs(len(a) - len(b)) + hang
        if best_cost is None or cost < best_cost:
            best, best_cost = (a, b), cost
    return best


def linear_gain_db(wav_path, target_i=None, target_tp=-1.5):
    """
    Сколько дБ добавить, чтобы клип звучал на LOUDNESS_TARGET — ОДНИМ
    постоянным числом на весь клип.

    Зачем именно так. Раньше стояло dynaudnorm=f=200:g=15 плюс однопроходный
    loudnorm. У dynaudnorm окно сглаживания гейна = g*f = 15*200 мс = 3 секунды,
    и первые ~2 секунды клипа гейн «въезжает»: замер по готовому файлу
    01.09.2026 показал +5.5 дБ на старте против +23 дБ дальше. На слух это
    ровно затухание в начале — пользователь его услышал и попросил убрать совсем.

    Постоянный гейн такого разгона не даёт по построению. Пики ловит alimiter.
    """
    if target_i is None:
        target_i = C.LOUDNESS_TARGET
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", str(wav_path), "-af",
             f"loudnorm=I={target_i}:TP={target_tp}:LRA=11:print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        txt = (r.stderr or "") + (r.stdout or "")
        i = txt.rfind("{")
        data = json.loads(txt[i:txt.rfind("}") + 1])
        in_i = float(data["input_i"])
        in_tp = float(data["input_tp"])
        if in_i < -70:            # тишина — нечего усиливать
            return 0.0
        g = target_i - in_i
        if in_tp + g > target_tp:  # не даём уйти в клиппинг
            g = target_tp - in_tp
        return round(max(-20.0, min(40.0, g)), 2)
    except Exception as e:
        print(f"    (замер громкости не удался: {e}; оставляю как есть)")
        return 0.0


def build_ass(phrases, title=None, dur=None, caps=False, words=None, nocensor=False):
    """
    Собирает .ass.

    ГЛАВНОЕ ПРО РЕФЕРЕНСЫ: во всех трёх присланных клипах текст
    НЕ МЕНЯЕТСЯ ВООБЩЕ — это один статичный заголовок в две строки,
    который висит от первого до последнего кадра. Субтитров речи там нет.
    Проверено покадрово: «якут не признается что / дарят на др в Якутии»
    висит все 79 секунд без изменений.

    Поэтому title (заголовок) — основной режим, phrases — опция.
    Верхняя строка жёлтая, нижняя белая, нижний регистр.
    """
    def ts(t):
        t = max(0.0, t)
        h, r = divmod(t, 3600)
        m, s = divmod(r, 60)
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"

    # Референс не держит кегль фиксированным: он подогнан так, чтобы самая
    # длинная строка занимала ~88% ширины. Короткий заголовок => крупнее.
    fs = C.FONT_SIZE
    if title and caps:
        title = title.upper()
    if title:
        longest = max(len(x) for x in wrap2(title.strip()) if x)
        fs = int(min(C.FONT_SIZE_MAX, max(C.FONT_SIZE_MIN,
                     C.OUT_W * C.FIT_WIDTH / (longest * C.CHAR_W))))
    step = int(fs * C.LINE_STEP_K)
    outl = max(4, round(C.OUTLINE * fs / C.FONT_SIZE))

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {C.OUT_W}
PlayResY: {C.OUT_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: L1,{C.FONT_NAME},{fs},{C.COLOR_RED_TITLE if caps else C.COLOR_LINE1},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,{outl},0,8,40,40,{C.SUB_MARGIN_TOP},204
Style: L2,{C.FONT_NAME},{fs},{C.COLOR_LINE2},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,{outl},0,8,40,40,{C.SUB_MARGIN_TOP+step},204
Style: SUB,{C.FONT_NAME},{C.GAME_SUB_SIZE},{C.COLOR_LINE2},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,6,0,8,40,40,{C.GAME_SUB_TOP},204

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    # Заголовок висит весь клип — ровно как в референсах.
    if title:
        t1, t2 = wrap2(esc_ass(title.strip()), C.MAX_CHARS_PER_LINE)
        end = ts(dur if dur else 60)
        ev.append(f"Dialogue: 1,{ts(0.0)},{end},L1,,0,0,0,,{t1}")
        if t2:
            ev.append(f"Dialogue: 1,{ts(0.0)},{end},L2,,0,0,0,,{t2}")

    # Пословные субтитры ПОД заголовком (--wordsubs). Заголовок висит вверху
    # на стыке вебки и геймплея, слова — внизу по геймплею, не пересекаются.
    # Нужно на разговорных моментах: речь на стриме тихая и невнятная.
    if words:
        for i, w in enumerate(words):
            st, en = w["s"], w["e"]
            nxt = words[i + 1]["s"] if i + 1 < len(words) else en + 0.4
            en = min(max(en, st + 0.25), nxt - 0.01, dur if dur else en + 0.4)
            if en <= st:
                continue
            txt = w['w'].upper()
            # Мат на экране закрываем звёздочками — то же решение, что и в
            # стиле cam (см. память project_clip_censor). Звук не трогаем.
            if nocensor is not True:
                txt = censor(txt)
            ev.append(f"Dialogue: 0,{ts(st)},{ts(en)},SUB,,0,0,0,,"
                      f"{esc_ass(txt)}")

    # Субтитры речи фразами — только если заголовка нет (иначе наложатся).
    if not title:
        for p in phrases:
            st, en = p["start"], p["end"]
            if dur:
                if st >= dur:
                    continue
                en = min(en, dur)
            if en - st < 0.15:
                continue
            l1, l2 = wrap2(esc_ass(p["text"]))
            if not l1:
                continue
            ev.append(f"Dialogue: 0,{ts(st)},{ts(en)},L1,,0,0,0,,"
                      f"{{\\fad(60,60)}}{l1}")
            if l2:
                ev.append(f"Dialogue: 0,{ts(st)},{ts(en)},L2,,0,0,0,,"
                          f"{{\\fad(60,60)}}{l2}")
    return head + "\n".join(ev) + "\n"


ASS_RENDER_K = getattr(C, "ASS_RENDER_K", 0.636)   # свой у каждого шрифта, см. fit_size_px()


def fit_size_px(lines, max_w_px, size_max=None, size_min=None):
    """
    Подбирает кегль, РЕАЛЬНО измеряя строку шрифтом, а не по коэффициенту.

    Зачем: `CHAR_W = 0.35` снят на строчных буквах игрового заголовка,
    а бытовой заголовок всегда КАПСОМ — заглавные Montserrat почти вдвое
    шире строчных. Из-за этого длинная строка молча уезжала за края кадра:
    в клипе «завтрак миллиардера» вместо «НЕМНОЖКО МЕЖДУ КАТКАМИ» на экране
    было «ЕМНОЖКО МЕЖДУ КАТКАМ» — обрезано с обеих сторон. Заметить это
    можно было только просмотром готового файла.

    Игровой стиль намеренно НЕ трогаем: там вид подбирался в три захода
    и утверждён владельцем.
    """
    from PIL import ImageFont
    # libass рисует шрифт МЕЛЬЧЕ, чем PIL при том же номинальном кегле.
    # Замерено по трём готовым клипам (строка «ЗДОРОВЫЙ РЕЖИМ ДНЯ» и др.):
    # отношение отрисованной ширины к измеренной PIL = 0.636, стабильно
    # на разных кеглях и строках. Без поправки заголовок выходил 53 % ширины
    # кадра вместо 83 % у эталона второго аккаунта — то есть ровно та
    # ошибка «заголовок вдвое мельче», из-за которой провалилась первая заливка.
    # 0.636 — значение для Montserrat. У другого шрифта оно своё: при смене
    # шрифта замерить по готовому клипу и задать ASS_RENDER_K в мои_настройки.py.
    max_w_px = max_w_px / ASS_RENDER_K
    size_max = C.FONT_SIZE_MAX if size_max is None else size_max
    size_min = C.FONT_SIZE_MIN if size_min is None else size_min
    lines = [t for t in lines if t]
    if not lines:
        return size_max
    lo, hi, best = size_min, size_max, size_min
    while lo <= hi:
        mid = (lo + hi) // 2
        f = ImageFont.truetype(str(FONTS / C.FONT_FILE), mid)
        w = max(f.getbbox(t)[2] - f.getbbox(t)[0] for t in lines)
        if w <= max_w_px:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


def remap_times(items, keep, ka="s", kb="e", xfade=0.0):
    """
    Переносит тайминги слов/фраз из НЕПРЕРЫВНОГО окна исходника в склеенный клип.

    Зачем: транскрипт снимается с непрерывного куска `start..start+dur`,
    а видео на выходе склеено из `keep`. Если из середины вырезали 5 секунд,
    все последующие слова на выходе опаздывают ровно на эти 5 секунд.
    Баг был и в обычном вырезании пустот, просто там смещение появлялось
    в самом конце клипа и в глаза не бросалось; на явной сборке из кусков
    (--segments) субтитры разъехались сразу и заметно.

    Слова, попавшие в вырезанное, выбрасываются.
    """
    out = []
    for it in items:
        t0, t1 = it[ka], it[kb]
        off = 0.0
        for i, (a, b) in enumerate(keep):
            # при плавном переходе каждый следующий кусок наезжает на предыдущий
            off_i = off - i * xfade
            if a <= t0 < b:
                new = dict(it)
                new[ka] = off_i + (t0 - a)
                new[kb] = off_i + (min(t1, b) - a)
                out.append(new)
                break
            off += b - a
    return out


def build_ass_life(words, title, dur, nocensor=False):
    """
    .ass для «бытового» стиля второго аккаунта (образец чужой удачной нарезки).

    Отличия от игрового: заголовок КАПСОМ сверху (красная строка + белая),
    и обязательные пословные субтитры белым капсом внизу — по одному слову,
    синхронно с речью. Именно они держат внимание, когда в кадре только
    говорящая голова и нет геймплея.
    """
    def ts(t):
        t = max(0.0, t)
        h, r = divmod(t, 3600)
        m, s = divmod(r, 60)
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"

    if title and nocensor is not True:
        title = censor(title.upper())
    t1p, t2p = wrap2_top_heavy(esc_ass(title.strip().upper()), 20) if title else ("", "")
    # 0.84 ширины кадра — как в образце второго аккаунта, где длинная
    # строка занимает 83%. Меряем шрифтом, см. fit_size_px().
    ts_ = fit_size_px([t1p, t2p], C.OUT_W * 0.83, size_max=C.LIFE_TITLE_SIZE_MAX)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {C.OUT_W}
PlayResY: {C.OUT_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: T1,{C.FONT_NAME},{ts_},{C.LIFE_COLOR_RED},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,5,0,8,40,40,{C.LIFE_TITLE_TOP},204
Style: T2,{C.FONT_NAME},{ts_},{C.COLOR_LINE2},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,5,0,8,40,40,{C.LIFE_TITLE_TOP+int(ts_*1.04)},204
Style: SUB,{C.FONT_NAME},{C.LIFE_SUB_SIZE},{C.COLOR_LINE2},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,6,0,8,40,40,{C.LIFE_SUB_TOP},204

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if title:
        end = ts(dur)
        ev.append(f"Dialogue: 1,{ts(0.0)},{end},T1,,0,0,0,,{t1p}")
        if t2p:
            ev.append(f"Dialogue: 1,{ts(0.0)},{end},T2,,0,0,0,,{t2p}")

    # по одному слову, как в образце
    step = max(1, C.LIFE_SUB_MAX_WORDS)
    for i in range(0, len(words), step):
        grp = words[i:i + step]
        st, en = grp[0]["s"], grp[-1]["e"]
        nxt = words[i + step]["s"] if i + step < len(words) else en + 0.4
        en = min(max(en, st + 0.25), nxt - 0.01, dur)
        if en <= st:
            continue
        txt = join_words(grp).upper()
        if nocensor is not True:
            txt = censor(txt)
        txt = esc_ass(txt)
        ev.append(f"Dialogue: 0,{ts(st)},{ts(en)},SUB,,0,0,0,,{txt}")
    return head + "\n".join(ev) + "\n"


def censor(text):
    """
    Закрывает мат в ЭКРАННОМ тексте. Звук не трогается.

    Так сделано в эталонах 8, 9 и 10: на экране «С#КА», «БЛ**Ь», «на*уй»,
    а в звуке слово целиком. Дешёвая страховка от ограничения охвата,
    особенно на Instagram. Таблица и корни — в config.py.
    """
    import re

    def one(m):
        w = m.group(0)
        low = w.lower()
        rep = C.CENSOR_MAP.get(low)
        if rep is None:
            if not any(low.startswith(r) for r in C.CENSOR_ROOTS):
                return w
            # общее правило: закрываем третий знак
            if len(low) < 3:
                return w
            rep = low[:2] + C.CENSOR_CHAR + low[3:]
        # сохраняем регистр: КАПС остаётся КАПСОМ
        return rep.upper() if w.isupper() and len(w) > 1 else rep

    return re.sub(r"[А-Яа-яЁё]+", one, text)


def join_words(grp):
    """Склеивает слова в строку субтитра.

    Распознавание отрывает частицы «-то», «-ка», «-нибудь» в отдельное слово,
    и на экране получалось «ЧТО -ТО», «КАКОЙ -ТО» с пробелом перед дефисом
    (поймано 10.09.2026 на клипе про усталость). Поэтому слово, начинающееся
    с дефиса, приклеиваем к предыдущему без пробела.
    """
    out = ""
    for w in grp:
        t = w["w"]
        if out and t.startswith(("-", "—")):
            out += t
        elif out:
            out += " " + t
        else:
            out = t
    return out


def group_words(words, max_words, gap=0.28, max_chars=None):
    """
    Режет пословный поток на группы ПО ПАУЗАМ, по числу слов И по числу знаков.

    Механический шаг «каждые 3 слова» рвёт фразу посередине: на тесте вышло
    «трек, я на» / «наверное, три. а». В эталонах группы всегда осмысленные:
    «ты жирный» / «а ты нищий». Поэтому режем там, где в речи есть пауза,
    и добираем до max_words только внутри непрерывного куска.

    Ограничение по ЗНАКАМ добавлено 31.08.2026 по замечанию пользователя: три слова
    бывают короткие («а ты нищий»), а бывают «здравствуйте, великолепный,
    восхитительный» — и такая строка уезжает за оба края кадра. Считать надо
    длину строки, а не количество слов.
    """
    max_chars = C.CAM_SUB_MAX_CHARS if max_chars is None else max_chars
    out, cur = [], []
    for i, w in enumerate(words):
        # если слово уже не влезает в текущую группу — закрываем группу
        if cur and len(" ".join(x["w"] for x in cur)) + 1 + len(w["w"]) > max_chars:
            out.append(cur)
            cur = []
        cur.append(w)
        last = i == len(words) - 1
        pause = (not last) and (words[i + 1]["s"] - w["e"] >= gap)
        if last or pause or len(cur) >= max_words:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def build_ass_cam(words, title, dur, nocensor=False):
    """
    .ass для стиля «только вебка» (эталон 10 «ОТВЕТ ХЕЙТЕРУ»).

    Отличия от бытового: заголовок в ОДНУ строку и жёлтый, а не красно-белый
    в две; субтитры СТРОЧНЫМИ, а не капсом, и по 2-3 слова, а не по одному.
    И то и другое лежит ВНУТРИ картинки, у её кромок, а не на чёрном поле.
    Всё замерено покадрово по эталону, числа в config.py, раздел «СТИЛЬ 3».
    """
    def ts(t):
        t = max(0.0, t)
        h, r = divmod(t, 3600)
        m, s = divmod(r, 60)
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"

    t = title.strip().upper() if title else ""
    if t and nocensor is not True:
        t = censor(t)
    line = esc_ass(t)
    # Заголовок здесь однострочный. Если строка длинная, кегль сам уедет вниз
    # по ширине — переносить на вторую строку не надо, в эталоне её нет.
    tsz = fit_size_px([line], C.OUT_W * C.CAM_TITLE_FIT,
                      size_max=C.CAM_TITLE_SIZE_MAX) if line else C.CAM_TITLE_SIZE_MAX
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {C.OUT_W}
PlayResY: {C.OUT_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: T1,{C.FONT_NAME},{tsz},{C.CAM_COLOR_YELLOW},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,6,0,8,30,30,{C.CAM_TITLE_TOP},204
Style: SUB,{C.FONT_NAME},{C.CAM_SUB_SIZE},{C.COLOR_LINE2},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,6,0,8,40,40,{C.CAM_SUB_TOP},204

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if line:
        ev.append(f"Dialogue: 1,{ts(0.0)},{ts(dur)},T1,,0,0,0,,{line}")

    # По 2-3 слова за раз и ПО ПАУЗАМ — в эталоне группы осмысленные.
    groups = group_words(words, max(1, C.CAM_SUB_MAX_WORDS))
    for gi, grp in enumerate(groups):
        st, en = grp[0]["s"], grp[-1]["e"]
        nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else en + 0.4
        en = min(max(en, st + 0.25), nxt - 0.01, dur)
        if en <= st:
            continue
        # СТРОЧНЫМИ — единственное место, где мы не поднимаем регистр.
        txt = join_words(grp).lower()
        if nocensor is not True:
            txt = censor(txt)
        # Страховка от вылета за края: меряем строку шрифтом и, если она шире
        # положенного, сбиваем кегль именно у неё тегом {sN}. Общий кегль
        # не трогаем — иначе весь клип поедет мельче из-за одной длинной строки.
        fs = fit_size_px([txt.upper()], C.OUT_W * C.CAM_SUB_FIT,
                         size_max=C.CAM_SUB_SIZE, size_min=48)
        pre = "" if fs >= C.CAM_SUB_SIZE else "{\fs%d}" % fs
        ev.append(f"Dialogue: 0,{ts(st)},{ts(en)},SUB,,0,0,0,,{pre}{esc_ass(txt)}")
    return head + '\n'.join(ev) + '\n'


def build_ass_duo(words, title, dur, caps=True, nocensor=False, pfx="DUO",
                  cam_bottom=None, sub_top=None, title_w=None):
    """
    .ass для стиля `duo` — формат чужих нарезок конфликта с бабой (08.09.2026).

    Раскладка как в игровом стиле (вебка сверху, игра снизу), но вебка крупнее
    и шов проходит по 47 % высоты. Заголовок — ДВЕ строки КАПСОМ ровно на шве:
    первая строка на нижней кромке вебки, вторая уже на геймплее. Цвета
    красный+белый; `--lower` меняет их местами (так сделан один из пяти
    присланных эталонов).

    Субтитры пословные, белым капсом, в верхней трети блока геймплея —
    туда, где они стоят у них. Мат закрывается звёздочками, звук не трогаем.

    pfx выбирает набор координат: DUO (вебка 47 %) или TALK (вебка 70 %).
    При TITLE_ALIGN_BOTTOM заголовок не на шве, а прижат к низу блока вебки.

    cam_bottom, sub_top, title_w — для раскладки meme с вебкой не во всю
    ширину: низ окна вебки, верх субтитров и ширина под заголовок в px.
    """
    P = lambda k: getattr(C, f"{pfx}_{k}")

    def ts(t):
        t = max(0.0, t)
        h, r = divmod(t, 3600)
        m, s = divmod(r, 60)
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"

    t1p, t2p = wrap2_top_heavy(esc_ass(title.strip().upper()), 20) if title else ("", "")
    if title and nocensor is not True:
        t1p, t2p = censor(t1p), censor(t2p)
    # Кегль меряем настоящим шрифтом: заголовок всегда КАПСОМ, а CHAR_W
    # снят на строчных и врёт почти вдвое (см. fit_size_px).
    tsz = fit_size_px([t1p, t2p], title_w or C.OUT_W * P("TITLE_FIT"),
                      size_max=P("TITLE_SIZE_MAX"))
    step = int(tsz * P("TITLE_STEP_K"))
    title_top = P("TITLE_TOP")
    if title and getattr(C, "TITLE_ALIGN_BOTTOM", False):
        # Заголовок прижат к низу блока вебки: последняя строка кончается
        # за TITLE_BOTTOM_PAD пикселей до игры, лицо остаётся открытым.
        title_top = max(0, (cam_bottom or P("CAM_OUT_H")) - C.TITLE_BOTTOM_PAD - (step if t2p else 0)
                        - int(tsz * C.TITLE_LINE_K))
    c1 = C.LIFE_COLOR_RED if caps is not False else C.COLOR_LINE2
    c2 = C.COLOR_LINE2 if caps is not False else C.LIFE_COLOR_RED

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {C.OUT_W}
PlayResY: {C.OUT_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: T1,{C.FONT_NAME},{tsz},{c1},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,5,0,8,40,40,{title_top},204
Style: T2,{C.FONT_NAME},{tsz},{c2},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,5,0,8,40,40,{title_top+step},204
Style: SUB,{C.FONT_NAME},{P("SUB_SIZE")},{C.COLOR_LINE2},&H000000FF,{C.COLOR_OUTLINE},&H00000000,0,0,0,0,100,100,0,0,1,6,0,8,40,40,{sub_top or P("SUB_TOP")},204

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if title:
        end = ts(dur)
        ev.append(f"Dialogue: 1,{ts(0.0)},{end},T1,,0,0,0,,{t1p}")
        if t2p:
            ev.append(f"Dialogue: 1,{ts(0.0)},{end},T2,,0,0,0,,{t2p}")

    grp_n = max(1, P("SUB_MAX_WORDS"))
    for i in range(0, len(words), grp_n):
        grp = words[i:i + grp_n]
        st, en = grp[0]["s"], grp[-1]["e"]
        nxt = words[i + grp_n]["s"] if i + grp_n < len(words) else en + 0.4
        en = min(max(en, st + 0.25), nxt - 0.01, dur)
        if en <= st:
            continue
        txt = join_words(grp).upper()
        # Распознавание рвёт смех на куски и клеит дефис спереди: на экране
        # стояло «-ХА,». Дефис в начале слова смысла не несёт, только мусорит.
        txt = txt.lstrip("-— ").strip()
        if not txt:
            continue
        if nocensor is not True:
            txt = censor(txt)
        ev.append(f"Dialogue: 0,{ts(st)},{ts(en)},SUB,,0,0,0,,{esc_ass(txt)}")
    return head + chr(10).join(ev) + chr(10)


def fix_word(w):
    """Правит известные ошибки распознавания, см. C.ASR_FIX."""
    core = w.strip()
    tail = ""
    while core and core[-1] in ".,!?…:;":
        tail = core[-1] + tail
        core = core[:-1]
    rep = getattr(C, "ASR_FIX", {}).get(core.lower())
    if not rep:
        return w
    if core.isupper():
        rep = rep.upper()
    elif core[:1].isupper():
        rep = rep.capitalize()
    return rep + tail


def load_words(path):
    """
    04.09.2026: слова для пословных субтитров, снятые и ПРОВЕРЕННЫЕ руками
    (JSON: [{"w","s","e"}], секунды от начала клипа). Нужно, когда клип
    склеен из кусков и распознавание на склейке даёт мусор («Доксирия»,
    «ГБН», «11D»): проверенные слова точнее любого повторного прогона.
    """
    with open(path, encoding="utf-8") as f:
        ws = json.load(f)
    out = []
    for w in ws:
        t = str(w["w"]).strip()
        if t:
            out.append({"w": t, "s": float(w["s"]), "e": float(w["e"])})
    print(f"    слова субтитров взяты из файла: {len(out)} шт.")
    return out


def clip_words(wav_path, model=None):
    """Слова с таймкодами — для пословных субтитров."""
    stub_av()
    from faster_whisper import WhisperModel
    m = model or WhisperModel(C.ASR_MODEL_FINAL, device="cpu",
                              compute_type="int8", cpu_threads=C.ASR_THREADS)
    segs, _ = m.transcribe(read_wav_f32(wav_path), language="ru", beam_size=5,
                           word_timestamps=True, vad_filter=True,
                           vad_parameters=dict(min_silence_duration_ms=300))
    out = []
    for s in segs:
        for w in (s.words or []):
            t = fix_word(w.word.strip())
            if t:
                out.append({"w": t, "s": w.start, "e": w.end})
    return out


def transcribe_clip(wav_path, model=None):
    """Точный транскрипт короткого клипа фразами по 3-6 слов."""
    stub_av()
    from faster_whisper import WhisperModel
    m = model or WhisperModel(C.ASR_MODEL_FINAL, device="cpu",
                              compute_type="int8", cpu_threads=C.ASR_THREADS)
    segs, _ = m.transcribe(read_wav_f32(wav_path), language="ru", beam_size=5,
                           word_timestamps=True, vad_filter=True,
                           vad_parameters=dict(min_silence_duration_ms=300))
    words = []
    for s in segs:
        for w in (s.words or []):
            t = fix_word(w.word.strip())
            if t:
                words.append({"w": t, "s": w.start, "e": w.end})
    # Склеиваем в короткие фразы — так читается, как в референсах.
    phrases, buf = [], []
    for w in words:
        buf.append(w)
        long_pause = len(buf) > 1 and (w["s"] - buf[-2]["e"]) > 0.55
        if len(buf) >= 5 or long_pause or re.search(r"[.!?…]$", w["w"]):
            phrases.append({"start": buf[0]["s"], "end": buf[-1]["e"],
                            "text": " ".join(x["w"] for x in buf)})
            buf = []
    if buf:
        phrases.append({"start": buf[0]["s"], "end": buf[-1]["e"],
                        "text": " ".join(x["w"] for x in buf)})
    # Растягиваем короткие фразы, чтобы глаз успевал.
    for i, p in enumerate(phrases):
        nxt = phrases[i + 1]["start"] if i + 1 < len(phrases) else p["end"] + 1
        p["end"] = min(max(p["end"], p["start"] + 0.9), nxt - 0.02)
    return phrases


def audio_rms(wav_path, step=0.1):
    """RMS по окнам step секунд. Возвращает (массив dBFS, шаг)."""
    w = wave.open(str(wav_path))
    sr, n = w.getframerate(), w.getnframes()
    data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    win = max(1, int(sr * step))
    cnt = len(data) // win
    if cnt == 0:
        return np.array([]), step
    rms = np.sqrt((data[:cnt * win].reshape(cnt, win) ** 2).mean(axis=1))
    return np.clip(20 * np.log10(rms + 1e-9), -80, 0), step


def game_crop_for(src):
    """
    Кроп игры для замеров движения и яркости.

    Настройки сняты с одной записи, а сырые клипы бывают меньше по размеру.
    Кроп за границей кадра ломает ffmpeg, поэтому он уменьшается пропорционально.
    """
    g = C.GAME_CROP
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(src)],
                           capture_output=True, text=True, errors="replace", timeout=60)
        width, height = (int(x) for x in r.stdout.strip().split(",")[:2])
    except (ValueError, OSError, subprocess.SubprocessError):
        return g
    k = min(1.0, width / (g["x"] + g["w"]), height / (g["y"] + g["h"]))
    return g if k >= 1 else {key: int(value * k) for key, value in g.items()}


def motion_profile(src, start, dur, fps=5):
    """Движение в геймплее по времени клипа — массив на каждый 1/fps секунды."""
    from PIL import Image
    import tempfile, shutil
    td = tempfile.mkdtemp()
    try:
        g = game_crop_for(src)
        sh(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(dur),
            "-i", str(src),
            "-vf", f"crop={g['w']}:{g['h']}:{g['x']}:{g['y']},"
                   f"scale=128:-1,fps={fps}",
            "-q:v", "6", os.path.join(td, "f_%05d.jpg")])
        fs = sorted(f for f in os.listdir(td) if f.endswith(".jpg"))
        if len(fs) < 2:
            return np.array([])
        ar = [np.asarray(Image.open(os.path.join(td, f)).convert("L"),
                         dtype=np.float32) for f in fs]
        return np.array([np.abs(ar[i + 1] - ar[i]).mean()
                         for i in range(len(ar) - 1)])
    finally:
        shutil.rmtree(td, ignore_errors=True)


def find_dead_air(wav_path, src, start, dur):
    """
    Ищет «пустоту» — куски, где ОДНОВРЕМЕННО молчит стример и стоит картинка.
    Это и есть то, что убивает удержание: зритель ждёт, пока что-то произойдёт.

    Одного звука мало: он может молча целиться в кого-то — это не пустота.
    Поэтому режем только там, где тихо И в игре ничего не движется.
    """
    db, step = audio_rms(wav_path, C.PAUSE_STEP)
    if len(db) == 0:
        return []
    mo = motion_profile(src, start, dur, fps=C.PAUSE_MOTION_FPS)

    # Порог тишины подстраиваем под клип: у тихих кусков свой уровень.
    speech = db[db > db.max() - 25]
    quiet_th = max(C.PAUSE_SILENCE_DB,
                   (float(np.median(speech)) if len(speech) else -45) - 12)

    def motion_at(t):
        if len(mo) == 0:
            return 999.0                    # не смогли померить — не режем
        i = min(len(mo) - 1, int(t * C.PAUSE_MOTION_FPS))
        return float(mo[i])

    # Сначала находим все интервалы тишины, потом решаем, какие резать.
    quiet, run = [], None
    for i, v in enumerate(db):
        t = i * step
        if v < quiet_th and run is None:
            run = t
        elif v >= quiet_th and run is not None:
            quiet.append((run, t))
            run = None
    if run is not None:
        quiet.append((run, len(db) * step))

    # Порог движения зависит от длины паузы. В Dota картинка шевелится всегда,
    # поэтому строгое «тихо И статика» не срабатывало почти никогда.
    # Логика: короткую паузу щадим, если в игре бой; длинную режем почти
    # всегда — четыре секунды тишины убивают удержание при любой картинке.
    dead = []
    for a, b in quiet:
        ln = b - a
        if ln < C.PAUSE_MIN_SEC:
            continue
        peak = max((motion_at(t) for t in np.arange(a, b, 0.2)), default=999.0)
        th = C.PAUSE_MOTION_TH if ln < C.PAUSE_LONG_SEC else C.PAUSE_MOTION_TH_LONG
        if peak < th:
            dead.append((a, b))
    return dead


def segments_from_dead(dur, dead):
    """Из списка пустот делаем список кусков, которые оставляем."""
    keep, cur = [], 0.0
    edge = getattr(C, "EDGE_PROTECT_SEC", 0.0)
    for a, b in dead:
        # оставляем «дыхание» по краям, чтобы речь не звучала обрубленной
        a = min(dur, a + C.PAUSE_KEEP_EDGE)
        b = max(0.0, b - C.PAUSE_KEEP_EDGE)
        if b - a < C.PAUSE_MIN_SEC:
            continue
        # Защищённые края клипа: в первые и последние EDGE_PROTECT_SEC секунд
        # не режем. Но паузу, которая лишь ЗАХОДИТ в край, отбрасывать целиком
        # нельзя — иначе длинная тишина в середине остаётся в клипе (поймано
        # 02.09.2026: пауза начиналась на 1.6 сек и клип раздулся с 12 до 29).
        # Поэтому урезаем сам вырез до защищённой зоны, а не отменяем его.
        if edge:
            a = max(a, edge)
            b = min(b, dur - edge)
            if b - a < C.PAUSE_MIN_SEC:
                continue
        if a > cur + 0.4:
            keep.append((cur, a))
        cur = b
    if dur - cur > 0.4:
        keep.append((cur, dur))
    return keep or [(0.0, dur)]


def tune_start(src, start, dur, look=5.0):
    """
    Сдвигает точку входа так, чтобы клип НАЧИНАЛСЯ СО ЗВУКА.

    Это не украшение. Первый залитый клип открывался ровно секундой цифровой
    тишины (первый слышимый звук на 1.15 сек), тогда как успешные чужие нарезки
    звучат с 0.3 сек. Среднее время просмотра вышло 5.37 сек при длине 34 —
    зритель уходил, не дождавшись начала.
    """
    probe = job_dir() / "probe.wav"
    sh(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(look),
        "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(probe)])
    db, step = audio_rms(probe, 0.05)
    if len(db) == 0:
        return start, 0.0
    hits = np.where(db > C.START_LOUD_DB)[0]
    if len(hits) == 0:
        return start, 0.0
    first = float(hits[0]) * step
    if first <= C.START_MAX_SILENCE:
        return start, 0.0
    shift = max(0.0, first - 0.12)          # чуть раньше звука, не впритык
    # Потолок сдвига: без него вход уезжал вперёд настолько, что срезал тихую
    # атаку первого слова — пользователь слышал это как «обрывается на первом слове».
    shift = min(shift, getattr(C, "START_SHIFT_MAX", shift))
    return start + shift, shift


def pick_title_color(src, start, dur):
    """
    Сам выбирает цвет заголовка по яркости фона под ним.

    Жёлтый тонет на светлом (песок, небо, снег) — там читается красный.
    На тёмном наоборот: красный сливается, а жёлтый горит.
    Меряем полосу, куда ляжет текст, по нескольким кадрам клипа.
    """
    from PIL import Image
    import tempfile, shutil
    td = tempfile.mkdtemp()
    try:
        g = game_crop_for(src)
        # верх геймплея — ровно та полоса, где будет заголовок
        band_h = int(g["h"] * (C.LINE_STEP * 2.2) / C.GAME_OUT_H)
        sh(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(dur),
            "-i", str(src),
            "-vf", f"crop={g['w']}:{band_h}:{g['x']}:{g['y']},"
                   f"scale=120:-1,fps=1",
            "-q:v", "5", os.path.join(td, "b_%03d.jpg")])
        fs = sorted(f for f in os.listdir(td) if f.endswith(".jpg"))
        if not fs:
            return False
        vals = [np.asarray(Image.open(os.path.join(td, f)).convert("L"),
                           dtype=np.float32).mean() for f in fs]
        return float(np.mean(vals)) > C.TITLE_LIGHT_TH
    finally:
        shutil.rmtree(td, ignore_errors=True)


def head_motion(src, start, span=3.0):
    """Движение в геймплее за первые секунды — эталон референсов 10-14."""
    mo = motion_profile(src, start, span, fps=6)
    return float(mo.mean()) if len(mo) else 0.0


def fit_crop(area, aspect):
    """Самый большой кроп с пропорцией aspect (Ш/В) по центру области area."""
    if area["w"] / area["h"] > aspect:
        h = area["h"]
        w = h * aspect
    else:
        w = area["w"]
        h = w / aspect
    w, h = int(w) // 2 * 2, int(h) // 2 * 2
    return dict(w=w, h=h, x=area["x"] + (area["w"] - w) // 2,
                y=area["y"] + (area["h"] - h) // 2)


def meme_layout(mc, camscale, campos):
    """
    Пробная раскладка meme (просьба пользователя 2026-11-19: вебка в лучшем
    качестве, пусть даже меньше половины кадра). Вебка в исходных пропорциях,
    растянута в camscale раз (1.0 — пиксель в пиксель), стоит сверху или
    по центру. Полоса вебки во всю ширину, по бокам размытая копия той же
    вебки. Бой: при top — под полосой, при center — фоном на весь кадр.
    """
    w = int(mc[0] * camscale) // 2 * 2
    h = int(mc[1] * camscale) // 2 * 2
    if w < 200 or h < 150:
        raise SystemExit(f"--camscale {camscale}: вебка выходит {w}x{h}, слишком мелко")
    if w > C.OUT_W or h > C.OUT_H - 400:
        raise SystemExit(f"--camscale {camscale}: вебка выходит {w}x{h}, не влезает в кадр")
    y = 0 if campos == "top" else (C.OUT_H - h) // 4 * 2
    return dict(w=w, h=h, x=(C.OUT_W - w) // 4 * 2, y=y,
                fight_h=C.OUT_H - h if campos == "top" else C.OUT_H)


def render_clip(src, start, dur, out_path, title=None, subs=True, model=None,
                wordsubs=False,
                tighten=True, zoom=None, style="game", caps=None, camcrop=None, gamecrop=None, nocensor=False, blurbg=False,
                noshift=False, segments=None, xfade=0.0, words_file=None, gametrack=None,
                nick=None, fill=None, camscale=None, campos="top"):
    """
    segments=[(a,b), ...] — явная сборка клипа из НЕСКОЛЬКИХ кусков стрима
    (абсолютные секунды). Тогда вырезание пустот не работает: куски заданы
    руками. Два применения:
      1) «склейка сюжета» из эталона 5 — развязку доснять из другого места.
      2) вырезать из клипа кусок с чужой музыкой или чужим контентом.

    camcrop=(w,h,x,y) — откуда в исходнике брать вебку, вместо чисел из config.
    Нужен, когда стример разворачивает вебку на весь экран (показывает еду,
    что-то в руках): тогда вебка занимает не свой угол, а весь кадр 1920x1080,
    и обычный кроп берёт кусок стены. Пропорции кропа должны совпадать с окном,
    иначе картинку растянет.

    fill=(файл, секунда) — только для стиля meme: откуда брать бой для нижней
    половины кадра. Бой идёт без звука, непрерывно с этой секунды, сколько
    длится клип.
    """
    if style == "meme" and not fill:
        raise SystemExit("стилю meme нужен бой снизу: --fill СЕК")
    # Пробная раскладка meme: вебка в исходном качестве, сверху или по центру.
    lay = None
    if style == "meme" and camscale is None and C.MEME_CAMSCALE is not None:
        # Решение пользователя 2026-11-20: вебка мема в исходном качестве сверху.
        # Значение из настроек применяется, только если раскладка возможна;
        # иначе (--zoom или вебка не влезает в кадр) — старая раскладка 50/50.
        k = C.MEME_CAMSCALE
        cw, ch = (camcrop or C.MEME_CAM_CROP)[:2]
        if not zoom and 200 <= cw * k <= C.OUT_W and 150 <= ch * k <= C.OUT_H - 400:
            camscale = k
        else:
            print("    MEME_CAMSCALE не подходит к этому клипу, раскладка 50/50")
    if style == "meme" and (camscale is not None or campos != "top"):
        if zoom:
            raise SystemExit("--zoom не работает с --camscale и --campos")
        lay = meme_layout(camcrop or C.MEME_CAM_CROP,
                          camscale if camscale is not None else 1.0, campos)
    tmp_wav = job_dir() / "clip_tmp.wav"
    ass = job_dir() / "clip_tmp.ass"

    if segments:
        # Куски заданы явно — точку входа не двигаем и пустоты не ищем.
        noshift, tighten = True, False
    if noshift:
        # Осознанный отказ от сдвига: иногда сильный ОПОРНЫЙ КАДР стоит
        # дороже громкого первого звука. Пример — он поднимает к камере
        # таз с едой, а речь при этом тише порога, и tune_start съедал
        # именно этот кадр. Тишины там нет, есть тихая речь; loudnorm
        # всё равно вытянет громкость до -14 LUFS.
        shift = 0.0
        print("    вход НЕ сдвигаю (--noshift)")
    else:
        start, shift = tune_start(src, start, dur)
    if shift > 0:
        print(f"    вход сдвинут на +{shift:.2f} сек — клип начинался с тишины")
        dur = max(4.0, dur - shift)

    if caps is None:
        if style in ("duo", "talk", "wide", "meme"):
            # В этом формате цвет не зависит от фона: у всех пяти замеренных
            # эталонов заголовок красный сверху и белый снизу (в одном
            # наоборот). Автовыбор по яркости тут только мешает.
            caps = True
            print("    цвет заголовка: красная строка сверху, белая снизу "
                  "(как в эталонах; --lower меняет местами)")
        else:
            caps = pick_title_color(src, start, dur)
            print(f"    цвет заголовка: {'красный капсом (фон светлый)' if caps else 'жёлтый строчными (фон тёмный)'}")

    hm = head_motion(src, start)
    if hm < C.HEAD_MOTION_MIN:
        print(f"    ВНИМАНИЕ: в первые 3 сек мало движения ({hm:.1f}, "
              f"у хороших нарезок 10-14). Зритель может уйти сразу.")

    # Аудио клипа нужно и для поиска пустот, и для субтитров.
    sh(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(dur),
        "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(tmp_wav)])

    keep = [(0.0, float(dur))]
    if segments:
        keep = [(a - start, b - start) for a, b in segments]
        total = sum(b - a for a, b in keep)
        print(f"    сборка из {len(keep)} кусков, суммарно {total:.1f} сек")
    elif tighten:
        dead = find_dead_air(tmp_wav, src, start, dur)
        keep = segments_from_dead(float(dur), dead)
        cut_total = float(dur) - sum(b - a for a, b in keep)
        if cut_total > 0.3:
            print(f"    вырезаю пустоты: {len(dead)} шт, "
                  f"-{cut_total:.1f} сек ({dur:.0f} -> {dur-cut_total:.0f})")
        else:
            print("    пустот нет, режу как есть")
    n_x = max(0, len(keep) - 1) if xfade > 0 else 0
    final_dur = sum(b - a for a, b in keep) - n_x * xfade

    if style == "meme":
        # Бой короче клипа: нижняя половина замерла бы на последнем кадре
        # без всякой ошибки. Проверяем до рендера.
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(fill[0])],
                           capture_output=True, text=True, errors="replace", timeout=60)
        try:
            fill_len = float(r.stdout.strip())
        except ValueError:
            raise SystemExit(f"--fillsrc: не удалось узнать длительность {fill[0]}")
        if fill[1] + final_dur > fill_len:
            raise SystemExit(f"--fill: бою нужно {final_dur:.1f} сек с {fill[1]:.1f}, "
                             f"а файл кончается на {fill_len:.1f}")

    if style in ("life", "cam", "duo", "talk", "wide", "meme"):
        # Обычно в бытовом стиле и в «только вебке» субтитры нужны: в кадре одна
        # говорящая голова, без бегущего слова смотреть не на что.
        # Но с 01.09.2026 пользователь просит на КАЖДЫЙ момент четыре версии —
        # вебка и вебка+игра, каждая с субтитрами и без, чтобы выбрать самому.
        # Поэтому --nosubs здесь тоже обязан работать (раньше игнорировался).
        if not subs:
            words = []
            print("    без субтитров (--nosubs), остаётся только заголовок")
        else:
            print("    транскрибирую пословно...")
            words = load_words(words_file) if words_file else clip_words(tmp_wav, model)
            n0 = len(words)
            words = remap_times(words, keep, "s", "e", xfade)
            if len(words) != n0:
                print(f"    слов: {len(words)} (из {n0}; остальные попали в вырезанное)")
            else:
                print(f"    слов: {len(words)}")
        with io.open(ass, "w", encoding="utf-8") as f:
            if style == "cam":
                f.write(build_ass_cam(words, title, final_dur,
                                      nocensor=nocensor))
            elif style == "duo":
                f.write(build_ass_duo(words, title, final_dur,
                                      caps=caps, nocensor=nocensor))
            elif style == "talk":
                # Та же вёрстка, что у duo, но координаты из набора TALK_*:
                # вебка 70 % сверху, игра 30 % снизу.
                f.write(build_ass_duo(words, title, final_dur,
                                      caps=caps, nocensor=nocensor, pfx="TALK"))
            elif style == "wide":
                f.write(build_ass_duo(words, title, final_dur,
                                      caps=caps, nocensor=nocensor, pfx="WIDE"))
            elif style == "meme" and lay:
                # Заголовок под подбородком внутри окна вебки и не шире окна.
                # Субтитры на бое ниже полосы вебки.
                below = lay["y"] + lay["h"]
                sub_top = min(below + max(80, (C.OUT_H - below) // 3), C.OUT_H - 300)
                f.write(build_ass_duo(words, title, final_dur,
                                      caps=caps, nocensor=nocensor, pfx="MEME",
                                      cam_bottom=below, sub_top=sub_top,
                                      title_w=min(C.OUT_W * C.MEME_TITLE_FIT,
                                                  lay["w"] * 0.9)))
            elif style == "meme":
                f.write(build_ass_duo(words, title, final_dur,
                                      caps=caps, nocensor=nocensor, pfx="MEME"))
            else:
                f.write(build_ass_life(words, title, final_dur, nocensor=nocensor))
    else:
        phrases, words = [], None
        if wordsubs:
            print("    транскрибирую пословно...")
            words = load_words(words_file) if words_file else clip_words(tmp_wav, model)
            words = remap_times(words, keep, "s", "e", xfade)
            print(f"    слов: {len(words)}")
        elif subs and not title:
            print("    транскрибирую клип...")
            phrases = transcribe_clip(tmp_wav, model)
            phrases = remap_times(phrases, keep, "start", "end", xfade)
            print(f"    фраз: {len(phrases)}")
        with io.open(ass, "w", encoding="utf-8") as f:
            f.write(build_ass(phrases, title=title, dur=final_dur, caps=caps, nocensor=nocensor,
                              words=words))

    # Постоянный гейн на весь клип — без разгона в начале (см. linear_gain_db).
    _g = linear_gain_db(tmp_wav)
    AGAIN = (f"volume={_g}dB,alimiter=limit=0.85:level=disabled," if _g else
             "alimiter=limit=0.85:level=disabled,")
    print(f"    громкость: постоянный гейн {_g:+.1f} дБ (без нарастания)")

    pad = min(float(getattr(C, "SEEK_PAD", 5.0)), float(start))
    game, parts = C.GAME_CROP, []
    # ПЕРЕД резкой обнуляем таймлайн обеих дорожек.
    # 08.09.2026, жалоба пользователя «в конце зависает картинка на 2-3 секунды».
    # Причина: у записи с твича после перемотки первый КАДР приходит с меткой
    # времени 0.8-1.7 сек (звук при этом с нуля). Картинка при этом правильная,
    # сдвинуты только метки — но trim режет ИМЕННО по меткам, поэтому с хвоста
    # пропадало ровно столько же секунд видео, сколько составлял сдвиг.
    # Видеодорожка выходила короче звуковой на 0.7-1.65 сек, и плеер держал
    # последний кадр, пока доигрывал звук.
    # setpts=PTS-STARTPTS ставит первый кадр в ноль, дальше trim работает
    # по честным секундам. split/asplit нужны потому, что один выход фильтра
    # нельзя подключить к нескольким входам.
    # SEEK_PAD: перематываемся на 5 секунд РАНЬШЕ нужного и режем от метки
    # SEEK_PAD. Иначе на записях с твича видео уезжает вперёд относительно звука.
    # Замер 08.09.2026: при `-ss 27466.30` первый кадр приходит с меткой 1.733,
    # и это не сбитая метка, а честный сдвиг — перемотка прыгает ВПЕРЁД, до
    # ближайшего опорного кадра, тогда как звук начинается ровно с запрошенной
    # секунды. Сверка по пикселям: первый кадр готового клипа совпадал с
    # исходником в точке start+1.70 (совпадение 1.0000). То есть картинка шла
    # на 1.7 сек впереди звука, а с хвоста ровно столько же видео пропадало —
    # отсюда жалоба пользователя «в конце зависает картинка на 2-3 секунды».
    # Опорные кадры тут раз в ~2 сек, поэтому запаса в 5 сек хватает с избытком.
    for i, (a, b) in enumerate(keep):
        parts.append(f"[0:v]trim=start={pad+a:.3f}:end={pad+b:.3f},"
                     f"setpts=PTS-STARTPTS,settb=AVTB[cv{i}];")
        # микро-фейды по 15 мс убирают щелчок на стыке
        parts.append(f"[0:a]atrim=start={pad+a:.3f}:end={pad+b:.3f},"
                     f"asetpts=PTS-STARTPTS,"
                     f"afade=t=in:st=0:d=0.015,"
                     f"afade=t=out:st={max(0, b-a-0.015):.3f}:d=0.015[ca{i}];")
    if xfade > 0 and len(keep) > 1:
        # Плавный переход вместо стыка встык. пользователь: «всё должно плавно
        # переходить, а не прям сразу резаться». Каждый следующий кусок
        # наезжает на предыдущий на xfade секунд — и картинкой, и звуком.
        lens = [b - a for a, b in keep]
        vprev, aprev, acc = "cv0", "ca0", lens[0]
        for i in range(1, len(keep)):
            off = acc - xfade
            parts.append(f"[{vprev}][cv{i}]xfade=transition=fade:"
                         f"duration={xfade:.3f}:offset={off:.3f}[vx{i}];")
            parts.append(f"[{aprev}][ca{i}]acrossfade=d={xfade:.3f}:"
                         f"c1=tri:c2=tri[ax{i}];")
            vprev, aprev = f"vx{i}", f"ax{i}"
            acc += lens[i] - xfade
        parts.append(f"[{vprev}]null[jv];[{aprev}]anull[ja];")
    else:
        join = "".join(f"[cv{i}][ca{i}]" for i in range(len(keep)))
        parts.append(f"{join}concat=n={len(keep)}:v=1:a=1[jv][ja];")

    # Зум на геймплей: 1.0 -> ZOOM за ZOOM_DUR секунд, начиная с zoom[0].
    gz = ""
    if zoom:
        zt, zk = float(zoom[0]), float(zoom[1])
        # 31.08.2026: было crop с выражением по t — ffmpeg вычисляет w/h фильтра
        # crop ОДИН раз при инициализации, поэтому переменная t там недоступна:
        # «Error when evaluating the expression» и рендер падал целиком.
        # Правильный инструмент для наезда — zoompan, он считает z на каждом кадре.
        # Время берём из номера выходного кадра: on / OUT_FPS.
        сек = f"(on/{C.OUT_FPS})"
        z = (f"min(max(1,1+({zk}-1)*({сек}-{zt})/{C.ZOOM_DUR}),{zk})")
        # fps ПЕРЕД zoompan обязателен: он берёт один входной кадр на один
        # выходной (d=1), но частоту выхода объявляет своим параметром fps.
        # На входе 60 к/с, а объявлено 30 — и клип выходил ровно вдвое длиннее.
        gz = (f",fps={C.OUT_FPS},scale={int(C.OUT_W*2)}:{int(C.GAME_OUT_H*2)}:flags=lanczos,"
              f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":s={C.OUT_W}x{C.GAME_OUT_H}:fps={C.OUT_FPS},setsar=1")

    def gz_for(h):
        """Зум под высоту игрового блока конкретного стиля."""
        return (gz.replace(f":{int(C.GAME_OUT_H*2)}:flags", f":{int(h*2)}:flags")
                  .replace(f"x{C.GAME_OUT_H}:fps", f"x{h}:fps"))

    hf = ""                 # отражение убрано; заглушка, чтобы не править фильтры
    if style == "cam":
        # Только вебка, широкая 4:3, строго по центру — эталон 10.
        # Пропорция кропа обязана быть 4:3, иначе картинку растянет.
        cc = camcrop or C.KICK_CAM_CROP
        crop = f"crop={cc[0]}:{cc[1]}:{cc[2]}:{cc[3]},{hf}"
        if blurbg:
            # Фон: тот же кадр во весь холст, размытый и притемнённый.
            # Поверх — резкая полоса вебки на своём месте.
            bg = (f"[bg]{crop}"
                  f"scale={C.OUT_W}:{C.OUT_H}:force_original_aspect_ratio=increase,"
                  f"crop={C.OUT_W}:{C.OUT_H},gblur=sigma={C.BLUR_SIGMA},"
                  f"eq=brightness=-{C.BLUR_DARKEN},setsar=1[bgb];")
            fg = (f"[fg]{crop}"
                  f"scale={C.OUT_W}:{C.CAM_WIN_H}:flags=lanczos,setsar=1[fgs];")
            base = (f"[jv]split=2[bg][fg];" + bg + fg +
                    f"[bgb][fgs]overlay=0:{C.CAM_WIN_TOP}[st];")
        else:
            base = (f"[jv]{crop}"
                    f"scale={C.OUT_W}:{C.CAM_WIN_H}:flags=lanczos,setsar=1,"
                    f"pad={C.OUT_W}:{C.OUT_H}:0:{C.CAM_WIN_TOP}:black[st];")
        fc = "".join(parts) + base + (
            f"[st]subtitles=clip_tmp.ass:fontsdir=.[v];"
            f"[ja]{AGAIN}aresample=48000[a]"
        )
    elif style == "duo":
        # Формат чужих нарезок: вебка крупно сверху (47 % кадра) и ОТЗЕРКАЛЕНА,
        # игра снизу — как есть. Числа замерены по пяти их клипам, см. config.
        dc = camcrop or C.DUO_CAM_CROP
        dg = C.DUO_GAME_CROP
        hf_cam = "hflip," if C.DUO_CAM_MIRROR else ""
        hf_game = ""
        fc = "".join(parts) + (
            f"[jv]split=2[jv1][jv2];"
            f"[jv1]crop={dc[0]}:{dc[1]}:{dc[2]}:{dc[3]},{hf_cam}"
            f"scale={C.OUT_W}:{C.DUO_CAM_OUT_H}:flags=lanczos,setsar=1[cam];"
            f"[jv2]crop={dg['w']}:{dg['h']}:{dg['x']}:{dg['y']},{hf_game}"
            f"scale={C.OUT_W}:{C.DUO_GAME_OUT_H}:flags=lanczos{gz_for(C.DUO_GAME_OUT_H)},setsar=1[game];"
            f"[cam][game]vstack=inputs=2[st];"
            f"[st]subtitles=clip_tmp.ass:fontsdir=.[v];"
            f"[ja]{AGAIN}aresample=48000[a]"
        )
    elif style == "talk":
        # Вебка 70 % сверху, игра 30 % снизу (заказ пользователя). Лицо главное,
        # игра даёт движение в кадре и понятный контекст, но места почти
        # не занимает. Вебку НЕ зеркалим — это не формат конкурентов, а наш.
        tc = camcrop or C.TALK_CAM_CROP
        # --gamecrop нужен для сцен, где вебка стоит в другом углу кадра.
        tg = dict(zip("whxy", gamecrop)) if gamecrop else C.TALK_GAME_CROP
        fc = "".join(parts) + (
            f"[jv]split=2[jv1][jv2];"
            f"[jv1]crop={tc[0]}:{tc[1]}:{tc[2]}:{tc[3]},{hf}"
            f"scale={C.OUT_W}:{C.TALK_CAM_OUT_H}:flags=lanczos,setsar=1[cam];"
            f"[jv2]crop={tg['w']}:{tg['h']}:{tg['x']}:{tg['y']},{hf}"
            f"scale={C.OUT_W}:{C.TALK_GAME_OUT_H}:flags=lanczos{gz_for(C.TALK_GAME_OUT_H)},setsar=1[game];"
            f"[cam][game]vstack=inputs=2[st];"
            f"[st]subtitles=clip_tmp.ass:fontsdir=.[v];"
            f"[ja]{AGAIN}aresample=48000[a]"
        )
    elif style == "meme" and lay:
        mc = camcrop or C.MEME_CAM_CROP
        area = (dict(zip("whxy", gamecrop)) if gamecrop
                else getattr(C, "MEME_FILL_AREA", None) or C.MEME_FILL_CROP)
        mg = fit_crop(area, C.OUT_W / lay["fight_h"])
        # Полоса вебки: размытая копия на всю ширину + резкое окно по центру.
        band = (f"[jv]crop={mc[0]}:{mc[1]}:{mc[2]}:{mc[3]},{hf}split=2[cb][cf];"
                f"[cb]scale={C.OUT_W}:{lay['h']}:force_original_aspect_ratio=increase,"
                f"crop={C.OUT_W}:{lay['h']},gblur=sigma={C.BLUR_SIGMA},"
                f"eq=brightness=-{C.BLUR_DARKEN},setsar=1[cbb];"
                f"[cf]scale={lay['w']}:{lay['h']}:flags=lanczos,setsar=1[cfs];"
                f"[cbb][cfs]overlay={lay['x']}:0[cam];")
        game = (f"[1:v]setpts=PTS-STARTPTS,settb=AVTB,trim=duration={final_dur:.3f},"
                f"crop={mg['w']}:{mg['h']}:{mg['x']}:{mg['y']},{hf}"
                f"scale={C.OUT_W}:{lay['fight_h']}:flags=lanczos,setsar=1[game];")
        if campos == "top":
            stack = "[cam][game]vstack=inputs=2[st];"
        else:
            stack = f"[game][cam]overlay=0:{lay['y']}[st];"
        fc = "".join(parts) + band + game + stack + (
            f"[st]subtitles=clip_tmp.ass:fontsdir=.[v];"
            f"[ja]{AGAIN}aresample=48000[a]"
        )
    elif style == "meme":
        # Разговорный мем (шаблон пользователя с 2026-11-15): вебка сверху,
        # снизу бой из другого места записи, вход 1. Звук только со входа 0.
        # setpts обнуляет метки боя по той же причине, что и у кусков выше;
        # trim по длине клипа, чтобы бой не тянул ролик дальше речи.
        mc = camcrop or C.MEME_CAM_CROP
        mg = dict(zip("whxy", gamecrop)) if gamecrop else C.MEME_FILL_CROP
        fc = "".join(parts) + (
            f"[jv]crop={mc[0]}:{mc[1]}:{mc[2]}:{mc[3]},{hf}"
            f"scale={C.OUT_W}:{C.MEME_CAM_OUT_H}:flags=lanczos,setsar=1[cam];"
            f"[1:v]setpts=PTS-STARTPTS,settb=AVTB,trim=duration={final_dur:.3f},"
            f"crop={mg['w']}:{mg['h']}:{mg['x']}:{mg['y']},{hf}"
            f"scale={C.OUT_W}:{C.MEME_GAME_OUT_H}:flags=lanczos{gz_for(C.MEME_GAME_OUT_H)},setsar=1[game];"
            f"[cam][game]vstack=inputs=2[st];"
            f"[st]subtitles=clip_tmp.ass:fontsdir=.[v];"
            f"[ja]{AGAIN}aresample=48000[a]"
        )
    elif style == "wide":
        # Игровой момент: вебка узкой полосой сверху, основной кадр — игра.
        wc = camcrop or C.WIDE_CAM_CROP
        wg = dict(zip("whxy", gamecrop)) if gamecrop else C.WIDE_GAME_CROP
        # При --gametrack левый край кропа считается на каждом кадре.
        game_x = (f"'{track_x_expr(gametrack, start, wg['w'])}'" if gametrack
                  else wg["x"])
        fc = "".join(parts) + (
            f"[jv]split=2[jv1][jv2];"
            f"[jv1]crop={wc[0]}:{wc[1]}:{wc[2]}:{wc[3]},{hf}"
            f"scale={C.OUT_W}:{C.WIDE_CAM_OUT_H}:flags=lanczos,setsar=1[cam];"
            f"[jv2]crop={wg['w']}:{wg['h']}:{game_x}:{wg['y']},{hf}"
            f"scale={C.OUT_W}:{C.WIDE_GAME_OUT_H}:flags=lanczos{gz_for(C.WIDE_GAME_OUT_H)},setsar=1[game];"
            f"[cam][game]vstack=inputs=2[st];"
            f"[st]subtitles=clip_tmp.ass:fontsdir=.[v];"
            f"[ja]{AGAIN}aresample=48000[a]"
        )
    elif style == "life":
        # Только вебка, крупно, с чёрными полями сверху и снизу — как в образце.
        lc = camcrop or (C.LIFE_CAM_CROP_W, C.LIFE_CAM_CROP_H,
                         C.LIFE_CAM_CROP_X, C.LIFE_CAM_CROP_Y)
        pad_top = C.LIFE_WIN_TOP
        fc = "".join(parts) + (
            f"[jv]crop={lc[0]}:{lc[1]}:{lc[2]}:{lc[3]},{hf}"
            f"scale={C.OUT_W}:{C.LIFE_WIN_H}:flags=lanczos,setsar=1,"
            f"pad={C.OUT_W}:{C.OUT_H}:0:{pad_top}:black[st];"
            f"[st]subtitles=clip_tmp.ass:fontsdir=.[v];"
            f"[ja]{AGAIN}aresample=48000[a]"
        )
    else:
        gc = camcrop or (C.CAM_CROP_W, C.CAM_CROP_H, C.CAM_CROP_X, C.CAM_CROP_Y)
        fc = "".join(parts) + (
        f"[jv]split=2[jv1][jv2];"
        f"[jv1]crop={gc[0]}:{gc[1]}:{gc[2]}:{gc[3]},{hf}"
        f"scale={C.OUT_W}:{C.CAM_OUT_H}:flags=lanczos,setsar=1[cam];"
        f"[jv2]crop={game['w']}:{game['h']}:{game['x']}:{game['y']},{hf}"
        f"scale={C.OUT_W}:{C.GAME_OUT_H}:flags=lanczos{gz},setsar=1[game];"
        f"[cam][game]vstack=inputs=2[st];"
        f"[st]subtitles=clip_tmp.ass:fontsdir=.[v];"
        # Стрим тише референсов на 20 дБ — без этого клип теряется в ленте.
        f"[ja]{AGAIN}aresample=48000[a]"
    )
    # ffmpeg + libass на Windows спотыкается о двоеточие в "C:\...",
    # поэтому запускаем из личной папки процесса и берём относительные имена.
    # Шрифты берутся прямо из движок/fonts относительным путём.
    if fc.count(":fontsdir=.[") != fc.count("subtitles="):
        raise SystemExit("цепочка фильтров изменилась: не нашёл fontsdir у субтитров")
    fc = fc.replace(":fontsdir=.[", f":fontsdir={FONTS_REL}[")

    # Плашка с ником накладывается под заголовок и субтитры.
    nick = C.STREAMER_NICK if nick is None else nick
    if nick:
        render_nick_badge(nick, job_dir() / "clip_nick.png")
        fc = fc.replace(
            "[st]subtitles=",
            f"movie=clip_nick.png[nick];"
            f"[st][nick]overlay={C.NICK_BADGE_X}:{C.NICK_BADGE_Y}[stn];"
            f"[stn]subtitles=")
        if "[stn]subtitles=" not in fc:
            raise SystemExit("не удалось вставить плашку с ником в цепочку фильтров")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def run(filter_str, dst):
        # ВАЖНО: читаем из исходника на 2 секунды БОЛЬШЕ, чем нужно.
        # 08.09.2026 пользователь: «почему у всех видео в конце зависает картинка
        # на последних 2-3 секундах». Замер показал причину: при быстрой
        # перемотке (-ss до -i) видеодорожка выходила КОРОЧЕ звуковой
        # на 0.7-1.65 сек — ffmpeg отсчитывает -t от опорного кадра, а не
        # от запрошенной секунды, и хвост видео обрезался. Плеер в таком
        # файле держит последний кадр, пока доигрывает звук.
        # Точную длину всё равно задаёт фильтр trim/atrim, поэтому лишние
        # секунды на входе ни на что не влияют, кроме этого хвоста.
        # Бой для стиля meme — второй вход. Те же 2 секунды запаса на хвост.
        fill_in = []
        if style == "meme":
            fill_in = ["-ss", str(fill[1]), "-t", str(final_dur + 2.0),
                       "-i", str(Path(fill[0]).resolve())]
        sh(["ffmpeg", "-y", "-v", "error", "-ss", str(start - pad),
            "-t", str(pad + dur + 2.0),
            "-i", str(Path(src).resolve()), *fill_in,
            "-filter_complex", filter_str, "-map", "[v]", "-map", "[a]",
            "-r", str(C.OUT_FPS), "-c:v", "libx264", "-preset", C.PRESET,
            "-crf", str(C.CRF), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            str(Path(dst).resolve())], cwd=str(job_dir()))

    print("    рендерю...")
    t0 = time.time()
    tmp_out = job_dir() / "render.mp4"
    run(fc, tmp_out)
    finish_file(tmp_out, out_path)

    # Лёгкой копии «для телефона» больше НЕТ. 31.08.2026 пользователь сказал:
    # «забудь про то, что нужно делать для телефона, всё делай в хорошем
    # качестве, только для постинга в ТТ». Он смотрел именно сжатую копию
    # и решил, что качество плохое. Теперь на выходе один файл — тот,
    # который идёт в TikTok.
    print(f"    готово за {time.time()-t0:.0f} сек | {final_dur:.1f} сек")
    print(f"      файл: {out_path.name} "
          f"({out_path.stat().st_size/1e6:.1f} МБ)")
    return final_dur


def append_outro(clip_path, outro_path):
    """
    Дописывает заставку в конец готового ролика (решение пользователя 2026-11-12).

    Склейка с перекодированием: у заставки и ролика разные параметры кодека,
    поэтому склейка без перекодирования (-c copy) ненадёжна. Звук приводится
    к 48 кГц стерео с обеих сторон, иначе concat не соберёт дорожки.
    """
    import shutil
    clip_path, outro_path = Path(clip_path), Path(outro_path)
    if not outro_path.exists():
        raise SystemExit(f"нет файла заставки: {outro_path}")
    # Временный файл не в папке партии: если процесс оборвётся, мусор
    # не попадёт к готовым роликам и в архив приёмки. Папка у процесса своя,
    # поэтому два одновременных ролика с одинаковым именем не столкнутся.
    tmp = job_dir() / "outro.mp4"
    af = "aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo"
    fc = (f"[0:v]fps={C.OUT_FPS},setsar=1[v0];[1:v]fps={C.OUT_FPS},"
          f"scale={C.OUT_W}:{C.OUT_H},setsar=1[v1];"
          f"[0:a]{af}[a0];[1:a]{af}[a1];"
          f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
    try:
        sh(["ffmpeg", "-y", "-v", "error", "-i", str(clip_path.resolve()),
            "-i", str(outro_path.resolve()), "-filter_complex", fc,
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", C.PRESET,
            "-crf", str(C.CRF), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(tmp.resolve())])
        finish_file(tmp, clip_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    print(f"    заставка добавлена: {outro_path.name}")


def maybe_outro(out, nick=None, nooutro=False):
    """
    Заставка ставится, если она задана в настройках, не отключена флагом
    и клип режется для своего стримера. Чужой ник или пустой --nick
    (ролик без плашки, обычно чужой стрим) её отключают: на заставке
    лицо и ник STREAMER_NICK.
    """
    own = nick is None or nick == C.STREAMER_NICK
    if C.OUTRO_FILE and own and not nooutro:
        append_outro(out, HERE / C.OUTRO_FILE)


def stream_length_label(src):
    """Длительность записи для имени папки: "5ч 12м" или "47м"; при ошибке пусто."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(src)],
                           capture_output=True, text=True, errors="replace", timeout=60)
        minutes = int(float(r.stdout.strip()) // 60)
    except (ValueError, OverflowError, OSError, subprocess.TimeoutExpired):
        print("    ВНИМАНИЕ: не удалось узнать длительность записи для имени папки")
        return ""
    hours, minutes = divmod(minutes, 60)
    return f"{hours}ч {minutes}м" if hours else f"{minutes}м"


KINDS = ("Мем", "Файт", "Матч")
# Имя папки партии. Тип впереди (решение пользователя 2026-11-20), чтобы
# в проводнике папки группировались: «Мем …», «Файт …», «Матч …».
# Старые папки без типа тоже принимаются. Хвост — имя записи и длительность;
# в нём запрещены разделители путей и двоеточие.
BATCH_RE = re.compile(r"(?:(Мем|Файт|Матч) )?([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}-[0-9]{2})"
                      r"(?: ([^\\/:*?\"<>|\x00-\x1f]+))?")


def check_batch_name(name):
    if (not BATCH_RE.fullmatch(name) or ".." in name or name != name.strip()
            or "  " in name):
        raise SystemExit("--batch: нужно имя вида «Мем 2026-08-30 14-30 запись-5 47м» "
                         "или «2026-08-30 14-30»; без слэшей и двоеточий")
    return name


def batch_name(kind, src=None, name=None):
    """Каноническое имя новой партии: «<Тип> <дата> <время> <название>[ <длительность>]»."""
    from datetime import datetime
    if kind not in KINDS:
        raise SystemExit(f"--kind: одно из {', '.join(KINDS)}")
    if name is None:
        if not src:
            raise SystemExit("нужен исходник или --name")
        name = " ".join(p for p in (Path(src).stem, stream_length_label(src)) if p)
    name = " ".join(name.split())      # двойные пробелы из имени файла
    return check_batch_name(f"{kind} {datetime.now():%Y-%m-%d %H-%M} {name}")


def batch_dir(src, batch=None, kind=None):
    """
    Папка партии: Clips Ready/Мем 2026-08-30 14-30 запись-5 5ч 12м/

    Команда (/мем, /файт, /матч) берёт имя один раз в начале задачи
    (clipper.py batchname) и передаёт его в каждый cut через --batch.
    Так ролики одного запуска лежат в одной папке, а параллельные задачи
    в соседних чатах не попадают в чужую.

    Без --batch — старое поведение: берётся уже созданная сегодня папка
    этой записи (с тем же типом), иначе заводится новая.
    """
    from datetime import datetime
    if batch is not None:
        d = OUT / check_batch_name(batch)
    else:
        now = datetime.now()
        stem = Path(src).stem
        head = re.escape(kind) + " " if kind else ""
        # Длительность необязательна: сбой ffprobe не должен расколоть партию.
        pattern = (rf"{head}{now:%Y-%m-%d} \d{{2}}-\d{{2}} {re.escape(stem)}"
                   r"( (\d+ч )?\d+м)?")
        today = sorted(p for p in OUT.iterdir()
                       if p.is_dir() and re.fullmatch(pattern, p.name))
        if today:
            d = today[-1]
        elif kind:
            d = OUT / batch_name(kind, src)
        else:
            tail = " ".join(p for p in (stem, stream_length_label(src)) if p)
            d = OUT / f"{now:%Y-%m-%d %H-%M} {tail}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_batchname(args):
    """Печатает имя новой партии — команда передаёт его во все cut через --batch."""
    if args.src and not Path(args.src).is_file():
        raise SystemExit(f"нет файла: {args.src}")
    print(batch_name(args.kind, args.src, args.name))


def cmd_check(args):
    """
    Проверка готового клипа перед заливкой: нет ли в кадре чужой графики.

    Появилось не на пустом месте. TikTok пометил клип «опущенный калаш» как
    неоригинальный и указал интервал 00:06-00:18 — там в кадр попал БАННЕР
    ДОНАТ-АЛЕРТА со стрима (яркая картинка со спонсором). Чужая графика для
    TikTok = чужой логотип = неоригинальный контент.
    """
    from PIL import Image
    import tempfile, shutil
    print("[CHECK] ищу чужую графику в кадре")
    td = tempfile.mkdtemp()
    try:
        sh(["ffmpeg", "-y", "-v", "error", "-i", str(args.src),
            "-vf", "scale=270:480,fps=4", "-q:v", "4",
            os.path.join(td, "f_%04d.jpg")])
        fs = sorted(f for f in os.listdir(td) if f.endswith(".jpg"))
        if not fs:
            print("  не смог разобрать файл")
            return
        vals = []
        for f in fs:
            a = np.asarray(Image.open(os.path.join(td, f)).convert("RGB"),
                           dtype=np.int16)
            low = a[int(480 * 0.80):]          # низ кадра, туда лезут алерты
            sat = low.max(axis=2) - low.min(axis=2)
            vals.append(float(((sat > 70) & (low.mean(axis=2) > 120)).mean()))
        v = np.array(vals)
        med = float(np.median(v))
        th = max(med * 4 + 0.05, 0.12)
        bad = [(i / 4, x) for i, x in enumerate(v) if x > th]
        if not bad:
            print(f"  чисто (фон ровный, медиана {med*100:.1f}%)")
        else:
            print(f"  ПОДОЗРИТЕЛЬНО — посмотри эти моменты глазами:")
            for t, x in bad[:10]:
                print(f"    {t:5.1f} сек  ({x*100:.0f}% ярких насыщенных пикселей)")
            print("  Ложно срабатывает на небо, прицел, кору дерева, песок —")
            print("  всё яркое и насыщенное. Ищешь ПРЯМОУГОЛЬНУЮ вставку с")
            print("  картинкой: баннер донат-алерта, чужой логотип. Если он —")
            print("  бери другой момент или подними кроп в config.py.")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def render_nick_badge(nick, dst):
    """Рисует PNG-плашку: иконка Twitch и ник на полупрозрачной подложке."""
    from PIL import Image, ImageDraw, ImageFont

    # Рисуем с запасом и уменьшаем: так края получаются сглаженными.
    s = 4
    height, icon, pad, gap = (x * s for x in (C.NICK_BADGE_H, C.NICK_ICON_SIZE,
                                              C.NICK_BADGE_PAD, C.NICK_BADGE_GAP))
    font = ImageFont.truetype(str(FONTS / C.FONT_FILE), C.NICK_FONT_SIZE * s)
    width = int(pad + icon + gap + font.getlength(nick) + pad)

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=height // 4,
                           fill=(0, 0, 0, C.NICK_BADGE_ALPHA))

    # Контуры иконки заданы в квадрате 24x24.
    k = icon / 24
    top = (height - icon) / 2

    def shape(points):
        return [(pad + x * k, top + y * k) for x, y in points]

    purple, white = (145, 70, 255, 255), (255, 255, 255, 255)
    draw.polygon(shape([(6, 0), (1.714, 4.286), (1.714, 19.714), (6.857, 19.714),
                        (6.857, 24), (11.143, 19.714), (14.571, 19.714),
                        (22.286, 12), (22.286, 0)]), fill=purple)
    draw.polygon(shape([(6.857, 1.714), (20.571, 1.714), (20.571, 11.143),
                        (17.143, 14.571), (13.714, 14.571), (10.714, 17.571),
                        (10.714, 14.571), (6.857, 14.571)]), fill=white)
    for x0, x1 in ((11.571, 13.286), (16.286, 18.0)):
        draw.polygon(shape([(x0, 4.714), (x1, 4.714), (x1, 9.857), (x0, 9.857)]),
                     fill=purple)

    draw.text((pad + icon + gap, height / 2), nick, font=font, anchor="lm",
              fill=(255, 255, 255, 235))
    img.resize((width // s, height // s), Image.LANCZOS).save(dst)


def parse_gametrack(text):
    """Разбирает "СЕК:X,СЕК:X" в отсортированный список точек (секунда, центр X)."""
    points = []
    for part in text.split(","):
        try:
            sec, center = part.split(":")
            points.append((float(sec), float(center)))
        except ValueError:
            raise SystemExit(f"--gametrack: не понял точку «{part}», нужно СЕК:X")
    points.sort()
    if len(points) < 2:
        raise SystemExit("--gametrack: нужно минимум две точки")
    if any(b[0] <= a[0] for a, b in zip(points, points[1:])):
        raise SystemExit("--gametrack: две точки на одну и ту же секунду")
    return points


def track_x_expr(points, start, crop_w):
    """
    Выражение ffmpeg для левого края кропа, плавно идущего через точки.

    t в фильтре отсчитывается от начала клипа. Между точками — сглаженный
    переход (smoothstep), до первой и после последней кадр стоит на месте.
    Выход за границы кадра crop ограничивает сам.
    """
    pts = [(sec - start, center - crop_w / 2) for sec, center in points]
    terms = [f"lt(t,{pts[0][0]:.3f})*{pts[0][1]:.1f}"]
    for (t0, x0), (t1, x1) in zip(pts, pts[1:]):
        p = f"((t-{t0:.3f})/{t1 - t0:.3f})"
        terms.append(f"gte(t,{t0:.3f})*lt(t,{t1:.3f})*"
                     f"({x0:.1f}+({x1 - x0:.1f})*{p}*{p}*(3-2*{p}))")
    terms.append(f"gte(t,{pts[-1][0]:.3f})*{pts[-1][1]:.1f}")
    return "+".join(terms)


def cmd_cut(args):
    print("[4/4] CUT")
    if args.start is None and not getattr(args, "segments", None):
        raise SystemExit("нужен --start или --segments")
    # До создания папки партии: иначе в Clips Ready осталась бы пустая папка.
    if not Path(args.src).is_file():
        raise SystemExit(f"нет файла: {args.src}")
    d = batch_dir(args.src, getattr(args, "batch", None), getattr(args, "kind", None))
    # Все клипы стрима лежат прямо в его папке, по одному файлу на клип.
    name = (args.out or f"момент {int(args.start)}")
    # Только имя файла: ролик не должен уйти за пределы папки партии.
    if Path(name).name != name or name in (".", ".."):
        raise SystemExit("--out: нужно имя файла, а не путь")
    if name.endswith(".mp4"):
        name = name[:-4]
    out = d / f"{name}.mp4"
    zoom = None
    if args.zoom:
        zoom = [float(x) for x in args.zoom.split(":")]
    segments = None
    if getattr(args, "segments", None):
        segments = []
        for part in args.segments.split(","):
            a_, b_ = part.split("-")
            segments.append((float(a_), float(b_)))
        segments.sort()
        args.start = segments[0][0]
        args.dur = segments[-1][1] - args.start
    camcrop = None
    if getattr(args, "camcrop", None):
        camcrop = tuple(int(x) for x in args.camcrop.split(":"))
        if len(camcrop) != 4:
            raise SystemExit("--camcrop хочет четыре числа: Ш:В:X:Y")
    gamecrop = None
    if getattr(args, "gamecrop", None):
        if args.style not in ("talk", "wide", "meme"):
            raise SystemExit("--gamecrop работает только со стилями talk, wide и meme")
        gamecrop = tuple(int(x) for x in args.gamecrop.split(":"))
        if len(gamecrop) != 4:
            raise SystemExit("--gamecrop хочет четыре числа: Ш:В:X:Y")
        if gamecrop[0] <= 0 or gamecrop[1] <= 0 or gamecrop[2] < 0 or gamecrop[3] < 0:
            raise SystemExit("--gamecrop: ширина и высота больше нуля, X и Y не меньше нуля")
        game_out_h = {"talk": C.TALK_GAME_OUT_H, "wide": C.WIDE_GAME_OUT_H,
                      "meme": C.MEME_GAME_OUT_H}[args.style]
        free = args.style == "meme" and (args.camscale is not None or args.campos != "top"
                                         or C.MEME_CAMSCALE is not None)
        if not free and abs(gamecrop[0] / gamecrop[1] - C.OUT_W / game_out_h) > 0.02:
            print("    ВНИМАНИЕ: пропорция --gamecrop не совпадает с блоком игры, картинку растянет")
    gametrack = None
    if getattr(args, "gametrack", None):
        if args.style != "wide":
            raise SystemExit("--gametrack работает только со стилем wide")
        if segments or not args.notighten or zoom:
            # Время точек отсчитывается по непрерывному куску стрима.
            raise SystemExit("--gametrack требует --notighten и не сочетается "
                             "с --segments и --zoom")
        gametrack = parse_gametrack(args.gametrack)
    if (args.camscale is not None or args.campos != "top") and args.style != "meme":
        raise SystemExit("--camscale и --campos работают только со стилем meme")
    if args.camscale is not None and args.camscale <= 0:
        raise SystemExit("--camscale: число больше нуля, 1.0 — пиксель в пиксель")
    fill = None
    if args.fill is not None or args.fillsrc:
        if args.style != "meme":
            raise SystemExit("--fill и --fillsrc работают только со стилем meme")
        if args.fill is None or args.fill < 0:
            raise SystemExit("--fill: секунда начала боя, не меньше нуля")
        fill_src = Path(args.fillsrc) if args.fillsrc else Path(args.src)
        if not fill_src.is_file():
            raise SystemExit(f"--fillsrc: нет файла {fill_src}")
        fill = (fill_src, args.fill)
    elif args.style == "meme":
        raise SystemExit("стилю meme нужен бой снизу: --fill СЕК")
    render_clip(args.src, args.start, args.dur, out,
                title=args.title, subs=not args.nosubs,
                wordsubs=args.wordsubs,
                tighten=not args.notighten, zoom=zoom, style=args.style,
                caps=args.caps, camcrop=camcrop, gamecrop=gamecrop,
                gametrack=gametrack, nick=getattr(args, "nick", None),
                nocensor=getattr(args, "nocensor", False),
                blurbg=getattr(args, "blurbg", False),
                noshift=getattr(args, "noshift", False), segments=segments,
                xfade=float(getattr(args, "xfade", 0) or 0),
                words_file=getattr(args, "words", None), fill=fill,
                camscale=args.camscale, campos=args.campos)
    maybe_outro(out, getattr(args, "nick", None), getattr(args, "nooutro", False))


# ────────────────────────────── AUTO ──────────────────────────────────────

def cmd_auto(args):
    cmd_scan(args)
    cmd_asr(args)
    cmd_pick(args)
    print("\n[+] AUTO — рендерю топ по эвристике")
    d = batch_dir(args.src)
    cands = jload(stream_cache(args.src) / "candidates.json")[: args.n]
    stub_av()
    from faster_whisper import WhisperModel
    model = WhisperModel(C.ASR_MODEL_FINAL, device="cpu",
                         compute_type="int8", cpu_threads=C.ASR_THREADS)
    for i, c in enumerate(cands, 1):
        start = max(0, c["t"] - C.PRE_ROLL)
        dur = C.PRE_ROLL + C.POST_ROLL + 12
        print(f"\n  клип {i}/{len(cands)} — {c['time']}")
        nm = f"момент {c['time'].replace(':', '-')}"
        render_clip(args.src, start, dur, d / f"{nm}.mp4",
                    title=None, subs=True, model=model)
        maybe_outro(d / f"{nm}.mp4")
    print(f"\nГотово. Клипы в {d}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("batchname",
                       help="имя новой партии для --batch: «<Тип> <дата> <время> <название>»")
    p.add_argument("src", nargs="?", default=None,
                   help="исходник: название берётся из имени файла и длительности")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--name", default=None,
                   help="название вместо имени исходника, напр. «сырые клипы»")

    for name in ("scan", "asr", "pick", "cut", "auto", "check"):
        p = sub.add_parser(name)
        if name == "pick":
            p.add_argument("src", nargs="?", default=None,
                           help="стрим .mp4 (нужен, чтобы померить движение в кадре)")
        else:
            p.add_argument("src", help="путь к стриму .mp4")
        if name == "asr":
            p.add_argument("--force", action="store_true")
        if name == "cut":
            p.add_argument("--start", type=float, default=None)
            p.add_argument("--xfade", type=float, default=0.0, metavar="СЕК",
                           help="плавный переход между кусками, напр. 0.4")
            p.add_argument("--segments", default=None, metavar="A-B,C-D",
                           help="собрать клип из нескольких кусков стрима, "
                                "абсолютные секунды; отменяет --start/--dur")
            p.add_argument("--dur", type=float, default=30)
            p.add_argument("--title", default=None,
                           help="заголовок, висит весь клип")
            p.add_argument("--out", default=None)
            p.add_argument("--batch", default=None, metavar="ИМЯ",
                           help="имя папки партии в Clips Ready (из clipper.py batchname)")
            p.add_argument("--kind", default=None, choices=KINDS,
                           help="тип партии в имени папки, если --batch не задан")
            p.add_argument("--nosubs", action="store_true")
            p.add_argument("--wordsubs", action="store_true",
                           help="игровой стиль: пословные субтитры внизу "
                                "вместе с заголовком — для разговорных моментов")
            p.add_argument("--noshift", action="store_true",
                           help="не двигать вход на первый громкий звук — "
                                "когда важнее опорный кадр")
            p.add_argument("--gamecrop", default=None, metavar="Ш:В:X:Y",
                           help="кусок игры для стилей wide, talk и meme; пропорция "
                                "блока: wide 1080:1380, talk 1080:576, meme 1080:960 "
                                "(для meme — кусок кадра боя; с --camscale/--campos — "
                                "область, из которой кусок нужной пропорции берётся по центру)")
            p.add_argument("--fill", type=float, default=None, metavar="СЕК",
                           help="стиль meme: с какой секунды брать бой для нижней "
                                "половины кадра; бой идёт без звука")
            p.add_argument("--camscale", type=float, default=None, metavar="К",
                           help="стиль meme, проба: вебка в исходных пропорциях, "
                                "растянута в К раз (1.0 — пиксель в пиксель)")
            p.add_argument("--campos", choices=["top", "center"], default="top",
                           help="стиль meme, проба: вебка сверху или по центру кадра")
            p.add_argument("--fillsrc", default=None, metavar="ФАЙЛ",
                           help="стиль meme: файл, откуда брать бой; "
                                "по умолчанию тот же стрим")
            p.add_argument("--nooutro", action="store_true",
                           help="не добавлять заставку в конце ролика")
            p.add_argument("--nick", default=None, metavar="НИК",
                           help="ник на плашке, если стрим другого автора; "
                                "пустая строка убирает плашку; чужой или пустой "
                                "ник отключает и заставку")
            p.add_argument("--gametrack", default=None, metavar="СЕК:X,СЕК:X,...",
                           help="стиль wide: кадр игры едет по точкам. СЕК — секунда "
                                "стрима, X — центр кадра в пикселях исходника; "
                                "точки сортируются по времени; при --gamecrop из него "
                                "берутся только Ш, В и Y; требует --notighten, "
                                "несовместимо с --segments и --zoom")
            p.add_argument("--camcrop", default=None, metavar="Ш:В:X:Y",
                           help="откуда брать вебку, если она развёрнута "
                                "на весь экран (пропорции как у окна стиля)")
            p.add_argument("--words", default=None, metavar="ФАЙЛ.json",
                           help="проверенные руками слова для пословных "
                                "субтитров вместо распознавания клипа")
            p.add_argument("--notighten", action="store_true",
                           help="не вырезать пустоты")
            p.add_argument("--zoom", default=None, metavar="СЕК:КРАТ",
                           help="наезд на геймплей, напр. 4.5:1.35")
            p.add_argument("--caps", dest="caps", action="store_const",
                           const=True, default=None,
                           help="принудительно красный КАПСОМ")
            p.add_argument("--lower", dest="caps", action="store_const",
                           const=False,
                           help="принудительно жёлтый строчными; "
                                "без обоих флагов цвет выбирается по яркости фона")
            p.add_argument("--blurbg", action="store_true",
                           help="размытый фон вместо чёрных полей (стиль cam)")
            p.add_argument("--nocensor", action="store_true",
                           help="не закрывать мат звёздочками в экранном "
                                "тексте (по умолчанию закрывается во всех стилях)")
            p.add_argument("--style", choices=["game", "life", "cam", "duo", "talk", "wide", "meme"],
                           default="game",
                           help="game = вебка+геймплей (аккаунт 1); "
                                "life = вебка крупно, бытовое (аккаунт 2); "
                                "cam = только вебка 4:3 по центру, жёлтый "
                                "однострочный заголовок (эталон 10); "
                                "duo = вебка крупно сверху (отзеркалена) + "
                                "игра снизу, заголовок капсом на шве — формат "
                                "чужих нарезок конфликта с бабой; "
                                "meme = разговорный мем: вебка сверху, снизу "
                                "без звука бой из --fill")
        if name == "auto":
            p.add_argument("-n", type=int, default=C.N_CLIPS)

    args = ap.parse_args()
    if args.cmd in ("pick", "cut", "auto"):
        apply_layout(args.src)
    {"scan": cmd_scan, "asr": cmd_asr, "pick": cmd_pick,
     "cut": cmd_cut, "auto": cmd_auto, "check": cmd_check,
     "batchname": cmd_batchname}[args.cmd](args)


if __name__ == "__main__":
    main()
