"""Bounded ZIP processing. Entries are handled in memory, never extracted."""
from collections import Counter
import io
from pathlib import PurePosixPath
import re
import stat
import zipfile

import ticket_meta

MAX_FILES = 50
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_OUTPUT_BYTES = 48 * 1024 * 1024


class BatchError(ValueError):
    pass


def trim_memory():
    """Сбрасывает выделенную память обратно ОС (glibc malloc_trim + Python gc)."""
    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def safe_name(name):
    return re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", name).strip(" .")[:180] or "ticket.pdf"


def read_zip(data):
    if len(data) > MAX_PDF_BYTES:
        raise BatchError("ZIP должен быть не больше 20 МБ.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 1000:
                raise BatchError("В ZIP слишком много записей. Оставьте только PDF-билеты.")
            entries = []
            total = 0
            for item in archive.infolist():
                name = item.filename
                path = PurePosixPath(name)
                if (path.is_absolute() or ".." in path.parts or "\\" in name
                        or ":" in name or "\x00" in name
                        or stat.S_ISLNK(item.external_attr >> 16)):
                    raise BatchError("В ZIP обнаружен недопустимый путь или ссылка.")
                if item.is_dir() or not name.lower().endswith(".pdf"):
                    continue
                if item.flag_bits & 1:
                    raise BatchError("ZIP с паролем не поддерживается.")
                if len(entries) >= MAX_FILES or item.file_size > MAX_PDF_BYTES:
                    raise BatchError("В пачке максимум 50 PDF, каждый до 20 МБ.")
                total += item.file_size
                if total > MAX_TOTAL_BYTES or item.file_size > max(item.compress_size, 1) * 300:
                    raise BatchError("Слишком большой распакованный объём ZIP.")
                with archive.open(item) as stream:
                    content = stream.read(MAX_PDF_BYTES + 1)
                if len(content) > MAX_PDF_BYTES:
                    raise BatchError("PDF в ZIP слишком большой.")
                entries.append((safe_name(path.name), content))
            if not entries:
                raise BatchError("В ZIP нет PDF-билетов.")
            return entries
    except BatchError:
        raise
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as error:
        raise BatchError("Не удалось прочитать ZIP.") from error


def _batch_sort_key(item):
    meta = item.get("meta") or {}
    date_val = str(meta.get("date") or "").strip()
    m_d = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", date_val)
    if m_d:
        d, m, y = int(m_d.group(1)), int(m_d.group(2)), int(m_d.group(3))
        if y < 100:
            y += 2000
        date_key = f"{y:04d}-{m:02d}-{d:02d}"
    else:
        date_key = date_val
    time_key = str(meta.get("time") or "").strip()
    title_key = str(meta.get("title") or "").strip().lower()
    theater_key = str(meta.get("theater") or "").strip().lower()
    sector_key = str(meta.get("sector") or "").strip().lower()
    row_val = str(meta.get("row") or "").strip()
    m_row = re.search(r"\d+", row_val)
    row_key = (0, int(m_row.group(0))) if m_row else (1, row_val)
    seats = meta.get("seats") or []
    first_seat = str(seats[0] if seats else "").strip()
    m_seat = re.search(r"\d+", first_seat)
    seat_key = (0, int(m_seat.group(0))) if m_seat else (1, first_seat)
    return (date_key, time_key, title_key, theater_key, sector_key, row_key, seat_key, item.get("orig_index", 0))


def _process_items_impl(items, cleaner, on_progress=None):
    if not items or len(items) > MAX_FILES or sum(len(data) for _name, data in items) > MAX_TOTAL_BYTES:
        raise BatchError("Пачка пуста или превышает лимиты.")
    buf = io.BytesIO()
    taken = set()
    errors = []
    cleaned_entries = []
    good = failed = changed = size = 0

    for index, (name, data) in enumerate(items, 1):
        if on_progress:
            on_progress("item", index, len(items), good, failed)
        name = safe_name(name)
        ok, result, info = cleaner(data)
        if not ok:
            failed += 1
            errors.append(f"ОШИБКА | {name} | {result}")
            if on_progress:
                on_progress("completed", index, len(items), good, failed)
            continue

        meta = ticket_meta.extract_ticket_metadata(result)
        good += 1
        changed += int(info["changed"])
        cleaned_entries.append({
            "orig_name": name,
            "orig_index": index,
            "orig_data": data,
            "result": result,
            "meta": meta,
            "changed": info["changed"],
        })
        if on_progress:
            on_progress("completed", index, len(items), good, failed)

    if on_progress:
        on_progress("pack", len(items), len(items), good, failed)

    sorted_entries = sorted(cleaned_entries, key=_batch_sort_key)
    processed_items = []
    cleaned_items = []

    distinct_events = {
        ticket_meta.get_event_key(entry.get("meta")): entry.get("meta")
        for entry in sorted_entries
        if entry.get("meta") and entry["meta"].get("title")
    }
    use_folders = len(distinct_events) > 1

    event_folders = {}
    taken_folders = set()
    if use_folders:
        for ev_key, meta in distinct_events.items():
            base_folder = ticket_meta.make_event_folder_name(meta)
            folder = base_folder
            suffix = 2
            while folder.casefold() in taken_folders:
                folder = f"{base_folder} ({suffix})"
                suffix += 1
            taken_folders.add(folder.casefold())
            event_folders[ev_key] = folder

        event_counts = Counter(ticket_meta.get_event_key(entry.get("meta")) for entry in sorted_entries)
        event_indices = Counter()
    else:
        num_width = max(2, len(str(len(sorted_entries))))

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for idx, entry in enumerate(sorted_entries, 1):
            clean_name = ticket_meta.make_clean_ticket_filename(entry["meta"], fallback=entry["orig_name"])

            if use_folders:
                ev_key = ticket_meta.get_event_key(entry.get("meta"))
                folder = event_folders.get(ev_key) or "Разное"
                event_indices[ev_key] += 1
                group_idx = event_indices[ev_key]
                group_total = event_counts[ev_key]
                group_width = max(2, len(str(group_total)))
                prefix = f"{group_idx:0{group_width}d}. "
                output_name = f"{folder}/{prefix}{clean_name}"
            else:
                prefix = f"{idx:0{num_width}d}. "
                output_name = prefix + clean_name

            base = output_name[:-4] if output_name.lower().endswith(".pdf") else output_name
            suffix = 2
            while output_name.casefold() in taken:
                output_name = f"{base}_{suffix}.pdf"
                suffix += 1
            taken.add(output_name.casefold())

            size += len(entry["result"])
            if size > MAX_TOTAL_BYTES:
                raise BatchError("Готовая пачка слишком большая; отправьте её частями.")
            archive.writestr(output_name, entry["result"])

            processed_items.append({
                "name": entry["orig_name"],
                "output_name": output_name,
                "meta": entry["meta"],
                "changed": entry["changed"],
            })
            cleaned_items.append({
                "name": entry["orig_name"],
                "output_name": output_name,
                "orig_data": entry["orig_data"],
                "result": entry["result"],
                "meta": entry["meta"],
                "changed": entry["changed"],
            })

        report_text = ticket_meta.format_batch_report_table(processed_items)
        if errors:
            report_text += "\n\nОШИБКИ ПРИ ОБРАБОТКЕ:\n" + "\n".join(errors)
        archive.writestr("report.txt", report_text.encode("utf-8"))

    result = buf.getvalue()
    if len(result) > MAX_OUTPUT_BYTES:
        raise BatchError("Готовый ZIP превышает лимит отправки; разделите пачку.")
    zip_name = ticket_meta.make_batch_zip_filename(processed_items, good)
    return {
        "data": result,
        "ok": good,
        "failed": failed,
        "changed": changed,
        "items_meta": processed_items,
        "cleaned_items": cleaned_items,
        "zip_name": zip_name,
    }


def process_items(items, cleaner, on_progress=None):
    try:
        return _process_items_impl(items, cleaner, on_progress=on_progress)
    finally:
        trim_memory()


def process_zip(data, cleaner, on_progress=None):
    return process_items(read_zip(data), cleaner, on_progress)
