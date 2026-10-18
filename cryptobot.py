"""CryptoBot (Crypto Pay API) integration for @biletgetbot / ДЕД РЕДАЧИТ.

Provides invoice creation, status checking, and automatic subscription activation
via CryptoBot (USDT, TON, BTC, bank cards via CryptoBot).
"""
import logging
import os
from pathlib import Path
import time
import requests

log = logging.getLogger(__name__)

DEFAULT_CRYPTOBOT_TOKEN = "646236:AAK0ucV1apPIJmYAa3m7DTy6GIcgtnNyJ1i"
API_URL = "https://pay.crypt.bot/api"


def get_cryptobot_token():
    env = os.environ.get("CRYPTOBOT_TOKEN", "").strip()
    if env:
        return env
    token_file = Path(__file__).resolve().parent / ".cryptobot_token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return DEFAULT_CRYPTOBOT_TOKEN


def create_invoice(user_id, amount_rub=750, description="ДЕД РЕДАЧИТ: абонемент на 30 дней", bot_username="ded_bilet_bot"):
    """Create a fiat RUB invoice in CryptoBot accepting any supported crypto/fiat asset."""
    token = get_cryptobot_token()
    if not token:
        log.warning("CryptoBot token is missing")
        return None
    headers = {"Crypto-Pay-API-Token": token}
    payload = {
        "currency_type": "fiat",
        "fiat": "RUB",
        "amount": str(amount_rub),
        "description": description,
        "payload": f"sub_{user_id}_{int(time.time())}",
        "paid_btn_name": "openBot",
        "paid_btn_url": f"https://t.me/{bot_username}",
        "expires_in": 86400,
    }
    try:
        resp = requests.post(f"{API_URL}/createInvoice", headers=headers, json=payload, timeout=12)
        data = resp.json()
        if data.get("ok"):
            return data["result"]
        log.warning("CryptoBot createInvoice failed: %s", data)
    except Exception as e:
        log.warning("CryptoBot createInvoice network exception: %s", e)
    return None


def check_invoice(invoice_id):
    """Retrieve current invoice state from CryptoBot (active, paid, expired)."""
    token = get_cryptobot_token()
    if not token:
        return None
    headers = {"Crypto-Pay-API-Token": token}
    try:
        resp = requests.get(f"{API_URL}/getInvoices", headers=headers, params={"invoice_ids": str(invoice_id)}, timeout=12)
        data = resp.json()
        if data.get("ok") and data.get("result", {}).get("items"):
            return data["result"]["items"][0]
    except Exception as e:
        log.warning("CryptoBot getInvoices network exception: %s", e)
    return None
