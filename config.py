"""Загрузка и валидация subscription.json (цена, срок, админы).

Отдельный модуль, чтобы и bot.py, и тексты читали одни и те же настройки
ровно один раз и с одинаковой валидацией.
"""
import json
import os
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))

CONFIG = json.loads(Path(HERE, "subscription.json").read_text())
if (type(CONFIG.get("price_stars")) is not int or not 1 <= CONFIG["price_stars"] <= 10000
        or CONFIG.get("period_days") != 30 or CONFIG.get("unlimited") is not True
        or not CONFIG.get("admin_ids")
        or not all(type(uid) is int and uid > 0 for uid in CONFIG["admin_ids"])):
    raise RuntimeError("Invalid subscription configuration")
