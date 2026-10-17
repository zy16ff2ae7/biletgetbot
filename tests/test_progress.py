"""Offline progress/card integration checks; no Telegram requests."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

from test_processing import bot, ticket
from test_batches import archive
from progress import Progress, BATCH_STEPS
import batches


class ProgressTests(unittest.TestCase):
    def setUp(self):
        cache = patch.object(bot, '_cached_file', return_value=None)
        cache.start()
        self.addCleanup(cache.stop)

    def transport(self, method, **kwargs):
        return {"ok": True, "result": {"message_id": 17}}

    def test_filename_and_details_are_escaped(self):
        card = Progress(1, '<a>&\nfile.pdf', self.transport)
        card.update(1, '<b>unsafe</b> & name')
        text = card.render()
        self.assertIn('&lt;a&gt;&amp; file.pdf', text)
        self.assertNotIn('<b>unsafe</b>', text)
        self.assertIn('&lt;b&gt;unsafe&lt;/b&gt;', text)

    def test_heartbeat_edits_one_message_without_new_messages(self):
        calls = []
        updated = threading.Event()
        def transport(method, **payload):
            calls.append((method, payload))
            if method == 'editMessageText':
                updated.set()
            return self.transport(method, **payload)
        with Progress(1, 'a.pdf', transport, interval=0.01) as card:
            self.assertTrue(updated.wait(1))
            card.update(2, 'Сверяю данные')
            card.finish('Файл отправлен')
        card.thread.join(1)
        self.assertFalse(card.thread.is_alive())
        self.assertEqual(1, sum(method == 'sendMessage' for method, _ in calls))
        edits = [payload for method, payload in calls if method == 'editMessageText']
        self.assertTrue(all(payload['message_id'] == 17 for payload in edits))
        self.assertIn('Всё готово', edits[-1]['text'])

    def test_completion_during_slow_edit_is_not_lost(self):
        entered, release = threading.Event(), threading.Event()
        edits = []
        def transport(method, **payload):
            if method == 'editMessageText':
                edits.append(payload['text'])
                if len(edits) == 1:
                    entered.set()
                    release.wait(1)
            return self.transport(method, **payload)
        with Progress(1, 'a.pdf', transport, interval=0.01) as card:
            self.assertTrue(entered.wait(1))
            card.finish('Файл отправлен')
            release.set()
        card.thread.join(1)
        self.assertIn('Всё готово', edits[-1])

    def test_unexpected_failure_stops_spinner_and_does_not_claim_success(self):
        with self.assertRaises(ValueError):
            with Progress(1, 'a.pdf', self.transport, interval=0.01) as card:
                card.update(1)
                raise ValueError('test')
        card.thread.join(1)
        self.assertFalse(card.thread.is_alive())
        self.assertEqual('error', card.outcome)
        self.assertNotIn('Всё готово', card.render())
        self.assertIn('○ Отправляю результат', card.render())

    def test_failed_initial_notification_exits_cosmetic_thread(self):
        with Progress(1, 'a.pdf', Mock(side_effect=OSError('network'))) as card:
            card.finish('Файл отправлен')
        card.thread.join(1)
        self.assertFalse(card.thread.is_alive())
        self.assertIsNone(card.message_id)

    def test_slow_initial_card_never_blocks_ticket_delivery(self):
        entered, release, delivered = threading.Event(), threading.Event(), threading.Event()
        returned = threading.Event()
        cards = []
        def transport(method, **payload):
            if method == 'sendMessage':
                entered.set()
                release.wait(5)
                returned.set()
            return self.transport(method, **payload)
        def make_card(chat, name, _transport, steps, **_kwargs):
            card = Progress(chat, name, transport, steps, interval=0.01)
            cards.append(card)
            return card
        def download(_file_id):
            self.assertTrue(entered.wait(1))
            return ticket('plain')
        def upload(*_args, **_kwargs):
            self.assertFalse(returned.is_set(), 'Ticket waited for cosmetic sendMessage')
            delivered.set()
            return True
        try:
            with patch.object(bot, 'has_access', return_value=True), \
                    patch.object(bot, 'Progress', side_effect=make_card), \
                    patch.object(bot, 'download_file', side_effect=download), \
                    patch.object(bot, 'send_cleaned', side_effect=upload):
                bot.process_document({'chat': {'id': 1}, 'from': {'id': 1},
                                      'document': {'file_id': 'offline', 'file_name': 'a.pdf'}})
            self.assertTrue(delivered.is_set())
            self.assertFalse(release.is_set())
        finally:
            release.set()
            for card in cards:
                card.thread.join(1)

    def test_cosmetic_thread_closes_its_own_transport(self):
        cleaned = threading.Event()
        with Progress(1, 'a.pdf', Mock(return_value={}), cleanup=cleaned.set) as card:
            card.finish('Файл отправлен')
        card.thread.join(1)
        self.assertTrue(cleaned.is_set())

    def test_http_connections_are_reused_only_within_each_thread(self):
        sessions = []
        finished = threading.Event()
        with patch.object(bot, '_http_local', threading.local()), \
                patch.object(bot.requests, 'Session', side_effect=lambda: Mock()) as factory:
            first = bot.http_session()
            self.assertIs(first, bot.http_session())
            def worker():
                second = bot.http_session()
                sessions.append(second)
                self.assertIs(second, bot.http_session())
                bot.close_http_session()
                finished.set()
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(finished.wait(1))
            thread.join(1)
            self.assertIsNot(first, sessions[0])
            self.assertEqual(2, factory.call_count)
            sessions[0].close.assert_called_once()
            first.close.assert_not_called()
            bot.close_http_session()
            first.close.assert_called_once()

    def test_rate_limit_is_respected_then_final_status_retried(self):
        calls = []
        retrying = threading.Event()
        def transport(method, **payload):
            if method == 'editMessageText':
                calls.append(time.monotonic())
                if len(calls) == 1:
                    retrying.set()
                    return {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 1}}
            return self.transport(method, **payload)
        with Progress(1, 'a.pdf', transport, interval=0.01) as card:
            self.assertTrue(retrying.wait(1))
            card.finish('Файл отправлен')
        card.thread.join(2)
        self.assertEqual(2, len(calls))
        self.assertGreaterEqual(calls[1] - calls[0], 0.95)
        self.assertFalse(card.thread.is_alive())

    def test_batch_reports_real_counts_including_failed_pdf(self):
        events = []
        def cleaner(data):
            return (False, 'bad', {}) if data == b'bad' else (True, data, {'changed': False})
        result = batches.process_items([('a.pdf', b'good'), ('b.pdf', b'bad')], cleaner,
                                       lambda *args: events.append(args))
        self.assertEqual((1, 1), (result['ok'], result['failed']))
        self.assertEqual(('pack', 2, 2, 1, 1), events[-1])
        self.assertEqual([1, 2], [item[1] for item in events if item[0] == 'completed'])
        card = Progress(1, 'Пачка', self.transport, BATCH_STEPS)
        card.batch(1, 2, 1)
        self.assertIn('Обработано: 1 из 2 · ошибок: 1', card.render())
        self.assertIn('▰▰▰▰▰▱▱▱▱▱', card.render())

    def run_document(self, source, name='a.pdf', delivered=True, progress_available=True):
        cards, phases = [], []
        transport = self.transport if progress_available else Mock(return_value={})
        class RecordingProgress(Progress):
            def update(self, stage, detail=None):
                phases.append(stage)
                super().update(stage, detail)
        def make_card(chat, filename, _transport, steps, **_kwargs):
            card = RecordingProgress(chat, filename, transport, steps, interval=0.01)
            cards.append(card)
            return card
        def upload(*_args, **_kwargs):
            self.assertIsNone(cards[-1].outcome)
            self.assertEqual(3, cards[-1].stage)
            return delivered
        with patch.object(bot, 'has_access', return_value=True), \
                patch.object(bot, 'download_file', return_value=source), \
                patch.object(bot, 'Progress', side_effect=make_card), \
                patch.object(bot, 'send_text') as text, \
                patch.object(bot, 'send_cleaned', side_effect=upload) as sent:
            bot.process_document({'chat': {'id': 1}, 'from': {'id': 1},
                                  'document': {'file_id': 'offline', 'file_name': name}})
        if cards[-1].thread:
            cards[-1].thread.join(1)
        return cards[-1], phases, sent, text

    def test_real_pdf_stages_and_success_only_after_upload(self):
        card, phases, sent, _ = self.run_document(ticket('kassir'))
        self.assertEqual([1, 2, 3], phases)
        self.assertEqual('done', card.outcome)
        sent.assert_called_once()

    def test_failed_upload_does_not_show_ready(self):
        card, _, _, text = self.run_document(ticket('plain'), delivered=False)
        self.assertEqual('error', card.outcome)
        self.assertNotIn('Всё готово', card.render())
        text.assert_called_once()

    def test_download_failure_leaves_future_stages_incomplete(self):
        card, _, sent, text = self.run_document(None)
        self.assertEqual('error', card.outcome)
        sent.assert_not_called()
        text.assert_called_once()

    def test_invalid_pdf_rejected_and_not_sent(self):
        card, _, sent, text = self.run_document(b'not PDF')
        self.assertEqual('error', card.outcome)
        sent.assert_not_called()
        text.assert_called_once()

    def test_progress_notification_failure_does_not_block_delivery(self):
        card, _, sent, _ = self.run_document(ticket('kassir'), progress_available=False)
        self.assertEqual('done', card.outcome)
        sent.assert_called_once()

    def test_zip_has_real_progress_and_partial_failure_summary(self):
        card, phases, sent, _ = self.run_document(archive([('a.pdf', ticket('plain')),
                                                         ('b.pdf', b'bad')]), 'tickets.zip')
        self.assertEqual((2, 2, 1), (card.completed, card.total, card.failed))
        self.assertEqual('done', card.outcome)
        self.assertIn(2, phases)
        self.assertIn('ошибок: 1', card.render())
        self.assertTrue(sent.call_args.args[2].endswith('.zip'))
        self.assertIn('р6 м23', sent.call_args.args[2])

    def test_dispatch_has_no_progress_network_calls(self):
        store = Mock()
        store.seen.return_value = False
        with patch.object(bot, 'get_store', return_value=store), \
                patch.object(bot, 'progress_request') as request:
            bot.dispatch_update({'update_id': 99, 'message': {'chat': {'id': 1}, 'from': {'id': 1},
                                                            'document': {'file_id': 'offline'}}})
        request.assert_not_called()
        store.enqueue.assert_called_once()


if __name__ == '__main__':
    unittest.main()
