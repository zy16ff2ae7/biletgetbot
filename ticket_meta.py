"""Ticket metadata extraction and clean naming for @biletgetbot.

Extracts show title, theater, date/time, sector, row, seats, and nominal price
WITHOUT personal buyer data (no FIO). Formats descriptive filenames, single-ticket
captions, and batch reports.
"""
from __future__ import annotations

import os
import re
from typing import Any, Sequence

import fitz

MONTH_MAP = {
    "января": "01", "янв": "01",
    "февраля": "02", "фев": "02",
    "марта": "03", "мар": "03",
    "апреля": "04", "апр": "04",
    "мая": "05",
    "июня": "06", "июн": "06",
    "июля": "07", "июл": "07",
    "августа": "08", "авг": "08",
    "сентября": "09", "сен": "09", "сент": "09",
    "октября": "10", "окт": "10",
    "ноября": "11", "ноя": "11",
    "декабря": "12", "дек": "12",
}

THEATER_ALIASES = [
    (re.compile(r"московский художественный|мхт|чехова", re.I), "МХТ Чехова"),
    (re.compile(r"большой театр|габт", re.I), "Большой театр"),
    (re.compile(r"мариинск", re.I), "Мариинский театр"),
    (re.compile(r"вахтангов", re.I), "Театр Вахтангова"),
    (re.compile(r"ленком", re.I), "Ленком"),
    (re.compile(r"сатирикон", re.I), "Сатирикон"),
    (re.compile(r"елизаров", re.I), "Театр Елизарова"),
    (re.compile(r"моссовет", re.I), "Театр Моссовета"),
    (re.compile(r"образцов", re.I), "Театр Образцова"),
    (re.compile(r"фоменко", re.I), "Мастерская Фоменко"),
    (re.compile(r"маяковск", re.I), "Театр Маяковского"),
    (re.compile(r"театр наций", re.I), "Театр Наций"),
    (re.compile(r"\bрамт\b", re.I), "РАМТ"),
    (re.compile(r"бдт|товстоногов", re.I), "БДТ"),
    (re.compile(r"табаков|табакерк", re.I), "Театр Табакова"),
    (re.compile(r"пушкин", re.I), "Театр Пушкина"),
    (re.compile(r"драматический театр рб|гардт рб", re.I), "Русский драмтеатр РБ"),
    (re.compile(r"русский драмтеатр|русский драматический театр", re.I), "Русский драмтеатр"),
]

THEATER_KEYWORDS = [
    "театр", "мхт", "бдт", "мариинск", "вахтангов", "ленком", "сатирикон",
    "моссовет", "образцов", "елизаров", "драмтеатр", "габт", "филармония",
    "консерватория", "дворец культуры", "кз ", "концертный зал",
]

SKIP_TITLE_RE = re.compile(
    r"пушкинская карта|в рамках программы|электронный билет|контроль|линия сгиба|внимание|"
    r"запрещается|распечатать|использование|штрих-код|сложить билет|билетоприемник|поместить билет|"
    r"правила|информация|категория|возрастной ценз|справочная служба|справки|адрес|место проведения|"
    r"организатор|сервис|крымская онлайн|возврат|паспорт|покупатель|зритель|клиент|проход|условия|"
    r"стоимость|без сервисного сбора|распространитель|оферта",
    re.I,
)

LABEL_START_RE = re.compile(
    r"^(?:дата|время|начало|цена|стоимость|ряд|место|сектор|секция|билет|заказ|номер|e-ticket|ticket|order)[\s#№:.]",
    re.I,
)


def clean_theater_name(raw: str | None) -> str:
    """Normalize raw theater/venue string into a human-readable name, or '' if not a venue."""
    if not raw:
        return ""
    text = " ".join(raw.split())
    for pattern, alias in THEATER_ALIASES:
        if pattern.search(text):
            return alias

    # Quote-enclosed name: «...» or "..."
    quoted = re.search(r"[«\"]([^»\"]+)[»\"]", text)
    if quoted:
        candidate = quoted.group(1).strip()
        candidate = re.sub(
            r"^(?:ФГБУК|ГБУК|ГАУК|ГАТ|ГБУКиИ|Государственный|Академический|Московский|Театр)\s+",
            "",
            candidate,
            flags=re.I,
        ).strip()
        if any(k in candidate.lower() for k in THEATER_KEYWORDS):
            return candidate[:32]

    # Check if text contains theater keywords
    if any(k in text.lower() for k in THEATER_KEYWORDS):
        clean = re.sub(
            r"^(?:Организатор:\s*|Место проведения:\s*|Площадка:\s*|Адрес:\s*)",
            "",
            text,
            flags=re.I,
        )
        clean = re.sub(
            r"^(?:Федеральное|Государственное|Бюджетное|Автономное|Учреждение|Культуры|ФГБУК|ГБУК|ГАУК|ГБУКиИ)\s+",
            "",
            clean,
            flags=re.I,
        )
        return clean.strip(" ,.")[:32]

    return ""


def clean_show_title(raw: str | None) -> str:
    """Normalize show / performance / event title."""
    if not raw:
        return ""
    title = " ".join(raw.split()).strip(" «»\"'.,:;-")
    if SKIP_TITLE_RE.search(title):
        return ""
    if LABEL_START_RE.match(title):
        return ""
    if re.match(r"^\d{1,2}\+$", title) or re.match(r"^[.\s_—-]+$", title) or re.match(r"^\d+$", title):
        return ""
    return title[:42]


def parse_date_and_time(text: str, lines: list[str]) -> tuple[str, str, str]:
    """Return (full_date, time_str, short_date) e.g. ('04.11.2026', '19:00', '04.11')."""
    full_date = ""
    time_str = ""

    # Labeled lines
    for i, line in enumerate(lines):
        norm = line.lower().strip()
        if norm in {"дата", "дата проведения", "дата события"} and i + 1 < len(lines):
            cand = lines[i + 1].strip()
            m = re.search(r"\b(\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)\b", cand)
            if m:
                full_date = m.group(1)
        elif norm in {"время", "начало", "время начала"} and i + 1 < len(lines):
            cand = lines[i + 1].strip()
            m = re.search(r"\b(\d{1,2}:\d{2})\b", cand)
            if m:
                time_str = m.group(1)

    # Russian month textual search: "17 октября 2026" or "17 декабря 2026 19:00"
    if not full_date:
        for month_name, month_num in MONTH_MAP.items():
            pattern = rf"\b(\d{{1,2}})\s+{month_name}(?:\s+(\d{{4}}))?"
            match = re.search(pattern, text, re.I)
            if match:
                day = int(match.group(1))
                year = match.group(2) or "2026"
                full_date = f"{day:02d}.{month_num}.{year}"
                break

    # Standard numeric date DD.MM.YYYY
    if not full_date:
        m_num = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", text)
        if m_num:
            full_date = m_num.group(1)

    # Search for HH:MM time
    if not time_str:
        m_tm = re.search(r"\b([012]?\d:[0-5]\d)\b", text)
        if m_tm:
            time_str = m_tm.group(1)

    # Short date DD.MM
    short_date = ""
    if full_date:
        m_short = re.match(r"(\d{1,2})[./-](\d{1,2})", full_date)
        if m_short:
            short_date = f"{int(m_short.group(1)):02d}.{int(m_short.group(2)):02d}"

    return full_date, time_str, short_date


def parse_price(val: str | None) -> tuple[int, str]:
    """Parse numeric and display string for price e.g. (3000, '3 000 ₽')."""
    if not val:
        return 0, ""
    val_clean = re.sub(r"[,.]\d{2}\s*(?:руб|₽|р\.|$)", "", val)
    cleaned = re.sub(r"[^\d]", "", val_clean)
    if not cleaned:
        return 0, ""
    try:
        num = int(cleaned)
        if num < 50 or num > 200_000:
            return 0, ""
        formatted = f"{num:,}".replace(",", " ") + " ₽"
        return num, formatted
    except ValueError:
        return 0, ""


def extract_page_meta(page: fitz.Page, srv=None) -> dict[str, Any]:
    """Extract metadata from a single PDF page."""
    text = page.get_text()
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    theater = ""
    title = ""

    # Theater search
    for line in lines:
        if any(k in line.lower() for k in THEATER_KEYWORDS):
            t_name = clean_theater_name(line)
            if t_name:
                theater = t_name
                break

    # Show title search: 1. check labels
    for i, line in enumerate(lines):
        norm = line.lower()
        if norm in {"наименование", "мероприятие", "спектакль", "событие"} and i + 1 < len(lines):
            cand = clean_show_title(lines[i + 1])
            if cand:
                title = cand
                break

    # Show title search: 2. check top lines
    if not title:
        for line in lines[:15]:
            cand = clean_show_title(line)
            if cand and not clean_theater_name(cand):
                title = cand
                break

    full_date, time_str, short_date = parse_date_and_time(text, lines)

    sector = ""
    row = ""
    seat = ""
    price_val = 0
    price_str = ""

    if srv:
        try:
            extracted = srv.extract_lines(page)
            tagged = srv.field_tags(extracted)
            for item in tagged:
                tags = set(item.get("tags", []))
                val = item.get("text", "").strip()
                if "row" in tags and not row:
                    m = re.search(r"\d+", val)
                    if m:
                        row = m.group(0)
                elif "seat" in tags and not seat:
                    m = re.search(r"\d+", val)
                    if m:
                        seat = m.group(0)
                elif "sector" in tags and val.lower() != "сектор" and not sector:
                    sector = re.sub(r"^(?:сектор|секция)\s*[:№]?\s*", "", val, flags=re.I).strip()
                elif "price" in tags and not price_str:
                    price_val, price_str = parse_price(val)
        except Exception:
            pass

    # Special handler: Ticketland / MTS Live structured layout
    if "TICKETLAND" in text.upper() or "МТС ЛАЙВ" in text.upper() or "НАИМЕНОВАНИЕ МЕРОПРИЯТИЯ" in text.upper():
        try:
            tl_blocks = [b for b in page.get_text("blocks") if 180 <= b[0] <= 380]
            tl_blocks.sort(key=lambda b: b[1])
            for i, b in enumerate(tl_blocks):
                t = b[4].strip()
                if "Наименование мероприятия:" in t and i + 1 < len(tl_blocks):
                    tl_title = clean_show_title(tl_blocks[i + 1][4].strip().splitlines()[0])
                    if tl_title:
                        title = tl_title
                elif "Место проведения:" in t and i + 1 < len(tl_blocks):
                    tl_th = clean_theater_name(tl_blocks[i + 1][4].strip().splitlines()[0])
                    if tl_th:
                        theater = tl_th
                elif "Дата и время" in t and i + 1 < len(tl_blocks):
                    cand_dt = tl_blocks[i + 1][4].strip()
                    f_d, t_s, s_d = parse_date_and_time(cand_dt, [cand_dt])
                    if f_d:
                        full_date, time_str, short_date = f_d, t_s, s_d
                elif "Секция:" in t and i + 1 < len(tl_blocks):
                    sector = tl_blocks[i + 1][4].strip().splitlines()[0]
                elif "Стоимость:" in t and i + 1 < len(tl_blocks):
                    cand_pr = tl_blocks[i + 1][4].strip().splitlines()[0]
                    p_val, p_str = parse_price(cand_pr)
                    if p_val:
                        price_val, price_str = p_val, p_str

            tl_words = [w for w in page.get_text("words") if 180 <= w[0] <= 380 and 320 <= w[1] <= 420]
            row_x, seat_x = None, None
            for w in tl_words:
                if "ряд" in w[4].lower():
                    row_x = w[0]
                elif "место" in w[4].lower():
                    seat_x = w[0]
            for w in tl_words:
                val = w[4].strip()
                if val.isdigit():
                    if row_x is not None and abs(w[0] - row_x) < 25 and not row:
                        row = val
                    elif seat_x is not None and abs(w[0] - seat_x) < 25 and not seat:
                        seat = val
        except Exception:
            pass

    # Regex fallbacks
    if not row:
        m_r = re.search(r"(?:Ряд|ряд|РЯД)\s*[:№]?\s*(\d+)", text)
        if m_r:
            row = m_r.group(1)
    if not seat:
        m_s = re.search(r"(?:Место|место|МЕСТО)\s*[:№]?\s*(\d+)", text)
        if m_s:
            seat = m_s.group(1)
    if not sector:
        m_sec = re.search(r"\b(Партер|Балкон[^\n,]*|Бельэтаж[^\n,]*|Амфитеатр[^\n,]*|Ложа[^\n,]*)\b", text, re.I)
        if m_sec:
            sector = m_sec.group(1).strip()
        else:
            m_sec2 = re.search(r"(?:Сектор|Секция)\s*[:№]?\s*([^\n,]+)", text, re.I)
            if m_sec2:
                sector = m_sec2.group(1).strip()
    if not price_str:
        m_pr = re.search(r"(\d[\d\s]{2,})(?:[.,]\d{2})?\s*(?:руб|₽|р\.)", text, re.I)
        if m_pr:
            price_val, price_str = parse_price(m_pr.group(0))

    return {
        "theater": theater,
        "title": title,
        "date": full_date,
        "time": time_str,
        "short_date": short_date,
        "sector": sector,
        "row": row,
        "seat": seat,
        "price": price_val,
        "price_str": price_str,
    }


def extract_ticket_metadata(doc_or_bytes: fitz.Document | bytes, srv=None) -> dict[str, Any]:
    """Extract aggregated ticket metadata for multi-page or single-page PDF."""
    doc = None
    should_close = False
    try:
        if isinstance(doc_or_bytes, (bytes, bytearray)):
            doc = fitz.open(stream=doc_or_bytes, filetype="pdf")
            should_close = True
        else:
            doc = doc_or_bytes

        if not doc or len(doc) == 0:
            return {}

        pages_meta = [extract_page_meta(page, srv=srv) for page in doc]
        first = pages_meta[0]

        theater = first["theater"] or ""
        title = first["title"] or ""
        date = first["date"] or ""
        time_str = first["time"] or ""
        short_date = first["short_date"] or ""
        sector = first["sector"] or ""
        row = first["row"] or ""

        # Aggregate across all pages if first page missed something
        for pm in pages_meta[1:]:
            if not theater and pm["theater"]:
                theater = pm["theater"]
            if not title and pm["title"]:
                title = pm["title"]
            if not date and pm["date"]:
                date = pm["date"]
            if not time_str and pm["time"]:
                time_str = pm["time"]
            if not short_date and pm["short_date"]:
                short_date = pm["short_date"]
            if not sector and pm["sector"]:
                sector = pm["sector"]
            if not row and pm["row"]:
                row = pm["row"]

        # Collect distinct seats and total price
        seats = [pm["seat"] for pm in pages_meta if pm.get("seat")]
        prices = [pm["price"] for pm in pages_meta if pm.get("price")]
        single_price = prices[0] if prices else 0
        total_price = sum(prices) if prices else single_price

        single_price_str = f"{single_price:,}".replace(",", " ") + " ₽" if single_price else ""
        total_price_str = f"{total_price:,}".replace(",", " ") + " ₽" if total_price else ""

        return {
            "title": title,
            "theater": theater,
            "date": date,
            "time": time_str,
            "short_date": short_date,
            "sector": sector,
            "row": row,
            "seats": seats,
            "price": single_price,
            "price_str": single_price_str,
            "total_price": total_price,
            "total_price_str": total_price_str,
            "pages": len(doc),
        }
    except Exception:
        return {}
    finally:
        if should_close and doc is not None:
            try:
                doc.close()
            except Exception:
                pass


def format_seats_string(seats: Sequence[str]) -> str:
    """Format seats as readable string e.g. '14' or '12, 13, 14' or '12–14'."""
    if not seats:
        return ""
    clean_seats = [str(s).strip() for s in seats if str(s).strip()]
    if not clean_seats:
        return ""
    if len(clean_seats) == 1:
        return clean_seats[0]

    # Try numeric range condensation
    try:
        int_seats = sorted({int(s) for s in clean_seats})
        if len(int_seats) == 1:
            return str(int_seats[0])
        ranges = []
        start = int_seats[0]
        prev = start
        for x in int_seats[1:]:
            if x == prev + 1:
                prev = x
            else:
                ranges.append((start, prev))
                start = x
                prev = x
        ranges.append((start, prev))

        parts = []
        for s, e in ranges:
            if s == e:
                parts.append(str(s))
            elif e == s + 1:
                parts.append(f"{s}, {e}")
            else:
                parts.append(f"{s}–{e}")
        return ", ".join(parts)
    except ValueError:
        pass
    return ", ".join(clean_seats)


def make_clean_ticket_filename(meta: dict[str, Any] | None, fallback: str, hide_price: bool = False) -> str:
    """Build a detailed, informative, safe PDF filename WITHOUT buyer FIO.

    Format: <Спектакль> (<Театр>, <Дата>) — <Сектор> р<Ряд> м<Места> — <Номинал>.pdf
    Example: Чайка (МХТ Чехова, 04.11) — Балкон середина р4 м12–14 — 3000р.pdf
    """
    if not meta:
        base = os.path.splitext(fallback)[0]
        base = re.sub(r"_cleaned$", "", base, flags=re.I)
        return f"{base}_cleaned.pdf"

    title = meta.get("title") or ""
    theater = meta.get("theater") or ""
    short_date = meta.get("short_date") or ""

    # Show title + Theater/Date context
    ctx = []
    if theater:
        ctx.append(theater)
    if short_date:
        ctx.append(short_date)
    ctx_str = f" ({', '.join(ctx)})" if ctx else ""

    head = f"{title}{ctx_str}" if title else (theater or "")

    # Seat location: Sector, Row, Seats
    loc_tokens = []
    if meta.get("sector"):
        loc_tokens.append(meta["sector"])
    if meta.get("row"):
        loc_tokens.append(f"р{meta['row']}")
    if meta.get("seats"):
        loc_tokens.append(f"м{format_seats_string(meta['seats'])}")

    loc_str = " ".join(loc_tokens)

    # Price token
    price_token = ""
    if not hide_price and meta.get("price"):
        price_token = f"{meta['price']}р"

    chunks = []
    if head:
        chunks.append(head)
    if loc_str:
        chunks.append(loc_str)
    if price_token:
        chunks.append(price_token)

    if not chunks:
        base = os.path.splitext(fallback)[0]
        base = re.sub(r"_cleaned$", "", base, flags=re.I)
        return f"{base}_cleaned.pdf"

    filename = " — ".join(chunks) + ".pdf"

    # Sanitize forbidden filesystem characters
    filename = re.sub(r'[/\\:*?"<>|]', "", filename)
    filename = re.sub(r"\s+", " ", filename).strip(" .")

    # Limit filename length safely
    if len(filename) > 170:
        base, ext = os.path.splitext(filename)
        filename = base[:166] + ext

    return filename or f"{os.path.splitext(fallback)[0]}_cleaned.pdf"


def format_single_ticket_caption(meta: dict[str, Any] | None, info: dict[str, Any] | None = None) -> str:
    """Format Telegram document caption for a single processed ticket."""
    info = info or {}
    if not meta or not meta.get("title"):
        verified = info.get("verification", {}) if isinstance(info, dict) else {}
        return (
            f"🩷 Мама приготовила билет. Проверены коды: {verified.get('readable_codes', 0)} · поля: {verified.get('fields', 0)}."
            if (isinstance(info, dict) and info.get("changed"))
            else "🩷 Автоматических правок нет: возвращаю исходный PDF без изменений."
        )

    lines = []
    title = meta["title"]
    pages = meta.get("pages", 1)
    if pages > 1:
        title += f" ({pages} шт.)"
    lines.append(f"🎭 {title}")

    if meta.get("theater"):
        lines.append(f"🏛 {meta['theater']}")

    dt_tokens = []
    if meta.get("date"):
        dt_tokens.append(meta["date"])
    if meta.get("time"):
        dt_tokens.append(meta["time"])
    if dt_tokens:
        lines.append(f"📅 {' '.join(dt_tokens)}")

    loc_tokens = []
    if meta.get("sector"):
        loc_tokens.append(meta["sector"])
    if meta.get("row"):
        loc_tokens.append(f"ряд {meta['row']}")
    if meta.get("seats"):
        seats_label = "место" if len(meta["seats"]) == 1 else "места"
        loc_tokens.append(f"{seats_label} {format_seats_string(meta['seats'])}")
    if loc_tokens:
        lines.append(f"📍 {' · '.join(loc_tokens)}")

    if isinstance(info, dict) and info.get("hide_price"):
        lines.append("💰 Номинал: скрыт 🎁")
    elif meta.get("price_str"):
        price_line = f"💰 Номинал: {meta['price_str']}"
        if isinstance(info, dict) and info.get("custom_price"):
            price_line += " (изменено ✏️)"
        elif pages > 1 and meta.get("total_price") and meta["total_price"] != meta["price"]:
            price_line += f" (сумма: {meta['total_price_str']})"
        lines.append(price_line)

    if isinstance(info, dict) and info.get("spectator_fio"):
        lines.append(f"👤 Зритель: {info['spectator_fio']}")

    lines.append("")
    if isinstance(info, dict) and info.get("seat_info"):
        lines.append("🩷 Мама пересадила зрителя на новые места 🪑")
    elif isinstance(info, dict) and info.get("custom_price"):
        lines.append("🩷 Мама обновила цену билета ✏️")
    elif isinstance(info, dict) and info.get("spectator_fio"):
        lines.append(f"🩷 Мама добавила зрителя {info['spectator_fio']}")
    elif isinstance(info, dict) and info.get("hide_price"):
        lines.append("🩷 Мама скрыла цену билета 🎁")
    elif isinstance(info, dict) and info.get("changed"):
        lines.append("🩷 Мама очистила билет")
    else:
        lines.append("🩷 Исходный PDF без изменений")

    return "\n".join(lines)


def make_merged_pdf_filename(items_meta: list[dict[str, Any]], good: int) -> str:
    """Generate a clean filename for merged multi-page PDF."""
    zip_name = make_batch_zip_filename(items_meta, good)
    if zip_name.lower().endswith(".zip"):
        return zip_name[:-4] + ".pdf"
    return f"{zip_name}.pdf"


def check_batch_duplicates(items_meta: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect duplicate seats or ticket identifiers across the batch."""
    seen = {}
    duplicates = []
    for idx, item in enumerate(items_meta, 1):
        meta = item.get("meta") or {}
        title = (meta.get("title") or "").strip().lower()
        date = (meta.get("date") or meta.get("short_date") or "").strip()
        row = str(meta.get("row") or "").strip()
        seats = [str(s).strip() for s in (meta.get("seats") or []) if str(s).strip()]
        for s in seats:
            key = (title, date, row, s)
            if key in seen:
                duplicates.append({
                    "first_idx": seen[key],
                    "duplicate_idx": idx,
                    "title": meta.get("title") or "Спектакль",
                    "row": row,
                    "seat": s,
                })
            else:
                seen[key] = idx
    return duplicates


def format_batch_report_table(items_meta: list[dict[str, Any]]) -> str:
    """Format structured plaintext table for report.txt inside ZIP."""
    lines = [
        "=" * 88,
        "РЕЕСТР ОБРАБОТАННЫХ БИЛЕТОВ (МАМА · @biletgetbot)",
        "=" * 88,
        "",
        f"{'№':<3} | {'Спектакль':<24} | {'Театр':<16} | {'Дата':<10} | {'Места':<22} | {'Номинал':<10} | {'Имя файла'}",
        "-" * 88,
    ]

    total_sum = 0
    total_tickets = 0

    for idx, item in enumerate(items_meta, 1):
        meta = item.get("meta") or {}
        show = (meta.get("title") or "—")[:24]
        theater = (meta.get("theater") or "—")[:16]
        date = (meta.get("date") or "—")[:10]

        loc = []
        if meta.get("sector"):
            loc.append(meta["sector"])
        if meta.get("row"):
            loc.append(f"р{meta['row']}")
        if meta.get("seats"):
            loc.append(f"м{format_seats_string(meta['seats'])}")
        loc_str = " ".join(loc)[:22] or "—"

        price_str = meta.get("price_str") or "—"
        out_name = item.get("output_name") or "ticket.pdf"

        lines.append(
            f"{idx:<3} | {show:<24} | {theater:<16} | {date:<10} | {loc_str:<22} | {price_str:<10} | {out_name}"
        )

        total_sum += meta.get("total_price", 0) or meta.get("price", 0) or 0
        total_tickets += meta.get("pages", 1) or 1

    lines.extend([
        "-" * 88,
        f"ИТОГО: файлов: {len(items_meta)}, билетов: {total_tickets}, сумма номиналов: {total_sum:,}".replace(",", " ") + " ₽.",
        "=" * 88,
    ])

    dups = check_batch_duplicates(items_meta)
    if dups:
        lines.extend([
            "",
            "⚠️ ВНИМАНИЕ: В ПАЧКЕ ОБНАРУЖЕНЫ ДУБЛИКАТЫ МЕСТ!",
        ])
        for d in dups:
            lines.append(f"  * {d['title']}: ряд {d['row']}, место {d['seat']} (файлы №{d['first_idx']} и №{d['duplicate_idx']})")
        lines.append("=" * 88)

    return "\n".join(lines)


def format_batch_tg_caption(items_meta: list[dict[str, Any]], good: int, failed: int) -> str:
    """Format concise summary for Telegram ZIP document caption."""
    header = f"🩷 Мама упаковала пачку: {good} готово"
    if failed:
        header += f", {failed} с ошибкой"
    header += "\n"

    dups = check_batch_duplicates(items_meta)
    if dups:
        dup_msgs = [f"р{d['row']} м{d['seat']} (№{d['first_idx']} и №{d['duplicate_idx']})" for d in dups[:2]]
        header += "⚠️ Дубликаты мест: " + ", ".join(dup_msgs) + "\n"

    rows = []
    for idx, item in enumerate(items_meta, 1):
        meta = item.get("meta") or {}
        show = meta.get("title") or "Билет"
        loc = []
        if meta.get("sector"):
            loc.append(meta["sector"])
        if meta.get("row"):
            loc.append(f"р{meta['row']}")
        if meta.get("seats"):
            loc.append(f"м{format_seats_string(meta['seats'])}")
        loc_str = " ".join(loc)
        price_str = meta.get("price_str") or ""

        parts = [f"{idx}. {show}"]
        if loc_str:
            parts.append(loc_str)
        if price_str:
            parts.append(price_str)
        rows.append(" · ".join(parts))

    caption = header + "\n" + "\n".join(rows) + "\n\n📄 Подробный реестр — в report.txt внутри ZIP."

    # Telegram caption hard limit is 1024 chars
    if len(caption) > 1000:
        short_rows = rows[:10]
        remainder = len(rows) - len(short_rows)
        caption = (
            header + "\n" + "\n".join(short_rows) + f"\n... и ещё {remainder} шт."
            + "\n\n📄 Полный реестр — в report.txt внутри ZIP."
        )

    return caption[:1020]


def get_event_key(meta: dict[str, Any] | None) -> tuple:
    """Group key identifying a distinct event (show/date/venue)."""
    meta = meta or {}
    title = (meta.get("title") or "").strip().lower()
    theater = (meta.get("theater") or "").strip().lower()
    date = (meta.get("date") or meta.get("short_date") or "").strip()
    return (title, theater, date)


def make_event_folder_name(meta: dict[str, Any] | None) -> str:
    """Generate a clean folder name for an event in a multi-event batch.

    Example: 'Чайка (МХТ Чехова, 04.11)' or 'Серёжа (МХТ Чехова, 28.12)'.
    """
    meta = meta or {}
    title = meta.get("title") or "Билеты"
    theater = meta.get("theater") or ""
    date = meta.get("short_date") or meta.get("date") or ""

    ctx = []
    if theater:
        ctx.append(theater)
    if date:
        ctx.append(date)

    if ctx:
        folder = f"{title} ({', '.join(ctx)})"
    else:
        folder = title

    folder = re.sub(r'[/\\:*?"<>|]', "", folder).strip(" .")
    folder = re.sub(r"\s+", " ", folder)
    if len(folder) > 80:
        folder = folder[:80].strip()
    return folder or "Билеты"


def make_batch_zip_filename(items_meta: list[dict[str, Any]], good: int) -> str:
    """Generate a clean, informative ZIP filename based on the tickets in the batch."""
    if not items_meta:
        return "tickets_cleaned.zip"

    metas = [item["meta"] for item in items_meta if item.get("meta") and item["meta"].get("title")]
    if not metas:
        return "tickets_cleaned.zip"

    titles = {m["title"] for m in metas}
    dates = {m.get("short_date") for m in metas if m.get("short_date")}
    theaters = {m.get("theater") for m in metas if m.get("theater")}

    def _tickets_word(n: int) -> str:
        if 11 <= n % 100 <= 19:
            return f"{n} билетов"
        rem = n % 10
        if rem == 1:
            return f"{n} билет"
        if 2 <= rem <= 4:
            return f"{n} билета"
        return f"{n} билетов"

    if len(titles) == 1:
        title = list(titles)[0]
        theater = list(theaters)[0] if len(theaters) == 1 else ""
        date = list(dates)[0] if len(dates) == 1 else ""
        ctx = [c for c in [theater, date] if c]
        ctx_str = " (" + ", ".join(ctx) + ")" if ctx else ""

        sectors = {str(m.get("sector")).strip() for m in metas if m.get("sector")}
        rows = {str(m.get("row")).strip() for m in metas if m.get("row")}
        all_seats = []
        for m in metas:
            all_seats.extend(m.get("seats") or [])
        seats_str = format_seats_string(all_seats)

        if len(sectors) <= 1 and len(rows) <= 1 and rows and seats_str:
            sec_row = []
            if sectors:
                sec_row.append(list(sectors)[0])
            sec_row.append("р" + str(list(rows)[0]))
            sec_row.append("м" + seats_str)
            loc_str = " ".join(sec_row)
            name = f"{title}{ctx_str} — {loc_str}.zip"
        else:
            name = f"{title}{ctx_str} — {_tickets_word(good)}.zip"
    else:
        theater_prefix = "Билеты " + list(theaters)[0] if len(theaters) == 1 else "Билеты"
        date_str = ""
        if len(dates) == 1:
            date_str = " (" + list(dates)[0] + ")"
        elif len(dates) > 1:
            sorted_dates = sorted(dates)
            date_str = f" ({sorted_dates[0]}–{sorted_dates[-1]})"
        name = f"{theater_prefix}{date_str} — {_tickets_word(good)}.zip"

    name = re.sub(r'[/\\:*?"<>|]', "", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    if not name.endswith(".zip"):
        name += ".zip"
    if len(name) > 160:
        name = name[:156] + ".zip"
    return name or "tickets_cleaned.zip"
