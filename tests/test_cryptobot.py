"""Unit tests for CryptoBot payment integration."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import bot
import cryptobot
from subscriptions import SubscriptionStore


class TestCryptoBotIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SubscriptionStore(Path(self.tmp.name) / "store.sqlite3")

    @patch("cryptobot.requests.post")
    def test_create_invoice_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ok": True,
            "result": {
                "invoice_id": 999111,
                "bot_invoice_url": "https://t.me/CryptoBot?start=test1234",
                "status": "active",
                "amount": "750",
            }
        }
        mock_post.return_value = mock_resp

        inv = cryptobot.create_invoice(12345, 750)
        self.assertIsNotNone(inv)
        self.assertEqual(999111, inv["invoice_id"])
        self.assertEqual("https://t.me/CryptoBot?start=test1234", inv["bot_invoice_url"])

    @patch("cryptobot.requests.get")
    def test_check_invoice_paid(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ok": True,
            "result": {
                "items": [
                    {
                        "invoice_id": 999111,
                        "status": "paid",
                        "amount": "750",
                    }
                ]
            }
        }
        mock_get.return_value = mock_resp

        inv = cryptobot.check_invoice(999111)
        self.assertIsNotNone(inv)
        self.assertEqual("paid", inv["status"])

    @patch.object(bot, "get_store")
    @patch("cryptobot.create_invoice")
    @patch.object(bot, "send_text")
    def test_handle_cryptobot_create_sends_invoice_button(self, mock_send, mock_create, mock_get_store):
        mock_get_store.return_value = self.store
        mock_create.return_value = {
            "invoice_id": 888222,
            "bot_invoice_url": "https://t.me/CryptoBot?start=IV888222",
        }

        bot.handle_cryptobot_create(chat=1001, user_id=1001)
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        self.assertIn("CRYPTOBOT", args[1])
        kb = kwargs.get("reply_markup") or kwargs.get("markup") or {}
        urls = [b.get("url") for row in kb.get("inline_keyboard", []) for b in row if b.get("url")]
        self.assertIn("https://t.me/CryptoBot?start=IV888222", urls)

    @patch.object(bot, "get_store")
    @patch("cryptobot.check_invoice")
    @patch.object(bot, "edit_or_send")
    def test_handle_cryptobot_check_grants_access_when_paid(self, mock_edit_or_send, mock_check, mock_get_store):
        mock_get_store.return_value = self.store
        mock_check.return_value = {
            "invoice_id": 777333,
            "status": "paid",
        }

        self.assertFalse(self.store.status(1002)["active"])
        bot.handle_cryptobot_check(chat=1002, user_id=1002, inv_id="777333")
        self.assertTrue(self.store.status(1002)["active"])
        mock_edit_or_send.assert_called_once()
        args, _ = mock_edit_or_send.call_args
        self.assertIn("ПОДТВЕРЖДЕНА", args[2])

    @patch.object(bot, "get_store")
    @patch.object(bot, "handle_cryptobot_create")
    def test_callback_routing_cbpay_create(self, mock_create, mock_get_store):
        mock_get_store.return_value = self.store
        cb = {
            "data": "cbpay:create",
            "from": {"id": 1003},
            "message": {"chat": {"id": 1003}, "message_id": 55},
        }
        bot.handle_callback_query(cb)
        mock_create.assert_called_once_with(1003, 1003, msg_id=55)


if __name__ == "__main__":
    unittest.main()
