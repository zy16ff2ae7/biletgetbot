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

import fitz
import requests

import batches
import validation
import ticket_meta
import seat_tools
import eraser_tools
from progress import Progress, PDF_STEPS, BATCH_STEPS, COLLECT_STEPS
from delivery_cache import DeliveryCache, key_for
from url_delivery import Publisher
from telegram_file_transport import TelegramFileTransport, MetadataAPIError, MetadataUnavailable
from subscriptions import SubscriptionStore, PERIOD

HERE = os.path.dirname(os.path.abspath(__file__))
# Редактор «Билетная»: по умолчанию ~/Билетная, но на VDS его кладут куда
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
CONFIG = json.loads(Path(HERE, "subscription.json").read_text())
if (type(CONFIG.get("price_stars")) is not int or not 1 <= CONFIG["price_stars"] <= 10000
        or CONFIG.get("period_days") != 30 or CONFIG.get("unlimited") is not True
        or not CONFIG.get("admin_ids")
        or not all(type(uid) is int and uid > 0 for uid in CONFIG["admin_ids"])):
    raise RuntimeError("Invalid subscription configuration")
STATE_DIR = Path(os.environ.get("BILET_STATE_DIR") or Path(HERE, "state"))
_store = None
_store_lock = threading.Lock()
_http_local = threading.local()
_work_ready = threading.Event()
_publisher = None
_publisher_lock = threading.Lock()
_file_transport = None
_file_transport_lock = threading.Lock()


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


def _save_doc_token(token, file_id, custom_price=None, hide_price=None, fio=None, seat_info=None, del_texts=None, strip_pushkin=None, uid=None):
    try:
        get_store().save_doc_token(token, file_id, custom_price=custom_price, hide_price=hide_price, fio=fio, seat_info=seat_info, del_texts=del_texts, strip_pushkin=strip_pushkin)
        if uid is not None:
            get_store().set_user_last_doc_token(uid, token)
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


def date_label(timestamp):
    from datetime import datetime
    return datetime.fromtimestamp(timestamp, ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M МСК")


GREETING = (
    "МАМА · Мама кормит\n\n"
    "Привет! Я МАМА, из одной сети с ДОЧЕЙ. Пришли PDF или ZIP с билетами — верну обработанные файлы прямо в чат.\n"
    "Сохранность кодов, даты, мест и цены проверяется перед отправкой.\n"
    f"Абонемент: {CONFIG['price_stars']} Telegram Stars за 30 дней, без лимита билетов. "
    "Автопродление каждые 30 дней; отмена: /cancel_subscription.\n"
    "/subscribe — оформить; /subscription — статус.\n"
    "/settings — меню настроек (ФИО, скрытие цены, формат пачек, авто-превью).\n"
    "/fio — настроить ФИО зрителя (например, /fio Иванов Иван Иванович); /fio сброс — очистить.\n"
    "/price — изменить цену билета (например, /price 2500); /price сброс — вернуть исходную.\n"
    "/seat — изменить ряд/место/сектор (например: /seat Ряд 3 Место 12); /seat сброс — вернуть исходные места.\n"
    "/pushkin — снять Пушкинскую карту с билета.\n"
    "/del — стереть любой лишний текст (например: /del Сервисный сбор); /del сброс — вернуть.\n"
    "/avito — защищённое фото-превью со скрытыми кодами для Авито и соцсетей.\n"
    "/batch — собрать пачку PDF; /done — получить ZIP; /cancelbatch — отменить.\n"
    "/id — ваш Telegram ID; /terms — условия; /paysupport — помощь с оплатой."
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("bilet-bot")


# --- движок чистки из «Билетной» -----------------------------------------
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
        payment, buyer = srv._chelny_fields(page, lines)
        family = payment is not None or srv._is_kassa24_family(lines)
        t_names = srv._ticketland_name_rows(page, lines)
        k_rows = srv._kassir_name_rows(lines)
        is_af = any(srv.AFISHA_SELLER_RE.search(l["text"] or "") for l in lines)
        p_slots = []
        for i, l in enumerate(lines):
            if intickets and i not in intickets_names:
                continue
            if not (family or is_af) and i not in (t_names | intickets_names):
                continue
            if payment is not None and i != buyer:
                continue
            what = srv._personal_line_what(l)
            if not what and is_af and 208 <= l["bbox"][1] <= 235 and 150 <= l["bbox"][0] <= 450:
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
            if fio_val:
                for pno, (page, slot) in enumerate(zip(cleaned, name_slots)):
                    if slot:
                        origin = slot["origin"]
                        size = slot.get("size", 8.0)
                        color = srv.int_to_rgb(slot.get("color", 0))
                        text_to_write = fio_val.upper() if slot.get("text", "").isupper() else fio_val
                        writer = fitz.TextWriter(page.rect)
                        writer.append(origin, text_to_write, font=srv.FONT_REG, fontsize=size)
                        writer.write_text(page, color=color)
                    elif any(srv.AFISHA_SELLER_RE.search(l["text"] or "") for l in srv.extract_lines(page)):
                        writer = fitz.TextWriter(page.rect)
                        writer.append((155, 222), f"Зритель: {fio_val}", font=srv.FONT_REG, fontsize=8.0)
                        writer.write_text(page, color=(0, 0, 0))

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
            info["changed"] = bool(ghosts or count or fio_val or hide_price or (price_val is not None) or seat_applied or del_applied or pushkin_applied)
            info["removed"] = count
            if fio_val:
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
    """Return True on delivery, None for an invalid reusable ID, False on failure."""
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
                    return True
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
        url = tg("createInvoiceLink", title="МАМА: абонемент на месяц",
                 description="МАМА — мама кормит. 30 дней без лимита билетов. PDF и ZIP, проверка кодов и полей. Автопродление; отмена /cancel_subscription.",
                 payload=invoice["payload"], provider_token="", currency="XTR",
                 prices=[{"label": "30 дней", "amount": invoice["price"]}], subscription_period=PERIOD)
        if not url:
            return send_text(chat, "Не удалось создать счёт. Попробуйте /subscribe позже.")
        get_store().save_url(invoice["payload"], url)
    return send_text(chat, f"{CONFIG['price_stars']} Stars за 30 дней без лимита билетов. Автопродление каждые 30 дней; отмена /cancel_subscription.",
                     {"inline_keyboard": [[{"text": "Оформить абонемент", "url": url}]]})


def show_status(chat, user_id):
    if is_admin(user_id):
        return send_text(chat, "Постоянный администраторский доступ. Подписка не требуется.")
    status = get_store().status(user_id)
    if status["active"]:
        text = "Абонемент активен до " + date_label(status["expires"]) + ". Билеты без лимита."
        invoice = status["invoice"]
        if invoice and not invoice["canceled"]:
            text += "\nАвтопродление включено. Отмена: /cancel_subscription."
        return send_text(chat, text)
    return send_text(chat, "Абонемент не активен. Оформить: /subscribe.")


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
            [{"text": "🔄 Сбросить настройки", "callback_data": "set:reset"}],
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


def _single_ticket_markup(token, hide_price=False, pages=1):
    seat_btn = {"text": "🪑 Изменить место/ряд", "callback_data": f"seat:{token}"}
    fio_btn = {"text": "👤 Изменить ФИО", "callback_data": f"fio:{token}"}
    price_edit_btn = {"text": "✏️ Изменить цену", "callback_data": f"price:{token}"}
    if hide_price:
        price_toggle_btn = {"text": "💰 Показать цену", "callback_data": f"toggleprice:{token}:0"}
    else:
        price_toggle_btn = {"text": "🎁 Скрыть цену", "callback_data": f"toggleprice:{token}:1"}
    preview_btn = {"text": "🖼 Фото: До и После", "callback_data": f"compare:{token}"}
    pushkin_btn = {"text": "🧹 Снять Пушкинскую", "callback_data": f"pushkin:{token}"}
    avito_btn = {"text": "🛡 Для Авито", "callback_data": f"avito:{token}"}

    rows = [
        [seat_btn],
        [fio_btn, price_edit_btn],
        [price_toggle_btn, preview_btn],
        [pushkin_btn, avito_btn],
    ]
    if pages > 1:
        rows.append([{"text": f"✂️ Разбить на билеты ({pages} шт.)", "callback_data": f"split:{token}"}])
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


def _send_batch_result(chat, result, progress, retry_message, uid=None):
    progress.update(3, "Отправляю результат обработки в чат.")
    items_meta = result.get("items_meta", [])
    caption = ticket_meta.format_batch_tg_caption(items_meta, result["ok"], result["failed"])
    zip_name = result.get("zip_name") or "tickets_cleaned.zip"

    settings = _user_settings(uid) if uid else {}
    batch_mode = settings.get("batch_mode") or "zip"
    auto_preview = bool(settings.get("auto_preview", 0))

    reply_markup = None
    if result.get("ok", 0) > 0:
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
            token = secrets.token_hex(8)
            markup = {
                "inline_keyboard": [
                    [{"text": "🖼 Фото-превью первого билета", "callback_data": f"preview:{token}"}]
                ]
            }
            if send_cleaned(chat, merged_bytes, merged_name, m_caption, reply_markup=markup):
                progress.finish(f"Склеенный PDF отправлен 🩷 Билетов: {count}.")
                if auto_preview:
                    img_bytes = render_pdf_preview(merged_bytes)
                    if img_bytes:
                        send_photo(chat, img_bytes, caption="🖼 Авто-превью первого билета")
                return True

    if not send_cleaned(chat, result["data"], zip_name, caption, reply_markup=reply_markup):
        _progress_error(progress, chat, retry_message)
        return False
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

        if not items:
            _progress_error(progress, chat, "Не удалось скачать файлы из пачки. Отправьте их ещё раз.")
            return

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


def process_incoming_document(msg, job_id=None):
    chat = msg["chat"]["id"]
    uid = msg["from"]["id"]
    if not has_access(uid):
        return show_status(chat, uid)
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
    if not has_access(uid):
        return show_status(chat, uid)
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
                progress.update(1, "Убираю распознанные ФИО и блоки Пушкинской карты.")
            else:
                progress.update(2, "Сверяю QR-коды, штрихкоды, дату, места и цену.")

        ok, result, info = clean_ticket_with_report(data, on_progress=phase, spectator_fio=user_fio, hide_price=hide_price, custom_price=custom_price)
        if not ok:
            return _progress_error(progress, chat, result)
        meta = ticket_meta.extract_ticket_metadata(result, srv=srv)
        out_name = ticket_meta.make_clean_ticket_filename(meta, fallback=batches.safe_name(name), hide_price=hide_price)
        caption = ticket_meta.format_single_ticket_caption(meta, info)
        progress.update(3, "Проверка завершена. Отправляю билет в чат.")
        reuse_id = document["file_id"] if (not info["changed"] and not user_fio and not hide_price and custom_price is None) else None
        pages_cnt = 1
        try:
            with fitz.open(stream=result, filetype="pdf") as _doc:
                pages_cnt = len(_doc)
        except Exception:
            pass
        markup = _single_ticket_markup(token, hide_price=hide_price, pages=pages_cnt)
        sent = send_cleaned(chat, result, out_name, caption, cache_keys=cache_keys, reuse_id=reuse_id, reply_markup=markup)
        if sent is None and reuse_id:
            sent = send_cleaned(chat, result, out_name, caption, cache_keys=cache_keys, reply_markup=markup)
        if not sent:
            return _progress_error(progress, chat, "Не удалось отправить файл. Отправьте исходный PDF ещё раз.")
        progress.finish("Билет отправлен 🩷 Можно присылать следующий." if info["changed"]
                        else "Исходный PDF отправлен 🩷 Автоматических правок нет.")
        if auto_preview:
            img_bytes = render_pdf_preview(result)
            if img_bytes:
                send_photo(chat, img_bytes, caption="🖼 Авто-превью билета")


def handle_callback_query(cb):
    data = cb.get("data") or ""
    if not any(data == x or data.startswith(f"{x}:") for x in ("unpack", "merge", "preview", "compare", "price", "fio", "seat", "choose_sec", "pushkin", "avito", "set", "toggleprice", "split")):
        return

    sender = cb.get("from") or {}
    uid = sender.get("id")
    msg = cb.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    msg_id = msg.get("message_id")
    if not chat or not uid:
        return
    if not has_access(uid):
        return show_status(chat, uid)

    if data.startswith("set:"):
        action = data.split(":", 1)[1]
        if action == "fio":
            get_store().set_user_state(uid, "waiting_fio_settings")
            return send_text(chat, "👤 Введите ФИО зрителя для билетов (например, Иванов Иван Иванович) или отправьте «сброс» для очистки:\n(для отмены напишите «отмена»)")
        if action == "custom_price":
            get_store().set_user_state(uid, "waiting_price_settings")
            return send_text(chat, "💰 Введите цену билета по умолчанию в рублях (например, 2500) или отправьте «сброс» для очистки:\n(для отмены напишите «отмена»)")
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
        if msg_id:
            tg("editMessageText", chat_id=chat, message_id=msg_id, text=text_card, reply_markup=markup)
        else:
            send_text(chat, text_card, markup=markup)
        return

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
        ok, result, info = clean_ticket_with_report(data_bytes, spectator_fio=cur_fio, hide_price=hide_price)
        pdf_to_split = result if ok else data_bytes
        entries = split_pdf_tickets(pdf_to_split, srv=srv, hide_price=hide_price)
        if len(entries) <= 1:
            return send_text(chat, "В этом файле только одна страница (один билет).")
        send_text(chat, f"✂️ Разбиваю многостраничный PDF на отдельные билеты ({len(entries)} шт.)...")
        if len(entries) <= 10:
            sent_count = 0
            for p_name, p_data, p_meta in entries:
                p_token = secrets.token_hex(8)
                get_store().save_doc_token(p_token, target_file_id)
                p_markup = _single_ticket_markup(p_token, hide_price=hide_price, pages=1)
                p_caption = ticket_meta.format_single_ticket_caption(p_meta, info)
                sent = send_cleaned(chat, p_data, p_name, p_caption, reply_markup=p_markup)
                if sent:
                    sent_count += 1
                time.sleep(0.3)
            send_text(chat, f"Готово 🩷 Распаковано билетов: {sent_count} из {len(entries)}.")
        else:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
                for p_name, p_data, _ in entries:
                    z.writestr(p_name, p_data)
            zip_data = buf.getvalue()
            zip_name = ticket_meta.make_batch_zip_filename([{"name": n, "meta": m} for n, _, m in entries], len(entries))
            caption = ticket_meta.format_batch_tg_caption([{"name": n, "meta": m} for n, _, m in entries], len(entries), 0)
            send_cleaned(chat, zip_data, zip_name, caption)
            send_text(chat, f"Готово 🩷 Билетов в архиве: {len(entries)}.")
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
            time.sleep(0.3)

        if msg_id:
            tg("editMessageReplyMarkup", chat_id=chat, message_id=msg_id, reply_markup={"inline_keyboard": []})

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
            f"Введите новую цену билета в рублях (например, 2500 или 3500) или напишите «сброс» для возврата исходной цены:\n"
            f"(для отмены напишите «отмена»)"
            if cur_price is not None else
            f"💰 Введите новую цену билета в рублях (например, 2500 или 3500) для печати на билете:\n"
            f"(для отмены напишите «отмена»)"
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
            f"(для отмены напишите «отмена»)"
            if cur_fio else
            f"👤 Введите ФИО зрителя (например, Иванов Иван Иванович) для печати на билете:\n"
            f"(для отмены напишите «отмена»)"
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
            f"(для отмены напишите «отмена»)"
        )
        return send_text(chat, prompt, reply_markup=kb)

    if data.startswith("choose_sec:"):
        parts = data.split(":", 2)
        token = parts[1]
        sector = parts[2] if len(parts) > 2 else ""
        get_store().set_user_state(uid, f"waiting_seat_row:{token}:{sector}")
        prompt = (
            f"🎭 Выбран сектор: **{sector}**.\n\n"
            f"Теперь введите ряд и место (например: «Ряд 3 Место 12» или просто «3 12»):\n"
            f"• Если на билете несколько страниц, места назначим подряд автоматически (например «3 12» -> 12, 13, 14...)\n"
            f"• Если хотите применить только сектор без смены ряда/места, напишите «только сектор»\n"
            f"(для отмены напишите «отмена»)"
        )
        return send_text(chat, prompt)

    if data.startswith("pushkin:"):
        token = data.split(":", 1)[1] if ":" in data else None
        return _reissue_ticket_with_pushkin_strip(chat, uid, token)

    if data.startswith("avito:"):
        token = data.split(":", 1)[1] if ":" in data else None
        return _send_avito_preview(chat, uid, token)


def _reissue_ticket_with_seat(chat, uid, ref, seat_input_text):
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
                "(для отмены напишите «отмена»)"
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

    send_text(chat, "🧹 Снимаю Пушкинскую карту и перевожу билет в коммерческий вид...")
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
    caption = "🧹 Пушкинская карта снята с билета!\n\n" + ticket_meta.format_single_ticket_caption(meta, info)
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
        return send_text(chat, "Не удалось сгенерировать превью для Авито.")

    caption = (
        "🛡 **Защищённое фото для публикации на Авито / Юле / соцсетях**\n\n"
        "• Штрихкоды и QR-коды надёжно скрыты защитной плашкой — мошенники не украдут билет до покупки.\n"
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
        send_text(chat, f"✂️ Стираю «{clean_target}» с билета...")

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
    caption = f"✂️ Текст «{clean_target}» удалён!\n\n" + ticket_meta.format_single_ticket_caption(meta, info) if new_del else ticket_meta.format_single_ticket_caption(meta, info)
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
    text = (msg.get("text") or "").strip()
    command = text.split()[0].split('@')[0].lower() if text else ""
    if msg.get("successful_payment"):
        return payment_message(chat, uid, msg["successful_payment"])
    if msg.get("refunded_payment"):
        refunded = msg["refunded_payment"]
        get_store().refund(uid, refunded["telegram_payment_charge_id"])
        return send_text(chat, "Возврат оплаты учтён. Статус доступа: /subscription.")
    if command == "/id":
        return send_text(chat, f"Ваш Telegram ID: {uid}")
    if command in {"/settings", "/menu"}:
        text_card, markup = build_settings_card(uid)
        return send_text(chat, text_card, markup=markup)
    if command in {"/subscribe", "/buy"}:
        return subscribe(chat, uid)
    if command in {"/subscription", "/balance", "/dolg"}:
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
                    f"(для отмены напишите «отмена»)"
                )
                return send_text(chat, prompt, reply_markup=kb)
            else:
                return send_text(
                    chat,
                    "🪑 Смена места, ряда и сектора на билете.\n\n"
                    "Сначала пришлите билет в PDF. Затем нажмите кнопку «🪑 Изменить место/ряд» "
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
                    "Напишите текст, который нужно стереть с билета (например: «Пушкинская карта», «ФИО зрителя» или «Сервисный сбор»):\n"
                    "Для сброса всех удалений напишите «сброс»\n"
                    "(для отмены напишите «отмена»)"
                )
            else:
                return send_text(
                    chat,
                    "✂️ **Универсальный ластик**\n\n"
                    "Сначала пришлите билет в PDF. Затем используйте команду:\n"
                    "/del Пушкинская карта\n"
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
            return send_text(chat, "🛡 Не найден недавний билет. Пришлите PDF с билетом, затем используйте команду /avito.")
        return _send_avito_preview(chat, uid, recent_token)
    if command == "/terms":
        return send_text(chat, f"Абонемент: {CONFIG['price_stars']} Stars за 30 дней без лимита билетов. Автопродление — каждые 30 дней; отмена /cancel_subscription. В одной пачке до 50 PDF и 100 МБ распакованных данных. Входящий файл до 20 МБ. Исходные файлы пачки хранятся на сервере не более часа и удаляются после отправки. По вопросам оплаты: /paysupport.")
    if command == "/paysupport":
        return send_text(chat, "Свяжитесь с администратором по вопросам обработки билетов, оплаты или возврата.",
                         {"inline_keyboard": [[{"text": "Администратор", "url": "tg://user?id="+str(CONFIG['admin_ids'][0])}]]})
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
                return send_text(chat, "\n".join(f"ID {p['user_id']} | {p['amount']} Stars | {p['charge_id']} | возврат={p['refunded']}" for p in get_store().payments()) or "Оплат пока нет.")
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
            return show_status(chat, uid)
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
    if msg.get("document"):
        if not has_access(uid):
            return show_status(chat, uid)
        collection = get_store().open_collection(uid, chat)
        ident = collection["id"] if collection else None
        if queued:
            minimal = {"chat": {"id": chat}, "from": {"id": uid}, "document": msg["document"]}
            get_store().enqueue(update_id, {"kind": "document", "message": minimal, "collection_id": ident})
            return send_text(chat, "Файл в очереди. Сообщу результат обработки.")
        return process_incoming_document(msg, job_id=update_id)
    return send_text(chat, GREETING)


def worker_loop():
    while True:
        try:
            cleanup_collections()
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
        text = "Обрабатываю..."
        if data == "unpack":
            text = "Распаковываю билеты..."
        elif data == "merge":
            text = "Склеиваю билеты в один PDF..."
        elif data.startswith("preview"):
            text = "Генерирую фото-превью..."
        elif data.startswith("fio"):
            text = "Редактирование ФИО..."
        elif data.startswith("choose_sec"):
            text = "Выбор сектора..."
        elif data.startswith("pushkin"):
            text = "Снимаю Пушкинскую карту..."
        elif data.startswith("avito"):
            text = "Генерирую версию для Авито..."
        if cb_id:
            tg("answerCallbackQuery", callback_query_id=cb_id, text=text)
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
    threading.Thread(target=worker_loop, daemon=True).start()
    offset = 0
    log.info("subscription bot started")
    while True:
        updates = tg("getUpdates", offset=offset, timeout=LONG_POLL, allowed_updates=["message", "pre_checkout_query", "callback_query"])
        if updates is None:
            time.sleep(2)
            continue
        for update in updates:
            ident = update["update_id"]
            dispatch_update(update)
            offset = ident + 1


# --- самопроверка без токена ---------------------------------------------
def self_test():
    import os.path as osp
    import tempfile
    dl = os.path.expanduser("~/Downloads")
    cases = [
        ("ticket_1756181.pdf", True),
        ("ticket_1756122.pdf", True),
        ("ticket_1756128.pdf", True),
        ("Afisha-Tickets-03107-722708.pdf", True),   # без изменений — исходный PDF
        ("188007073.pdf", True),                     # Афиша/МХТ тоже поддерживается
    ]
    fails = 0
    for name, expect_ok in cases:
        path = osp.join(dl, name)
        if not osp.exists(path):
            print(f"  пропуск {name} (нет файла)")
            continue
        with open(path, "rb") as fh:
            ok, res = clean_ticket(fh.read())
        good = (ok == expect_ok)
        if not good:
            fails += 1
        detail = f"{len(res)} байт" if ok else res[:70]
        print(f"  {'ok  ' if good else 'BAD '} {name}: ok={ok} (ожидали {expect_ok}) — {detail}")

    print("самопроверка:", "провалов нет" if not fails else f"{fails} провалов")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    if not BOT_TOKEN:
        print("Нет токена. Создай бота у @BotFather, получи токен и положи его:")
        print("  в файл .token рядом с bot.py,  ИЛИ")
        print("  в переменную окружения BILET_BOT_TOKEN")
        sys.exit(1)
    loop()
