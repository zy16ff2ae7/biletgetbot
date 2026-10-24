"""Independent preservation checks for ticket fields and admission codes."""
from collections import Counter
import re

import fitz
import numpy as np
import zxingcpp


class ValidationError(ValueError):
    pass


def _text(value):
    return " ".join(value.split())


def _inside(rect, areas):
    rect = fitz.Rect(rect)
    return any((rect & fitz.Rect(area)).get_area() > rect.get_area() * 0.5
               for area in areas)


def _codes(page):
    scale = 2
    if page.rect.get_area() * scale * scale > 15_000_000:
        raise ValidationError("Слишком большой размер страницы для проверки кодов.")
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY)
    pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    found = []
    for code in zxingcpp.read_barcodes(pixels):
        points = [code.position.top_left, code.position.top_right,
                  code.position.bottom_left, code.position.bottom_right]
        rect = fitz.Rect(min(p.x for p in points) / scale, min(p.y for p in points) / scale,
                         max(p.x for p in points) / scale, max(p.y for p in points) / scale)
        found.append(((str(code.format), bytes(code.bytes)), rect))
    return found


def _visual_same(before, after, rect, decoded_regions=()):
    rect = (fitz.Rect(rect) + (-2, -2, 2, 2)) & before.rect
    a = before.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, colorspace=fitz.csGRAY)
    b = after.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, colorspace=fitz.csGRAY)
    if (a.width, a.height) != (b.width, b.height):
        return False
    shape = (a.height, a.width)
    original = np.frombuffer(a.samples, dtype=np.uint8).reshape(shape).astype(np.int16)
    result = np.frombuffer(b.samples, dtype=np.uint8).reshape(shape).astype(np.int16)
    if decoded_regions:
        xs = (np.arange(a.width) + a.x + .5) / 2
        ys = (np.arange(a.height) + a.y + .5) / 2
        known = np.zeros(shape, dtype=bool)
        for code in decoded_regions:
            box = fitz.Rect(code) + (-2, -2, 2, 2)
            known |= ((ys[:, None] >= box.y0) & (ys[:, None] <= box.y1)
                      & (xs[None, :] >= box.x0) & (xs[None, :] <= box.x1))
        # Exempt only decoded pixels, not a whole image containing another code.
        original, result = original[~known], result[~known]
        if not original.size:
            return True
        if (original.min() >= 160 and result.min() >= 160
                and np.ptp(original) <= 8 and np.ptp(result) <= 8):
            return True  # A uniform light quiet zone around a readable code.
    diff = np.abs(original - result)
    if not diff.size or diff.max() <= 8:
        return True
    # A transparent QR can sit on a grey background that cleanup whitens.
    # Permit that specific change only with stable dark ink and unchanged
    # contrast-normalized geometry, keeping the same per-pixel tolerance.
    dark_a, light_a = np.percentile(original, [5, 95])
    dark_b, light_b = np.percentile(result, [5, 95])
    if (light_a < 160 or light_b < 240 or light_b < light_a
            or abs(dark_a - dark_b) > 8
            or min(light_a - dark_a, light_b - dark_b) < 80):
        return False
    normalized_a = np.rint(np.clip((original - dark_a) * 255 / (light_a - dark_a), 0, 255))
    normalized_b = np.rint(np.clip((result - dark_b) * 255 / (light_b - dark_b), 0, 255))
    return np.abs(normalized_a - normalized_b).max() <= 8


DATE_RE = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}:\d{2}\b")


def _raster_code_regions(page):
    """Protect dense monochrome code-like images even if decoding fails."""
    regions = []
    for item in page.get_image_info():
        rect = fitz.Rect(item["bbox"]) & page.rect
        if min(rect.width, rect.height) < 12 or rect.get_area() > page.rect.get_area() * 0.25:
            continue
        aspect = rect.width / rect.height
        square = 0.65 < aspect < 1.55
        bars = aspect > 1.8 or aspect < 0.56
        if not square and not bars:
            continue
        if bars and rect.width > 260:
            continue
        scale = min(2, 512 / max(rect.width, rect.height))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect, colorspace=fitz.csGRAY)
        pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        if pixels.size < 100 or (pixels < 120).mean() < 0.08 or (pixels > 170).mean() < 0.08:
            continue
        # Код — ЧБ; цветной значок промо-баннера (mos.ru и т.п.) не защищаем.
        rgb_pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect, colorspace=fitz.csRGB)
        rgb = np.frombuffer(rgb_pix.samples, dtype=np.uint8).reshape(rgb_pix.height, rgb_pix.width, 3).astype(np.int16)
        if (rgb.max(axis=2) - rgb.min(axis=2)).mean() > 40:
            continue
        binary = pixels < 145
        horizontal = (binary[:, 1:] != binary[:, :-1]).mean()
        vertical = (binary[1:, :] != binary[:-1, :]).mean()
        if ((square and min(horizontal, vertical) > 0.04)
                or (bars and max(horizontal, vertical) > 0.04)):
            regions.append(rect)
    return regions


def _parallel_bar_regions(members):
    """Find aligned barcode bars without treating a connected table as a code."""
    groups = []
    for member in members:
        fill = member.get("fill")
        rect = fitz.Rect(member["rect"])
        if not fill or max(fill) >= 0.3 or min(rect.width, rect.height) >= 3:
            continue
        vertical = rect.height > rect.width
        short, long = ((rect.width, rect.height) if vertical else (rect.height, rect.width))
        if short <= 0 or long < 12 or long < short * 5:
            continue
        start, end = (rect.y0, rect.y1) if vertical else (rect.x0, rect.x1)
        for group in groups:
            if (vertical == group["vertical"]
                    and max(group["starts"] + [start]) - min(group["starts"] + [start]) <= 2
                    and max(group["ends"] + [end]) - min(group["ends"] + [end]) <= 2):
                group["starts"].append(start)
                group["ends"].append(end)
                group["bars"].append(rect)
                break
        else:
            groups.append({"vertical": vertical, "starts": [start], "ends": [end], "bars": [rect]})
    regions = []
    for group in groups:
        # Repeated painting of the same line does not create twenty bars.
        positions = {round((bar.x0 + bar.x1) / 2 if group["vertical"]
                           else (bar.y0 + bar.y1) / 2, 2) for bar in group["bars"]}
        if len(positions) < 20:
            continue
        rect = fitz.Rect(group["bars"][0])
        for bar in group["bars"][1:]:
            rect |= bar
        regions.append(rect)
    return regions


def _image_signature(page, info):
    """Compare decoded image and alpha samples, rather than PDF xref numbers."""
    xref = info.get("xref")
    if not xref or page.parent.xref_get_key(xref, "Mask")[0] != "null":
        return None
    image = page.parent.extract_image(xref)
    smask = image.get("smask", 0)
    mask = None
    if smask:
        pix = fitz.Pixmap(page.parent, smask)
        mask = (pix.width, pix.height, pix.n, pix.alpha, pix.digest)
    return (info["digest"], info["width"], info["height"], info["bpc"],
            info["colorspace"], info["cs-name"], bool(info.get("has-mask")), mask)


def _image_occurrence(page, region):
    matches = [info for info in page.get_image_info(hashes=True, xrefs=True)
               if fitz.Rect(info["bbox"]) == fitz.Rect(region)]
    if len(matches) != 1:
        return None
    info = matches[0]
    a, b, c, d, _x, _y = info["transform"]
    if not info.get("xref") or a <= 0 or d <= 0 or abs(b) > 1e-6 or abs(c) > 1e-6:
        return None
    # Nested Form transforms / clipping cannot be reproduced by insert_image.
    if not any(item[0] == info["xref"] and item[-1] == 0 for item in page.get_images(full=True)):
        return None
    return info


def _pristine_image_pixels(page, info, clip):
    """Render the same PDF image resource alone, including its ICC and smask."""
    with fitz.open() as isolated:
        isolated.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        image_page = isolated[0]
        match = _image_occurrence(image_page, info["bbox"])
        if match is None or match["digest"] != info["digest"]:
            return None
        empty = isolated.get_new_xref()
        isolated.update_object(empty, "<<>>")
        isolated.update_stream(empty, b"")
        image_page.set_contents(empty)
        image_page.insert_image(fitz.Rect(info["bbox"]), xref=match["xref"], keep_proportion=False)
        return image_page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip,
                                    colorspace=fitz.csRGB, alpha=False, annots=False)


def _caption_reveals_original_image(before, after, region, captions, decoded):
    """Allow removed program lettering only if it reveals the original pixels."""
    if (not captions or before.rotation or before.rect.x0 or before.rect.y0
            or before.cropbox != before.mediabox):
        return False
    source_info = _image_occurrence(before, region)
    result_info = _image_occurrence(after, region)
    if (source_info is None or result_info is None
            or source_info["transform"] != result_info["transform"]):
        return False
    signature = _image_signature(before, source_info)
    if signature is None or signature != _image_signature(after, result_info):
        return False
    image_box = fitz.Rect(region)
    approved = [fitz.Rect(box) + (-2, -2, 2, 2) for box in captions
                if image_box.intersects(fitz.Rect(box))]
    if not approved or any(box.intersects(code + (-2, -2, 2, 2))
                           for box in approved for code in decoded):
        return False
    clip = (image_box + (-2, -2, 2, 2)) & before.rect
    original = before.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip,
                                 colorspace=fitz.csRGB, alpha=False, annots=True)
    result = after.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip,
                             colorspace=fitz.csRGB, alpha=False, annots=True)
    pristine = _pristine_image_pixels(before, source_info, clip)
    if pristine is None or any((pix.x, pix.y, pix.width, pix.height, pix.n)
                               != (original.x, original.y, original.width, original.height, original.n)
                               for pix in (result, pristine)):
        return False
    shape = (original.height, original.width, original.n)
    source_pixels, result_pixels, pristine_pixels = [
        np.frombuffer(pix.samples, dtype=np.uint8).reshape(shape).astype(np.int16)
        for pix in (original, result, pristine)]
    xs = (np.arange(original.width) + original.x + .5) / 2
    ys = (np.arange(original.height) + original.y + .5) / 2
    inside_image = ((ys[:, None] >= image_box.y0) & (ys[:, None] <= image_box.y1)
                    & (xs[None, :] >= image_box.x0) & (xs[None, :] <= image_box.x1))
    caption_mask = np.zeros(shape[:2], dtype=bool)
    for box in approved:
        caption_mask |= ((ys[:, None] >= box.y0) & (ys[:, None] <= box.y1)
                         & (xs[None, :] >= box.x0) & (xs[None, :] <= box.x1))
    caption_mask &= inside_image
    if not caption_mask.any():
        return False
    # The source/reference comparison outside the lettering also fails closed
    # for unsupported clipping, blending, or opacity of the original image.
    comparisons = ((source_pixels, result_pixels, ~caption_mask),
                   (source_pixels, pristine_pixels, inside_image & ~caption_mask),
                   (result_pixels, pristine_pixels, caption_mask))
    return all(not mask.any() or np.abs(a[mask] - b[mask]).max() <= 8
               for a, b, mask in comparisons)


def validate_ticket(source, output, engine, removed_targets):
    if len(source) != len(output):
        raise ValidationError("Изменилось число страниц билета.")
    counts = {"readable_codes": 0, "code_regions": 0, "fields": 0}
    for pno, (before, after) in enumerate(zip(source, output)):
        if before.rect != after.rect or before.rotation != after.rotation:
            raise ValidationError("Изменились размер или поворот страницы.")
        promos = [fitz.Rect(r) for _xref, r in engine._promo_qr_geom(before)]
        if hasattr(engine, "_fiscal_qr_geom"):
            promos += [fitz.Rect(r) for _xref, r in engine._fiscal_qr_geom(before)]
        allowed_text = [fitz.Rect(r) for _kind, r, _text_value in engine._removal_rects(before)]
        removed = set(removed_targets[pno]) if pno < len(removed_targets) else set()
        lines = engine.field_tags(engine.extract_lines(before))
        _ages, banners = engine._ticketland_pushkin_block(before, lines)
        pk_hits = [l for l in lines if _text(l["text"]) in removed and engine.PUSHKIN_TEXT_RE.search(l.get("text") or "")]
        has_pk = bool(pk_hits or any(engine.PUSHKIN_TEXT_RE.search(l.get("text") or "") for l in lines))
        for item in before.get_image_info(xrefs=True):
            r = fitz.Rect(item["bbox"])
            if item.get("xref") in banners:
                promos.append(r)
            elif (r.y0 < 90 and r.width >= 80 and r.height <= 90 and r.width >= 1.8 * r.height
                  and any(r.intersects(fitz.Rect(l["bbox"]) + (-2, -12, 2, 12)) for l in pk_hits)):
                promos.append(r)
            elif has_pk and 70 <= r.width <= 130 and 15 <= r.height <= 40 and r.width >= 2.8 * r.height:
                promos.append(r)
        result_lines = engine.extract_lines(after)
        captions = [fitz.Rect(line["bbox"]) for line in lines
                    if _text(line["text"]) in removed and engine.PUSHKIN_TEXT_RE.search(line["text"])
                    and not any(_text(candidate["text"]) == _text(line["text"])
                                and fitz.Rect(candidate["bbox"]).intersects(fitz.Rect(line["bbox"]))
                                for candidate in result_lines)]
        afisha = any(engine.AFISHA_SELLER_RE.search(line["text"]) for line in lines)
        purchase = [fitz.Rect(l["bbox"]) for l in lines
                    if afisha and "дата покупки" in l["text"].lower()]
        for line in lines:
            text = _text(line["text"])
            box = fitz.Rect(line["bbox"])
            tags = set(line.get("tags", []))
            important = bool(tags & {"row", "seat", "price", "sector"}) or (
                len(text) < 80 and (DATE_RE.search(text) or re.fullmatch(r"\d{6,}", text)))
            if not important or text in removed or _inside(box, allowed_text):
                continue
            if any(abs(box.x0 - r.x0) < 5 and r.y0 - 1 <= box.y0 <= r.y1 + 26
                   for r in purchase):
                continue
            if not any(_text(candidate["text"]) == text
                       and fitz.Rect(candidate["bbox"]).intersects(box + (-2, -2, 2, 2))
                       for candidate in result_lines):
                raise ValidationError("Не сохранились дата, место, цена или номер билета.")
            counts["fields"] += 1
        decoded = [(value, box) for value, box in _codes(before)
                   if not _inside(box, promos)
                   and not re.search(r"https?://(?:cheques|[^/\s]*ofd\.ru)|\b(?:fn|fp|i)=\d+", str(value), re.I)]
        remaining = Counter(value for value, _box in _codes(after))
        expected = Counter(value for value, _box in decoded)
        if expected - remaining:
            raise ValidationError("Не сохранилось содержимое QR-кода или штрихкода.")
        decoded_regions = [box for _value, box in decoded]
        # Decodable codes are validated by their contents, not by screenshot
        # pixels: removing an overlaid caption or background is legitimate.
        # Visual fallback is reserved for code-like regions we cannot decode.
        regions = [(rect, True) for rect in _raster_code_regions(before) if not _inside(rect, promos)]
        for rect, members in engine._draw_clusters(before):
            if rect.get_area() > before.rect.get_area() * 0.25 or min(rect.width, rect.height) < 12:
                continue
            dark = sum(bool(m.get("fill") and max(m["fill"]) < 0.3) for m in members)
            vector_regions = _parallel_bar_regions(members)
            if len(members) >= 80 and dark >= 40 and 0.75 < rect.width / rect.height < 1.3:
                vector_regions.append(rect)
            regions.extend((box, False) for box in vector_regions if not _inside(box, promos))
        for region, raster in regions:
            if not _visual_same(before, after, region, decoded_regions) and not (
                    raster and _caption_reveals_original_image(before, after, region, captions, decoded_regions)):
                raise ValidationError("Изменилось изображение QR-кода или штрихкода.")
        counts["readable_codes"] += len(decoded)
        counts["code_regions"] += len(regions)
    return counts
