"""Tools for manual text redaction, Pushkin Card removal, and Avito safe preview generation.

Designed for @biletgetbot to provide:
1. /del <text> (force eraser for any user-specified text on ticket)
2. '🧹 Снять Пушкинскую карту' (aggressive Pushkin card marker and banner removal)
3. '🛡 Для Авито' (safe marketplace preview with masked barcodes & anti-fraud watermarks)
"""
from __future__ import annotations

import io
import re
from typing import Any

import fitz
import numpy as np


PUSHKIN_PATTERNS = [
    re.compile(r"пушкинск\w*\s+карт\w*", re.I),
    re.compile(r"в\s+рамках\s+программ\w*\s*[«\"']?пушкинск\w*", re.I),
    re.compile(r"программ\w*\s*[«\"']?пушкинск\w*", re.I),
    re.compile(r"оплат\w*:\s*пушкинск\w*", re.I),
    re.compile(r"номинал\s*пк\b", re.I),
    re.compile(r"\bпушкинск\w*\b", re.I),
    re.compile(r"билет\s+в\s+рамках\s+программы", re.I),
]


def redact_text_from_doc(doc: fitz.Document, target_text: str, srv: Any = None) -> tuple[int, list[str]]:
    """Search for target_text on all pages and apply clean white redactions.

    Returns:
        (redaction_count, list_of_removed_texts)
    """
    clean_target = target_text.strip()
    if not clean_target:
        return 0, []

    hits_count = 0
    removed_texts: list[str] = []

    for pno in range(len(doc)):
        page = doc[pno]
        page_rect = page.rect

        # 1. Direct search_for with case variations
        rects = page.search_for(clean_target)
        if not rects:
            # Try lower and upper
            rects = page.search_for(clean_target.lower())
        if not rects:
            rects = page.search_for(clean_target.upper())

        # 2. Line-based search (useful when text is part of a formatted line)
        if srv and hasattr(srv, "extract_lines"):
            try:
                lines = srv.extract_lines(page)
                for l in lines:
                    t = (l.get("text") or "").strip()
                    if clean_target.lower() in t.lower():
                        l_box = fitz.Rect(l["bbox"])
                        # Check if already covered by rects
                        if not any(l_box.intersects(r) for r in rects):
                            rects.append(l_box)
            except Exception:
                pass

        # Apply redactions for this page
        for r in rects:
            # Expand slightly by 1pt to avoid residual fringe pixels
            redact_box = (fitz.Rect(r) + (-1.0, -1.0, 1.0, 1.0)) & page_rect
            page.add_redact_annot(redact_box, fill=(1, 1, 1))
            hits_count += 1
            removed_texts.append(clean_target)

        page.apply_redactions()

    return hits_count, removed_texts


def strip_pushkin_card_from_doc(doc: fitz.Document, srv: Any = None) -> tuple[int, list[str]]:
    """Aggressively find and remove all Pushkin Card texts, banners, and marks.

    Returns:
        (removal_count, list_of_removed_texts)
    """
    removal_count = 0
    removed_texts: list[str] = []

    for pno in range(len(doc)):
        page = doc[pno]
        page_rect = page.rect
        page_lines = []

        if srv and hasattr(srv, "extract_lines"):
            try:
                page_lines = srv.extract_lines(page)
            except Exception:
                page_lines = []

        # Find matching lines
        for l in page_lines:
            t = (l.get("text") or "").strip()
            if not t:
                continue
            matched = any(p.search(t) for p in PUSHKIN_PATTERNS)
            if matched:
                r = (fitz.Rect(l["bbox"]) + (-1.5, -1.5, 1.5, 1.5)) & page_rect
                page.add_redact_annot(r, fill=(1, 1, 1))
                removal_count += 1
                removed_texts.append(t)

        # Fallback text search for standard phrases
        for phrase in ("Пушкинская карта", "Пушкинская Карта", "ПУШКИНСКАЯ КАРТА",
                       "В рамках программы «Пушкинская карта»", "В рамках программы \"Пушкинская карта\""):
            for r in page.search_for(phrase):
                box = (fitz.Rect(r) + (-1.5, -1.5, 1.5, 1.5)) & page_rect
                page.add_redact_annot(box, fill=(1, 1, 1))
                removal_count += 1
                removed_texts.append(phrase)

        # Check for Pushkin header banner image (175x50 etc.)
        for im in page.get_image_info(xrefs=True):
            r = fitz.Rect(im["bbox"])
            # Proportions of the Pushkin Card top logo / banner
            if r.y0 < 120 and r.width >= 70 and r.height <= 100 and r.width >= 1.6 * r.height:
                # If banner is near top of page and resembles Pushkin badge
                box = (r + (-1.0, -1.0, 1.0, 1.0)) & page_rect
                page.add_redact_annot(box, fill=(1, 1, 1))
                removal_count += 1

        page.apply_redactions()

    return removal_count, removed_texts


def make_avito_preview(pdf_bytes: bytes, srv: Any = None) -> bytes | None:
    """Render a safe showcase preview for Avito/social media.

    Masks all QR codes, linear barcodes, and barcode serial numbers with
    a security badge, and adds an anti-theft watermark across the ticket.
    Returns PNG image bytes.
    """
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if len(doc) == 0:
                return None
            # Work on a copy of page 0
            page = doc[0]
            page_rect = page.rect

            # 1. Detect barcode/QR regions from images
            mask_rects: list[fitz.Rect] = []
            for im in page.get_image_info(xrefs=True):
                r = fitz.Rect(im["bbox"])
                if r.width < 10 or r.height < 10:
                    continue
                # Linear barcode: wide and low
                is_barcode = r.width >= 2.2 * max(r.height, 1.0) and r.width > 30
                # QR code: roughly square
                is_qr = abs(r.width - r.height) < r.width * 0.40 and 30 < r.width < 250
                if is_barcode or is_qr:
                    mask_rects.append(r)

            # 2. Detect vector drawings resembling barcodes
            for d in page.get_drawings():
                rd = d.get("rect") and fitz.Rect(d["rect"])
                if rd and rd.width > 40 and rd.height > 15:
                    if rd.width >= 2.2 * rd.height:
                        mask_rects.append(rd)

            # 3. Detect barcode digit lines (e.g. 12-24 digit numbers near barcodes)
            if srv and hasattr(srv, "extract_lines"):
                try:
                    for l in srv.extract_lines(page):
                        t = (l.get("text") or "").strip()
                        if re.fullmatch(r"[\d\s-]{10,32}", t):
                            l_box = fitz.Rect(l["bbox"])
                            # If near any barcode or looks like serial
                            mask_rects.append(l_box)
                except Exception:
                    pass

            # 4. Fallback: if no barcodes were auto-detected, detect standard corners/bottom zones
            if not mask_rects:
                # Typical ticket barcode positions: bottom 25% or top right
                mask_rects.append(fitz.Rect(page_rect.width * 0.55, page_rect.height * 0.75, page_rect.width - 20, page_rect.height - 20))

            # Merge overlapping mask rects
            merged_rects: list[fitz.Rect] = []
            for r in mask_rects:
                r_exp = (fitz.Rect(r) + (-4, -4, 4, 4)) & page_rect
                merged = False
                for i, existing in enumerate(merged_rects):
                    if r_exp.intersects(existing):
                        merged_rects[i] = existing | r_exp
                        merged = True
                        break
                if not merged:
                    merged_rects.append(r_exp)

            # 5. Draw protective badges over all code areas
            for rect in merged_rects:
                # Solid stylish background
                page.draw_rect(rect, color=(0.12, 0.14, 0.18), fill=(0.12, 0.14, 0.18))
                # Border
                page.draw_rect(rect, color=(0.95, 0.77, 0.05), width=1.5)
                # Protective text
                label = "🔒 КОД СКРЫТ ДЛЯ АВИТО"
                fontsize = max(7.0, min(12.0, rect.height * 0.35, rect.width / (len(label) * 0.6)))
                text_x = rect.x0 + max(2.0, (rect.width - (len(label) * fontsize * 0.52)) / 2)
                text_y = rect.y0 + (rect.height + fontsize * 0.7) / 2
                page.insert_text(
                    (text_x, text_y),
                    label,
                    fontsize=fontsize,
                    color=(1, 1, 1)
                )

            # 6. Anti-fraud watermark across ticket body
            wm_text = "🛡 ДЛЯ ПРОДАЖИ НА АВИТО · КОДЫ СКРЫТЫ 🛡"
            center_x = max(20.0, page_rect.width * 0.10)
            center_y = page_rect.height * 0.52
            page.insert_text(
                (center_x, center_y),
                wm_text,
                fontsize=15.0,
                color=(0.75, 0.2, 0.2),
                rotate=0
            )
            page.insert_text(
                (center_x + 10, center_y + 24),
                "НЕ ДЛЯ ПРОХОДА · ТОЛЬКО ДЛЯ ОЗНАКОМЛЕНИЯ",
                fontsize=11.0,
                color=(0.5, 0.5, 0.5),
                rotate=0
            )

            # 7. Render high-resolution PNG
            pix = page.get_pixmap(dpi=150)
            return pix.tobytes("png")
    except Exception:
        return None
