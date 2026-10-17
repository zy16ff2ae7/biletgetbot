"""Сквозной путь бесплатного тира: 2 билета обрабатываются, третий — отказ."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import _stub_engine
_stub_engine.ensure_engine()

from test_processing import bot, ticket


class FakeProgress:
    """Синхронная замена Progress: финал попадает в records сразу."""

    records = []

    def __init__(self, chat, name, transport=None, steps=(), cleanup=None, **kwargs):
        self.chat = chat
        self.name = name
        FakeProgress.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, *args, **kwargs):
        pass

    def batch(self, *args, **kwargs):
        pass

    def finish(self, detail, failed=False):
        FakeProgress.last.outcome = "error" if failed else "done"
        FakeProgress.last.detail = detail
        FakeProgress.records.append(("finish", detail))


class FreeTierFlowTests(unittest.TestCase):
    def setUp(self):
        FakeProgress.last = None
        FakeProgress.records = []
        self.stack = ExitStack()
        tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        from subscriptions import SubscriptionStore
        self.store = SubscriptionStore(tmp / "s.sqlite3")
        self.sent = []
        self.stack.enter_context(patch.object(bot, "get_store", return_value=self.store))
        self.stack.enter_context(patch.object(bot, "send_text",
                                              side_effect=lambda chat, text, markup=None:
                                              self.sent.append(("send", text)) or True))
        self.stack.enter_context(patch.object(bot, "Progress", FakeProgress))
        self.stack.enter_context(patch.object(bot, "_photo_or_none", return_value=None))
        self.stack.enter_context(patch.object(bot, "send_cleaned", return_value="file-id-1"))
        self.stack.enter_context(patch.object(bot, "download_file",
                                              side_effect=lambda fid: ticket("kassir")))

    def tearDown(self):
        self.stack.close()

    def _doc_msg(self, file_id="fid1", name="t.pdf"):
        return {"chat": {"id": 501, "type": "private"},
                "from": {"id": 501}, "document": {"file_id": file_id, "file_unique_id": "u" + file_id,
                                                  "file_name": name, "file_size": 1000}}

    def _all_texts(self):
        return [text for _, text in self.sent + FakeProgress.records]

    def test_free_user_processing_flow(self):
        # 1-й билет: бесплатник обрабатывается без абонемента, лимит списывается.
        bot.process_document(self._doc_msg(), None, job_id=1)
        self.assertEqual(1, self.store.daily_left(501))
        self.assertTrue(any("Бесплатных осталось сегодня: 1" in t for t in self._all_texts()),
                        self._all_texts())
        # 2-й билет: ещё проходит, лимит исчерпан + подсказка про абонемент.
        bot.process_document(self._doc_msg("fid2", "t2.pdf"), None, job_id=2)
        self.assertEqual(0, self.store.daily_left(501))
        self.assertTrue(any("Бесплатный лимит на сегодня исчерпан" in t for t in self._all_texts()),
                        self._all_texts())
        self.assertTrue(any("Это были бесплатные 2 билета" in t for t in self._all_texts()),
                        self._all_texts())
        # 3-й: отказ с кнопкой абонемента.
        bot.process_document(self._doc_msg("fid3", "t3.pdf"), None, job_id=3)
        self.assertTrue(any("уже перепечатаны" in t for t in self._all_texts()), self._all_texts())
        # через входную точку — тот же отказ.
        bot.process_incoming_document(self._doc_msg("fid4", "t4.pdf"), job_id=4)
        self.assertTrue(any("уже перепечатаны" in t for t in self._all_texts()), self._all_texts())
        # лимит не уходит ниже нуля.
        self.assertEqual(0, self.store.daily_left(501))
        # у абонементчика лимитов нет: обычный финал, списания не было.
        self.store.grant_trial(501, days=1)
        self.assertEqual(2, self.store.daily_left(501))
        bot.process_document(self._doc_msg("fid5", "t5.pdf"), None, job_id=5)
        self.assertTrue(any("Автоматических правок нет" in t for t in self._all_texts()),
                        self._all_texts())
        self.assertEqual(2, self.store.daily_left(501))


if __name__ == "__main__":
    unittest.main()
