"""Рассылка: только по подтверждению и в фоне."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import _stub_engine
_stub_engine.ensure_engine()

from test_processing import bot


class BroadcastGuardTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(bot, "get_store", return_value=bot.get_store()))
        self.send = self.stack.enter_context(patch.object(bot, "send_text", return_value=True))
        self.stack.enter_context(patch.object(bot, "progress_request", return_value={}))

    def tearDown(self):
        self.stack.close()
        bot._pending_broadcast.clear()

    def test_broadcast_requires_confirmation(self):
        bot._pending_broadcast.clear()
        bot.broadcast_text(999, 999, "привет")
        # Ничего не отправлено пользователям, показан предпросмотр.
        body = self.send.call_args[0][1]
        self.assertIn("Предпросмотр", body)
        self.assertNotIn("завершена", body)

    def test_confirmed_broadcast_runs_in_background_thread(self):
        done = threading.Event()
        recipients = [401, 402, 403]

        def fake_all():
            done.set()
            return recipients

        with patch.object(bot, "is_admin", return_value=True), \
             patch.object(bot.get_store(), "all_user_ids", side_effect=fake_all), \
             patch.object(bot, "_broadcast_running", False):
            bot._pending_broadcast[999] = "тест"
            bot.handle_callback_query({"data": "adm:bcastgo", "from": {"id": 999, "username": None},
                                       "message": {"chat": {"id": 999}}})
            self.assertTrue(done.wait(5), "рассылка должна выполняться в фоне")
            deadline = time.time() + 5
            while bot._broadcast_running and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(bot._broadcast_running)


if __name__ == "__main__":
    unittest.main()
