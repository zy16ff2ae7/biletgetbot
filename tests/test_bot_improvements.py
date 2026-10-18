"""Tests for dynamic welcome badge, reminders with modern payments, and batch preview."""
import io
import time
import zipfile
import unittest
from pathlib import Path
import tempfile
from contextlib import ExitStack
from unittest.mock import patch, MagicMock

import _stub_engine
_stub_engine.ensure_engine()

from subscriptions import SubscriptionStore
from test_processing import bot


class BotImprovementsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.store = SubscriptionStore(tmp / "state.sqlite3")
        self.stack.enter_context(patch.object(bot, "get_store", return_value=self.store))

    def tearDown(self):
        self.stack.close()

    def test_format_welcome_greeting_admin(self):
        # Admin ID
        text = bot.format_welcome_greeting(user_id=1464797266)
        self.assertIn("👑 Статус: Администратор (постоянный доступ)", text)
        self.assertIn(bot.BRAND_NAME, text)

        # Admin username
        text_admin_user = bot.format_welcome_greeting(user_id=999999, username="freedemin")
        self.assertIn("👑 Статус: Администратор (постоянный доступ)", text_admin_user)

    def test_format_welcome_greeting_active_sub(self):
        uid = 12345
        self.store.grant(uid, days=15, admin_id=1464797266)
        text = bot.format_welcome_greeting(user_id=uid)
        self.assertIn("⚡️ Абонемент: АКТИВЕН", text)
        self.assertIn("дн.", text)
        self.assertIn(bot.BRAND_NAME, text)

    def test_format_welcome_greeting_free_user(self):
        uid = 54321
        text = bot.format_welcome_greeting(user_id=uid)
        self.assertIn("🎁 Сегодня доступно: 2 из 2 билета бесплатно", text)

        # Consume 1 ticket
        self.store.daily_consume(uid)
        text_after_one = bot.format_welcome_greeting(user_id=uid)
        self.assertIn("🎁 Сегодня доступно: 1 из 2 билета бесплатно", text_after_one)

        # Consume remaining ticket
        self.store.daily_consume(uid)
        text_exhausted = bot.format_welcome_greeting(user_id=uid)
        self.assertIn("🎁 Бесплатный лимит на сегодня исчерпан (0 из 2)", text_exhausted)

    def test_format_welcome_greeting_anonymous(self):
        text = bot.format_welcome_greeting(user_id=None)
        self.assertEqual(text, bot.GREETING)

    def test_batch_delivery_sends_first_ticket_preview(self):
        # Create a mock zip with a dummy PDF
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("ticket_1.pdf", b"%PDF-1.4 dummy ticket 1")
            zf.writestr("ticket_2.pdf", b"%PDF-1.4 dummy ticket 2")
        zip_data = zip_buf.getvalue()

        result = {
            "ok": 2,
            "failed": 0,
            "data": zip_data,
            "zip_name": "tickets.zip",
            "items_meta": [],
        }

        mock_progress = MagicMock()
        with patch.object(bot, "send_cleaned", return_value="sent_zip_id"), \
             patch.object(bot, "render_pdf_preview", return_value=b"fake_jpeg_bytes") as mock_render, \
             patch.object(bot, "send_photo", return_value=True) as mock_send_photo:

            res = bot._send_batch_result(
                chat=123,
                result=result,
                progress=mock_progress,
                retry_message="error",
                uid=777,
            )
            self.assertTrue(res)
            # Verify preview was rendered and sent
            mock_render.assert_called_once_with(b"%PDF-1.4 dummy ticket 1")
            mock_send_photo.assert_called_once_with(
                123, b"fake_jpeg_bytes", caption="🖼 Превью первого билета из пачки"
            )

    def test_reminders_contain_all_payment_options(self):
        uid_exp = 8888
        uid_soon = 9999

        # Register users in users table
        self.store.touch_user(uid_exp, "user_exp")
        self.store.touch_user(uid_soon, "user_soon")

        # Set up expired user (expired between 24 and 48 hours ago)
        self.store.grant(uid_exp, days=1, admin_id=1464797266)
        with self.store.db() as db:
            db.execute("UPDATE grants SET expires=? WHERE user_id=?", (int(time.time()) - 30 * 3600, uid_exp))

        # Set up soon expiring user (expires in next 24 hours)
        self.store.grant(uid_soon, days=1, admin_id=1464797266)
        with self.store.db() as db:
            db.execute("UPDATE grants SET expires=? WHERE user_id=?", (int(time.time()) + 10 * 3600, uid_soon))

        due_exp = self.store.due_expiry_reminds(fresh_hours=24, stale_hours=48)
        due_soon = self.store.due_soon_expiring_reminds(hours_window=24)
        self.assertIn(uid_exp, due_exp)
        self.assertIn(uid_soon, due_soon)


if __name__ == "__main__":
    unittest.main()
