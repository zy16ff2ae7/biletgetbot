"""Tools for parsing, planning, and applying seat/row/sector edits in @biletgetbot.

Uses the underlying 'Билетная' replacement engine with native subset glyphs.
"""
from __future__ import annotations

import re
import secrets
from typing import Any

LBL = {
    "row": re.compile(r"^(\s*(?:ряд|row)\s*[:.№\/-]*\s*(?:row)?\s*[:.№\/-]*\s*)(.*)$", re.I),
    "seat": re.compile(r"^(\s*(?:мест[аоеум]*|seat|кресло)\s*[:.№\/-]*\s*(?:seat)?\s*[:.№\/-]*\s*)(.*)$", re.I),
    "sector": re.compile(r"^(\s*(?:сектор|sector)\s*[:.№\/-]*\s*(?:sector)?\s*[:.№\/-]*\s*)(.*)$", re.I),
}


def label_only(text: str, key: str) -> bool:
    """Return True if the text contains only a label without any value."""
    m = LBL[key].match(str(text).strip())
    return bool(m and not m.group(2).strip())


def parse_seat_input(raw: str) -> dict[str, str]:
    """Parse user text input into row, seat, and/or sector.

    Examples:
        'Ряд 3 Место 12' -> {'row': '3', 'seat': '12'}
        'Партер, ряд 5, место 10' -> {'row': '5', 'seat': '10', 'sector': 'Партер'}
        '3 12' -> {'row': '3', 'seat': '12'}
        'ряд 7' -> {'row': '7'}
        'место 15' -> {'seat': '15'}
        'сектор Балкон' -> {'sector': 'Балкон'}
    """
    res: dict[str, str] = {}
    rest = " " + str(raw or "").replace("\xa0", " ").strip() + " "

    def take(*patterns: str) -> str | None:
        nonlocal rest
        best = None
        for p in patterns:
            m = re.search(p, rest, re.I)
            if m and (best is None or m.start() < best.start()):
                best = m
        if not best:
            return None
        val = next((x for x in best.groups() if x is not None), "")
        rest = rest[:best.start()] + " " + rest[best.end():]
        return val.strip()

    r = take(
        r"(?:ряд|row)\s*[:=.№]?\s*(\d{1,3}[а-яёa-z]?)(?![а-яёa-z0-9])",
        r"(\d{1,3}[а-яёa-z]?)\s*(?:ряд(?![а-яёa-z])|р\.?(?![а-яё]))"
    )
    if r:
        res["row"] = re.sub(r"\s+", "", r)

    r = take(
        r"(?<![а-яёa-z])(?:мест[аоеум]*|seat|м)\.?\s*[:=№]?\s*(\d{1,3}(?:\s*[-–,]\s*\d{1,3})*)(?!\d)",
        r"(\d{1,3}\s*[-–]\s*\d{1,3})",
        r"(\d{1,3}(?:\s*,\s*\d{1,3})+)"
    )
    if r:
        res["seat"] = re.sub(r"\s+", "", r).replace("–", "-")

    r = take(
        r"(?:сектор|сект|зона)\.?\s*[:=№]?\s*([а-яёa-z0-9\s-]+?)(?=[\s,;]+(?:ряд|место|seat|row|\d)|[\s,;]*$)",
        r"((?:ярус|трибуна|подиум|ложа|галерея)\.?\s*[:=№]?\s*[а-яёa-z0-9\s-]+?)(?=[\s,;]+(?:ряд|место|seat|row|\d)|[\s,;]*$)",
        r"((?<![а-яёa-z])(?:партер|амфитеатр|бельэтаж|бенуар|балкон|ложа|ярус|трибуна|галерея|подиум|танцпол|танцевальный партер|фан-?зона)(?:\s+(?:лев[а-яё]+|прав[а-яё]+|центр[а-яё]+|\d+(?:-?[а-яё]+)?|[a-zа-яё0-9]+))?)"
    )
    if r:
        res["sector"] = r.strip()

    # Fallback: if row or seat not extracted, check pattern 'X Y' (two plain numbers)
    if not res.get("row") and not res.get("seat"):
        m_two = re.search(r"\b(\d{1,3})\s*[,;\s]\s*(\d{1,4})\b", rest)
        if m_two:
            res["row"] = m_two.group(1)
            res["seat"] = m_two.group(2)
        else:
            # Single plain number fallback: if only 1 number was provided and user said 'ряд' or 'место'
            m_single = re.search(r"\b(\d{1,4})\b", rest)
            if m_single:
                if "ряд" in raw.lower() or "row" in raw.lower():
                    res["row"] = m_single.group(1)
                elif "мест" in raw.lower() or "seat" in raw.lower():
                    res["seat"] = m_single.group(1)

    return res


def get_seats_for_pages(seat_spec: str, page_count: int) -> list[str]:
    """Given a seat specification and number of pages, return consecutive seat value for each page.

    Examples:
        get_seats_for_pages("12", 3) -> ["12", "13", "14"]
        get_seats_for_pages("12-14", 3) -> ["12", "13", "14"]
        get_seats_for_pages("12, 14, 16", 3) -> ["12", "14", "16"]
        get_seats_for_pages("5А", 2) -> ["5А", "6А"]
        get_seats_for_pages("Входной", 2) -> ["Входной", "Входной"]
    """
    if page_count <= 1 or not seat_spec:
        return [seat_spec] if seat_spec else []

    # Comma-separated list: "12, 13, 14"
    if "," in seat_spec:
        items = [s.strip() for s in seat_spec.split(",") if s.strip()]
        if len(items) >= page_count:
            return items[:page_count]
        last = items[-1]
        m = re.match(r"^(\d+)([а-яёa-z]?)$", last, re.I)
        if m:
            base_n = int(m.group(1))
            suffix = m.group(2)
            needed = page_count - len(items)
            for i in range(1, needed + 1):
                items.append(f"{base_n + i}{suffix}")
            return items
        return items + [last] * (page_count - len(items))

    # Range: "12-14" or "12–14"
    m_range = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", seat_spec)
    if m_range:
        start = int(m_range.group(1))
        end = int(m_range.group(2))
        step = 1 if end >= start else -1
        res = [str(x) for x in range(start, end + step, step)]
        if len(res) >= page_count:
            return res[:page_count]
        last_val = int(res[-1])
        for i in range(1, page_count - len(res) + 1):
            res.append(str(last_val + i))
        return res

    # Single numeric value with optional letter: "12" or "12А"
    m_single = re.match(r"^(\d+)([а-яёa-z]?)$", seat_spec.strip(), re.I)
    if m_single:
        base_num = int(m_single.group(1))
        suffix = m_single.group(2)
        return [f"{base_num + i}{suffix}" for i in range(page_count)]

    return [seat_spec] * page_count


def format_seat_summary(parsed: dict[str, str], page_count: int = 1) -> str:
    """Format human-readable summary of parsed seat instructions."""
    tokens = []
    if parsed.get("sector"):
        tokens.append(f"сектор {parsed['sector']}")
    if parsed.get("row"):
        tokens.append(f"ряд {parsed['row']}")
    if parsed.get("seat"):
        if page_count > 1:
            seats = get_seats_for_pages(parsed["seat"], page_count)
            if seats and len(seats) > 1 and seats[0] != seats[-1]:
                tokens.append(f"места {seats[0]}–{seats[-1]} ({page_count} шт.)")
            elif seats:
                tokens.append(f"место {seats[0]}")
        else:
            tokens.append(f"место {parsed['seat']}")
    return " · ".join(tokens) or "новые места"


def new_text_for_line(line: dict[str, Any], p: dict[str, str]) -> str | None:
    """Compute replacement string for a tagged ticket text line."""
    tags = line.get("tags") or []
    t = line.get("text", "")
    out = t
    hit = False

    # Check if this line is a combined sector + row + seat line:
    # e.g. 'Партер, ряд 9, место 10' or 'Ложа 1-го яруса, ряд 1, место 3'
    m_comb = re.match(r"^(.*?)(,?\s*(?:ряд|row)\b.*)$", t, re.I)
    if m_comb and ("row" in tags or "seat" in tags or "sector" in tags):
        sec_part, row_seat_part = m_comb.group(1), m_comb.group(2)
        if p.get("sector"):
            s_l = LBL["sector"].match(sec_part)
            prefix = s_l.group(1) + p["sector"] if s_l else p["sector"]
        else:
            prefix = sec_part

        if p.get("row"):
            row_seat_part = re.sub(
                r"((?:ряд|row|р(?![а-яёa-z]))\.?\s*[:=№]?\s*)(\d{1,3}[а-яёa-z]?)(?=[\s,;.]|$)",
                r"\g<1>" + p["row"], row_seat_part, flags=re.I
            )
        if p.get("seat"):
            row_seat_part = re.sub(
                r"((?:мест[аоеум]*|seat|м(?![а-яёa-z]))\.?\s*[:=№]?\s*)([\d,\s–-]+)",
                r"\g<1>" + p["seat"], row_seat_part, flags=re.I
            )
        out = prefix + row_seat_part
        return out if out != t else None

    # Individual fields or 'Ряд X Место Y'
    if p.get("row") and "row" in tags and not label_only(t, "row"):
        row_val = p["row"]
        if re.fullmatch(r"\d{1,3}[а-яёa-z]?", t.strip(), re.I):
            out = row_val
        else:
            out_re = re.sub(
                r"((?:ряд|row|р(?![а-яёa-z]))\.?\s*[:=№]?\s*)(\d{1,3}[а-яёa-z]?)(?=[\s,;.]|$)",
                r"\g<1>" + row_val, out, flags=re.I
            )
            if out_re != out:
                out = out_re
            else:
                out = re.sub(
                    r"((?:ряд|row|р(?![а-яёa-z]))\.?\s*:?\s*)(\S+)",
                    r"\g<1>" + row_val, out, flags=re.I
                )
        hit = True

    if p.get("seat") and "seat" in tags and not label_only(t, "seat"):
        seat_val = p["seat"]
        if re.fullmatch(r"\d{1,4}([,\s\-–]+\d{1,4})*", t.strip()):
            out = seat_val
        else:
            out_re = re.sub(
                r"((?:мест[аоеум]*|seat|м(?![а-яёa-z]))\.?\s*[:=№]?\s*)([\d,\s–-]+)",
                r"\g<1>" + seat_val, out, flags=re.I
            )
            if out_re != out:
                out = out_re
            else:
                out = re.sub(r"\d{1,4}(?:\s*[-–,]\s*\d{1,4})*", seat_val, t)
        hit = True

    if p.get("sector") and "sector" in tags and not label_only(t, "sector"):
        sec_val = p["sector"]
        m = LBL["sector"].match(t)
        out = m.group(1) + sec_val if m else sec_val
        hit = True

    return out if (hit and out != t) else None


def apply_seat_edits(doc: Any, seat_input: str | dict[str, str], srv: Any) -> tuple[bool, list[dict[str, Any]], dict[int, list[str]], str | None]:
    """Plan and apply seat/row/sector edits to a fitz Document using srv.apply_edits.

    Returns:
        (ok, edits, replaced_lines_by_page, error_message)
    """
    if isinstance(seat_input, dict):
        parsed = seat_input
    else:
        parsed = parse_seat_input(str(seat_input or ""))

    if not parsed or not (parsed.get("row") or parsed.get("seat") or parsed.get("sector")):
        return False, [], {}, "Не удалось определить ряд, место или сектор из вашего сообщения."

    edits: list[dict[str, Any]] = []
    replaced_lines: dict[int, list[str]] = {}
    page_count = len(doc)
    page_seats = get_seats_for_pages(parsed.get("seat", ""), page_count) if (page_count > 1 and parsed.get("seat")) else [parsed.get("seat")] * page_count

    for pno in range(page_count):
        page = doc[pno]
        page_parsed = dict(parsed)
        if page_seats[pno] is not None:
            page_parsed["seat"] = page_seats[pno]
        lines = srv.field_tags(srv.extract_lines(page))
        for l in lines:
            tags = set(l.get("tags") or [])
            if not (tags & {"row", "seat", "sector"}):
                continue
            nt = new_text_for_line(l, page_parsed)
            if nt and nt != l.get("text"):
                edits.append({
                    "page": pno,
                    "bbox": l["bbox"],
                    "origin": l.get("origin"),
                    "orig": l["text"],
                    "size": l.get("size", 10.0),
                    "bold": l.get("bold", False),
                    "color": None,  # preserve original color
                    "text": nt,
                })
                replaced_lines.setdefault(pno, []).append(l["text"])

    if not edits:
        return False, [], {}, "На билете не найдены распознанные поля ряда, места или сектора для замены."

    doc_id = f"seat-{secrets.token_hex(4)}"
    bad = srv.apply_edits(doc, doc_id, edits)
    if bad:
        # Check if there were fatal failures
        fails = [b for b in bad if b.get("kind") == "fail"]
        if fails:
            return False, edits, replaced_lines, f"Ошибка при замене мест: {fails[0].get('why', 'не удалось применить правку')}"

    return True, edits, replaced_lines, None
