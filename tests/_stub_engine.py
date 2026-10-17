"""Stub «Билетной» для движко-независимых тестов (CI, песочница).

Если настоящий движок не найден, подкладываем минимальный server.py,
чтобы bot.py импортировался; очистка PDF в таких тестах не вызывается.
На VDS с настоящим движком ничего не меняет.
"""
import os
import tempfile
from pathlib import Path


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
    (stub / "server.py").write_text(
        '"""Test stub: only for engine-independent tests."""\n'
        'import fitz\n\n\n'
        'def extract_lines(page):\n    return []\n\n\n'
        'def field_tags(lines):\n    return lines\n',
        encoding="utf-8")
    (stub / "raster.py").write_text("")
    os.environ["BILETNAYA"] = str(stub)
