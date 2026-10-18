"""Telegram PDF processing with the same automatic cleanup as Билетная.

The editor engine is loaded from BILETNAYA. Offline tests do not poll Telegram.
"""

import importlib.util
import hashlib
import atexit
import io
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import stat
import zipfile
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo
import sys
import threading
import time
import urllib.parse

import fitz
import requests

import batches
import validation
import ticket_meta
import seat_tools
import eraser_tools
import cryptobot
from progress import Progress, PDF_STEPS, BATCH_STEPS, COLLECT_STEPS
from delivery_cache import DeliveryCache, key_for
from url_delivery import Publisher
from telegram_file_transport import TelegramFileTransport, MetadataAPIError, MetadataUnavailable
from subscriptions import SubscriptionStore, PERIOD
from config import CONFIG
from texts import (BRAND_NAME, BRAND_AVATAR, WELCOME_ANIMATION, FREE_DAILY,
                   tickets_word, FREE_BATCH_TEXT, FREE_BATCH_KB,
                   GREETING, HELP_TEXT, ADMIN_PANEL_TEXT)

# Дневной бесплатный лимит (env BILET_DAILY_FREE) — чтобы копирайты не разошлись с ним.

HERE = os.path.dirname(os.path.abspath(__file__))
# Редактор Билетная: по умолчанию ~/Билетная, но на VDS его кладут куда
# удобнее — путь можно перекрыть переменной окружения.
BILETNAYA = os.environ.get("BILETNAYA") or os.path.expanduser("~/Билетная")

# --- токен ---------------------------------------------------------------
def _load_token():
    env = os.environ.get("BILET_BOT_TOKEN", "").strip()
    if env:
        return env
    f = os.path.join(HERE, ".token")
    if os.path.exists(f):
        with open(f, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return ""


BOT_TOKEN = _load_token()
API = "https://api.telegram.org/bot" + BOT_TOKEN

MAX_BYTES = 20 * 1024 * 1024          # лимит Telegram на скачивание файла ботом
LONG_POLL = 30
STATE_DIR = Path(os.environ.get("BILET_STATE_DIR") or Path(HERE, "state"))
_store = None
_store_lock = threading.Lock()
_http_local = threading.local()
_work_ready = threading.Event()
_publisher = None
_publisher_lock = threading.Lock()
_file_transport = None
_file_transport_lock = threading.Lock()
_last_action = {}
_last_action_lock = threading.Lock()
_pending_broadcast = {}
_broadcast_running = False
_broadcast_lock = threading.Lock()
_last_remind_check = 0.0


def get_file_transport():
    global _file_transport
    with _file_transport_lock:
        if _file_transport is None:
            _file_transport = TelegramFileTransport(BOT_TOKEN)
            atexit.register(_file_transport.close)
        return _file_transport


def get_publisher():
    """The HTTPS reverse proxy is configured separately from the chat bot."""
    base = os.environ.get("BILET_PUBLIC_DELIVERY", "").strip()
    if not base:
        return None
    global _publisher
    with _publisher_lock:
        if _publisher is None:
            _publisher = Publisher(base)
            atexit.register(_publisher.close)
        return _publisher


def http_session():
    """Reuse connections within a thread, never share a mutable Session."""
    if not hasattr(_http_local, "session"):
        _http_local.session = requests.Session()
    return _http_local.session


def close_http_session():
    session = getattr(_http_local, "session", None)
    if session is not None:
        session.close()
        del _http_local.session


def get_store():
    global _store
    with _store_lock:
        if _store is None:
            _store = SubscriptionStore(STATE_DIR / "subscriptions.sqlite3")
            for username, days in CONFIG.get("gift_usernames", {}).items():
                _store.register_invite(username, days, CONFIG["admin_ids"][0])
    return _store


def _user_fio(uid):
    try:
        return get_store().get_user_fio(uid)
    except Exception:
        return None


def _user_settings(uid):
    try:
        return get_store().get_user_settings(uid)
    except Exception:
        return {"fio": None, "state": None, "hide_price": 0, "batch_mode": "zip", "auto_preview": 0}


def _save_doc_token(token, file_id, custom_price=None, hide_price=None, fio=None, seat_info=None, del_texts=None, strip_pushkin=None, uid=None, meta=None):
    try:
        get_store().save_doc_token(token, file_id, custom_price=custom_price, hide_price=hide_price, fio=fio, seat_info=seat_info, del_texts=del_texts, strip_pushkin=strip_pushkin)
        if uid is not None:
            get_store().set_user_last_doc_token(uid, token)
            if meta and isinstance(meta, dict):
                title = meta.get("title")
                dt = f"{meta.get('date', '')} {meta.get('time', '')}".strip()
                row = meta.get("row")
                seats = meta.get("seats") or []
                seat_str = f"Ряд {row}, Место {seats[0]}" if row and seats else (f"Ряд {row}" if row else (f"Место {seats[0]}" if seats else seat_info))
                p = custom_price if custom_price is not None else meta.get("price")
                get_store().add_ticket_history(uid, token, title=title, date_str=dt, seat_str=seat_str, price=p)
    except Exception:
        pass


ADMIN_USERNAMES = {"freedemin", "studio0111"}


def is_admin(user_id, username=None):
    if user_id in CONFIG["admin_ids"]:
        return True
    if username and username.lower() in ADMIN_USERNAMES:
        return True
    return False


def has_access(user_id):
    return is_admin(user_id) or get_store().status(user_id)["active"]


def cooldown_ok(uid, seconds=None):
    """Простой троттлинг перевыпуска билетов: кнопки цены/ФИО/места
    запускают полный рендер PDF, спамить ими нельзя."""
    if seconds is None:
        if os.environ.get("BILET_NO_COOLDOWN") or "unittest" in sys.modules:
            return True
        seconds = float(os.environ.get("BILET_COOLDOWN", "3.0"))
    if seconds <= 0:
        return True
    now = time.monotonic()
    with _last_action_lock:
        last = _last_action.get(uid, 0.0)
        if now - last < seconds:
            return False
        _last_action[uid] = now
        return True


def date_label(timestamp):
    from datetime import datetime
    return datetime.fromtimestamp(timestamp, ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M МСК")


_WELCOME_MEDIA_ID = None  # кеш file_id, чтобы не перезагружать ролик


def _photo_or_none(path):
    try:
        return path.read_bytes() if path.exists() else None
    except OSError:
        return None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("bilet-bot")


# --- движок чистки из Билетной -----------------------------------------
def _load_server():
    """Импортировать server.py редактора как модуль (как это делают его тесты)."""
    path = os.path.join(BILETNAYA, "server.py")
    if not os.path.exists(path):
        raise RuntimeError(f"не найден редактор Билетная: {path}")
    if BILETNAYA not in sys.path:
        sys.path.insert(0, BILETNAYA)
    spec = importlib.util.spec_from_file_location("biletnaya_srv", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


srv = _load_server()
PROCESSING_VERSION = hashlib.sha256(b"".join(Path(path).read_bytes() for path in (
    __file__, Path(HERE, "validation.py"), Path(HERE, "ticket_meta.py"), Path(HERE, "seat_tools.py"), Path(HERE, "eraser_tools.py"), Path(BILETNAYA, "server.py"), Path(BILETNAYA, "raster.py")
)) + BOT_TOKEN.encode()).hexdigest()


def _cached_file(key):
    if key is None:
        return None
    try:
        return DeliveryCache(STATE_DIR / "delivery-cache.sqlite3").get(key)
    except (OSError, sqlite3.Error):
        log.warning("Delivery cache unavailable; using regular processing")
        return None


def _remember_delivery(keys, file_id, caption):
    if not keys or not file_id:
        return
    try:
        DeliveryCache(STATE_DIR / "delivery-cache.sqlite3").put(keys, file_id, caption)
    except (OSError, sqlite3.Error):
        log.warning("Delivered file could not be cached")


def _forget_delivery(keys):
    try:
        DeliveryCache(STATE_DIR / "delivery-cache.sqlite3").forget(keys)
    except (OSError, sqlite3.Error):
        log.warning("Invalid delivery cache entry could not be removed")


def _normalized(text):
    return " ".join(text.split())


def _cleanup_targets(doc):
    """Recognized program/name lines that must disappear, grouped by page."""
    targets = []
    for page in doc:
        lines = srv.field_tags(srv.extract_lines(page))
        payment, buyer = srv._chelny_fields(page, lines)
        family = payment is not None or srv._is_kassa24_family(lines)
        personal = srv._ticketland_name_rows(page, lines)
        if srv._is_intickets_template(page, lines):
            personal = srv._intickets_name_rows(page, lines)
        elif hasattr(srv, "_is_operann_template") and srv._is_operann_template(page, lines):
            personal = srv._operann_name_rows(page, lines)
        elif payment is not None:
            personal = {buyer} if buyer is not None else set()
        elif family:
            personal |= srv._kassir_name_rows(lines)
            personal |= {i for i, line in enumerate(lines)
                         if srv._personal_line_what(line)}
            ages, _banners = srv._ticketland_pushkin_block(page, lines)
            if ages:
                personal |= {i for i, line in enumerate(lines)
                             if srv.EMAIL_RE.search(line["text"])}
        targets.append({_normalized(line["text"]) for i, line in enumerate(lines)
                        if i in personal or srv.PUSHKIN_TEXT_RE.search(line["text"])})
    return targets


def _find_name_slots(source):
    slots = []
    prepared_lines = [srv.field_tags(srv.extract_lines(p)) for p in source]
    for page, lines in zip(source, prepared_lines):
        ticketland_ages, banners = srv._ticketland_pushkin_block(page, lines)
        intickets = srv._is_intickets_template(page, lines)
        intickets_names = srv._intickets_name_rows(page, lines)
        is_operann = hasattr(srv, "_is_operann_template") and srv._is_operann_template(page, lines)
        operann_names = srv._operann_name_rows(page, lines) if is_operann else set()
        payment, buyer = srv._chelny_fields(page, lines)
        family = payment is not None or srv._is_kassa24_family(lines)
        t_names = srv._ticketland_name_rows(page, lines)
        k_rows = srv._kassir_name_rows(lines)
        is_af = any(srv.AFISHA_SELLER_RE.search(l["text"] or "") for l in lines)
        p_slots = []
        for i, l in enumerate(lines):
            if intickets and i not in intickets_names:
                continue
            if is_operann and i not in operann_names:
                continue
            if not (family or is_af or is_operann) and i not in (t_names | intickets_names):
                continue
            if payment is not None and i != buyer:
                continue
            what = srv._personal_line_what(l)
            if is_operann and i in operann_names:
                if not srv.PUSHKIN_TEXT_RE.search(l["text"]) and not re.search(r"в рамках программы", l["text"], re.I):
                    what = "operann"
            elif not what and is_af and 208 <= l["bbox"][1] <= 235 and 150 <= l["bbox"][0] <= 450:
                if not any(k in l["text"].lower() for k in ["заказ", "сектор", "ряд", "место"]):
                    what = "afisha"
            if i in t_names:
                what = "ticketland"
            if i in intickets_names:
                what = "intickets"
            if i in k_rows:
                what = "kassir"
            if not what or "почта" in what:
                continue
            p_slots.append(l)

        name_candidates = [s for s in p_slots if srv.is_name(s["text"])]
        chosen = name_candidates[0] if name_candidates else (p_slots[-1] if p_slots else None)
        if not chosen:
            fallback_candidates = [
                l for l in lines
                if (("name" in (l.get("tags") or [])) or srv.is_name(l["text"]))
                and not l.get("stale")
                and l["bbox"][1] < page.rect.height * 0.85
            ]
            if fallback_candidates:
                chosen = fallback_candidates[0]
        if is_operann:
            order_line = None
            for l in lines:
                if re.search(r"номер заказа", l["text"], re.I):
                    order_line = l
                    break
            right_x = order_line["bbox"][2] if order_line else 540.8
            origin_y = chosen["origin"][1] if chosen else 366.5
            if not chosen:
                chosen = {
                    "text": "",
                    "bbox": [right_x - 120, origin_y - 10, right_x, origin_y + 2],
                    "size": 11.0,
                    "bold": False,
                    "serif": False,
                    "color": 0,
                    "origin": [right_x - 120, origin_y],
                    "font": "PTAstraSans-Regular",
                    "tags": ["name"],
                }
            chosen = dict(chosen)
            chosen["align_right"] = right_x
            chosen["size"] = 11.0
        slots.append(chosen)
    return slots


def replace_price_in_text(old_text, new_price):
    """Replace numeric price in line with new price while preserving prefix/suffix/format."""
    has_space_sep = bool(re.search(r"\d\s+\d{3}", old_text))
    has_decimals = bool(re.search(r"[,.]\d{2}\s*(?:руб|₽|р|rub|$)", old_text, re.I))
    if has_decimals:
        formatted_num = f"{new_price:,.2f}"
    else:
        formatted_num = f"{new_price:,}"
    if has_space_sep or " " in old_text:
        formatted_num = formatted_num.replace(",", " ")
    else:
        formatted_num = formatted_num.replace(",", "")
    m = re.search(r"(\d[\d\s.,]*\d|\d)", old_text)
    if m:
        prefix = old_text[:m.start(1)]
        suffix = old_text[m.end(1):]
        return prefix + formatted_num + suffix
    return f"{formatted_num} руб."


def trim_memory():
    """Сбрасывает выделенную память обратно ОС (glibc malloc_trim + Python gc)."""
    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def clean_ticket_with_report(data, on_progress=None, spectator_fio=None, hide_price=False, custom_price=None, seat_info=None, del_texts=None, strip_pushkin=False):
    """Return (ok, PDF bytes or explanation, cleanup metadata)."""
    try:
        return _clean_ticket_with_report_impl(
            data,
            on_progress=on_progress,
            spectator_fio=spectator_fio,
            hide_price=hide_price,
            custom_price=custom_price,
            seat_info=seat_info,
            del_texts=del_texts,
            strip_pushkin=strip_pushkin,
        )
    finally:
        trim_memory()


def _clean_ticket_with_report_impl(data, on_progress=None, spectator_fio=None, hide_price=False, custom_price=None, seat_info=None, del_texts=None, strip_pushkin=False):
    """Return (ok, PDF bytes or explanation, cleanup metadata)."""
    info = {"changed": False, "removed": 0}
    started = time.monotonic()
    if not data or len(data) > MAX_BYTES:
        return False, "Нужен непустой PDF-файл размером до 20 МБ.", info
    try:
        with fitz.open(stream=data, filetype="pdf") as source:
            if source.needs_pass or len(source) == 0:
                return False, "PDF защищён паролем или в нём нет страниц.", info
            if len(source) > 100 or any(page.rect.get_area() * 4 > 15_000_000 for page in source):
                return False, "Максимум 100 страниц, с обычным размером листа.", info
    except Exception:
        return False, "Не удалось открыть файл как PDF.", info

    try:
        if on_progress:
            on_progress("clean")
        sanitized, ghosts = srv.sanitize_doc_bytes(data)
        with fitz.open(stream=sanitized, filetype="pdf") as source:
            personal_targets = _cleanup_targets(source)
            name_slots = _find_name_slots(source)
            cleaned, report = srv.autoclean_doc(source)
            validation_targets = [set(t) for t in personal_targets]
            price_val = None
            if custom_price is not None:
                try:
                    price_val = int(custom_price)
                except (ValueError, TypeError):
                    price_val = None

            price_replacements = []
            if hide_price:
                for pno, (spage, cpage) in enumerate(zip(source, cleaned)):
                    lines = srv.field_tags(srv.extract_lines(spage))
                    for l in lines:
                        t = l.get("text", "").strip().lower()
                        if ("price" in l.get("tags", []) or srv.PRICE_RE.search(t)
                                or t in {"цена", "цена:", "стоимость", "стоимость:", "номинал", "номинал:"}):
                            validation_targets[pno].add(_normalized(l["text"]))
                            cpage.add_redact_annot(fitz.Rect(l["bbox"]) + (-1, -1, 1, 1), fill=(1, 1, 1))
                    cpage.apply_redactions()
            elif price_val is not None:
                for pno, (spage, cpage) in enumerate(zip(source, cleaned)):
                    lines = srv.field_tags(srv.extract_lines(spage))
                    for l in lines:
                        t_txt = l.get("text", "").strip().lower()
                        tags = set(l.get("tags", []))
                        if re.search(r"\d", l.get("text", "")) and ("price" in tags or srv.PRICE_RE.search(t_txt)):
                            validation_targets[pno].add(_normalized(l["text"]))
                            cpage.add_redact_annot(fitz.Rect(l["bbox"]) + (-1, -1, 1, 1), fill=(1, 1, 1))
                            new_text = replace_price_in_text(l["text"], price_val)
                            price_replacements.append((pno, l["origin"], new_text, l.get("size", 8.0), l.get("color", 0)))
                    cpage.apply_redactions()
        try:
            fio_val = spectator_fio.strip() if spectator_fio and spectator_fio.strip() else None
            fio_applied = False
            if fio_val:
                for pno, (page, slot) in enumerate(zip(cleaned, name_slots)):
                    if slot:
                        origin = slot["origin"]
                        size = slot.get("size", 8.0)
                        color = srv.int_to_rgb(slot.get("color", 0))
                        text_to_write = fio_val.upper() if slot.get("text", "").isupper() else fio_val
                        if slot.get("align_right"):
                            text_w = srv.FONT_REG.text_length(text_to_write, fontsize=size)
                            origin = (slot["align_right"] - text_w, origin[1])
                        writer = fitz.TextWriter(page.rect)
                        writer.append(origin, text_to_write, font=srv.FONT_REG, fontsize=size)
                        writer.write_text(page, color=color)
                        fio_applied = True
                    elif any(srv.AFISHA_SELLER_RE.search(l["text"] or "") for l in srv.extract_lines(page)):
                        writer = fitz.TextWriter(page.rect)
                        writer.append((155, 222), f"Зритель: {fio_val}", font=srv.FONT_REG, fontsize=8.0)
                        writer.write_text(page, color=(0, 0, 0))
                        fio_applied = True

            if price_replacements:
                for pno, origin, new_text, size, color in price_replacements:
                    page = cleaned[pno]
                    writer = fitz.TextWriter(page.rect)
                    writer.append(origin, new_text, font=srv.FONT_REG, fontsize=size)
                    writer.write_text(page, color=srv.int_to_rgb(color))

            seat_applied = False
            if seat_info:
                ok_seat, seat_edits, replaced_by_pno, seat_err = seat_tools.apply_seat_edits(cleaned, seat_info, srv)
                if ok_seat and seat_edits:
                    seat_applied = True
                    info["seat_info"] = seat_info
                    for pno, old_texts in replaced_by_pno.items():
                        if pno < len(validation_targets):
                            for ot in old_texts:
                                validation_targets[pno].add(_normalized(ot))
                elif seat_err:
                    log.warning("seat replacement warning: %s", seat_err)

            del_applied = False
            if del_texts:
                if isinstance(del_texts, str):
                    targets = [t.strip() for t in del_texts.split("||") if t.strip()]
                else:
                    targets = list(del_texts)
                for dt in targets:
                    h_cnt, rem = eraser_tools.redact_text_from_doc(cleaned, dt, srv=srv)
                    if h_cnt:
                        del_applied = True
                        for pno in range(len(cleaned)):
                            if pno < len(validation_targets):
                                for rm_str in rem:
                                    validation_targets[pno].add(_normalized(rm_str))

            pushkin_applied = False
            if strip_pushkin:
                p_cnt, p_rem = eraser_tools.strip_pushkin_card_from_doc(cleaned, srv=srv)
                if p_cnt:
                    pushkin_applied = True
                    for pno in range(len(cleaned)):
                        if pno < len(validation_targets):
                            for rm_str in p_rem:
                                validation_targets[pno].add(_normalized(rm_str))

            warnings = [warning for item in report for warning in item.get("warn", [])]
            if warnings:
                log.warning("cleanup incomplete: %d engine warnings", len(warnings))
                return False, ("Автоочистка не смогла завершиться полностью. "
                               "Файл не отправлен. "
                               "Свяжись с администратором: /paysupport."), info
            for page, expected in zip(cleaned, personal_targets):
                rem_expected = expected - ({_normalized(fio_val)} if fio_val else set())
                remaining = {_normalized(line["text"]) for line in srv.extract_lines(page)}
                if rem_expected & remaining:
                    log.warning("cleanup incomplete: recognized personal/program lines remain")
                    return False, ("Не удалось полностью удалить распознанные данные. "
                                   "Файл не отправлен."), info
            count = sum(len(item.get("removed", [])) for item in report)
            info["changed"] = bool(ghosts or count or fio_applied or hide_price or (price_val is not None) or seat_applied or del_applied or pushkin_applied)
            info["removed"] = count
            if fio_applied:
                info["spectator_fio"] = fio_val
            if hide_price:
                info["hide_price"] = True
            elif price_val is not None:
                info["custom_price"] = price_val
            if not info["changed"]:
                if on_progress:
                    on_progress("check")
                info["verification"] = {"unchanged": True}
                log.info("PDF unchanged: total=%.3fs", time.monotonic() - started)
                return True, data, info
            buf = io.BytesIO()
            cleaned.save(buf, garbage=3, deflate=True)
            result = buf.getvalue()
            if on_progress:
                on_progress("check")
            verification_started = time.monotonic()
            with fitz.open(stream=sanitized, filetype="pdf") as before, fitz.open(stream=result, filetype="pdf") as after:
                info["verification"] = validation.validate_ticket(before, after, srv, validation_targets)
            log.info("PDF cleaned: %d elements, %d pages; engine=%.3fs verification=%.3fs total=%.3fs",
                     count, len(cleaned), verification_started - started,
                     time.monotonic() - verification_started, time.monotonic() - started)
            return True, result, info
        finally:
            cleaned.close()
    except validation.ValidationError as error:
        log.warning("ticket preservation failed: %s", error)
        return False, "Проверка сохранности не пройдена. Файл не отправлен: " + str(error), info
    except Exception:
        log.exception("PDF cleanup failed")
        return False, "Не удалось обработать PDF. Обработка не завершена.", info


def clean_ticket(data):
    """Compatibility entry point for offline checks."""
    ok, result, _info = clean_ticket_with_report(data)
    return ok, result


# --- Telegram ------------------------------------------------------------
def _safe_error(error):
    message = str(error)
    if BOT_TOKEN:
        message = message.replace(BOT_TOKEN, "<token>")
    # Telegram can echo a failed URL; never persist a bearer ticket link.
    message = re.sub(r"https?://[^\s'\"<>]*?/-bilet-download/[^\s'\"<>]+", "<temporary-file>", message)
    return re.sub(r"/-bilet-download/[^\s'\"<>]+", "<temporary-file>", message)


def tg(method, **data):
    url = f"{API}/{method}"
    for attempt in range(3):
        try:
            r = http_session().post(url, json=data, timeout=5 if method == "answerPreCheckoutQuery" else 60)
            j = r.json()
            if j.get("ok"):
                return j.get("result")
            log.warning("tg %s: %s", method, j.get("description"))
            retry = (j.get("parameters") or {}).get("retry_after")
            if retry and method != "answerPreCheckoutQuery" and attempt < 2:
                time.sleep(min(int(retry), 30))
                continue
            return None
        except requests.RequestException as e:
            log.warning("tg %s: %s (попытка %d)", method, _safe_error(e), attempt + 1)
            if method == "answerPreCheckoutQuery":
                return None
            time.sleep(2 * (attempt + 1))
    return None


def send_text(chat, text, markup=None):
    payload = {"chat_id": chat, "text": text[:4000]}
    if markup:
        payload["reply_markup"] = markup
    return tg("sendMessage", **payload)


def edit_or_send(chat, msg_id, text, markup=None):
    """Edit message text in-place if possible (for text messages),
    otherwise seamlessly send a new message (e.g. when triggered from media captions,
    stale messages, or when edit fails)."""
    if msg_id:
        try:
            payload = {"chat_id": chat, "message_id": msg_id, "text": text[:4000]}
            if markup is not None:
                payload["reply_markup"] = markup
            res = tg("editMessageText", **payload)
            if res:
                return res
        except Exception:
            pass
    return send_text(chat, text, markup=markup)



def progress_request(method, **payload):
    """Single short attempt: progress must not delay checkout or file work."""
    try:
        response = http_session().post(f"{API}/{method}", json=payload, timeout=(3, 4)).json()
        if (not response.get("ok") and response.get("error_code") != 429
                and "message is not modified" not in response.get("description", "")):
            log.warning("progress %s: %s", method, _safe_error(response.get("description", "error")))
        return response
    except (requests.RequestException, ValueError):
        log.warning("progress %s: request failed", method)
        return {}


def file_metadata(file_id):
    """Hedge only read-only metadata; never bypass a Telegram rejection."""
    started = time.monotonic()
    try:
        for attempt in range(2):
            try:
                return get_file_transport().get_file(file_id)
            except MetadataAPIError as error:
                log.warning("getFile rejected: code=%s", error.error_code)
                if error.error_code == 429 and error.retry_after and attempt == 0:
                    time.sleep(error.retry_after)
                    continue
                return None
            except (MetadataUnavailable, ValueError):
                log.warning("getFile unavailable")
                return None
    finally:
        log.info("File metadata: %.3fs", time.monotonic() - started)


def download_file(file_id):
    if not file_id:
        return None
    if isinstance(file_id, str) and (file_id.startswith("/") or file_id.startswith("./") or "\\" in file_id):
        try:
            p = Path(file_id)
            if p.exists() and p.is_file():
                return p.read_bytes()
        except Exception:
            pass
    res = file_metadata(file_id)
    if not res:
        return None
    path = res.get("file_path")
    if not path:
        return None
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}"
    started = time.monotonic()
    size = 0
    try:
        with http_session().get(url, timeout=(3, 45), stream=True) as response:
            if response.status_code != 200:
                return None
            buf = io.BytesIO()
            for chunk in response.iter_content(65536):
                if buf.tell() + len(chunk) > MAX_BYTES:
                    return None
                buf.write(chunk)
                size = buf.tell()
            return buf.getvalue()
    except requests.RequestException as e:
        log.warning("download: %s", _safe_error(e))
    finally:
        log.info("File body download: %.3fs, %d bytes", time.monotonic() - started, size)
    return None


def send_cleaned(chat, data, name, caption, cache_keys=(), reuse_id=None, reply_markup=None):
    """Return delivered file_id (or True) on delivery, None on invalid reusable ID, False on failure."""
    started = time.monotonic()
    url = f"{API}/sendDocument"
    payload = {"chat_id": str(chat), "caption": caption[:1000]}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    lease = None
    mode = "file_id" if reuse_id else "upload"
    if reuse_id:
        payload["document"] = reuse_id
    elif len(data) <= MAX_BYTES and not any(ord(c) > 127 for c in name):
        try:
            publisher = get_publisher()
            if publisher is not None:
                lease = publisher.publish(data, name)
                payload["document"] = lease.url
                mode = "url"
        except (OSError, ValueError, RuntimeError):
            log.warning("Temporary delivery unavailable; using upload")
    # Keep large ZIP support: Telegram URL fetches have a lower size limit.
    files = None
    keep_lease = False
    try:
        for attempt in range(3):
            request_started = time.monotonic()
            try:
                if mode != "upload":
                    r = http_session().post(url, json=payload, timeout=(10, 60))
                else:
                    if files is None:
                        files = {"document": (name, io.BytesIO(data),
                            "application/zip" if name.lower().endswith(".zip") else "application/pdf")}
                    files["document"][1].seek(0)
                    post_data = dict(payload)
                    post_data.pop("document", None)
                    post_data["disable_content_type_detection"] = "true"
                    if reply_markup is not None:
                        post_data["reply_markup"] = json.dumps(reply_markup)
                    r = http_session().post(url, files=files, data=post_data, timeout=(10, 240))
                response = r.json()
                log.info("sendDocument response: %.3fs; mode=%s; ok=%s",
                         time.monotonic() - request_started, mode, bool(response.get("ok")))
                if response.get("ok"):
                    message = response.get("result") or {}
                    file_id = (message.get("document") or {}).get("file_id")
                    _remember_delivery(cache_keys, file_id or reuse_id, caption)
                    log.info("File delivered: %.3fs, %d bytes; mode=%s",
                             time.monotonic() - started, len(data), mode)
                    return file_id or True
                description = str(response.get("description", ""))
                log.warning("sendDocument: %s", _safe_error(description))
                invalid_id = (response.get("error_code") == 400 and reuse_id
                              and any(word in description.lower() for word in
                                      ("file identifier", "file_id", "file reference", "file_id_invalid")))
                if invalid_id:
                    _forget_delivery(cache_keys)
                    return None
                # Only an explicit fetch rejection permits a different delivery path.
                rejected_url = (response.get("error_code") == 400 and mode == "url"
                                and any(word in description.lower() for word in
                                        ("failed to get http url", "wrong http url", "wrong type of the web page content")))
                if rejected_url and attempt < 2:
                    lease.close()
                    mode = "upload"
                    continue
                retry = (response.get("parameters") or {}).get("retry_after")
                if response.get("error_code") == 429 and retry and attempt < 2:
                    time.sleep(int(retry))
                    continue
                keep_lease = mode == "url" and response.get("error_code", 500) >= 500
                return False
            except (requests.RequestException, ValueError) as e:
                log.warning("sendDocument: %s (попытка %d)", _safe_error(e), attempt + 1)
                if mode == "url":
                    # An unacknowledged request may still be fetching the PDF.
                    # Retain its lease until expiry, without sending a duplicate.
                    keep_lease = True
                    return False
                time.sleep(3)
        return False
    finally:
        if lease is not None:
            stats = lease.stats()
            log.info("Telegram URL fetch: gets=%d; bytes=%d", stats["gets"], stats["bytes_sent"])
            if not keep_lease:
                lease.close()


def send_photo(chat, data, caption=""):
    """Send JPEG photo to chat."""
    url = f"{API}/sendPhoto"
    payload = {"chat_id": str(chat), "caption": caption[:1000]}
    files = {"photo": ("preview.jpg", io.BytesIO(data), "image/jpeg")}
    try:
        r = http_session().post(url, files=files, data=payload, timeout=(10, 60))
        res = r.json()
        if res.get("ok"):
            return True
        log.warning("sendPhoto: %s", _safe_error(res.get("description", "")))
        return False
    except requests.RequestException as e:
        log.warning("sendPhoto: %s", _safe_error(e))
        return False


def send_media_group_photos(chat, photos_with_captions):
    """Send an album of photos to chat. photos_with_captions is list of (photo_bytes, caption)."""
    if not photos_with_captions:
        return False
    if len(photos_with_captions) == 1:
        p_bytes, cap = photos_with_captions[0]
        return send_photo(chat, p_bytes, caption=cap)

    url = f"{API}/sendMediaGroup"
    media = []
    files = {}
    for idx, (p_bytes, cap) in enumerate(photos_with_captions):
        attach_name = f"photo_{idx}"
        media.append({
            "type": "photo",
            "media": f"attach://{attach_name}",
            "caption": cap[:1000] if cap else "",
        })
        files[attach_name] = (f"{attach_name}.jpg", io.BytesIO(p_bytes), "image/jpeg")

    data = {"chat_id": str(chat), "media": json.dumps(media)}
    try:
        r = http_session().post(url, files=files, data=data, timeout=(10, 60))
        res = r.json()
        if res.get("ok"):
            return True
        log.warning("sendMediaGroup: %s", _safe_error(res.get("description", "")))
    except requests.RequestException as e:
        log.warning("sendMediaGroup: %s", _safe_error(e))

    # Fallback: sequential send_photo
    ok = True
    for p_bytes, cap in photos_with_captions:
        if not send_photo(chat, p_bytes, caption=cap):
            ok = False
    return ok


def render_pdf_preview(pdf_bytes):
    """Render page 0 of a PDF into a 150 DPI JPEG byte string."""
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if len(doc) == 0:
                return None
            page = doc[0]
            pix = page.get_pixmap(dpi=150, alpha=False)
            data = pix.tobytes("jpeg")
            pix = None
            return data
    except Exception as e:
        log.warning("render_pdf_preview failed: %s", _safe_error(e))
        return None
    finally:
        trim_memory()


def merge_batch_pdfs(zip_bytes):
    """Merge all PDF tickets from a batch zip into a single multi-page PDF in RAM.

    Returns (merged_bytes, merged_filename, count, caption).
    """
    entries = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for item in archive.infolist():
            if item.is_dir() or not item.filename.lower().endswith(".pdf"):
                continue
            path = PurePosixPath(item.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in item.filename
                    or ":" in item.filename or "\x00" in item.filename
                    or stat.S_ISLNK(item.external_attr >> 16)):
                continue
            if item.file_size > batches.MAX_PDF_BYTES:
                continue
            entries.append((item.filename, archive.read(item.filename)))

    if not entries:
        return None, None, 0, ""

    entries.sort(key=lambda x: x[0])
    merged_doc = fitz.open()
    items_meta = []
    try:
        for fname, pdf_data in entries:
            with fitz.open(stream=pdf_data, filetype="pdf") as sub_doc:
                merged_doc.insert_pdf(sub_doc)
            meta = ticket_meta.extract_ticket_metadata(pdf_data, srv=srv)
            items_meta.append({"meta": meta, "output_name": fname})

        buf = io.BytesIO()
        merged_doc.save(buf, garbage=3, deflate=True)
        merged_bytes = buf.getvalue()
    finally:
        merged_doc.close()

    count = len(entries)
    merged_filename = ticket_meta.make_merged_pdf_filename(items_meta, count)
    meta_all = ticket_meta.extract_ticket_metadata(merged_bytes, srv=srv)
    caption = ticket_meta.format_single_ticket_caption(meta_all, {"changed": True})
    return merged_bytes, merged_filename, count, caption


def _send_cached_ticket(chat, cached, keys, progress, reply_markup=None):
    progress.update(3, "Этот билет уже проверен. Отправляю готовый PDF без повторной загрузки.")
    sent = send_cleaned(chat, b"", "ticket.pdf", cached["caption"], cache_keys=keys,
                        reuse_id=cached["file_id"], reply_markup=reply_markup)
    if sent:
        progress.finish("Готовый билет отправлен 🩷 Без повторной обработки.")
    elif sent is False:
        _progress_error(progress, chat, "Не удалось отправить готовый билет. Отправьте файл ещё раз.")
    return sent


def subscribe(chat, user_id):
    if is_admin(user_id):
        return send_text(chat, "У вас постоянный администраторский доступ. Оплата не нужна.")
    status = get_store().status(user_id)
    if status["active"]:
        return show_status(chat, user_id)
    existing = status["invoice"]
    if existing and not existing["canceled"]:
        return send_text(chat, "У вас уже есть подписка с автопродлением. Управляйте ею в Telegram или отмените /cancel_subscription перед новой покупкой.")
    invoice = get_store().invoice(user_id, CONFIG["price_stars"])
    url = invoice["url"]
    if not url:
        url = tg("createInvoiceLink", title="ДЕД РЕДАЧИТ: абонемент на месяц",
                 description=f"Дед перепечатывает билеты начисто: 30 дней без лимита билетов. Специальная цена {CONFIG['price_stars']} рублей (скидка до повышения). Автопродление; отмена /cancel_subscription.",
                 payload=invoice["payload"], provider_token="", currency="XTR",
                 prices=[{"label": f"30 дней со скидкой ({CONFIG['price_stars']} ₽)", "amount": invoice["price"]}], subscription_period=PERIOD)
        if not url:
            return send_text(
                chat,
                f"🏷 Абонемент: {CONFIG['price_stars']} рублей за 30 дней безлимита.\n\n"
                f"💳 **Выберите удобный способ оплаты:**\n"
                f"1️⃣ Через CryptoBot (USDT, TON, карты)\n"
                f"2️⃣ Напрямую через админа @FreeDemin (СБП / перевод на карту)",
                {"inline_keyboard": [
                    [{"text": f"💎 Оплатить через CryptoBot — {CONFIG['price_stars']} руб.", "callback_data": "cbpay:create"}],
                    [{"text": "💬 Оплата через админа (@FreeDemin)", "url": "https://t.me/FreeDemin"}],
                    [{"text": "🏠 В главное меню", "callback_data": "welcome:menu"}],
                ]}
            )
        get_store().save_url(invoice["payload"], url)
    return send_text(
        chat,
        f"🏷 **Специальный тариф:** {CONFIG['price_stars']} рублей за 30 дней (вместо 1500 ₽)\n"
        f"⏳ Временная скидка 50% — успейте взять, скоро повышение стоимости!\n\n"
        f"Полный безлимит билетов и пачек, замена ФИО и кодов.\n\n"
        f"💳 **Выберите удобный способ оплаты:**\n"
        f"1️⃣ В боте картой через Telegram\n"
        f"2️⃣ Через CryptoBot (USDT, TON, карты, крипта)\n"
        f"3️⃣ Напрямую через админа @FreeDemin (СБП / карта)",
        {"inline_keyboard": [
            [{"text": f"💳 Оплатить в Telegram — {CONFIG['price_stars']} руб.", "url": url}],
            [{"text": f"💎 Оплатить через CryptoBot — {CONFIG['price_stars']} руб.", "callback_data": "cbpay:create"}],
            [{"text": "💬 Оплата через админа (@FreeDemin)", "url": "https://t.me/FreeDemin"}],
            [{"text": "🏠 В главное меню", "callback_data": "welcome:menu"}],
        ]}
    )


def handle_cryptobot_create(chat, user_id, msg_id=None):
    if is_admin(user_id):
        return send_text(chat, "У вас постоянный доступ администратора. Оплата не нужна.")
    status = get_store().status(user_id)
    if status["active"]:
        return show_status(chat, user_id)
    bot_user = get_bot_username() or "ded_bilet_bot"
    inv = cryptobot.create_invoice(
        user_id,
        amount_rub=CONFIG["price_stars"],
        description=f"ДЕД РЕДАЧИТ: абонемент на 30 дней ({CONFIG['price_stars']} ₽)",
        bot_username=bot_user,
    )
    if not inv or not (inv.get("bot_invoice_url") or inv.get("pay_url")):
        return send_text(chat, "Не удалось создать счёт CryptoBot. Попробуйте оплатить картой в Telegram или напишите @FreeDemin.")
    inv_id = inv["invoice_id"]
    inv_url = inv.get("bot_invoice_url") or inv.get("pay_url")
    text = (
        f"💎 **СЧЁТ CRYPTOBOT · {BRAND_NAME}**\n\n"
        f"Сумма к оплате: **{CONFIG['price_stars']} ₽**\n"
        f"Принимаются: **USDT, TON, BTC, карты, Telegram Wallet**\n"
        f"Срок подписки: **30 дней безлимита**\n\n"
        f"1. Нажмите кнопку **«Оплатить в @CryptoBot»** ниже.\n"
        f"2. После перевода нажмите **«🔄 Проверить оплату»** — доступ включится автоматически!"
    )
    kb = {
        "inline_keyboard": [
            [{"text": f"💎 Оплатить в @CryptoBot ({CONFIG['price_stars']} ₽)", "url": inv_url}],
            [{"text": "🔄 Проверить оплату", "callback_data": f"cbpay:check:{inv_id}"}],
            [{"text": "💬 Оплата через @FreeDemin", "url": "https://t.me/FreeDemin"}],
            [{"text": "⬅️ Назад к тарифам", "callback_data": "welcome:subscribe"}],
        ]
    }
    return edit_or_send(chat, msg_id, text, markup=kb)


def handle_cryptobot_check(chat, user_id, inv_id, msg_id=None):
    inv = cryptobot.check_invoice(inv_id)
    if not inv:
        return send_text(chat, "⚠️ Не удалось связаться с CryptoBot. Пожалуйста, попробуйте снова через пару секунд.")
    status = inv.get("status")
    if status == "paid":
        action_id = f"cb_paid_{inv_id}"
        expires = get_store().grant(user_id, 30, 0, action_id=action_id)
        congrats = (
            f"🎉 **ОПЛАТА ЧЕРЕЗ CRYPTOBOT ПОДТВЕРЖДЕНА!**\n\n"
            f"Абонемент активирован на 30 дней (до {date_label(expires)}).\n"
            f"Вам открыт полный безлимит билетов и пачек. Приятной работы ✍️"
        )
        markup = {"inline_keyboard": [
            [{"text": "🎟 Мой абонемент", "callback_data": "welcome:status"}],
            [{"text": "🏠 В главное меню", "callback_data": "welcome:menu"}],
        ]}
        return edit_or_send(chat, msg_id, congrats, markup=markup)
    elif status == "active":
        return send_text(chat, "⏳ Оплата ещё не поступила. Завершите перевод в @CryptoBot и нажмите «Проверить оплату» снова.")
    else:
        return send_text(chat, f"Счёт находится в статусе «{status}». Если оплата списалась, свяжитесь с администратором @FreeDemin.")



_BOT_USERNAME = None


def get_bot_username():
    global _BOT_USERNAME
    if _BOT_USERNAME:
        return _BOT_USERNAME
    try:
        me = tg("getMe")
        if me and isinstance(me, dict) and me.get("username"):
            _BOT_USERNAME = me["username"]
            return _BOT_USERNAME
    except Exception:
        pass
    return "ded_bilet_bot"


def build_subscription_card(user_id):
    if is_admin(user_id):
        text = (
            f"👑 **СТАТУС ПОДПИСКИ · {BRAND_NAME}**\n\n"
            f"🛠 **Доступ: Администраторский, постоянный**\n"
            f"⚡️ Безлимитная перепечатка билетов и пачек\n"
            f"Оплата и подписка не требуются."
        )
        kb = [
            [{"text": "👑 Панель владельца", "callback_data": "welcome:admin"}],
            [{"text": "🏠 В главное меню", "callback_data": "welcome:menu"}],
        ]
        return text, {"inline_keyboard": kb}

    status = get_store().status(user_id)
    now = time.time()
    kb = []

    if status["active"]:
        expires = status["expires"]
        rem_sec = max(0, int(expires - now))
        days = rem_sec // 86400
        hours = (rem_sec % 86400) // 3600

        total_days = 30
        used_days = min(total_days, max(0, total_days - days))
        filled = int((used_days / total_days) * 10)
        bar = "🟩" * (10 - filled) + "⬜️" * filled

        invoice = status.get("invoice")
        auto_renew = invoice and not invoice.get("canceled") and invoice.get("initial_charge")
        renew_str = "✅ Включено (списание каждые 30 дней)" if auto_renew else "❌ Выключено (разовый доступ)"

        text = (
            f"🎟 **СТАТУС ПОДПИСКИ · {BRAND_NAME}**\n\n"
            f"⚡️ **Абонемент: АКТИВЕН**\n"
            f"📅 Действует до: **{date_label(expires)}**\n"
            f"⏳ Осталось: **{days} дн. {hours} ч.**\n\n"
            f"Срок: {bar}\n\n"
            f"📦 Пакетная обработка: **без ограничений**\n"
            f"🎫 Дневной лимит: **безлимитно**\n"
            f"🔄 Автопродление: **{renew_str}**"
        )
        kb.append([
            {"text": f"✍️ Продлить за {CONFIG['price_stars']} руб. (скидка)", "callback_data": "welcome:subscribe"},
            {"text": "💎 CryptoBot", "callback_data": "cbpay:create"},
        ])
        kb.append([
            {"text": "💬 Оплата через @FreeDemin", "url": "https://t.me/FreeDemin"},
            {"text": "🎟 Ввести промокод", "callback_data": "welcome:promo"},
        ])
        kb.append([
            {"text": "👤 Профиль", "callback_data": "welcome:profile"},
            {"text": "🏠 В главное меню", "callback_data": "welcome:menu"},
        ])
        if auto_renew:
            kb.append([{"text": "❌ Отключить автопродление", "callback_data": "sub:cancel"}])
    else:
        left = get_store().daily_left(user_id)
        text = (
            f"🎟 **АБОНЕМЕНТ · {BRAND_NAME}**\n\n"
            f"Статус: **не активен**\n"
            f"🎁 Бесплатно сегодня: **{left} из {FREE_DAILY} {tickets_word(FREE_DAILY)}**\n\n"
            f"⚡️ **Что даёт абонемент:**\n"
            f"• Полный безлимит на перепечатку любых билетов\n"
            f"• Пачки любого объёма (ZIP или единый склеенный PDF)\n"
            f"• Смена ФИО, цен и рассадка зрителей по местам\n"
            f"• Моментальное фото До/После и авто-превью\n"
            f"• Сохранение персональных шаблонов настроек\n\n"
            f"🏷 **Специальный тариф:** {CONFIG['price_stars']} рублей / 30 дней (вместо 1500 ₽)\n"
            f"⏳ Временная скидка 50% — успейте взять, скоро повышение стоимости!\n"
            f"💬 Оплата: в боте, через CryptoBot или напрямую через @FreeDemin."
        )
        kb.append([
            {"text": f"✍️ Оформить за {CONFIG['price_stars']} руб. (скидка)", "callback_data": "welcome:subscribe"},
            {"text": "🎁 Пробный день", "callback_data": "welcome:trial"},
        ])
        kb.append([
            {"text": "💎 Оплата через CryptoBot", "callback_data": "cbpay:create"},
            {"text": "💬 Оплата через @FreeDemin", "url": "https://t.me/FreeDemin"},
        ])
        kb.append([
            {"text": "🎟 Ввести промокод", "callback_data": "welcome:promo"},
            {"text": "🏠 В главное меню", "callback_data": "welcome:menu"},
        ])


    return text, {"inline_keyboard": kb}


def build_referral_card(user_id):
    bot_user = get_bot_username()
    ref_link = f"https://t.me/{bot_user}?start=ref_{user_id}"
    stats = get_store().get_referral_stats(user_id)
    share_text = f"Перепечатывай билеты без чужих ФИО и чар Черномора: https://t.me/{bot_user}?start=ref_{user_id}"
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(share_text)}"

    text = (
        f"🎁 **ПРИГЛАСИ КОЛЛЕГУ И ПОЛУЧИ +3 ДНЯ**\n\n"
        f"Поделитесь ботом с коллегами, организаторами или зрителями:\n"
        f"• Вы получаете: **+3 дня полного доступа** за каждого приглашённого\n"
        f"• Ваш коллега получает: **+1 день доступа** в подарок при старте\n\n"
        f"🔗 **Ваша персональная ссылка:**\n"
        f"`{ref_link}`\n\n"
        f"📊 **Ваша статистика:**\n"
        f"• Приглашено друзей: **{stats['count']}**\n"
        f"• Заработано дней доступа: **+{stats['days_earned']} дн.**"
    )
    kb = {
        "inline_keyboard": [
            [{"text": "↗️ Поделиться ссылкой", "url": share_url}],
            [{"text": "🎟 Мой абонемент", "callback_data": "welcome:status"},
             {"text": "👤 В профиль", "callback_data": "welcome:profile"}],
            [{"text": "⬅️ В главное меню", "callback_data": "welcome:menu"}],
        ]
    }
    return text, kb


def build_history_card(user_id):
    items = get_store().get_ticket_history(user_id, limit=8)
    if not items:
        text = (
            f"🕒 **История обработанных билетов**\n\n"
            f"Вы пока не перепечатывали билеты через бота.\n"
            f"Отправьте PDF-билет в чат — дед сохранит его в историю, "
            f"чтобы вы могли вернуться к нему в любой момент!"
        )
        kb = {"inline_keyboard": [
            [{"text": "👤 В профиль", "callback_data": "welcome:profile"},
             {"text": "⬅️ В главное меню", "callback_data": "welcome:menu"}]
        ]}
        return text, kb

    lines = ["🕒 **Недавние билеты:**\n"]
    kb_rows = []
    btn_row = []

    for i, it in enumerate(items, 1):
        title = it.get("title") or "Билет"
        dt = it.get("date_str") or ""
        seat = it.get("seat_str") or ""
        price = f"{it['price']} ₽" if it.get("price") is not None else ""
        desc_parts = [p for p in [dt, seat, price] if p]
        desc = f" ({', '.join(desc_parts)})" if desc_parts else ""
        lines.append(f"{i}. 🎭 **{title}**{desc}")

        btn_row.append({"text": f"Билет №{i}", "callback_data": f"hist:open:{it['token']}"})
        if len(btn_row) == 4:
            kb_rows.append(btn_row)
            btn_row = []

    if btn_row:
        kb_rows.append(btn_row)

    kb_rows.append([
        {"text": "👤 В профиль", "callback_data": "welcome:profile"},
        {"text": "⬅️ В главное меню", "callback_data": "welcome:menu"}
    ])
    return "\n".join(lines), {"inline_keyboard": kb_rows}


def show_status(chat, user_id):
    text, kb = build_subscription_card(user_id)
    return send_text(chat, text, markup=kb)


def get_main_markup(user_id=None, username=None):
    rows = [
        [{"text": "✍️ Оформить абонемент", "callback_data": "welcome:subscribe"},
         {"text": "🎁 Пробный день", "callback_data": "welcome:trial"}],
        [{"text": "⚙️ Настройки (ФИО / цена)", "callback_data": "welcome:settings"},
         {"text": "📂 Шаблоны", "callback_data": "tpl:menu"}],
        [{"text": "🎟 Промокод", "callback_data": "welcome:promo"},
         {"text": "👤 Профиль", "callback_data": "welcome:profile"}],
        [{"text": "📖 Как пользоваться", "callback_data": "welcome:help"}],
    ]
    if user_id and is_admin(user_id, username):
        rows.append([{"text": "👑 Панель владельца", "callback_data": "welcome:admin"}])
    return {"inline_keyboard": rows}


MAIN_MARKUP = get_main_markup()


def send_help(chat, user_id=None, username=None):
    return send_text(chat, HELP_TEXT, markup=get_main_markup(user_id, username))


def _send_brand_media(chat, path, kind, caption, markup):
    """Send brand media by cached file_id, upload as fallback. Return bool."""
    global _WELCOME_MEDIA_ID
    payload = {"chat_id": str(chat), "caption": caption[:1000], "reply_markup": json.dumps(markup)}
    field = "animation" if kind == "video" else "photo"
    method = "sendAnimation" if kind == "video" else "sendPhoto"
    if _WELCOME_MEDIA_ID:
        try:
            r = http_session().post(f"{API}/{method}", json={**payload, field: _WELCOME_MEDIA_ID}, timeout=(10, 60))
            if r.json().get("ok"):
                return True
            _WELCOME_MEDIA_ID = None
        except (requests.RequestException, ValueError) as e:
            log.warning("brand media by id: %s", _safe_error(e))
    try:
        files = {field: (path.name, io.BytesIO(path.read_bytes()),
                         "video/mp4" if kind == "video" else "image/jpeg")}
        thumb_path = Path(HERE, "brand", "thumb.jpg")
        if kind == "video" and thumb_path.exists():
            files["thumbnail"] = ("thumb.jpg", io.BytesIO(thumb_path.read_bytes()), "image/jpeg")
        r = http_session().post(f"{API}/{method}", files=files, data=payload, timeout=(10, 120))
        res = r.json()
        if res.get("ok"):
            result = res.get("result") or {}
            if kind == "video":
                new_id = (result.get("animation") or result.get("video") or {}).get("file_id")
            else:
                photos = result.get("photo") or []
                new_id = photos[-1].get("file_id") if photos else None
            if new_id:
                _WELCOME_MEDIA_ID = new_id
            return True
        log.warning("sendBrandMedia: %s", _safe_error(res.get("description", "")))
        return False
    except (requests.RequestException, ValueError) as e:
        log.warning("sendBrandMedia upload: %s", _safe_error(e))
        return False


def send_welcome(chat, user_id=None, username=None):
    markup = get_main_markup(user_id, username)
    if WELCOME_ANIMATION.exists() and _send_brand_media(chat, WELCOME_ANIMATION, "video", GREETING, markup):
        return True
    photo = _photo_or_none(WELCOME_IMAGE)
    if photo:
        payload = {"chat_id": str(chat), "caption": GREETING[:1000], "reply_markup": json.dumps(markup)}
        try:
            r = http_session().post(f"{API}/sendPhoto",
                                    files={"photo": ("welcome.jpg", io.BytesIO(photo), "image/jpeg")},
                                    data=payload, timeout=(10, 60))
            if r.json().get("ok"):
                return True
        except (requests.RequestException, ValueError) as e:
            log.warning("sendPhoto welcome: %s", _safe_error(e))
    return send_text(chat, GREETING, markup=markup)



def send_profile(chat, user_id, username=None):
    try:
        status = get_store().status(user_id)
        info = get_store().user_info(user_id)
        settings = _user_settings(user_id)
    except Exception:
        return send_text(chat, "Профиль временно недоступен. Попробуйте позже.")
    if status["active"]:
        access = ("🎟 Доступ: активен до " + date_label(status["expires"])
                  + f" · осталось {max(0, int(status['expires'] - time.time()) // 86400)} дн.")
    elif is_admin(user_id):
        access = "🛠 Доступ: администраторский, постоянный"
    else:
        access = "🎟 Доступ: не активен — /subscribe или /promo КОД"
    fio = settings.get("fio")
    price = settings.get("custom_price")
    hide = bool(settings.get("hide_price", 0))
    price_disp = f"{price} ₽" if price is not None else "исходная (/price)"
    try:
        free_line = f"🎁 Бесплатно сегодня: {get_store().daily_left(user_id)} из {FREE_DAILY} {tickets_word(FREE_DAILY)}\n"
    except Exception:
        free_line = ""
    text = (
        f"👤 ПРОФИЛЬ · {BRAND_NAME}\n\n"
        f"🆔 Telegram ID: {user_id}\n"
        f"{access}\n"
        f"{free_line}"
        f"🎫 Билетов перепечатано: {info.get('processed', 0)}\n"
    )
    if info.get("first_seen"):
        text += f"📅 В машинке с: {date_label(info['first_seen'])}\n"
    text += (
        "\n⚙️ Настройки:\n"
        f"• ФИО зрителя: {fio or '— не задано (/fio)'}\n"
        f"• Цена на билете: {price_disp}\n"
        f"• Отображение цены: {'скрыта 🎁' if hide else 'показывается'}\n"
    )
    markup_rows = []
    if not status["active"] and not is_admin(user_id, username):
        markup_rows.append([
            {"text": "✍️ Оформить абонемент", "callback_data": "welcome:subscribe"},
            {"text": "🎁 Пробный день", "callback_data": "welcome:trial"},
        ])
    markup_rows.append([
        {"text": "🎟 Мой абонемент", "callback_data": "welcome:status"},
        {"text": "💬 Оплата через @FreeDemin", "url": "https://t.me/FreeDemin"},
    ])
    markup_rows.append([
        {"text": "🕒 История билетов", "callback_data": "history:menu"},
        {"text": "📂 Шаблоны настроек", "callback_data": "tpl:menu"},
    ])
    markup_rows.append([
        {"text": "⚙️ Настройки печати", "callback_data": "welcome:settings"},
        {"text": "📖 Помощь", "callback_data": "welcome:help"},
    ])
    if is_admin(user_id, username):
        markup_rows.append([
            {"text": "👑 Панель владельца", "callback_data": "welcome:admin"},
        ])
    markup_rows.append([
        {"text": "🏠 В главное меню", "callback_data": "welcome:menu"},
    ])
    markup = {"inline_keyboard": markup_rows}
    photo = _photo_or_none(BRAND_AVATAR)
    if photo:
        payload = {"chat_id": str(chat), "caption": text[:1000], "reply_markup": json.dumps(markup)}
        try:
            r = http_session().post(f"{API}/sendPhoto",
                                    files={"photo": ("ded.jpg", io.BytesIO(photo), "image/jpeg")},
                                    data=payload, timeout=(10, 60))
            if r.json().get("ok"):
                return True
        except (requests.RequestException, ValueError) as e:
            log.warning("sendPhoto profile: %s", _safe_error(e))
    return send_text(chat, text, markup=markup or None)


def cmd_trial(chat, user_id):
    if is_admin(user_id):
        return send_text(chat, "У вас постоянный администраторский доступ.")
    status = get_store().status(user_id)
    if status["active"]:
        return show_status(chat, user_id)
    try:
        expires, created = get_store().grant_trial(user_id, days=1)
    except Exception:
        return send_text(chat, "Не удалось выдать пробный день. Попробуйте позже.")
    if created:
        return send_text(chat, "🎁 Пробный день активирован! Доступ до " + date_label(expires)
                         + ".\nПришлите PDF — дед начнёт перепечатывать.")
    return send_text(chat, "Пробный день уже использовался. Оформить абонемент: /subscribe.")


def cmd_promo(chat, user_id, arg):
    if not arg:
        return send_text(chat, "🎟 Формат: /promo КОД\nПромокоды выдаёт администратор бота.")
    try:
        expires, days = get_store().promo_redeem(user_id, arg)
    except ValueError as error:
        return send_text(chat, f"🚫 Промокод не принят: {error}")
    except Exception:
        return send_text(chat, "Не удалось активировать промокод. Попробуйте позже.")
    return send_text(chat, f"🎟 Промокод принят: +{days} дн. Доступ до " + date_label(expires) + ".")


def admin_stats_text():
    try:
        s = get_store().admin_stats()
    except Exception:
        return "Статистика временно недоступна."
    return (
        f"📊 СТАТИСТИКА · {BRAND_NAME}\n\n"
        f"Пользователей: {s['users_total']} (+{s['users_week']} за 7 дней)\n"
        f"Доступ активен: {s['active_now']}\n"
        f"Оплачено: {s['paid_stars']} руб. · возвратов: {s['refunded_stars']} руб.\n"
        f"Билетов перепечатано: {s['processed_total']}\n"
        f"Промокодов: {s['promos_active']} активных из {s['promos_total']}\n"
        f"Инцидентов оплат: {s['incidents']}"
    )


def admin_panel_markup():
    return {"inline_keyboard": [
        [{"text": "📊 Статистика", "callback_data": "adm:stats"},
         {"text": "💳 Последние оплаты", "callback_data": "adm:payments"}],
        [{"text": "🎟 Промокоды", "callback_data": "adm:promos"},
         {"text": "⚠️ Инциденты оплат", "callback_data": "adm:incidents"}],
        [{"text": "📣 Как разослать объявление", "callback_data": "adm:broadcast_hint"}],
        [{"text": "🔄 Обновить", "callback_data": "adm:refresh"},
         {"text": "⬅️ В главное меню", "callback_data": "welcome:menu"}],
    ]}


def send_admin_panel(chat, msg_id=None):
    return edit_or_send(chat, msg_id, ADMIN_PANEL_TEXT, markup=admin_panel_markup())



def admin_payments_text():
    rows = get_store().payments()
    if not rows:
        return "Оплат пока нет."
    return "\n".join(f"ID {p['user_id']} | {p['amount']} руб. | {p['charge_id']} | возврат={p['refunded']}" for p in rows)


def admin_promos_text():
    rows = get_store().promo_list()
    if not rows:
        return ("Промокодов ещё нет. Создать:\n/promogen КОД ДНИ АКТИВАЦИЙ\n"
                "Пример: /promogen THEATR2026 7 50 — 7 дней, 50 активаций, годен 30 дней.")
    return "\n".join(
        f"{p['code']}: +{p['days']} дн · {p['used']}/{p['uses_max']} активаций · "
        f"годен до {date_label(p['valid_until'])}" for p in rows)


def broadcast_text(chat, admin_id, message_text):
    """Сначала предпросмотр с подтверждением; сама рассылка — в фоне."""
    body = message_text.strip()
    if not body:
        return send_text(chat, "Формат: /broadcast ТЕКСТ — сообщение придёт всем пользователям бота.")
    recipients = get_store().all_user_ids()
    _pending_broadcast[admin_id] = body
    return send_text(
        chat,
        f"📣 Предпросмотр рассылки для {len(recipients)} получателей:\n\n"
        f"─────\n📣 {body}\n─────\n\nОтправить?",
        {"inline_keyboard": [[{"text": "✅ Отправить всем", "callback_data": "adm:bcastgo"},
                              {"text": "❌ Отмена", "callback_data": "adm:bcastno"}]]})


def _run_broadcast(chat, body):
    """Фоновая рассылка: воркеры очереди не блокируются."""
    global _broadcast_running
    recipients = get_store().all_user_ids()
    delivered = failed = 0
    for index, uid in enumerate(recipients, 1):
        if send_text(uid, "📣 " + body):
            delivered += 1
        else:
            failed += 1
        if index % 20 == 0:
            time.sleep(1)  # Telegram: не более ~30 сообщений в секунду на бота
    with _broadcast_lock:
        _broadcast_running = False
    send_text(chat, f"📣 Рассылка завершена: доставлено {delivered}, не доставлено {failed}.")


def set_my_commands():
    commands = [
        {"command": "start", "description": "Запустить деда"},
        {"command": "help", "description": "Как пользоваться"},
        {"command": "profile", "description": "Профиль и статус доступа"},
        {"command": "subscribe", "description": "Оформить абонемент"},
        {"command": "subscription", "description": "Статус доступа"},
        {"command": "trial", "description": "Бесплатный пробный день"},
        {"command": "promo", "description": "Активировать промокод"},
        {"command": "settings", "description": "Настройки билетов"},
        {"command": "templates", "description": "Шаблоны настроек печати"},
        {"command": "history", "description": "История недавних билетов"},
        {"command": "fio", "description": "ФИО зрителя на билетах"},
        {"command": "price", "description": "Цена на билете"},
        {"command": "seat", "description": "Ряд, место, сектор"},
        {"command": "batch", "description": "Собрать пачку PDF"},
        {"command": "id", "description": "Мой Telegram ID"},
        {"command": "paysupport", "description": "Оплата через админа (@FreeDemin)"},
    ]
    try:
        tg("setMyCommands", commands=commands)
        log.info("command menu published")
    except Exception:
        log.warning("setMyCommands failed", exc_info=True)


def set_my_profile_info():
    price_stars = CONFIG.get("price_stars", 750)
    free_daily = SubscriptionStore.DAILY_LIMIT
    description = (
        "Дед сел за пишущую машинку и перепечатывает билеты заново — аккуратно, быстро и без лишних строк.\n\n"
        "Убирает чужие ФИО, служебные строки и чары Черномора, сохраняя QR-коды, штрихкоды, даты, места и цену.\n\n"
        f"🎁 {free_daily} билета каждый день — бесплатно!\n"
        f"📦 Пачки и полный безлимит — с абонементом ({price_stars} ₽ / 30 дней вместо 1500 ₽)."
    )
    short_description = "Перепечатка билетов начисто: удаление чужих ФИО и чар Черномора. 2 билета в день бесплатно!"
    for lang in ("", "ru"):
        try:
            tg("setMyDescription", description=description, language_code=lang)
            tg("setMyShortDescription", short_description=short_description, language_code=lang)
        except Exception:
            log.warning("setMyDescription failed for lang %r", lang, exc_info=True)


def payment_message(chat, user_id, payment):
    if get_store().apply_payment(user_id, payment):
        send_text(chat, "Оплата подтверждена. Доступ открыт до " + date_label(get_store().status(user_id)["expires"]) + ".")


def answer_checkout(query):
    valid = get_store().checkout_valid((query.get("from") or {}).get("id"), query)
    payload = {"pre_checkout_query_id": query["id"], "ok": valid}
    if not valid:
        payload["error_message"] = "Счёт не соответствует вашему аккаунту или у вас уже есть активный абонемент. Откройте /subscription."
    return tg("answerPreCheckoutQuery", **payload)


def _collection_dir(ident):
    if not re.fullmatch(r"[0-9a-f]{32}", ident):
        raise ValueError("Invalid collection id")
    return STATE_DIR / "spool" / ident


def cleanup_collections():
    for ident in get_store().expired_collections():
        shutil.rmtree(_collection_dir(ident), ignore_errors=True)
        get_store().forget_collection(ident)


def split_pdf_tickets(pdf_bytes, srv=None, hide_price=False):
    """Split a multi-page PDF into individual 1-page PDF tickets.

    Returns a list of (filename, pdf_bytes, meta_dict).
    """
    entries = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for i in range(len(doc)):
            single = fitz.open()
            single.insert_pdf(doc, from_page=i, to_page=i)
            buf = io.BytesIO()
            single.save(buf, garbage=3, deflate=True)
            page_data = buf.getvalue()
            single.close()
            meta = ticket_meta.extract_ticket_metadata(page_data, srv=srv)
            name = ticket_meta.make_clean_ticket_filename(meta, fallback=f"ticket_{i+1}.pdf", hide_price=hide_price)
            entries.append((name, page_data, meta))
    return entries


BUILTIN_PRESETS = {
    "gift": {
        "name": "🎁 Подарочный",
        "hide_price": 1,
        "auto_preview": 1,
        "fio": None,
        "custom_price": None,
        "batch_mode": "zip",
        "desc": "Скрыть цену, авто-превью вкл, пачки в ZIP",
    },
    "std": {
        "name": "💼 Стандарт",
        "hide_price": 0,
        "auto_preview": 0,
        "fio": None,
        "custom_price": None,
        "batch_mode": "zip",
        "desc": "Цена исходная, превью выкл, пачки в ZIP",
    },
    "print": {
        "name": "📑 Для печати",
        "hide_price": 0,
        "auto_preview": 1,
        "fio": None,
        "custom_price": None,
        "batch_mode": "merge",
        "desc": "Цена исходная, авто-превью вкл, пачки склеены в PDF",
    },
    "price2500": {
        "name": "💎 Своя цена 2500 ₽",
        "hide_price": 0,
        "auto_preview": 0,
        "fio": None,
        "custom_price": 2500,
        "batch_mode": "zip",
        "desc": "Цена 2500 ₽, превью выкл, пачки в ZIP",
    },
}


def _apply_preset_to_user(uid, preset):
    store = get_store()
    if "hide_price" in preset and preset["hide_price"] is not None:
        store.update_user_setting(uid, "hide_price", int(bool(preset["hide_price"])))
    if "batch_mode" in preset and preset["batch_mode"] is not None:
        store.update_user_setting(uid, "batch_mode", preset["batch_mode"])
    if "auto_preview" in preset and preset["auto_preview"] is not None:
        store.update_user_setting(uid, "auto_preview", int(bool(preset["auto_preview"])))
    if "custom_price" in preset:
        store.update_user_setting(uid, "custom_price", preset["custom_price"])
    if "fio" in preset and preset["fio"] is not None:
        store.update_user_setting(uid, "fio", preset["fio"])


def _apply_preset_to_batch(batch_token, preset):
    session = get_store().get_batch_session(batch_token)
    if not session:
        return 0
    items = session.get("items", [])
    count = 0
    for idx in range(len(items)):
        kwargs = {}
        if "hide_price" in preset and preset["hide_price"] is not None:
            kwargs["hide_price"] = bool(preset["hide_price"])
        if "custom_price" in preset and preset["custom_price"] is not None:
            kwargs["custom_price"] = preset["custom_price"]
        elif "custom_price" in preset and preset["custom_price"] is None and not preset.get("hide_price"):
            kwargs["custom_price"] = "сброс"
        if "fio" in preset and preset["fio"]:
            kwargs["fio"] = preset["fio"]
        ok, _ = _reprocess_batch_item(batch_token, idx, **kwargs)
        if ok:
            count += 1
    return count


def build_templates_card(uid, target_batch_token=None):
    store = get_store()
    custom_presets = store.get_user_presets(uid)

    if target_batch_token:
        title = "📂 **Шаблоны настроек для пачки**\n\n"
        subtitle = "Выберите шаблон, чтобы применить его ко всем билетам в этой пачке:\n\n"
        prefix = f"btpl:apply:{target_batch_token}:"
    else:
        title = "📂 **Шаблоны настроек печати**\n\n"
        subtitle = "Выберите готовый шаблон или сохраните свои текущие настройки:\n\n"
        prefix = "tpl:apply:"

    body_parts = [title, subtitle, "⚡️ **Готовые шаблоны:**\n"]
    for key, p in BUILTIN_PRESETS.items():
        body_parts.append(f"• **{p['name']}** — {p['desc']}\n")

    if custom_presets:
        body_parts.append("\n⭐ **Ваши сохранённые шаблоны:**\n")
        for p in custom_presets:
            details = []
            if p.get("hide_price"):
                details.append("скрыть цену")
            elif p.get("custom_price") is not None:
                details.append(f"{p['custom_price']} ₽")
            if p.get("fio"):
                details.append(f"ФИО: {p['fio']}")
            if p.get("batch_mode"):
                details.append(f"пачки: {p['batch_mode']}")
            if p.get("auto_preview"):
                details.append("превью")
            det_str = f" ({', '.join(details)})" if details else ""
            body_parts.append(f"• **{p['name']}**{det_str}\n")
    else:
        body_parts.append("\n_(У вас пока нет своих сохранённых шаблонов)_\n")

    text = "".join(body_parts)

    kb = []
    # Built-in buttons
    builtins_row1 = [
        {"text": BUILTIN_PRESETS["gift"]["name"], "callback_data": f"{prefix}builtin_gift"},
        {"text": BUILTIN_PRESETS["std"]["name"], "callback_data": f"{prefix}builtin_std"},
    ]
    builtins_row2 = [
        {"text": BUILTIN_PRESETS["print"]["name"], "callback_data": f"{prefix}builtin_print"},
        {"text": BUILTIN_PRESETS["price2500"]["name"], "callback_data": f"{prefix}builtin_price2500"},
    ]
    kb.append(builtins_row1)
    kb.append(builtins_row2)

    # Custom preset buttons (up to 2 per row)
    if custom_presets:
        row = []
        for p in custom_presets:
            btn_text = f"⭐ {p['name'][:18]}"
            row.append({"text": btn_text, "callback_data": f"{prefix}custom_{p['id']}"})
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)

    if target_batch_token:
        kb.append([{"text": "⬅️ Вернуться к пачке", "callback_data": f"bcfg:{target_batch_token}"}])
    else:
        action_row = [
            {"text": "💾 Сохранить текущие как шаблон", "callback_data": "tpl:save"},
        ]
        if custom_presets:
            action_row.append({"text": "🗑 Удалить шаблон", "callback_data": "tpl:del_menu"})
        kb.append(action_row)
        kb.append([
            {"text": "⚙️ Назад в настройки", "callback_data": "welcome:settings"},
            {"text": "⬅️ В главное меню", "callback_data": "welcome:menu"},
        ])

    return text, {"inline_keyboard": kb}


def build_settings_card(uid):
    settings = _user_settings(uid)
    fio = settings.get("fio") or "не установлено (очищается)"
    hide_price = bool(settings.get("hide_price", 0))
    custom_price = settings.get("custom_price")
    custom_price_display = f"{custom_price} ₽" if custom_price is not None else "исходная"
    batch_mode = settings.get("batch_mode") or "zip"
    auto_preview = bool(settings.get("auto_preview", 0))

    batch_labels = {
        "zip": "🗂 ZIP-архив",
        "merge": "📑 Склеенный PDF",
        "separate": "📄 По отдельности",
    }
    batch_display = batch_labels.get(batch_mode, "🗂 ZIP-архив")
    price_display = "🎁 Скрывать (Подарочный режим)" if hide_price else "✅ Показывать"
    preview_display = "⚡️ Включено" if auto_preview else "❌ Выключено"

    text = (
        "⚙️ Настройки обработки билетов:\n\n"
        f"👤 ФИО зрителя: {fio}\n"
        f"💰 Кастомная цена: {custom_price_display}\n"
        f"🎁 Отображение цены: {price_display}\n"
        f"📦 Формат пачек: {batch_display}\n"
        f"🖼 Авто-превью: {preview_display}\n\n"
        "Нажмите кнопку ниже, чтобы переключить параметр:"
    )

    price_btn = "🎁 Скрыть цену" if not hide_price else "💰 Показать цену"
    preview_btn = "🖼 Превью: ⚡️ Вкл" if auto_preview else "🖼 Превью: ❌ Выкл"
    batch_btn = f"📦 Пачки: {batch_display}"

    reply_markup = {
        "inline_keyboard": [
            [{"text": "👤 Изменить ФИО", "callback_data": "set:fio"}, {"text": "✏️ Изменить цену", "callback_data": "set:custom_price"}],
            [{"text": price_btn, "callback_data": "set:price"}],
            [{"text": batch_btn, "callback_data": "set:batch"}],
            [{"text": preview_btn, "callback_data": "set:preview"}],
            [{"text": "📂 Шаблоны настроек", "callback_data": "tpl:menu"}],
            [{"text": "🔄 Сбросить настройки", "callback_data": "set:reset"}],
            [{"text": "⬅️ В главное меню", "callback_data": "welcome:menu"}],
        ]
    }
    return text, reply_markup


def _sector_keyboard(token):
    return {
        "inline_keyboard": [
            [{"text": "🎭 Партер", "callback_data": f"choose_sec:{token}:Партер"},
             {"text": "🏛 Бельэтаж", "callback_data": f"choose_sec:{token}:Бельэтаж"}],
            [{"text": "🏰 Балкон", "callback_data": f"choose_sec:{token}:Балкон"},
             {"text": "🎪 Амфитеатр", "callback_data": f"choose_sec:{token}:Амфитеатр"}],
            [{"text": "🚪 Ложа", "callback_data": f"choose_sec:{token}:Ложа"},
             {"text": "🎩 Бенуар", "callback_data": f"choose_sec:{token}:Бенуар"}],
            [{"text": "🏟 Трибуна", "callback_data": f"choose_sec:{token}:Трибуна"},
             {"text": "🌌 Галерея", "callback_data": f"choose_sec:{token}:Галерея"}],
        ]
    }


def _single_ticket_markup(token, hide_price=False, pages=1, batch_token=None):
    quick_btn = {"text": "⚡️ Быстрая правка (всё в 1 строке)", "callback_data": f"quick:{token}"}
    seat_btn = {"text": "🪑 Изменить место/ряд", "callback_data": f"seat:{token}"}
    fio_btn = {"text": "👤 Изменить ФИО", "callback_data": f"fio:{token}"}
    price_edit_btn = {"text": "✏️ Изменить цену", "callback_data": f"price:{token}"}
    if hide_price:
        price_toggle_btn = {"text": "💰 Показать цену", "callback_data": f"toggleprice:{token}:0"}
    else:
        price_toggle_btn = {"text": "🎁 Скрыть цену", "callback_data": f"toggleprice:{token}:1"}
    preview_btn = {"text": "🖼 Фото: До и После", "callback_data": f"compare:{token}"}
    pushkin_btn = {"text": "🧹 Снять чары Черномора", "callback_data": f"pushkin:{token}"}

    rows = [
        [quick_btn],
        [seat_btn],
        [fio_btn, price_edit_btn],
        [price_toggle_btn, preview_btn],
        [pushkin_btn],
    ]
    if pages > 1:
        rows.append([{"text": f"✂️ Разбить на билеты ({pages} шт.)", "callback_data": f"split:{token}"}])
        if batch_token:
            rows.append([{"text": "⚙️ Настроить билеты пачки", "callback_data": f"bcfg:{batch_token}"}])
            rows.append([
                {"text": "⚡️ Заполнить всё списком", "callback_data": f"bmass:combo:{batch_token}"},
            ])
            rows.append([
                {"text": "👥 Список ФИО", "callback_data": f"bmass:fio:{batch_token}"},
                {"text": "🪑 Рассадить по порядку", "callback_data": f"bmass:seat:{batch_token}"},
            ])
    return {"inline_keyboard": rows}


def _batch_callbacks(progress, spectator_fio=None, hide_price=False, custom_price=None):
    current = {"index": 0, "total": 0}

    def notify(stage, index, total, good, failed):
        current.update(index=index, total=total)
        progress.batch(index if stage != "item" else index - 1, total, failed)
        if stage == "pack":
            progress.update(2, "Собираю билеты и отчёт в один архив.")
        else:
            progress.update(1, f"Билет {index} из {total} · обрабатываю и проверяю.")

    def cleaner(data):
        def phase(stage):
            label = "убираю лишнее" if stage == "clean" else "проверяю коды и данные"
            progress.update(1, f"Билет {current['index']} из {current['total']} · {label}.")
        return clean_ticket_with_report(data, on_progress=phase, spectator_fio=spectator_fio, hide_price=hide_price, custom_price=custom_price)

    return cleaner, notify


def _progress_error(progress, chat, explanation):
    progress.finish("Файл не отправлен. Причина — в сообщении ниже.", failed=True)
    return send_text(chat, explanation)


def _init_batch_session(uid, chat, result):
    cleaned_items = result.get("cleaned_items")
    if not cleaned_items and result.get("data"):
        try:
            cleaned_items = []
            with zipfile.ZipFile(io.BytesIO(result["data"])) as arc:
                for idx, it in enumerate(arc.infolist()):
                    if not it.is_dir() and it.filename.lower().endswith(".pdf"):
                        pdf_data = arc.read(it.filename)
                        meta = ticket_meta.extract_ticket_metadata(pdf_data, srv=srv)
                        cleaned_items.append({
                            "name": Path(it.filename).name,
                            "output_name": it.filename,
                            "orig_data": pdf_data,
                            "result": pdf_data,
                            "meta": meta,
                            "changed": True,
                        })
        except Exception:
            pass

    if not cleaned_items:
        return None

    batch_token = secrets.token_hex(8)
    spool_dir = STATE_DIR / "batch_spool" / f"batch_{batch_token}"
    spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    u_settings = _user_settings(uid) if uid else {}
    fio = u_settings.get("fio")
    custom_price = u_settings.get("custom_price")
    hide_price = bool(u_settings.get("hide_price", 0))

    items_list = []
    for idx, item in enumerate(cleaned_items):
        orig_path = spool_dir / f"orig_{idx}.pdf"
        clean_path = spool_dir / f"clean_{idx}.pdf"
        orig_data = item.get("orig_data") or item.get("result")
        clean_data = item.get("result") or item.get("orig_data")
        orig_path.write_bytes(orig_data)
        clean_path.write_bytes(clean_data)

        item_doc_token = secrets.token_hex(8)
        _save_doc_token(item_doc_token, str(orig_path), custom_price=custom_price, hide_price=hide_price, fio=fio, uid=uid)

        items_list.append({
            "idx": idx,
            "orig_name": item["name"],
            "output_name": item.get("output_name") or item["name"],
            "orig_path": str(orig_path),
            "clean_path": str(clean_path),
            "doc_token": item_doc_token,
            "meta": item.get("meta") or {},
            "fio": fio,
            "seat_info": None,
            "custom_price": custom_price,
            "hide_price": hide_price,
            "del_texts": None,
        })

    get_store().save_batch_session(batch_token, uid, chat, items_list)
    return batch_token


def _build_batch_dashboard(batch_token, page=0):
    session = get_store().get_batch_session(batch_token)
    if not session:
        return "⚠️ Сессия пачки не найдена или истекла.", None
    items = session.get("items", [])
    if not items:
        return "⚠️ В этой пачке нет билетов.", None

    total = len(items)
    page_size = 8
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * page_size
    end_idx = min(start_idx + page_size, total)

    lines = [f"⚙️ **Управление билетами в пачке** (всего: {total} шт.):\n"]
    for i in range(start_idx, end_idx):
        it = items[i]
        meta = it.get("meta") or {}
        title = meta.get("title") or it.get("orig_name") or f"Билет {i+1}"
        row = meta.get("row")
        seats = meta.get("seats") or []
        seat_str = f"Ряд {row}, Место {seats[0]}" if row and seats else (f"Ряд {row}" if row else (f"Место {seats[0]}" if seats else "места не указаны"))
        fio_str = it.get("fio") or "Без ФИО"
        if it.get("hide_price"):
            price_str = "Цена скрыта"
        elif it.get("custom_price") is not None:
            price_str = f"{it['custom_price']} ₽"
        elif meta.get("price") is not None:
            price_str = f"{meta['price']} ₽"
        else:
            price_str = "исходная цена"

        lines.append(f"**№{i+1}**. {title} — {seat_str}\n     👤 {fio_str} | 💰 {price_str}")

    lines.append("\nВыберите билет для настройки или используйте массовые действия:")
    text = "\n".join(lines)

    kb = []
    btn_row = []
    for i in range(start_idx, end_idx):
        btn_row.append({"text": f"№ {i+1}", "callback_data": f"bedit:{batch_token}:{i}"})
        if len(btn_row) == 4:
            kb.append(btn_row)
            btn_row = []
    if btn_row:
        kb.append(btn_row)

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append({"text": "⬅️ Назад", "callback_data": f"bcfg:{batch_token}:{page-1}"})
        nav.append({"text": f"Стр. {page+1}/{total_pages}", "callback_data": f"bcfg:{batch_token}:{page}"})
        if page < total_pages - 1:
            nav.append({"text": "Вперёд ➡️", "callback_data": f"bcfg:{batch_token}:{page+1}"})
        kb.append(nav)

    kb.append([
        {"text": "⚡️ Заполнить всё списком (ФИО + Места + Цены)", "callback_data": f"bmass:combo:{batch_token}"},
    ])
    kb.append([
        {"text": "👥 Список ФИО", "callback_data": f"bmass:fio:{batch_token}"},
        {"text": "🪑 Рассадить по порядку", "callback_data": f"bmass:seat:{batch_token}"},
    ])
    kb.append([
        {"text": "💰 Задать цену всем", "callback_data": f"bmass:price:{batch_token}"},
        {"text": "🎁 Скрыть все цены", "callback_data": f"bmass:hideprice:{batch_token}"},
    ])
    kb.append([
        {"text": "📂 Применить шаблон к пачке", "callback_data": f"btpl:menu:{batch_token}"}
    ])
    kb.append([
        {"text": "📦 Скачать новый ZIP", "callback_data": f"bzip:{batch_token}"},
        {"text": "📑 Скачать единый PDF", "callback_data": f"bmerge:{batch_token}"},
    ])
    kb.append([
        {"text": "📄 Прислать билеты в чат", "callback_data": f"bunpack:{batch_token}"}
    ])

    return text, {"inline_keyboard": kb}


def _build_batch_item_card(batch_token, idx):
    session = get_store().get_batch_session(batch_token)
    if not session or idx >= len(session.get("items", [])):
        return "⚠️ Билет не найден.", None
    item = session["items"][idx]
    meta = item.get("meta") or {}
    total = len(session["items"])

    title = meta.get("title") or item.get("orig_name") or f"Билет {idx+1}"
    date = meta.get("date") or "не указана"
    time_str = meta.get("time") or ""
    dt_str = f"{date} {time_str}".strip()

    row = meta.get("row")
    seats = meta.get("seats") or []
    seat_str = f"Ряд {row}, Место {seats[0]}" if row and seats else (f"Ряд {row}" if row else (f"Место {seats[0]}" if seats else "не указаны"))
    sector = meta.get("sector")
    if sector:
        seat_str = f"{sector}, {seat_str}"

    fio = item.get("fio") or "не указано (очищено)"
    if item.get("hide_price"):
        price_display = "🎁 Скрыта (подарочный режим)"
    elif item.get("custom_price") is not None:
        price_display = f"💰 {item['custom_price']} ₽ (кастомная)"
    elif meta.get("price") is not None:
        price_display = f"💰 {meta['price']} ₽"
    else:
        price_display = "💰 Исходная"

    text = (
        f"🎟 **Билет №{idx+1} из {total}**\n\n"
        f"🎭 Спектакль: **{title}**\n"
        f"📅 Дата: {dt_str}\n"
        f"🪑 Место: **{seat_str}**\n"
        f"👤 ФИО зрителя: **{fio}**\n"
        f"💵 Цена: **{price_display}**\n\n"
        f"Выберите действие для этого билета:"
    )

    price_toggle_label = "💰 Показать цену" if item.get("hide_price") else "🎁 Скрыть цену"

    kb = {
        "inline_keyboard": [
            [
                {"text": "⚡️ Быстрая правка (всё в 1 строке)", "callback_data": f"bset:quick:{batch_token}:{idx}"},
            ],
            [
                {"text": "👤 Изменить ФИО", "callback_data": f"bset:fio:{batch_token}:{idx}"},
                {"text": "🪑 Изменить место/ряд", "callback_data": f"bset:seat:{batch_token}:{idx}"},
            ],
            [
                {"text": "✏️ Изменить цену", "callback_data": f"bset:price:{batch_token}:{idx}"},
                {"text": price_toggle_label, "callback_data": f"btog:price:{batch_token}:{idx}"},
            ],
            [
                {"text": "🖼 Фото-превью", "callback_data": f"bprev:{batch_token}:{idx}"},
                {"text": "📄 Отправить этот билет", "callback_data": f"bsend:{batch_token}:{idx}"},
            ],
            [
                {"text": "⬅️ К списку билетов пачки", "callback_data": f"bcfg:{batch_token}"}
            ]
        ]
    }
    return text, kb


def _reprocess_batch_item(batch_token, idx, fio=None, seat_info=None, custom_price=None, hide_price=None, del_texts=None):
    session = get_store().get_batch_session(batch_token)
    if not session or idx >= len(session.get("items", [])):
        return False, "Сессия не найдена"
    items = session["items"]
    item = items[idx]

    orig_p = Path(item["orig_path"])
    if not orig_p.exists():
        return False, "Исходный файл билета недоступен"
    orig_bytes = orig_p.read_bytes()

    if fio is not None:
        item["fio"] = None if fio in {"сброс", "reset", "clear"} else fio.strip()
    if seat_info is not None:
        item["seat_info"] = None if seat_info in {"сброс", "reset", "clear"} else seat_info.strip()
    if custom_price is not None:
        item["custom_price"] = None if custom_price in {"сброс", "reset", "clear"} else custom_price
    if hide_price is not None:
        item["hide_price"] = bool(hide_price)
    if del_texts is not None:
        item["del_texts"] = None if del_texts in {"сброс", "reset", "clear"} else del_texts

    ok, result, info = clean_ticket_with_report(
        orig_bytes,
        spectator_fio=item.get("fio"),
        hide_price=item.get("hide_price", False),
        custom_price=item.get("custom_price"),
        seat_info=item.get("seat_info"),
        del_texts=item.get("del_texts"),
    )
    if not ok:
        return False, result

    clean_p = Path(item["clean_path"])
    clean_p.write_bytes(result)

    meta = ticket_meta.extract_ticket_metadata(result, srv=srv)
    item["meta"] = meta
    item["output_name"] = ticket_meta.make_clean_ticket_filename(meta, fallback=item.get("orig_name", "ticket.pdf"), hide_price=item.get("hide_price", False))

    items[idx] = item
    get_store().save_batch_session(batch_token, session["user_id"], session["chat_id"], items)
    return True, item


def _apply_mass_fio_to_batch(batch_token, fio_list):
    session = get_store().get_batch_session(batch_token)
    if not session:
        return 0
    items = session.get("items", [])
    count = 0
    for idx, fio_line in enumerate(fio_list):
        if idx >= len(items):
            break
        cleaned_fio = fio_line.strip()
        if not cleaned_fio:
            continue
        ok, _ = _reprocess_batch_item(batch_token, idx, fio=cleaned_fio)
        if ok:
            count += 1
    return count


def _apply_mass_seats_to_batch(batch_token, seat_input_text):
    session = get_store().get_batch_session(batch_token)
    if not session:
        return 0, "Сессия не найдена"
    items = session.get("items", [])
    if not items:
        return 0, "В пачке нет билетов"

    parsed = seat_tools.parse_seat_input(seat_input_text)
    if not parsed or (not parsed.get("seat") and not parsed.get("row")):
        return 0, "Не удалось распознать ряд и место. Формат: Ряд 5 Место 10 или просто 5 10."

    sector = parsed.get("sector", "")
    row = parsed.get("row", "")
    start_seat_str = parsed.get("seat", "")

    m_num = re.search(r"\d+", start_seat_str) if start_seat_str else None
    start_seat = int(m_num.group(0)) if m_num else None

    count = 0
    for idx in range(len(items)):
        if start_seat is not None:
            curr_seat = start_seat + idx
            seat_parts = []
            if sector:
                seat_parts.append(f"сектор {sector}")
            if row:
                seat_parts.append(f"ряд {row}")
            seat_parts.append(f"место {curr_seat}")
            seat_str = ", ".join(seat_parts)
        else:
            seat_parts = []
            if sector:
                seat_parts.append(f"сектор {sector}")
            if row:
                seat_parts.append(f"ряд {row}")
            seat_str = ", ".join(seat_parts)

        ok, _ = _reprocess_batch_item(batch_token, idx, seat_info=seat_str)
        if ok:
            count += 1

    summary = seat_tools.format_seat_summary(parsed)
    return count, summary


def _apply_mass_combo_to_batch(batch_token, raw_text):
    session = get_store().get_batch_session(batch_token)
    if not session:
        return 0, ["Сессия не найдена или истекла."]
    items = session.get("items", [])
    total = len(items)
    if total == 0:
        return 0, ["В пачке нет билетов."]

    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    if not lines:
        return 0, ["Список пуст."]

    if len(lines) == 1 and lines[0].lower() in {"сброс", "reset", "clear"}:
        for i in range(total):
            _reprocess_batch_item(batch_token, i, fio="сброс", seat_info="сброс", custom_price="сброс", hide_price=False)
        return total, ["Исходные данные восстановлены на всех билетах."]

    reports = []
    count = 0

    is_multiline_list = len(lines) > 1 or bool(re.match(r"^\d+[.)\s-]", lines[0]))

    if is_multiline_list:
        for idx, line in enumerate(lines):
            target_idx = idx
            m_num = re.match(r"^(\d+)[.)\s-]+", line)
            if m_num:
                parsed_idx = int(m_num.group(1)) - 1
                if 0 <= parsed_idx < total:
                    target_idx = parsed_idx
            if target_idx >= total:
                continue
            q = seat_tools.parse_quick_edit(line)
            kwargs = {}
            if q["reset_all"]:
                kwargs = {"fio": "сброс", "seat_info": "сброс", "custom_price": "сброс", "hide_price": False}
            else:
                if q["reset_fio"]:
                    kwargs["fio"] = "сброс"
                elif q["fio"] is not None:
                    kwargs["fio"] = q["fio"]

                if q["reset_price"]:
                    kwargs["custom_price"] = "сброс"
                    kwargs["hide_price"] = False
                elif q["hide_price"] is not None:
                    kwargs["hide_price"] = q["hide_price"]
                elif q["custom_price"] is not None:
                    kwargs["custom_price"] = q["custom_price"]
                    kwargs["hide_price"] = False

                if q["reset_seat"]:
                    kwargs["seat_info"] = "сброс"
                elif q["seat_info"] is not None:
                    kwargs["seat_info"] = seat_tools.format_seat_summary(q["seat_info"])

            if kwargs:
                ok, res_item = _reprocess_batch_item(batch_token, target_idx, **kwargs)
                if ok:
                    count += 1
                    parts = []
                    if res_item.get("fio"):
                        parts.append(res_item["fio"])
                    if res_item.get("seat_info"):
                        parts.append(res_item["seat_info"])
                    elif res_item.get("meta") and (res_item["meta"].get("row") or res_item["meta"].get("seats")):
                        m = res_item["meta"]
                        r = m.get("row")
                        s = m.get("seats", [])
                        parts.append(f"ряд {r}, место {s[0]}" if r and s else (f"ряд {r}" if r else f"место {s[0]}"))
                    if res_item.get("hide_price"):
                        parts.append("цена скрыта")
                    elif res_item.get("custom_price") is not None:
                        parts.append(f"{res_item['custom_price']} ₽")
                    reports.append(f"• Билет №{target_idx+1}: " + (" · ".join(parts) if parts else "обновлён"))
                else:
                    reports.append(f"• Билет №{target_idx+1}: ⚠️ ошибка ({res_item})")
    else:
        single_line = lines[0]
        q = seat_tools.parse_quick_edit(single_line)
        if (not q["reset_all"] and not q["reset_fio"] and not q["reset_price"] and not q["reset_seat"]
                and q["fio"] is None and q["custom_price"] is None and q["hide_price"] is None and q["seat_info"] is None):
            return 0, ["Не удалось распознать параметры в строке."]

        sector = q["seat_info"].get("sector", "") if q["seat_info"] else ""
        row = q["seat_info"].get("row", "") if q["seat_info"] else ""
        start_seat_str = q["seat_info"].get("seat", "") if q["seat_info"] else ""
        m_num = re.search(r"\d+", start_seat_str) if start_seat_str else None
        start_seat = int(m_num.group(0)) if m_num else None

        for idx in range(total):
            kwargs = {}
            if q["reset_all"]:
                kwargs = {"fio": "сброс", "seat_info": "сброс", "custom_price": "сброс", "hide_price": False}
            else:
                if q["reset_fio"]:
                    kwargs["fio"] = "сброс"
                elif q["fio"] is not None:
                    kwargs["fio"] = q["fio"]

                if q["reset_price"]:
                    kwargs["custom_price"] = "сброс"
                    kwargs["hide_price"] = False
                elif q["hide_price"] is not None:
                    kwargs["hide_price"] = q["hide_price"]
                elif q["custom_price"] is not None:
                    kwargs["custom_price"] = q["custom_price"]
                    kwargs["hide_price"] = False

                if q["reset_seat"]:
                    kwargs["seat_info"] = "сброс"
                elif q["seat_info"] is not None:
                    if start_seat is not None:
                        curr_seat = start_seat + idx
                        parts = []
                        if sector:
                            parts.append(f"сектор {sector}")
                        if row:
                            parts.append(f"ряд {row}")
                        parts.append(f"место {curr_seat}")
                        kwargs["seat_info"] = ", ".join(parts)
                    else:
                        kwargs["seat_info"] = seat_tools.format_seat_summary(q["seat_info"])

            if kwargs:
                ok, res_item = _reprocess_batch_item(batch_token, idx, **kwargs)
                if ok:
                    count += 1

        summary_parts = []
        if q["seat_info"]:
            if start_seat is not None:
                summary_parts.append(f"места с {start_seat} по {start_seat + total - 1} подряд")
            else:
                summary_parts.append(seat_tools.format_seat_summary(q["seat_info"]))
        if q["hide_price"]:
            summary_parts.append("цена скрыта")
        elif q["custom_price"] is not None:
            summary_parts.append(f"цена {q['custom_price']} ₽")
        if q["fio"]:
            summary_parts.append(f"ФИО: {q['fio']}")
        reports.append(" · ".join(summary_parts) or "параметры обновлены")

    return count, reports


def _make_batch_zip(batch_token):
    session = get_store().get_batch_session(batch_token)
    if not session:
        return None, None
    items = session.get("items", [])
    if not items:
        return None, None

    buf = io.BytesIO()
    processed_items = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for idx, it in enumerate(items, 1):
            clean_p = Path(it["clean_path"])
            clean_bytes = clean_p.read_bytes() if clean_p.exists() else b""
            if not clean_bytes:
                continue
            out_name = it.get("output_name") or f"{idx:02d}. ticket_{idx}.pdf"
            archive.writestr(out_name, clean_bytes)
            processed_items.append({
                "name": it.get("orig_name") or out_name,
                "output_name": out_name,
                "meta": it.get("meta") or {},
                "changed": True,
            })
        report_text = ticket_meta.format_batch_report_table(processed_items)
        archive.writestr("report.txt", report_text.encode("utf-8"))

    zip_bytes = buf.getvalue()
    zip_name = ticket_meta.make_batch_zip_filename(processed_items, len(processed_items))
    return zip_bytes, zip_name


def _make_batch_merged_pdf(batch_token):
    session = get_store().get_batch_session(batch_token)
    if not session:
        return None, None, 0, ""
    items = session.get("items", [])
    if not items:
        return None, None, 0, ""

    merged = fitz.open()
    processed_meta = []
    for idx, it in enumerate(items, 1):
        clean_p = Path(it["clean_path"])
        clean_bytes = clean_p.read_bytes() if clean_p.exists() else b""
        if not clean_bytes:
            continue
        try:
            sub = fitz.open(stream=clean_bytes, filetype="pdf")
            merged.insert_pdf(sub)
            sub.close()
            processed_meta.append(it.get("meta") or {})
        except Exception as e:
            log.warning("merge pdf item %d error: %s", idx, e)

    count = len(merged)
    buf = io.BytesIO()
    merged.save(buf, garbage=3, deflate=True)
    merged.close()
    merged_bytes = buf.getvalue()

    first_meta = processed_meta[0] if processed_meta else {}
    merged_name = ticket_meta.make_clean_ticket_filename(first_meta, fallback="tickets_merged.pdf")
    if not merged_name.endswith(".pdf"):
        merged_name += ".pdf"
    caption = f"📑 Склеенный PDF: {count} {tickets_word(count)} в одном файле."
    return merged_bytes, merged_name, count, caption


def _unpack_batch_to_chat(chat, uid, batch_token):
    session = get_store().get_batch_session(batch_token)
    if not session:
        return send_text(chat, "Сессия пачки не найдена или истекла.")
    items = session.get("items", [])
    if not items:
        return send_text(chat, "В этой пачке нет билетов.")

    send_text(chat, f"Отправляю билеты по отдельности ({len(items)} шт.)...")
    sent_count = 0
    for idx, it in enumerate(items, 1):
        clean_p = Path(it["clean_path"])
        orig_p = Path(it["orig_path"])
        if not clean_p.exists():
            continue
        clean_bytes = clean_p.read_bytes()
        meta = it.get("meta") or {}
        out_name = it.get("output_name") or f"ticket_{idx}.pdf"
        caption = ticket_meta.format_single_ticket_caption(meta, {"changed": True, "hide_price": it.get("hide_price", False)})

        token = secrets.token_hex(8)
        _save_doc_token(
            token,
            str(orig_p),
            custom_price=it.get("custom_price"),
            hide_price=it.get("hide_price", False),
            fio=it.get("fio"),
            seat_info=it.get("seat_info"),
            del_texts=it.get("del_texts"),
            uid=uid,
        )
        markup = _single_ticket_markup(token, hide_price=it.get("hide_price", False), pages=1)
        sent = send_cleaned(chat, clean_bytes, out_name, caption, reply_markup=markup)
        if sent:
            sent_count += 1
        time.sleep(0.3)

    get_store().bump_processed(uid, sent_count)
    send_text(chat, f"Готово 🩷 Отправлено билетов: {sent_count} из {len(items)}.")


def _send_batch_result(chat, result, progress, retry_message, uid=None):
    progress.update(3, "Отправляю результат обработки в чат.")
    items_meta = result.get("items_meta", [])
    caption = ticket_meta.format_batch_tg_caption(items_meta, result["ok"], result["failed"])
    zip_name = result.get("zip_name") or "tickets_cleaned.zip"

    settings = _user_settings(uid) if uid else {}
    batch_mode = settings.get("batch_mode") or "zip"
    auto_preview = bool(settings.get("auto_preview", 0))

    batch_token = None
    if result.get("ok", 0) > 0:
        batch_token = _init_batch_session(uid, chat, result)

    reply_markup = None
    if batch_token:
        reply_markup = {
            "inline_keyboard": [
                [{"text": "⚙️ Настроить билеты (ФИО / места)", "callback_data": f"bcfg:{batch_token}"}],
                [
                    {"text": "👥 Список ФИО", "callback_data": f"bmass:fio:{batch_token}"},
                    {"text": "🪑 Рассадить по порядку", "callback_data": f"bmass:seat:{batch_token}"},
                ],
                [
                    {"text": "📄 Билеты по отдельности", "callback_data": f"bunpack:{batch_token}"},
                    {"text": "📑 Склеить в один PDF", "callback_data": f"bmerge:{batch_token}"},
                ],
                [
                    {"text": "💰 Задать цену всем", "callback_data": f"bmass:price:{batch_token}"},
                    {"text": "🎁 Скрыть все цены", "callback_data": f"bmass:hideprice:{batch_token}"},
                ],
                [{"text": "🖼 Фото-превью первого билета", "callback_data": f"preview:{batch_token}"}],
            ]
        }
    elif result.get("ok", 0) > 0:
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "📄 Прислать билеты по отдельности", "callback_data": "unpack"},
                    {"text": "📑 Склеить в один PDF", "callback_data": "merge"},
                ],
                [
                    {"text": "🖼 Фото-превью первого билета", "callback_data": "preview"}
                ]
            ]
        }

    if batch_mode == "merge" and result.get("ok", 0) > 0:
        merged_bytes, merged_name, count, m_caption = merge_batch_pdfs(result["data"])
        if merged_bytes and count > 0:
            token = batch_token or secrets.token_hex(8)
            sent_res = send_cleaned(chat, merged_bytes, merged_name, m_caption, reply_markup=reply_markup)
            if sent_res:
                if batch_token and isinstance(sent_res, str):
                    session = get_store().get_batch_session(batch_token)
                    if session:
                        get_store().save_batch_session(batch_token, uid, chat, session["items"], merged_file_id=sent_res)
                progress.finish(f"Склеенный PDF отправлен 🩷 Билетов: {count}.")
                if auto_preview:
                    img_bytes = render_pdf_preview(merged_bytes)
                    if img_bytes:
                        send_photo(chat, img_bytes, caption="🖼 Авто-превью первого билета")
                return True

    sent_res = send_cleaned(chat, result["data"], zip_name, caption, reply_markup=reply_markup)
    if not sent_res:
        _progress_error(progress, chat, retry_message)
        return False
    if batch_token and isinstance(sent_res, str):
        session = get_store().get_batch_session(batch_token)
        if session:
            get_store().save_batch_session(batch_token, uid, chat, session["items"], zip_file_id=sent_res)
    get_store().bump_processed(uid, result["ok"])
    progress.finish(f"ZIP отправлен 🩷 Готовых PDF: {result['ok']} · ошибок: {result['failed']}.")
    if auto_preview and result.get("ok", 0) > 0:
        try:
            with zipfile.ZipFile(io.BytesIO(result["data"])) as archive:
                for item in archive.infolist():
                    if not item.is_dir() and item.filename.lower().endswith(".pdf"):
                        first_pdf = archive.read(item.filename)
                        img_bytes = render_pdf_preview(first_pdf)
                        if img_bytes:
                            send_photo(chat, img_bytes, caption="🖼 Авто-превью первого билета")
                        break
        except Exception:
            pass
    return True


def _finish_collection(chat, user_id, ident):
    collection = get_store().collection(ident)
    if not collection or collection["user_id"] != user_id or collection["chat_id"] != chat or collection["state"] == "canceled":
        return send_text(chat, "Пачка отменена или срок хранения истёк.")
    files = get_store().collection_files(ident)
    if not files:
        get_store().collection_state(ident, "done")
        return send_text(chat, "В пачке нет файлов. Начните новую: /batch.")
    with Progress(chat, "Пачка билетов", progress_request, BATCH_STEPS, cleanup=close_http_session) as progress:
        progress.batch(0, len(files))
        items = [(item["name"], Path(item["path"]).read_bytes()) for item in files]
        try:
            u_settings = _user_settings(user_id)
            spectator_fio = u_settings.get("fio")
            hide_price = bool(u_settings.get("hide_price", 0))
            cleaner, notify = _batch_callbacks(progress, spectator_fio=spectator_fio, hide_price=hide_price)
            result = batches.process_items(items, cleaner, on_progress=notify)
            if _send_batch_result(chat, result, progress, "Не удалось отправить ZIP. Повторите /done.", uid=user_id):
                get_store().collection_state(ident, "done")
                shutil.rmtree(_collection_dir(ident), ignore_errors=True)
                get_store().forget_collection(ident)
        except batches.BatchError as error:
            _progress_error(progress, chat, str(error))
            get_store().collection_state(ident, "done")
            shutil.rmtree(_collection_dir(ident), ignore_errors=True)
            get_store().forget_collection(ident)


def _process_auto_zip_batch(chat, uid, docs):
    """Process a burst of >2 tickets and deliver them as a clean ZIP archive with report.txt."""
    with Progress(chat, "Пачка билетов", progress_request, BATCH_STEPS, cleanup=close_http_session) as progress:
        progress.batch(0, len(docs))
        items = []
        for index, (jid, dmsg) in enumerate(docs, 1):
            doc = dmsg["document"]
            dname = doc.get("file_name") or f"ticket_{index}.pdf"
            if (doc.get("file_size") or 0) > MAX_BYTES:
                continue
            data = download_file(doc["file_id"])
            if data:
                items.append((dname, data))

        missing = len(docs) - len(items)
        if not items:
            _progress_error(progress, chat, "Не удалось скачать файлы из пачки. Отправьте их ещё раз.")
            return
        if missing:
            send_text(chat, f"⚠️ Не удалось скачать {missing} из {len(docs)} файлов — они не войдут в пачку.")

        try:
            u_settings = _user_settings(uid)
            spectator_fio = u_settings.get("fio")
            hide_price = bool(u_settings.get("hide_price", 0))
            custom_price = u_settings.get("custom_price")
            cleaner, notify = _batch_callbacks(progress, spectator_fio=spectator_fio, hide_price=hide_price, custom_price=custom_price)
            result = batches.process_items(items, cleaner, on_progress=notify)
            _send_batch_result(chat, result, progress, "Не удалось отправить ZIP. Отправьте билеты ещё раз.", uid=uid)
        except batches.BatchError as error:
            _progress_error(progress, chat, str(error))


def no_access_message(uid):
    """Сообщение без доступа: учитываем бесплатные билеты дня."""
    try:
        left = get_store().daily_left(uid)
    except Exception:
        left = 0
    if left > 0:
        return send_text(uid,
            f"🎁 Сегодня осталось {left} из {FREE_DAILY} бесплатных билетов — просто пришли PDF.\n"
            "Без лимита — с абонементом.",
            {"inline_keyboard": [[
                {"text": "✍️ Без лимита", "callback_data": "welcome:subscribe"},
                {"text": "🎁 Пробный день", "callback_data": "welcome:trial"}]]})
    return send_text(uid,
        f"🎟 Бесплатные {FREE_DAILY} {tickets_word(FREE_DAILY)} на сегодня уже перепечатаны.\n"
        "Лимит обновится завтра утром. А без лимита — прямо сейчас:",
        {"inline_keyboard": [[
            {"text": "✍️ Оформить абонемент", "callback_data": "welcome:subscribe"},
            {"text": "🎟 Промокод", "callback_data": "welcome:promo"}]]})


def process_incoming_document(msg, job_id=None):
    chat = msg["chat"]["id"]
    uid = msg["from"]["id"]
    if not has_access(uid):
        # Бесплатный тир: 2 билета в день, по одному за раз, без авто-пачек.
        if get_store().daily_left(uid) <= 0:
            return no_access_message(uid)
        return process_document(msg, None, job_id=job_id)
    collection = get_store().open_collection(uid, chat)
    collection_id = collection["id"] if collection else None
    if collection_id:
        return process_document(msg, collection_id, job_id=job_id)

    doc = msg.get("document") or {}
    name = doc.get("file_name") or "ticket.pdf"
    if not name.lower().endswith(".pdf"):
        return process_document(msg, None, job_id=job_id)

    # Gather any other pending PDFs from the same user (burst / multi-select / media group)
    docs = [(job_id, msg)]
    has_mg = bool(msg.get("media_group_id"))
    default_debounce = 2.0 if has_mg else 1.2
    debounce_sec = float(os.environ.get("BILET_AUTO_DEBOUNCE", str(default_debounce)))
    deadline = time.monotonic() + debounce_sec
    max_deadline = time.monotonic() + (5.0 if has_mg else 3.5)
    extra_claimed = []
    while True:
        extra = get_store().claim_pending_documents(uid, chat)
        if extra:
            for extra_id, extra_payload in extra:
                extra_msg = extra_payload.get("message", {})
                docs.append((extra_id, extra_msg))
                extra_claimed.append(extra_id)
                if extra_msg.get("media_group_id"):
                    has_mg = True
                    max_deadline = max(max_deadline, time.monotonic() + 4.0)
            step_wait = 1.5 if has_mg else 1.0
            deadline = min(max_deadline, time.monotonic() + step_wait)
        else:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)

    try:
        min_batch = int(os.environ.get("BILET_MIN_BATCH_SIZE", "2"))
        if len(docs) >= min_batch:
            return _process_auto_zip_batch(chat, uid, docs)
        else:
            for jid, dmsg in docs:
                process_document(dmsg, None, job_id=jid)
    finally:
        for xid in extra_claimed:
            get_store().finish(xid)


def process_document(msg, collection_id=None, job_id=None):
    chat = msg["chat"]["id"]
    uid = msg["from"]["id"]
    if not has_access(uid) and get_store().daily_left(uid) <= 0:
        return no_access_message(uid)
    document = msg["document"]
    name = document.get("file_name") or "ticket.pdf"
    if not name.lower().endswith((".pdf", ".zip")) or (document.get("file_size") or 0) > MAX_BYTES:
        return send_text(chat, "Принимаются PDF и ZIP размером до 20 МБ.")
    steps = COLLECT_STEPS if collection_id else BATCH_STEPS if name.lower().endswith(".zip") else PDF_STEPS
    u_settings = _user_settings(uid)
    user_fio = u_settings.get("fio")
    hide_price = bool(u_settings.get("hide_price", 0))
    custom_price = u_settings.get("custom_price")
    auto_preview = bool(u_settings.get("auto_preview", 0))
    cache_suffix = f":{user_fio or ''}:hp{1 if hide_price else 0}:cp{custom_price or ''}" if (user_fio or hide_price or custom_price is not None) else ""
    token = secrets.token_hex(8)
    _save_doc_token(token, document["file_id"], custom_price=custom_price, hide_price=hide_price, fio=user_fio, uid=uid)
    markup = _single_ticket_markup(token, hide_price=hide_price, pages=1)
    with Progress(chat, name, progress_request, steps, cleanup=close_http_session) as progress:
        cache_keys = []
        if not collection_id and name.lower().endswith(".pdf"):
            unique = document.get("file_unique_id")
            if unique:
                cache_keys.append(key_for(PROCESSING_VERSION, uid, chat, name + cache_suffix, "telegram:" + unique))
                cached = _cached_file(cache_keys[0])
                if cached and _send_cached_ticket(chat, cached, cache_keys, progress, reply_markup=markup) is not None:
                    return
                if cached:
                    progress.update(0, "Готовая копия недоступна. Получаю исходный файл заново.")
        download_started = time.monotonic()
        data = download_file(document["file_id"])
        log.info("File download: %.3fs, %d bytes", time.monotonic() - download_started, len(data or b""))
        if data is None:
            return _progress_error(progress, chat, "Не удалось скачать файл или он превышает 20 МБ. Отправьте его ещё раз.")
        if collection_id:
            progress.update(1, "Сохраняю PDF в твою пачку.")
            collection = get_store().collection(collection_id)
            if not collection or collection["state"] not in {"open", "sealed"} or collection["user_id"] != uid:
                return _progress_error(progress, chat, "Пачка уже отменена или истекла.")
            if name.lower().endswith(".zip"):
                return _progress_error(progress, chat, "В режиме /batch отправляйте отдельные PDF. ZIP можно отправить вне пачки.")
            directory = _collection_dir(collection_id)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / (str(job_id) if job_id is not None else secrets.token_hex(12))
            path.write_bytes(data)
            try:
                count = get_store().add_collection_file(collection_id, batches.safe_name(name), path, len(data))
            except ValueError:
                path.unlink(missing_ok=True)
                return _progress_error(progress, chat, "Лимит пачки: 50 PDF и 100 МБ. Завершите её командой /done.")
            progress.finish(f"В пачке: {count} PDF 🩷 Когда закончишь — /done.")
            return
        if name.lower().endswith(".zip"):
            if not has_access(uid):
                # ZIP обрабатывается как пачка: без гейта бесплатник получал бы
                # пакеты билетов, не расходуя дневной лимит.
                return _progress_error(progress, chat, FREE_BATCH_TEXT)
            try:
                cleaner, notify = _batch_callbacks(progress, spectator_fio=user_fio, hide_price=hide_price, custom_price=custom_price)
                result = batches.process_zip(data, cleaner, on_progress=notify)
                _send_batch_result(chat, result, progress, "Не удалось отправить архив. Отправьте исходный ZIP ещё раз.", uid=uid)
            except batches.BatchError as error:
                _progress_error(progress, chat, str(error))
            return

        content_key = key_for(PROCESSING_VERSION, uid, chat, name + cache_suffix,
                              "sha256:" + hashlib.sha256(data).hexdigest())
        cache_keys.append(content_key)
        cached = _cached_file(content_key)
        if cached and _send_cached_ticket(chat, cached, cache_keys, progress, reply_markup=markup) is not None:
            return

        def phase(stage):
            if stage == "clean":
                progress.update(1, "Стираем чужие ФИО и чары Черномора ✍️")
            else:
                progress.update(2, "Сверяем QR-коды, дату, сектор, ряд и цену 🔍")

        ok, result, info = clean_ticket_with_report(data, on_progress=phase, spectator_fio=user_fio, hide_price=hide_price, custom_price=custom_price)
        if not ok:
            return _progress_error(progress, chat, result)
        meta = ticket_meta.extract_ticket_metadata(result, srv=srv)
        out_name = ticket_meta.make_clean_ticket_filename(meta, fallback=batches.safe_name(name), hide_price=hide_price)
        caption = ticket_meta.format_single_ticket_caption(meta, info)
        progress.update(3, "Печать завершена. Чистый билет летит в чат 🎟")
        reuse_id = document["file_id"] if (not info["changed"] and not user_fio and not hide_price and custom_price is None) else None
        pages_cnt = 1
        batch_token = None
        try:
            with fitz.open(stream=result, filetype="pdf") as _doc:
                pages_cnt = len(_doc)
        except Exception:
            pass
        if pages_cnt > 1:
            try:
                orig_entries = split_pdf_tickets(data, srv=srv, hide_price=hide_price)
                clean_entries = split_pdf_tickets(result, srv=srv, hide_price=hide_price)
                if len(orig_entries) == pages_cnt:
                    b_token = secrets.token_hex(8)
                    spool_dir = STATE_DIR / "batch_spool" / f"batch_{b_token}"
                    spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    session_items = []
                    for idx, (p_name, p_orig_bytes, p_meta) in enumerate(orig_entries):
                        p_clean_bytes = clean_entries[idx][1] if idx < len(clean_entries) else p_orig_bytes
                        p_clean_meta = clean_entries[idx][2] if idx < len(clean_entries) else p_meta
                        orig_path = spool_dir / f"orig_{idx}.pdf"
                        clean_path = spool_dir / f"clean_{idx}.pdf"
                        orig_path.write_bytes(p_orig_bytes)
                        clean_path.write_bytes(p_clean_bytes)
                        item_doc_token = secrets.token_hex(8)
                        _save_doc_token(item_doc_token, str(orig_path), custom_price=custom_price, hide_price=hide_price, fio=user_fio, uid=uid)
                        session_items.append({
                            "idx": idx,
                            "orig_name": p_name,
                            "output_name": ticket_meta.make_clean_ticket_filename(p_clean_meta, fallback=p_name, hide_price=hide_price),
                            "orig_path": str(orig_path),
                            "clean_path": str(clean_path),
                            "doc_token": item_doc_token,
                            "meta": p_clean_meta,
                            "fio": user_fio,
                            "seat_info": None,
                            "custom_price": custom_price,
                            "hide_price": hide_price,
                            "del_texts": None,
                        })
                    get_store().save_batch_session(b_token, uid, chat, session_items)
                    batch_token = b_token
            except Exception as e:
                log.warning("multi-page batch init error: %s", e)
        markup = _single_ticket_markup(token, hide_price=hide_price, pages=pages_cnt, batch_token=batch_token)
        sent = send_cleaned(chat, result, out_name, caption, cache_keys=cache_keys, reuse_id=reuse_id, reply_markup=markup)
        if sent is None and reuse_id:
            sent = send_cleaned(chat, result, out_name, caption, cache_keys=cache_keys, reply_markup=markup)
        if not sent:
            return _progress_error(progress, chat, "Не удалось отправить файл. Отправьте исходный PDF ещё раз.")
        get_store().bump_processed(uid)
        if meta and uid is not None:
            title = meta.get("title")
            dt = f"{meta.get('date', '')} {meta.get('time', '')}".strip()
            row = meta.get("row")
            seats = meta.get("seats") or []
            seat_str = f"Ряд {row}, Место {seats[0]}" if row and seats else (f"Ряд {row}" if row else (f"Место {seats[0]}" if seats else None))
            p = custom_price if custom_price is not None else meta.get("price")
            get_store().add_ticket_history(uid, token, title=title, date_str=dt, seat_str=seat_str, price=p)
        if not has_access(uid):
            try:
                if get_store().daily_consume(uid):
                    left_after = get_store().daily_left(uid)
                    if left_after > 0:
                        progress.finish(f"Билет отправлен 🩷 Бесплатных осталось сегодня: {left_after}.")
                    else:
                        progress.finish("Билет отправлен 🩷 Бесплатный лимит на сегодня исчерпан.")
                        send_text(chat, f"Это были бесплатные {FREE_DAILY} {tickets_word(FREE_DAILY)} на сегодня.\n"
                                        "Без лимита — с абонементом:",
                                  {"inline_keyboard": [[{"text": "✍️ Оформить", "callback_data": "welcome:subscribe"}]]})
                # consume() == False: параллельная гонка исчерпала лимит —
                # билет уже ушёл, пользователя не ругаем.
            except Exception:
                pass
            return
        progress.finish("Билет отправлен 🩷 Можно присылать следующий." if info["changed"]
                        else "Исходный PDF отправлен 🩷 Автоматических правок нет.")
        if auto_preview:
            img_bytes = render_pdf_preview(result)
            if img_bytes:
                send_photo(chat, img_bytes, caption="🖼 Авто-превью билета")


def handle_callback_query(cb):
    data = cb.get("data") or ""
    if not any(data == x or data.startswith(f"{x}:") for x in (
        "quick", "unpack", "merge", "preview", "compare", "price", "fio", "seat", "choose_sec",
        "pushkin", "avito", "set", "toggleprice", "split", "welcome", "adm",
        "bcfg", "bedit", "bset", "btog", "bprev", "bmass", "bzip", "bmerge", "bunpack", "bsend",
        "tpl", "btpl", "ref", "hist", "history", "sub", "cbpay"
    )):
        return

    sender = cb.get("from") or {}
    uid = sender.get("id")
    msg = cb.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    msg_id = msg.get("message_id")
    if not chat or not uid:
        return

    if data.startswith("welcome:"):
        action = data.split(":", 1)[1]
        if action == "subscribe":
            return subscribe(chat, uid)
        if action == "trial":
            return cmd_trial(chat, uid)
        if action == "help":
            return send_help(chat, uid, sender.get("username"))
        if action == "profile":
            return send_profile(chat, uid, sender.get("username"))
        if action == "menu":
            return send_welcome(chat, uid, sender.get("username"))
        if action == "admin":
            if not is_admin(uid, sender.get("username")):
                return send_text(chat, "Панель доступна только администраторам бота.")
            return send_admin_panel(chat, msg_id=msg_id)
        if action == "status":
            text_card, markup = build_subscription_card(uid)
            return edit_or_send(chat, msg_id, text_card, markup=markup)
        if action == "settings":
            text_card, markup = build_settings_card(uid)
            return edit_or_send(chat, msg_id, text_card, markup=markup)
        if action == "promo":
            get_store().set_user_state(uid, "waiting_promo")
            return send_text(chat, "🎟 Отправьте промокод одним сообщением (например: THEATR2026).\n(для отмены напишите: отмена)")
        return

    global _broadcast_running

    if data in ("adm:bcastgo", "adm:bcastno"):
        if not is_admin(uid, sender.get("username")):
            return send_text(chat, "Эта панель доступна администратору.")
        if data == "adm:bcastno":
            _pending_broadcast.pop(uid, None)
            return send_text(chat, "Рассылка отменена.")
        body = _pending_broadcast.pop(uid, None)
        if not body:
            return send_text(chat, "Предпросмотр не найден. Создай заново: /broadcast ТЕКСТ")
        with _broadcast_lock:
            if _broadcast_running:
                return send_text(chat, "Предыдущая рассылка ещё идёт, подожди.")
            _broadcast_running = True
        threading.Thread(target=_run_broadcast, args=(chat, body), daemon=True,
                         name="broadcast").start()
        return send_text(chat, "📣 Рассылка запущена в фоне. Отчёт пришлю сюда.")

    if data.startswith("adm:"):
        if not is_admin(uid, sender.get("username")):
            return send_text(chat, "Эта панель доступна администратору.")
        action = data.split(":", 1)[1]
        if action == "stats":
            body = admin_stats_text()
        elif action == "payments":
            body = admin_payments_text()
        elif action == "promos":
            body = admin_promos_text()
        elif action == "incidents":
            rows = get_store().payment_incidents()
            body = "\n".join(f"Событие {p['update_id']} | ID {p['user_id']} | {p['reason']}" for p in rows) or "Ошибок подтверждения оплаты нет."
        elif action == "broadcast_hint":
            body = ("Формат: /broadcast ТЕКСТ → бот покажет предпросмотр →\n"
                    "кнопка Отправить всем. Рассылка идёт в фоне,\n"
                    "обработка билетов не останавливается.")
        else:  # refresh
            return send_admin_panel(chat, msg_id=msg_id)
        return edit_or_send(chat, msg_id, body[:4000], markup=admin_panel_markup())

    if data == "cbpay:create":
        return handle_cryptobot_create(chat, uid, msg_id=msg_id)

    if data.startswith("cbpay:check:"):
        inv_id = data.split(":", 2)[2]
        return handle_cryptobot_check(chat, uid, inv_id, msg_id=msg_id)

    if data.startswith("ref:"):
        text, markup = build_referral_card(uid)
        return edit_or_send(chat, msg_id, text, markup=markup)

    if data.startswith("history:"):
        text, markup = build_history_card(uid)
        return edit_or_send(chat, msg_id, text, markup=markup)

    if data.startswith("hist:open:"):
        tok = data.split(":", 2)[2]
        doc_info = get_store().get_doc_token_info(tok)
        if not doc_info:
            return send_text(chat, "Билет не найден в памяти бота.")
        markup = _single_ticket_markup(tok, hide_price=bool(doc_info.get("hide_price")), pages=1)
        text = (
            f"🎟 **Управление билетом**\n\n"
            f"👤 ФИО: **{doc_info.get('fio') or 'не указано'}**\n"
            f"💰 Цена: **{doc_info.get('custom_price') or 'исходная'} ₽**\n"
            f"🪑 Место: **{doc_info.get('seat_info') or 'не указано'}**\n\n"
            f"Выберите действие с билетом:"
        )
        return send_text(chat, text, markup=markup)

    if data == "sub:cancel":
        invoice = get_store().status(uid)["invoice"]
        if not invoice or not invoice.get("initial_charge"):
            return send_text(chat, "У вас нет подписки с включённым автопродлением.")
        if tg("editUserStarSubscription", user_id=uid, telegram_payment_charge_id=invoice["initial_charge"], is_canceled=True):
            get_store().canceled(invoice["payload"])
            send_text(chat, "✅ Автопродление абонемента отменено. Доступ сохранится до конца оплаченного срока.")
            text_card, markup = build_subscription_card(uid)
            return edit_or_send(chat, msg_id, text_card, markup=markup)
        return send_text(chat, "Telegram не подтвердил отмену. Попробуйте позже или откройте управление подписками в Telegram.")

    if data.startswith("tpl:"):
        action = data[4:]
        if action == "menu":
            text_card, markup = build_templates_card(uid)
            return edit_or_send(chat, msg_id, text_card, markup=markup)
        if action == "save":
            store = get_store()
            custom_presets = store.get_user_presets(uid)
            if len(custom_presets) >= 10:
                return send_text(chat, "⚠️ Достигнут лимит: максимум 10 сохранённых шаблонов. Удалите ненужные, чтобы сохранить новый.")
            store.set_user_state(uid, "waiting_preset_name")
            return send_text(chat, "💾 Введите название для шаблона (например: Мой театр):\n(для отмены напишите: отмена)")
        if action == "del_menu":
            store = get_store()
            custom_presets = store.get_user_presets(uid)
            if not custom_presets:
                return send_text(chat, "У вас нет сохранённых шаблонов.")
            kb = []
            for p in custom_presets:
                kb.append([{"text": f"🗑 {p['name']}", "callback_data": f"tpl:del:{p['id']}"}])
            kb.append([{"text": "⬅️ Назад к шаблонам", "callback_data": "tpl:menu"}])
            text_del = "🗑 **Удаление шаблона**\n\nВыберите шаблон, который хотите удалить:"
            return edit_or_send(chat, msg_id, text_del, markup={"inline_keyboard": kb})
        if action.startswith("del:"):
            pid = int(action.split(":", 1)[1])
            store = get_store()
            store.delete_user_preset(pid, uid)
            send_text(chat, "🗑 Шаблон успешно удалён.")
            text_card, markup = build_templates_card(uid)
            return edit_or_send(chat, msg_id, text_card, markup=markup)
        if action.startswith("apply:"):
            preset_key = action.split(":", 1)[1]
            store = get_store()
            preset = None
            if preset_key.startswith("builtin_"):
                bkey = preset_key.replace("builtin_", "")
                preset = BUILTIN_PRESETS.get(bkey)
            elif preset_key.startswith("custom_"):
                pid = int(preset_key.replace("custom_", ""))
                preset = store.get_user_preset(pid, uid)

            if not preset:
                return send_text(chat, "⚠️ Шаблон не найден.")

            _apply_preset_to_user(uid, preset)
            send_text(chat, f"✅ Применён шаблон: {preset['name']}")
            text_card, markup = build_settings_card(uid)
            return edit_or_send(chat, msg_id, text_card, markup=markup)
        return

    if data.startswith("btpl:"):
        sub = data[5:]
        if sub.startswith("menu:"):
            batch_token = sub.split(":", 1)[1]
            text_card, markup = build_templates_card(uid, target_batch_token=batch_token)
            return edit_or_send(chat, msg_id, text_card, markup=markup)
        if sub.startswith("apply:"):
            if not has_access(uid):
                return show_status(chat, uid)
            parts = sub.split(":", 2)
            batch_token = parts[1]
            preset_key = parts[2]
            store = get_store()
            preset = None
            if preset_key.startswith("builtin_"):
                bkey = preset_key.replace("builtin_", "")
                preset = BUILTIN_PRESETS.get(bkey)
            elif preset_key.startswith("custom_"):
                pid = int(preset_key.replace("custom_", ""))
                preset = store.get_user_preset(pid, uid)

            if not preset:
                return send_text(chat, "⚠️ Шаблон не найден.")

            send_text(chat, f"⚙️ Применяю шаблон {preset['name']} ко всем билетам пачки...")
            count = _apply_preset_to_batch(batch_token, preset)
            send_text(chat, f"✅ Шаблон {preset['name']} применён к {count} {tickets_word(count)}!")
            text_dash, kb = _build_batch_dashboard(batch_token)
            return edit_or_send(chat, msg_id, text_dash, markup=kb)
        return

    if data.startswith("set:"):
        action = data.split(":", 1)[1]
        if action == "fio":
            get_store().set_user_state(uid, "waiting_fio_settings")
            return send_text(chat, "👤 Введите ФИО зрителя для билетов (например, Иванов Иван Иванович) или отправьте сброс для очистки:\n(для отмены напишите: отмена)")
        if action == "custom_price":
            get_store().set_user_state(uid, "waiting_price_settings")
            return send_text(chat, "💰 Введите цену билета по умолчанию в рублях (например, 2500) или отправьте сброс для очистки:\n(для отмены напишите: отмена)")
        if action == "price":
            cur = _user_settings(uid).get("hide_price", 0)
            get_store().update_user_setting(uid, "hide_price", 0 if cur else 1)
        elif action == "preview":
            cur = _user_settings(uid).get("auto_preview", 0)
            get_store().update_user_setting(uid, "auto_preview", 0 if cur else 1)
        elif action == "batch":
            modes = ["zip", "merge", "separate"]
            cur = _user_settings(uid).get("batch_mode", "zip")
            idx = (modes.index(cur) + 1) % len(modes) if cur in modes else 0
            get_store().update_user_setting(uid, "batch_mode", modes[idx])
        elif action == "reset":
            get_store().update_user_setting(uid, "fio", None)
            get_store().update_user_setting(uid, "custom_price", None)
            get_store().update_user_setting(uid, "hide_price", 0)
            get_store().update_user_setting(uid, "batch_mode", "zip")
            get_store().update_user_setting(uid, "auto_preview", 0)
            get_store().set_user_state(uid, None)
        text_card, markup = build_settings_card(uid)
        return edit_or_send(chat, msg_id, text_card, markup=markup)

    if not has_access(uid):
        return show_status(chat, uid)

    if not cooldown_ok(uid):
        return  # тяжёлый рендер не чаще раза в 3 секунды


    if data.startswith("toggleprice:"):
        parts = data.split(":")
        token = parts[1]
        hide_flag = (parts[2] == "1") if len(parts) > 2 else True
        doc_info = get_store().get_doc_token_info(token)
        if doc_info:
            target_file_id = doc_info["file_id"]
            custom_price = doc_info["custom_price"]
            cur_fio = doc_info["fio"] if doc_info["fio"] is not None else get_store().get_user_fio(uid)
            seat_info = doc_info.get("seat_info")
        else:
            target_file_id = get_store().get_doc_by_token(token) or token
            custom_price = get_store().get_user_custom_price(uid)
            cur_fio = get_store().get_user_fio(uid)
            seat_info = None
        if not target_file_id:
            return send_text(chat, "Не удалось определить файл билета.")
        data_bytes = download_file(target_file_id)
        if not data_bytes:
            return send_text(chat, "Не удалось скачать файл билета.")
        ok, result, info = clean_ticket_with_report(data_bytes, spectator_fio=cur_fio, hide_price=hide_flag, custom_price=custom_price, seat_info=seat_info)
        if not ok:
            return send_text(chat, f"Не удалось обновить билет: {result}")
        meta = ticket_meta.extract_ticket_metadata(result, srv=srv)
        out_name = ticket_meta.make_clean_ticket_filename(meta, fallback="ticket.pdf", hide_price=hide_flag)
        caption = ticket_meta.format_single_ticket_caption(meta, info)
        pages_cnt = 1
        try:
            with fitz.open(stream=result, filetype="pdf") as _d:
                pages_cnt = len(_d)
        except Exception:
            pass
        new_token = secrets.token_hex(8)
        _save_doc_token(new_token, target_file_id, custom_price=custom_price, hide_price=hide_flag, fio=cur_fio, seat_info=seat_info, uid=uid)
        markup = _single_ticket_markup(new_token, hide_price=hide_flag, pages=pages_cnt)
        sent = send_cleaned(chat, result, out_name, caption, reply_markup=markup)
        if not sent:
            return send_text(chat, "Не удалось отправить обновлённый билет.")
        if msg_id:
            tg("editMessageReplyMarkup", chat_id=chat, message_id=msg_id, reply_markup={"inline_keyboard": []})
        return

    if data.startswith("split:"):
        token = data.split(":", 1)[1]
        target_file_id = get_store().get_doc_by_token(token) or token
        if not target_file_id:
            return send_text(chat, "Не удалось определить файл билета.")
        data_bytes = download_file(target_file_id)
        if not data_bytes:
            return send_text(chat, "Не удалось скачать файл билета.")
        settings = _user_settings(uid)
        cur_fio = settings.get("fio")
        hide_price = bool(settings.get("hide_price", 0))
        custom_price = settings.get("custom_price")

        orig_entries = split_pdf_tickets(data_bytes, srv=srv, hide_price=hide_price)
        if len(orig_entries) <= 1:
            return send_text(chat, "В этом файле только одна страница (один билет).")

        send_text(chat, f"✂️ Разбиваю многостраничный PDF на отдельные билеты ({len(orig_entries)} шт.)...")
        b_token = secrets.token_hex(8)
        spool_dir = STATE_DIR / "batch_spool" / f"batch_{b_token}"
        spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        session_items = []
        for idx, (p_name, p_orig_bytes, p_meta) in enumerate(orig_entries):
            orig_path = spool_dir / f"orig_{idx}.pdf"
            clean_path = spool_dir / f"clean_{idx}.pdf"
            orig_path.write_bytes(p_orig_bytes)

            ok, p_clean_bytes, p_info = clean_ticket_with_report(
                p_orig_bytes,
                spectator_fio=cur_fio,
                hide_price=hide_price,
                custom_price=custom_price,
            )
            if not ok:
                p_clean_bytes = p_orig_bytes
            clean_path.write_bytes(p_clean_bytes)

            p_clean_meta = ticket_meta.extract_ticket_metadata(p_clean_bytes, srv=srv)
            item_doc_token = secrets.token_hex(8)
            _save_doc_token(item_doc_token, str(orig_path), custom_price=custom_price, hide_price=hide_price, fio=cur_fio, uid=uid)
            session_items.append({
                "idx": idx,
                "orig_name": p_name,
                "output_name": ticket_meta.make_clean_ticket_filename(p_clean_meta, fallback=p_name, hide_price=hide_price),
                "orig_path": str(orig_path),
                "clean_path": str(clean_path),
                "doc_token": item_doc_token,
                "meta": p_clean_meta,
                "fio": cur_fio,
                "seat_info": None,
                "custom_price": custom_price,
                "hide_price": hide_price,
                "del_texts": None,
            })
        get_store().save_batch_session(b_token, uid, chat, session_items)

        if len(session_items) <= 10:
            _unpack_batch_to_chat(chat, uid, b_token)
            text_dash, kb_dash = _build_batch_dashboard(b_token)
            send_text(chat, text_dash, reply_markup=kb_dash)
        else:
            zip_bytes, zip_name = _make_batch_zip(b_token)
            caption = ticket_meta.format_batch_tg_caption([{"name": it["orig_name"], "meta": it["meta"]} for it in session_items], len(session_items), 0)
            text_dash, kb_dash = _build_batch_dashboard(b_token)
            send_cleaned(chat, zip_bytes, zip_name, caption, reply_markup=kb_dash)
            send_text(chat, f"Готово 🩷 Билетов в архиве: {len(session_items)}.")
        if msg_id:
            tg("editMessageReplyMarkup", chat_id=chat, message_id=msg_id, reply_markup={"inline_keyboard": []})
        return

    if data == "unpack":
        doc = msg.get("document") or {}
        file_id = doc.get("file_id")
        if not file_id:
            return send_text(chat, "Не удалось найти файл архива для распаковки.")

        if (doc.get("file_size") or 0) > MAX_BYTES:
            return send_text(chat, "Архив больше 20 МБ — бот Telegram не может скачать файлы такого размера для распаковки.")

        zip_bytes = download_file(file_id)
        if not zip_bytes:
            return send_text(chat, "Не удалось скачать архив из Telegram. Попробуйте ещё раз.")

        try:
            entries = []
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
                for item in archive.infolist():
                    if item.is_dir() or not item.filename.lower().endswith(".pdf"):
                        continue
                    path = PurePosixPath(item.filename)
                    if (path.is_absolute() or ".." in path.parts or "\\" in item.filename
                            or ":" in item.filename or "\x00" in item.filename
                            or stat.S_ISLNK(item.external_attr >> 16)):
                        continue
                    if item.file_size > batches.MAX_PDF_BYTES:
                        continue
                    pdf_data = archive.read(item.filename)
                    entries.append((item.filename, path.name, pdf_data))
        except Exception as e:
            log.warning("unpack: failed to read zip: %s", _safe_error(e))
            return send_text(chat, "Не удалось прочитать ZIP-архив.")

        if not entries:
            return send_text(chat, "В архиве не найдено PDF-билетов.")

        send_text(chat, f"Распаковываю билеты ({len(entries)} шт.)...")

        sent_count = 0
        u_settings = _user_settings(uid)
        hide_price = bool(u_settings.get("hide_price", 0))
        for zip_path, file_name, pdf_data in entries:
            meta = ticket_meta.extract_ticket_metadata(pdf_data, srv=srv)
            clean_name = ticket_meta.make_clean_ticket_filename(meta, fallback=batches.safe_name(file_name), hide_price=hide_price)
            caption = ticket_meta.format_single_ticket_caption(meta, {"changed": True, "hide_price": hide_price})
            token = secrets.token_hex(8)
            get_store().save_doc_token(token, file_id)
            markup = _single_ticket_markup(token, hide_price=hide_price, pages=1)
            sent = send_cleaned(chat, pdf_data, clean_name, caption, reply_markup=markup)
            if sent:
                sent_count += 1
                if isinstance(sent, str):
                    get_store().save_doc_token(token, sent)
            time.sleep(0.3)

        if msg_id:
            tg("editMessageReplyMarkup", chat_id=chat, message_id=msg_id, reply_markup={"inline_keyboard": []})

        get_store().bump_processed(uid, sent_count)
        send_text(chat, f"Готово 🩷 Распаковано билетов: {sent_count} из {len(entries)}.")
        return

    if data == "merge":
        doc = msg.get("document") or {}
        file_id = doc.get("file_id")
        if not file_id:
            return send_text(chat, "Не удалось найти файл архива для склейки.")
        if (doc.get("file_size") or 0) > MAX_BYTES:
            return send_text(chat, "Архив больше 20 МБ — бот Telegram не может скачать файлы такого размера для склейки.")
        zip_bytes = download_file(file_id)
        if not zip_bytes:
            return send_text(chat, "Не удалось скачать архив из Telegram. Попробуйте ещё раз.")

        send_text(chat, "Склеиваю билеты пачки в один многостраничный PDF...")
        merged_bytes, merged_name, count, caption = merge_batch_pdfs(zip_bytes)
        if not merged_bytes or count == 0:
            return send_text(chat, "Не удалось объединить билеты из архива.")
        token = secrets.token_hex(8)
        get_store().save_doc_token(token, file_id)
        markup = {
            "inline_keyboard": [
                [{"text": "🖼 Фото-превью первого билета", "callback_data": f"preview:{token}"}]
            ]
        }
        sent = send_cleaned(chat, merged_bytes, merged_name, caption, reply_markup=markup)
        if not sent:
            return send_text(chat, "Не удалось отправить объединённый PDF.")
        send_text(chat, f"Готово 🩷 Билетов в файле: {count}.")
        return

    if data in {"preview", "compare"} or data.startswith("preview:") or data.startswith("compare:"):
        token = data.split(":", 1)[1] if ":" in data else None
        if token:
            batch_sess = get_store().get_batch_session(token)
            if batch_sess and batch_sess.get("items"):
                first_clean = Path(batch_sess["items"][0]["clean_path"])
                first_orig = Path(batch_sess["items"][0]["orig_path"])
                if first_clean.exists() and first_orig.exists():
                    img_bef = render_pdf_preview(first_orig.read_bytes())
                    img_aft = render_pdf_preview(first_clean.read_bytes())
                    if img_bef and img_aft:
                        photos = [
                            (img_bef, "📄 ДО (Исходный билет)"),
                            (img_aft, "✨ ПОСЛЕ (Очищенный билет 🩷)"),
                        ]
                        if send_media_group_photos(chat, photos):
                            return
                    elif img_aft:
                        return send_photo(chat, img_aft, caption="🖼 Превью первого билета в пачке")

        doc_info = get_store().get_doc_token_info(token) if token else None
        if doc_info:
            target_file_id = doc_info["file_id"]
            tok_price = doc_info["custom_price"]
            tok_hide = doc_info["hide_price"]
            tok_fio = doc_info["fio"]
            tok_seat = doc_info.get("seat_info")
        else:
            target_file_id = get_store().get_doc_by_token(token) if token else None
            tok_price = None
            tok_hide = None
            tok_fio = None
            tok_seat = None
        if not target_file_id:
            doc = msg.get("document") or {}
            target_file_id = doc.get("file_id")
        if not target_file_id:
            return send_text(chat, "Не удалось найти файл для генерации превью.")
        file_bytes = download_file(target_file_id)
        if not file_bytes:
            return send_text(chat, "Не удалось скачать файл из Telegram для превью.")

        u_settings = _user_settings(uid)
        eff_fio = tok_fio if tok_fio is not None else u_settings.get("fio")
        eff_hide = tok_hide if tok_hide is not None else bool(u_settings.get("hide_price", 0))
        eff_price = tok_price if tok_price is not None else u_settings.get("custom_price")

        pdf_bytes = None
        first_name = ""
        if file_bytes[:4] == b"PK\x03\x04":
            try:
                with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
                    for item in archive.infolist():
                        if not item.is_dir() and item.filename.lower().endswith(".pdf"):
                            pdf_bytes = archive.read(item.filename)
                            first_name = PurePosixPath(item.filename).name
                            break
            except Exception:
                pass
        else:
            pdf_bytes = file_bytes

        if not pdf_bytes:
            return send_text(chat, "В файле не найдено страниц PDF для превью.")

        # Render BEFORE preview
        img_before = render_pdf_preview(pdf_bytes)
        if not img_before:
            return send_text(chat, "Не удалось сформировать изображение превью.")

        # Render AFTER preview
        ok, cleaned_bytes, _ = clean_ticket_with_report(
            pdf_bytes, spectator_fio=eff_fio, hide_price=eff_hide, custom_price=eff_price, seat_info=tok_seat
        )
        after_bytes = cleaned_bytes if ok else pdf_bytes
        img_after = render_pdf_preview(after_bytes)

        if not img_after:
            cap = f"🖼 Фото билета ({first_name})" if first_name else "🖼 Фото билета"
            return send_photo(chat, img_before, caption=cap)

        cap_before = f"📄 ДО: Исходный билет ({first_name})" if first_name else "📄 ДО (Исходный билет)"
        cap_after = f"✨ ПОСЛЕ: Очищенный билет ({first_name}) 🩷" if first_name else "✨ ПОСЛЕ (Очищенный билет 🩷)"

        photos = [(img_before, cap_before), (img_after, cap_after)]
        if not send_media_group_photos(chat, photos):
            return send_text(chat, "Не удалось отправить фото До и После.")
        return

    if data.startswith("bcfg:"):
        parts = data.split(":")
        batch_token = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        text, markup = _build_batch_dashboard(batch_token, page=page)
        if not markup:
            return send_text(chat, text)
        return edit_or_send(chat, msg_id, text, markup)

    if data.startswith("bedit:"):
        parts = data.split(":")
        batch_token = parts[1]
        idx = int(parts[2])
        text, markup = _build_batch_item_card(batch_token, idx)
        if not markup:
            return send_text(chat, text)
        return edit_or_send(chat, msg_id, text, markup)

    if data.startswith("bset:"):
        parts = data.split(":")
        action = parts[1]
        batch_token = parts[2]
        idx = int(parts[3])
        session = get_store().get_batch_session(batch_token)
        if not session or idx >= len(session.get("items", [])):
            return send_text(chat, "⚠️ Билет не найден.")
        item = session["items"][idx]

        if action == "quick":
            get_store().set_user_state(uid, f"waiting_batch_item_quick:{batch_token}:{idx}")
            prompt = (
                f"⚡️ **Быстрая правка билета №{idx+1}** (всё в 1 строке):\n\n"
                f"Отправьте любые параметры через пробел или запятую:\n"
                f"• ФИО, ряд и место, цену (например: `2500` или `3000 руб`), `скрыть цену`\n\n"
                f"💡 Примеры:\n"
                f"• `Иванов И.И. 2500 ряд 3 место 12`\n"
                f"• `Петров 3500`\n"
                f"• `3 12 скрыть цену`\n"
                f"• `сброс` — вернуть исходные данные билета\n\n"
                f"(для отмены напишите: отмена)"
            )
            return send_text(chat, prompt)
        elif action == "fio":
            get_store().set_user_state(uid, f"waiting_batch_item_fio:{batch_token}:{idx}")
            cur_fio = item.get("fio") or "не установлено"
            prompt = (
                f"👤 Билет №{idx+1}: сейчас ФИО — {cur_fio}.\n\n"
                f"Введите новое ФИО зрителя (например: Иванов Иван Иванович) или напишите сброс:\n"
                f"(для отмены напишите: отмена)"
            )
            return send_text(chat, prompt)
        elif action == "seat":
            get_store().set_user_state(uid, f"waiting_batch_item_seat:{batch_token}:{idx}")
            prompt = (
                f"🪑 Билет №{idx+1}: введите ряд и место.\n\n"
                f"Примеры ввода:\n"
                f"• Ряд 3 Место 12\n"
                f"• Партер, ряд 5, место 10\n"
                f"• 3 12 (быстрый ввод: ряд 3, место 12)\n"
                f"• сброс — вернуть исходные места\n"
                f"(для отмены напишите: отмена)"
            )
            return send_text(chat, prompt)
        elif action == "price":
            get_store().set_user_state(uid, f"waiting_batch_item_price:{batch_token}:{idx}")
            prompt = (
                f"💰 Билет №{idx+1}: введите новую цену в рублях (например: 2500) или напишите сброс для исходной цены:\n"
                f"(для отмены напишите: отмена)"
            )
            return send_text(chat, prompt)

    if data.startswith("btog:price:"):
        parts = data.split(":")
        batch_token = parts[2]
        idx = int(parts[3])
        session = get_store().get_batch_session(batch_token)
        if not session or idx >= len(session.get("items", [])):
            return send_text(chat, "⚠️ Билет не найден.")
        cur_hide = bool(session["items"][idx].get("hide_price", False))
        ok, res = _reprocess_batch_item(batch_token, idx, hide_price=(not cur_hide))
        if not ok:
            return send_text(chat, f"Не удалось обновить цену: {res}")
        text, markup = _build_batch_item_card(batch_token, idx)
        return edit_or_send(chat, msg_id, text, markup)

    if data.startswith("bprev:"):
        parts = data.split(":")
        batch_token = parts[1]
        idx = int(parts[2])
        session = get_store().get_batch_session(batch_token)
        if not session or idx >= len(session.get("items", [])):
            return send_text(chat, "⚠️ Билет не найден.")
        item = session["items"][idx]
        clean_p = Path(item["clean_path"])
        if not clean_p.exists():
            return send_text(chat, "Файл билета не найден.")
        img_bytes = render_pdf_preview(clean_p.read_bytes())
        if not img_bytes:
            return send_text(chat, "Не удалось создать превью билета.")
        return send_photo(chat, img_bytes, caption=f"🖼 Превью билета №{idx+1}")

    if data.startswith("bsend:"):
        parts = data.split(":")
        batch_token = parts[1]
        idx = int(parts[2])
        session = get_store().get_batch_session(batch_token)
        if not session or idx >= len(session.get("items", [])):
            return send_text(chat, "⚠️ Билет не найден.")
        item = session["items"][idx]
        clean_p = Path(item["clean_path"])
        orig_p = Path(item["orig_path"])
        if not clean_p.exists():
            return send_text(chat, "Файл билета не найден.")
        token = secrets.token_hex(8)
        _save_doc_token(
            token,
            str(orig_p),
            custom_price=item.get("custom_price"),
            hide_price=item.get("hide_price", False),
            fio=item.get("fio"),
            seat_info=item.get("seat_info"),
            del_texts=item.get("del_texts"),
            uid=uid,
        )
        markup = _single_ticket_markup(token, hide_price=item.get("hide_price", False), pages=1)
        caption = ticket_meta.format_single_ticket_caption(item.get("meta") or {}, {"changed": True, "hide_price": item.get("hide_price", False)})
        sent = send_cleaned(chat, clean_p.read_bytes(), item.get("output_name") or f"ticket_{idx+1}.pdf", caption, reply_markup=markup)
        if sent:
            get_store().bump_processed(uid)
        return

    if data.startswith("bmass:"):
        parts = data.split(":")
        action = parts[1]
        batch_token = parts[2]
        session = get_store().get_batch_session(batch_token)
        if not session:
            return send_text(chat, "⚠️ Сессия пачки не найдена или истекла.")
        total = len(session.get("items", []))

        if action == "combo":
            get_store().set_user_state(uid, f"waiting_batch_combo:{batch_token}")
            prompt = (
                f"⚡️ **Супер-редактор пачки билетов** (всего {total} шт.):\n\n"
                f"Вы можете настроить все билеты одним сообщением!\n\n"
                f"📋 **Вариант 1: Список построчно (по 1 строке на каждый билет):**\n"
                f"`1. Иванов Иван — ряд 3 место 10 — 3000 руб`\n"
                f"`2. Петров Петр — ряд 3 место 11 — 3000 руб`\n"
                f"`3. Сидорова Анна — ряд 3 место 12 — скрыть цену`\n\n"
                f"💡 *В каждой строке можно указывать ФИО, ряд и место, цену (или 'скрыть цену'). Номер в начале строки не обязателен.*\n\n"
                f"⚡️ **Вариант 2: Одной строкой для всей пачки:**\n"
                f"• `Ряд 5 место 10, 4500 руб` — рассадит места подряд (10, 11, 12...) и поставит цену 4500 ₽ на все {total} билетов\n"
                f"• `3500 руб` — задаст цену 3500 ₽ всем билетам\n"
                f"• `скрыть цену` — скроет цены на всех билетах\n"
                f"• `сброс` — вернёт все билеты к исходному виду\n\n"
                f"(для отмены напишите: отмена)"
            )
            return send_text(chat, prompt)
        elif action == "fio":
            get_store().set_user_state(uid, f"waiting_batch_fio:{batch_token}")
            prompt = (
                f"👥 **Массовое назначение ФИО** (в пачке {total} билетов):\n\n"
                f"Отправьте список ФИО одним сообщением, каждое ФИО с новой строки.\n\n"
                f"Пример:\n"
                f"Иванов Иван Иванович\n"
                f"Петров Петр Сергеевич\n"
                f"Сидорова Анна Павловна\n\n"
                f"Имена будут назначены на билеты 1, 2, 3... по порядку.\n"
                f"(для отмены напишите: отмена)"
            )
            return send_text(chat, prompt)
        elif action == "seat":
            get_store().set_user_state(uid, f"waiting_batch_seat:{batch_token}")
            prompt = (
                f"🪑 **Рассадка по порядку** (в пачке {total} билетов):\n\n"
                f"Введите начальное место, например: Ряд 5 Место 10 или просто 5 10.\n\n"
                f"Билеты получат места подряд:\n"
                f"• Билет №1: Ряд 5, Место 10\n"
                f"• Билет №2: Ряд 5, Место 11\n"
                f"• Билет №3: Ряд 5, Место 12\n"
                f"и так далее.\n\n"
                f"(для отмены напишите: отмена)"
            )
            return send_text(chat, prompt)
        elif action == "price":
            get_store().set_user_state(uid, f"waiting_batch_price:{batch_token}")
            prompt = (
                f"💰 **Задать общую цену** (в пачке {total} билетов):\n\n"
                f"Введите цену в рублях (например: 3500) или напишите сброс для возврата исходных цен:\n"
                f"(для отмены напишите: отмена)"
            )
            return send_text(chat, prompt)
        elif action == "hideprice":
            send_text(chat, f"🎁 Скрываю цены на всех билетах пачки ({total} шт.)...")
            for i in range(total):
                _reprocess_batch_item(batch_token, i, hide_price=True)
            send_text(chat, "✅ Цены скрыты на всех билетах пачки!")
            text, markup = _build_batch_dashboard(batch_token)
            return send_text(chat, text, markup=markup)

    if data.startswith("bzip:"):
        batch_token = data.split(":", 1)[1]
        send_text(chat, "📦 Собираю свежий ZIP-архив с обновлёнными билетами...")
        zip_bytes, zip_name = _make_batch_zip(batch_token)
        if not zip_bytes:
            return send_text(chat, "Не удалось собрать ZIP-архив.")
        session = get_store().get_batch_session(batch_token)
        items = session.get("items", []) if session else []
        caption = ticket_meta.format_batch_tg_caption([{"name": it["orig_name"], "meta": it["meta"]} for it in items], len(items), 0)
        text_dash, kb = _build_batch_dashboard(batch_token)
        sent = send_cleaned(chat, zip_bytes, zip_name, caption, reply_markup=kb)
        if sent:
            get_store().bump_processed(uid, len(items))
        return

    if data.startswith("bmerge:"):
        batch_token = data.split(":", 1)[1]
        send_text(chat, "📑 Склеиваю обновлённые билеты в один многостраничный PDF...")
        m_bytes, m_name, count, caption = _make_batch_merged_pdf(batch_token)
        if not m_bytes or count == 0:
            return send_text(chat, "Не удалось объединить билеты в PDF.")
        text_dash, kb = _build_batch_dashboard(batch_token)
        sent = send_cleaned(chat, m_bytes, m_name, caption, reply_markup=kb)
        if sent:
            get_store().bump_processed(uid, count)
        return

    if data.startswith("bunpack:"):
        batch_token = data.split(":", 1)[1]
        _unpack_batch_to_chat(chat, uid, batch_token)
        if msg_id:
            tg("editMessageReplyMarkup", chat_id=chat, message_id=msg_id, reply_markup={"inline_keyboard": []})
        return

    if data == "quick" or data.startswith("quick:"):
        token = data.split(":", 1)[1] if ":" in data else None
        target_file_id = None
        doc_info = get_store().get_doc_token_info(token) if token else None
        if doc_info:
            target_file_id = doc_info["file_id"]
        elif token:
            target_file_id = get_store().get_doc_by_token(token) or token
        if not target_file_id:
            doc = msg.get("document") or {}
            target_file_id = doc.get("file_id")
        if not target_file_id:
            return send_text(chat, "Не удалось определить файл билета.")
        state_key = token or target_file_id
        get_store().set_user_state(uid, f"waiting_quick:{state_key}")
        cur_fio = doc_info.get("fio") if doc_info else None
        cur_price = doc_info.get("custom_price") if doc_info else None
        cur_seat = doc_info.get("seat_info") if doc_info else None
        cur_hide = doc_info.get("hide_price") if doc_info else False

        info_lines = []
        if cur_fio:
            info_lines.append(f"• ФИО: {cur_fio}")
        if cur_seat:
            info_lines.append(f"• Место: {cur_seat}")
        if cur_hide:
            info_lines.append("• Цена: скрыта")
        elif cur_price is not None:
            info_lines.append(f"• Цена: {cur_price} ₽")

        status_prefix = ("Сейчас установлено:\n" + "\n".join(info_lines) + "\n\n") if info_lines else ""
        prompt = (
            f"⚡️ **Быстрая правка (всё в 1 строке)**\n\n"
            f"{status_prefix}"
            f"Отправьте данные одной строкой. Можно указать любое сочетание (ФИО, цену, места):\n\n"
            f"💡 **Примеры:**\n"
            f"• `Иванов И.И. 2500 ряд 3 место 12`\n"
            f"• `Петров 3500`\n"
            f"• `3 12 4000 руб`\n"
            f"• `Партер ряд 1 место 5 скрыть цену`\n"
            f"• `сброс` — вернуть исходные данные билета\n\n"
            f"(для отмены напишите: отмена)"
        )
        return send_text(chat, prompt)

    if data == "price" or data.startswith("price:"):
        token = data.split(":", 1)[1] if ":" in data else None
        target_file_id = None
        doc_info = get_store().get_doc_token_info(token) if token else None
        if doc_info:
            target_file_id = doc_info["file_id"]
        elif token:
            target_file_id = get_store().get_doc_by_token(token) or token
        if not target_file_id:
            doc = msg.get("document") or {}
            target_file_id = doc.get("file_id")
        if not target_file_id:
            return send_text(chat, "Не удалось определить файл билета.")
        state_key = token or target_file_id
        get_store().set_user_state(uid, f"waiting_price:{state_key}")
        cur_price = doc_info["custom_price"] if doc_info and doc_info["custom_price"] is not None else get_store().get_user_custom_price(uid)
        prompt = (
            f"💰 Сейчас установлена цена: {cur_price} ₽\n\n"
            f"Введите новую цену билета в рублях (например, 2500 или 3500) или напишите сброс для возврата исходной цены:\n"
            f"(для отмены напишите: отмена)"
            if cur_price is not None else
            f"💰 Введите новую цену билета в рублях (например, 2500 или 3500) для печати на билете:\n"
            f"(для отмены напишите: отмена)"
        )
        return send_text(chat, prompt)

    if data == "fio" or data.startswith("fio:"):
        token = data.split(":", 1)[1] if ":" in data else None
        target_file_id = None
        doc_info = get_store().get_doc_token_info(token) if token else None
        if doc_info:
            target_file_id = doc_info["file_id"]
        elif token:
            target_file_id = get_store().get_doc_by_token(token) or token
        if not target_file_id:
            doc = msg.get("document") or {}
            target_file_id = doc.get("file_id")
        if not target_file_id:
            return send_text(chat, "Не удалось определить файл билета.")
        state_key = token or target_file_id
        get_store().set_user_state(uid, f"waiting_fio:{state_key}")
        cur_fio = doc_info["fio"] if doc_info and doc_info["fio"] is not None else get_store().get_user_fio(uid)
        prompt = (
            f"👤 Сейчас установлено: {cur_fio}\n\n"
            f"Введите новое ФИО зрителя (например, Иванов Иван Иванович) или команду /fio сброс:\n"
            f"(для отмены напишите: отмена)"
            if cur_fio else
            f"👤 Введите ФИО зрителя (например, Иванов Иван Иванович) для печати на билете:\n"
            f"(для отмены напишите: отмена)"
        )
        return send_text(chat, prompt)

    if data == "seat" or data.startswith("seat:"):
        token = data.split(":", 1)[1] if ":" in data else None
        target_file_id = None
        doc_info = get_store().get_doc_token_info(token) if token else None
        if doc_info:
            target_file_id = doc_info["file_id"]
        elif token:
            target_file_id = get_store().get_doc_by_token(token) or token
        if not target_file_id:
            doc = msg.get("document") or {}
            target_file_id = doc.get("file_id")
        if not target_file_id:
            return send_text(chat, "Не удалось определить файл билета.")
        state_key = token or target_file_id
        get_store().set_user_state(uid, f"waiting_seat:{state_key}")
        cur_seat = doc_info.get("seat_info") if doc_info else None
        kb = _sector_keyboard(state_key)
        prefix = f"🪑 Сейчас указано: {cur_seat}\n\n" if cur_seat else "🪑 Введите новый ряд, место или сектор:\n\n"
        prompt = (
            f"{prefix}"
            f"Выберите сектор кнопкой ниже или напишите в чат вручную:\n"
            f"(бывают и любые другие сектора: Сектор А, VIP-ложа, Танцпол, Подиум, Ярус 2 и т.д.)\n\n"
            f"Примеры ввода:\n"
            f"• Ряд 3 Место 12\n"
            f"• Партер, ряд 5, место 10\n"
            f"• 3 12 (быстрый ввод: ряд 3, место 12)\n"
            f"• Ряд 4 Места 12-14 (для нескольких билетов подряд)\n"
            f"• сброс — вернуть исходные места\n"
            f"(для отмены напишите: отмена)"
        )
        return send_text(chat, prompt, reply_markup=kb)

    if data.startswith("choose_sec:"):
        parts = data.split(":", 2)
        token = parts[1]
        sector = parts[2] if len(parts) > 2 else ""
        get_store().set_user_state(uid, f"waiting_seat_row:{token}:{sector}")
        prompt = (
            f"🎭 Выбран сектор: **{sector}**.\n\n"
            f"Теперь введите ряд и место (например: Ряд 3 Место 12 или просто 3 12):\n"
            f"• Если на билете несколько страниц, места назначим подряд автоматически (например 3 12 -> 12, 13, 14...)\n"
            f"• Если хотите применить только сектор без смены ряда/места, напишите: только сектор\n"
            f"(для отмены напишите: отмена)"
        )
        return send_text(chat, prompt)

    if data.startswith("pushkin:"):
        token = data.split(":", 1)[1] if ":" in data else None
        return _reissue_ticket_with_pushkin_strip(chat, uid, token)

    if data.startswith("avito:"):
        token = data.split(":", 1)[1] if ":" in data else None
        return _send_avito_preview(chat, uid, token)


def _reissue_ticket_with_quick(chat, uid, ref, text_input):
    if not cooldown_ok(uid):
        return send_text(chat, "Слишком часто. Подожди пару секунд и повтори.")
    get_store().set_user_state(uid, None)
    cleaned_cmd = text_input.strip()
    if cleaned_cmd.lower() in {"отмена", "cancel"}:
        return send_text(chat, "Быстрая правка отменена.")

    doc_info = get_store().get_doc_token_info(ref)
    if doc_info:
        target_file_id = doc_info["file_id"]
        tok_price = doc_info["custom_price"]
        tok_hide = doc_info["hide_price"]
        tok_fio = doc_info["fio"]
        tok_seat = doc_info.get("seat_info")
        tok_del = doc_info.get("del_texts")
        tok_pushkin = doc_info.get("strip_pushkin")
    else:
        target_file_id = get_store().get_doc_by_token(ref) or ref
        tok_price = None
        tok_hide = None
        tok_fio = None
        tok_seat = None
        tok_del = None
        tok_pushkin = False

    q = seat_tools.parse_quick_edit(cleaned_cmd)
    if (not q["reset_all"] and not q["reset_fio"] and not q["reset_price"] and not q["reset_seat"]
            and q["fio"] is None and q["custom_price"] is None and q["hide_price"] is None and q["seat_info"] is None):
        return send_text(
            chat,
            "⚠️ Не удалось распознать параметры быстрой правки.\n\n"
            "Вы можете указать любые данные вместе или по отдельности:\n"
            "• `Иванов И.И. 2500 ряд 3 место 12`\n"
            "• `Петров 3500`\n"
            "• `3 12 4000 руб`\n"
            "• `скрыть цену`\n"
            "• `сброс` — вернуть исходные данные\n\n"
            "(для отмены напишите: отмена)"
        )

    if q["reset_all"]:
        new_fio = None
        new_price = None
        new_hide = False
        new_seat = None
    else:
        if q["reset_fio"]:
            new_fio = None
        elif q["fio"] is not None:
            new_fio = q["fio"]
        else:
            new_fio = tok_fio

        if q["reset_price"]:
            new_price = None
            new_hide = False
        elif q["hide_price"] is not None:
            new_hide = q["hide_price"]
            new_price = tok_price if not new_hide else None
        elif q["custom_price"] is not None:
            new_price = q["custom_price"]
            new_hide = False
        else:
            new_price = tok_price
            new_hide = tok_hide

        if q["reset_seat"]:
            new_seat = None
        elif q["seat_info"] is not None:
            new_seat = seat_tools.format_seat_summary(q["seat_info"])
        else:
            new_seat = tok_seat

    data = download_file(target_file_id)
    if not data:
        return send_text(chat, "Не удалось скачать исходный файл. Пожалуйста, отправьте билет заново.")

    u_settings = _user_settings(uid)
    if q["reset_all"] or q["reset_price"]:
        hide_price = False
        custom_price = None
    else:
        hide_price = new_hide if new_hide is not None else bool(u_settings.get("hide_price", 0))
        custom_price = new_price if new_price is not None else u_settings.get("custom_price")

    if q["reset_all"] or q["reset_fio"]:
        cur_fio = None
    else:
        cur_fio = new_fio if new_fio is not None else get_store().get_user_fio(uid)

    summary_items = []
    if cur_fio:
        summary_items.append(f"👤 {cur_fio}")
    if new_seat:
        summary_items.append(f"🪑 {new_seat}")
    if hide_price:
        summary_items.append("🎁 Цена скрыта")
    elif custom_price is not None:
        summary_items.append(f"💰 {custom_price} ₽")
    changes_str = " · ".join(summary_items) if summary_items else "исходные параметры"
    send_text(chat, f"⚡️ Применяю: {changes_str}... Перевыпускаю билет...")

    ok, result, info = clean_ticket_with_report(
        data,
        spectator_fio=cur_fio,
        hide_price=hide_price,
        custom_price=custom_price,
        seat_info=new_seat,
        del_texts=tok_del,
        strip_pushkin=tok_pushkin
    )
    if not ok:
        return send_text(chat, f"Не удалось обновить билет: {result}")

    meta = ticket_meta.extract_ticket_metadata(result, srv=srv)
    out_name = ticket_meta.make_clean_ticket_filename(meta, fallback="ticket.pdf", hide_price=hide_price)
    caption = ticket_meta.format_single_ticket_caption(meta, info)
    new_token = secrets.token_hex(8)
    _save_doc_token(new_token, target_file_id, custom_price=custom_price, hide_price=hide_price, fio=cur_fio, seat_info=new_seat, del_texts=tok_del, strip_pushkin=tok_pushkin, uid=uid)
    pages_cnt = 1
    try:
        with fitz.open(stream=result, filetype="pdf") as _d:
            pages_cnt = len(_d)
    except Exception:
        pass
    markup = _single_ticket_markup(new_token, hide_price=hide_price, pages=pages_cnt)
    sent = send_cleaned(chat, result, out_name, caption, reply_markup=markup)
    if not sent:
        return send_text(chat, "Не удалось отправить обновлённый билет.")
    auto_preview = bool(u_settings.get("auto_preview", 0))
    if auto_preview:
        img_bytes = render_pdf_preview(result)
        if img_bytes:
            send_photo(chat, img_bytes, caption="🖼 Авто-превью билета")
    return


def _reissue_ticket_with_seat(chat, uid, ref, seat_input_text):
    if not cooldown_ok(uid):
        return send_text(chat, "Слишком часто. Подожди пару секунд и повтори.")
    get_store().set_user_state(uid, None)
    cleaned_cmd = seat_input_text.strip()
    if cleaned_cmd.lower() in {"отмена", "cancel"}:
        return send_text(chat, "Изменение места отменено.")

    doc_info = get_store().get_doc_token_info(ref)
    if doc_info:
        target_file_id = doc_info["file_id"]
        tok_price = doc_info["custom_price"]
        tok_hide = doc_info["hide_price"]
        tok_fio = doc_info["fio"]
        tok_del = doc_info.get("del_texts")
        tok_pushkin = doc_info.get("strip_pushkin")
    else:
        target_file_id = get_store().get_doc_by_token(ref) or ref
        tok_price = None
        tok_hide = None
        tok_fio = None
        tok_del = None
        tok_pushkin = False

    if cleaned_cmd.lower() in {"сброс", "reset", "clear", "none"}:
        new_seat = None
        send_text(chat, "Место сброшено к исходному. Перевыпускаю билет...")
    else:
        parsed = seat_tools.parse_seat_input(cleaned_cmd)
        if not parsed:
            return send_text(
                chat,
                "Не удалось распознать ряд/место/сектор.\n"
                "Примеры ввода:\n"
                "• Ряд 3 Место 12\n"
                "• Партер, ряд 5, место 10\n"
                "• 3 12\n"
                "• Сектор Балкон\n"
                "(для отмены напишите: отмена)"
            )
        new_seat = cleaned_cmd
        summary = seat_tools.format_seat_summary(parsed)
        send_text(chat, f"Место выбрано ({summary}). Перевыпускаю билет...")

    data = download_file(target_file_id)
    if not data:
        return send_text(chat, "Не удалось скачать исходный файл. Пожалуйста, отправьте билет заново.")

    u_settings = _user_settings(uid)
    cur_fio = tok_fio if tok_fio is not None else get_store().get_user_fio(uid)
    hide_price = tok_hide if tok_hide is not None else bool(u_settings.get("hide_price", 0))
    custom_price = tok_price if tok_price is not None else u_settings.get("custom_price")

    ok, result, info = clean_ticket_with_report(
        data,
        spectator_fio=cur_fio,
        hide_price=hide_price,
        custom_price=custom_price,
        seat_info=new_seat,
        del_texts=tok_del,
        strip_pushkin=tok_pushkin
    )
    if not ok:
        return send_text(chat, f"Не удалось обновить билет: {result}")

    meta = ticket_meta.extract_ticket_metadata(result, srv=srv)
    out_name = ticket_meta.make_clean_ticket_filename(meta, fallback="ticket.pdf", hide_price=hide_price)
    caption = ticket_meta.format_single_ticket_caption(meta, info)
    new_token = secrets.token_hex(8)
    _save_doc_token(new_token, target_file_id, custom_price=custom_price, hide_price=hide_price, fio=cur_fio, seat_info=new_seat, del_texts=tok_del, strip_pushkin=tok_pushkin, uid=uid)
    pages_cnt = 1
    try:
        with fitz.open(stream=result, filetype="pdf") as _d:
            pages_cnt = len(_d)
    except Exception:
        pass
    markup = _single_ticket_markup(new_token, hide_price=hide_price, pages=pages_cnt)
    sent = send_cleaned(chat, result, out_name, caption, reply_markup=markup)
    if not sent:
        return send_text(chat, "Не удалось отправить обновлённый билет.")
    auto_preview = bool(u_settings.get("auto_preview", 0))
    if auto_preview:
        img_bytes = render_pdf_preview(result)
        if img_bytes:
            send_photo(chat, img_bytes, caption="🖼 Авто-превью билета")
    return


def _reissue_ticket_with_pushkin_strip(chat, uid, ref):
    get_store().set_user_state(uid, None)
    doc_info = get_store().get_doc_token_info(ref)
    if doc_info:
        target_file_id = doc_info["file_id"]
        tok_price = doc_info["custom_price"]
        tok_hide = doc_info["hide_price"]
        tok_fio = doc_info["fio"]
        tok_seat = doc_info["seat_info"]
        tok_del = doc_info.get("del_texts")
    else:
        target_file_id = get_store().get_doc_by_token(ref) or ref
        tok_price = None
        tok_hide = None
        tok_fio = None
        tok_seat = None
        tok_del = None

    data = download_file(target_file_id)
    if not data:
        return send_text(chat, "Не удалось скачать исходный файл.")

    u_settings = _user_settings(uid)
    cur_fio = tok_fio if tok_fio is not None else get_store().get_user_fio(uid)
    hide_price = tok_hide if tok_hide is not None else bool(u_settings.get("hide_price", 0))
    custom_price = tok_price if tok_price is not None else u_settings.get("custom_price")

    send_text(chat, "🧹 Снимаю чары Черномора и перевожу билет в коммерческий вид...")
    ok, result, info = clean_ticket_with_report(
        data,
        spectator_fio=cur_fio,
        hide_price=hide_price,
        custom_price=custom_price,
        seat_info=tok_seat,
        del_texts=tok_del,
        strip_pushkin=True
    )
    if not ok:
        return send_text(chat, f"Не удалось обновить билет: {result}")

    meta = ticket_meta.extract_ticket_metadata(result, srv=srv)
    out_name = ticket_meta.make_clean_ticket_filename(meta, fallback="ticket.pdf", hide_price=hide_price)
    caption = "🧹 Чары Черномора сняты с билета!\n\n" + ticket_meta.format_single_ticket_caption(meta, info)
    new_token = secrets.token_hex(8)
    _save_doc_token(new_token, target_file_id, custom_price=custom_price, hide_price=hide_price, fio=cur_fio, seat_info=tok_seat, del_texts=tok_del, strip_pushkin=True, uid=uid)
    pages_cnt = 1
    try:
        with fitz.open(stream=result, filetype="pdf") as _d:
            pages_cnt = len(_d)
    except Exception:
        pass
    markup = _single_ticket_markup(new_token, hide_price=hide_price, pages=pages_cnt)
    sent = send_cleaned(chat, result, out_name, caption, reply_markup=markup)
    if not sent:
        return send_text(chat, "Не удалось отправить обновлённый билет.")
    auto_preview = bool(u_settings.get("auto_preview", 0))
    if auto_preview:
        img_bytes = render_pdf_preview(result)
        if img_bytes:
            send_photo(chat, img_bytes, caption="🖼 Авто-превью билета")
    return


def _send_avito_preview(chat, uid, ref):
    doc_info = get_store().get_doc_token_info(ref)
    if doc_info:
        target_file_id = doc_info["file_id"]
        tok_price = doc_info["custom_price"]
        tok_hide = doc_info["hide_price"]
        tok_fio = doc_info["fio"]
        tok_seat = doc_info["seat_info"]
        tok_del = doc_info.get("del_texts")
        tok_pushkin = doc_info.get("strip_pushkin")
    else:
        target_file_id = get_store().get_doc_by_token(ref) or ref
        tok_price = None
        tok_hide = None
        tok_fio = None
        tok_seat = None
        tok_del = None
        tok_pushkin = False

    data = download_file(target_file_id)
    if not data:
        return send_text(chat, "Не удалось скачать файл для создания превью.")

    u_settings = _user_settings(uid)
    cur_fio = tok_fio if tok_fio is not None else get_store().get_user_fio(uid)
    hide_price = tok_hide if tok_hide is not None else bool(u_settings.get("hide_price", 0))
    custom_price = tok_price if tok_price is not None else u_settings.get("custom_price")

    ok, result, info = clean_ticket_with_report(
        data,
        spectator_fio=cur_fio,
        hide_price=hide_price,
        custom_price=custom_price,
        seat_info=tok_seat,
        del_texts=tok_del,
        strip_pushkin=tok_pushkin
    )
    pdf_to_mask = result if ok and isinstance(result, (bytes, bytearray)) else data
    png_bytes = eraser_tools.make_avito_preview(pdf_to_mask, srv=srv)
    if not png_bytes:
        return send_text(chat, "Не удалось сгенерировать защищённое фото.")

    caption = (
        "🛡 **Защищённое фото для публикации на маркетплейсах и в соцсетях**\n\n"
        "• Штрихкоды и QR-коды надёжно скрыты защитной плашкой — билет защищён от копирования до покупки.\n"
        "• Мероприятие, дата, ряд, место и цена отлично видны покупателям.\n"
        "• Можно сразу выкладывать в объявление!"
    )
    return send_photo(chat, png_bytes, caption=caption)


def _reissue_ticket_with_deletion(chat, uid, ref, target_text):
    get_store().set_user_state(uid, None)
    clean_target = target_text.strip()
    if clean_target.lower() in {"отмена", "cancel"}:
        return send_text(chat, "Удаление текста отменено.")

    doc_info = get_store().get_doc_token_info(ref)
    if doc_info:
        target_file_id = doc_info["file_id"]
        tok_price = doc_info["custom_price"]
        tok_hide = doc_info["hide_price"]
        tok_fio = doc_info["fio"]
        tok_seat = doc_info["seat_info"]
        tok_del = doc_info.get("del_texts")
        tok_pushkin = doc_info.get("strip_pushkin")
    else:
        target_file_id = get_store().get_doc_by_token(ref) or ref
        tok_price = None
        tok_hide = None
        tok_fio = None
        tok_seat = None
        tok_del = None
        tok_pushkin = False

    if clean_target.lower() in {"сброс", "reset", "clear"}:
        new_del = None
        send_text(chat, "Все ручные удаления сброшены. Перевыпускаю билет...")
    else:
        new_del = f"{tok_del}||{clean_target}" if tok_del else clean_target
        send_text(chat, f"✂️ Стираю {clean_target} с билета...")

    data = download_file(target_file_id)
    if not data:
        return send_text(chat, "Не удалось скачать исходный файл.")

    u_settings = _user_settings(uid)
    cur_fio = tok_fio if tok_fio is not None else get_store().get_user_fio(uid)
    hide_price = tok_hide if tok_hide is not None else bool(u_settings.get("hide_price", 0))
    custom_price = tok_price if tok_price is not None else u_settings.get("custom_price")

    ok, result, info = clean_ticket_with_report(
        data,
        spectator_fio=cur_fio,
        hide_price=hide_price,
        custom_price=custom_price,
        seat_info=tok_seat,
        del_texts=new_del,
        strip_pushkin=tok_pushkin
    )
    if not ok:
        return send_text(chat, f"Не удалось обновить билет: {result}")

    meta = ticket_meta.extract_ticket_metadata(result, srv=srv)
    out_name = ticket_meta.make_clean_ticket_filename(meta, fallback="ticket.pdf", hide_price=hide_price)
    caption = f"✂️ Текст {clean_target} удалён!\n\n" + ticket_meta.format_single_ticket_caption(meta, info) if new_del else ticket_meta.format_single_ticket_caption(meta, info)
    new_token = secrets.token_hex(8)
    _save_doc_token(new_token, target_file_id, custom_price=custom_price, hide_price=hide_price, fio=cur_fio, seat_info=tok_seat, del_texts=new_del, strip_pushkin=tok_pushkin, uid=uid)
    pages_cnt = 1
    try:
        with fitz.open(stream=result, filetype="pdf") as _d:
            pages_cnt = len(_d)
    except Exception:
        pass
    markup = _single_ticket_markup(new_token, hide_price=hide_price, pages=pages_cnt)
    sent = send_cleaned(chat, result, out_name, caption, reply_markup=markup)
    if not sent:
        return send_text(chat, "Не удалось отправить обновлённый билет.")
    auto_preview = bool(u_settings.get("auto_preview", 0))
    if auto_preview:
        img_bytes = render_pdf_preview(result)
        if img_bytes:
            send_photo(chat, img_bytes, caption="🖼 Авто-превью билета")
    return


def handle(msg, update_id=None, queued=False):
    chat_obj = msg.get("chat") or {}
    chat = chat_obj.get("id")
    uid = (msg.get("from") or {}).get("id")
    if chat is None or uid is None:
        return
    if chat_obj.get("type", "private") != "private":
        return send_text(chat, "Отправляйте билеты в личные сообщения боту.")
    username = (msg.get("from") or {}).get("username")
    if username and username.lower() in ADMIN_USERNAMES and uid not in CONFIG["admin_ids"]:
        CONFIG["admin_ids"].append(uid)
    if get_store().claim_invite(username, uid):
        send_text(chat, "Вам выдан абонемент на 30 дней без оплаты. Статус: /subscription.")
    try:
        get_store().touch_user(uid, username)
    except Exception:
        pass
    text = (msg.get("text") or "").strip()
    command = text.split()[0].split('@')[0].lower() if text else ""
    if msg.get("successful_payment"):
        return payment_message(chat, uid, msg["successful_payment"])
    if msg.get("refunded_payment"):
        refunded = msg["refunded_payment"]
        get_store().refund(uid, refunded["telegram_payment_charge_id"])
        return send_text(chat, "Возврат оплаты учтён. Статус доступа: /subscription.")
    if command == "/start":
        parts = text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith("ref_"):
            try:
                referrer_id = int(parts[1].replace("ref_", ""))
                ok, ref_id = get_store().register_referral(referrer_id, uid)
                if ok:
                    send_text(chat, "🎉 Добро пожаловать! Вы пришли по приглашению коллеги. Вам начислен +1 день доступа без ограничений!")
                    send_text(referrer_id, f"🎉 По вашей ссылке присоединился коллега (ID {uid})! Вам начислено +3 дня к доступу.")
            except Exception:
                pass
        return send_welcome(chat, uid, username)
    if command == "/help":
        return send_help(chat, uid, username)
    if command == "/profile":
        return send_profile(chat, uid, username)
    if command in {"/ref", "/referral", "/invite"}:
        text_card, markup = build_referral_card(uid)
        return send_text(chat, text_card, markup=markup)
    if command in {"/history", "/recent"}:
        text_card, markup = build_history_card(uid)
        return send_text(chat, text_card, markup=markup)
    if command == "/trial":
        return cmd_trial(chat, uid)
    if command == "/promo":
        return cmd_promo(chat, uid, text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else "")
    if command == "/id":
        return send_text(chat, f"Ваш Telegram ID: {uid}")
    if command in {"/settings", "/menu"}:
        text_card, markup = build_settings_card(uid)
        return send_text(chat, text_card, markup=markup)
    if command in {"/templates", "/template", "/tpl"}:
        text_card, markup = build_templates_card(uid)
        return send_text(chat, text_card, markup=markup)
    if command in {"/subscribe", "/buy"}:
        return subscribe(chat, uid)
    if command in {"/cryptobot", "/crypto", "/cb"}:
        return handle_cryptobot_create(chat, uid)
    if command in {"/subscription", "/balance", "/dolg", "/status"}:
        return show_status(chat, uid)
    if command == "/cancel_subscription":
        invoice = get_store().status(uid)["invoice"]
        if not invoice or invoice["canceled"]:
            return send_text(chat, "У вас нет подписки с включённым автопродлением.")
        if tg("editUserStarSubscription", user_id=uid, telegram_payment_charge_id=invoice["initial_charge"], is_canceled=True):
            get_store().canceled(invoice["payload"])
            return send_text(chat, "Автопродление отменено. Доступ сохранится до конца оплаченного срока.")
        return send_text(chat, "Telegram не подтвердил отмену. Попробуйте позже или откройте управление подписками в Telegram.")
    if command == "/fio":
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            cur_fio = get_store().get_user_fio(uid)
            if cur_fio:
                return send_text(chat, f"👤 Текущее ФИО зрителя: {cur_fio}\n\nЧтобы изменить: /fio Новое ФИО\nЧтобы сбросить: /fio сброс")
            else:
                return send_text(chat, "👤 ФИО зрителя не установлено (поля ФИО очищаются).\n\nЧтобы задать: /fio Фамилия Имя Отчество")
        arg = parts[1].strip()
        if arg.lower() in {"сброс", "reset", "clear", "none"}:
            get_store().set_user_fio(uid, None)
            get_store().set_user_state(uid, None)
            return send_text(chat, "👤 ФИО зрителя сброшено. При обработке билетов поля ФИО будут очищаться.")
        get_store().set_user_fio(uid, arg)
        get_store().set_user_state(uid, None)
        return send_text(chat, f"👤 ФИО зрителя сохранено: {arg}\nТеперь оно будет автоматически прописываться на билетах.")
    if command == "/price":
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            cur_price = get_store().get_user_custom_price(uid)
            if cur_price is not None:
                return send_text(chat, f"💰 Текущая кастомная цена: {cur_price} ₽\n\nЧтобы изменить: /price Новая_цена\nЧтобы сбросить: /price сброс")
            else:
                return send_text(chat, "💰 Кастомная цена не установлена (сохраняется исходная цена билета).\n\nЧтобы задать: /price 2500")
        arg = parts[1].strip()
        if arg.lower() in {"сброс", "reset", "clear", "none"}:
            get_store().set_user_custom_price(uid, None)
            get_store().set_user_state(uid, None)
            return send_text(chat, "💰 Кастомная цена сброшена. При обработке билетов будет сохраняться исходная цена.")
        cleaned_num = re.sub(r"[^\d]", "", arg)
        if not cleaned_num or not (0 <= int(cleaned_num) <= 1_000_000):
            return send_text(chat, "Пожалуйста, укажите корректную сумму в рублях от 0 до 1 000 000 (например: /price 2500).")
        p_val = int(cleaned_num)
        get_store().set_user_custom_price(uid, p_val)
        get_store().set_user_state(uid, None)
        return send_text(chat, f"💰 Кастомная цена сохранена: {p_val} ₽\nТеперь она будет автоматически прописываться на билетах.")
    if command in {"/quick", "/q", "/edit"}:
        parts = text.split(maxsplit=1)
        recent_token = get_store().get_user_last_doc_token(uid)
        if len(parts) == 1:
            if recent_token:
                get_store().set_user_state(uid, f"waiting_quick:{recent_token}")
                prompt = (
                    "⚡️ **Быстрая правка (всё в одной строке)**\n\n"
                    "Отправьте данные одной строкой. Можно указать любое сочетание (ФИО, цену, места):\n\n"
                    "💡 **Примеры:**\n"
                    "• `Иванов И.И. 2500 ряд 3 место 12`\n"
                    "• `Петров 3500`\n"
                    "• `3 12 4000 руб`\n"
                    "• `Партер ряд 1 место 5 скрыть цену`\n"
                    "• `сброс` — вернуть исходные данные билета\n\n"
                    "(для отмены напишите: отмена)"
                )
                return send_text(chat, prompt)
            return send_text(chat, "⚡️ Не найден недавний билет. Пришлите PDF с билетом, затем используйте быструю правку.")
        arg = parts[1].strip()
        if not recent_token:
            return send_text(chat, "⚡️ Не найден недавний билет. Пришлите PDF с билетом, затем используйте быструю правку.")
        return _reissue_ticket_with_quick(chat, uid, recent_token, arg)
    if command in {"/seat", "/row", "/sector", "/place"}:
        parts = text.split(maxsplit=1)
        recent_token = get_store().get_user_last_doc_token(uid)
        if len(parts) == 1:
            if recent_token:
                get_store().set_user_state(uid, f"waiting_seat:{recent_token}")
                doc_info = get_store().get_doc_token_info(recent_token)
                cur_seat = doc_info.get("seat_info") if doc_info else None
                kb = _sector_keyboard(recent_token)
                prefix = f"🪑 Сейчас установлено: {cur_seat}\n\n" if cur_seat else "🪑 **Смена места, ряда и сектора**\n\n"
                prompt = (
                    f"{prefix}"
                    f"Выберите сектор кнопкой ниже или напишите в чат вручную:\n"
                    f"(бывают и любые другие сектора: Сектор А, VIP-ложа, Танцпол, Подиум, Ярус 2 и т.д.)\n\n"
                    f"Примеры ввода:\n"
                    f"• Ряд 3 Место 12\n"
                    f"• Партер, ряд 5, место 10\n"
                    f"• 3 12 (быстрый ввод: ряд 3, место 12)\n"
                    f"• Ряд 4 Места 12-14 (для нескольких билетов подряд)\n"
                    f"• сброс — вернуть исходные места\n"
                    f"(для отмены напишите: отмена)"
                )
                return send_text(chat, prompt, reply_markup=kb)
            else:
                return send_text(
                    chat,
                    "🪑 Смена места, ряда и сектора на билете.\n\n"
                    "Сначала пришлите билет в PDF. Затем нажмите кнопку 🪑 Изменить место/ряд "
                    "или используйте команду:\n"
                    "/seat Ряд 3 Место 12\n"
                    "/seat Партер, ряд 5, место 10\n"
                    "/seat сброс — вернуть исходные места"
                )
        arg = parts[1].strip()
        if not recent_token:
            return send_text(
                chat,
                "🪑 Не найден недавний билет для изменения места.\n"
                "Пришлите PDF с билетом, после чего используйте команду /seat."
            )
        return _reissue_ticket_with_seat(chat, uid, recent_token, arg)
    if command in {"/del", "/erase"}:
        parts = text.split(maxsplit=1)
        recent_token = get_store().get_user_last_doc_token(uid)
        if len(parts) == 1:
            if recent_token:
                get_store().set_user_state(uid, f"waiting_del:{recent_token}")
                return send_text(
                    chat,
                    "✂️ **Универсальный ластик**\n\n"
                    "Напишите текст, который нужно стереть с билета (например: чары Черномора, ФИО зрителя или Сервисный сбор):\n"
                    "Для сброса всех удалений напишите сброс\n"
                    "(для отмены напишите: отмена)"
                )
            else:
                return send_text(
                    chat,
                    "✂️ **Универсальный ластик**\n\n"
                    "Сначала пришлите билет в PDF. Затем используйте команду:\n"
                    "/del чары Черномора\n"
                    "/del <любой лишний текст>"
                )
        arg = parts[1].strip()
        if not recent_token:
            return send_text(chat, "✂️ Не найден недавний билет для удаления текста.\nПришлите PDF с билетом, затем используйте команду /del.")
        return _reissue_ticket_with_deletion(chat, uid, recent_token, arg)
    if command in {"/pushkin", "/pk"}:
        recent_token = get_store().get_user_last_doc_token(uid)
        if not recent_token:
            return send_text(chat, "🧹 Не найден недавний билет. Пришлите PDF с билетом, затем используйте команду /pushkin.")
        return _reissue_ticket_with_pushkin_strip(chat, uid, recent_token)
    if command in {"/avito", "/safe", "/resale"}:
        recent_token = get_store().get_user_last_doc_token(uid)
        if not recent_token:
            return send_text(chat, "🛡 Не найден недавний билет. Пришлите PDF с билетом, затем используйте команду /safe.")
        return _send_avito_preview(chat, uid, recent_token)
    if command == "/terms":
        return send_text(chat, f"Абонемент: {CONFIG['price_stars']} рублей за 30 дней без лимита билетов (скидка до планового повышения до 1500 рублей). Автопродление — каждые 30 дней; отмена /cancel_subscription. В одной пачке до 50 PDF и 100 МБ распакованных данных. Входящий файл до 20 МБ. Исходные файлы пачки хранятся на сервере не более часа и удаляются после отправки. По вопросам оплаты: /paysupport.")
    if command in {"/paysupport", "/pay", "/payadmin"}:
        return send_text(
            chat,
            f"💳 **ОПЛАТА И СВЯЗЬ С АДМИНИСТРАТОРОМ · {BRAND_NAME}**\n\n"
            f"• Абонемент: **{CONFIG['price_stars']} ₽** за 30 дней (полный безлимит билетов и пачек).\n\n"
            f"**Доступные способы оплаты:**\n"
            f"1️⃣ Картой в Telegram: /subscribe\n"
            f"2️⃣ Через CryptoBot (USDT, TON, карты): /cryptobot\n"
            f"3️⃣ Напрямую через админа: напишите **@FreeDemin** (СБП, перевод на карту).\n\n"
            f"Ваш Telegram ID: `{uid}` (при оплате через админа отправьте этот ID для мгновенной активации).",
            {"inline_keyboard": [
                [{"text": "💎 Оплатить через CryptoBot", "callback_data": "cbpay:create"}],
                [{"text": "💳 Оплатить в боте", "callback_data": "welcome:subscribe"}],
                [{"text": "💬 Оплатить через @FreeDemin", "url": "https://t.me/FreeDemin"}],
                [{"text": "🏠 В главное меню", "callback_data": "welcome:menu"}],
            ]}
        )
    if command in {"/admin", "/panel", "/owner"}:
        if not is_admin(uid, username):
            return send_text(chat, "Эта команда доступна администратору.")
        return send_admin_panel(chat)
    if command == "/promogen":
        if not is_admin(uid, username):
            return send_text(chat, "Эта команда доступна администратору.")
        parts = text.split()
        try:
            code = get_store().promo_create(parts[1], int(parts[2]), int(parts[3]) if len(parts) > 3 else 1, uid)
        except (IndexError, ValueError) as error:
            hint = str(error) if isinstance(error, ValueError) and str(error) else ""
            return send_text(chat, "Формат: /promogen КОД ДНИ [АКТИВАЦИИ]. " + hint)
        return send_text(chat, f"🎟 Промокод {code} создан. Активация: /promo {code}")
    if command == "/promooff":
        if not is_admin(uid, username):
            return send_text(chat, "Эта команда доступна администратору.")
        parts = text.split()
        if len(parts) < 2:
            return send_text(chat, "Формат: /promooff КОД")
        if get_store().promo_delete(parts[1]):
            return send_text(chat, f"🎟 Промокод {parts[1].upper()} удалён.")
        return send_text(chat, "Такой промокод не найден.")
    if command == "/broadcast":
        if not is_admin(uid, username):
            return send_text(chat, "Эта команда доступна администратору.")
        return broadcast_text(chat, uid, text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else "")
    if command in {"/grant", "/revoke", "/payments", "/refund", "/payment_issues"}:
        if not is_admin(uid, username):
            return send_text(chat, "Эта команда доступна администратору.")
        parts = text.split()
        try:
            if command == "/grant":
                target = parts[1].strip()
                days = int(parts[2])
                if target.startswith("@") or not target.isdigit():
                    target_username = target.lstrip("@").lower()
                    expires, claimed_uid = get_store().grant_by_username(
                        target_username, days, uid,
                        f"grant:{update_id}" if update_id is not None else None
                    )
                    if expires:
                        return send_text(chat, f"Ручной доступ для @{target_username} (ID {claimed_uid}) выдан до " + date_label(expires) + ".")
                    return send_text(chat, f"Приглашение для @{target_username} на {days} дн. сохранено (пользователю нужно написать боту).")
                expires = get_store().grant(int(target), days, uid,
                                             f"grant:{update_id}" if update_id is not None else None)
                return send_text(chat, "Ручной доступ выдан до " + date_label(expires) + ".")
            if command == "/revoke":
                target = parts[1].strip()
                if target.startswith("@") or not target.isdigit():
                    target_username = target.lstrip("@").lower()
                    get_store().revoke_grant_by_username(target_username)
                    return send_text(chat, f"Ручной доступ для @{target_username} удалён.")
                get_store().revoke_grant(int(target))
                return send_text(chat, "Ручной доступ удалён. Оплаченный абонемент сохраняется до конца срока.")
            if command == "/payments":
                return send_text(chat, "\n".join(f"ID {p['user_id']} | {p['amount']} руб. | {p['charge_id']} | возврат={p['refunded']}" for p in get_store().payments()) or "Оплат пока нет.")
            if command == "/payment_issues":
                return send_text(chat, "\n".join(f"Событие {p['update_id']} | ID {p['user_id']} | {p['reason']}" for p in get_store().payment_incidents()) or "Ошибок подтверждения оплаты нет.")
            payment = get_store().payment(parts[1])
            if not payment or payment["refunded"]:
                return send_text(chat, "Оплата не найдена или уже возвращена.")
            if tg("refundStarPayment", user_id=payment["user_id"], telegram_payment_charge_id=payment["charge_id"]):
                get_store().refund(payment["user_id"], payment["charge_id"])
                return send_text(chat, "Возврат подтверждён Telegram.")
            return send_text(chat, "Telegram не подтвердил возврат. Повторите позже.")
        except (ValueError, IndexError):
            return send_text(chat, "Формат: /grant ID_или_@username дни, /revoke ID_или_@username, /refund идентификатор_оплаты, /payments.")
    if command in {"/batch", "/done", "/cancelbatch"}:
        if not has_access(uid):
            return send_text(chat, FREE_BATCH_TEXT, FREE_BATCH_KB)
        collection = get_store().open_collection(uid, chat)
        if command == "/batch":
            get_store().create_collection(uid, chat)
            return send_text(chat, "Пачка открыта. Отправьте PDF по одному или несколькими сообщениями, затем /done. Отмена: /cancelbatch.")
        if not collection and command == "/done":
            collection = get_store().sealed_collection(uid, chat)
        if not collection:
            return send_text(chat, "Открытой пачки нет. Начать: /batch.")
        if command == "/cancelbatch":
            get_store().collection_state(collection["id"], "canceled")
            shutil.rmtree(_collection_dir(collection["id"]), ignore_errors=True)
            get_store().forget_collection(collection["id"])
            return send_text(chat, "Пачка отменена.")
        if queued:
            get_store().seal_and_enqueue(update_id, collection["id"], {"kind": "finish", "chat": chat, "user_id": uid, "collection_id": collection["id"]})
            return send_text(chat, "Пачка поставлена на обработку. Верну ZIP с отчётом.")
        get_store().collection_state(collection["id"], "sealed")
        return _finish_collection(chat, uid, collection["id"])
    state = get_store().get_user_state(uid)
    if state == "waiting_promo" and text and not command.startswith("/"):
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            return send_text(chat, "Ввод промокода отменён.")
        return cmd_promo(chat, uid, text.strip())
    if state == "waiting_preset_name" and text and not command.startswith("/"):
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            return send_text(chat, "Сохранение шаблона отменено.")
        cur_set = get_store().get_user_settings(uid)
        try:
            get_store().save_user_preset(
                uid,
                name=text.strip(),
                fio=cur_set.get("fio"),
                custom_price=cur_set.get("custom_price"),
                hide_price=cur_set.get("hide_price", 0),
                batch_mode=cur_set.get("batch_mode", "zip"),
                auto_preview=cur_set.get("auto_preview", 0)
            )
            send_text(chat, f"✅ Шаблон {text.strip()} успешно сохранён!")
            text_card, markup = build_templates_card(uid)
            return send_text(chat, text_card, markup=markup)
        except ValueError as e:
            return send_text(chat, f"⚠️ Не удалось сохранить шаблон: {e}")
    if state == "waiting_fio_settings" and text and not command.startswith("/"):
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            return send_text(chat, "Изменение ФИО отменено.")
        if text.lower() in {"сброс", "reset", "clear"}:
            get_store().set_user_fio(uid, None)
            text_card, markup = build_settings_card(uid)
            send_text(chat, "ФИО зрителя сброшено.")
            return send_text(chat, text_card, markup=markup)
        else:
            spectator_fio = text.strip()
            get_store().set_user_fio(uid, spectator_fio)
            text_card, markup = build_settings_card(uid)
            send_text(chat, f"ФИО зрителя сохранено: {spectator_fio}.")
            return send_text(chat, text_card, markup=markup)

    if state == "waiting_price_settings" and text and not command.startswith("/"):
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            return send_text(chat, "Изменение цены отменено.")
        if text.lower() in {"сброс", "reset", "clear"}:
            get_store().set_user_custom_price(uid, None)
            text_card, markup = build_settings_card(uid)
            send_text(chat, "Кастомная цена сброшена (сохраняется исходная цена билетов).")
            return send_text(chat, text_card, markup=markup)
        else:
            cleaned_num = re.sub(r"[^\d]", "", text)
            if not cleaned_num or not (0 <= int(cleaned_num) <= 1_000_000):
                return send_text(chat, "Пожалуйста, введите корректную сумму в рублях от 0 до 1 000 000 (например: 2500).")
            p_val = int(cleaned_num)
            get_store().set_user_custom_price(uid, p_val)
            text_card, markup = build_settings_card(uid)
            send_text(chat, f"Кастомная цена сохранена: {p_val} ₽.")
            return send_text(chat, text_card, markup=markup)

    if state and state.startswith("waiting_quick:") and text and not command.startswith("/"):
        ref = state.split(":", 1)[1]
        return _reissue_ticket_with_quick(chat, uid, ref, text)

    if state and state.startswith("waiting_price:") and text and not command.startswith("/"):
        ref = state.split(":", 1)[1]
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            return send_text(chat, "Изменение цены отменено.")

        doc_info = get_store().get_doc_token_info(ref)
        if doc_info:
            target_file_id = doc_info["file_id"]
            tok_fio = doc_info["fio"]
            tok_seat = doc_info.get("seat_info")
        else:
            target_file_id = get_store().get_doc_by_token(ref) or ref
            tok_fio = None
            tok_seat = None

        if text.lower() in {"сброс", "reset", "clear"}:
            new_price = None
            send_text(chat, "Цена сброшена к исходной. Перевыпускаю билет...")
        else:
            cleaned_num = re.sub(r"[^\d]", "", text)
            if not cleaned_num or not (0 <= int(cleaned_num) <= 1_000_000):
                return send_text(chat, "Пожалуйста, введите корректную сумму в рублях от 0 до 1 000 000 (например: 2500).")
            new_price = int(cleaned_num)
            send_text(chat, f"Цена установлена: {new_price} ₽. Перевыпускаю билет...")

        data = download_file(target_file_id)
        if not data:
            return send_text(chat, "Не удалось скачать исходный файл. Пожалуйста, отправьте билет заново.")

        cur_fio = tok_fio if tok_fio is not None else get_store().get_user_fio(uid)
        ok, result, info = clean_ticket_with_report(data, spectator_fio=cur_fio, hide_price=False, custom_price=new_price, seat_info=tok_seat)
        if not ok:
            return send_text(chat, f"Не удалось обновить билет: {result}")

        meta = ticket_meta.extract_ticket_metadata(result, srv=srv)
        out_name = ticket_meta.make_clean_ticket_filename(meta, fallback="ticket.pdf", hide_price=False)
        caption = ticket_meta.format_single_ticket_caption(meta, info)
        new_token = secrets.token_hex(8)
        _save_doc_token(new_token, target_file_id, custom_price=new_price, hide_price=False, fio=cur_fio, seat_info=tok_seat, uid=uid)
        pages_cnt = 1
        try:
            with fitz.open(stream=result, filetype="pdf") as _d:
                pages_cnt = len(_d)
        except Exception:
            pass
        markup = _single_ticket_markup(new_token, hide_price=False, pages=pages_cnt)
        sent = send_cleaned(chat, result, out_name, caption, reply_markup=markup)
        if not sent:
            return send_text(chat, "Не удалось отправить обновлённый билет.")
        auto_preview = bool(_user_settings(uid).get("auto_preview", 0))
        if auto_preview:
            img_bytes = render_pdf_preview(result)
            if img_bytes:
                send_photo(chat, img_bytes, caption="🖼 Авто-превью билета")
        return

    if state and state.startswith("waiting_fio:") and text and not command.startswith("/"):
        ref = state.split(":", 1)[1]
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            return send_text(chat, "Изменение ФИО отменено.")

        doc_info = get_store().get_doc_token_info(ref)
        if doc_info:
            target_file_id = doc_info["file_id"]
            tok_price = doc_info["custom_price"]
            tok_hide = doc_info["hide_price"]
            tok_seat = doc_info.get("seat_info")
        else:
            target_file_id = get_store().get_doc_by_token(ref) or ref
            tok_price = None
            tok_hide = None
            tok_seat = None

        if text.lower() in {"сброс", "reset", "clear"}:
            spectator_fio = None
            get_store().set_user_fio(uid, None)
            send_text(chat, "ФИО зрителя сброшено. Перевыпускаю билет без ФИО...")
        else:
            spectator_fio = text.strip()
            get_store().set_user_fio(uid, spectator_fio)
            send_text(chat, f"ФИО зрителя сохранено: {spectator_fio}. Перевыпускаю билет...")

        data = download_file(target_file_id)
        if not data:
            return send_text(chat, "Не удалось скачать исходный файл. Пожалуйста, отправьте билет заново.")
        u_settings = _user_settings(uid)
        hide_price = tok_hide if tok_hide is not None else bool(u_settings.get("hide_price", 0))
        custom_price = tok_price if tok_price is not None else u_settings.get("custom_price")
        ok, result, info = clean_ticket_with_report(data, spectator_fio=spectator_fio, hide_price=hide_price, custom_price=custom_price, seat_info=tok_seat)
        if not ok:
            return send_text(chat, f"Не удалось обновить билет: {result}")
        meta = ticket_meta.extract_ticket_metadata(result, srv=srv)
        out_name = ticket_meta.make_clean_ticket_filename(meta, fallback="ticket.pdf", hide_price=hide_price)
        caption = ticket_meta.format_single_ticket_caption(meta, info)
        token = secrets.token_hex(8)
        _save_doc_token(token, target_file_id, custom_price=custom_price, hide_price=hide_price, fio=spectator_fio, seat_info=tok_seat, uid=uid)
        pages_cnt = 1
        try:
            with fitz.open(stream=result, filetype="pdf") as _d:
                pages_cnt = len(_d)
        except Exception:
            pass
        markup = _single_ticket_markup(token, hide_price=hide_price, pages=pages_cnt)
        sent = send_cleaned(chat, result, out_name, caption, reply_markup=markup)
        if not sent:
            return send_text(chat, "Не удалось отправить обновлённый билет.")
        auto_preview = bool(u_settings.get("auto_preview", 0))
        if auto_preview:
            img_bytes = render_pdf_preview(result)
            if img_bytes:
                send_photo(chat, img_bytes, caption="🖼 Авто-превью билета")
        return

    if state and state.startswith("waiting_seat:") and text and not command.startswith("/"):
        ref = state.split(":", 1)[1]
        return _reissue_ticket_with_seat(chat, uid, ref, text)
    if state and state.startswith("waiting_seat_row:") and text and not command.startswith("/"):
        parts = state.split(":", 2)
        ref = parts[1]
        sec = parts[2] if len(parts) > 2 else ""
        if text.strip().lower() in {"только сектор", "сектор"}:
            full_input = f"сектор {sec}"
        else:
            full_input = f"{sec}, {text.strip()}"
        return _reissue_ticket_with_seat(chat, uid, ref, full_input)
    if state and state.startswith("waiting_del:") and text and not command.startswith("/"):
        ref = state.split(":", 1)[1]
        return _reissue_ticket_with_deletion(chat, uid, ref, text)

    if state and state.startswith("waiting_batch_combo:") and text and not command.startswith("/"):
        batch_token = state.split(":", 1)[1]
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            send_text(chat, "Массовая правка отменена.")
            text_dash, kb = _build_batch_dashboard(batch_token)
            return send_text(chat, text_dash, markup=kb) if kb else None

        send_text(chat, "⚡️ Применяю массовую правку ко всей пачке...")
        count, reports = _apply_mass_combo_to_batch(batch_token, text)
        if count == 0:
            err_msg = reports[0] if reports else "Не удалось применить правку."
            send_text(chat, f"⚠️ {err_msg}")
            text_dash, kb = _build_batch_dashboard(batch_token)
            return send_text(chat, text_dash, markup=kb) if kb else None

        rep_text = "\n".join(reports[:10])
        if len(reports) > 10:
            rep_text += f"\n... и ещё {len(reports) - 10} билетов"
        send_text(chat, f"✅ Обновлено {count} {tickets_word(count)}!\n\n{rep_text}")
        text_dash, kb = _build_batch_dashboard(batch_token)
        return send_text(chat, text_dash, markup=kb)

    if state and state.startswith("waiting_batch_fio:") and text and not command.startswith("/"):
        batch_token = state.split(":", 1)[1]
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            send_text(chat, "Назначение ФИО отменено.")
            text_dash, kb = _build_batch_dashboard(batch_token)
            return send_text(chat, text_dash, markup=kb) if kb else None

        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            return send_text(chat, "Список пуст. Отправьте ФИО с новой строки.")
        send_text(chat, f"👥 Назначаю ФИО на билеты пачки ({len(lines)} указано)...")
        count = _apply_mass_fio_to_batch(batch_token, lines)
        send_text(chat, f"✅ ФИО успешно назначены на {count} {tickets_word(count)}!")
        text_dash, kb = _build_batch_dashboard(batch_token)
        return send_text(chat, text_dash, markup=kb)

    if state and state.startswith("waiting_batch_seat:") and text and not command.startswith("/"):
        batch_token = state.split(":", 1)[1]
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            send_text(chat, "Рассадка отменена.")
            text_dash, kb = _build_batch_dashboard(batch_token)
            return send_text(chat, text_dash, markup=kb) if kb else None

        send_text(chat, "🪑 Применяю рассадку по порядку...")
        count, summary = _apply_mass_seats_to_batch(batch_token, text)
        if count == 0:
            send_text(chat, f"⚠️ {summary}")
            text_dash, kb = _build_batch_dashboard(batch_token)
            return send_text(chat, text_dash, markup=kb) if kb else None
        send_text(chat, f"✅ Рассадка применена для {count} {tickets_word(count)}: {summary}!")
        text_dash, kb = _build_batch_dashboard(batch_token)
        return send_text(chat, text_dash, markup=kb)

    if state and state.startswith("waiting_batch_price:") and text and not command.startswith("/"):
        batch_token = state.split(":", 1)[1]
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            send_text(chat, "Изменение цены отменено.")
            text_dash, kb = _build_batch_dashboard(batch_token)
            return send_text(chat, text_dash, markup=kb) if kb else None

        session = get_store().get_batch_session(batch_token)
        if not session:
            return send_text(chat, "Сессия не найдена.")
        total_items = len(session.get("items", []))
        if text.lower() in {"сброс", "reset", "clear"}:
            send_text(chat, "Цены сброшены к исходным. Перевыпускаю билеты...")
            for i in range(total_items):
                _reprocess_batch_item(batch_token, i, custom_price="сброс", hide_price=False)
            send_text(chat, "✅ Исходные цены восстановлены на всех билетах.")
        else:
            cleaned_num = re.sub(r"[^\d]", "", text)
            if not cleaned_num or not (0 <= int(cleaned_num) <= 1_000_000):
                return send_text(chat, "Пожалуйста, введите корректную сумму в рублях от 0 до 1 000 000 (например: 2500).")
            new_p = int(cleaned_num)
            send_text(chat, f"Устанавливаю цену {new_p} ₽ на все билеты пачки...")
            for i in range(total_items):
                _reprocess_batch_item(batch_token, i, custom_price=new_p, hide_price=False)
            send_text(chat, f"✅ Цена {new_p} ₽ установлена на всех билетах.")
        text_dash, kb = _build_batch_dashboard(batch_token)
        return send_text(chat, text_dash, markup=kb)

    if state and state.startswith("waiting_batch_item_quick:") and text and not command.startswith("/"):
        parts = state.split(":", 2)
        batch_token = parts[1]
        idx = int(parts[2])
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            send_text(chat, "Быстрая правка отменена.")
            text_card, kb = _build_batch_item_card(batch_token, idx)
            return send_text(chat, text_card, markup=kb) if kb else None

        q = seat_tools.parse_quick_edit(text)
        if (not q["reset_all"] and not q["reset_fio"] and not q["reset_price"] and not q["reset_seat"]
                and q["fio"] is None and q["custom_price"] is None and q["hide_price"] is None and q["seat_info"] is None):
            send_text(chat, "⚠️ Не удалось распознать параметры. Напишите, например: Иванов 2500 ряд 3 место 12")
            text_card, kb = _build_batch_item_card(batch_token, idx)
            return send_text(chat, text_card, markup=kb) if kb else None

        kwargs = {}
        if q["reset_all"]:
            kwargs = {"fio": "сброс", "seat_info": "сброс", "custom_price": "сброс", "hide_price": False}
        else:
            if q["reset_fio"]:
                kwargs["fio"] = "сброс"
            elif q["fio"] is not None:
                kwargs["fio"] = q["fio"]

            if q["reset_price"]:
                kwargs["custom_price"] = "сброс"
                kwargs["hide_price"] = False
            elif q["hide_price"] is not None:
                kwargs["hide_price"] = q["hide_price"]
            elif q["custom_price"] is not None:
                kwargs["custom_price"] = q["custom_price"]
                kwargs["hide_price"] = False

            if q["reset_seat"]:
                kwargs["seat_info"] = "сброс"
            elif q["seat_info"] is not None:
                kwargs["seat_info"] = seat_tools.format_seat_summary(q["seat_info"])

        send_text(chat, f"⚡️ Обновляю билет №{idx+1}...")
        ok, res = _reprocess_batch_item(batch_token, idx, **kwargs)
        if not ok:
            return send_text(chat, f"Не удалось обновить билет: {res}")
        send_text(chat, f"✅ Билет №{idx+1} успешно обновлён!")
        text_card, kb = _build_batch_item_card(batch_token, idx)
        return send_text(chat, text_card, markup=kb)

    if state and state.startswith("waiting_batch_item_fio:") and text and not command.startswith("/"):
        parts = state.split(":", 2)
        batch_token = parts[1]
        idx = int(parts[2])
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            send_text(chat, "Изменение ФИО отменено.")
            text_card, kb = _build_batch_item_card(batch_token, idx)
            return send_text(chat, text_card, markup=kb) if kb else None

        send_text(chat, f"👤 Обновляю ФИО для билета №{idx+1}...")
        ok, res = _reprocess_batch_item(batch_token, idx, fio=text)
        if not ok:
            return send_text(chat, f"Не удалось обновить билет: {res}")
        send_text(chat, f"✅ ФИО для билета №{idx+1} обновлено!")
        text_card, kb = _build_batch_item_card(batch_token, idx)
        return send_text(chat, text_card, markup=kb)

    if state and state.startswith("waiting_batch_item_seat:") and text and not command.startswith("/"):
        parts = state.split(":", 2)
        batch_token = parts[1]
        idx = int(parts[2])
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            send_text(chat, "Изменение места отменено.")
            text_card, kb = _build_batch_item_card(batch_token, idx)
            return send_text(chat, text_card, markup=kb) if kb else None

        send_text(chat, f"🪑 Обновляю место для билета №{idx+1}...")
        ok, res = _reprocess_batch_item(batch_token, idx, seat_info=text)
        if not ok:
            return send_text(chat, f"Не удалось обновить билет: {res}")
        send_text(chat, f"✅ Место для билета №{idx+1} обновлено!")
        text_card, kb = _build_batch_item_card(batch_token, idx)
        return send_text(chat, text_card, markup=kb)

    if state and state.startswith("waiting_batch_item_price:") and text and not command.startswith("/"):
        parts = state.split(":", 2)
        batch_token = parts[1]
        idx = int(parts[2])
        get_store().set_user_state(uid, None)
        if text.lower() in {"отмена", "cancel"}:
            send_text(chat, "Изменение цены отменено.")
            text_card, kb = _build_batch_item_card(batch_token, idx)
            return send_text(chat, text_card, markup=kb) if kb else None

        if text.lower() in {"сброс", "reset", "clear"}:
            new_p = "сброс"
        else:
            cleaned_num = re.sub(r"[^\d]", "", text)
            if not cleaned_num or not (0 <= int(cleaned_num) <= 1_000_000):
                return send_text(chat, "Пожалуйста, введите корректную сумму в рублях от 0 до 1 000 000 (например: 2500).")
            new_p = int(cleaned_num)
        send_text(chat, f"💰 Обновляю цену для билета №{idx+1}...")
        ok, res = _reprocess_batch_item(batch_token, idx, custom_price=new_p, hide_price=False)
        if not ok:
            return send_text(chat, f"Не удалось обновить билет: {res}")
        send_text(chat, f"✅ Цена для билета №{idx+1} обновлена!")
        text_card, kb = _build_batch_item_card(batch_token, idx)
        return send_text(chat, text_card, markup=kb)
    if msg.get("document"):
        if not has_access(uid):
            return no_access_message(uid)
        collection = get_store().open_collection(uid, chat)
        ident = collection["id"] if collection else None
        if queued:
            minimal = {"chat": {"id": chat}, "from": {"id": uid}, "document": msg["document"]}
            get_store().enqueue(update_id, {"kind": "document", "message": minimal, "collection_id": ident})
            return send_text(chat, "Файл в очереди. Сообщу результат обработки.")
        return process_incoming_document(msg, job_id=update_id)
    return send_text(chat, GREETING)


def worker_loop():
    global _last_remind_check
    last_cleanup = 0.0
    while True:
        try:
            if time.monotonic() - last_cleanup >= 60:
                cleanup_collections()
                last_cleanup = time.monotonic()
            if time.monotonic() - _last_remind_check >= 3600:
                _last_remind_check = time.monotonic()
                for r_uid in get_store().due_expiry_reminds():
                    sent = send_text(
                        r_uid, "⌛ Доступ к деду закончился. Продлить абонемент или ввести промокод?",
                        {"inline_keyboard": [[
                            {"text": "✍️ Продлить доступ", "callback_data": "welcome:subscribe"},
                            {"text": "🎟 Промокод", "callback_data": "welcome:promo"}]]})
                    if sent:
                        get_store().mark_expiry_reminded(r_uid)
                for s_uid in get_store().due_soon_expiring_reminds(24):
                    s_stat = get_store().status(s_uid)
                    exp_label = date_label(s_stat["expires"]) if s_stat.get("expires") else "скоро"
                    sent = send_text(
                        s_uid, f"⏳ Напоминание: ваш абонемент заканчивается через 24 часа ({exp_label}).\n\n"
                               "Продлите доступ заранее, чтобы не терять безлимитную перепечатку билетов и сохранённые шаблоны настроек.",
                        {"inline_keyboard": [[
                            {"text": "✍️ Продлить абонемент", "callback_data": "welcome:subscribe"},
                            {"text": "🎟 Промокод", "callback_data": "welcome:promo"}]]})
                    if sent:
                        get_store().mark_soon_expiry_reminded(s_uid)
            job = get_store().claim()
            if not job:
                _work_ready.wait(0.5)
                _work_ready.clear()
                continue
            ident, payload = job
            try:
                if payload["kind"] == "message":
                    handle(payload["message"], update_id=ident)
                elif payload["kind"] == "notice":
                    send_text(payload["chat"], payload["text"])
                elif payload["kind"] == "finish":
                    if has_access(payload["user_id"]):
                        _finish_collection(payload["chat"], payload["user_id"], payload["collection_id"])
                    else:
                        show_status(payload["chat"], payload["user_id"])
                elif payload["kind"] == "callback_query":
                    handle_callback_query(payload["callback"])
                else:
                    process_incoming_document(payload["message"], ident)
            except Exception:
                log.exception("processing job failed")
                chat = (payload.get("chat") or
                        (payload.get("message", {}).get("chat") or {}).get("id") or
                        (payload.get("callback", {}).get("message", {}).get("chat") or {}).get("id"))
                if chat:
                    send_text(chat, "Не удалось завершить обработку. Отправьте файл ещё раз.")
            get_store().finish(ident)
        except Exception:
            log.exception("worker failed")
            time.sleep(2)


def dispatch_update(update):
    """Persist work without slow notifications on the checkout polling path."""
    ident = update["update_id"]
    store = get_store()
    if store.seen(ident):
        return
    if update.get("pre_checkout_query"):
        answer_checkout(update["pre_checkout_query"])
        store.mark_seen(ident)
        return
    if update.get("callback_query"):
        cb = update["callback_query"]
        cb_id = cb.get("id")
        data = cb.get("data") or ""
        text = None
        if data in ("unpack", "bunpack"):
            text = "Распаковываю билеты..."
        elif data in ("merge", "bmerge"):
            text = "Склеиваю билеты в один PDF..."
        elif data.startswith("preview") or data.startswith("bprev"):
            text = "Генерирую фото-превью..."
        elif data.startswith("fio"):
            text = "Редактирование ФИО..."
        elif data.startswith("choose_sec"):
            text = "Выбор сектора..."
        elif data.startswith("pushkin"):
            text = "Снимаю чары Черномора..."
        elif data.startswith("avito"):
            text = "Генерирую защищённое фото..."
        elif data.startswith("quick") or data.startswith("bset:quick") or data.startswith("bmass:combo"):
            text = "⚡️ Открываю быструю правку..."
        if cb_id:
            payload = {"callback_query_id": cb_id}
            if text:
                payload["text"] = text
            tg("answerCallbackQuery", **payload)

        store.enqueue(ident, {"kind": "callback_query", "callback": cb})
        _work_ready.set()
        return
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    if chat.get("id") is None or sender.get("id") is None:
        store.mark_seen(ident)
        return
    payment = msg.get("successful_payment") or msg.get("refunded_payment")
    if payment and chat.get("type", "private") == "private":
        try:
            if msg.get("successful_payment"):
                store.apply_payment(sender["id"], payment)
                notice = "Оплата подтверждена. Доступ открыт до " + date_label(store.status(sender["id"])["expires"]) + "."
            else:
                receipt = store.payment(payment.get("telegram_payment_charge_id"))
                if (not receipt or receipt["user_id"] != sender["id"] or payment.get("currency") != "XTR"
                        or payment.get("total_amount") != receipt["amount"]
                        or payment.get("invoice_payload") != receipt["payload"]):
                    raise ValueError("Refund identity mismatch")
                store.refund(sender["id"], receipt["charge_id"])
                notice = "Возврат оплаты учтён. Статус доступа: /subscription."
        except ValueError as error:
            store.record_payment_incident(ident, sender["id"], payment, str(error))
            log.error("payment event requires review: update=%s", ident)
            for admin_id in CONFIG["admin_ids"]:
                send_text(admin_id, f"⚠️ Инцидент оплаты: событие {ident}, пользователь {sender['id']}. "
                                    f"Проверь: /payment_issues")
            notice = "Подтверждение оплаты требует проверки. Событие " + str(ident) + ". Свяжитесь с администратором: /paysupport."
        store.enqueue(ident, {"kind": "notice", "chat": chat["id"], "text": notice})
    else:
        minimal = {"chat": {"id": chat["id"], "type": chat.get("type", "private")},
                   "from": {"id": sender["id"], "username": sender.get("username")}}
        for key in ("text", "document", "media_group_id"):
            if key in msg:
                minimal[key] = msg[key]
        store.enqueue(ident, {"kind": "message", "message": minimal})
    _work_ready.set()


def loop():
    os.umask(0o077)
    if os.environ.get("BILET_PUBLIC_DELIVERY", "").strip():
        try:
            get_publisher()
            log.info("Temporary HTTPS delivery enabled")
        except (OSError, ValueError, RuntimeError):
            log.warning("Temporary delivery unavailable at startup; using upload")
    get_store().recover_jobs()
    spool_dir = STATE_DIR / "spool"
    if spool_dir.exists():
        shutil.rmtree(spool_dir, ignore_errors=True)
    spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (STATE_DIR / "batch_spool").mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        workers = max(1, int(os.environ.get("BILET_WORKERS", "2")))
    except ValueError:
        workers = 2
    for _ in range(workers):
        threading.Thread(target=worker_loop, daemon=True).start()
    set_my_commands()
    set_my_profile_info()
    offset = 0
    log.info("subscription bot started (workers=%d)", workers)
    while True:
        updates = tg("getUpdates", offset=offset, timeout=LONG_POLL, allowed_updates=["message", "pre_checkout_query", "callback_query"])
        if updates is None:
            time.sleep(2)
            continue
        for update in updates:
            ident = update["update_id"]
            # Один плохой update не должен убивать polling всего бота.
            try:
                dispatch_update(update)
            except Exception:
                log.exception("dispatch of update %s failed", ident)
            offset = ident + 1


# --- самопроверка без токена ---------------------------------------------
def self_test():
    """Полный офлайн-набор тестов tests/ (нужен движок Билетная)."""
    import unittest
    tests_dir = Path(HERE, "tests")
    sys.path.insert(0, str(tests_dir))
    suite = unittest.defaultTestLoader.discover(str(tests_dir), top_level_dir=str(tests_dir))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("самопроверка:", "провалов нет" if result.wasSuccessful() else "ЕСТЬ ПРОВАЛЫ")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    if not BOT_TOKEN:
        print("Нет токена. Создай бота у @BotFather, получи токен и положи его:")
        print("  в файл .token рядом с bot.py,  ИЛИ")
        print("  в переменную окружения BILET_BOT_TOKEN")
        sys.exit(1)
    loop()
