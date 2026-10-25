"""Растровые билеты — картинка без текстового слоя (kassa24.ru и подобные).

Зачем отдельный модуль. Часть билетов приходит не текстом, а рендером страницы:
на странице одна картинка на весь лист, шрифтов и текстового слоя нет. Ни панель
строк, ни правки на них не работают — редактор просто «не видит» билет. Здесь это
лечится тремя шагами:

1. **OCR** системным Vision (`vendor/biletocr`, сборка — `tools/biletocr.m`)
   даёт строки и слова с координатами. Результат кэшируется по sha картинки, так
   что повторное открытие билета распознавание не повторяет.

2. **Атлас глифов из пикселей самого билета.** Новое значение собирается из ТЕХ
   ЖЕ букв и цифр, что уже нарисованы на билете: нарезка слова на символы идёт по
   метрикам шрифта-побратима (PFDinTextPro-Light, тот же рисунок) с прищёлкиванием
   границ к провалам чернил, кегль берётся у самой строки (высота её глифов),
   выравнивание — по базовой линии. Поэтому шрифт, кегль и сглаживание совпадают
   по определению: подбирать шрифт не нужно. Того символа, которого на билете нет,
   в атласе тоже нет — для него рисуем запасным шрифтом с подгонкой веса.

3. **Правка** = стереть полосу старой строки фоном и вклеить собранное значение,
   после чего итоговая картинка заменяет изображение страницы (`replace_image`).
   Правки применяются к ЧИСТОЙ картинке заново на каждый запрос (клиент присылает
   весь список правок), поэтому повторное применение идемпотентно.
"""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading

import fitz
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
OCR_BIN = os.path.join(ROOT, "vendor", "biletocr")
FALLBACK_TTF = os.path.join(ROOT, "vendor", "PFDinTextPro-Light.ttf")
# системные шрифты на случай символов, которых нет у шрифта-побратима
# Кандидаты по убыванию предпочтения (macOS, Linux, Windows): берётся первый
# существующий, в котором есть нужный глиф.
ALT_TTF = (
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arialuni.ttf",
    "C:/Windows/Fonts/arial.ttf",
)
OCR_DIR = os.path.join(os.path.expanduser("~"), ".biletnaya", "ocr")

BG = 255.0          # фон билета: белый лист, текст поверх
INK_THR = 246.0     # темнее этого — чернила (порог по яркости)
CAP_RATIO = 0.72    # высота прописной к высоте кегля у этого рисунка шрифта
HALO = 1.0          # на сколько сглаживание раздувает краску глифа по вертикали
MIN_COVER = 0.9     # картинка «на всю страницу»: доля площади листа
FIT_MIN = 0.35      # предел горизонтального сжатия значения, как в _draw_native
FIT_TOL = 0.97      # сжатие меньше 3% на глаз незаметно — не шумим
SHAPE_MIN = 0.62    # ниже этого глиф из атласа — чужая буква, рисуем шрифтом
SHAPE_THR = 0.35    # порог «это краска» для сравнения масок глифов (IoU)
ASPECT_LO = 0.62    # доля от пропорции нужной буквы: уже — обрубок нарезки
ASPECT_HI = 1.65    # шире — склейка двух букв в один кластер краски
RESCALE_MAX = 0.25  # дальше этого глиф атласа не растягиваем: рисовать мыло
                    # хуже, чем взять шрифт-побратим ровно нужного кегля

_STATE = {}                       # doc_id -> {pno: state}
_LOCK = threading.RLock()
_FB = {}                          # кэш глифов запасного шрифта (символ, кегль)
_FB_GAIN = {}                     # кегль -> во сколько догонять краску запасного
_FB_GAIN_DOC = None               # для какого билета эти множители посчитаны
_RAW = {}                         # кэш отрисовок шрифта-побратима (символ, кегль)
_RATIO = {}                       # символ -> высота краски в долях кегля
_AR = {}                          # символ -> ширина краски в долях её высоты
_SHAPE = {}                       # символ -> эталонная маска 24x24
_FNT = None                       # сам шрифт-побратим (ленивый)
_ALT = {}                         # символ -> (путь, шрифт) системного запасного

# Vision читает метки-«капитель» (small caps) то кириллицей, то латиницей:
# «МЕСТО» → «MECTO», «ДАТА» → «дАтА». Латиницу в кириллицу переводим только
# тогда, когда кириллическое прочтение — известное билетное слово: иначе
# «kassa24.ru» превратилось бы в «касса24.ru».
_LAT2CYR = str.maketrans({
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    # «3» и «З» Vision путает в «ЗАЛ», «ЗАКАЗА», «ЗОНА». Цифру это не портит:
    # перевод идёт в слово целиком и применяется только к известным билетным
    # словам, а «12З» в них не значится.
    "3": "З",
})
_CANON = {
    "ряд": "РЯД", "места": "МЕСТА", "место": "МЕСТО", "цена": "ЦЕНА",
    "дата": "ДАТА", "начало": "НАЧАЛО", "адрес": "АДРЕС", "зал": "ЗАЛ",
    "сектор": "СЕКТОР", "сервис": "СЕРВИС", "организатор": "ОРГАНИЗАТОР",
    "покупатель": "ПОКУПАТЕЛЬ", "номер": "НОМЕР", "заказа": "ЗАКАЗА",
    "линия": "ЛИНИЯ", "сгиба": "СГИБА", "правила": "ПРАВИЛА", "билет": "БИЛЕТ",
    "электронный": "ЭЛЕКТРОННЫЙ", "театр": "ТЕАТР", "инн": "ИНН",
    "стоимость": "СТОИМОСТЬ", "зритель": "ЗРИТЕЛЬ", "зона": "ЗОНА",
    "ярус": "ЯРУС", "время": "ВРЕМЯ", "вход": "ВХОД", "скидка": "СКИДКА",
}


# ─────────────────────────── распознавание ───────────────────────────

def _vision_ready():
    """Собран ли помощник распознавания И запустится ли он здесь.

    Помощник собран из tools/biletocr.m под macOS Vision (Mach-O), поэтому на
    другой ОС файл может лежать (и даже иметь +x — git его таким и хранит), но
    не запустится. Без проверки платформы «готов» означало бы «файл на месте»:
    растровый билет ушёл бы в raster-путь, _run_ocr поймал «Exec format error»,
    и панель осталась бы пустой без объяснения."""
    return (sys.platform == "darwin" and os.path.isfile(OCR_BIN)
            and os.access(OCR_BIN, os.X_OK))


_PY_OCR = False           # False — ещё не пробовали; None — нет; иначе движок


def _py_ocr():
    """Python-распознавание (rapidocr) — замена macOS Vision на других ОС.

    Без него растровый билет вне macOS не редактировался ВОВСЕ: ocr_ready()
    отвечал «нет», страница уходила в обычный путь, текста на ней не
    находилось, и панель оставалась пустой. Движок поднимаем лениво и один раз
    (инициализация недешёвая), результат запоминаем — ocr_ready() спрашивают на
    каждом открытии файла.

    BILETNAYA_OCR=0 отключает запасной путь целиком (останется только Vision);
    BILETNAYA_OCR_LANG задаёт язык распознавания (по умолчанию кириллица).
    Кириллическая модель подтягивается при первом запуске; если взять её негде,
    остаёмся на базовой — тогда цифры и латиница читаются, а метки могут выйти
    искажёнными, но править значения это не мешает."""
    global _PY_OCR
    if _PY_OCR is not False:
        return _PY_OCR
    if os.environ.get("BILETNAYA_OCR", "1").strip().lower() in ("0", "off", "no", "false"):
        _PY_OCR = None
        return None
    try:
        from rapidocr import RapidOCR
    except Exception:                       # не установлен — только Vision
        _PY_OCR = None
        return None
    lang = os.environ.get("BILETNAYA_OCR_LANG", "cyrillic").strip() or "cyrillic"
    tries = []
    try:                                    # кириллица: отдельная модель v5
        from rapidocr.utils.typings import ModelType, OCRVersion
        tries.append({"Global.return_word_box": True, "Rec.lang_type": lang,
                      "Rec.model_type": ModelType.MOBILE,
                      "Rec.ocr_version": OCRVersion.PPOCRV5})
    except Exception:
        pass
    tries.append({"Global.return_word_box": True, "Rec.lang_type": lang})
    tries.append({"Global.return_word_box": True})
    for params in tries:
        try:
            _PY_OCR = RapidOCR(params=params)
            return _PY_OCR
        except Exception:                   # модель не скачалась/не та версия
            continue
    _PY_OCR = None
    return None


def ocr_ready():
    """Есть ли чем распознать растровый билет: macOS Vision или rapidocr."""
    return _vision_ready() or _py_ocr() is not None


def _lev1(a, b):
    """Расстояние Левенштейна, но только «0, 1 или больше». Строки короткие."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 1:
        return 2
    if len(a) == len(b):
        return 1 if sum(x != y for x, y in zip(a, b)) == 1 else 2
    if len(a) > len(b):
        a, b = b, a
    i = j = 0                      # b длиннее a ровно на один символ
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
        elif j - i >= 1:           # второй лишний символ — уже не опечатка
            return 2
        j += 1
    return 1


def _one_drop(short, full):
    """`short` получается из `full` ВЫБРОСОМ ровно одной буквы (не заменой).

    Именно так Vision портит мелкую капитель: «МЕСТО» → «МЕТО». Подмену сюда не
    пускаем сознательно — «ЦЕНЫ» тоже в одной правке от «ЦЕНА», но это другое
    слово, настоящее значение билета."""
    if len(full) != len(short) + 1:
        return False
    i = 0
    while i < len(short) and short[i] == full[i]:
        i += 1
    return short[i:] == full[i + 1:]


def fix_text(text, nruns=None):
    """Омоглифы Vision → кириллица для известных билетных слов + канон капители.

    `nruns` — сколько кластеров краски в этом слове на картинке. С ним чинится
    кириллическая метка с ПРОПАВШЕЙ буквой («МЕТО» ← «МЕСТО»): переписываем
    только когда краски ровно столько, сколько букв в каноне, то есть потерю
    подтверждают пиксели билета. Без подтверждения нельзя: «ПРАВИЛ ПОСЕЩЕНИЯ»
    тоже в одной правке от «ПРАВИЛА», но это настоящее слово, а не метка."""
    out = []
    for w in re.split(r"(\W+)", text or ""):
        if not w:
            out.append(w)
            continue
        # Сравнивать надо РЕЗУЛЬТАТ перевода: `_LAT2CYR` — таблица с числовыми
        # ключами, и `ch in _LAT2CYR` по строке всегда False, из-за чего «MECTO»
        # так и оставалось латиницей.
        cand = w.translate(_LAT2CYR)
        if cand != w:
            low = cand.lower()
            if low not in _CANON:
                # На мелкой капители Vision теряет букву: «МЕСТО» → «METO»
                # (латиница осталась, а кириллического слова не вышло). Догоняем
                # по ОДНОЙ опечатке — и только там, где буква была латинской.
                # Настоящее кириллическое значение так переписывать нельзя:
                # «ЦЕНЫ» тоже в одной правке от «ЦЕНА».
                low = next((k for k in _CANON
                            if not any(c.isdigit() for c in low) and _lev1(low, k) <= 1), "")
            if low in _CANON:
                w = _CANON[low]
        elif w.lower() in _CANON:
            w = _CANON[w.lower()]
        elif nruns:
            low = w.lower()
            if w.isupper() and low.isalpha() and len(low) >= 3:
                cands = [k for k in _CANON if _one_drop(low, k)]
                if len(cands) == 1 and nruns == len(cands[0]):
                    w = _CANON[cands[0]]
        out.append(w)
    return "".join(out)


def _big_image(page):
    """Единственная картинка на весь лист — признак растрового билета."""
    infos = page.get_image_info(xrefs=True)
    if len(infos) != 1:
        return None
    r = fitz.Rect(infos[0]["bbox"])
    if abs(r.get_area()) < MIN_COVER * abs(page.rect.get_area()):
        return None
    return infos[0]


def is_raster_page(page):
    """Страница без текстового слоя, целиком занятая одной картинкой."""
    if not ocr_ready():
        return False
    try:
        if page.get_text("text").strip():
            return False
    except Exception:
        return False
    return _big_image(page) is not None


def _split_words(box, text):
    """Регион распознавания → слова с боксами.

    rapidocr отдаёт бокс на регион целиком, а в регионе бывает несколько слов
    («2500 py.»). Контракт Vision — бокс символа равен боксу слова, поэтому
    режем регион между словами пропорционально числу знаков: дальше raster сам
    уточняет границы букв по провалам чернил (_cut), так что приближённая ширина
    слова ему не мешает."""
    parts = [p for p in str(text).split() if p]
    if len(parts) <= 1:
        return [(str(text).strip(), [float(v) for v in box])] if str(text).strip() else []
    x0, y0, x1, y1 = [float(v) for v in box]
    total = sum(len(p) for p in parts)
    span = (x1 - x0) / max(total, 1)
    out, cx = [], x0
    for p in parts:
        w = span * len(p)
        out.append((p, [cx, y0, cx + w, y1]))
        cx += w
    return out


def _group_lines(words):
    """Слова распознавания → визуальные строки.

    rapidocr режет билет по регионам: метка «РЯД» и её значение «3» приходят
    отдельными регионами, а панель и теги полей ждут их ОДНОЙ строкой — как
    отдаёт Vision. Склеиваем регионы, которые пересекаются по вертикали хотя бы
    на половину меньшего из них."""
    groups = []
    for wtext, score, box in sorted(words, key=lambda w: (w[2][1] + w[2][3]) / 2.0):
        h = max(box[3] - box[1], 1.0)
        for g in groups:
            ov = min(box[3], g["y1"]) - max(box[1], g["y0"])
            if ov >= 0.5 * min(h, max(g["y1"] - g["y0"], 1.0)):
                g["words"].append((wtext, score, box))
                g["y0"] = min(g["y0"], box[1])
                g["y1"] = max(g["y1"], box[3])
                break
        else:
            groups.append({"y0": box[1], "y1": box[3], "words": [(wtext, score, box)]})
    out = []
    for g in groups:
        ws = sorted(g["words"], key=lambda w: w[2][0])
        text, chars, boxes, conf = [], [], [], []
        for i, (wtext, score, box) in enumerate(ws):
            text.append(wtext)
            chars += [list(box) for _ in wtext]
            boxes.append(box)
            conf.append(score)
            if i + 1 < len(ws):
                text.append(" ")
                chars.append([])                # пробел = граница слов
        out.append({"text": "".join(text),
                    "conf": round(sum(conf) / max(len(conf), 1), 4),
                    "chars": chars,
                    "bbox": [round(min(b[0] for b in boxes), 1),
                             round(min(b[1] for b in boxes), 1),
                             round(max(b[2] for b in boxes), 1),
                             round(max(b[3] for b in boxes), 1)]})
    out.sort(key=lambda l: (l["bbox"][1], l["bbox"][0]))
    return out


def _ocr_rapid(png_path):
    """Распознавание через rapidocr в том же JSON, что отдаёт Vision."""
    eng = _py_ocr()
    if eng is None:
        raise RuntimeError("нет распознавания: ни macOS Vision, ни rapidocr")
    from PIL import Image
    with Image.open(png_path) as im:
        W, H = im.size
    res = eng(png_path)
    words = []
    for block in (getattr(res, "word_results", None) or ()):
        for item in (block or ()):
            try:
                wtext, score, quad = item
            except Exception:
                continue
            xs = [float(p[0]) for p in quad]
            ys = [float(p[1]) for p in quad]
            box = [min(xs), min(ys), max(xs), max(ys)]
            for w, b in _split_words(box, wtext):
                words.append((w, float(score), b))
    if not words:                            # слов не дали — берём регионы целиком
        for t, s, q in zip(res.txts or (), res.scores or (), res.boxes or ()):
            pts = np.asarray(q, float).reshape(-1, 2)
            box = [float(pts[:, 0].min()), float(pts[:, 1].min()),
                   float(pts[:, 0].max()), float(pts[:, 1].max())]
            for w, b in _split_words(box, t):
                words.append((w, float(s), b))
    return {"w": int(W), "h": int(H), "lines": _group_lines(words)}


def _run_ocr(png_path):
    if not _vision_ready():
        return _ocr_rapid(png_path)
    res = subprocess.run([OCR_BIN, png_path, "ru-RU,en-US", "1"],
                         capture_output=True, timeout=180)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError((res.stderr or b"").decode("utf-8", "replace")[:200]
                           or "распознавание не удалось")
    return json.loads(res.stdout.decode("utf-8"))


def _ocr_cached(rgb, info):
    """Распознавание с дисковым кэшем по sha картинки (повторное открытие — мгновенно)."""
    key = hashlib.sha256(bytes(info.get("digest") or b"") +
                         b"|%dx%d" % (info.get("width") or 0, info.get("height") or 0)
                         ).hexdigest()[:16]
    path = os.path.join(OCR_DIR, key + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    from PIL import Image
    os.makedirs(OCR_DIR, exist_ok=True)
    tmp = os.path.join(OCR_DIR, key + ".png")
    Image.fromarray(rgb).save(tmp, format="PNG")
    try:
        data = _run_ocr(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass
    return data


# ─────────────────────────── атлас глифов ───────────────────────────

def _word_boxes(line):
    """Vision отдаёт бокс символа = бокс всего слова → собираем слова обратно."""
    out, cur, curbox = [], "", None
    for ch, cb in zip(line.get("text", ""), line.get("chars") or []):
        if not cb:
            if cur:
                out.append((cur, curbox))
                cur, curbox = "", None
            continue
        cb = [round(float(v), 1) for v in cb]
        if curbox is None:
            curbox = list(cb)
        elif cb != curbox:
            out.append((cur, curbox))
            cur, curbox = ch, list(cb)
            continue
        cur += ch
    if cur:
        out.append((cur, curbox))
    return out


def _cut(text, col, fnt, sizes=None):
    """Границы символов: метрики шрифта-побратима → прищёлкнуть к провалу чернил.

    Только метрики дают «на глазок» (у билета начертание тяжелее, авансы шире),
    только профиль чернил — рвёт слипшиеся буквы и путает число кусков. Вместе
    получается ровно len(text) границ, каждая — в самом светлом столбце зазора.
    `sizes` — кегль каждого символа в px: в одном «слове» OCR нередко живут метка
    мелкой капителью и её значение крупно, и одним кеглем их не разложить.
    """
    n = len(text)
    if n <= 1:
        return [(0, len(col))]
    cum = [0.0]
    for i, ch in enumerate(text):
        sz = sizes[i] if sizes else 1.0
        cum.append(cum[-1] + max(fnt.text_length(ch, sz), 0.15))
    total, W = cum[-1], len(col)
    bounds = [0]
    for i in range(1, n):
        ideal = cum[i] / total * W
        half = 0.4 * W / n
        lo = max(bounds[-1] + 1, int(ideal - half))
        hi = min(W - 1, int(ideal + half))
        b = int(round(ideal)) if hi <= lo else lo + int(np.argmin(col[lo:hi]))
        bounds.append(max(b, bounds[-1] + 1))
    bounds.append(W)
    return list(zip(bounds[:-1], bounds[1:]))


def _thr(sub):
    """Порог «чернила/фон» — середина между фоном и самой тёмной точкой слова.

    Постоянный порог не годится ни в одну сторону. Высокий (INK_THR) считает
    чернилами ещё и ореол сглаживания, и зазоры между буквами схлопываются:
    «МЕСТО» превращалось в ОДИН кусок краски, «Электронный» — в 12 кусков на
    11 букв. Низкий рвёт светлую мелкую капитель. Середина по самому тёмному
    пикселю слова даёт ровно по куску на букву (проверено: 13/13 на
    «ЗАКАЗА2041217», 12/12, 8/8, 6/6) и не зависит от того, светлее набрана
    метка или темнее значение.
    """
    lo = float(sub.min()) if sub.size else BG
    if lo >= INK_THR:                 # чернил в полосе нет вовсе
        return INK_THR
    return (BG + lo) / 2.0


def _ink_runs(sub, thr=None):
    """Кластеры краски по столбцам (границы в координатах переданной полосы)."""
    if thr is None:
        thr = _thr(sub)
    col = sub.min(axis=0) < thr
    runs, s = [], None
    for i, v in enumerate(col):
        if v and s is None:
            s = i
        elif not v and s is not None:
            runs.append([s, i])
            s = None
    if s is not None:
        runs.append([s, len(col)])
    return runs


def _ink_rows(sub, thr=None):
    if thr is None:
        thr = _thr(sub)
    rows = np.nonzero(sub.min(axis=1) < thr)[0]
    return (int(rows[0]), int(rows[-1]) + 1) if len(rows) else (0, 0)


def _groups(wtext, sub, fnt):
    """Разложить символы «слова» OCR по кластерам краски — с их кеглями.

    Vision нередко сливает в одно слово метку мелкой капителью и её значение
    крупно («ЗАКАЗА2041217»), причём БЕЗ пробела между ними. Резать такое одним
    кеглем нельзя: границы символов уезжают, и в атлас попадает чужая буква
    (проверено: «2» из «2041217» уезжала в «1»). Поэтому символы раскладываем по
    реальным кластерам краски: цена раскладки — расхождение метрической ширины
    части с шириной её кластера при кегле этого же кластера.
    """
    n = len(wtext)
    thr = _thr(sub)
    runs = _ink_runs(sub, thr)
    if not runs or n < 1:
        return []
    while len(runs) > n and len(runs) > 1:       # «й», «ё», «%» дают лишний кластер
        gaps = [runs[i + 1][0] - runs[i][1] for i in range(len(runs) - 1)]
        i = int(np.argmin(gaps))
        runs[i] = [runs[i][0], runs[i + 1][1]]
        del runs[i + 1]
    k = len(runs)
    if k < 1:
        return []
    info = []
    for a, b in runs:
        r0, r1 = _ink_rows(sub[:, a:b], thr)
        info.append({"a": a, "b": b, "w": b - a, "h": max(r1 - r0, 1)})
    INF = 1e18
    dp = [[INF] * (k + 1) for _ in range(n + 1)]
    back = [[0] * (k + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for j in range(1, k + 1):
        inf = info[j - 1]
        size = max(inf["h"] / CAP_RATIO, 3.0)
        for i in range(1, n + 1):
            for p in range(j - 1, i):
                if dp[p][j - 1] >= INF:
                    continue
                want = fnt.text_length(wtext[p:i], size)
                cost = dp[p][j - 1] + abs(want - inf["w"]) / max(inf["w"], 1.0)
                if cost < dp[i][j]:
                    dp[i][j] = cost
                    back[i][j] = p
    if dp[n][k] >= INF or dp[n][k] > 0.9 * k:    # раскладка не сходится — не рискуем
        return []
    parts, i, j = [], n, k
    while j > 0:
        p = back[i][j]
        parts.append((wtext[p:i], info[j - 1]))
        i, j = p, j - 1
    parts.reverse()
    return parts


def _build_atlas(rgb, ocr):
    """Нарезка всех слов билета на символы → атлас «(символ, высота) → глифы»."""
    lum = rgb.astype(np.float32).mean(axis=2)
    fnt = fitz.Font(fontfile=FALLBACK_TTF) if os.path.isfile(FALLBACK_TTF) else None
    atlas, words = {}, []
    if fnt is None:
        return atlas, words
    for li, ln in enumerate(ocr.get("lines") or []):
        for wtext, wbox in _word_boxes(ln):
            if not wtext.strip():
                continue
            x0, y0, x1, y1 = [int(round(v)) for v in wbox]
            x0, y0 = max(0, x0), max(0, y0)
            sub = lum[y0:y1, x0:x1]
            if sub.size == 0:
                continue
            parts = _groups(wtext, sub, fnt)
            if not parts:
                # раскладка по кластерам не сошлась (символов и кусков краски
                # не свести) — берём слово целиком: режем только по метрикам
                r0, r1 = _ink_rows(sub)
                parts = [(wtext, {"a": 0, "b": sub.shape[1], "w": sub.shape[1],
                                  "h": max(r1 - r0, 1)})]
            tiles = []
            for ptext, pinf in parts:
                pa = int(pinf["a"])
                band = sub[:, pa:int(pinf["b"])]
                col = np.clip((BG - band) / (BG - 40.0), 0, 1).sum(axis=0)
                on = np.nonzero(col > 0.2)[0]
                if len(on) == 0:
                    continue
                a0, a1 = int(on[0]), int(on[-1]) + 1
                for ch, (a, b) in zip(ptext, _cut(ptext, col[a0:a1], fnt)):
                    t = band[:, a0 + a:a0 + b]
                    if t.shape[1] == 0:
                        continue
                    rows = np.nonzero(t.min(axis=1) < INK_THR)[0]
                    if len(rows) == 0:
                        continue
                    r0, r1 = int(rows[0]), int(rows[-1]) + 1
                    # по ВЕРТИКАЛИ обрезаем по чернилам, по ГОРИЗОНТАЛИ — нет:
                    # границы реза уже приходятся на середину межбуквенного зазора,
                    # и если их срезать по краске, при сборке пропадут пробелы
                    # между буквами (слово выйдет уже оригинала)
                    t = t[r0:r1]
                    tiles.append({"c": ch, "lum": t, "top": y0 + r0, "bot": y0 + r1,
                                  "x": x0 + pa + a0 + a, "w": t.shape[1]})
            if not tiles:
                continue
            # слово панели = слово OCR целиком (куски краски — только способ
            # нарезки на буквы): иначе каждая буква стала бы отдельным «словом»
            # и строка собиралась бы через пробел — «Э л е к т р о н н ы й»
            bots = np.array([t["bot"] for t in tiles])
            vals, counts = np.unique(bots, return_counts=True)
            base = int(vals[np.argmax(counts)])          # мода низа = базовая линия
            size = _word_size([(t["c"], t["bot"] - t["top"]) for t in tiles])
            key = max(t["bot"] - t["top"] for t in tiles)
            wid = len(words)
            # Кластеров краски в слове: по ним fix_text отличает «распознавание
            # потеряло букву в метке» от настоящего слова той же длины.
            nruns = len(_ink_runs(sub))
            words.append({"text": wtext, "tiles": tiles, "key": key, "base": base,
                          "size": size, "line": li, "wid": wid, "nruns": nruns})
            for t in tiles:
                # краска глифа — по САМОМУ тёмному его пикселю, не по перцентилю:
                # у мелкой капители («ЦЕНА», «РЯД») полной краски может не быть
                # вовсе, и перцентиль делал перерисованную метку бледнее оригинала
                ink = float(t["lum"].min())
                a = np.clip((BG - t["lum"]) / max(BG - ink, 12.0), 0, 1).astype(np.float32)
                rgb_t = rgb[t["top"]:t["bot"], t["x"]:t["x"] + a.shape[1]]
                flat = rgb_t.reshape(-1, 3)
                aa = a.reshape(-1)
                dark = flat[aa > 0.9]
                if not len(dark):
                    dark = flat[aa >= aa.max() - 1e-3] if len(aa) else flat
                color = (dark.mean(axis=0) if len(dark) else np.array([40, 40, 40], np.float32))
                # ключ атласа — высота краски САМОГО глифа, а не всего слова: у «р»
                # с выносом и «а» без него она разная, и по ключу слова «а» потом
                # находилась бы чужая, более крупная буква
                atlas.setdefault((t["c"], t["bot"] - t["top"]), []).append(
                    {"g": a, "rgb": color.astype(np.float32), "dy": base - t["top"],
                     "dh": t["bot"] - base, "w": a.shape[1], "wid": wid, "size": size})
    return atlas, words


# ─────────────────────────── строки для панели ───────────────────────────

def _lines(words, z, page_w):
    """Строки панели: одна строка OCR = одна строка редактора (как у векторных
    билетов, где метка и значение тоже попадают в один сегмент)."""
    by_line = {}
    for w in words:
        by_line.setdefault(w["line"], []).append(w)
    out = []
    for li in sorted(by_line):
        ws = sorted(by_line[li], key=lambda w: min(t["x"] for t in w["tiles"]))
        tiles = [t for w in ws for t in w["tiles"]]
        if not tiles:
            continue
        x0 = min(t["x"] for t in tiles)
        x1 = max(t["x"] + t["w"] for t in tiles)
        y0 = min(t["top"] for t in tiles)
        y1 = max(t["bot"] for t in tiles)
        key = max(t["bot"] - t["top"] for t in tiles)
        base = int(np.median([t["bot"] for t in tiles]))
        size = float(np.median([w["size"] for w in ws if w["size"] > 0] or [0]))
        dark = np.concatenate([t["lum"].reshape(-1) for t in tiles])
        ink = float(np.percentile(dark, 3))
        out.append({
            "text": " ".join(fix_text(w["text"], w.get("nruns")) for w in ws),
            "bbox": [round(x0 / z, 1), round(y0 / z, 1), round(x1 / z, 1), round(y1 / z, 1)],
            "size": round(size / z, 1),
            "bold": False, "serif": False,
            "color": int(np.clip(ink, 0, 255)) * 0x010101,
            "origin": [round(x0 / z, 1), round(base / z, 1)],
            "font": "Растр", "raster": True,
            "px": [int(x0), int(y0), int(x1), int(y1)],
            "key": int(key), "base": int(base), "line": li,
            # слова строки со СВОИМ кеглем и своим левым краем: в билете метка
            # («ЦЕНА», «РЯД») набрана мелкой капителью, а значение — крупно, и
            # собирать новое значение одним размером нельзя
            "words": [{"text": w["text"],
                       "nruns": int(w.get("nruns") or 0),
                       "x0": int(min(t["x"] for t in w["tiles"])),
                       "x1": int(max(t["x"] + t["w"] for t in w["tiles"])),
                       "key": int(max(t["bot"] - t["top"] for t in w["tiles"])),
                       "size": float(w["size"] or size),
                       "tiles": w["tiles"]} for w in ws],
        })
    return out


def _raster_rgb(page, info):
    """Пиксели билета КАК ЕСТЬ — из самой картинки, без рендера страницы.

    Рендер страницы подсовывает пересэмплирование: лист 595 pt, картинка
    1191 px, и округление масштаба даёт 1190×1684 или 1191×1683 вместо родных
    1191×1684. Стоит потом подменить картинку — и страница начинает показываться
    чуть иначе, хотя править её никто не собирался (проверено: 2% листа
    «расходилось» даже на пустой правке). Поэтому берём пиксели напрямую, а
    рендер оставляем только на случай повёрнутой или хитро уложенной картинки.
    """
    w, h = int(info.get("width") or 0), int(info.get("height") or 0)
    if w > 0 and h > 0 and not page.rotation:
        try:
            from PIL import Image
            raw = page.parent.extract_image(info["xref"])
            im = Image.open(io.BytesIO(raw["image"]))
            if im.mode != "RGB":
                im = im.convert("RGB")
            arr = np.asarray(im, dtype=np.uint8)
            if arr.shape[0] == h and arr.shape[1] == w:
                return arr, w / max(page.rect.width, 1.0)
        except Exception:
            pass
    zx = w / max(page.rect.width, 1.0)
    zy = h / max(page.rect.height, 1.0)
    if zx <= 0 or zy <= 0:
        return None, 0.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zx, zy))
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3].copy()
    return arr, zx


def _build(page):
    info = _big_image(page)
    if info is None:
        return None
    rgb, z = _raster_rgb(page, info)
    if rgb is None or not z:
        return None
    ocr = _ocr_cached(rgb, info)
    atlas, words = _build_atlas(rgb, ocr)
    if not words:
        return None
    return {"rgb": rgb, "atlas": atlas, "words": words, "z": z,
            "xref": info.get("xref"), "lines": _lines(words, z, page.rect.width)}


def state(doc_id, page):
    """Состояние страницы (кэш на процесс). None — если это не растровый билет."""
    if not is_raster_page(page):
        return None
    with _LOCK:
        per = _STATE.setdefault(doc_id, {})
        pno = page.number
        if pno not in per:
            per[pno] = _build(page)
        return per[pno]


def drop(doc_id):
    with _LOCK:
        _STATE.pop(doc_id, None)


def lines(doc_id, page):
    """Строки панели для растрового билета или None (обычный путь редактора)."""
    try:
        st = state(doc_id, page)
    except Exception:
        return None
    return None if st is None else st["lines"]


# Поля строки, которые нужны панели. Всё остальное (слова, плитки, карта краски
# глифов) остаётся на сервере: numpy-массив в JSON не сериализуется, а тащить в
# браузер пиксели глифов незачем — сборка значения идёт здесь.
_PANEL_KEYS = ("text", "bbox", "size", "bold", "serif", "color", "origin",
               "font", "raster")


def panel(lines_):
    """Строки для панели: только JSON-совместимые поля, без numpy."""
    out = []
    for l in lines_ or []:
        d = {}
        for k in _PANEL_KEYS:
            v = l.get(k)
            if isinstance(v, (list, tuple)):
                v = [float(x) for x in v]
            elif isinstance(v, np.generic):
                v = v.item()
            d[k] = v
        out.append(d)
    return out


def raster_doc(doc):
    """Есть ли в документе хоть одна растровая страница (для автоочистки)."""
    for page in doc:
        if is_raster_page(page):
            return True
    return False


# ─────────────────────────── метрики шрифта-побратима ───────────────────────────

def _fb_font():
    """Шрифт-побратим (тот же рисунок, что у билета) или None."""
    if not os.path.isfile(FALLBACK_TTF):
        return None
    global _FNT
    if _FNT is None:
        try:
            _FNT = fitz.Font(fontfile=FALLBACK_TTF)
        except Exception:
            _FNT = False
    return _FNT or None


def _alt_font(ch):
    """Запасной системный шрифт для символов, которых нет у шрифта-побратима.

    В билет вшит только тот набор знаков, что на нём нарисован, и «Ж», «Ш»,
    «Ъ», «Ё», «;», «%» в него не попали. Рисовать их нечем — но выбросить
    символ нельзя: «Шевченко» превратилось бы в «евченко». Поэтому берём
    системный шрифт: рисунок чужой, зато буква на месте.
    """
    if ch in _ALT:
        return _ALT[ch]
    res = None
    for path in ALT_TTF:
        if not os.path.isfile(path):
            continue
        try:
            f = fitz.Font(fontfile=path)
            if f.has_glyph(ord(ch)):
                res = (path, f)
                break
        except Exception:
            continue
    _ALT[ch] = res
    return res


def _render(ch, size):
    """Отрисовка символа: карта краски и её границы в пикселях."""
    ck = (ch, int(round(size)))
    if ck in _RAW:
        return _RAW[ck]
    fnt = _fb_font()
    path = FALLBACK_TTF
    if fnt is None or not fnt.has_glyph(ord(ch)):
        alt = _alt_font(ch)
        if alt is None:
            _RAW[ck] = None
            return None
        path, fnt = alt
    pad = max(6, int(size * 0.6))
    adv = fnt.text_length(ch, size)
    w = int(adv + 2 * pad) + 4
    h = int(size * 1.8) + 2 * pad
    doc = fitz.open()
    pg = doc.new_page(width=w, height=h)
    base = int(pad + size)
    try:
        pg.insert_text((pad, base), ch, fontname="FB", fontsize=size,
                       fontfile=path, color=(0, 0, 0))
    except Exception:
        doc.close()
        _RAW[ck] = None
        return None
    pix = pg.get_pixmap(alpha=False)
    doc.close()
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, 0]
    a = np.clip((BG - arr.astype(np.float32)) / (BG - 30.0), 0, 1)
    rows = np.nonzero(a.max(axis=1) > 0.25)[0]
    cols = np.nonzero(a.max(axis=0) > 0.25)[0]
    if len(rows) == 0 or len(cols) == 0:
        _RAW[ck] = None
        return None
    r0, r1, c0, c1 = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
    res = {"g": a[r0:r1, c0:c1], "dy": base - r0, "dh": r1 - base,
           "w": int(cols[-1]) + 1 - int(cols[0])}
    _RAW[ck] = res
    return res


def _ratio(ch):
    """Высота краски символа в долях кегля (постоянная рисунка шрифта).

    Нужна, чтобы по высоте букв на билете восстановить кегль: у прописной и
    цифры это ~0.70, у строчной без выносов ~0.50, у «р», «у» ~0.70 (вынос),
    у «д», «ц», «щ» ~0.63. Мерим на крупном кегле (200 px) — там погрешность
    округления в пиксель ничего не значит.
    """
    if ch in _RATIO:
        return _RATIO[ch]
    r = 0.0
    g = _render(ch, 200)
    if g is not None:
        r = g["g"].shape[0] / 200.0
    _RATIO[ch] = r
    return r


def _word_size(tiles):
    """Кегль слова в px — по высотам его букв и метрикам шрифта-побратима.

    Метки на билете набраны КАПИТЕЛЬЮ (мелкие прописные): слово читается как
    «ряд», а нарисовано формами «РЯД», и высоты у них не строчные, а прописные.
    Поэтому пробуем обе трактовки и берём ту, где высоты букв согласуются между
    собой: у «ряд» строчная даёт 12.9 / 18.2 / 15.9 px (разнобой), а капительная
    — 12.9 / 12.9 / 13.4 px. Согласованность важнее абсолютной величины: именно
    по ней потом выбираются глифы нужного размера.
    """
    best = None
    for up in (False, True):
        est = []
        for c, h in tiles:
            r = _ratio(c.upper() if up else c)
            if r > 0.05:
                est.append(max(h - HALO, 1.0) / r)
        if not est:
            continue
        med = float(np.median(est))
        spread = float(np.mean([abs(e - med) for e in est])) / max(med, 1e-9)
        if best is None or spread < best[0]:
            best = (spread, med)
    return best[1] if best else 0.0


# ─────────────────────────── запасной шрифт ───────────────────────────

def _fallback_glyph(ch, size):
    """Глиф шрифтом-побратимом в нужном кегле, подогнанный по весу к билету.

    Нужен для символов, которых на билете нет вовсе (напр. «Ё», «Ш», «;»), и
    для тех, чей кегль в атласе не нашёлся. Рисунок тот же (PF Din Text Pro),
    но начертание светлее, поэтому краску догоняем до плотности, средней по
    атласу для этого кегля (см. calibrate_weight).
    """
    ck = (ch, int(round(size)))
    if ck in _FB:
        return _FB[ck]
    if ch.isspace():
        # пробела в атласе нет по построению (слова нарезаются по пробелам):
        # ставим пустую плитку шириной в аванс пробела этого кегля
        fnt = _fb_font()
        w = max(2, int(round(fnt.text_length(" ", size)))) if fnt else max(2, int(size * 0.3))
        res = {"g": np.zeros((1, w), np.float32),
               "rgb": np.zeros((1, w, 3), np.float32),
               "dy": 0, "dh": 0, "w": w, "wid": -2}
        _FB[ck] = res
        return res
    raw = _render(ch, size)
    if raw is None:
        _FB[ck] = None
        return None
    a = raw["g"]
    gain = _FB_GAIN.get(ck[1], 1.0)
    if abs(gain - 1.0) > 0.02:
        a = np.clip(a * gain, 0, 1)
    res = {"g": a, "rgb": np.array([40.0, 40.0, 40.0], np.float32),
           "dy": raw["dy"], "dh": raw["dh"], "w": a.shape[1], "wid": -1}
    _FB[ck] = res
    return res


def calibrate_weight(atlas):
    """Во сколько раз догонять краску запасного глифа до плотности билета.

    Меряем на круглых буквах (есть и на билете, и в шрифте): средняя плотность
    краски глифов атласа этого кегля против отрисованного запасного. Возвращает
    множитель краски по кеглям.

    Догоняем УМНОЖЕНИЕМ КРАСКИ, а не расширением контура. Расширение на один
    пиксель удваивало массу (плотность «П» 0.41 → 0.58 при цели 0.52), и
    перерисованная буква выходила жирнее родных соседей — на глаз это заметнее
    всего в «СЕКТОР Бельэтаж», где «Б» стоит вплотную к атласным буквам.
    Умножение подтягивает только полутон по кромке: ширина штриха та же, а вес
    сходится. Потолок 1.5 — выше это уже не «тот же рисунок светлее», а чужой
    шрифт, и лучше оставить как есть, чем зачернить кромки.
    """
    dens = {}
    for (ch, _key), lst in atlas.items():
        if ch.lower() not in ("о", "o", "а", "е", "с"):
            continue
        for e in lst:
            sz = int(round(e.get("size") or 0))
            if sz > 0:
                dens.setdefault(sz, []).append(float(e["g"].mean()))
    for size, vals in dens.items():
        g = _render("о", size)
        if g is None:
            continue
        cur = float(g["g"].mean())
        if cur <= 0:
            continue
        _FB_GAIN[size] = float(np.clip(float(np.mean(vals)) / cur, 0.85, 1.5))
        _FB.clear()
    return dict(_FB_GAIN)


# ─────────────────────────── сборка значения ───────────────────────────

def _norm_shape(a, box=24):
    """Маска глифа, приведённая к квадрату с сохранением пропорций.

    Сравнивать формы надо независимо от кегля, поэтому обе маски ужимаем в
    один квадрат. Пропорции сохраняем: у «а» и «л» высоты совпадают, и только
    по ширине их и можно различить.
    """
    from PIL import Image
    h, w = a.shape
    k = box / max(h, w, 1)
    im = Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8)).resize(
        (max(1, int(round(w * k))), max(1, int(round(h * k)))), Image.BILINEAR)
    out = Image.new("L", (box, box), 0)
    out.paste(im, ((box - im.width) // 2, (box - im.height) // 2))
    return np.asarray(out, dtype=np.float32) / 255.0


def _shape_ref(ch):
    """Эталонная маска символа — отрисовка шрифтом-побратимом того же рисунка."""
    if ch in _SHAPE:
        return _SHAPE[ch]
    m = None
    g = _render(ch, 64)
    if g is not None:
        m = _norm_shape(_trim(g["g"]))
    _SHAPE[ch] = m
    return m


def _trim(a):
    rows = np.nonzero(a.max(axis=1) > 0.25)[0]
    cols = np.nonzero(a.max(axis=0) > 0.25)[0]
    if not len(rows) or not len(cols):
        return a
    return a[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]


def _shape_fit(g, ch):
    """Насколько глиф похож на нужный символ (1 — как влитой, 0 — чужой).

    Нарезка слов на буквы иногда ошибается: куску краски достаётся соседняя
    буква, и в атласе под «а» лежит рисунок «н». По одной высоте такую подмену
    не видно — высоты у них одинаковые. Сравнение с отрисовкой шрифта-побратима
    того же рисунка её ловит.

    Мера — ПЕРЕСЕЧЕНИЕ масок (IoU), а не средняя разница яркостей. Средняя
    разница слишком мягкая: «н» под именем «а» давала по ней 0.68 при пороге
    0.55, то есть мусор проходил и заголовок собирался в «Кнакнзн». По IoU та же
    «н» даёт 0.44, а настоящие «а» — 0.63..0.76, и подмена отсекается с запасом.
    """
    ref = _shape_ref(ch)
    if ref is None:
        return None
    cur = _norm_shape(_trim(g["g"]))
    a, b = ref > SHAPE_THR, cur > SHAPE_THR
    union = int((a | b).sum())
    iou = float((a & b).sum()) / union if union else 0.0
    # Одного пересечения мало: у тонких букв («е») краска светлее шрифта-
    # побратима, и честная «е» набирает всего 0.62, тогда как чужая «в» — 0.57.
    # Корреляция к весу штриха нечувствительна, поэтому берём ЛУЧШУЮ из двух:
    # тонкая родная буква проходит, подмена — нет.
    x = ref.ravel() - ref.mean()
    y = cur.ravel() - cur.mean()
    d = float(np.sqrt((x * x).sum() * (y * y).sum()))
    corr = float((x * y).sum()) / d if d else 0.0
    return max(iou, corr)


def _aspect(ch):
    """Ширина краски символа в долях её высоты (постоянная рисунка шрифта).

    По одной высоте обрубок нарезки неотличим от буквы: куску краски иногда
    достаётся лишь вертикальная стойка соседа, и под «Б» в атласе лежит полоска
    5×19 — ровно той высоты, что просят. Пропорция её и выдаёт.
    """
    if ch in _AR:
        return _AR[ch]
    ar = None
    g = _render(ch, 200)
    if g is not None:
        h, w = g["g"].shape
        if h >= 2:
            ar = w / float(h)
    _AR[ch] = ar
    return ar


def _aspect_ok(e, ch):
    """Похожа ли пропорция глифа на нужную букву (обрубки и склейки — нет)."""
    want = _aspect(ch)
    if not want:
        return True                  # мерить нечем — не мешаем
    h, w = e["g"].shape
    if h < 2 or w < 2:
        return False
    got = w / float(h)
    return ASPECT_LO * want <= got <= ASPECT_HI * want


def _rescale(e, k):
    """Масштаб глифа атласа к другому кеглю (когда ровно такого нет).

    Глиф берём с билета, а не из запасного шрифта: рисунок и краска у него
    свои, и даже чуть увеличенный он выглядит роднее, чем чужая отрисовка.
    """
    if abs(k - 1.0) < 0.02:
        return e
    from PIL import Image
    h, w = e["g"].shape
    H, W = max(1, int(round(h * k))), max(1, int(round(w * k)))
    g = np.asarray(Image.fromarray((e["g"] * 255).astype(np.uint8)).resize(
        (W, H), Image.LANCZOS), dtype=np.float32) / 255.0
    return {"g": g, "rgb": e["rgb"], "dy": int(round(e["dy"] * k)),
            "dh": int(round(e["dh"] * k)), "w": W, "wid": e["wid"]}


def _pick(atlas, ch, key, skip=None):
    """Глиф символа нужного кегля: самый похожий из атласа или None.

    Регистр: сначала ищем ровно ту форму, что просят, и только если такой на
    билете нет вовсе — смотрим парную. Так надо из-за капители: метка «РЯД»
    нарисована формами «РЯД», но в атласе лежит под строчными «ряд», и без
    перебора регистра буква пропала бы. Обратный порядок портил значения:
    «Б» из «Бельэтаж» подменялась строчной «б» — та нарисована как «6».

    Кегли рядом с нужным перебираем ВСЕ, а не только ближайший. На «а» с целью
    21 px рядом лежали и испорченный ключ 20, и чистый 22; выбор одного
    ближайшего (20) упирался в мусор, хотя вплотную лежала родная буква.
    """
    same = [(k, v) for (c, k), v in atlas.items() if c == ch]
    near = [(k, v) for k, v in same if abs(k - key) <= 1]
    if not same:                     # такой формы на билете нет вовсе — берём парную
        alt = [(k, v) for (c, k), v in atlas.items() if c == ch.swapcase()]
        alt_near = [(k, v) for k, v in alt if abs(k - key) <= 1]
        if alt_near:
            near = alt_near
        elif alt:
            near = [min(alt, key=lambda kv: abs(kv[0] - key))]
    if not near and same:
        near = [min(same, key=lambda kv: abs(kv[0] - key))]
    pool = [(k, e) for k, lst in near for e in lst if e["wid"] != skip]
    if not pool:
        pool = [(k, e) for k, lst in near for e in lst]
    scored = []
    for k, e in pool:
        if not _aspect_ok(e, ch):    # обрубок нарезки или склейка двух букв
            continue
        fit = _shape_fit(e, ch)
        scored.append((1.0 if fit is None else fit, k, e))
    if not scored:                       # все экземпляры — не та буква
        return None
    best = max(scored, key=lambda s: s[0])
    if best[0] < SHAPE_MIN:              # все экземпляры — чужие буквы
        return None
    # среди похожих берём самый ТИПИЧНЫЙ по плотности: одна особо тёмная
    # отрисовка символа делала перерисованное значение жирнее соседей
    good = [s for s in scored if s[0] >= best[0] - 0.06]
    dens = np.array([float(s[2]["g"].mean()) for s in good])
    mid = float(np.median(dens))
    _fit, k, e = good[int(np.argmin(np.abs(dens - mid)))]
    if abs(k - key) <= 1:
        return e
    kk = key / max(k, 1)
    if abs(kk - 1.0) > RESCALE_MAX:
        # Мелкий глиф атласа (он с другой строки билета, где кегль меньше)
        # при растяжке на полтора размера превращается в мыло. У шрифта-побратима
        # тот же рисунок и ровно нужный кегль — он и нарисует букву.
        return None
    return _rescale(e, kk)


def compose(atlas, text, size, base, skip=None, missing=None, keys=None, slots=None):
    """Собрать значение из глифов атласа → (карта краски, цвета по пикселям).

    `size` — кегль в пикселях: высоту краски каждого символа считаем по метрикам
    шрифта-побратима, а не берём одну на всё слово. Иначе «Б» из «Бельэтаж»
    искалось бы по высоте «р» и находился бы глиф чужого размера.

    `keys` и `slots` — высоты краски и левые края букв исходного слова билета.
    Когда новое значение той же длины, что старое («11» → «12», «Партер» →
    «Балкон»), это точнее любой оценки: буквы встают ровно туда и того размера,
    как на билете. Ширину плитки для раскладки брать нельзя — нарезка иногда
    даёт обрубок («ы» в «Зрительный» вышла 2 px) или склейку на две буквы.
    """
    tiles = []
    for n, ch in enumerate(text):
        if ch.isspace():
            e = _fallback_glyph(ch, size)
        else:
            if keys and n < len(keys):
                key = keys[n]
            else:
                # +HALO: ключи атласа — это ИЗМЕРЕННЫЕ высоты краски, а в них
                # уже сидит ореол сглаживания; без поправки буква выходила на
                # пару пикселей ниже оригинала
                key = max(1, int(round((_ratio(ch) or CAP_RATIO) * size)) + int(HALO))
            e = _pick(atlas, ch, key, skip)
            if e is None:
                e = _fallback_glyph(ch, size)
        if e is None:
            if missing is not None:
                missing.append(ch)
            continue
        tiles.append(e)
    if not tiles:
        return None, None, 0
    # раскладка по горизонтали
    fnt = _fb_font()
    xs, x = [], 0
    for n, t in enumerate(tiles):
        xs.append(x)
        if slots and n + 1 < len(slots):
            x = int(slots[n + 1]) - int(slots[0])
        else:
            step = t["w"]
            if fnt is not None and n < len(text) and not text[n].isspace() \
                    and fnt.has_glyph(ord(text[n])):
                # шаг не меньше аванса символа и не меньше самой плитки: иначе
                # буквы наезжают друг на друга и слово читается слитно
                step = max(step, int(round(fnt.text_length(text[n], size))))
            x += step
    W = max(xs[n] + tiles[n]["w"] for n in range(len(tiles)))
    top = min(base - t["dy"] for t in tiles)
    bot = max(base + t["dh"] for t in tiles)
    a = np.zeros((bot - top, W), np.float32)
    rgb = np.zeros((bot - top, W, 3), np.float32)
    for n, t in enumerate(tiles):
        y = (base - t["dy"]) - top
        h, w = t["g"].shape
        x = xs[n]
        a[y:y + h, x:x + w] = np.maximum(a[y:y + h, x:x + w], t["g"])
        rgb[y:y + h, x:x + w] = t["rgb"]
    return a, rgb, top


def _bg_color(rgb, box):
    """Цвет фона вокруг стираемой полосы: медиана кольца шириной 3px."""
    x0, y0, x1, y1 = box
    h, w = rgb.shape[:2]
    rx0, ry0 = max(0, x0 - 4), max(0, y0 - 4)
    rx1, ry1 = min(w, x1 + 4), min(h, y1 + 4)
    ring = np.concatenate([
        rgb[ry0:y0 + 1, rx0:rx1].reshape(-1, 3),
        rgb[y1 + 1:ry1, rx0:rx1].reshape(-1, 3),
        rgb[y0 + 1:y1, rx0:x0 + 1].reshape(-1, 3),
        rgb[y0 + 1:y1, x1 + 1:rx1].reshape(-1, 3),
    ]) if rx1 > rx0 and ry1 > ry0 else rgb.reshape(-1, 3)
    if len(ring) == 0:
        return np.array([255.0, 255.0, 255.0], np.float32)
    light = ring[ring.mean(axis=1) > 200]
    src = light if len(light) >= max(8, len(ring) // 5) else ring
    return np.median(src, axis=0).astype(np.float32)


def _right_limit(rgb, box, z, page_w):
    """Докуда значению можно расти: начало чужой краски справа на этой строке."""
    x0, y0, x1, y1 = box
    h, w = rgb.shape[:2]
    band = rgb[y0:y1 + 1, x1 + 2:].astype(np.float32).mean(axis=2)
    if band.size:
        cols = np.nonzero(band.min(axis=0) < INK_THR)[0]
        if len(cols):
            return float(x1 + 2 + cols[0])
    return float(w)


def _word_box(tiles):
    """Габарит слова билета с запасом в пиксель — что стирать под новое значение."""
    if not tiles:
        return None
    return (int(min(t["x"] for t in tiles)) - 1,
            int(min(t["top"] for t in tiles)) - 1,
            int(max(t["x"] + t["w"] for t in tiles)) + 1,
            int(max(t["bot"] for t in tiles)) + 1)


def _slots_ok(tiles):
    """Годятся ли плитки слова как раскладка: нет склеек и обрубков.

    Нарезка иногда даёт кусок на две буквы («и» в «Зрительный» вышла 26 px при
    соседях по 12) или обрубок («ы» — 2 px). Ставить по таким левым краям
    нельзя: слово разъедется. Тогда раскладываем по авансам шрифта.
    """
    ws = [t["w"] for t in tiles]
    if len(ws) < 2:
        return True
    m = float(np.median(ws))
    return m > 2 and all(0.5 * m <= w <= 1.6 * m for w in ws)


def _same_word(a, b):
    """Слово панели совпадает с исходным (регистр не в счёт: метки в капители)."""
    return re.sub(r"\s+", "", (a or "")).lower() == re.sub(r"\s+", "", (b or "")).lower()


def _scale_h(a, rgb, k):
    """Горизонтальное сжатие собранного значения (как _draw_native: k=max(avail/w,.35))."""
    if k >= 0.999:
        return a, rgb, False
    W = max(1, int(round(a.shape[1] * k)))
    xi = np.clip((np.arange(W) / k).astype(int), 0, a.shape[1] - 1)
    return a[:, xi], rgb[:, xi], True


def _fit_width(a, col, avail, fit=None):
    """Сжать слово, если оно шире свободного места (не меньше FIT_MIN, как у вектора)."""
    w = a.shape[1]
    if w <= avail + 0.3 or w < 3:
        return a, col
    k = max(avail / w, FIT_MIN)
    a2, c2, _ = _scale_h(a, col, k)
    if fit is not None and k < FIT_TOL:
        fit.append("значение шире свободного места (%.0f px против %.0f свободных) — "
                   "сжато по горизонтали до %.0f%%" % (w, avail, k * 100))
    return a2, c2


def _paste(rgb, a, col, x, y):
    h, w = a.shape
    H, W = rgb.shape[:2]
    if x < 0 or y < 0 or x + w > W or y + h > H:
        return False
    region = rgb[y:y + h, x:x + w]
    al = a[:, :, None]
    rgb[y:y + h, x:x + w] = region * (1 - al) + col * al
    return True


def apply(doc_id, page, edits, fail, warn):
    """Применить правки к растровому билету. Картинка пересобирается из чистой."""
    st = state(doc_id, page)
    if st is None:
        return
    z = st["z"]
    rgb = st["rgb"].copy()
    atlas = st["atlas"]
    global _FB_GAIN_DOC
    # Множители краски считаются ПО АТЛАСУ конкретного билета. Кэш общий на
    # процесс, поэтому при смене билета его надо пересобрать: иначе второй
    # билет рисовался бы запасными глифами с чужим весом (в тестах оба билета
    # идут подряд в одном процессе — там это и вылезало).
    if _FB_GAIN_DOC != doc_id:
        try:
            _FB_GAIN.clear()
            calibrate_weight(atlas)
            _FB_GAIN_DOC = doc_id
        except Exception:
            pass
    for e in edits:
        text = e.get("text")
        if text is None or not str(text).strip():
            fail(e, "пустое значение")
            continue
        text = str(text)
        line = _match(st["lines"], e)
        if line is None:
            fail(e, "строка не найдена на растровом билете")
            continue
        if norm_line(line["text"]) != norm_line(str(e.get("orig") or line["text"])):
            fail(e, "поле на этом билете отличается от исходного — правка "
                    "перенесена с другого билета и отклонена")
            continue
        box = line["px"]
        # 1) пересобираем ТОЛЬКО изменённые слова. Нетронутые остаются родными
        #    пикселями билета — их не надо ни стирать, ни рисовать заново, и на
        #    них не накапливаются огрехи нарезки. Стираем полосу каждого
        #    изменённого слова и добираем слова, которых в новом значении нет
        missing, fit = [], []
        src = line["words"]
        neww = text.split()
        fnt = _fb_font()
        plan, erase, fited = [], [], None
        cursor = None
        for i, w in enumerate(neww):
            if i < len(src):
                s = src[i]
                st_tiles = s["tiles"]
                wbox = _word_box(st_tiles)
                # Сравнивать надо с ТЕМ ЖЕ написанием, что попало в панель:
                # на втором билете Vision читает «METO 12» (теряет «С»), панель
                # показывает починенное «МЕСТО 12», и сырое сравнение считало
                # слово изменённым — билет перерисовывался на пустой правке.
                if _same_word(fix_text(s["text"], s.get("nruns")), w):
                    cursor = int(max(t["x"] + t["w"] for t in st_tiles))
                    continue                  # слово не тронуто — пиксели не трогаем
                x, wsize = s["x0"], float(s["size"] or line["size"] * z)
                # высоты и левые края исходных букв — когда их ровно столько же,
                # сколько символов в новом слове: тогда каждой букве есть пара
                same = len(st_tiles) == len(w) and _slots_ok(st_tiles)
                keys = [t["bot"] - t["top"] for t in st_tiles] if same else None
                slots = [t["x"] for t in st_tiles] if same else None
            else:                             # слов больше, чем было: продолжаем справа
                s = src[-1]
                wbox = None
                x, wsize = int(cursor), float(s["size"] or line["size"] * z)
                keys = slots = None
            # предел справа: чужая краска на этой строке или начало следующего
            # слова — чтобы подросшее значение не наехало на соседнее поле
            limit = _right_limit(rgb, box, z, page.rect.width)
            if i + 1 < len(src):
                limit = min(limit, float(src[i + 1]["x0"] - 2))
            a, col, top = compose(atlas, w, wsize, line["base"],
                                  missing=missing, keys=keys, slots=slots)
            if a is None:
                continue
            # кегль и цвет панели: у растра масштабируем и перекрашиваем
            try:
                req = float(e.get("size") or line["size"])
            except (TypeError, ValueError):
                req = line["size"]
            if req > 0 and abs(req - line["size"]) > 0.05:
                a, col, _ = _scale_h(a, col, req / max(line["size"], 0.1))
            hx = str(e.get("color") or "").lstrip("#")
            if re.fullmatch(r"[0-9a-fA-F]{6}", hx):
                c = np.array([int(hx[j:j + 2], 16) for j in (0, 2, 4)], np.float32)
                col = np.tile(c, (a.shape[0], a.shape[1], 1))
            a, col = _fit_width(a, col, max(limit - x, 8.0), fit)
            if a.shape[1] < 2:
                continue
            plan.append((a, col, int(top), int(x)))
            erase.append(wbox or (int(x) - 1, int(top) - 1,
                                  int(x) + a.shape[1] + 1, int(top) + a.shape[0] + 1))
            space = max(2.0, fnt.text_length(" ", wsize)) if fnt else wsize * 0.3
            cursor = x + a.shape[1] + space
        # слова, которых в новом значении не осталось, просто стираем
        for s in src[len(neww):]:
            erase.append(_word_box(s["tiles"]))
        if not plan and not erase:
            continue
        # 2) стереть полосы изменённых слов фоном (ничего вокруг не двигаем)
        saved = []
        for b in erase:
            if not b:
                continue
            bx0, by0 = max(0, b[0]), max(0, b[1])
            bx1, by1 = min(rgb.shape[1], b[2]), min(rgb.shape[0], b[3])
            if bx1 <= bx0 or by1 <= by0:
                continue
            saved.append(((bx0, by0, bx1, by1), rgb[by0:by1, bx0:bx1].copy()))
            rgb[by0:by1, bx0:bx1] = _bg_color(rgb, (bx0, by0, bx1, by1))
        # 3) вклеить собранное
        for a, col, top, x in plan:
            if not _paste(rgb, a, col, x, top):
                fited = "значение не поместилось на билете"
                break
        if fited:
            for (bx0, by0, bx1, by1), old in saved:
                rgb[by0:by1, bx0:bx1] = old
            fail(e, fited)
            continue
        if fit:
            warn(e, fit[0])
        if missing:
            warn(e, "на билете нет таких символов — они нарисованы похожим шрифтом: "
                    + "".join(sorted(set(missing))))
    # картинку страницы заменяем целиком (одна картинка на лист)
    if st.get("xref"):
        from PIL import Image
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="PNG", optimize=True)
        page.replace_image(st["xref"], stream=buf.getvalue())


def norm_line(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _match(lines, e):
    """Найти строку по bbox правки, иначе по исходному тексту."""
    bb = e.get("bbox")
    if bb:
        try:
            b = [float(v) for v in bb]
        except (TypeError, ValueError):
            b = None
        if b:
            best, dist = None, 1e9
            for l in lines:
                d = max(abs(l["bbox"][0] - b[0]), abs(l["bbox"][1] - b[1]),
                        abs(l["bbox"][2] - b[2]), abs(l["bbox"][3] - b[3]))
                if d < dist:
                    best, dist = l, d
            if dist <= 3.0:
                return best
    orig = norm_line(str(e.get("orig") or ""))
    if orig:
        for l in lines:
            if norm_line(l["text"]) == orig:
                return l
    return None
