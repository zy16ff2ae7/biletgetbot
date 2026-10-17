"""Offline delivery reuse, stale ID fallback, access and retention checks."""
from contextlib import ExitStack, closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_processing import bot, ticket
import delivery_cache


class DeliveryCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = delivery_cache.DeliveryCache(Path(self.tmp.name) / 'cache.sqlite3')

    def test_keys_isolate_owner_chat_name_and_processing_version(self):
        base = ['v1', 111, 111, 'a.pdf', 'telegram:unique']
        key = delivery_cache.key_for(*base)
        self.cache.put([key], 'ready-id', 'Verified')
        self.assertEqual('ready-id', self.cache.get(key)['file_id'])
        for index, value in enumerate(['v2', 222, 333, 'b.pdf', 'telegram:other']):
            changed = base.copy()
            changed[index] = value
            self.assertIsNone(self.cache.get(delivery_cache.key_for(*changed)))

    def test_expiration_capacity_permissions_and_no_pdf_storage(self):
        with patch.object(delivery_cache, 'MAX_ENTRIES', 2), \
                patch.object(delivery_cache.time, 'time', return_value=100000):
            self.cache.put(['one', 'two', 'three'], 'ready-id', 'Caption')
        with closing(sqlite3.connect(self.cache.path)) as db:
            self.assertEqual(2, db.execute('SELECT COUNT(*) FROM files').fetchone()[0])
            self.assertEqual(['key', 'file_id', 'caption', 'created'],
                             [row[1] for row in db.execute('PRAGMA table_info(files)')])
        self.assertEqual(0o600, self.cache.path.stat().st_mode & 0o777)
        with patch.object(delivery_cache.time, 'time', return_value=100000 + delivery_cache.TTL):
            self.assertIsNone(self.cache.get('one'))
            self.assertIsNone(self.cache.get('two'))
            self.assertIsNone(self.cache.get('three'))

    def test_successful_upload_stores_file_id_and_disables_type_detection(self):
        response = Mock()
        response.json.return_value = {'ok': True, 'result': {'document': {'file_id': 'ready-id'}}}
        with patch.object(bot, 'http_session') as session, patch.object(bot, 'STATE_DIR', Path(self.tmp.name)):
            session.return_value.post.return_value = response
            self.assertTrue(bot.send_cleaned(111, b'pdf', 'a.pdf', 'Caption', cache_keys=['key']))
            kwargs = session.return_value.post.call_args.kwargs
            self.assertEqual('true', kwargs['data']['disable_content_type_detection'])
            self.assertEqual('application/pdf', kwargs['files']['document'][2])
            self.assertEqual('ready-id', bot._cached_file('key')['file_id'])

    def test_reuse_has_no_multipart_upload_and_invalid_id_is_evicted(self):
        response = Mock()
        response.json.return_value = {'ok': False, 'error_code': 400,
                                     'description': 'Bad Request: wrong file identifier specified'}
        with patch.object(bot, 'http_session') as session, patch.object(bot, 'STATE_DIR', Path(self.tmp.name)):
            bot._remember_delivery(['key'], 'old-id', 'Caption')
            session.return_value.post.return_value = response
            self.assertIsNone(bot.send_cleaned(111, b'', 'a.pdf', 'Caption', ['key'], 'old-id'))
            kwargs = session.return_value.post.call_args.kwargs
            self.assertNotIn('files', kwargs)
            self.assertEqual('old-id', kwargs['json']['document'])
            self.assertIsNone(bot._cached_file('key'))

    def test_429_honors_full_retry_after_without_uploading_again(self):
        response = Mock()
        response.json.side_effect = [{'ok': False, 'error_code': 429, 'parameters': {'retry_after': 61}},
                                     {'ok': True}]
        with patch.object(bot, 'http_session') as session, patch.object(bot.time, 'sleep') as sleep:
            session.return_value.post.return_value = response
            self.assertTrue(bot.send_cleaned(111, b'', 'a.pdf', 'Caption', reuse_id='ready-id'))
            sleep.assert_called_once_with(61)
            self.assertEqual(2, session.return_value.post.call_count)

    def test_failed_upload_never_populates_cache(self):
        response = Mock()
        response.json.return_value = {'ok': False, 'error_code': 403, 'description': 'Forbidden'}
        with patch.object(bot, 'http_session') as session, patch.object(bot, 'STATE_DIR', Path(self.tmp.name)):
            session.return_value.post.return_value = response
            self.assertFalse(bot.send_cleaned(111, b'pdf', 'a.pdf', 'Caption', cache_keys=['key']))
            self.assertIsNone(bot._cached_file('key'))

    def run_document(self, cached, sent, source=None, allowed=True):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(bot, 'STATE_DIR', Path(self.tmp.name)))
        stack.enter_context(patch.object(bot, 'has_access', return_value=allowed))
        card = stack.enter_context(patch.object(bot, 'Progress')).return_value.__enter__.return_value
        stack.enter_context(patch.object(bot, '_cached_file', side_effect=cached))
        download = stack.enter_context(patch.object(bot, 'download_file', return_value=source or ticket('kassir')))
        clean = stack.enter_context(patch.object(bot, 'clean_ticket_with_report', wraps=bot.clean_ticket_with_report))
        send = stack.enter_context(patch.object(bot, 'send_cleaned', side_effect=sent))
        stack.enter_context(patch.object(bot, 'show_status'))
        stack.enter_context(patch.object(bot, 'send_text'))
        bot.process_document({'chat': {'id': 111}, 'from': {'id': 111},
                              'document': {'file_id': 'source-id', 'file_unique_id': 'source-unique', 'file_name': 'a.pdf'}})
        return card, download, clean, send

    def test_repeat_uses_cached_id_without_download_or_processing(self):
        card, download, clean, send = self.run_document([{'file_id': 'ready-id', 'caption': 'Verified'}], [True])
        download.assert_not_called()
        clean.assert_not_called()
        self.assertEqual('ready-id', send.call_args.kwargs['reuse_id'])
        card.finish.assert_called_once()

    def test_changed_telegram_identity_can_reuse_matching_source_hash(self):
        _, download, clean, send = self.run_document([None, {'file_id': 'ready-id', 'caption': 'Verified'}], [True])
        download.assert_called_once()
        clean.assert_not_called()
        self.assertEqual('ready-id', send.call_args.kwargs['reuse_id'])

    def test_invalid_cached_id_falls_back_to_normal_cleaning(self):
        _, download, clean, send = self.run_document([{'file_id': 'stale-id', 'caption': 'Verified'}, None], [None, True])
        download.assert_called_once()
        clean.assert_called_once()
        self.assertEqual(2, send.call_count)
        self.assertIsNone(send.call_args.kwargs['reuse_id'])

    def test_network_failure_does_not_trigger_duplicate_fallback_upload(self):
        _, download, clean, send = self.run_document([{'file_id': 'ready-id', 'caption': 'Verified'}], [False])
        download.assert_not_called()
        clean.assert_not_called()
        send.assert_called_once()

    def test_no_subscription_means_no_cached_delivery(self):
        _, download, clean, send = self.run_document([], [], allowed=False)
        download.assert_not_called()
        clean.assert_not_called()
        send.assert_not_called()

    def test_unchanged_source_is_returned_by_its_original_file_id(self):
        _, download, clean, send = self.run_document([None, None], [True], source=ticket('plain'))
        self.assertEqual('source-id', send.call_args.kwargs['reuse_id'])
        download.assert_called_once()
        clean.assert_called_once()

    def test_cache_storage_failure_does_not_fail_delivered_file(self):
        response = Mock()
        response.json.return_value = {'ok': True, 'result': {'document': {'file_id': 'ready-id'}}}
        with patch.object(bot, 'DeliveryCache', side_effect=sqlite3.OperationalError('test')):
            self.assertIsNone(bot._cached_file('key'))
            with patch.object(bot, 'http_session') as session:
                session.return_value.post.return_value = response
                self.assertTrue(bot.send_cleaned(111, b'pdf', 'a.pdf', 'Caption', cache_keys=['key']))


if __name__ == '__main__':
    unittest.main()
