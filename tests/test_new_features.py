"""Промокоды, триал, напоминания, статистика, профиль, кулдаун."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import _stub_engine
_stub_engine.ensure_engine()

from test_processing import bot
from subscriptions import SubscriptionStore


class NewFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.store = SubscriptionStore(tmp / "state.sqlite3")
        self.stack.enter_context(patch.object(bot, "get_store", return_value=self.store))
        self.send = self.stack.enter_context(patch.object(bot, "send_text", return_value=True))
        self.stack.enter_context(patch.object(bot, "progress_request", return_value={}))
        # без HTTP-загрузки аватарок в тестах профиля
        self.stack.enter_context(patch.object(bot, "_photo_or_none", return_value=None))

    def tearDown(self):
        self.stack.close()

    # --- промокоды -----------------------------------------------------------
    def test_promo_lifecycle(self):
        self.store.touch_user(7, "user7")
        self.store.promo_create("promo-1", 5, 2, 999)
        expires, days = self.store.promo_redeem(7, "PROMO-1")
        self.assertEqual(5, days)
        self.assertTrue(self.store.status(7)["active"])
        self.store.promo_redeem(7, "promo-1")  # вторая активация
        with self.assertRaises(ValueError):
            self.store.promo_redeem(8, "PROMO-1")  # лимит исчерпан
        with self.assertRaises(ValueError):
            self.store.promo_redeem(8, "НЕТУ")
        self.assertTrue(self.store.promo_delete("PROMO-1"))
        self.assertFalse(self.store.promo_delete("PROMO-1"))

    def test_promo_cmd_rejects_bad_input(self):
        bot.cmd_promo(7, 7, "")
        self.assertIn("/promo", self.send.call_args[0][1])

    # --- триал ---------------------------------------------------------------
    def test_trial_once_only(self):
        expires, created = self.store.grant_trial(9, days=1)
        self.assertTrue(created)
        again, created2 = self.store.grant_trial(9, days=1)
        self.assertFalse(created2)
        self.assertEqual(expires, again)

    # --- напоминания ---------------------------------------------------------
    def test_expiry_reminders_single_shot(self):
        self.store.touch_user(11)
        with self.store.db() as db:
            past = int(time.time()) - 30 * 3600  # кончился 30 часов назад
            db.execute("INSERT INTO grants(user_id,expires,admin_id) VALUES(?,?,0)", (11, past))
        self.assertEqual([11], self.store.due_expiry_reminds())
        self.store.mark_expiry_reminded(11)
        self.assertEqual([], self.store.due_expiry_reminds())

    def test_active_users_are_not_reminded(self):
        self.store.touch_user(12)
        self.store.grant_trial(12, days=1)
        self.assertEqual([], self.store.due_expiry_reminds())

    # --- статистика ----------------------------------------------------------
    def test_admin_stats_counts(self):
        self.store.touch_user(13)
        self.store.bump_processed(13, 4)
        self.store.grant_trial(13)
        s = self.store.admin_stats()
        self.assertGreaterEqual(s["users_total"], 1)
        self.assertGreaterEqual(s["active_now"], 1)
        self.assertGreaterEqual(s["processed_total"], 4)

    # --- кулдаун -------------------------------------------------------------
    def test_cooldown_blocks_second_call(self):
        self.assertTrue(bot.cooldown_ok(21, seconds=10))
        self.assertFalse(bot.cooldown_ok(21, seconds=10))
        self.assertTrue(bot.cooldown_ok(22, seconds=10))  # другой пользователь не заблокирован

    # --- профиль -------------------------------------------------------------
    def test_profile_mentions_days_left(self):
        self.store.touch_user(31)
        self.store.grant_trial(31, days=1)
        bot.send_profile(31, 31)
        text = self.send.call_args[0][1]
        self.assertIn("осталось", text)
        self.assertIn("Билетов перепечатано", text)


if __name__ == "__main__":
    unittest.main()
