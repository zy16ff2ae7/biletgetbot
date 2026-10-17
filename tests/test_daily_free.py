"""Бесплатный тир: 2 билета в день, списание и восстановление на новый день."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest

import _stub_engine
_stub_engine.ensure_engine()

from subscriptions import SubscriptionStore


class DailyFreeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.store = SubscriptionStore(tmp / "s.sqlite3")

    def tearDown(self):
        self.stack.close()

    def test_two_free_per_day(self):
        self.assertEqual(2, self.store.daily_left(1))
        self.assertTrue(self.store.daily_consume(1))
        self.assertEqual(1, self.store.daily_left(1))
        self.assertTrue(self.store.daily_consume(1))
        self.assertEqual(0, self.store.daily_left(1))
        self.assertFalse(self.store.daily_consume(1))  # лимит исчерпан
        self.assertFalse(self.store.daily_consume(1, count=1))

    def test_batch_consumption_respects_limit(self):
        self.assertTrue(self.store.daily_consume(2, count=2))
        self.assertEqual(0, self.store.daily_left(2))
        self.assertFalse(self.store.daily_consume(2, count=1))

    def test_users_are_independent(self):
        self.assertTrue(self.store.daily_consume(3, count=2))
        self.assertEqual(2, self.store.daily_left(4))

    def test_new_day_resets_limit(self):
        import sqlite3, time
        self.store.daily_consume(5, count=2)
        with self.store.db() as db:
            yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
            db.execute("UPDATE daily_usage SET day=? WHERE user_id=5", (yesterday,))
        self.assertEqual(2, self.store.daily_left(5))


if __name__ == "__main__":
    unittest.main()
