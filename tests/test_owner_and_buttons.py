import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import tempfile
from contextlib import ExitStack

import _stub_engine
_stub_engine.ensure_engine()

from test_processing import bot
from subscriptions import SubscriptionStore


class OwnerAndButtonsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.store = SubscriptionStore(tmp / "state.sqlite3")
        self.stack.enter_context(patch.object(bot, "get_store", return_value=self.store))
        self.send_mock = self.stack.enter_context(patch.object(bot, "send_text", return_value=True))
        self.tg_mock = self.stack.enter_context(patch.object(bot, "tg", return_value=None))

    def tearDown(self):
        self.stack.close()

    def test_owner_button_present_for_admin_only(self):
        # Admin user
        admin_markup = bot.get_main_markup(user_id=1464797266)
        all_buttons = [btn["text"] for row in admin_markup["inline_keyboard"] for btn in row]
        self.assertIn("👑 Панель владельца", all_buttons)

        # Admin by username
        admin_user_markup = bot.get_main_markup(user_id=999999, username="freedemin")
        all_buttons_user = [btn["text"] for row in admin_user_markup["inline_keyboard"] for btn in row]
        self.assertIn("👑 Панель владельца", all_buttons_user)

        # Non-admin user
        regular_markup = bot.get_main_markup(user_id=123456, username="regular_user")
        all_buttons_reg = [btn["text"] for row in regular_markup["inline_keyboard"] for btn in row]
        self.assertNotIn("👑 Панель владельца", all_buttons_reg)

    def test_edit_or_send_falls_back_when_edit_fails(self):
        # When editMessageText returns None (failure, e.g. on media message)
        self.tg_mock.return_value = None
        bot.edit_or_send(chat=111, msg_id=222, text="Test text", markup={"inline_keyboard": []})
        self.send_mock.assert_called_once_with(111, "Test text", markup={"inline_keyboard": []})

    def test_edit_or_send_edits_in_place_when_possible(self):
        # When editMessageText succeeds
        self.tg_mock.return_value = {"message_id": 222, "text": "Test text"}
        res = bot.edit_or_send(chat=111, msg_id=222, text="Test text", markup={"inline_keyboard": []})
        self.assertEqual(res, {"message_id": 222, "text": "Test text"})
        self.send_mock.assert_not_called()

    def test_callback_welcome_admin_access(self):
        # Non-admin clicks welcome:admin
        cb_user = {
            "id": "cb1",
            "from": {"id": 12345, "username": "someuser"},
            "message": {"chat": {"id": 12345}, "message_id": 10},
            "data": "welcome:admin"
        }
        bot.handle_callback_query(cb_user)
        self.send_mock.assert_called_with(12345, "Панель доступна только администраторам бота.")

        self.send_mock.reset_mock()
        # Admin clicks welcome:admin
        cb_admin = {
            "id": "cb2",
            "from": {"id": 1464797266, "username": "admin"},
            "message": {"chat": {"id": 1464797266}, "message_id": 11},
            "data": "welcome:admin"
        }
        bot.handle_callback_query(cb_admin)
        self.send_mock.assert_called()
        call_text = self.send_mock.call_args[0][1]
        self.assertIn("ПАНЕЛЬ ВЛАДЕЛЬЦА", call_text)

    def test_templates_menu_accessible_even_without_subscription(self):
        # User without active subscription clicks tpl:menu
        cb = {
            "id": "cb3",
            "from": {"id": 55555, "username": "newbie"},
            "message": {"chat": {"id": 55555}, "message_id": 20},
            "data": "tpl:menu"
        }
        bot.handle_callback_query(cb)
        self.send_mock.assert_called()
        call_text = self.send_mock.call_args[0][1]
        self.assertIn("Готовые шаблоны", call_text)

    def test_settings_toggle_accessible_without_cooldown(self):
        # Rapid settings toggle does not get blocked
        cb = {
            "id": "cb4",
            "from": {"id": 55555, "username": "newbie"},
            "message": {"chat": {"id": 55555}, "message_id": 21},
            "data": "set:price"
        }
        bot.handle_callback_query(cb)
        self.send_mock.assert_called()


if __name__ == "__main__":
    unittest.main()
