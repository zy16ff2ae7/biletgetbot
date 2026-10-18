import difflib
import hashlib
import io
import json
import os
import re
import struct
import threading
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

import fitz

ROOT = os.path.dirname(os.path.abspath(__file__))
FONT_REG = fitz.Font(fontfile=os.path.join(ROOT, "vendor", "Arial.ttf"))
FONT_BOLD = fitz.Font(fontfile=os.path.join(ROOT, "vendor", "Arial-Bold.ttf"))
INK = (0.05, 0.05, 0.08)
# Полные стиль-совместимые шрифты для символов, которых НЕТ в сабсете билета
# (новые цифры/буквы при правке). Сабсет хранит только реально использованные
# глифы: сменив «935» на «7000», упираемся в отсутствие «7»/«0» → нужен полный
# шрифт того же начертания. Liberation Serif ≡ Times, Liberation Sans ≡ Arial по
# метрикам, поэтому засечку подменяем Times'ом, рубленый — Arial'ом (по жирности).
# Кандидаты по убыванию предпочтения: macOS, Linux, Windows — берётся первый
# существующий, поэтому на macOS это ровно те же шрифты, что и были. На Linux
# засечку подменяет Liberation (метрически равен Times), а когда его нет —
# DejaVu: рисунок другой, зато кириллица и ₽ на месте и не приходится падать на
# встроенный фолбэк MuPDF (тот рисует ₽ засечкой посреди рубленой строки).
SERIF_FONTS = {
    False: ("/System/Library/Fonts/Supplemental/Times New Roman.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
            "/usr/share/fonts/liberation/LiberationSerif-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
            "C:/Windows/Fonts/times.ttf"),
    True:  ("/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
            "/usr/share/fonts/liberation/LiberationSerif-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
            "C:/Windows/Fonts/timesbd.ttf"),
}
SANS_FONTS = {
    False: ("/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/arial.ttf"),
    True:  ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf"),
}
# Системные шрифты с полным набором кириллицы и ₽ — последний рубеж фолбэка,
# когда во встроенном подмножестве билета нужного символа нет (₽ нет и в Times/Arial).
SYS_FONTS = [
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/SFNS.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
]
BOLD_RX = re.compile(r"bold|black|heavy|semib|demib|extrab", re.I)
# Разбор текста без вложенных картинок: см. _segments
TEXT_NO_IMG = fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES
# Засечка/рубленость по ИМЕНИ шрифта — надёжнее флага PDF: билетные Golos/DejaVu и
# афишный «Font» объявляют себя serif, будучи рубленными. Имя вернее: если оно
# узнаваемо — верим ему, иначе падаем на флаг serif (bit 2) из span.
SERIF_RX = re.compile(r"serif|times|roman|georgia|minion|garamond|charter|palatino", re.I)
SANS_RX = re.compile(r"sans|arial|arimo|helvet|golos|roboto|verdana|tahoma|calibri|"
                     r"segoe|myriad|inter|gothic|grotesq|bebas|nimbus|yandex|stolzl|"
                     r"madefor|avantgarde|pfdin|heros|frutiger|golos", re.I)


def _is_serif(name, flags):
    n = name or ""
    if SERIF_RX.search(n):
        return True
    if SANS_RX.search(n):
        return False
    return bool(flags & 4)

DOCS = {}
LOCK = threading.Lock()
_file_fonts = {}   # путь -> fitz.Font | None
_doc_fonts = {}    # doc_id -> {короткое имя шрифта: fitz.Font}
_ink = {}          # (id(font), codepoint) -> реально ли красится глиф
_valid = {}        # id(font) -> set(codepoints) из cmap (быстрый негативный фильтр)
_TRUSTED = set()   # id(font) полных шрифтов (vendor/системные) — has_glyph им можно верить
_TRUSTED.update({id(FONT_REG), id(FONT_BOLD)})
_MISS = object()   # маркер «в кэше нет» для атомарного .get (см. _has_ink)
# Тяжёлая работа с fitz — под одним замком: MuPDF не потокобезопасен, а
# ThreadingHTTPServer даёт поток на запрос. Общие fitz.Font (FONT_REG/системные
# фолбэки/_doc_fonts) и кэши _ink/_valid раньше использовались двумя запросами
# одновременно → искажённые метрики, битый рендер, KeyError на вычищенном кэше.
# Инструмент однопользовательский, поэтому цена сериализации ничтожна.
WORK = threading.RLock()


def norm(s):
    return s.replace("\xa0", " ")


def int_to_rgb(c):
    return (((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255)


def hex_to_rgb(h):
    h = (h or "").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        return None
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


# Символы, которыми пробуем cmap встроенного шрифта: латиница, пунктуация,
# кириллица и типографика. Больше не нужно — подсет билета столько не содержит.
_GLYPH_CPS = ([c for c in range(0x20, 0x80)] + [0xA0, 0xA7, 0xAB, 0xAD, 0xBB,
              0x401, 0x451, 0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D,
              0x2026, 0x20BD, 0x2116] + [c for c in range(0x400, 0x460)])


def _cmap_is_identity(doc, xref):
    """ToUnicode шрифта тождественный: CID → тот же код?

    Так делают subset-теры: вместо настоящей таблицы пишут
    `1 beginbfrange <0001><006D><0001> endbfrange`, и MuPDF честно отдаёт коды
    глифов как Юникод — панель видит «\\x13\\x07\\x18» вместо «E-TICKET».
    Править такой билет нельзя: ни поле распознать, ни исходное значение
    сличить. Отсутствие ToUnicode сюда не относится — там MuPDF берёт cmap
    самого шрифта и текст сразу правильный."""
    m = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", doc.xref_object(xref, compressed=True) or "")
    if not m:
        return False
    try:
        txt = (doc.xref_stream(int(m.group(1))) or b"").decode("latin-1", "replace")
    except Exception:
        return False
    rows = []
    for body, kind in ((r"beginbfchar(.*?)endbfchar", 2), (r"beginbfrange(.*?)endbfrange", 3)):
        for blk in re.findall(body, txt, re.S):
            for cells in re.findall(r"((?:<[0-9A-Fa-f]+>\s*){%d})" % kind, blk):
                rows.append([int(v, 16) for v in re.findall(r"<([0-9A-Fa-f]+)>", cells)])
    if not rows:
        return False
    # bfchar: <код> <символ>; bfrange: <от> <до> <начало> — тождественно, когда
    # код и назначение совпадают
    return all(r[0] == r[-1] for r in rows)


def _glyph_map(doc, xref):
    """{код глифа: символ} по cmap самого встроенного шрифта."""
    try:
        buf = doc.extract_font(xref)[3]
        fnt = fitz.Font(fontbuffer=buf) if buf else None
    except Exception:
        return {}
    if not fnt:
        return {}
    out = {}
    for cp in _GLYPH_CPS:
        try:
            g = fnt.has_glyph(cp)
        except Exception:
            continue
        if g and g not in out:
            out[g] = chr(cp)
    return out


def _font_fixes(page):
    """{имя шрифта: {код: символ}} — чем чинить текст страницы.

    Только для шрифтов с тождественным ToUnicode: у остальных текст и так
    настоящий, и подменять его кодами глифов было бы ошибкой."""
    out = {}
    try:
        fonts = page.get_fonts(full=True)
    except Exception:
        return out
    for f in fonts:
        xref, basefont, name = f[0], f[3], f[4]
        try:
            if not _cmap_is_identity(page.parent, xref):
                continue
        except Exception:
            continue
        gm = _glyph_map(page.parent, xref)
        if not gm:
            continue
        for k in {name, basefont, re.sub(r"^[A-Z]{6}\+", "", basefont or "")}:
            if k:
                out[k] = gm
    return out


def _fix_span(sp, fixes):
    """Починить символы спана по cmap шрифта. Возвращает True, если что-то меняли."""
    gm = fixes.get(sp.get("font"))
    if not gm:
        return False
    chars = sp.get("chars") or []
    hit = False
    for c in chars:
        u = gm.get(ord(c["c"])) if len(c.get("c") or "") == 1 else None
        if u and u != c["c"]:
            c["c"] = u
            hit = True
    return hit


def _segments(page):
    """Сегменты страницы: спаны одной строки, между которыми нет большого зазора.
    Берём rawdict (а не dict) — он даёт ещё и каждый символ с его bbox/origin.
    Это единственный источник сегментации для панели строк, автоочистки и правок,
    а символы нужны точечной правке: сохранить неизменное начало/конец строки
    (жирную метку «Место:», « руб.», нативный ₽) вместо перерисовки всей строки."""
    out = []
    blocks = page.get_text("rawdict", flags=TEXT_NO_IMG)["blocks"]
    # Чинить cmap-переводом только те шрифты, чей текст на странице реально битый:
    # тождественный ToUnicode в них выдал коды глифов (управляющие символы). У
    # здоровых билетов с тем же Adobe-Identity-UCS (TCPDF, mos.ru) MuPDF уже отдал
    # Текст правильно, и подмена кодами глифа только испортила бы его.
    bad_fonts = set()
    for block in blocks:
        if block["type"] != 0:
            continue
        for ln in block["lines"]:
            for sp in ln["spans"]:
                for c in sp.get("chars") or []:
                    ch = c.get("c") or ""
                    if len(ch) == 1 and ord(ch) < 0x20:
                        bad_fonts.add(sp.get("font"))
    fixes = _font_fixes(page)
    fixes = {k: v for k, v in fixes.items() if k in bad_fonts} if bad_fonts else {}
    # без TEXT_PRESERVE_IMAGES: картинки нам не нужны (ниже отбрасываем блоки не
    # типа 0), а rawdict перекодирует каждую в PNG — на билете с афишей в 3Мпикс
    # это 1.4с на страницу вместо 33мс, и платит за это каждое превью правки
    for block in blocks:
        if block["type"] != 0:
            continue
        for ln in block["lines"]:
            spans = []
            for sp in ln["spans"]:
                chars = sp.get("chars") or []
                _fix_span(sp, fixes)
                txt = norm("".join(c["c"] for c in chars))
                if not txt.strip():
                    continue
                sp = dict(sp)
                sp["text"] = txt
                spans.append(sp)
            if not spans:
                continue
            segs = [[spans[0]]]
            for sp in spans[1:]:
                prev = segs[-1][-1]
                gap = sp["bbox"][0] - prev["bbox"][2]
                if gap > max(prev["size"], sp["size"]) * 1.5:
                    segs.append([sp])
                else:
                    segs[-1].append(sp)
            out.extend(segs)
    return out


def _seg_info(seg):
    """Сводка сегмента — ровно тот словарь, что уходит в панель строк."""
    text = norm("".join(sp["text"] for sp in seg)).strip()
    if not text:
        return None
    x0 = min(sp["bbox"][0] for sp in seg)
    y0 = min(sp["bbox"][1] for sp in seg)
    x1 = max(sp["bbox"][2] for sp in seg)
    y1 = max(sp["bbox"][3] for sp in seg)
    dom = max(seg, key=lambda s: len(s["text"]))
    first = seg[0]
    return {
        "text": text,
        "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
        "size": round(dom["size"], 1),
        "bold": bool((dom["flags"] & 16) or re.search(r"bold|black|heavy|semib", dom["font"], re.I)),
        "serif": _is_serif(dom["font"], dom["flags"]),
        "color": dom["color"],
        "origin": [round(first["origin"][0], 1), round(first["origin"][1], 1)],
        "font": dom["font"],
    }


def _rect_overlap(a, b):
    """Доля площади МЕНЬШЕГО прямоугольника, накрытая пересечением."""
    ra, rb = fitz.Rect(a), fitz.Rect(b)
    it = ra & rb
    if it.is_empty:
        return 0.0
    return it.get_area() / max(min(abs(ra.get_area()), abs(rb.get_area())), 1e-6)


def _mark_stale(items):
    """items = [(инфо, спаны, индекс_обхода)] — помечает `stale` строки, поверх
    которых на том же месте нарисован ДРУГОЙ текст.

    Так выглядит билет, уже поправленный другим инструментом: старое значение из
    потока не удалено, новое вбито сверху (Afisha-…-edited: «9» шрифтом Font и «2»
    шрифтом LiberationSans на одном origin, seqno 163 и 185; «Коротенко Степан
    Михайлович» и «Кучерова» там же). Видно только последнее, а панель метила
    ряд/место по устаревшему спану — либо, когда bbox совпадали до десятых, не
    метила вообще. Порядок обхода rawdict совпадает с порядком seqno в трассе
    (проверено на этом файле), поэтому «нарисовано позже» = больший индекс.

    Гейт узкий, ровно под подмену значения на месте: тексты разные, близкий
    левый край (±3.5pt — подменённое значение бывает смещено: «20» поверх «1» на
    «Партер Ряд 12 Место 20…-1977139276» стоит на 2.6pt левее), тот же кегль,
    почти полное перекрытие и одна базовая линия
    (|Δy0| ≤ 0.1 высоты). Последнее обязательно: у мелкого шрифта bbox выше
    межстрочного шага, и СОСЕДНИЕ строки абзаца перекрываются на 0.68–0.84
    (Билет_20.08.26: «1. Вход в Учреждение…» и «2. Приобретая Билет…», Δy0=2.0) —
    без этого условия абзац правил помечался бы невидимым. Двойная печать одного
    и того же текста (имитация жирности) не в счёт — тексты равны; а если текст
    встречается на странице дважды, это дубль блока (там же весь абзац правил
    нарисован по второму разу со сдвигом), и кто сверху — не определить."""
    order = sorted(range(len(items)), key=lambda i: items[i][0]["bbox"][1])
    seen = {}
    for i, _s, _k in items:
        seen[i["text"]] = seen.get(i["text"], 0) + 1
    for pos, a in enumerate(order):
        ia = items[a][0]
        ha = ia["bbox"][3] - ia["bbox"][1]
        for b in order[pos + 1:]:
            ib = items[b][0]
            dy = ib["bbox"][1] - ia["bbox"][1]
            if dy > 3 + 0.5 * ha:
                break
            if ia["text"] == ib["text"] or seen[ia["text"]] > 1 or seen[ib["text"]] > 1:
                continue
            if dy > max(0.1 * ha, 0.4):
                continue
            if abs(ia["bbox"][0] - ib["bbox"][0]) > 3.5:
                continue
            sa, sb = ia["size"] or 1, ib["size"] or 1
            if max(sa, sb) / min(sa, sb) > 1.6:
                continue
            if _rect_overlap(ia["bbox"], ib["bbox"]) < 0.6:
                continue
            older = a if items[a][2] < items[b][2] else b
            items[older][0]["stale"] = True


def line_segments(page):
    """[(инфо-строка, спаны сегмента)] — то же, что extract_lines, но со спанами."""
    items = [(i, seg, k) for k, (seg, i) in
             enumerate((s, _seg_info(s)) for s in _segments(page)) if i]
    _mark_stale(items)
    items.sort(key=lambda p: (round(p[0]["bbox"][1]), p[0]["bbox"][0]))
    return [(i, seg) for i, seg, _k in items]


def extract_lines(page):
    return [i for i, _seg in line_segments(page)]


def page_objects(page):
    """Всё, что есть на странице и что можно править/удалять: текст, картинки,
    графика. Текст — те же строки, что в панели; картинки — по get_image_info;
    графика — штрихи get_drawings, слитые в кластеры (рамка/пунктир/круг)."""
    out = []
    for l in extract_lines(page):
        out.append({"kind": "text", "bbox": l["bbox"], "text": l["text"],
                    "tags": l.get("tags") or []})
    for im in page.get_image_info(xrefs=True):
        r = fitz.Rect(im["bbox"])
        if r.width < 2 or r.height < 2:
            continue
        # delete_image оставляет на месте прозрачную 1×1 — в «Элементы» не показываем
        if (im.get("width") or 99) <= 1 or (im.get("height") or 99) <= 1:
            continue
        out.append({"kind": "image", "bbox": [round(v, 1) for v in r],
                    "w": im["width"], "h": im["height"]})
    for rect, members in _draw_clusters(page):
        if rect.width < 1 and rect.height < 1:
            continue
        o = {"kind": "draw", "bbox": [round(v, 1) for v in rect]}
        if _is_pushkin_logo(rect, members):
            o["pushkin"] = True
        out.append(o)
    return out


def _draw_clusters(page):
    """Кластеры штрихов: [(рамка, штрихи)]. Фоновая подложка на весь лист не
    должна сливать всё в один объект (иначе «графика» = вся страница и удалить
    логотип отдельно нельзя), поэтому рамки площадью в полстраницы и больше в
    кластеризации не участвуют вовсе. Пунктир и рамки, идущие одной линией,
    сливаются в один объект."""
    page_area = page.rect.width * page.rect.height or 1
    draws = []
    for d in page.get_drawings():
        r = d.get("rect") and fitz.Rect(d["rect"])
        # штрих-линия даёт рамку нулевой высоты — это нормальный объект;
        # отбрасываем только вырожденные точки
        if not r or (r.width < 1 and r.height < 1):
            continue
        # фоновая подложка на пол-листа и больше не участвует: иначе она
        # сольёт всю графику страницы в один неудаляемый объект
        if r.get_area() >= 0.5 * page_area:
            continue
        draws.append(d)
    clusters = []
    for d in draws:
        r = fitz.Rect(d["rect"]) + (-2, -2, 2, 2)
        for c in clusters:
            if c[0].intersects(r):
                c[0] |= r
                c[1].append(d)
                break
        else:
            clusters.append([r, [d]])
    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if clusters[i][0].intersects(clusters[j][0]):
                    clusters[i][0] |= clusters[j][0]
                    clusters[i][1] += clusters[j][1]
                    del clusters[j]
                    changed = True
                    break
            if changed:
                break
    # одна линия из штрихов: низкие кластеры на одной высоте с зазором < 15pt
    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            ri, hi = clusters[i][0], clusters[i][0].height
            if hi > 8:
                continue
            for j in range(i + 1, len(clusters)):
                rj = clusters[j][0]
                if rj.height > 8:
                    continue
                if abs((ri.y0 + ri.y1) / 2 - (rj.y0 + rj.y1) / 2) > 3:
                    continue
                gap = max(ri.x0 - rj.x1, rj.x0 - ri.x1)
                if 0 <= gap < 15:
                    clusters[i][0] |= rj
                    clusters[i][1] += clusters[j][1]
                    del clusters[j]
                    changed = True
                    break
            if changed:
                break
    out = []
    for expanded, members in clusters:
        exact = fitz.Rect(members[0]["rect"])
        for m in members[1:]:
            exact |= fitz.Rect(m["rect"])
        out.append((exact, members))
    return out


def _is_pushkin_logo(rect, members):
    """Векторный логотип «Пушкинская карта»: тёмная плашка, занимающая почти
    весь кластер, поверх — белые буквы; плашка широкая. Так на билете внизу
    стоит чёрный логотип рядом с QR — его надо узнавать и удалять вместе с
    остальным блоком Пушкина."""
    if rect.width < 20 or rect.height < 8 or rect.width / rect.height < 1.6:
        return False
    area = rect.get_area() or 1
    dark = any(m.get("fill") and max(m["fill"]) < 0.35
               and fitz.Rect(m["rect"]).get_area() > 0.5 * area for m in members)
    white = any(m.get("fill") and min(m["fill"]) > 0.9
                and 0 < fitz.Rect(m["rect"]).get_area() < 0.9 * area
                for m in members)
    return dark and white


PUSHKIN_TEXT_RE = re.compile(
    r"\bпушкинск[а-яё]*\s*карт[а-яё]*\b|\bв\s*рамках\s*программ[а-яё]*\b", re.I)

# Доп. строки, которых нет на эталоне Afisha/МХТ после чистки
# («Партер 5 ряд 1 место 5500 (2).pdf»): реквизиты Афиши под таблицей.
# «Остались вопросы», правила театра и контакты — на эталоне есть, не трогаем.
AFISHA_NOISE_RE = re.compile(
    r"ООО\s*[\"«]?\s*Компания\s+Афиша|Дата\s+покупки", re.I)

# Продавец Афиши: «Дата покупки» снимается только на билетах Afisha/МХТ — на
# kassy.ru это обычное поле билета, его трогать не должны.
AFISHA_SELLER_RE = re.compile(r"компания\s+афиша", re.I)


def _is_gray_fill(f):
    if not f or len(f) < 3:
        return False
    r, g, b = f[0], f[1], f[2]
    return (abs(r - g) < 0.06 and abs(g - b) < 0.06
            and 0.78 <= max(r, g, b) <= 0.96)


def _is_green_fill(f):
    """Зелёный логотип МХТ — его сносить нельзя."""
    return bool(f and len(f) >= 3 and f[1] > f[0] + 0.08 and f[1] > f[2] + 0.08)


def _is_pushkin_watermark(rect, members, page_area, page_h):
    """Светло-серый декоративный слой «Пушкинская карта» ПОД полями билета
    (Afisha/МХТ): заливка ~0.8–0.95, много штрихов, в верхней половине листа.
    Зелёный логотип театра и шапка — не он."""
    if rect.width < 35 or rect.height < 20:
        return False
    if rect.get_area() >= 0.4 * page_area:
        return False
    if rect.y0 > page_h * 0.55:
        return False
    if any(_is_green_fill(m.get("fill")) for m in members):
        return False
    if rect.y1 < 42 and rect.height < 25:
        return False                      # чёрная шапка «КОНТРОЛЬ»
    total = rect.get_area() or 1
    gray = sum(fitz.Rect(m["rect"]).get_area() for m in members
               if _is_gray_fill(m.get("fill")))
    if gray >= 0.25 * total:
        return True
    return (len(members) >= 25 and rect.height >= 70 and rect.width >= 50
            and any(_is_gray_fill(m.get("fill")) for m in members))


def _pushkin_wm_rects(page):
    """Прямоугольники серого водяного знака «Пушкинская карта» — по ОТДЕЛЬНЫМ
    штрихам. Кластеризация склеивает их с зелёным логотипом МХТ и чёрной
    шапкой, из-за чего весь кластер нельзя снести.

    Полноразмерная серая панель (≥ половины ширины листа) — это вёрстка самого
    билета kassy.ru, в ней лежат все поля; снёс её — разорвал билет. Настоящий
    знак всегда узкий (≤160pt по корпусу), так что широкий прямоугольник не он.
    """
    page_area = page.rect.width * page.rect.height or 1
    page_w = page.rect.width or 1
    page_h = page.rect.height or 1
    gray, dark_acc = [], []
    for d in page.get_drawings():
        r = d.get("rect") and fitz.Rect(d["rect"])
        f = d.get("fill")
        if not r or not f or r.width < 10 or r.height < 8:
            continue
        if r.get_area() >= 0.35 * page_area:
            continue
        if r.width >= 0.5 * page_w:
            continue                      # панель контента, а не знак
        if r.y1 < 42 or r.y0 > page_h * 0.55:
            continue
        if _is_green_fill(f):
            continue
        if _is_gray_fill(f) and r.get_area() >= 200:
            gray.append(r)
        # тёмные акценты НЕ трогаем: плашка «Вход в зрительный зал…» (y≈339)
        # на эталоне есть, а зона правил не имеет отношения к Пушкину
    if not gray:
        return []
    return list(gray)


def _is_barcode_rect(r):
    """Вытянутый штрихкод (широкий и низкий), а не квадратный QR."""
    return r.width >= 2.5 * max(r.height, 1.0) and r.width > 20


def _has_pushkin_magenta(page, xref):
    try:
        im = page.parent.extract_image(xref)
        data = im.get("image")
        if not data:
            return False
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        sample = img.resize((min(w, 50), min(h, 50)))
        pixels = list(sample.get_flattened_data()) if hasattr(sample, "get_flattened_data") else list(sample.getdata())
        if pixels and isinstance(pixels[0], int):
            rgb_tuples = [pixels[k:k+3] for k in range(0, len(pixels), 3)]
        else:
            rgb_tuples = pixels
        magenta_count = sum(1 for r, g, b in rgb_tuples if r > 180 and g < 120 and b > 100)
        return magenta_count > len(rgb_tuples) * 0.05
    except Exception:
        return False


def _is_pushkin_image(r, pk_hits=(), page=None, xref=None):
    """Картинки блока «Пушкинская карта»: баннер в шапке (175×50), крупный
    водяной знак (451×372) и фиолетовый силуэт Пушкина в шапке KASSIR.RU (~44×43).
    Мелкие квадратные QR/логотипы театра — нет."""
    if r.width < 2 or r.height < 2:
        return False

    # Фиолетовый профиль Пушкина в шапке Kassir (между номером билета и Kassir.ru)
    if pk_hits and r.y0 < 90 and 250 <= r.x0 <= 430 and 15 <= r.width <= 75 and 15 <= r.height <= 75:
        return True

    if page is not None and xref is not None and _has_pushkin_magenta(page, xref):
        return True

    # квадратная иконка / QR
    if abs(r.width - r.height) < r.width * 0.45 and r.width < 80:
        return False
    # Баннер в шапке должен находиться рядом с подписью программы.
    # Одни размеры совпадают и с логотипом kassy.ru слева сверху.
    # Проверяем ДО штрихкода: 175×50 по пропорциям на него похож.
    if r.y0 < 90 and r.width >= 80 and r.height <= 90 and r.width >= 1.8 * r.height:
        return any(r.intersects(fitz.Rect(l["bbox"]) + (-2, -12, 2, 12))
                   for l in pk_hits)
    if _is_barcode_rect(r):
        return False
    # крупный водяной знак в верхних 2/3 листа
    return r.width >= 180 and r.height >= 120 and r.y1 < 842 * 0.72


def _table_grid_lines(page):
    """Тонкие тёмные линии сетки билета в панели полей (горизонталь + вертикаль)."""
    out = []
    for d in page.get_drawings():
        r = d.get("rect") and fitz.Rect(d["rect"])
        f = d.get("fill")
        if not r or not f or len(f) < 3 or max(f[:3]) > 0.4:
            continue
        thin = r.height < 2.5 or r.width < 2.5
        if not thin or r.get_area() < 8:
            continue
        # только сетка самой таблицы (низ рамки ~y=229); хвосты под ней
        # (y=232–258) сюда не входят — restore не должен их возвращать
        if r.y0 < 15 or r.y1 > 232:
            continue
        if r.x0 < 15 or r.x1 > 590:
            continue
        out.append(fitz.Rect(r))
    return out


def _grid_key(r):
    return (round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1))


def _restore_table_lines(page, before):
    """Вернуть линии сетки, срезанные краской водяного знака. Без них в
    таблице остаются дыры (пробел в оформлении) — не как на эталоне."""
    have = {_grid_key(r) for r in _table_grid_lines(page)}
    n = 0
    for r in before:
        if _grid_key(r) in have:
            continue
        try:
            page.draw_rect(r, color=None, fill=(0.1725, 0.1804, 0.2078), width=0)
            n += 1
        except Exception:
            pass
    return n


def _is_kassa24_family(lines):
    """Семейство билетов kassa24 / «Пушкинская карта» (Елизаров, kassa24.ru,
    «В рамках программы», строка ПОКУПАТЕЛЬ). У театральных (Театриум,
    зелёные клетки «Ряд|Место», ЗРИТЕЛЬ/ПЛАТЕЛЬЩИК) — другой тип, имена
    правятся руками, автоочистка их трогать не должна.

    Маркер — именно Пушкин/kassa24: слово «Покупатель» есть на ЛЮБОМ билете
    Афиши (в т.ч. театральных без Пушкина), и от него автоочистка сносила
    блок «Покупатель»/«Посетитель» вместе с именами (Afisha-Tickets-03107)."""
    blob = " ".join((l.get("text") or "") for l in lines).lower()
    return (bool(PUSHKIN_TEXT_RE.search(blob))
            or "kassa24" in blob)


def _personal_line_what(l):
    """Что персонального в строке (ПОКУПАТЕЛЬ / КЛИЕНТ / ФИО / ЗРИТЕЛЬ) или None."""
    t = (l.get("text") or "").strip()
    tl = t.lower()
    if re.match(r"покупатель(?:\s|[:.\-]|$)", tl):
        return f"покупатель: {t[:40]}"
    # kassy.ru: подпись и ФИО в одной строке — «Клиент: Рязанов Максим Юрьевич»
    if re.match(r"(клиент|фио)\s*[:.\-]", tl):
        return f"фио: {t[:40]}"
    if re.match(r"(?:билет\s+на\s+имя|на\s+имя)\s*[:.\-]", tl):
        return f"билет на имя: {t[:40]}"
    if "name" in (l.get("tags") or []):
        return f"имя: {t[:40]}"
    if re.fullmatch(r"(зритель|viewer|посетитель)\s*[:.\-–]?", tl):
        return f"метка: {t[:20]}"
    return None


def _kassir_name_rows(lines):
    """KASSIR: русская метка, английская метка и ФИО на одной строке.

    Связка с метками не зависит от медианного кегля мелких правил внизу PDF.
    Индексы применяются только в семействе билетов с Пушкинской картой.
    """
    rows = set()
    for label in lines:
        if label.get("stale") or not re.fullmatch(
                r"имя\s*,\s*фамилия\s*[:.]?", label["text"].strip(), re.I):
            continue
        b = fitz.Rect(label["bbox"])
        row = [i for i, line in enumerate(lines)
               if not line.get("stale") and line["bbox"][0] >= b.x0 - 1
               and abs((line["bbox"][1] + line["bbox"][3]) / 2
                       - (b.y0 + b.y1) / 2) <= max(2, b.height * 0.3)]
        if any(lines[i]["text"].strip().lower() == "name" for i in row):
            rows.update(row)
    return rows


def _pushkin_notice_lines(lines):
    """Инструкция о документе между двумя строками Пушкинской карты.

    Та же инструкция в общих правилах билета не относится к этому блоку.
    """
    captions = [fitz.Rect(l["bbox"]) for l in lines
                if PUSHKIN_TEXT_RE.search(l["text"])]
    result = []
    for line in lines:
        text = line["text"].strip().lower()
        if (not text.startswith("при входе на мероприятие")
                or "документ, удостоверяющий личность" not in text):
            continue
        b = fitz.Rect(line["bbox"])
        aligned = [r for r in captions if abs(r.x0 - b.x0) < 3]
        if (any(0 <= b.y0 - r.y1 <= 4 for r in aligned)
                and any(0 <= r.y0 - b.y1 <= 4 for r in aligned)):
            result.append(line)
    return result


def _is_intickets_template(page, lines):
    """Вертикальный Intickets с колонками заказа, билета, даты и стоимости."""
    if page.rotation or page.rect.width >= page.rect.height:
        return False
    blob = " ".join(l["text"] for l in lines)
    labels = {l["text"].strip().lower() for l in lines}
    return (bool(re.search(r'ООО\s*["«]?\s*ИНТИКЕТС\b', blob, re.I))
            and {"номер заказа:", "номер билета:", "дата покупки:", "стоимость:"} <= labels)


def _intickets_name_rows(page, lines):
    """Метка ФИО и все части имени под ней в одной колонке Intickets."""
    if not _is_intickets_template(page, lines):
        return set()
    found = set()
    for i, label in enumerate(lines):
        if label.get("stale") or not re.fullmatch(r"фио\s*[:.]?", label["text"].strip(), re.I):
            continue
        b = fitz.Rect(label["bbox"])
        for j, line in enumerate(lines):
            r = fitz.Rect(line["bbox"])
            if (not line.get("stale") and abs(r.x0 - b.x0) < 3
                    and b.y1 - 1 <= r.y0 <= b.y1 + 35
                    and line["text"].strip()):
                found.update((i, j))
    return found


def _is_operann_template(page, lines):
    """Шаблон Нижегородского театра оперы и балета им. А.С.Пушкина (operann.ru)."""
    if page.rotation or page.rect.width >= page.rect.height:
        return False
    full = " ".join(l["text"] for l in lines).lower()
    return (
        "operann.ru" in full
        or "оперы и балета им. а.с.пушкина" in full
        or "оперы и балета им. а.с. пушкина" in full
        or ("номер заказа:" in full and "билет №" in full and "нижегородский" in full)
    )


def _operann_name_rows(page, lines):
    """Строки покупателя/зрителя и Пушкинской карты под номером заказа на бланке operann.ru."""
    if not _is_operann_template(page, lines):
        return set()
    found = set()
    order_y = None
    for l in lines:
        if re.search(r"номер заказа", l["text"], re.I):
            order_y = l["bbox"][3]
            break
    if order_y is None:
        return found
    for i, l in enumerate(lines):
        if l.get("stale"):
            continue
        # Правая колонка под строкой заказа
        if l["bbox"][0] > page.rect.width * 0.4 and l["bbox"][1] >= order_y - 2:
            found.add(i)
    return found


def _chelny_fields(page, lines):
    """Поля шаблона Челнинского театра: метки запечены в фон, значения — текст.

    Узнаём его по двум инструкциям к считывателю, фону на весь лист и паре
    «заказ / тип оплаты» в левой колонке. ФИО ищем только в следующей ячейке.
    """
    if sum("BRING TICKET TO THE READER" in l["text"] for l in lines) < 2:
        return None, None
    if not any((fitz.Rect(im["bbox"]) & page.rect).get_area()
               >= 0.95 * page.rect.get_area() for im in page.get_image_info()):
        return None, None
    x = page.rect.width * 179 / 595
    for i, l in enumerate(lines):
        b = l["bbox"]
        text = re.sub(r"\s+", "", l["text"]).lower()
        if (text not in ("пушкинскаякарта", "банковскаякарта")
                or abs(b[0] - x) > 5 or not 0.08 * page.rect.height < b[1] < 0.13 * page.rect.height):
            continue
        order = any(re.fullmatch(r"\d{5,10}", o["text"].strip())
                    and abs(o["bbox"][0] - b[0]) < 3
                    and 15 < b[1] - o["bbox"][1] < 35 for o in lines)
        if not order:
            continue
        buyer = next((j for j, n in enumerate(lines)
                      if abs(n["bbox"][0] - b[0]) < 3
                      and 15 < n["bbox"][1] - b[1] < 35
                      and n["text"].strip()), None)
        return i, buyer
    return None, None


def _ticketland_pushkin_block(page, lines):
    """Нижний блок программы на горизонтальном Ticketland: подпись, возраст, баннер.

    Картинка связывается с двумя соседними подписями, а не с размерами листа.
    Изображение, использованное также вне блока, сохраняем.
    """
    if page.rotation or page.rect.width <= page.rect.height or not any(
            "ticketland.ru" in l["text"].lower() for l in lines):
        return [], set()
    age_re = re.compile(r"^допускаются\s*зрители\s*строго\s*от\s*14\s*до\s*22\s*лет[.!]?$", re.I)
    captions, ages = [], []
    for caption in lines:
        b = fitz.Rect(caption["bbox"])
        if b.y0 < page.rect.height * 0.5 or not PUSHKIN_TEXT_RE.search(caption["text"]):
            continue
        for age in lines:
            a = fitz.Rect(age["bbox"])
            if (age_re.fullmatch(age["text"].strip()) and abs(a.x0 - b.x0) < 5
                    and -2 <= a.y0 - b.y1 <= 16):
                captions.append(b)
                ages.append(age)
    if not ages:
        return [], set()
    uses = {}
    for im in page.get_image_info(xrefs=True):
        if im.get("xref") and im.get("width", 0) > 1 and im.get("height", 0) > 1:
            uses.setdefault(im["xref"], []).append(fitz.Rect(im["bbox"]))

    def above_caption(r):
        if not (80 <= r.width <= 240 and 20 <= r.height <= 110
                and 1.6 <= r.width / r.height <= 5):
            return False
        return any(0 <= b.y0 - r.y1 <= 35
                   and min(r.x1, b.x1) - max(r.x0, b.x0) > 0.5 * min(r.width, b.width)
                   for b in captions)

    return ages, {xref for xref, rects in uses.items() if all(above_caption(r) for r in rects)}


def _ticketland_name_rows(page, lines):
    """ФИО внизу третьей колонки Ticketland, включая уже очищенный кэш.

    У этого шаблона имя может быть целиком заглавным, со строчной буквы и без метки.
    Проверяем продавца, положение строки и отчество (или наличие блока программы),
    чтобы не принять правила за ФИО.
    """
    if (page.rotation or page.rect.width <= page.rect.height
            or not any("ticketland.ru" in l["text"].lower() for l in lines)):
        return set()
    found = set()
    has_pushkin = any(PUSHKIN_TEXT_RE.search(l["text"]) for l in lines)
    for i, line in enumerate(lines):
        if line.get("stale"):
            continue
        b = fitz.Rect(line["bbox"])
        if not (0.49 * page.rect.width <= b.x0 <= 0.55 * page.rect.width
                and b.x1 < 0.75 * page.rect.width
                and 0.91 * page.rect.height <= b.y0 <= 0.96 * page.rect.height):
            continue
        words = line["text"].split()
        is_patronymic = bool(re.search(r"(?:ович|евич|ич|овна|евна|инична|оглы|кызы)$",
                                       words[-1], re.I)) if words else False
        if (2 <= len(words) <= 4
                and all(re.fullmatch(r"[А-ЯЁа-яёA-Za-z'’\-]+", w) for w in words)
                and (has_pushkin or is_patronymic)):
            found.add(i)
    return found


def strip_pushkin_doc(doc, clear_name=True):
    """Снять «Пушкинскую карту» при открытии Afisha/МХТ — по эталону
    «Партер 5 ряд 1 место 5500 (2).pdf»:
      * серый водяной знак «Пушкинская карта» на панели;
      * надпись «В рамках программы…» (штрихкод и номер под ним — целы);
      * имя зрителя и метку «Зритель»;
      * ООО «Компания Афиша», «Дата покупки».
    QR + штрихкод не удаляем (как на эталоне). Линии сетки таблицы
    восстанавливаются после сноса водяного знака.

    ПОКУПАТЕЛЬ / ФИО чистятся на семействе kassa24/Пушкин. Театральные
    билеты (Театриум, зелёные клетки) НЕ трогаем — имена правятся руками.
    ФИО внизу третьей колонки Ticketland чистится и без блока программы,
    чтобы восстановленные вкладки не сохраняли пропущенное ранее имя."""
    out = []
    prepared_lines = [field_tags(extract_lines(page)) for page in doc]
    ticketland_blocks = [_ticketland_pushkin_block(page, lines)
                        for page, lines in zip(doc, prepared_lines)]
    banner_xrefs = {xref for _ages, refs in ticketland_blocks for xref in refs}
    if banner_xrefs:
        # delete_image заменяет xref во всём документе, не только на этой странице.
        # Каждое его использование должно принадлежать распознанному блоку.
        for page, (_ages, allowed) in zip(doc, ticketland_blocks):
            reused = {im.get("xref") for im in page.get_image_info(xrefs=True)}
            banner_xrefs.difference_update(reused - allowed)
    for pno in range(len(doc)):
        page = doc[pno]
        if page.rotation:
            continue
        _pin_form_res(page)
        lines = prepared_lines[pno]
        ticketland_ages, local_banners = ticketland_blocks[pno]
        ticketland_banners = local_banners & banner_xrefs
        ticketland_block = bool(ticketland_ages)
        intickets = _is_intickets_template(page, lines)
        intickets_names = _intickets_name_rows(page, lines)
        operann = _is_operann_template(page, lines)
        operann_names = _operann_name_rows(page, lines)
        preserve_art = ticketland_block or intickets or operann
        payment, buyer = _chelny_fields(page, lines)
        if buyer is not None:
            lines[buyer]["tags"] = ["name"]
        family = payment is not None or _is_kassa24_family(lines)
        ticketland_names = _ticketland_name_rows(page, lines)
        is_afisha = any(AFISHA_SELLER_RE.search(l["text"] or "") for l in lines)
        removed = []
        personal = set()
        if clear_name and (family or ticketland_names or intickets_names or is_afisha or operann_names):
            kassir_rows = _kassir_name_rows(lines)
            for i, l in enumerate(lines):
                if intickets and i not in intickets_names:
                    continue
                if operann and i not in operann_names:
                    continue
                is_tl_email = ticketland_block and bool(EMAIL_RE.search(l["text"]))
                if (ticketland_block or ticketland_names) and i not in ticketland_names and not is_tl_email:
                    continue
                if not (family or is_afisha or operann) and i not in (ticketland_names | intickets_names) and not is_tl_email:
                    continue
                if payment is not None and i != buyer:
                    continue
                what = _personal_line_what(l)
                if operann and i in operann_names:
                    what = f"зритель Operann: {l['text'][:40]}"
                elif not what and is_afisha and 208 <= l["bbox"][1] <= 235 and 150 <= l["bbox"][0] <= 450:
                    if not any(k in l["text"].lower() for k in ["заказ", "сектор", "ряд", "место"]):
                        what = f"зритель Afisha: {l['text'][:40]}"
                if i in ticketland_names:
                    what = f"фио Ticketland: {l['text'][:40]}"
                if i in intickets_names:
                    what = f"фио Intickets: {l['text'][:40]}"
                if i in kassir_rows:
                    what = f"строка имени: {l['text'][:40]}"
                if ticketland_block and EMAIL_RE.search(l["text"]):
                    what = f"почта: {l['text'][:40]}"
                if not what:
                    continue
                personal.add(i)
                page.add_redact_annot(fitz.Rect(l["bbox"]), fill=False)
                removed.append(what)
            # Применить персональные редакты отдельно, «аккуратным» режимом:
            # REMOVE_IF_TOUCHED съедает серую панель kassy.ru, на которой лежит
            # строка «Клиент: …» — билет остаётся с драной вёрсткой. IF_COVERED
            # сносит только то, что строка покрывает целиком (сам текст).
            if personal:
                page.apply_redactions(
                    images=fitz.PDF_REDACT_IMAGE_NONE,
                    graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                    text=fitz.PDF_REDACT_TEXT_REMOVE,
                )
        # Чистим Пушкина только если на странице есть Пушкинская карта.
        # «ООО Компания Афиша» / «Дата покупки» есть на ЛЮБОМ билете Афиши —
        # по ним триггериться нельзя, иначе landscape-Afisha теряют имя зрителя.
        if payment is not None and "пушкин" in lines[payment]["text"].lower():
            l = lines[payment]
            page.add_redact_annot(fitz.Rect(l["bbox"]), fill=False)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                  text=fitz.PDF_REDACT_TEXT_REMOVE)
            writer = fitz.TextWriter(page.rect)
            writer.append(l["origin"], "Банковская карта", font=FONT_REG, fontsize=l["size"])
            writer.write_text(page, color=int_to_rgb(l["color"]))
            removed.append("тип оплаты: Банковская карта")
        pk_hits = [l for i, l in enumerate(lines)
                   if i != payment and i not in personal
                   and PUSHKIN_TEXT_RE.search(l["text"] or "")]
        if not pk_hits:
            if removed:
                page.apply_redactions(
                    images=fitz.PDF_REDACT_IMAGE_NONE,
                    graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                    text=fitz.PDF_REDACT_TEXT_REMOVE,
                )
                out.append({"page": pno, "removed": removed})
            continue
        # «Дата покупки» и реквизиты Афиши — только на билетах Afisha/МХТ.
        # На kassy.ru это поля самого билета, их трогать не должны.
        is_afisha = any(AFISHA_SELLER_RE.search(l["text"] or "") for l in lines)
        notices = _pushkin_notice_lines(lines)
        hits = pk_hits + notices + [l for l in lines
                          if is_afisha and AFISHA_NOISE_RE.search(l["text"] or "")
                          and l not in pk_hits]
        _pin_form_res(page)
        grid_before = [] if preserve_art else _table_grid_lines(page)
        text_y1 = max(l["bbox"][3] for l in hits)
        zone = fitz.Rect(
            min(l["bbox"][0] for l in hits) - 90,
            min(l["bbox"][1] for l in hits) - 28,
            max(l["bbox"][2] for l in hits) + 28,
            text_y1 + 36) & page.rect
        page_area = page.rect.width * page.rect.height or 1
        wm = [] if preserve_art else _pushkin_wm_rects(page)
        wm_set = {_grid_key(r) for r in wm}

        uses = {}
        for inf in page.get_image_info(xrefs=True):
            r = fitz.Rect(inf["bbox"])
            xref = inf.get("xref")
            if xref and r.width > 2 and r.height > 2:
                uses.setdefault(xref, []).append(r)
        barcode_rects = [r for rects in uses.values() for r in rects
                         if _is_barcode_rect(r)]

        for i, l in enumerate(lines):
            if i in personal or i == payment:
                continue
            r = fitz.Rect(l["bbox"])
            t = (l["text"] or "").strip()
            if (PUSHKIN_TEXT_RE.search(t) or l in notices or l in ticketland_ages
                    or (is_afisha and AFISHA_NOISE_RE.search(t))):
                page.add_redact_annot(r, fill=False)
                removed.append(t[:48])

        for d in (() if preserve_art else page.get_drawings()):
            rd = d.get("rect") and fitz.Rect(d["rect"])
            f = d.get("fill")
            if not rd or not f or len(f) < 3 or min(f[:3]) <= 0.95:
                continue
            if not rd.intersects(zone) or rd.get_area() >= 0.05 * page_area:
                continue
            if _grid_key(rd) in wm_set:
                continue
            if any(rd.intersects(b) for b in barcode_rects):
                continue
            if any(rd.intersects(r) for rects in uses.values() for r in rects
                   if abs(r.width - r.height) < r.width * 0.45 and r.width < 80):
                continue
            page.add_redact_annot(rd, fill=False)

        # хвостовая черта под таблицей: на эталоне «Партер 5…» зона y≈232–258
        # пустая, на сырых Afisha остаётся короткая линия — снимаем
        for d in (() if preserve_art else page.get_drawings()):
            rd = d.get("rect") and fitz.Rect(d["rect"])
            f = d.get("fill")
            if not rd or not f or len(f) < 3:
                continue
            if (rd.height < 3 and 20 < rd.width < 90
                    and 232 <= rd.y0 <= 258 and max(f[:3]) < 0.45):
                page.add_redact_annot(rd, fill=False)
                removed.append(f"хвост под таблицей {rd}")

        if removed:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=(fitz.PDF_REDACT_LINE_ART_NONE if preserve_art
                          else fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED),
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )

        # Картинки блока «Пушкинская карта» (баннер 175×50, водяной знак 451×372):
        # delete_image, а не редакт — текст полей поверх водяного знака остаётся.
        for xref, rects in (() if intickets else uses.items()):
            if xref in ticketland_banners or all(_is_pushkin_image(r, pk_hits, page=page, xref=xref) for r in rects):
                try:
                    page.delete_image(xref)
                    removed.append(f"картинка Пушкина xref={xref}")
                except Exception:
                    pass

        # Векторный логотип «Пушкинская карта» внизу у QR — тёмная плашка с
        # белыми буквами. Текст «QR-код должен…» рядом не трогаем (TEXT_NONE).
        logo_boxes = []
        for rect, members in (() if preserve_art else _draw_clusters(page)):
            if not _is_pushkin_logo(rect, members):
                continue
            logo_boxes.append(fitz.Rect(rect.x0 - 2, rect.y0 - 2, rect.x1 + 2, rect.y1 + 2))
            removed.append(f"логотип Пушкина {rect}")
        if logo_boxes:
            for box in logo_boxes:
                page.add_redact_annot(box, fill=(1, 1, 1))
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=fitz.PDF_REDACT_TEXT_NONE,
            )

        if wm:
            for r in wm:
                page.add_redact_annot(r + (-1.0, -1.0, 1.0, 1.0), fill=False)
                removed.append(f"водяной знак {r}")
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=fitz.PDF_REDACT_TEXT_NONE,
            )

        # линии сетки, срезанные вместе с серым знаком — вернуть
        restored = _restore_table_lines(page, grid_before)
        if restored:
            removed.append(f"сетка восстановлена ({restored} линий)")

        if removed:
            out.append({"page": pno, "removed": removed})
    return out


def seg_chars(seg):
    """Символы сегмента по порядку, каждый со ссылкой на свой спан. Крайние
    пробелы отброшены, \\xa0 → пробел — чтобы индексы совпадали с text сегмента."""
    chars = []
    for sp in seg:
        for c in sp.get("chars") or []:
            chars.append({"c": norm(c["c"]), "bbox": c["bbox"],
                          "origin": c["origin"], "sp": sp})
    while chars and chars[0]["c"].isspace():
        chars.pop(0)
    while chars and chars[-1]["c"].isspace():
        chars.pop()
    return chars


def _common_edges(old, new):
    """Длина общего начала и общего конца двух строк (не перекрывая друг друга)."""
    n = min(len(old), len(new))
    p = 0
    while p < n and old[p] == new[p]:
        p += 1
    s = 0
    while s < n - p and old[len(old) - 1 - s] == new[len(new) - 1 - s]:
        s += 1
    return p, s


def _file_font(path):
    if path not in _file_fonts:
        try:
            f = fitz.Font(fontfile=path)
            _file_fonts[path] = f
            _TRUSTED.add(id(f))   # полный шрифт с диска — has_glyph надёжен
        except Exception:
            _file_fonts[path] = None
    return _file_fonts[path]


def _fallbacks(bold, serif=False):
    """Хвост цепочки для символов вне сабсета: сперва стиль-совместимый полный
    шрифт (Times для засечки / Arial для рубленого, той же жирности) — чтобы новые
    цифры/буквы совпали по начертанию с оригиналом; затем системный шрифт с ₽ и
    кириллицей (₽ нет в Times/Arial); в самом хвосте — vendor Arial как гарантия."""
    out = []
    # первый существующий из кандидатов: путь один на всю цепочку, остальные —
    # чужие ОС, на этой машине их просто нет
    for path in (SERIF_FONTS if serif else SANS_FONTS).get(bool(bold)) or ():
        f = _file_font(path)
        if f:
            out.append(f)
            break
    for path in SYS_FONTS:
        g = _file_font(path)
        if g and g not in out:
            out.append(g)
    tail = FONT_BOLD if bold else FONT_REG
    if tail not in out:
        out.append(tail)
    return out


def doc_fonts(doc, doc_id):
    """Все встроенные шрифты документа: короткое имя (без префикса) -> [fitz.Font].

    Список, а не один шрифт: один и тот же шрифт часто вложен НЕСКОЛЬКИМИ
    сабсетами (Chrome/QuickTickets кладут «Roboto-Regular» дважды — в одном
    буквы, в другом цифры и пунктуация; в mos.ru-билете «ArialMT» встречается
    23 раза). Раньше мы брали первый и теряли глифы, которые есть в соседнем
    сабсете ТОГО ЖЕ шрифта, — и цифры уезжали в Arial-фолбэк при живом родном
    шрифте. Теперь в цепочку идут все однофамильцы."""
    cached = _doc_fonts.get(doc_id)     # атомарно: .pop в _evict_locked мог успеть между in и []
    if cached is not None:
        return cached
    found = {}
    seen = set()
    for pno in range(doc.page_count):
        try:
            page_fonts = doc[pno].get_fonts(full=True)
        except Exception:
            continue
        for info in page_fonts:
            xref, base = info[0], info[3] or ""
            short = re.sub(r"^[A-Z]{6}\+", "", base)
            if not xref or xref in seen:
                continue
            seen.add(xref)
            try:
                _n, ext, _t, buf = doc.extract_font(xref)
                if not buf or len(buf) < 64 or ext in ("n/a", ""):
                    continue
                found.setdefault(short, []).append(fitz.Font(fontbuffer=buf))
            except Exception:
                continue
    with LOCK:
        _doc_fonts[doc_id] = found
    return found


def doc_font_list(doc, doc_id):
    """Плоский список всех встроенных шрифтов документа."""
    return [f for lst in doc_fonts(doc, doc_id).values() for f in lst]


def font_chain(doc, doc_id, name, bold, serif=False):
    """Родной шрифт спана (все его сабсеты) → стиль-совместимый полный шрифт
    (Times при засечках, Arial без) → прочие встроенные шрифты документа →
    системный. Стиль-фолбэк идёт СРАЗУ за родным: символ, которого нет в сабсете
    (напр. цифры «7»,«0» в цене, чей LiberationSerif-subset хранил лишь 3/5/9),
    берётся в ВЕРНОМ начертании, а не первым попавшимся чужим сабсетом другого
    стиля (так «7000» рисовалось рубленым)."""
    fonts = doc_fonts(doc, doc_id)
    chain = []
    for f in fonts.get(re.sub(r"^[A-Z]{6}\+", "", name or ""), []):
        chain.append(f)
    for f in _fallbacks(bold, serif):
        if f not in chain:
            chain.append(f)
    for key, lst in fonts.items():
        if bool(BOLD_RX.search(key)) == bold:
            for f in lst:
                if f not in chain:
                    chain.append(f)
    for f in doc_font_list(doc, doc_id):
        if f not in chain:
            chain.append(f)
    return chain or [FONT_BOLD if bold else FONT_REG]


def _has_ink(font, ch):
    """Подмножества билетных шрифтов часто хранят cmap/метрики, но выкидывают
    сами контуры: has_glyph() говорит «есть», а на странице пусто (так пропадала
    «М» в «Мария»). Поэтому проверяем реальную краску пробным рендером."""
    if ch.isspace():
        return True
    cp = ord(ch)
    key = (id(font), cp)
    # .get с маркером, а не «in + []»: между проверкой и чтением другой поток мог
    # вычистить кэш в _evict_locked (было KeyError → шрифт молча терялся).
    cached = _ink.get(key, _MISS)
    if cached is not _MISS:
        return cached
    # быстрые пути: полным шрифтам (Arial/системные) верим по has_glyph —
    # у них нет обрезанных контуров, рендерить каждый глиф не нужно.
    if id(font) in _TRUSTED:
        ok = False
        try:
            ok = bool(font.has_glyph(cp))
        except Exception:
            ok = False
        _ink[key] = ok
        return ok
    # для подмножеств: если кодовой точки нет в cmap — краски точно нет (мгновенно),
    # иначе рендерим (cmap может объявлять глиф без контура, как «ё»).
    fid = id(font)
    vc = _valid.get(fid, _MISS)
    if vc is _MISS:
        try:
            vc = set(font.valid_codepoints())
        except Exception:
            vc = None
        _valid[fid] = vc
    if vc is not None and cp not in vc:
        _ink[key] = False
        return False
    ok = False
    try:
        if font.has_glyph(cp):
            d = fitz.open()
            pg = d.new_page(width=40, height=40)
            tw = fitz.TextWriter(pg.rect)
            tw.append((8, 30), ch, font=font, fontsize=22)
            tw.write_text(pg)
            ok = min(pg.get_pixmap(alpha=False).samples) < 160
            d.close()
    except Exception:
        ok = False
    _ink[key] = ok
    return ok


def font_runs(text, chain, head=None):
    """Разбить строку на куски по шрифтам, реально рисующим нужные глифы.
    head (если задан) пробуется ПЕРВЫМ для НЕпробельных символов: так кириллическая
    «Р» знака рубля достаётся фирменному рубль-шрифту (родной ₽ с чёрточкой), а не
    перехватывается обычной «Р» из Times. Пробел через head не гоняем — у рубль-
    сабсета нет метрики пробела (1 глиф), иначе съедет позиция следующего знака."""
    runs = []
    for ch in text:
        seq = ([head] + chain) if (head and not ch.isspace()) else chain
        pick = None
        for cand in seq:
            try:
                if _has_ink(cand, ch):
                    pick = cand
                    break
            except Exception:
                continue
        if pick is None:
            pick = chain[0]
        if runs and runs[-1][0] is pick:
            runs[-1][1] += ch
        else:
            runs.append([pick, ch])
    return runs


def _short(name):
    return re.sub(r"^[A-Z]{6}\+", "", name or "")


def _utf16be_one(hexs):
    """Один код из <hex> ToUnicode-CMap; None — если это лигатура/пусто."""
    try:
        s = bytes.fromhex(hexs if len(hexs) % 2 == 0 else "0" + hexs).decode("utf-16-be")
    except Exception:
        return None
    return ord(s) if len(s) == 1 else None


def _to_unicode_map(data):
    """{юникод: cid} из ToUnicode-CMap встроенного шрифта. Это и есть недостающая
    unicode-cmap: сам font-файл сабсета её обычно не содержит (потому fitz.Font по
    нему ничего и не рисует), а в PDF она лежит рядом со шрифтом."""
    txt = data.decode("latin-1", "replace")
    out = {}
    for m in re.finditer(r"beginbfchar(.*?)endbfchar", txt, re.S):
        for a, b in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", m.group(1)):
            u = _utf16be_one(b)
            if u is not None:
                out.setdefault(u, int(a, 16))
    for m in re.finditer(r"beginbfrange(.*?)endbfrange", txt, re.S):
        toks = re.findall(r"<[0-9A-Fa-f]+>|\[|\]", m.group(1))
        i = 0
        while i + 2 < len(toks) + 1:
            if i + 1 >= len(toks) or toks[i] == "[" or toks[i] == "]":
                i += 1
                continue
            lo, hi = int(toks[i][1:-1], 16), int(toks[i + 1][1:-1], 16)
            i += 2
            if i < len(toks) and toks[i] == "[":            # <lo> <hi> [<u1> <u2> …]
                i += 1
                k = 0
                while i < len(toks) and toks[i] != "]":
                    u = _utf16be_one(toks[i][1:-1])
                    if u is not None:
                        out.setdefault(u, lo + k)
                    i += 1
                    k += 1
                i += 1
            elif i < len(toks):                             # <lo> <hi> <u первого>
                u0 = _utf16be_one(toks[i][1:-1])
                i += 1
                if u0 is not None and 0 <= hi - lo < 65536:
                    for k in range(hi - lo + 1):
                        out.setdefault(u0 + k, lo + k)
    return out


def _cid_widths(txt):
    """{cid: аванс в долях кегля} из массива /W CIDFont-а."""
    toks = re.findall(r"\[|\]|-?[\d.]+", txt or "")
    out, i = {}, 0
    while i < len(toks):
        if toks[i] in "[]":
            i += 1
            continue
        try:
            c = int(float(toks[i]))
        except ValueError:
            i += 1
            continue
        if i + 1 < len(toks) and toks[i + 1] == "[":         # c [w w w …]
            i, k = i + 2, 0
            while i < len(toks) and toks[i] != "]":
                try:
                    out[c + k] = float(toks[i]) / 1000.0
                except ValueError:
                    pass
                i += 1
                k += 1
            i += 1
        elif i + 2 < len(toks):                             # c1 c2 w
            try:
                c2, w = int(float(toks[i + 1])), float(toks[i + 2]) / 1000.0
            except ValueError:
                i += 1
                continue
            if 0 <= c2 - c < 65536:
                for cc in range(c, c2 + 1):
                    out[cc] = w
            i += 3
        else:
            break
    return out



def _nat_descendant(doc, x):
    """xref потомка-CIDFont у Type0-шрифта."""
    k, v = doc.xref_get_key(x, "DescendantFonts")
    if k == "array":
        m = re.search(r"(\d+)\s+0\s+R", v)
        return int(m.group(1)) if m else None
    if k == "xref":
        n = int(v.split()[0])
        o = doc.xref_object(n, compressed=True).strip()
        if o.startswith("["):
            m = re.search(r"(\d+)\s+0\s+R", o)
            return int(m.group(1)) if m else None
        return n
    return None


def _nat_read_font(doc, x):
    """Метрики встроенного Identity-H шрифта прямо из PDF:
    unicode→cid (ToUnicode), cid→gid (CIDToGIDMap), cid→аванс (/W, /DW).

    Всё это лежит в самом файле и является истиной для набора: аванс из /W
    совпадает с фактическим шагом пера на всём стенде (проверено посимвольно на
    9 источниках), а cid≠gid у половины билетов — PTSans «В» это cid 1042 и
    gid 222, у BebasNeue карта лежит потоком на 65536 записей. Раньше в Tj
    подставлялся gid из трассы: для Identity-карт это случайно работало, а на
    BebasNeue молча рисовался .notdef."""
    out = {"u2c": {}, "g": None, "w": {}, "dw": 1.0, "strict": False}
    try:
        k, v = doc.xref_get_key(x, "ToUnicode")
        if k != "xref":
            return out                  # без CMap не знаем, каким кодом рисовать
        out["u2c"] = _to_unicode_map(doc.xref_stream(int(v.split()[0])))
        n = _nat_descendant(doc, x)
        if n is None:
            return out
        out["strict"] = doc.xref_get_key(n, "Subtype")[1] == "/CIDFontType2"
        k, v = doc.xref_get_key(n, "CIDToGIDMap")
        if k == "xref":
            raw = doc.xref_stream(int(v.split()[0]))
            gg = struct.unpack(">%dH" % (len(raw) // 2), raw[:len(raw) // 2 * 2])
            out["g"] = {i: q for i, q in enumerate(gg) if q}
        k, v = doc.xref_get_key(n, "W")
        if k == "array":
            out["w"] = _cid_widths(v)
        elif k == "xref":
            out["w"] = _cid_widths(doc.xref_object(int(v.split()[0]), compressed=True))
        k, v = doc.xref_get_key(n, "DW")
        if k in ("int", "float"):
            out["dw"] = float(v) / 1000.0
    except Exception:
        pass
    return out


def _nat_font(nat, x):
    fi = nat["info"].get(x)
    if fi is None:
        fi = nat["info"][x] = _nat_read_font(nat["doc"], x)
    return fi


def page_natives(page):
    """Что на странице есть для перерисовки её же глифами: {имя шрифта: [xref]}
    плюс наблюдённые в трассе gid. Разбор метрик — ленивый (см. _nat_font): у
    мос.ру-билета 25 сабсетов ArialMT по 4.5k записей /W, тянуть их все ради
    одной правки незачем.

    Зачем всё это. У половины билетных PDF значение набрано Type0/Identity-H
    сабсетом без unicode-cmap: fitz.Font по такому шрифту рисовать нечем, и
    строка перерисовывалась системным Arial/Times. Метрики расходились до 19%
    («1 500 Р» Times-клоном с цифрой 0.42em против системных 0.5em) — значение
    выпирало из своего места. Но глифы-то в PDF есть: дописываем в content
    stream тот же ресурс шрифта с тем же кодом и тем же авансом."""
    if page.rotation:
        return None
    doc = page.parent
    fonts, res = {}, {}
    for x, _ext, _typ, base, nm, enc, *_ in page.get_fonts(full=True):
        if enc != "Identity-H":
            continue
        lst = fonts.setdefault(_short(base), [])
        if x not in lst:
            lst.append(x)
        res.setdefault(x, nm)
    if not fonts:
        return None
    gid, spw, st = {}, {}, {}
    for sp in page.get_texttrace():
        if sp.get("type") or sp.get("wmode"):
            continue
        if tuple(round(v, 3) for v in (sp.get("dir") or ())) != (1.0, 0.0):
            continue
        sh, sz, cs = _short(sp.get("font")), round(sp["size"], 2), sp["chars"]
        for a, b in zip(cs, cs[1:]):     # шаг пера внутри спана — набор производителя
            d = b[2][0] - a[2][0]
            if 0.05 < d < 60:
                st.setdefault((sh, sz, a[0]), []).append(d)
        if sh not in fonts:
            continue
        for u, g, _o, _b in cs:
            gid.setdefault((sh, u), set()).add(g)
        if sp.get("spacewidth") and sp["size"]:
            spw.setdefault(sh, sp["spacewidth"] / sp["size"])
    return {"doc": doc, "fonts": fonts, "res": res, "gid": gid, "spw": spw,
            "info": {}, "sq": {}, "st": st, "mod": {}, "grid": {}}


def _nat_pick(nat, sh, u, prefer=None):
    """(xref, cid, gid|None) — каким ресурсом и каким кодом страница рисует символ.

    Одноимённых сабсетов бывает много (QuickTickets: по два Roboto каждого веса,
    мос.ру: 25 сабсетов ArialMT, Афиша: два разных шрифта с базовым именем
    «Font»), и глифы в них нумерованы по-своему. Поэтому выбираем не по имени, а
    по делу: у кандидата должна быть эта буква в его ToUnicode, а если буква
    видна на странице — ещё и тот же gid, что в трассе. gid сравним только у
    CIDFontType2: у CFF-потомков он идёт через charset, там проверяем юникодом
    при обратном чтении.

    prefer — xref, выбранный по метрикам строки (см. _nat_best_xref): когда
    одноимённые сабсеты нумерованы одинаково, ни ToUnicode, ни gid их не
    различают, и только /W показывают, каким весом набрано."""
    want = nat["gid"].get((sh, u))
    pref = hit = best = None
    for x in nat["fonts"].get(sh, ()):
        fi = _nat_font(nat, x)
        cid = fi["u2c"].get(u)
        if cid is None or cid > 0xFFFF:
            continue
        gid = cid if fi["g"] is None else fi["g"].get(cid, 0)
        if fi["strict"]:
            if not gid:
                continue                        # .notdef — нарисовали бы пустоту
            if want and gid not in want:
                continue                        # тот же символ, но чужой сабсет
        else:
            gid = None
        got = (x, cid, gid)
        if x == prefer:
            pref = got
        if hit is None and want and gid is not None and gid in want:
            hit = got
        if best is None:
            best = got
    return pref or hit or best


def _nat_steps(chars):
    """Шаг пера, замеренный в самой правимой строке: {(шрифт, кегль, символ): pt}.

    Только внутри одного спана: между спанами стоит свой Td, и он не равен
    авансу. Именно на этом раньше ломалось сохранение хвоста в «1 500 Р» — «шаг»
    последнего пробела мерился до знака ₽ из соседнего спана (1.77pt против
    аванса 2.36pt), модель не сходилась с PDF и хвост перерисовывался.

    Из нескольких наблюдений берём самое частое (при равенстве — первое):
    выключка может растянуть отдельный пробел, но не большинство символов."""
    acc = {}
    for a, b in zip(chars, chars[1:]):
        if a["sp"] is not b["sp"] or not a["c"]:
            continue
        st = b["origin"][0] - a["origin"][0]
        if st <= 0:
            continue
        acc.setdefault((_short(a["sp"]["font"]), round(a["sp"]["size"], 2),
                        ord(a["c"])), []).append(st)
    return {k: _mode_avg(v) for k, v in acc.items()}


def _mode_avg(vals):
    """Самый частый шаг — но не округлённым, а средним по его же замерам.

    Считать моду надо по округлённым до 0.001pt значениям (иначе равных не
    найти), а класть в аванс округление нельзя: на «2304.00 руб.» +0.0003pt на
    цифру к восьмому символу давали 0.003pt, и при мелком рендере растеризатор
    снимал «у» на целый пиксель — круг расходился на один глиф при верных
    авансах."""
    n, first = {}, {}
    for i, v in enumerate(vals):
        q = round(v, 3)
        if q not in n:
            n[q], first[q] = 0, i
        n[q] += 1
    m = max(n, key=lambda q: (n[q], -first[q]))
    sel = [v for v in vals if round(v, 3) == m]
    return sum(sel) / len(sel)


def _nat_squeeze(nat, x, sh, sz, steps):
    """Во сколько раз фактический шаг набора уже /W шрифта.

    Билет № 1097180684 набран на целочисленной сетке: «/F8 19 Tf» и ровно «8 0 Td»
    на каждую цифру под масштабом 0.786×0.75 — шаг 4.719pt против аванса /W
    5.021pt, то есть на 6% плотнее. Глифы при этом нормальной ширины (никакого Tz
    в потоке нет), поэтому отношение идёт только в аванс: без него «2 400» из тех
    же цифр считалось на 0.6pt шире старого и хвост с родным ₽ перерисовывался.

    Берём медиану и только если отношение постоянно: у выключенного текста
    (Афиша) шаги гуляют вокруг /W, там своего сжима нет."""
    key = (x, round(sz, 2))
    if key in nat["sq"]:
        return nat["sq"][key]
    fi = _nat_font(nat, x)
    rs = []
    for (sh2, sz2, u), st in steps.items():
        if sh2 != sh or abs(sz2 - sz) > 0.02 or chr(u).isspace():
            continue
        cid = fi["u2c"].get(u)
        if cid is None:
            continue
        w = fi["w"].get(cid, fi["dw"]) * sz
        if w > 0.05:
            rs.append(st / w)
    k = 1.0
    if rs:
        rs.sort()
        med = rs[len(rs) // 2]
        if 0.5 <= med <= 2.0 and 4 * sum(abs(r - med) < 0.004 for r in rs) >= 3 * len(rs):
            k = med if abs(med - 1.0) > 0.004 else 1.0
        nat["sq"][key] = k
    return k


def _nat_best_xref(nat, sh, sz, steps):
    """Из одноимённых шрифтов — тот, чьи /W сходятся с замеренным шагом пера, или None.

    Билет QuickTickets (order_20475307) держит два Identity-H шрифта с одним
    базовым именем /(null): ресурсы GolosTextRegularFull и GolosTextSemiBoldFull.
    Нумерация глифов у них общая (cid 406 = «3» в обоих), ToUnicode и дескрипторы
    неотличимы, так что _nat_pick честно берёт первый — и цена перерисовывалась
    светлым вместо полужирного. Различаются только /W: на 8pt шаги пера
    4.832/4.792/5.112pt — это ровно SemiBold, Regular на 3-4% уже.

    Решаем один раз на (шрифт, кегль), а не на символ: у отдельной буквы веса
    иногда совпадают до сотых (у «р» 0.615em в обоих), и посимвольный выбор
    смешал бы веса внутри слова.

    Берём только при явном перевесе: /W победителя должны сойтись почти в точку,
    а второй кандидат — заметно промахнуться. Если набор идёт по своей сетке
    (см. _nat_squeeze), шаги уже /W у всех кандидатов, судить не по чему —
    возвращаем None и остаёмся на прежнем выборе.

    По одному символу тоже решаем, но строже: у ряда «8» шага внутри строки нет
    вовсе, а на странице тем же кеглем набрано только «17» — единственная улика
    это шаг единицы 5.645pt при 11.2pt, и он ровно SemiBold (Regular 5.432).
    В потоке там и стоит /GolosTextSemiBoldFull — проверено по Tf."""
    xs = nat["fonts"].get(sh) or ()
    if len(xs) < 2:
        return None
    sc = []
    for x in xs:
        fi = _nat_font(nat, x)
        err = n = 0
        for (sh2, sz2, u), st in steps.items():
            if sh2 != sh or abs(sz2 - sz) > 0.02 or chr(u).isspace():
                continue
            cid = fi["u2c"].get(u)
            if cid is None:
                continue
            w = fi["w"].get(cid, fi["dw"]) * sz
            if w > 0.05:
                err += abs(w - st)
                n += 1
        if n:
            sc.append((err / n, x, n))
    if len(sc) < 2:
        return None
    sc.sort()
    fit, mul = (0.005, 4.0) if sc[0][2] < 2 else (0.01, 2.0)
    if sc[0][0] < fit * sz and sc[1][0] > mul * sc[0][0] + 0.004 * sz:
        return sc[0][1]
    return None


def _nat_modes(nat):
    """Шаг пера по всей странице: {(шрифт, кегль, символ): pt}, самое частое значение.

    Зачем, если есть замеры в самой строке. В строке видны только её символы, а
    новое значение почти всегда приносит другие: у «400 ₽» замерены 4, 0 и пробел,
    а набрать надо «1 250 ₽». Для 1, 2 и 5 приходилось брать /W шрифта, и у Афиши
    он врал — 3.31 вместо 3.00 у единицы, 5.47 вместо 5.25 у пятёрки: «1 250 ₽»
    выходило на 0.6pt шире, чем набрал бы сам билет. На странице те же цифры уже
    набраны в датах и суммах тем же шрифтом и кеглем — берём шаг оттуда.

    Мода, а не среднее: выключка растягивает отдельные пробелы, но не большинство
    символов (у Афиши на 9pt каждый символ даёт один и тот же шаг в 2-11 замерах)."""
    if not nat["mod"]:
        nat["mod"] = {k: _mode_avg(v) for k, v in nat["st"].items()}
    return nat["mod"]


def _nat_grid(nat, sh, sz):
    """Шаг сетки набора для (шрифт, кегль) или None: на что кратны шаги пера.

    Билеты «из браузера» ставят каждый глиф в целый device-пиксель: у Афиши на
    9pt все шаги кратны 0.75pt (1/96 дюйма) — 2.999, 4.499, 5.248, 6.748; у
    Times-билета сетка 0.5899pt (8 единиц под масштабом 0.786×0.75). Нужно для
    символов, которых на странице нет вовсе: их аванс берётся из /W, и его стоит
    положить в узел сетки, а не между.

    Наибольший шаг, объясняющий почти все моды. Требуем не меньше четырёх РАЗНЫХ
    мод: если все шаги одинаковы (Times-билет набран одной цифровой сеткой), то
    «сеткой» станет сам аванс, и редкий символ уехал бы на полшага. Случайно
    сетка тоже не выходит: попасть в допуск 0.02pt при шаге 0.75 — это 5% на моду,
    а требуем 90%."""
    key = (sh, round(sz, 2))
    if key in nat["grid"]:
        return nat["grid"][key]
    obs = [v for (sh2, sz2, u), v in _nat_modes(nat).items()
           if sh2 == sh and abs(sz2 - key[1]) < 0.02 and not chr(u).isspace()]
    g = None
    if len(obs) >= 6 and len({round(v, 3) for v in obs}) >= 4:
        med = sorted(obs)[len(obs) // 2]
        cand = {round(s / n, 4) for s in set(obs) for n in range(1, 13)
                if 0.15 <= s / n <= 0.5 * med}
        for c in sorted(cand, reverse=True):
            if sum(1 for s in obs if abs(s - round(s / c) * c) <= 0.02) >= 0.9 * len(obs):
                g = c
                break
    nat["grid"][key] = g
    return g


def native_runs(nat, chars, text, font, size, pen_y=None):
    """Символы text родными глифами: [(xref, кегль, аванс, сдвиг базовой, cid,
    gid|None, юникод, сжим)] или None, если хоть одного символа нет.

    Сначала пробуем шрифт своего спана, потом остальные шрифты этой же строки —
    так знак рубля достаётся фирменному ALSRubl*-шрифту с его собственным кеглем
    (6.5pt при цене 11.2pt: у него ascender 1.11em, поэтому ₽ выходит в рост
    цифр) и с его собственной базовой линией. Не хватило хоть одного символа —
    возвращаем None и рисуем фолбэком: смесь родных и системных глифов в одном
    слове выглядит хуже, чем ровный системный набор.

    Аванс: сперва шаг, замеренный в самой правимой строке (он воспроизводит набор
    посимвольно, включая выключку), потом шаг этого символа где-нибудь ещё на
    странице тем же шрифтом и кеглем (см. _nat_modes), и лишь потом /W шрифта со
    сжимом набора — с укладкой в сетку набора, если она есть."""
    if not nat or not text:
        return None
    line = _nat_steps(chars or [])           # шаги своей строки — они важнее
    steps = dict(_nat_modes(nat))            # и шаги со всей страницы
    steps.update(line)
    cand = [(_short(font), float(size), 0.0)]
    for c in chars or []:
        sp = c["sp"]
        q = (_short(sp["font"]), float(sp["size"]),
             (float(sp["origin"][1]) - pen_y) if pen_y is not None else 0.0)
        if q not in cand:
            cand.append(q)
    # какой из одноимённых сабсетов взять — по шагам своей строки, а страничные
    # шаги для них смешаны между весами и годятся лишь когда своих замеров нет
    pref = {}
    for sh, sz, _off in cand:
        k = (sh, round(sz, 2))
        if k not in pref:
            pref[k] = (_nat_best_xref(nat, sh, sz, line)
                       or _nat_best_xref(nat, sh, sz, steps))
    out = []
    for ch in text:
        u = ord(ch)
        run = None
        for sh, sz, off in cand:
            got = _nat_pick(nat, sh, u, pref.get((sh, round(sz, 2))))
            if got is None:
                continue
            x, cid, gid = got
            fi = _nat_font(nat, x)
            # сжим набора — только по своей строке: по всей странице у Times-билета
            # мешаются буквы с другим отношением, и постоянные 0.94 на цифрах
            # перестают читаться (тогда «11» уезжало на 0.6pt)
            kz = _nat_squeeze(nat, x, sh, sz, line)
            adv = steps.get((sh, round(sz, 2), u))
            if adv is None:
                adv = fi["w"].get(cid, fi["dw"]) * sz * kz
                g = _nat_grid(nat, sh, sz)
                if g and adv > g / 2:
                    adv = round(adv / g) * g
            run = (x, sz, adv, off, cid, gid, u, kz)
            break
        if run is None and ch.isspace():         # пробел могли не рисовать глифом
            for sh, sz, off in cand:
                adv = steps.get((sh, round(sz, 2), u)) or nat["spw"].get(sh, 0.0) * sz
                if adv > 0:
                    run = (None, sz, adv, off, None, None, u, 1.0)
                    break
        if run is None:
            return None
        out.append(run)
    return out


def native_width(runs):
    return sum(r[2] for r in runs or ())





def open_doc(doc_id):
    data = DOCS.get(doc_id)
    if data is None:
        raise KeyError(doc_id)
    return fitz.open(stream=bytes(data["bytes"]), filetype="pdf")


MAX_DOCS = 96                     # держим в памяти только последние N PDF
MAX_UPLOAD = 80 * 1024 * 1024     # предел на один загружаемый файл, байт
MAX_JSON = 32 * 1024 * 1024       # предел на JSON-тело (правки сотни вкладок влезают)
MAX_BYTES = 512 * 1024 * 1024     # общий бюджет памяти под PDF (96 билетов мелкие,
                                  # но один 80-мегабайтный отсканированный съест всё)
# Открыть доступ с чужих хостов (показ через ngrok/trycloudflare) — только явным
# BILETNAYA_ALLOW_REMOTE=1. По умолчанию редактор слушает 127.0.0.1 и принимает
# запросы только от самого себя: иначе любая открытая в браузере страница может
# слать сюда POST и править/качать чужие билеты (DNS-rebinding).
ALLOW_REMOTE = os.environ.get("BILETNAYA_ALLOW_REMOTE") == "1"
# Адрес, на котором слушаем. По умолчанию петля — тот же смысл, что у ALLOW_REMOTE:
# не открывать редактор наружу без явной просьбы. BILETNAYA_HOST=0.0.0.0 нужен,
# когда браузер приходит не с этой машины (туннель/контейнер с预览); сам по себе
# он проверку источника не отключает — для этого всё так же нужен ALLOW_REMOTE.
BIND_HOST = os.environ.get("BILETNAYA_HOST") or "127.0.0.1"


def _evict_locked(protect=frozenset()):
    """Вытеснение по обоим порогам (число файлов и суммарный вес). Вызывать под LOCK.

    protect — id, которые НЕЛЬЗЯ трогать: свеже-созданные части одного ответа
    (иначе «развязать» на >96 частей вытеснял свои же ранние части ещё до отправки
    ответа, и первые вкладки открывались с мёртвым id → 404 «unknown doc»)."""
    total = sum(len(v["bytes"]) for v in DOCS.values())
    evicted = False
    while len(DOCS) > 1 and (len(DOCS) > MAX_DOCS or total > MAX_BYTES):
        old = next((k for k in DOCS if k not in protect), None)
        if old is None:
            break                      # всё оставшееся защищено — вытеснять нечего
        total -= len(DOCS[old]["bytes"])
        DOCS.pop(old, None)
        _doc_fonts.pop(old, None)
        _drop_raster(old)
        evicted = True
    if evicted:
        _ink.clear()   # ключи по id(font) — после вычистки шрифтов не переиспользуем
        _valid.clear()


CACHE_DIR = os.path.join(os.path.expanduser("~"), ".biletnaya", "cache")
CACHE_KEEP = 300                      # файлов в дисковом кэше
CACHE_BYTES = 1024 * 1024 * 1024      # и не больше 1 ГБ суммарно


def _cache_ok(sha):
    return bool(re.fullmatch(r"[0-9a-f]{8,64}", sha or ""))


def _cache_trim():
    """LRU по mtime: кэш не должен расти без предела (билеты качают пачками)."""
    try:
        files = []
        for nm in os.listdir(CACHE_DIR):
            if nm.endswith(".pdf"):
                p = os.path.join(CACHE_DIR, nm)
                st = os.stat(p)
                files.append((st.st_mtime, st.st_size, p))
    except OSError:
        return
    files.sort()
    total = sum(f[1] for f in files)
    while files and (len(files) > CACHE_KEEP or total > CACHE_BYTES):
        _m, size, p = files.pop(0)
        try:
            os.remove(p)
        except OSError:
            break
        total -= size


def cache_put(sha, data):
    """Сложить исходный PDF на диск под именем sha контента.

    DOCS живут в памяти и умирают с процессом, а вкладки в браузере ключуются по
    sha (правки в localStorage — тоже). Кэш позволяет поднять ту же сессию после
    перезапуска сервера или перезагрузки страницы (см. ручку /api/reopen), не
    заставляя пользователя заново перетаскивать файлы."""
    if not _cache_ok(sha):
        return
    try:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        os.chmod(CACHE_DIR, 0o700)
        path = os.path.join(CACHE_DIR, sha + ".pdf")
        if os.path.exists(path):
            os.chmod(path, 0o600)
            os.utime(path, None)
        else:
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(data)
            os.replace(tmp, path)      # читатель видит только целый файл
        _cache_trim()
    except OSError:
        pass                            # кэш — удобство, ошибка диска не повод падать


def cache_get(sha):
    if not _cache_ok(sha):
        return None
    path = os.path.join(CACHE_DIR, sha + ".pdf")
    try:
        with open(path, "rb") as f:
            data = f.read()
        os.utime(path, None)
        return data
    except OSError:
        return None


def store_doc(data, name):
    """Сохранить PDF в память под новым id и подчистить старьё (иначе DOCS/_doc_fonts текут)."""
    doc_id = uuid.uuid4().hex[:12]
    with LOCK:
        DOCS[doc_id] = {"bytes": bytearray(data), "name": name}
        _evict_locked()
    return doc_id


def store_docs(items):
    """Атомарно сохранить пачку PDF: кладём все под LOCK и вытесняем ОДИН раз в
    конце, защитив свеже-созданные id. Так id из ответа гарантированно живы к
    моменту получения клиентом (см. _evict_locked про >96 частей)."""
    ids = []
    with LOCK:
        for data, name in items:
            doc_id = uuid.uuid4().hex[:12]
            DOCS[doc_id] = {"bytes": bytearray(data), "name": name}
            ids.append(doc_id)
        _evict_locked(protect=set(ids))
    return ids


def prep_doc(spec, problems=None):
    """Документ из запроса: либо голый id, либо {"doc": id, "edits": [...]}.

    Склейка/разбор работают по ТЕКУЩЕМУ состоянию — с наложенными правками,
    иначе пользователь склеил бы исходники и потерял всё, что наменял."""
    if isinstance(spec, dict):
        doc_id, edits = spec.get("doc"), spec.get("edits")
    else:
        doc_id, edits = spec, None
    doc = open_doc(doc_id)
    if edits:
        bad = apply_edits(doc, doc_id, edits)
        if bad and problems is not None:
            problems.extend(bad)
    return doc


def doc_name(spec, fallback="bilet.pdf"):
    doc_id = spec.get("doc") if isinstance(spec, dict) else spec
    nm = (isinstance(spec, dict) and spec.get("name")) or \
        DOCS.get(doc_id, {}).get("name") or fallback
    return re.sub(r"\.pdf$", "", str(nm), flags=re.I) or "bilet"


def _file_name_safe(raw, default):
    """Из любой строки браузера — безопасное имя файла: разделители пути и
    прочие неживущие в имени файла символы долой, пусто → default."""
    nm = re.sub(r"[/\\:]+", "-", str(raw or "").strip())
    nm = "".join(ch for ch in nm if ch.isprintable() and ch not in '"<>*?|')
    nm = re.sub(r"\s+", " ", nm).strip(" .-")
    return nm or default


def save_as_name(spec):
    """Имя файла для скачивания: сначала фамилия зрителя из панели (save_as),
    потом имя исходника. save_as приходит из браузера, поэтому чистится через
    _file_name_safe."""
    nm = _file_name_safe(spec.get("save_as") if isinstance(spec, dict) else "", None)
    if nm and not nm.lower().endswith(".pdf"):
        nm += ".pdf"
    return nm or doc_name(spec) + ".pdf"


def split_ranges(pages, req):
    """Границы частей (1-based, включительно) по режиму разбора."""
    mode = req.get("mode", "each")
    if mode == "range":
        a = max(1, min(int(req.get("from", 1)), pages))
        b = max(a, min(int(req.get("to", pages)), pages))
        return [(a, b)]
    if mode == "chunk":
        n = max(1, int(req.get("n", 1)))
        return [(a, min(a + n - 1, pages)) for a in range(1, pages + 1, n)]
    return [(a, a) for a in range(1, pages + 1)]


def one_part(doc, a, b):
    out = fitz.open()
    out.insert_pdf(doc, from_page=a - 1, to_page=b - 1)
    buf = io.BytesIO()
    out.save(buf, garbage=3, deflate=True)
    out.close()
    return buf.getvalue()


def _zip_name(taken, name):
    """Уникальное имя внутри ZIP: у билетов имена часто совпадают."""
    base = re.sub(r"[/\\:]+", "-", name).strip() or "bilet"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    if base not in taken:
        taken.add(base)
        return base
    stem = base[:-4]
    i = 2
    while f"{stem} ({i}).pdf" in taken:
        i += 1
    taken.add(f"{stem} ({i}).pdf")
    return f"{stem} ({i}).pdf"


def zip_bytes(items):
    """items: [(имя, байты)] → один ZIP."""
    buf = io.BytesIO()
    taken = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, data in items:
            z.writestr(_zip_name(taken, name), data)
    return buf.getvalue()


def _pick_line(cand, e):
    """Из строк с одним и тем же bbox — та, к которой относится правка.

    При тексте поверх текста перекрытая и видимая строки стоят ровно в одном
    bbox (билет 1977139276: «18» под «12» в клетке ряда). Кто из них первый в
    списке — дело порядка обхода, и правка ряда молча уходила к «18»: сверка с
    orig объявляла её «перенесённой с другого билета», а планировщик стирал
    невидимую копию и рисовал значение поверх живой. Поэтому берём ту, чей текст
    совпал с запомненным панелью orig, иначе — видимую (не перекрытую)."""
    if len(cand) < 2:
        return cand[0] if cand else None
    orig = e.get("orig")
    if orig is not None:
        for l in cand:
            if norm(l["text"]).strip() == norm(orig).strip():
                return l
    for l in cand:
        if not l.get("stale"):
            return l
    return cand[0]


def _same_box(l, box):
    return all(abs(l["bbox"][k] - box[k]) < 1.2 for k in range(4))


def _same_cell(a, box):
    """Сегмент a стоит в той же клетке, что bbox правки — то есть напечатан
    поверх значения, а не рядом с ним.

    По высоте он должен занимать ту же строку: перекрытие ≥60% и своей высоты, и
    высоты клетки. Так отсекаются и подпись «РЯД» вплотную под крупной цифрой, и
    мелкий шрифт бланка, проходящий через клетку насквозь, и мелкая пометка,
    попавшая в выносной элемент высокого бокса значения.
    По ширине — либо сегмент целиком в клетке (≥60% своей ширины: лишняя «6»,
    напечатанная поверх адреса «gg226666@gmail.com»), либо он накрывает саму
    клетку и сопоставим с ней по ширине (≥60% ширины клетки и не шире её в 2.5
    раза: «14» поверх «2»). Длинная строка того же кегля, проходящая через
    клетку, ни туда, ни туда не попадает — это сосед."""
    dy = min(a[3], box[3]) - max(a[1], box[1])
    dx = min(a[2], box[2]) - max(a[0], box[0])
    wa, wb = a[2] - a[0], box[2] - box[0]
    return (dy >= 0.6 * max(a[3] - a[1], 0.1) and dy >= 0.6 * max(box[3] - box[1], 0.1)
            and (dx >= 0.6 * max(wa, 0.1)
                 or (dx >= 0.6 * max(wb, 0.1) and max(wa, wb) <= 2.5 * min(wa, wb) + 4)))


def _stale_under(pairs, pick, box):
    """Полосы стирания чужих копий значения, напечатанных в клетке правки.

    Перекрытая копия не обязана стоять ровно в том же bbox, что видимая, и по
    _same_box в кандидаты не попадала: в билете, правленном прошлой версией
    редактора, под «3000 ₽» лежит «800 ₽» — цифры уехали под новое значение, а
    старый знак рубля торчал рядом с новым, и после следующей правки в клетке
    читалось «6333 ₽₽». Поэтому берём любую stale-строку, вписанную в клетку
    почти целиком (80% площади): её пиксели и так перекрыты, а видимое значение
    при непустых stale_rects перерисовывается целиком.

    И любой сегмент, занимающий ту же клетку (_same_cell), даже не помеченный
    stale: в «Билеты_партер_правая_2ряд…» в клетке места нарисованы и «14», и
    «2» — обе видимые, на одной базовой линии, и `_mark_stale` такую пару не
    метит, если такой же текст встречается где-то ещё на странице. Стирание
    одной оставляло от другой торчащий вбок хвост: после правки места на «5» в
    клетке читалось «54». Стираем клетку целиком — остаётся одно значение."""
    z = fitz.Rect(box) + (-1.5, -1.5, 1.5, 1.5)
    out = []
    for i, s in pairs:
        if i is pick:
            continue
        r = fitz.Rect(i["bbox"])
        if r.get_area() <= 0:
            continue
        if (i.get("stale") and (r & z).get_area() >= 0.8 * r.get_area()) \
                or _same_cell(i["bbox"], box):
            out += _erase_bands(seg_chars(s))
    return out


def match_line(orig_lines, e):
    bb = e.get("bbox")
    if not bb:
        return None
    box = [float(v) for v in bb]
    return _pick_line([l for l in orig_lines if _same_box(l, box)], e)


SBER_PATS = [
    r"кеш\s?бэк", r"кэш\s?бэк", r"cash\s?back",
    r"сбер\s?спасибо", r"спасибо от сбер",
    r"сбер\s?прайм", r"сберпрайм",
    r"бонус\w*\s+сбер", r"сбер\w*\s+бонус",
]
SBER_LABELS = [r"скидки и акции"]
MOSRU_PATS = [
    r"управляйте своим заказом",
    r"^скачайте билет",
    r"для этого отсканируйте",
    r"пин-код вашего билета",
    r"bilet\.mos\.ru",
]

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Цели правки — только те поля, что реально меняют на билете. Дата и номер
# заказа НЕ помечаются (их можно поправить кликом по строке): раньше DATE_RE
# ловил телефоны «+7 495…», а «заказ» — куски HTML, засоряя панель полей.
FIELD_LABELS = [
    (r"^(ряд|ряд\s*/\s*row|row)\b", "row"),
    (r"^(мест[оаеум]*|мест[оа]\s*/\s*seat|seat|кресло)", "seat"),
    (r"^(сектор|сектор\s*/\s*sector|sector|зона|ярус|уровень|вход|подъезд)", "sector"),
    (r"^(посетитель|зритель|билетодержатель|владелец|покупатель|гость|фио|ф\.?\s*и\.?\s*о|билет\s+на\s+имя|на\s+имя)", "name"),
    (r"^(стоимость( билета)?|цена|номинал|тариф|сумма|к оплате|итого)\b", "price"),
    (r"^(почта|электронн\w* почт\w*|e-?mail)", "email"),
]
# Зона/сектор как ЗНАЧЕНИЕ. Слова целиком (\b и закрытые окончания), иначе
# «правоотношениях» ловилось как «прав…» и вся строка метилась сектором.
ZONE_RE = re.compile(
    r"^(партер|балкон|ложа|бельэтаж|бенуар|ярус|амфитеатр|трибуна|сектор|"
    r"лев(ый|ая|ое|ые)|прав(ый|ая|ое|ые)|центральн(ый|ая|ое|ые))\b", re.I)
# «Левый вход», «Правый подъезд», «Центральное фойе» — это НЕ сектор зала, а
# навигация по зданию, хотя ZONE_RE и ловит их за «лев/прав/центральн».
NOT_ZONE_RE = re.compile(r"\b(вход|входа|входе|подъезд\w*|выход\w*|фойе|гардероб\w*|"
                         r"буфет\w*|туалет\w*|лестниц\w*|крыл[оь]\w*)\b", re.I)
# Валюта как ЗНАЧЕНИЕ: закрытые окончания у «руб» (иначе адрес «Рублёвское шоссе,
# д. 5» — цена), «р»/«р.» — только сразу за числом (иначе «р. Волга 3» и «р/с 4070…»).
# Перед «руб» цифра допустима — на билетах пишут «41581.60руб.».
PRICE_RE = re.compile(r"₽|(?<![а-яёa-z])руб(?:\.|\b|л(?:ь|я|ей|ями|[её]м))|\brub\b|"
                      r"\d\s*р\.?(?:\s|$)", re.I)
# Дата, разобранная на отдельные сегменты («26 | 08 | 2026»): такие числа нельзя
# раздавать под ряд/место. Признак — ОТДЕЛЬНЫЙ сегмент-год или сегмент-месяц в той
# же строке; целая дата «25.8.2026» в соседней колонке признаком не считается.
DAY_MONTH_RE = re.compile(r"^(0?[1-9]|[12]\d|3[01])$")
YEAR_RE = re.compile(r"(19|20)\d{2}")
MONTH_RE = re.compile(r"(январ|феврал|март|апрел|ма[йя]|июн|июл|август|"
                      r"сентябр|октябр|ноябр|декабр)[а-яё]*\.?", re.I)


NAME_STOP_WORDS_RE = re.compile(
    r"\b(пушкинск\w*|карт\w*|билет\w*|театр\w*|партер|балкон|ложа|бельэтаж|ярус|амфитеатр|"
    r"сектор\w*|ряд\w*|мест\w*|заказ\w*|руб\w*|копейк\w*|инн|кпп|офд|чек|смен\w*|"
    r"зал\w*|вход\w*|подъезд\w*|этаж\w*|мероприят\w*|спектакл\w*|концерт\w*)\b", re.I
)


def _token_is_name_part(w):
    subparts = w.split("-")
    for sp in subparts:
        if not sp:
            return False
        if sp.lower() in {"оглы", "кызы", "де", "фон", "ван", "эль"}:
            continue
        if not (re.fullmatch(r"[А-ЯЁA-Z][а-яёa-z]{1,}", sp) or re.fullmatch(r"[А-ЯЁA-Z]{2,}", sp)):
            return False
    return True


def is_name(text):
    s = text.strip()
    if re.search(r"[:@\d«»\"'()<>{}\[\]/\\|*+=~_!?,;]", s):
        return False
    if NAME_STOP_WORDS_RE.search(s):
        return False
    toks = s.split()
    if not 2 <= len(toks) <= 4:
        return False
    return all(_token_is_name_part(w) for w in toks)


def name_value_ok(text):
    """Кандидат в значение метки «Посетитель/ФИО»: 1–4 слова (в любом регистре),
    без цифр/двоеточий/@ — чтобы под метку не попало «Действителен: с … по …»."""
    s = text.strip()
    if re.search(r"[:@\d]", s):
        return False
    toks = s.split()
    return 1 <= len(toks) <= 4 and all(re.match(r"[А-Яа-яЁёA-Za-z]", w) for w in toks)


def zone_value_ok(text):
    """Значение-зона: название сектора («Партер», «Балкон левый»), но не навигация
    по зданию («Левый вход» — ZONE_RE ловит его за «лев…», сектором это не является)."""
    s = text.strip()
    return bool(ZONE_RE.match(s)) and not NOT_ZONE_RE.search(s)


def sector_value_ok(text):
    """Значение сектора: название зоны или КОРОТКИЙ идентификатор («A», «12», «II»,
    «2 этаж»). Свободный текст сектором не считаем — иначе «Зона отдыха на 2 этаже»
    и «Уровень сервиса премиум» метились сектором."""
    s = text.strip().strip(":.№/-").strip()
    if not s or NOT_ZONE_RE.search(s):
        return False
    if ZONE_RE.match(s):
        return True
    toks = s.split()
    return (len(toks) == 1 and len(s) <= 12) or (len(toks) <= 3 and len(s) <= 12)


def field_value_ok(tag, text):
    """Значение кандидата подходит метке. Один канон для вертикальной связки
    (значение ПОД меткой) и горизонтальной (значение СПРАВА в той же строке)."""
    t = (text or "").lower().strip()
    if tag == "row":
        return bool(re.fullmatch(r"[\dа-яёa-z]{1,3}[+]?", t, re.I))
    if tag == "seat":
        return bool(re.fullmatch(r"[\dа-яёa-z]{1,4}([,\s]+\d{1,4})*", t, re.I))
    if tag == "price":
        # «ИНН 7710002492» в той же строке, что «Стоимость», — не цена.
        # Число само по себе или с валютой; чужой код с буквами — нет.
        return bool(re.search(r"\d", t)) and bool(
            PRICE_RE.search(t) or re.fullmatch(r"\d[\d\s.,]*", t))
    if tag == "name":
        return name_value_ok(text)
    if tag == "sector":
        return sector_value_ok(text)
    if tag == "email":
        return bool(EMAIL_RE.search(text))
    return False


def field_tags(lines):
    if not lines:
        return lines
    tags = [set() for _ in lines]

    def label_tag(text):
        t = text.lower().strip()
        if re.search(r"\d", t):
            return None
        for pat, tag in FIELD_LABELS:
            if re.search(pat, t):
                return tag
        return None

    for i, l in enumerate(lines):
        # строка, накрытая более поздним текстом на том же месте, на странице не
        # видна: метить по ней поле — значит показать в панели чужое значение
        if l.get("stale"):
            continue
        t = l["text"].lower().strip()
        if EMAIL_RE.search(l["text"]) and not re.search(r"@maly\.ru\b", l["text"], re.I):
            tags[i].add("email")
        if PRICE_RE.search(t) and re.search(r"\d", t) and len(t) < 30:
            tags[i].add("price")
        # «Сектор: Партер», «Зона A», «Ярус 2» — метка и значение в одной строке.
        # Голая метка («Сектор:», «Сектор/Sector») НЕ помечается: значение под ней
        # найдёт связка метка→значение ниже, иначе получится два тега на одно поле.
        m = re.match(r"^(?:сектор|зона|ярус|уровень)\b[\s:.№/-]*"
                     r"(?:sector|zone|level|tier)?\b[\s:.№/-]*(.*)$", t)
        if m and re.search(r"[a-zа-яё0-9]", m.group(1)) and sector_value_ok(m.group(1)):
            tags[i].add("sector")
        # Сектор / зона в начале строки (в т.ч. перед рядом: «Партер, ряд 9, место 10»,
        # «Ложа 1-го яруса правая №1, ряд 1, место 3»)
        if zone_value_ok(l["text"]) and not re.match(r"^(?:ряд|row|мест|seat)\b", t):
            tags[i].add("sector")
        # «Ряд 6 Место 5» в одной строке (рус/eng), а также ряд/место после сектора
        has_row = bool(re.search(r"\b(?:ряд|row)\b\s*[:.№/-]*\s*\d+", t))
        has_seat = bool(re.search(r"\b(?:мест[оаеум]*|seat|кресло)\b\s*[:.№/-]*\s*\d+", t))
        if has_row and has_seat:
            tags[i].update(("row", "seat"))
        elif has_row and re.match(r"^(?:ряд|row)\b", t):
            tags[i].add("row")
        elif has_seat and re.match(r"^(?:мест[оа]|seat|кресло)\b", t):
            tags[i].add("seat")
        elif re.match(r"^(ряд|row)\b\D*\S+\s+(мест|seat)", t) and re.search(r"\d", t):
            tags[i].update(("row", "seat"))

    for i, l in enumerate(lines):
        if l.get("stale"):
            continue
        tag = label_tag(l["text"])
        if not tag:
            continue
        best = None       # значение ПОД меткой (классическая билетная колонка)
        best_h = None     # значение СПРАВА в той же строке (МХТ/Афиша: «Зритель»…)
        ly = (l["bbox"][1] + l["bbox"][3]) / 2
        lh = max(l["bbox"][3] - l["bbox"][1], 1.0)
        for j, v in enumerate(lines):
            if j == i or tags[j] or v.get("stale"):
                continue
            vy0 = v["bbox"][1]
            if vy0 >= l["bbox"][3] - 1 and vy0 - l["bbox"][3] < 26:
                lx0, lx1 = l["bbox"][0], l["bbox"][2]
                vx0, vx1 = v["bbox"][0], v["bbox"][2]
                ox = min(lx1, vx1) - max(lx0, vx0)
                # значение либо перекрывается по X с меткой, либо стоит
                # по центру под ней (узкая цифра под широкой меткой «Ряд/Место»
                # в билетных колонках — bbox не пересекаются, но центры совпадают)
                cdiff = abs((lx0 + lx1) / 2 - (vx0 + vx1) / 2)
                aligned = ox > 2 or cdiff < min((lx1 - lx0) / 2 + 10, 30)
                if aligned and (best is None or vy0 < best[0]):
                    best = (vy0, j)
            # Горизонталь: Afisha-Tickets (МХТ) кладёт ФИО в соседнюю клетку той же
            # строки — «Зритель» слева мелким, «Мухин Егор» справа/по центру. Снизу
            # под меткой пусто, вертикальная связка молчит. Берём ближайшее
            # перекрывающееся по Y значение правее метки.
            vh = max(v["bbox"][3] - v["bbox"][1], 1.0)
            vcy = (v["bbox"][1] + v["bbox"][3]) / 2
            overlap = min(l["bbox"][3], v["bbox"][3]) - max(l["bbox"][1], v["bbox"][1])
            if (v["bbox"][0] >= l["bbox"][2] - 4
                    and overlap > 0
                    and abs(vcy - ly) <= max(3.0, 0.45 * max(lh, vh))):
                if best_h is None or v["bbox"][0] < best_h[0]:
                    best_h = (v["bbox"][0], j)

        def _under_label(v):
            """v стоит ПОД меткой l в той же колонке (классическая связка)."""
            if v.get("stale") or not (v["bbox"][1] >= l["bbox"][3] - 1
                                      and v["bbox"][1] - l["bbox"][3] < 26):
                return False
            lx0, lx1 = l["bbox"][0], l["bbox"][2]
            vx0, vx1 = v["bbox"][0], v["bbox"][2]
            ox = min(lx1, vx1) - max(lx0, vx0)
            cdiff = abs((lx0 + lx1) / 2 - (vx0 + vx1) / 2)
            return ox > 2 or cdiff < min((lx1 - lx0) / 2 + 10, 30)

        # Сначала классика «метка → значение под ней». Горизонталь — запасной путь
        # (Afisha/МХТ), и ТОЛЬКО если под меткой ещё нет значения с этим же тегом:
        # PRICE_RE мог уже снять «3000 ₽» под «Стоимость», а в той же строке
        # справа лежит «ИНН 7710002492» — это не цена.
        already = any(_under_label(lines[j]) and tag in tags[j]
                      for j in range(len(lines)) if j != i)
        candidates = []
        if best:
            candidates.append(best[1])
        if best_h is not None and best_h[1] not in candidates and not already:
            candidates.append(best_h[1])
        for j in candidates:
            if field_value_ok(tag, lines[j]["text"]):
                tags[j].add(tag)
                break

    # Одна визуальная строка: жёсткая корзина round(y0/3) разрывала ряд и место,
    # если базовые линии расходятся на 1–2pt и попадают по разные стороны кратного 3.
    # Кластеризуем с допуском от кегля, анкер — первая строка кластера.
    free = sorted((i for i in range(len(lines))
                   if not tags[i] and not lines[i].get("stale")),
                  key=lambda i: lines[i]["bbox"][1])
    row_groups = []
    for i in free:
        y0 = lines[i]["bbox"][1]
        tol = max(1.6, 0.3 * (lines[i]["size"] or 10))
        if row_groups and y0 - lines[row_groups[-1][0]]["bbox"][1] <= tol:
            row_groups[-1].append(i)
        else:
            row_groups.append([i])
    for idxs in row_groups:
        nums = [i for i in idxs if re.fullmatch(r"\d{1,3}", lines[i]["text"].strip())]
        # метка («Сектор/Sector») тоже попадает под ZONE_RE — её не считаем значением
        zones = [i for i in idxs if zone_value_ok(lines[i]["text"])
                 and not label_tag(lines[i]["text"])]
        # дата, разобранная на сегменты («26 | 08 | 2026»), выглядит как два числа в
        # одной строке — ряд/место из неё делать нельзя. Признак: в этой же строке
        # есть отдельный сегмент-год или сегмент-месяц (целая дата в соседней
        # колонке — «25.8.2026 19:00 Партер 23 3» — признаком НЕ является).
        datish = any(YEAR_RE.fullmatch(lines[i]["text"].strip())
                     or MONTH_RE.fullmatch(lines[i]["text"].strip()) for i in idxs)
        if datish and all(DAY_MONTH_RE.match(lines[i]["text"].strip()) for i in nums):
            nums = []
        # ровно два числа = «ряд, место» (левее — ряд). Три и больше без меток
        # неоднозначны («сектор ряд место» → ряд уехал бы в сектор): не гадаем,
        # такую строку всё равно можно править кликом
        if len(nums) == 2:
            nums.sort(key=lambda i: lines[i]["bbox"][0])
            tags[nums[0]].add("row")
            tags[nums[1]].add("seat")
        for i in zones:
            tags[i].add("sector")

    # ФИО-эвристика: 2–3 слова с заглавных, НО не крупнее основного текста
    # страницы в 1.5 раза — иначе в «зрители» попадают заголовки событий
    # («Солнце Ландау» sz22 при медиане 7) и названия театров (sz11).
    sizes = sorted(l["size"] for l in lines)
    med = sizes[len(sizes) // 2] if sizes else 10.0
    for i, l in enumerate(lines):
        if not tags[i] and not l.get("stale") and l["size"] <= med * 1.5:
            if is_name(l["text"]):
                tags[i].add("name")
            else:
                m_name = re.match(r"^(?:билет\s+на\s+имя|на\s+имя)\s*[:.\-–]?\s*(.+)$", l["text"].strip(), re.I)
                if m_name and is_name(m_name.group(1)):
                    tags[i].add("name")

    for i, l in enumerate(lines):
        if tags[i]:
            l["tags"] = sorted(tags[i])
    return lines


def _removal_rects(page):
    """Что вырезать со страницы: сбер-кешбэк (каждая строка своим узким
    прямоугольником) и mos.ru-инструкции (по столбцу-кластеру). Возвращает список
    (src, rect, text), где src ∈ {"sber","mos"} — по источнику решаем, трогать ли
    вектор при редакте (см. autoclean_doc). Удаляем ТОЛЬКО текст поверх фона
    (add_redact_annot fill=False) — ничего не сдвигается, вертикальная подпись на
    кромке и вектор карточки остаются как в оригинале. Именно из-за сдвига
    show_pdf_page раньше разрезал «Электронный билет»."""
    lines = extract_lines(page)
    rects = []

    def header_floor(y0, top):
        # не заезжать в сохраняемый заголовок «Скидки и акции» над кешбэком
        for h in lines:
            hb = h["bbox"]
            if hb[3] <= top + 2 and any(re.search(p, h["text"], re.I) for p in SBER_LABELS):
                y0 = max(y0, hb[3] + 0.3)
        return y0

    for l in lines:
        if any(re.search(p, l["text"], re.I) for p in SBER_PATS):
            b = l["bbox"]
            y0 = header_floor(b[1] - 0.3, b[1])
            rects.append(("sber", fitz.Rect(b[0] - 0.5, y0, b[2] + 0.5, b[3] + 0.5),
                          l["text"][:60]))

    # mos.ru: совпавшие строки бьём на кластеры по вертикальному разрыву (>36pt),
    # каждый кластер — свой охватывающий прямоугольник, чтобы «bilet.mos.ru» в
    # подвале не растянул вырезку на пол-страницы.
    mos = sorted((l for l in lines if any(re.search(p, l["text"], re.I) for p in MOSRU_PATS)),
                 key=lambda l: l["bbox"][1])
    cluster = []
    for l in mos + [None]:
        if cluster and (l is None or l["bbox"][1] - cluster[-1]["bbox"][3] > 36):
            x0 = min(m["bbox"][0] for m in cluster); x1 = max(m["bbox"][2] for m in cluster)
            y0 = min(m["bbox"][1] for m in cluster); y1 = max(m["bbox"][3] for m in cluster)
            rects.append(("mos", fitz.Rect(x0 - 2, y0 - 2, x1 + 2, y1 + 2),
                          "; ".join(m["text"][:30] for m in cluster)))
            cluster = []
        if l is not None:
            cluster.append(l)
    return rects


def _qr_stats(doc, xref):
    """(dark_ratio, mean_sat) картинки: ЧБ QR → sat≈0, dark≈0.5; цветная иконка →
    sat высокая. Возвращает (None, None), если картинку не удалось раскодировать."""
    try:
        pm = fitz.Pixmap(doc, xref)
        if pm.alpha:
            pm = fitz.Pixmap(pm, 0)
        # n>=3 бывает и у CMYK (n=4): там первые три байта — C,M,Y, и «чёрный»
        # QR (0,0,0,255) читался бы как белый, а картинка — как насыщенная.
        # Приводим к RGB всё, что не RGB, а не только серое.
        rgb = pm if pm.colorspace and pm.colorspace.n == 3 else fitz.Pixmap(fitz.csRGB, pm)
    except Exception:
        return None, None
    data = rgb.samples
    step = rgb.n
    n = (rgb.width * rgb.height) or 1
    dark = sat = 0
    for i in range(0, len(data), step):
        r, g, b = data[i], data[i + 1], data[i + 2]
        if 0.299 * r + 0.587 * g + 0.114 * b < 128:
            dark += 1
        sat += max(r, g, b) - min(r, g, b)
    return dark / n, sat / n


def _clip_dark(page, r, z=4, thr=160):
    """Доля тёмных пикселей в клипе, отрендеренном СО СТРАНИЦЫ (не из XObject) —
    т.е. что реально ВИДНО с учётом перекрытий/z-порядка."""
    pm = page.get_pixmap(clip=r, matrix=fitz.Matrix(z, z), alpha=False)
    rgb = pm if pm.n >= 3 else fitz.Pixmap(fitz.csRGB, pm)
    data = rgb.samples
    step = rgb.n
    n = (rgb.width * rgb.height) or 1
    dark = 0
    for i in range(0, len(data), step):
        if 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2] < thr:
            dark += 1
    return dark / n


def _main_code(page, exclude=frozenset()):
    """Есть ли на странице «главный» код — входной QR (крупный квадрат) или штрихкод.
    Он кодирует МЕСТО и трогать его нельзя, поэтому он же служит страховкой: если,
    кроме найденного «промо-QR», других кодов на странице нет, значит найденный код
    и есть единственный — удалять его запрещено. По корпусу (756 файлов, 180 страниц
    с промо-QR) главный код рядом нашёлся в 100 % случаев, так что гейт бесплатный."""
    W, H = page.rect.width, page.rect.height
    doc = page.parent
    for im in page.get_image_info(xrefs=True):
        x = im.get("xref")
        if not x or x in exclude:
            continue
        r = fitz.Rect(im["bbox"])
        w, h = r.width, r.height
        mn, mx = min(w, h), max(w, h)
        if mn < 12 or (w > 0.8 * W and h > 0.6 * H):     # фон во всю страницу — не код
            continue
        square_qr = abs(w - h) <= 0.28 * mx and mn >= 60
        barcode = mx >= 80 and mx / max(mn, 1) >= 2.5
        if not (square_qr or barcode):
            continue
        dark, sat = _qr_stats(doc, x)
        if dark is None or sat > 40 or not (0.10 < dark < 0.80):
            continue
        return True
    return False


def _promo_qr_geom(page):
    """Промо-QR «Управляйте заказом» БЕЗ опоры на извлекаемый текст (частый вариант
    mos.ru, где весь блок запечён в фон-картинку и get_text его не видит). Ищем
    маленький ЧБ квадрат в нижней-левой четверти страницы, с QR-плотностью краски
    (~0.5). Отсекается тройной страховкой:
      • входной QR (кодирует МЕСТО, трогать нельзя): крупный (~126pt) → срезается
        порогом размера, плюс он справа-сверху (позиция);
      • цветная иконка «возьмите документ»: высокая насыщенность → срезается по sat;
      • фон во всю страницу: по размеру.
    ВАЖНО: get_image_info/Pixmap показывают пиксели самого XObject независимо от того,
    виден ли он на странице (перекрыт фоном, не отрисован). Поэтому финальный фильтр —
    рендер клипа СО СТРАНИЦЫ: берём картинку, только если она реально видна с
    QR-плотностью. Иначе на «чистых» билетах ловили невидимый placeholder и калечили
    редактом соседний текст.
    Возвращает [(xref, rect), …]."""
    W, H = page.rect.width, page.rect.height
    doc = page.parent
    out = []
    for im in page.get_image_info(xrefs=True):
        x = im.get("xref")
        if not x:
            continue
        r = fitz.Rect(im["bbox"])
        w, h = r.width, r.height
        mn = min(w, h)
        if mn < 20 or mn > 75:                     # не входной QR (126), не тонкая полоса
            continue
        if abs(w - h) > 0.28 * max(w, h):          # квадрат
            continue
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        if cx > 0.35 * W or cy < 0.5 * H:          # только нижняя-левая четверть
            continue
        dark, sat = _qr_stats(doc, x)
        if dark is None or sat > 25:               # цветная иконка — не трогаем
            continue
        if not (0.25 < dark < 0.65):               # ЧБ, но не QR-плотность (в XObject)
            continue
        if not (0.35 < _clip_dark(page, r) < 0.70):  # и реально ВИДЕН как QR на странице
            continue
        out.append((x, r))
    # последняя страховка: единственный код на странице удалять нельзя — вход по нему
    if out and not _main_code(page, exclude={x for x, _ in out}):
        return []
    return out


def _orphan_blue_lines(page, qr_rect):
    """Осиротевшие синие подчёркивания-ссылки промо-блока (в варианте без текста
    редакт их не снимает — текста-то нет). Берём mos.ru-синий штрих (низкий R, высокий
    B) в y-полосе самого QR — НО только «осиротевший»: без своего текста над линией.
    Легитимное подчёркивание ссылки (url возврата, satire.ru и т.п.) имеет прямо над
    собой текст-ссылку — такое НЕ трогаем, иначе на билетах с другим отступом «Правил»
    снесли бы реальную ссылку. Возвращает прямоугольники под редакт (graphics-only)."""
    band0, band1 = qr_rect.y0 - 14, qr_rect.y1 + 10
    spans = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b["lines"]:
            for s in l["spans"]:
                spans.append(fitz.Rect(s["bbox"]))
    rects = []
    for dr in page.get_drawings():
        c = dr.get("color")
        if not c or len(c) < 3:
            continue
        if not (c[0] < 0.30 and c[2] > 0.60 and c[1] > 0.35):
            continue
        rc = dr.get("rect")
        if not rc:
            continue
        if not (band0 <= (rc.y0 + rc.y1) / 2 <= band1):
            continue
        # есть ли над/на линии текст (значит это подчёркивание живой ссылки)?
        has_text = any(
            s.x1 > rc.x0 and s.x0 < rc.x1 and (rc.y0 - 12) <= s.y1 <= (rc.y1 + 3)
            for s in spans
        )
        if has_text:
            continue
        rects.append(fitz.Rect(rc.x0 - 1, rc.y0 - 1.5, rc.x1 + 1, rc.y1 + 1.5))
    return rects


# Категории ресурсов и операторы, по которым видно, что поток формы их ищет
FORM_RES_OPS = (
    ("ExtGState", re.compile(rb"/[^\s/\[\]<>(){}]+\s+gs(?![A-Za-z0-9])")),
    ("XObject", re.compile(rb"/[^\s/\[\]<>(){}]+\s+Do(?![A-Za-z0-9])")),
    ("Font", re.compile(rb"/[^\s/\[\]<>(){}]+\s+[-.\d]+\s+Tf(?![A-Za-z0-9])")),
    ("ColorSpace", re.compile(rb"/[^\s/\[\]<>(){}]+\s+(?:cs|CS)(?![A-Za-z0-9])")),
    ("Pattern", re.compile(rb"/[^\s/\[\]<>(){}]+\s+(?:scn|SCN)(?![A-Za-z0-9])")),
    ("Shading", re.compile(rb"/[^\s/\[\]<>(){}]+\s+sh(?![A-Za-z0-9])")),
    ("Properties", re.compile(rb"/[^\s/\[\]<>(){}]+\s+(?:BDC|DP)(?![A-Za-z0-9])")),
)


def _res_get(doc, xref, path, cat):
    """Есть ли у объекта категория ресурсов cat по пути path (через ссылку тоже)."""
    try:
        k, v = doc.xref_get_key(xref, path)
        if k == "xref":
            return doc.xref_get_key(int(v.split()[0]), cat)[0] != "null", int(v.split()[0]), ""
        if k == "dict":
            return doc.xref_get_key(xref, path + "/" + cat)[0] != "null", xref, path + "/"
    except Exception:
        pass
    return False, None, None


def _pin_form_res(page):
    """Проставить формам (Form XObject) их собственные /Resources ДО редакта.

    По стандарту форма может /Resources не иметь — имена тогда ищутся в ресурсах
    страницы. Фильтр apply_redactions так не умеет: он разбирает поток формы с
    пустым словарём, спотыкается на «/GS2 gs» (в логе «cannot find ExtGState
    resource») и теряет часть содержимого. На order_3175628817 от QR-кода
    оставалось 49 заливок из 225 — код становился нечитаемым, хотя правили цену в
    другом углу страницы. Явные ресурсы ссылаются на те же объекты, что видела
    форма через страницу, поэтому рендер от этого не меняется."""
    doc = page.parent
    tgt = _res_target(page)
    if tgt is None:
        return
    rx, pref = tgt
    seen = set()
    for _ in range(4):                     # форма в форме: пока находятся новые
        todo = [x for x, *_ in page.get_xobjects() if x not in seen]
        if not todo:
            return
        for x in todo:
            seen.add(x)
            try:
                st = doc.xref_stream(x)
            except Exception:
                continue
            own = doc.xref_get_key(x, "Resources")[0]
            add = {}
            for cat, op in FORM_RES_OPS:
                if not op.search(st):
                    continue
                if own != "null" and _res_get(doc, x, "Resources", cat)[0]:
                    continue                # своё есть — не трогаем
                k, v = doc.xref_get_key(rx, pref + cat)
                if k != "null":
                    add[cat] = v
            if not add:
                continue
            try:
                if own == "null":
                    doc.xref_set_key(x, "Resources", "<<%s>>" % "".join(
                        "/%s %s" % (c, v) for c, v in add.items()))
                else:
                    _ok, ox, opref = _res_get(doc, x, "Resources", next(iter(add)))
                    for c, v in add.items():
                        doc.xref_set_key(ox, opref + c, v)
            except Exception:
                continue


def _dashed(dr):
    """Штрих у чертежа непустой (пунктир), а не сплошная линия '[] 0'."""
    d = dr.get("dashes")
    return bool(d) and "[" in d and any(c.isdigit() for c in d.split("]")[0])


def _painted_xobjects(page):
    """Имена XObject, которые реально вызваны `/Name Do` в content-стримах.
    В ресурсах страницы остаются и мёртвые формы — их нельзя считать живыми."""
    names = set()
    doc = page.parent
    for xref in page.get_contents():
        try:
            s = doc.xref_stream(xref) or b""
        except Exception:
            continue
        for m in re.finditer(rb"(?<![A-Za-z0-9])/([A-Za-z0-9]+)\s+Do", s):
            names.add(m.group(1).decode())
    return names


def _ghost_forms(page, doc):
    """«Призрак»: page-level Form, поток — только `/X Do`, X — полноразмерная
    копия страницы, у формы маленький BBox + Matrix с ty≠0. Корректные вьюеры
    клипят по BBox; телефонные часто игнорируют клип → поверх билета ложится
    ВТОРАЯ страница и шапка «МОС:БИЛЕТ» дублируется (Лес / order_21022xxx).
    Считаем только реально нарисованные формы (см. _painted_xobjects)."""
    painted = _painted_xobjects(page)
    out = []
    ph = page.rect.height
    for xref, name, parent, bbox in page.get_xobjects():
        if parent != 0 or str(name) not in painted:
            continue
        try:
            obj = doc.xref_object(xref)
            stream = doc.xref_stream(xref) or b""
        except Exception:
            continue
        m = re.fullmatch(rb"/([A-Za-z0-9]+)\s+Do", stream.strip())
        if not m:
            continue
        nested = m.group(1).decode()
        mm = re.search(
            r"/Matrix\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+"
            r"([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s*\]", obj)
        if not mm:
            continue
        a, b, c, d, e, f = map(float, mm.groups())
        if abs(f) < 0.5:
            continue
        bm = re.search(
            r"/BBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s*\]",
            obj)
        if not bm:
            continue
        x0, y0, x1, y1 = map(float, bm.groups())
        if abs(y1 - y0) > 0.5 * ph:
            continue
        rm = re.search(r"/" + re.escape(nested) + r"\s+(\d+)\s+0\s+R", obj)
        if rm:
            try:
                nobj = doc.xref_object(int(rm.group(1)))
                nbm = re.search(
                    r"/BBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+"
                    r"([\d.\-]+)\s*\]", nobj)
                if nbm and abs(float(nbm.group(3)) - float(nbm.group(1))) < 0.5 * ph:
                    continue          # вложенная форма не полная — не призрак
            except Exception:
                pass
        # fitz: контент формы едет в page_y = form_y − f (f=-55.56 → +55.56)
        vis = fitz.Rect(x0 - e, y0 - f, x1 - e, y1 - f) & page.rect
        out.append((name, vis))
    return out


def _remove_do(page, name):
    doc = page.parent
    pat = re.compile(rb"(?<![A-Za-z0-9])/" + re.escape(name.encode()) + rb"\s+Do")
    n = 0
    for xref in page.get_contents():
        try:
            s = doc.xref_stream(xref)
        except Exception:
            continue
        if not s or ("/" + name + " Do").encode() not in s:
            continue
        new = pat.sub(b"", s)
        if new != s:
            doc.update_stream(xref, new)
            n += 1
    return n


def strip_ghost_forms(doc):
    """Запечь уникальные полосы «призрачных» Form-оверлеев и убрать их Do.

    Без клипа на телефоне оверлей даёт дубль шапки МОС:БИЛЕТ; просто удалить
    его нельзя — блок «Не забудьте» и пунктир живут ТОЛЬКО внутри формы.
    Запекаем РАСТРОМ (insert_image), не show_pdf_page: тот вкладывает полную
    страницу как Form, и на вьюере без клипа BBox дубль шапки возвращается.
    Порядок: diff с копией без Do → растровая полоса из первозданной страницы
    (MuPDF клипит по BBox) → удалить Do."""
    src = fitz.open()
    src.insert_pdf(doc)
    changed, notes = 0, []
    for pno in range(len(doc)):
        page = doc[pno]
        ghosts = _ghost_forms(page, doc)
        if not ghosts:
            continue
        nog = fitz.open()
        nog.insert_pdf(doc)
        for name, _v in ghosts:
            _remove_do(nog[pno], name)
        a = src[pno].get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
        b = nog[pno].get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
        nog.close()
        rows = []
        sa, sb = a.samples, b.samples
        W, H, n = a.width, a.height, a.n
        for y in range(H):
            base = y * W * n
            for x in range(0, W * n, n * 4):
                i = base + x
                if (abs(sa[i] - sb[i]) > 12 or abs(sa[i + 1] - sb[i + 1]) > 12
                        or abs(sa[i + 2] - sb[i + 2]) > 12):
                    rows.append(y)
                    break
        if not rows:
            for name, _v in ghosts:
                if _remove_do(page, name):
                    changed += 1
                    notes.append(f"p{pno} dropped {name} (no unique ink)")
            continue
        vis = fitz.Rect(0, max(0, rows[0] - 8), page.rect.width,
                        min(page.rect.height, rows[-1] + 9))
        for _name, fvis in ghosts:
            vis = vis | fvis
        vis = vis & page.rect
        if vis.is_empty or vis.height < 4:
            continue
        try:
            # 3x — читаемо на ретине; clip из src (там Do ещё есть, клип BBox)
            pix = src[pno].get_pixmap(matrix=fitz.Matrix(3, 3), clip=vis, alpha=False)
            page.insert_image(vis, pixmap=pix)
        except Exception as ex:
            notes.append(f"p{pno} bake fail {ex}")
            continue
        for name, _v in ghosts:
            if _remove_do(page, name):
                changed += 1
                notes.append(
                    f"p{pno} stripped {name} vis={vis} rows={rows[0]}-{rows[-1]}")
    src.close()
    return changed, notes


def sanitize_doc_bytes(data):
    """Открыть PDF, снять ghost-оверлеи; вернуть (bytes, n_stripped).
    Если призраков нет или санитайз упал — вернуть исходные байты."""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return data, 0
    try:
        n, _notes = strip_ghost_forms(doc)
        if not n:
            return data, 0
        buf = io.BytesIO()
        doc.save(buf, garbage=3, deflate=True)
        return buf.getvalue(), n
    except Exception:
        return data, 0
    finally:
        try:
            doc.close()
        except Exception:
            pass


# Зазор пунктир→«Правила» в чистой вёрстке mos.ru (замерено на эталоне
# order_20642735: dashed y=509, «Правила» y0=517.6).
REFLOW_GAP = 8.6


def _reflow_reminder(page, src, pno, promo_qr):
    """После сноса промо-QR «поднять» вёрстку mos.ru-билета: съехавший вверх из-за
    QR блок-напоминание («Не забудьте взять документ» + серая плашка + пунктир-
    разделитель) сдвинуть ВНИЗ так, чтобы пунктир встал вплотную (REFLOW_GAP) над
    заголовком «Правила…», как на чистом билете без QR. Полосу с блоком берём из
    ПЕРВОЗДАННОЙ страницы `src` и переклеиваем ниже через show_pdf_page (вектор/
    текст/картинка 1:1), старую полосу стираем, старую иконку убираем точечно.

    Строгий гейт (структура «плашка → пунктир → [QR] → Правила»): нет пунктира над
    QR / серой плашки над ним / текста «Правил» под QR — или сдвиг неправдоподобен —
    ничего не трогаем и возвращаем False."""
    if not promo_qr:
        return False
    if page.rotation:
        return False        # на повёрнутой странице геометрия полосы/вставки не наша
    W = page.rect.width
    qr_top = min(r.y0 for _x, r in promo_qr)
    qr_bot = max(r.y1 for _x, r in promo_qr)
    draw = page.get_drawings()
    # 1) широкий пунктир-разделитель ВЫШЕ QR
    y_dash = None
    for dr in draw:
        r = dr["rect"]
        if _dashed(dr) and r.width > 0.6 * W and r.height < 3 \
                and r.y1 < qr_top + 2 and r.y0 > qr_top - 90:
            yd = (r.y0 + r.y1) / 2
            if y_dash is None or yd > y_dash:
                y_dash = yd
    if y_dash is None:
        return False
    # 2) светлая серая плашка-карточка над пунктиром
    card_top = None
    for dr in draw:
        if dr.get("type") not in ("f", "fs"):
            continue
        r = dr["rect"]; fill = dr.get("fill")
        if fill and min(fill) > 0.88 and r.width > 40 \
                and r.y1 <= y_dash + 1 and r.y0 > y_dash - 70:
            card_top = r.y0 if card_top is None else min(card_top, r.y0)
    if card_top is None:
        return False
    # 3) первый текст НИЖЕ QR — заголовок «Правил»
    rules_top = None
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            y0 = ln["bbox"][1]
            if y0 > qr_bot - 1:
                rules_top = y0 if rules_top is None else min(rules_top, y0)
    if rules_top is None:
        return False
    delta = (rules_top - REFLOW_GAP) - y_dash
    if delta < 6 or delta > (qr_bot - y_dash) + 30:
        return False                          # закрывать нечего / сдвиг неправдоподобен
    # Полоса не должна залезать на QR: снизу её обрезает верх QR (пунктир выше него).
    # Иначе в переклеенную полосу попал бы кусок промо-QR из ПЕРВОЗДАННОЙ страницы —
    # и удалённый код проступил бы ниже.
    band = fitz.Rect(0, card_top - 5, W, min(y_dash + 3, qr_top - 1))
    if band.height < 8 or y_dash >= band.y1 - 0.5:
        return False        # полоса вырождена / пунктир не влез бы в неё целиком
    # страховка: крупный вектор (возможный фон-заливка) в полосе — не рискуем стирать
    for dr in draw:
        r = dr["rect"]
        if r.intersects(band) and (r.height > 120 or (r.width > 0.95 * W and r.height > 60)):
            return False
    tgt = band + (0, delta, 0, delta)
    # стереть старую полосу: вектор плашки/пунктира + текст напоминания (картинки — нет,
    # фон целый; иконку уберём отдельно)
    page.add_redact_annot(band, fill=False)
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,
        graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
        text=fitz.PDF_REDACT_TEXT_REMOVE,
    )
    for im in page.get_image_info(xrefs=True):   # маленькая иконка внутри старой полосы
        bb = im["bbox"]; w = bb[2] - bb[0]; h = bb[3] - bb[1]
        if w < 120 and h < 120 and band.y0 - 2 <= bb[1] and bb[3] <= band.y1 + 2:
            try:
                page.delete_image(im["xref"])
            except Exception:
                pass
    # С этого момента старая полоса уже стёрта. Если вставка сорвётся, блок-напоминание
    # просто исчезнет с билета — поэтому на любой сбой возвращаем полосу на место
    # (тем же show_pdf_page, но без сдвига) и честно докладываем неудачу.
    try:
        page.show_pdf_page(tgt, src, pno, clip=band)  # блок из первозданной — ниже
    except Exception:
        try:
            page.show_pdf_page(band, src, pno, clip=band)
        except Exception:
            pass
        raise
    return True


def autoclean_doc(doc):
    out = fitz.open()
    out.insert_pdf(doc)                       # копия: исходный PDF в памяти не трогаем
    report = []
    # «Пушкинская карта» (текст, QR/штрихкод блока, серый водяной знак) + пустое
    # имя зрителя — всегда, как на целевом билете Afisha/МХТ.
    try:
        report.extend(strip_pushkin_doc(out, clear_name=True))
    except Exception as e:
        report.append({"page": 0, "warn": [f"не удалось снять Пушкинскую карту: {e}"]})
    for pno in range(len(out)):
        page = out[pno]
        groups = _removal_rects(page)
        sber = [(r, t) for s, r, t in groups if s == "sber"]
        mos = [(r, t) for s, r, t in groups if s == "mos"]
        # Промо-QR ищем геометрией (ДО редакта, картинка ещё на месте): работает и
        # когда текст блока извлекается, и когда он запечён в фон (get_text пуст).
        promo_qr = _promo_qr_geom(page)
        if not (groups or promo_qr):
            continue
        removed = []
        _pin_form_res(page)
        # Сбер: как раньше — вектор карточки/«Скидки и акции» не трогаем.
        if sber:
            for rect, text in sber:
                page.add_redact_annot(rect, fill=False)
                removed.append(text)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
        # mos.ru-блок с извлекаемым текстом: сносим подпись и подчёркивания-ссылки под
        # ней (REMOVE_IF_TOUCHED снимает вектор, задетый рамкой; серый разделитель выше
        # рамки уцелевает).
        if mos:
            for rect, text in mos:
                page.add_redact_annot(rect, fill=False)
                removed.append(text)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
        # Осиротевшие синие подчёркивания в полосе промо-QR (вариант без текста — там
        # mos-проход их не трогал). Сносим ТОЛЬКО вектор (text=NONE!): линия может
        # налезать на соседний текст, а его стирать нельзя — так калечило «Правила».
        blue = []
        for x, r in promo_qr:
            blue += _orphan_blue_lines(page, r)
        if blue:
            for br in blue:
                page.add_redact_annot(br, fill=False)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                text=fitz.PDF_REDACT_TEXT_NONE,
            )
        for x in {x for x, _ in promo_qr}:    # сам QR — картинка, редакт её не берёт
            try:
                page.delete_image(x)
            except Exception:
                pass
        if promo_qr:
            removed.append("промо-QR «Управляйте заказом»")
        # «Поднять» вёрстку: сдвинуть блок-напоминание вниз на место QR (из pristine doc)
        warn = []
        try:
            if _reflow_reminder(page, doc, pno, promo_qr):
                removed.append("вёрстка поднята (сдвиг блока-напоминания)")
        except Exception as e:
            # молчать нельзя: полосу могли уже стереть, а вставка сорваться
            warn.append(f"не удалось поднять вёрстку: {e}")
        rec = {"page": pno, "removed": removed, "images": len({x for x, _ in promo_qr})}
        if warn:
            rec["warn"] = warn
        report.append(rec)
    # _reflow_reminder ждёт show_pdf_page → на странице появляется fzFrm0
    # (поток /fullpage Do, Matrix ty=-55.56, узкий BBox) — ровно тот же
    # «призрак», что и mos.ru Fm3. На телефоне без клипа Form BBox снова
    # двойной билет. Снимаем после reflow тем же strip_ghost_forms.
    try:
        n_gh, _notes = strip_ghost_forms(out)
        if n_gh:
            for rec in report:
                if rec.get("removed") and "вёрстка поднята" in " ".join(rec["removed"]):
                    rec["removed"].append(f"ghost-оверлей reflow снят ({n_gh})")
                    break
    except Exception as e:
        report.append({"page": 0, "warn": [f"не удалось снять ghost после reflow: {e}"]})
    return out, report


# Контрольный рендер стирания: масштаб и порог «это чернила, а не сглаживание».
# 8/255 — потолок шума по корпусу (717 файлов): перерисовка потока при редакте
# гуляет на ≤2, кромки глифов — до 8.
VIS_Z, VIS_INK = 3, 8


def _clip_shot(page, r):
    return page.get_pixmap(matrix=fitz.Matrix(VIS_Z, VIS_Z), clip=r, alpha=False).samples


def _ink_changed(a, b):
    """Изменилось ли в клипе хоть что-то, заметное глазу."""
    if len(a) != len(b):
        return True
    if a == b:
        return False
    return any(abs(x - y) > VIS_INK for x, y in zip(a, b))


def _erase_window(page, plan, e):
    """Окно проверки стирания: по x — то, что стираем, по y — вся строка. Редакт
    убирает глиф целиком, а полосы стирания задевают лишь середину рамки
    (см. _erase_bands), поэтому судить по высоте полос нельзя."""
    x0 = min(q.x0 for q in plan["rects"]); x1 = max(q.x1 for q in plan["rects"])
    y0 = min(q.y0 for q in plan["rects"]); y1 = max(q.y1 for q in plan["rects"])
    bb = [float(v) for v in (e.get("bbox") or ())]
    if len(bb) == 4:
        y0, y1 = min(y0, bb[1]), max(y1, bb[3])
    else:
        y0, y1 = y0 - plan["size"], y1 + 0.4 * plan["size"]
    return fitz.Rect(x0 - 1, y0 - 1, x1 + 1, y1 + 1) & page.rect


def _overlay_images(page):
    """Картинки, нарисованные ПОСЛЕ всего текста страницы: они прячут текст под
    собой. Афиша так и делает — ряд/место/имя/цена вклеены растром в конце потока,
    а текстовый слой под ними остался от шаблона с другими значениями
    (Afisha-Tickets-104883326: в тексте ряд «9», на картинке «3»). Фон-фотография,
    наоборот, идёт в начале потока, и текст поверх неё виден — её не берём."""
    try:
        cs = page.read_contents()
    except Exception:
        return []
    last_text = max((m.start() for m in
                     re.finditer(rb"(?<![A-Za-z0-9])(?:Tj|TJ|'|\")(?![A-Za-z0-9])", cs)),
                    default=-1)
    if last_text < 0:
        return []
    late = {m.group(1).decode("latin-1")
            for m in re.finditer(rb"/([^\s/\[\]<>(){}]+)\s+Do(?![A-Za-z0-9])", cs)
            if m.start() > last_text}
    if not late:
        return []
    want = {it[0] for it in page.get_images(full=True) if it[7] in late}
    out = []
    for inf in page.get_image_info(xrefs=True):
        r = fitz.Rect(inf["bbox"])
        if inf.get("xref") in want and not inf.get("has-mask") and min(r.width, r.height) > 6:
            out.append(r)
    return out


def mark_covered(page, lines):
    """Пометить строки, спрятанные под наклеенной поверх картинкой: панель должна
    сказать это заранее, а не после правки, которой не видно."""
    boxes = _overlay_images(page)
    for l in lines if boxes else ():
        if any(r.contains(fitz.Rect(l["bbox"])) for r in boxes):
            l["covered"] = True
    return lines


FIT_TOL = 0.97          # сжатие/уменьшение меньше 3% на глаз незаметно — не шумим


def _fit_why(f):
    """Предупреждение «значение не влезло в отведённое место», либо None.

    Оба способа рисования молча деформируют слишком длинное значение: родными
    глифами оно сжимается по горизонтали (Tz, вплоть до 35% ширины), цепочкой —
    уменьшается кегль шагами по 0.25pt. Место кончается не у края страницы, а там,
    где начинается сосед: хвост « руб.» с родным ₽, колонка «Место» правее «Ряд».
    Без предупреждения пользователь получает скукоженное или наехавшее на соседа
    значение и узнаёт об этом, только сверив готовый PDF."""
    if not f:
        return None
    near = f"({f['need']:.1f} pt против {f['avail']:.1f} свободных)"
    if f["how"] == "squeeze":
        if f["k"] > FIT_TOL:
            return None
        why = f"значение шире свободного места {near} — набор сжат до {round(f['k'] * 100)}%"
        if f["k"] <= 0.36:
            why += "; проверьте, не наехало ли на соседний текст"
        return why
    if not f["over"] and f["size"] / max(f["was"], 0.01) > FIT_TOL:
        return None
    why = (f"значение шире свободного места {near} — кегль уменьшен "
           f"с {f['was']:g} до {f['size']:g} pt")
    if f["over"]:
        why += "; оно всё равно не влезло и могло наехать на соседний текст"
    return why


# ₽ и кириллическая «Р» — один и тот же знак рубля (в билетных шрифтах ALSRubl*
# он закодирован «Р», и при перерисовке цепочкой честно заменяется на U+20BD),
# дефисы разной длины неотличимы на глаз, мягкий перенос невидим.
_FLAT_MAP = {ord("₽"): "Р", 0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-",
             0x2014: "-", 0x2212: "-", 0x00AD: ""}


def _flat(t):
    """Строка для сверки «что прочиталось» с «что задумано»: без пробелов и
    регистра — MuPDF рвёт слова по своим правилам, и пробел между «1» и «000» то
    есть, то нет, а вот лишняя цифра или потерянный символ так видны.

    Управляющие символы выброшены: у глифа без ToUnicode (например знак рубля,
    нарисованный прошлой версией редактора шрифтом без него) текст извлекается
    как \\x00 — на бумаге его нет, и требовать его в результате бессмысленно."""
    t = norm(t or "").translate(_FLAT_MAP)
    return re.sub(r"[\x00-\x1f\x7f]|\s+", "", t).lower()


def _band_chars(pairs, box, lo, hi):
    """Символы, физически стоящие в полосе правки: [(ключ позиции, символ, своя ли
    клетка)], слева направо.

    Считаем по СИМВОЛАМ, а не по сегментам: перерисовка меняет разбиение в обе
    стороны. «Цена: 7333 руб.» распадается на «Цена:», «7333», «руб.» — а в
    справке ФНС наоборот: отдельная «1» в 10 pt слева от значения после правки
    попала с ним в один сегмент, и полоса читалась как «154» вместо «54». Символ
    в полосе, если он пересекает её по высоте, а его центр по x лежит в [lo, hi] —
    у символа координата своя, поэтому склейка сегментов на неё не влияет.

    Ключ позиции (левый нижний угол с точностью 0.1 pt плюс сам символ) позволяет
    узнать чужой символ, просто оставшийся на месте: страницу мы не переливаем,
    поэтому у уцелевшего соседа координаты в точности прежние."""
    out = []
    for i, seg in pairs:
        own = _same_cell(i["bbox"], box)
        for c in seg_chars(seg):
            cb = c["bbox"]
            if cb[3] < box[1] + 1 or cb[1] > box[3] - 1:
                continue
            if lo <= (cb[0] + cb[2]) / 2 <= hi:
                out.append((cb[0], (round(cb[0], 1), round(cb[1], 1), c["c"]), c["c"], own))
    return [(k, c, own) for _x, k, c, own in sorted(out)]


def _chars(s):
    c = {}
    for ch in s:
        c[ch] = c.get(ch, 0) + 1
    return c


def _outside(lines, exempt):
    """Строки целиком вне зон правки (bbox правок + полосы стирания). Только их
    содержимое обязано уцелеть; сами правленые строки, естественно, меняются."""
    out = []
    for l in lines:
        r = fitz.Rect(l["bbox"])
        if not any((r & z).get_area() >= 0.5 * max(r.get_area(), 1e-6) for z in exempt):
            out.append(l)
    return out


def _row_flat(lines, box):
    """Текст в горизонтальной полосе строки: все сегменты, пересекающие её и по
    высоте, и по ширине, слева направо."""
    got = [(l["bbox"][0], l["text"]) for l in lines
           if l["bbox"][3] >= box[1] + 1 and l["bbox"][1] <= box[3] - 1
           and min(l["bbox"][2], box[2]) > max(l["bbox"][0], box[0])]
    return _flat(" ".join(t for _x, t in sorted(got)))


def _subseq(need, hay):
    """Все символы need встречаются в hay в том же порядке. Пропущенный символ
    ловится, а вставки и перерисовка соседа в несколько сегментов — нет."""
    it = iter(hay)
    return all(ch in it for ch in need)


def _lost_bit(need, hay):
    """Самый длинный кусок need, которого не хватило в hay — для сообщения."""
    bits = [need[a:b] for tag, a, b, _c, _d in
            difflib.SequenceMatcher(None, need, hay, autojunk=False).get_opcodes()
            if tag in ("delete", "replace")]
    return max(bits, key=len) if bits else ""


def _verify_page(page, before, done, exempt, warn):
    """Перечитать страницу ПОСЛЕ правок и сверить результат целиком.

    Ни один шаг выше этого не делает: _native_ok проверяет только дописанные
    глифы (тот символ, на том месте, того кегля), а TextWriter — вообще ничего,
    кроме «что-то нарисовалось». Мимо такой проверки проходит и хвост старого
    значения («51000» вместо «1000», если полоса стирания не задела первую
    цифру), и съеденная редактом соседняя строка, и значение, нарисованное не
    туда. Здесь страница читается заново ТОЙ ЖЕ сегментацией (line_segments), что
    и панель: в клетке правки должно читаться новое значение (_band_chars), а
    соседние строки — уцелеть посимвольно (пропажу ловим подпоследовательностью,
    иначе перерисовка соседа в два сегмента выглядела бы как потеря)."""
    after = line_segments(page)
    before_lines = [i for i, _s in before]
    after_lines = [i for i, _s in after]
    for e, p in done:
        bb = e.get("bbox")
        if not bb or _fit_why(p.get("fit")):
            continue        # переполнение уже описано отдельным предупреждением
        box = [float(v) for v in bb]
        new = norm(e.get("text") or "").strip()
        # правое ребро полосы — докуда правке позволено дорасти: значение длиннее
        # прежнего рисуется шире клетки, но не дальше avail (там начинается сосед)
        pen = p.get("pen") or (box[0], box[1])
        lo, hi = box[0] - 2, max(box[2], pen[0] + (p.get("avail") or 0)) + 2
        # В клетке должно читаться новое значение — и только оно. Чужие символы,
        # которые тут стояли и остались на месте (подпись «РЯД» вплотную под
        # крупной цифрой, мелкий шрифт бланка, проходящий через клетку), из
        # сравнения выброшены по позиции: их пропажу ловит проверка соседних строк
        # ниже, здесь она была бы вторым предупреждением об одном дефекте. Всё
        # прочее в полосе — наше: и хвост старого значения («51000» вместо
        # «1000»), и уцелевший знак рубля от перекрытой копии.
        # Сравниваем СОСТАВ, а не строку: порядок в выдаче MuPDF не отражает
        # картинку — уцелевший хвост исходного спана сохраняет прежний bbox на всю
        # ширину значения, дописанная середина идёт отдельным сегментом правее, и
        # идеальный «f0jdc@dollicons.com» читается как «fjdc@…0». Лишняя или
        # потерянная цифра в составе по-прежнему видна.
        alien = {k for k, _c, own in _band_chars(before, box, lo, hi) if not own}
        got = _flat("".join(c for k, c, _own in _band_chars(after, box, lo, hi)
                            if k not in alien))
        cg, want = _chars(got), _chars(_flat(new))
        if cg != want:
            extra = "".join(ch * (n - want.get(ch, 0))
                            for ch, n in sorted(cg.items()) if n > want.get(ch, 0))
            miss = "".join(ch * (n - cg.get(ch, 0))
                           for ch, n in sorted(want.items()) if n > cg.get(ch, 0))
            warn(e, f"после правки на этом месте читается «{got[:40]}», "
                    f"а должно «{_flat(new)[:40]}»"
                    + (f"; лишнее: «{extra[:12]}»" if extra else "")
                    + (f"; пропало: «{miss[:12]}»" if miss else ""))
    after_out = _outside(after_lines, exempt)
    for l in _outside(before_lines, exempt):
        need = _flat(l["text"])
        if not need:
            continue
        row = _row_flat(after_out, l["bbox"])
        if _subseq(need, row):
            continue
        lost = _lost_bit(need, row)
        warn(done[0][0], f"правка стёрла часть соседней строки «{l['text'][:28]}»"
                         + (f": пропало «{lost[:20]}»" if lost else ""))
        break


# ─────────────────── растровые билеты (без текстового слоя) ───────────────────
# Часть билетов — целиком картинка: ни шрифтов, ни текстового слоя, get_text() пуст
# (kassa24 «Танцевальные ритмы мира», 1191×1684 на лист 595 pt). Ни строк, ни правок
# для них не существовало: панель была пуста. raster.py распознаёт такую страницу,
# собирает атлас глифов ИЗ ЕЁ ЖЕ ПИКСЕЛЕЙ и перерисовывает значения теми самыми
# буквами, что уже стоят на билете, — подбирать шрифт не нужно.
# Импорт ленивый: модуль тянет numpy/PIL и собранный помощник OCR (Vision), а
# остальному корпусу он не нужен ни разу.
_RASTER = None


def raster_mod():
    """Модуль растровых билетов, если он есть и OCR собран, иначе None."""
    global _RASTER
    if _RASTER is None:
        try:
            import raster
            _RASTER = raster if raster.ocr_ready() else False
        except Exception:
            _RASTER = False
    return _RASTER or None


def raster_lines(doc_id, page):
    """Строки растрового билета или None — обычный (текстовый) путь редактора."""
    r = raster_mod()
    if r is None:
        return None
    try:
        return r.lines(doc_id, page) or None
    except Exception:
        return None


def _drop_raster(doc_id):
    """Забыть распознанное для вытесненного документа. Если модуль ни разу не
    поднимался — и кэша нет, лишний импорт на вытеснении не делаем."""
    if _RASTER:
        try:
            _RASTER.drop(doc_id)
        except Exception:
            pass


def apply_edits(doc, doc_id, edits):
    """Применить правки к открытому документу. Возвращает список НЕ применённых
    правок: молча не сделать правку хуже, чем отказаться — панель показывает новое
    значение, а в файле остаётся старое, и пользователь узнает об этом только
    сверив готовый PDF."""
    problems = []
    by_page = {}
    for e in edits or []:
        by_page.setdefault(int(e["page"]), []).append(e)
    # Прогреваем кэш встроенных шрифтов ДО любых правок: apply_redactions ниже
    # удаляет из страницы шрифты, которые после стирания текста стали не нужны
    # (напр. ALSRubl* от ценника, LiberationSerif от значения). Если снять их
    # состав уже после редакта — потеряем и определение рубля, и оригинальный
    # шрифт значения при перерисовке. Снимок здесь сохраняет полный набор.
    doc_fonts(doc, doc_id)

    def fail(e, why):
        problems.append({"page": int(e.get("page", 0)), "kind": "fail",
                         "text": (e.get("text") or "")[:60], "why": why})

    def warn(e, why):
        """Правка применена, но результат подозрительный: значение не влезло,
        читается не то, задет соседний текст. Отказаться уже нельзя (пиксели
        изменены), а промолчать нельзя тем более."""
        problems.append({"page": int(e.get("page", 0)), "kind": "warn",
                         "text": (e.get("text") or "")[:60], "why": why})

    for pno, pes in by_page.items():
        if pno < 0 or pno >= len(doc):
            for e in pes:
                fail(e, "нет такой страницы")
            continue
        page = doc[pno]
        if page.rotation:
            # координаты панели — в развёрнутом виде страницы, а content stream и
            # текстовая матрица глифов — в исходном. Ни стереть, ни нарисовать по
            # этим координатам нельзя: правка молча ничего не меняла. Билетов с
            # /Rotate в корпусе (717 файлов) нет ни одного, поэтому честный отказ
            # лучше поддержки на глазок.
            for e in pes:
                fail(e, "страница повёрнута — правка не поддерживается")
            continue
        rl = raster_lines(doc_id, page)
        if rl is not None:
            # Растровый билет: текста на странице нет вовсе, значения — пиксели
            # картинки. Правки пересобираются из атласа глифов на ЧИСТОМ снимке
            # картинки и вклеиваются в неё же, поэтому правка идемпотентна и
            # повторный показ не накапливает огрехи. Дальше обычный путь не идёт:
            # ни line_segments, ни apply_redactions картинку не видят.
            r = raster_mod()
            try:
                r.apply(doc_id, page, pes, fail, warn)
            except Exception as ex:
                for e in pes:
                    fail(e, f"растровый билет: {ex}")
            continue
        # Удаление любых элементов — текста, картинок, графики. Редакт с белой
        # заливкой и полным снятием вырезает всё, что попало в рамку: так удаляются
        # QR-коды, афиша, логотипы, рамки и пунктир, а не только текст.
        is_rem = lambda e: str(e.get("action", "")).lower() in ("remove", "delete")
        removes = [e for e in pes if is_rem(e)]
        if removes:
            pes = [e for e in pes if not is_rem(e)]
            img_r, draw_r, text_r = [], [], []
            for e in removes:
                try:
                    b = [float(v) for v in e["bbox"]]
                except (TypeError, ValueError, KeyError):
                    fail(e, "удалению нужна рамка bbox")
                    continue
                k = str(e.get("kind") or "").lower()
                (img_r if k == "image" else draw_r if k == "draw" else text_r).append(b)
            # Удаляем по типам РАЗНЫМИ проходами и БЕЗ общей белой заливки: афиша
            # Пушкина лежит ПОД заголовком и полями, и прежний редакт с TEXT_REMOVE
            # вместе с картинкой стирал текст билета. Теперь картинка снимается
            # одна (текст и графика нетронуты), графика — одна, текст — один.
            if img_r:
                # Картинку убираем снятием ссылки на XObject, НЕ редакто́м: редакт
                # с IMAGE_REMOVE вырезал и весь контент внутри рамки (текст и линии
                # поверх афиши), а delete_image трогает только саму картинку.
                infos = page.get_image_info(xrefs=True)
                for b in img_r:
                    r = fitz.Rect(*b)
                    hit = None
                    for im in infos:
                        ib = fitz.Rect(im["bbox"])
                        if (ib & r).get_area() > 0.5 * min(ib.get_area() or 1,
                                                          r.get_area() or 1):
                            hit = im
                            break
                    if hit is None:
                        fail({"bbox": b}, "картинка не найдена на странице")
                        continue
                    try:
                        page.delete_image(hit["xref"])
                    except Exception:
                        fail({"bbox": b}, "не удалось удалить картинку")
            if draw_r:
                for b in draw_r:
                    # +1pt: штрих, лежащий ровно на границе, не «covered»;
                    # с отступом он целиком внутри и снимается
                    page.add_redact_annot(fitz.Rect(b[0] - 1, b[1] - 1, b[2] + 1, b[3] + 1),
                                          fill=False)
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                      graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                      text=fitz.PDF_REDACT_TEXT_NONE)
            if text_r:
                for b in text_r:
                    page.add_redact_annot(fitz.Rect(*b), fill=False)
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                      graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                      text=fitz.PDF_REDACT_TEXT_REMOVE)
        if not pes:
            continue
        pairs = line_segments(page)
        orig_lines = [i for i, _s in pairs]
        page_w = page.rect.width
        # карту родных глифов снимаем с ЦЕЛОЙ страницы и до редакта: из неё берём
        # gid и шаг для перерисовки тем же шрифтом (см. page_natives)
        nat = page_natives(page)
        plans = []
        for e in pes:
            # Гард от переноса правок между разными билетами с одинаковым именем
            # файла: правка помнит исходный текст строки (orig). Если на ЭТОМ билете
            # в той же позиции текст другой — правка сделана на чужом билете, молча
            # применять её (перезаписав подлинное место/цену) нельзя. Честный отказ.
            orig = e.get("orig")
            if orig is not None and e.get("bbox"):
                m = match_line(orig_lines, e)
                if m is not None and norm(m["text"]).strip() != norm(orig).strip():
                    fail(e, "поле на этом билете отличается от исходного — правка "
                            "перенесена с другого билета и отклонена")
                    continue
            p = _edit_plan(doc, doc_id, e, pairs, orig_lines, page_w, nat)
            if p:
                plans.append((e, p))
            elif _wanted_change(e, pairs):
                fail(e, "не удалось спланировать правку")
        marked = 0
        # Снимок «до» по каждой правке: ниже проверим, что стирание вообще
        # что-то изменило на картинке. Если текст под наклеенным растром (Афиша),
        # редакт не меняет ни пикселя, а перерисовка ляжет ПОВЕРХ картинки — на
        # билете выходило «2» внахлёст с нарисованной «3». Такую правку не делаем.
        shots = []
        for e, p in plans:
            if not p["rects"]:
                continue
            w = _erase_window(page, p, e)
            if not w.is_empty:
                shots.append((e, p, w, _clip_shot(page, w)))
        # Строки страницы до стирания и зоны, где им законно меняться: своя строка
        # правки и сами полосы стирания. Всё остальное обязано уцелеть — сверка в
        # _verify_page ниже (по символам сегментов, той же line_segments).
        before_pairs = pairs if plans else []
        exempt = []
        for e, p in plans:
            if e.get("bbox"):
                b = [float(v) for v in e["bbox"]]
                exempt.append(fitz.Rect(b[0] - 0.6, b[1] - 0.6, b[2] + 0.6, b[3] + 0.6))
            exempt += [fitz.Rect(r) + (-0.6, -0.6, 0.6, 0.6) for r in p["rects"]]
        for _e, p in plans:
            for r in p["rects"]:
                # fill=False → удаляем только текст, фон (серые панели, рамки,
                # картинки) просвечивает как был. Никаких белых заплаток.
                page.add_redact_annot(r, fill=False)
                marked += 1
        if marked:
            _pin_form_res(page)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
        hidden = set()
        for e, p, w, before in shots:
            if not _ink_changed(before, _clip_shot(page, w)):
                fail(e, "это значение на странице нарисовано картинкой, а текст под ней "
                        "невидим — правка бы не отобразилась")
                hidden.add(id(p))
        # Знак рубля в билетных шрифтах ALSRubl*/RubleOne закодирован кириллической
        # «Р» (U+0420) с фирменной чёрточкой. Идеал — перерисовать «Р» этим же
        # встроенным шрифтом (родной ₽, вес совпадает с ценой). Это безопасно ТОЛЬКО
        # если редакт уже убрал шрифт из ресурсов страницы: иначе TextWriter спутает
        # glyph-id одноимённого шрифта и нарисует голую «Р» без чёрточки. Если шрифт
        # уцелел (рубль есть и вне правки) — коллизия неизбежна, поэтому подменяем «Р»
        # на настоящий ₽ (U+20BD): его нет ни в одном subset, и его нарисует системный
        # HelveticaNeue из хвоста цепочки (глиф чуть светлее, но это верный знак рубля).
        rubl_font = next((f for k, lst in doc_fonts(doc, doc_id).items()
                          if "rubl" in k.lower() for f in lst), None)
        page_font_names = {re.sub(r"^[A-Z]{6}\+", "", (i[3] or "")).lower()
                           for i in page.get_fonts(full=True)}
        rubl_survives = any("rubl" in n for n in page_font_names)
        done = []
        for e, p in plans:
            if id(p) in hidden:
                continue
            if p["text"] and not _draw_plan(page, doc, doc_id, p, rubl_font, rubl_survives):
                fail(e, "не удалось нарисовать значение")
                continue
            why = _fit_why(p.get("fit"))
            if why:
                warn(e, why)
            done.append((e, p))
        if done:
            _verify_page(page, before_pairs, done, exempt, warn)
    return problems


def _wanted_change(e, pairs):
    """Правка действительно что-то меняет? Планировщик отдаёт None и когда менять
    нечего (то же значение, тот же стиль) — такое молчание законно, а вот
    незапланированная настоящая правка это проблема."""
    bb = e.get("bbox")
    if not bb:
        return bool(norm(e.get("text") or "").strip())
    box = [float(v) for v in bb]
    i = _pick_line([l for l, _s in pairs if _same_box(l, box)], e)
    if i is not None:
        return (norm(e.get("text") or "").strip() != i["text"]
                or abs(float(e.get("size") or i["size"]) - float(i["size"])) > 0.05
                or bool(e.get("bold", i["bold"])) != bool(i["bold"])
                or _color_changed(e.get("color"), i["color"]))
    return bool(norm(e.get("text") or "").strip())


def _span_style(sp):
    """Кегль/жирность/засечка/цвет конкретного спана (не всей строки)."""
    return {
        "size": float(sp["size"]),
        "bold": bool((sp["flags"] & 16) or re.search(r"bold|black|heavy|semib", sp["font"], re.I)),
        "serif": _is_serif(sp["font"], sp["flags"]),
        "color": int_to_rgb(sp["color"]),
        "font": sp["font"],
    }


def _right_limit(orig_lines, x_from, y0, y1, page_w):
    """Докуда можно тянуть строку вправо: соседняя колонка на той же высоте
    (напр. «Место» правее «Ряд») либо край страницы. Значение продлеваем до неё,
    а не ужимаем в ширину старого текста — иначе «33-34» вместо «79» скукоживается."""
    right = page_w - 20
    for o in orig_lines:
        ob = o["bbox"]
        if ob[0] > x_from + 2 and not (ob[3] < y0 + 2 or ob[1] > y1 - 2):
            right = min(right, ob[0] - 4)
    return right


def _erase_bands(chars, x_left=None, x_right=None):
    """Узкие полосы стирания вместо прямоугольника во всю высоту строки.

    apply_redactions убирает глиф, который прямоугольник ЗАДЕЛ, а не только
    накрыл целиком, причём сравнивает с метрической рамкой символа (у всех
    символов спана она одной высоты — от выносного элемента до нижнего). Отсюда
    два правила:

    * по вертикали достаточно средних 30% рамки: они пересекают каждый символ
      своего спана, включая точку и запятую, но не задевают строку выше/ниже.
      Стирание «34» во всю высоту сносило верхушки «МЕСТО» строкой ниже
      (studio-билет: оставалось одинокое «О»). На корпусе (13.6k сегментов)
      полная высота задевала чужой текст в 58% случаев, полоса — в 2.4%
      (только 6pt-простыни правил, их не правят);
    * по горизонтали — ровно рамки нужных символов, без запаса: рамки соседей
      стыкуются вплотную, и запас даже в 0.4pt съедает соседнюю букву
      («Ряд:» → «Ряд 7» теряла «д»).

    Полоса на каждый спан: у метки и значения свои кегль и базовая линия."""
    by_sp = {}
    for c in chars:
        if not c["c"].isspace():
            by_sp.setdefault(id(c["sp"]), []).append(c)
    out = []
    for cs in by_sp.values():
        x0 = min(c["bbox"][0] for c in cs)
        x1 = max(c["bbox"][2] for c in cs)
        top = min(c["bbox"][1] for c in cs)
        bot = max(c["bbox"][3] for c in cs)
        mid, h = (top + bot) / 2, max(bot - top, 0.4)
        if x_left is not None:
            x0 = min(x0, x_left)
        if x_right is not None:
            x1 = min(x1, x_right)
        if x1 - x0 > 0.05:
            out.append(fitz.Rect(x0, mid - 0.15 * h, x1, mid + 0.15 * h))
    return out


def _centered_line(orig_lines, bb, page_w):
    """Центрирована ли строка. Догадки «центр строки близко к центру страницы»
    мало: у билетов 188007xxx сектор «Бельэтаж» стоит в колонке, и правка на слово
    другой длины перецентровывала его — значение уезжало влево на 2pt, хотя
    выключено оно по левому краю. Сначала смотрим на соседей по вертикали: тот же
    левый край рядом — левая выключка, центровать нельзя; тот же центр при другом
    крае — центровка есть. Соседей нет — остаётся прежняя догадка."""
    x0, y0, x1, y1 = (float(v) for v in bb)
    cx = (x0 + x1) / 2
    if (x1 - x0) >= page_w * 0.55:
        return False
    left = ctr = False
    for o in orig_lines:
        ob = o["bbox"]
        if abs((ob[1] + ob[3]) / 2 - (y0 + y1) / 2) > 45 or min(ob[2], x1) <= max(ob[0], x0):
            continue                                  # не сосед по вертикали
        if abs(ob[0] - x0) <= 0.7 and abs(ob[2] - x1) > 0.7:
            left = True
        elif abs((ob[0] + ob[2]) / 2 - cx) <= 0.7 and abs(ob[0] - x0) > 1.5:
            ctr = True
    if ctr:
        return True
    if left:
        return False
    # Колонка среди колонок («Ряд | Место | 6+»): короткое значение с соседями
    # по обе стороны НА ТОЙ ЖЕ высоте — не заголовок по центру листа. Иначе
    # догадка «центр близок к центру страницы» ловила «9»/«1» в клетке «Место»
    # (cx в 8% от центра A4), и «1»→«10» получало avail=10.8 и сжималось до 69%.
    # Соседи — по X при перекрытии по Y (та же строка), а не как выше — колонка.
    has_left = has_right = False
    for o in orig_lines:
        ob = o["bbox"]
        if abs((ob[1] + ob[3]) / 2 - (y0 + y1) / 2) > 45:
            continue                                  # не та же строка
        if min(ob[2], x1) > max(ob[0], x0):
            continue                                  # та же клетка / перекрытие
        if ob[0] > x1 + 2:
            has_right = True
        elif ob[2] < x0 - 2:
            has_left = True
        if has_left and has_right:
            return False
    return abs(cx - page_w / 2) < page_w * 0.08


def _plan_full(e, info, bb, orig_lines, page_w, chars=None, nat=None):
    """Полная перерисовка строки — прежнее поведение: стираем всю строку и рисуем
    заново по стилю доминирующего спана (или по стилю, заданному в панели)."""
    x0, y0, x1, y1 = bb
    new = norm(e.get("text") or "").strip()
    # знаем символы — стираем полосами по ним; не знаем (bbox не совпал ни с одной
    # строкой) — приходится бить прямоугольником во всю высоту, как раньше
    rects = (_erase_bands(chars) if chars
             else [fitz.Rect(x0 - 0.5, y0 - 0.5, x1 + 1.0, y1 + 0.5)])
    if info:
        size = float(e.get("size") or info["size"])
        color = hex_to_rgb(e.get("color")) or int_to_rgb(info["color"])
        # origin в info округлён до 0.1pt (он уходит в панель), а как пером его
        # брать нельзя: полная перерисовка вставала на 0.05pt мимо, и при мелком
        # зуме растеризатор снимал стебли на целый пиксель — круг A->B->A по
        # имени basis-билета расходился на 4431 пиксель при верных авансах.
        # Точное перо — origin первого непробельного символа сегмента.
        origin = ([float(v) for v in e["origin"]] if e.get("origin") else
                  [float(chars[0]["origin"][0]), float(chars[0]["origin"][1])] if chars else
                  [float(v) for v in info["origin"]])
        bold = bool(e.get("bold", info["bold"]))
        serif = bool(e.get("serif", info["serif"]))
        fname = info["font"]
    else:
        size = float(e.get("size") or 12)
        color = hex_to_rgb(e.get("color")) or INK
        origin = [float(v) for v in e.get("origin") or (60, 100)]
        bold = bool(e.get("bold"))
        serif = bool(e.get("serif", False))
        fname = ""
    center_seg = (x0 + x1) / 2
    centered = _centered_line(orig_lines, (x0, y0, x1, y1), page_w)
    right = _right_limit(orig_lines, x0, y0, y1, page_w)
    # свободное место — до соседней колонки, но НЕ уже, чем занимал сам оригинал:
    # длинный сектор «Партер. Левая сторона / Ряд…» уже выходит за найденный
    # _right_limit сосед (тот на самом деле не мешает), и значение той же длины
    # молча ужималось/предупреждалось на пустом месте. Оригинал влезал — влезет и
    # равное ему; гард переполнения сработает лишь когда новое ШИРЕ старого.
    avail = ((x1 - x0) + 3) if centered else max(right - origin[0], x1 - origin[0], 8)
    # родные глифы страницы — кроме случая, когда пользователь сменил жирность:
    # её родными глифами не изобразить, там нужна шрифтовая цепочка
    nrun = None
    if chars and not (info and bold != bool(info["bold"])):
        nrun = native_runs(nat, chars, new, fname, size, origin[1])
    return {"rects": rects, "text": new, "pen": origin, "size": size, "color": color,
            "bold": bold, "serif": serif, "font": fname, "avail": avail,
            "nat": nrun, "center": center_seg if centered else None}


def _edit_plan(doc, doc_id, e, pairs, orig_lines, page_w, nat=None):
    """Что стереть и что нарисовать для одной правки.

    Ключевая идея: стираем и перерисовываем ТОЛЬКО изменившуюся середину строки.
    Общее начало и общий конец остаются исходными пикселями, а с ними — жирная
    метка («Место:»), её собственный кегль (у kassa24 «ЦЕНА» 6pt при значении
    12pt), позиционный зазор между меткой и значением и родной знак ₽. Раньше
    строка перерисовывалась целиком одним стилем доминирующего спана, из-за чего
    «ЦЕНА 1500 руб.» превращалась в «ЦЕНА 3728 руб.» единым 12pt без отбивки.

    Полная перерисовка остаётся там, где она и нужна: общего начала/конца нет,
    строку стёрли целиком, пользователь сам сменил кегль/жирность/цвет или строка
    центрирована и её ширина меняется (такую надо перецентровать целиком)."""
    new = norm(e.get("text") or "").strip()
    bb = e.get("bbox")
    if not bb:
        if not new:
            return None
        origin = [float(v) for v in (e.get("origin") or (60, 100))]
        return {"rects": [], "text": new, "pen": origin,
                "size": float(e.get("size") or 12),
                "color": hex_to_rgb(e.get("color")) or INK,
                "bold": bool(e.get("bold")), "serif": bool(e.get("serif", False)),
                "font": "", "avail": max(page_w - 20 - origin[0], 8),
                "nat": None, "center": None}
    box = [float(v) for v in bb]
    info = seg = None
    cand = [(i, s) for i, s in pairs if _same_box(i, box)]
    pick = _pick_line([i for i, _s in cand], e)
    for i, s in cand:
        if i is pick:
            info, seg = i, s
            break
    # Текст поверх текста в той же клетке (билет 1977139276: «18» под видимым
    # «12»). Правим видимую строку, но перекрытую копию надо стереть тоже: иначе
    # старые глифы просвечивают из-под нового значения — самопроверка потом читает
    # «1 1 7» вместо «17». Собираем полосы стирания по её символам.
    stale_rects = _stale_under(pairs, pick, box)

    def _with_stale(pl):
        if pl and stale_rects:
            pl["rects"] = (pl.get("rects") or []) + stale_rects
        return pl

    if info is None or not new:
        return _with_stale(_plan_full(e, info, box, orig_lines, page_w,
                                      seg_chars(seg) if seg else None, nat))
    old = info["text"]
    chars = seg_chars(seg)
    restyled = (abs(float(e.get("size") or info["size"]) - float(info["size"])) > 0.05
                or bool(e.get("bold", info["bold"])) != bool(info["bold"])
                or _color_changed(e.get("color"), info["color"]))
    if not restyled and new == old and not stale_rects:
        return None                # значение не изменилось — не трогаем пиксели вовсе
    # Есть перекрытая копия — точечная правка не годится: она сохраняет общее
    # начало/конец исходными пикселями, а стирание перекрытой копии их сносит
    # («12»→«17» с сохранённой «1» под стиранием «18» давало «7»). Стираем и
    # рисуем всю видимую строку целиком.
    if restyled or stale_rects or "".join(c["c"] for c in chars) != old:
        return _with_stale(_plan_full(e, info, box, orig_lines, page_w, chars, nat))
    p, s = _common_edges(old, new)
    if not p and not s:
        return _with_stale(_plan_full(e, info, box, orig_lines, page_w, chars, nat))
    return _with_stale(
        _plan_precise(doc, doc_id, info, chars, old, new, p, s, orig_lines, page_w, nat)
        or _plan_full(e, info, box, orig_lines, page_w, chars, nat))


def _color_changed(hex_color, src_int):
    c = hex_to_rgb(hex_color)
    if c is None:
        return False
    return max(abs(a - b) for a, b in zip(c, int_to_rgb(src_int))) > 0.005


def _plan_precise(doc, doc_id, info, chars, old, new, p, s, orig_lines, page_w, nat=None):
    """План точечной правки: заменяем только середину old[p:len(old)-s].
    None — если точечно нельзя (зовущий сделает полную перерисовку строки)."""
    y0, y1 = info["bbox"][1], info["bbox"][3]
    mid_old = chars[p:len(old) - s]
    mid_new = new[p:len(new) - s]
    sfx = chars[len(old) - s:] if s else []
    if mid_old:
        cnt = {}
        for c in mid_old:
            cnt[id(c["sp"])] = cnt.get(id(c["sp"]), 0) + 1
        ref = max(mid_old, key=lambda c: cnt[id(c["sp"])])["sp"]
    else:
        ref = chars[p]["sp"] if p < len(chars) else chars[-1]["sp"]
    st = _span_style(ref)
    if mid_old:
        pen = list(mid_old[0]["origin"])
    elif p < len(chars):
        pen = list(chars[p]["origin"])
    else:
        pen = [max(c["bbox"][2] for c in chars), chars[-1]["origin"][1]]
    # ширину старой середины берём из самого PDF (расстояние между пёрами),
    # а не из метрик нашего шрифта — так точнее решаем, влезет ли новая на её место
    if mid_old:
        end = sfx[0]["origin"][0] if sfx else max(c["bbox"][2] for c in mid_old)
        w_old = end - pen[0]
    else:
        w_old = 0.0
    # Ширина новой середины — по тем же авансам, какими её и нарисуем. Если рисуем
    # родными глифами страницы, меряем ими: у билетных сабсетов шаг задаёт генератор
    # (у Times-клона цифра 0.421em против 0.5em у системного Times — ошибка 19%,
    # из-за неё «2 400 Р» считалось шире места и хвост с родным ₽ перерисовывался).
    # Проверяем модель на старой середине: её ширину знаем из PDF точно.
    nrun = native_runs(nat, chars, mid_new, st["font"], st["size"], pen[1]) if mid_new else []
    model_ok = False
    if nrun and sfx and mid_old:
        chk = native_runs(nat, chars, "".join(c["c"] for c in mid_old),
                          st["font"], st["size"], pen[1])
        model_ok = bool(chk) and abs(native_width(chk) - w_old) <= 0.35
        if not model_ok:
            nrun = None
    if nrun is None:
        chain = font_chain(doc, doc_id, st["font"], st["bold"], st["serif"])
        w_new = (sum(f.text_length(t, st["size"]) for f, t in font_runs(mid_new, chain))
                 if mid_new else 0.0)
    else:
        w_new = native_width(nrun)
    sfx_x = min(c["bbox"][0] for c in sfx) if sfx else None
    keep_tail = bool(sfx) and abs(w_new - w_old) <= 0.4
    # Краска середины под хвостом. Сравнивать метрические рамки в лоб нельзя: у
    # плотного набора рамка символа заведомо перекрывает перо следующего (Times-
    # билет: рамка 5.06 при шаге 4.72), но это не краска — оригинал с тем же
    # хвостом рисуется нормально. Поэтому запрет только там, где ширину новой
    # середины мы считали чужими метриками: сойдётся она или нет — неизвестно.
    if (keep_tail and mid_old and not model_ok
            and max(c["bbox"][2] for c in mid_old) > sfx_x + 0.05):
        keep_tail = False
    if (_centered_line(orig_lines, info["bbox"], page_w)
            and abs(w_new - w_old) > 0.4):
        return None                # центрированную строку иной ширины центруем целиком
    text = mid_new if keep_tail else new[p:]
    if not text and not mid_old:
        return None                # правка ничего не меняет
    if text != mid_new:
        nrun = native_runs(nat, chars, text, st["font"], st["size"], pen[1])
    # стираем только сами символы: середину (если хвост сохраняем) либо всё от
    # середины до конца строки. Полосами у базовой линии — см. _erase_bands.
    rects = _erase_bands(mid_old if keep_tail else chars[p:],
                         x_left=pen[0] if mid_old else None,
                         x_right=sfx_x if keep_tail else None)
    right = _right_limit(orig_lines, pen[0], y0, y1, page_w)
    if keep_tail:
        right = min(right, sfx_x)
        avail = max(right - pen[0], 8)
    else:
        # хвост тоже перерисовываем — опорная ширина оригинала до конца строки, а
        # не только середины: замена не длиннее старого хвоста заведомо влезает,
        # гард переполнения не должен пугать на равном по длине значении
        orig_right = max(c["bbox"][2] for c in chars)
        avail = max(right - pen[0], orig_right - pen[0], 8)
    return {"rects": rects, "text": text, "pen": pen, "size": st["size"],
            "color": st["color"], "bold": st["bold"], "serif": st["serif"],
            "font": st["font"], "avail": avail,
            "nat": nrun, "center": None}


def _res_target(page):
    """(xref, префикс-пути) словаря, куда можно дописать шрифт.

    У части билетов (kassa24) страница своих /Resources не имеет — они
    унаследованы от /Pages. Ставить такой странице собственный /Resources нельзя:
    он перекроет унаследованные /XObject и картинки с фоном исчезнут. Поэтому
    дописываем в тот словарь, который страница и так видит (он общий для страниц
    книжки, лишний шрифт в нём никому не мешает)."""
    doc = page.parent
    x = page.xref
    for _ in range(32):
        try:
            k, v = doc.xref_get_key(x, "Resources")
        except Exception:
            return None
        if k == "dict":
            return (x, "Resources/")
        if k == "xref":
            return (int(v.split()[0]), "")
        k, v = doc.xref_get_key(x, "Parent")
        if k != "xref":
            return None
        x = int(v.split()[0])
    return None


def _ensure_native_fonts(page, xrefs):
    """{xref: имя ресурса} для рисования родными глифами. Чего на странице нет —
    возвращаем в /Resources/Font под своим именем: apply_redactions выбрасывает
    шрифт, ставший ненужным (стёрли всю цену — ушёл ALSRubl), а рисовать надо им же."""
    doc = page.parent
    have, used = {}, set()
    for x, _e, _t, _b, nm, *_ in page.get_fonts(full=True):
        have.setdefault(x, nm)
        used.add(nm)
    out = {x: have[x] for x in xrefs if x in have}
    need = sorted(x for x in xrefs if x not in have)
    if not need:
        return out
    tgt = _res_target(page)
    if tgt is None:
        return None
    rx, pref = tgt
    for x in need:
        nm = "Zn%d" % x
        while nm in used:
            nm += "a"
        try:
            doc.xref_set_key(rx, "%sFont/%s" % (pref, nm), "%d 0 R" % x)
        except Exception:
            return None
        used.add(nm)
        out[x] = nm
    return out


def _native_ok(page, drawn, tol=0.3):
    """Каждый дописанный глиф должен оказаться там, куда его послали, того же
    кегля и быть тем самым символом — спрашиваем сам MuPDF. Так ловим и
    остаточный CTM в конце content stream (у части билетов — Афиша, Kassy — поток
    кончается внутри незакрытого «q … cm», и дописанный как есть текст уезжает и
    мельчает: 12pt → 2.88pt), и подстановку не того кода: юникод при обратном
    чтении MuPDF берёт из ToUnicode шрифта, то есть проверяет ровно наш выбор
    cid, а gid (там, где он сравним) — что это не .notdef и не чужой сабсет."""
    try:
        seen = [(c[0], c[1], c[2][0], c[2][1], sp["size"])
                for sp in page.get_texttrace()
                if not sp.get("type") for c in sp["chars"]]
    except Exception:
        return False
    for gid, ucs, x, y, sz in drawn:
        if not any(su == ucs and (gid is None or sg == gid)
                   and abs(sx - x) < tol and abs(sy - y) < tol and abs(ss - sz) < 0.06
                   for su, sg, sx, sy, ss in seen):
            return False
    return True


def _draw_native(page, plan):
    """Дописать текст в content stream страницы её же глифами. False — не вышло,
    рисует TextWriter.

    Td считается от матрицы СТРОКИ, а не от текущего пера, поэтому
    «<cid> Tj  аванс 0 Td» воспроизводит шаг генератора символ в символ и не
    зависит ни от /W, ни от Tz. Состояние текста (Tc/Tw/Tz/Ts/Tr) наследуется от
    предыдущего содержимого — сбрасываем явно, а сжим набора выставляем свой.
    Результат проверяем дважды: трассой (тот глиф, на месте, того кегля) и
    пикселями (что-то реально нарисовалось — поток мог кончиться внутри обрезки).
    Не сошлось — откат байтов, clean_contents() и вторая попытка: он нормализует
    поток, оставаясь пиксель-в-пиксель."""
    runs = plan.get("nat")
    doc = page.parent
    if not runs:
        return False
    xrefs = page.get_contents()
    if not xrefs:
        return False
    if doc.page_count > 1:                       # общий поток — правка ушла бы и на соседа
        for pno in range(doc.page_count):
            if pno != page.number and xrefs[-1] in doc[pno].get_contents():
                return False
    w = native_width(runs)
    k = 1.0
    if w > 0 and plan["avail"] > 0 and w > plan["avail"] + 0.3:
        k = max(plan["avail"] / w, 0.35)
    x = plan["center"] - w * k / 2 if plan["center"] is not None else plan["pen"][0]
    y = plan["pen"][1]
    top = max(r[1] for r in runs) * k
    clip = fitz.Rect(x - 1, y - 1.35 * top, x + w * k + 1, y + 0.5 * top) & page.rect
    mat = fitz.Matrix(2, 2)
    for attempt in (0, 1):
        last = xrefs[-1]
        names = _ensure_native_fonts(page, {r[0] for r in runs if r[0] is not None})
        if not names:
            return False
        ops = ["q", "%.5f %.5f %.5f rg" % tuple(plan["color"]),
               "BT", "0 Tc 0 Tw 100 Tz 0 Ts 0 Tr"]
        p0 = fitz.Point(x, y) * ~page.transformation_matrix
        ops.append("1 0 0 1 %.4f %.4f Tm" % (p0.x, p0.y))
        cur, anchor, acc, off_cur, drawn = None, 0.0, 0.0, 0.0, []
        for xref, sz0, adv, off, cid, gid, ucs, _kz in runs:
            if cid is not None:
                if abs(acc - anchor) > 1e-6 or abs(off - off_cur) > 1e-6:
                    ops.append("%.4f %.4f Td" % (acc - anchor, off_cur - off))
                    anchor, off_cur = acc, off
                sz = round(sz0 * k, 4)
                if (xref, sz) != cur:
                    ops.append("/%s %.4f Tf" % (names[xref], sz))
                    cur = (xref, sz)
                ops.append("<%04X> Tj" % cid)
                drawn.append((gid, ucs, x + acc, y + off, sz))
            acc += adv * k
        ops += ["ET", "Q"]
        if not drawn:
            return False
        try:
            pre = None if clip.is_empty else page.get_pixmap(
                matrix=mat, clip=clip, alpha=False).samples
            old = doc.xref_stream(last)
            doc.update_stream(last, old + ("\n" + "\n".join(ops) + "\n").encode("latin-1"))
        except Exception:
            return False
        painted = pre is None or page.get_pixmap(
            matrix=mat, clip=clip, alpha=False).samples != pre
        if painted and _native_ok(page, drawn):
            if k < 0.999:            # значение не влезло — см. _fit_why
                plan["fit"] = {"how": "squeeze", "k": k,
                               "need": w, "avail": plan["avail"]}
            return True
        try:
            doc.update_stream(last, old)
            if attempt:
                return False
            page.clean_contents()
            xrefs = page.get_contents()
        except Exception:
            return False
        if not xrefs:
            return False
    return False


def _draw_plan(page, doc, doc_id, plan, rubl_font, rubl_survives):
    """Нарисовать запланированный текст: сперва родными глифами страницы (тот же
    шрифт, тот же шаг, родной ₽), иначе — шрифтовая цепочка по стилю своего спана,
    подгонка кегля под доступную ширину, при необходимости — центровка.

    Возвращает, чем нарисовано («родные»/«цепочка»), либо None — значит на
    странице не появилось ничего, и правку нельзя считать применённой."""
    if _draw_native(page, plan):
        return "родные"
    t = plan["text"]

    # Рубль. Знак закодирован кириллической «Р» (U+0420) рубль-шрифта.
    head = None
    if rubl_font is not None and re.search(r"\d\s?Р(?!\w)", t):
        if rubl_survives:
            # шрифт уцелел на странице → коллизия glyph-id неизбежна,
            # рисуем настоящий ₽ (U+20BD) системным HelveticaNeue из хвоста
            t = re.sub(r"(\d\s?)Р(?!\w)", "\\1₽", t)
        else:
            # редакт убрал рубль-шрифт со страницы → коллизии нет: рисуем «Р»
            # тем же фирменным шрифтом (родной ₽ с чёрточкой, вес как у цены)
            head = rubl_font
    chain = font_chain(doc, doc_id, plan["font"], plan["bold"], plan["serif"])
    runs = font_runs(t, chain, head=head)
    size = plan["size"]

    def _rw(sz):
        return sum(f.text_length(s, sz) for f, s in runs)

    while size > 4 and _rw(size) > plan["avail"] + 0.3:
        size -= 0.25
    if size < plan["size"] - 0.01 or _rw(size) > plan["avail"] + 0.3:
        plan["fit"] = {"how": "shrink", "size": size, "was": plan["size"],
                       "need": _rw(plan["size"]), "avail": plan["avail"],
                       "over": _rw(size) > plan["avail"] + 0.3}
    x = plan["center"] - _rw(size) / 2 if plan["center"] is not None else plan["pen"][0]
    y = plan["pen"][1]
    tw = fitz.TextWriter(page.rect, color=plan["color"])
    for f, s in runs:
        try:
            tw.append(fitz.Point(x, y), s, font=f, fontsize=size)
        except Exception:
            f = FONT_BOLD if plan["bold"] else FONT_REG
            tw.append(fitz.Point(x, y), s, font=f, fontsize=size)
        x += f.text_length(s, size)
    tw.write_text(page)
    # TextWriter молчит, когда текст ушёл за пределы страницы (так было на
    # повёрнутых страницах: перо в развёрнутых координатах, глифы за краем).
    return "цепочка" if (tw.text_rect & page.rect).get_area() > 0 else None



def render_png(doc, pno, zoom):
    zoom = max(0.5, min(float(zoom or 1), 4.0))          # без верхней границы это RAM-бомба
    pno = max(0, min(int(pno), len(doc) - 1))
    pm = doc[pno].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pm.tobytes("png")


def _prob_head(problems):
    """Непримененные правки — в заголовок ответа: превью и сохранение отдают
    картинку и файл, вставить их в тело нечем, а знать о них фронтенд обязан.
    Заголовки только latin-1, поэтому JSON проценто-кодируем."""
    if not problems:
        return {}
    return {"X-Edit-Problems": quote(json.dumps(problems, ensure_ascii=False))}


class _HTTPError(Exception):
    """Ответ с конкретным кодом вместо общего 500 (слишком большое тело и т.п.)."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


class Handler(BaseHTTPRequestHandler):
    server_version = "Biletnaya/3.0"
    timeout = 60          # полуоткрытое соединение не должно держать поток вечно
    _read_body = None     # тело запроса, вычитанное до захвата WORK

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        # Второй ответ в одном запросе (обработчик упал ПОСЛЕ отправки картинки или
        # клиент отвалился на середине) писал бы заголовки в тело предыдущего —
        # браузер видел бы мусор. Отвечаем ровно один раз.
        if getattr(self, "_sent", False):
            return
        self._sent = True
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _dl(self, data, name, ctype="application/pdf", problems=None):
        """Отдать файл на скачивание: русское имя — и ASCII-копией, и RFC 5987."""
        ascii_name = re.sub(r"[^\w.\- ]", "", name.encode("ascii", "ignore").decode()).strip()
        if not re.search(r"\w", ascii_name.rsplit(".", 1)[0]):
            ascii_name = "bilet.zip" if name.lower().endswith(".zip") else "bilet.pdf"
        cd = (f'attachment; filename="{ascii_name}"; '
              f"filename*=UTF-8''{quote(name)}")
        self._send(200, data, ctype, {"Content-Disposition": cd, **_prob_head(problems)})

    def _read_socket_body(self, limit):
        # Content-Length без предела = RAM-бомба на любом ручке, кроме /api/open
        # (там свой, файловый лимит): «правки» на 4 ГБ читались бы целиком в память.
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise _HTTPError(413, "тело запроса слишком большое")
        data = self.rfile.read(n)
        if len(data) < n:
            raise _HTTPError(400, "тело запроса оборвалось")
        return data

    def _body(self, limit=MAX_JSON):
        # тело уже вычитано в do_POST (до захвата замка) — здесь только предел ручки
        data = self._read_body or b""
        if len(data) > limit:
            raise _HTTPError(413, "тело запроса слишком большое")
        return data

    def _qs(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _host_ok(self):
        # Защита от DNS-rebinding и запросов с чужих страниц. Была отключена
        # безусловным `return True` «на время показа через туннель» — и осталась
        # так: любой сайт, открытый в браузере, мог править билеты в редакторе.
        # Для туннеля теперь есть BILETNAYA_ALLOW_REMOTE=1.
        if ALLOW_REMOTE:
            return True
        h = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if h not in ("", "127.0.0.1", "localhost", "::1"):
            return False
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site and site not in ("same-origin", "same-site", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            oh = (urlparse(origin).hostname or "").lower()
            if oh not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

    # MuPDF не потокобезопасен, а ThreadingHTTPServer даёт по потоку на запрос:
    # превью одной вкладки параллельно с сохранением другой роняло процесс и
    # портило общие fitz.Font/кэши. Инструмент однопользовательский — сериализуем
    # всю работу с документами одним замком.
    def do_GET(self):
        with WORK:
            self._get()

    def do_POST(self):
        # Сокет вычитываем ДО замка: клиент, недославший тело, иначе держал бы
        # общий замок до self.timeout и морозил все остальные запросы.
        limit = MAX_UPLOAD if urlparse(self.path).path == "/api/open" else MAX_JSON
        try:
            self._read_body = self._read_socket_body(limit)
        except _HTTPError as e:
            return self._json({"error": e.msg}, e.code)
        with WORK:
            self._post()

    def _get(self):
        u = urlparse(self.path)
        q = self._qs()
        if not self._host_ok():
            return self._json({"error": "forbidden host"}, 403)
        try:
            if u.path == "/":
                with open(os.path.join(ROOT, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif u.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            elif u.path == "/api/page":
                doc = open_doc(q["doc"])
                self._send(200, render_png(doc, int(q.get("p", 0)), float(q.get("z", 2))), "image/png")
            elif u.path == "/api/lines":
                doc = open_doc(q["doc"])
                page = doc[int(q.get("p", 0))]
                rl = raster_lines(q["doc"], page)
                if rl is not None:
                    # растровый билет: строки даёт распознавание картинки, теги —
                    # те же самые (field_tags работает по text/bbox/size/color)
                    self._json({"lines": field_tags(raster_mod().panel(rl)),
                                "raster": True, "rotate": bool(page.rotation)})
                else:
                    self._json({"lines": mark_covered(page, field_tags(extract_lines(page)))})
            elif u.path == "/api/objects":
                doc = open_doc(q["doc"])
                page = doc[int(q.get("p", 0))]
                self._json({"objects": page_objects(page)})
            else:
                self._json({"error": "not found"}, 404)
        except _HTTPError as e:
            self._json({"error": e.msg}, e.code)
        except KeyError:
            self._json({"error": "unknown doc"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _post(self):
        u = urlparse(self.path)
        q = self._qs()
        if not self._host_ok():
            return self._json({"error": "forbidden host"}, 403)
        try:
            if u.path == "/api/open":
                if int(self.headers.get("Content-Length") or 0) > MAX_UPLOAD:
                    return self._json({"error": "файл слишком большой"}, 413)
                data = self._body(limit=MAX_UPLOAD)
                try:
                    doc = fitz.open(stream=data, filetype="pdf")
                except Exception:
                    return self._json({"error": "не удалось открыть файл как PDF"}, 400)
                if doc.needs_pass:
                    return self._json({"error": "PDF защищён паролем"}, 400)
                if len(doc) == 0:
                    return self._json({"error": "в PDF нет страниц"}, 400)
                doc.close()
                # Снять ghost-оверлеи mos.ru (дубль шапки на телефонах, которые
                # игнорируют Form BBox) ДО store/sha — дальше все ручки видят
                # уже чистый PDF.
                data, stripped = sanitize_doc_bytes(data)
                try:
                    doc = fitz.open(stream=data, filetype="pdf")
                except Exception:
                    return self._json({"error": "не удалось открыть файл как PDF"}, 400)
                name = q.get("name", "doc.pdf")
                # Пушкинская карта / водяной знак / имя зрителя — снимаем сразу
                # при открытии (Afisha/МХТ), без галочки в UI.
                pk = []
                try:
                    pk = strip_pushkin_doc(doc, clear_name=True)
                    if pk:
                        buf = io.BytesIO()
                        doc.save(buf, garbage=3, deflate=True)
                        data = buf.getvalue()
                        cache_put(hashlib.sha256(data).hexdigest()[:16], data)
                except Exception as e:
                    pk = []
                    print(f"[strip_pushkin open] {name}: {e}", flush=True)
                doc_id = store_doc(data, name)
                # sha контента (не имени!) — клиент ключует им локально сохранённые
                # правки. Разные билеты с одинаковым именем больше не подхватывают
                # чужие правки: ключ привязан к байтам файла.
                sha = hashlib.sha256(data).hexdigest()[:16]
                cache_put(sha, data)
                resp = {"id": doc_id, "pages": len(doc), "name": name,
                        "sha": sha}
                if stripped:
                    resp["ghost_stripped"] = stripped
                if pk:
                    resp["pushkin"] = pk
                self._json(resp)
            elif u.path == "/api/reopen":
                # поднять вкладку из дискового кэша по sha: DOCS не переживают
                # перезапуск сервера, а список вкладок в браузере переживает
                req = json.loads(self._body())
                data = cache_get(str(req.get("sha") or ""))
                if data is None:
                    return self._json({"error": "нет в кэше"}, 404)
                try:
                    doc = fitz.open(stream=data, filetype="pdf")
                    pages = len(doc)
                    if doc.needs_pass or pages == 0:
                        raise ValueError
                    doc.close()
                except Exception:
                    return self._json({"error": "кэш повреждён"}, 400)
                data2, stripped = sanitize_doc_bytes(data)
                try:
                    doc = fitz.open(stream=data2, filetype="pdf")
                    pages = len(doc)
                except Exception:
                    return self._json({"error": "кэш повреждён"}, 400)
                name = req.get("name") or "bilet.pdf"
                pk = []
                try:
                    pk = strip_pushkin_doc(doc, clear_name=True)
                    if pk:
                        buf = io.BytesIO()
                        doc.save(buf, garbage=3, deflate=True)
                        data2 = buf.getvalue()
                except Exception:
                    pk = []
                resp = {"id": store_doc(data2, name), "pages": pages,
                        "name": name, "sha": str(req["sha"])}
                if stripped:
                    resp["ghost_stripped"] = stripped
                if pk:
                    resp["pushkin"] = pk
                self._json(resp)
            elif u.path == "/api/autoclean":
                req = json.loads(self._body())
                src = open_doc(req["doc"])
                cleaned, report = autoclean_doc(src)
                buf = io.BytesIO()
                cleaned.save(buf, garbage=3, deflate=True)
                new_id = store_doc(buf.getvalue(), DOCS.get(req["doc"], {}).get("name", "doc.pdf"))
                self._json({"id": new_id, "pages": len(cleaned), "report": report})
            elif u.path == "/api/preview":
                req = json.loads(self._body())
                doc = open_doc(req["doc"])
                bad = apply_edits(doc, req["doc"], req.get("edits"))
                self._send(200, render_png(doc, int(req.get("p", 0)), float(req.get("z", 2))),
                           "image/png", _prob_head(bad))
            elif u.path == "/api/save":
                req = json.loads(self._body())
                doc = open_doc(req["doc"])
                bad = apply_edits(doc, req["doc"], req.get("edits"))
                try:
                    strip_ghost_forms(doc)
                except Exception:
                    pass
                buf = io.BytesIO()
                doc.save(buf, garbage=3, deflate=True)
                # имя файла как у исходника — без «-edited» в конце.
                # save_as — фамилия зрителя, если панель её нашла на билете
                self._dl(buf.getvalue(), save_as_name(req), problems=bad)
            elif u.path == "/api/save_all":
                # несколько открытых билетов сразу: по правкам каждого — свой PDF, всё в ZIP
                req = json.loads(self._body())
                items, bad = [], []
                for spec in req["files"]:
                    # один мёртвый id (сервер перезапускали, вкладка осталась) не
                    # должен уносить весь архив: пропускаем его и говорим об этом
                    try:
                        doc = prep_doc(spec, bad)
                        try:
                            strip_ghost_forms(doc)
                        except Exception:
                            pass
                        buf = io.BytesIO()
                        doc.save(buf, garbage=3, deflate=True)
                        doc.close()
                        items.append((save_as_name(spec), buf.getvalue()))
                    except KeyError:
                        bad.append({"page": 0, "why": f"«{doc_name(spec)}» не найден на "
                                                      f"сервере — открой билет заново"})
                    except Exception as e:
                        bad.append({"page": 0, "why": f"«{doc_name(spec)}»: {e}"})
                if not items:
                    return self._json({"error": "ни один билет не удалось сохранить",
                                       "problems": bad}, 404)
                self._dl(zip_bytes(items), _file_name_safe(req.get("zip"), "билеты") + ".zip",
                         "application/zip", problems=bad)
            elif u.path == "/api/split":
                req = json.loads(self._body())
                bad = []
                doc = prep_doc(req, bad)
                a = max(1, min(int(req.get("from", 1)), len(doc)))
                b = max(a, min(int(req.get("to", len(doc))), len(doc)))
                self._dl(one_part(doc, a, b),
                         f"{doc_name(req)}-{a}-{b}.pdf" if b > a else f"{doc_name(req)}-{a}.pdf",
                         problems=bad)
            elif u.path == "/api/split_parts":
                # «развязать на несколько»: каждая страница / по N / диапазон,
                # результат либо отдельными файлами в ZIP, либо новыми документами в редакторе
                req = json.loads(self._body())
                bad = []
                doc = prep_doc(req, bad)
                base = doc_name(req)
                parts = split_ranges(len(doc), req)
                as_docs = req.get("as") == "docs"
                # в редактор больше MAX_DOCS отдавать нельзя: следующая же запись в
                # DOCS вытеснит ранние части и вкладки получат 404 «unknown doc».
                # В ZIP ограничение мягче — там ничего не хранится в памяти.
                cap = MAX_DOCS if as_docs else 200
                if len(parts) > cap:
                    doc.close()
                    return self._json({"error": f"слишком много частей ({len(parts)}), "
                                                f"предел {cap}" +
                                                (" — сохрани частями в ZIP" if as_docs else "")}, 400)
                named = [((f"{base}-{a}.pdf" if a == b else f"{base}-{a}-{b}.pdf"),
                          one_part(doc, a, b), b - a + 1) for a, b in parts]
                doc.close()
                if as_docs:
                    ids = store_docs([(data, nm) for nm, data, _ in named])
                    self._json({"docs": [{"id": i, "name": nm, "pages": n}
                                         for i, (nm, _data, n) in zip(ids, named)],
                                "problems": bad})
                else:
                    self._dl(zip_bytes([(nm, data) for nm, data, _ in named]),
                             base + "-части.zip", "application/zip", problems=bad)
            elif u.path == "/api/merge":
                # «связать»: склеиваем в порядке списка, каждый — со своими правками.
                # Если какой-то части нет — честно падаем (склейка без страниц была бы
                # тихой потерей), но незакрытый out за собой не оставляем.
                req = json.loads(self._body())
                out, bad = fitz.open(), []
                try:
                    for spec in req["docs"]:
                        src = prep_doc(spec, bad)
                        out.insert_pdf(src)
                        src.close()
                    buf = io.BytesIO()
                    out.save(buf, garbage=3, deflate=True)
                    pages = len(out)
                finally:
                    out.close()
                name = req.get("name") or "склейка.pdf"
                self._json({"id": store_doc(buf.getvalue(), name),
                            "pages": pages, "name": name, "problems": bad})
            else:
                self._json({"error": "not found"}, 404)
        except _HTTPError as e:
            self._json({"error": e.msg}, e.code)
        except KeyError:
            self._json({"error": "unknown doc"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def main():
    import sys
    import subprocess
    import time
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4312
    try:
        srv = ThreadingHTTPServer((BIND_HOST, port), Handler)
    except OSError:
        # Порт занят — прибиваем ТОЛЬКО слушателя на нём, а не всех, у кого порт
        # 4312 на локальном ИЛИ удалённом конце (браузер с открытым соединением к
        # нам, чужой процесс с эфемерным портом 4312). Сначала мягко (SIGTERM),
        # упрямых — жёстко (SIGKILL).
        import signal
        pids = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                              capture_output=True, text=True).stdout.split()
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except Exception:
                pass
        time.sleep(0.6)
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except Exception:
                pass
        time.sleep(0.4)
        srv = ThreadingHTTPServer((BIND_HOST, port), Handler)
    # 0.0.0.0 — не адрес для браузера, показываем петлю
    shown = "127.0.0.1" if BIND_HOST in ("0.0.0.0", "::") else BIND_HOST
    print(f"Билетная: http://{shown}:{port}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()
