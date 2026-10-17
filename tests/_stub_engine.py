"""Stub «Билетной» для движко-независимых тестов (CI, песочница).

Если настоящий движок не найден, подкладываем минимальный server.py,
чтобы bot.py импортировался и прогонялись «пустые» обработки
(билет возвращается без изменений). На VDS с настоящим движком не используется.
"""
import os
import re
import tempfile
from pathlib import Path

STUB_SERVER = '''"""Test stub: only for engine-independent tests."""
import re

import fitz

PUSHKIN_TEXT_RE = re.compile(r"(?!x)x")
EMAIL_RE = re.compile(r"(?!x)x")
AFISHA_SELLER_RE = re.compile(r"(?!x)x")
PRICE_RE = re.compile(r"(?!x)x")
FONT_REG = None


def extract_lines(page):
    return []


def field_tags(lines):
    return lines


def sanitize_doc_bytes(data):
    return data, []


def autoclean_doc(doc):
    copied = fitz.open(stream=doc.tobytes(), filetype="pdf")
    return copied, [{"removed": [], "warn": []} for _ in doc]


def _chelny_fields(page, lines):
    return None, None


def _is_kassa24_family(lines):
    return False


def _ticketland_name_rows(page, lines):
    return set()


def _ticketland_pushkin_block(page, lines):
    return set(), []


def _is_intickets_template(page, lines):
    return False


def _intickets_name_rows(page, lines):
    return set()


def _kassir_name_rows(lines):
    return set()


def _personal_line_what(line):
    return None


def is_name(text):
    return False


def int_to_rgb(value):
    return (0, 0, 0)
'''


def ensure_engine():
    candidates = []
    env = os.environ.get("BILETNAYA")
    if env:
        candidates.append(env)
    candidates.append(os.path.expanduser("~/Билетная"))
    for candidate in candidates:
        if candidate and (Path(candidate) / "server.py").exists():
            return
    stub = Path(tempfile.mkdtemp(prefix="biletnaya-stub-"))
    (stub / "server.py").write_text(STUB_SERVER, encoding="utf-8")
    (stub / "raster.py").write_text("")
    # Тестовые билеты вставляют текст этим шрифтом; подойдёт любой TTF с кириллицей.
    vendor = stub / "vendor"
    vendor.mkdir()
    bundled = Path(__file__).resolve().parents[1] / "brand" / "Prata-Regular.ttf"
    if bundled.exists():
        (vendor / "Arial.ttf").write_bytes(bundled.read_bytes())
    os.environ["BILETNAYA"] = str(stub)
