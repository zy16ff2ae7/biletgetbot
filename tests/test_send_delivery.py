"""Offline first-file delivery, uncertainty and private-link lifecycle checks."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests

from test_processing import bot


class SendDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.publisher = Mock()
        self.lease = self.publisher.publish.return_value
        self.lease.url = 'https://tickets.example/-bilet-download/SECRET/a.pdf'
        self.lease.stats.return_value = {'gets': 1, 'bytes_sent': 4}
        self.response = Mock()
        self.response.json.return_value = {'ok': True, 'result': {'document': {'file_id': 'ready-id'}}}
        self.session = Mock()
        self.session.post.return_value = self.response
        for p in (patch.object(bot, 'STATE_DIR', Path(self.tmp.name)),
                  patch.object(bot, 'get_publisher', return_value=self.publisher),
                  patch.object(bot, 'http_session', return_value=self.session)):
            p.start()
            self.addCleanup(p.stop)

    def send(self, **kwargs):
        return bot.send_cleaned(111, b'pdf!', 'a.pdf', 'Verified', cache_keys=['key'], **kwargs)

    def test_first_file_uses_url_without_multipart_and_remembers_native_id(self):
        self.assertTrue(self.send())
        self.publisher.publish.assert_called_once_with(b'pdf!', 'a.pdf')
        kwargs = self.session.post.call_args.kwargs
        self.assertNotIn('files', kwargs)
        self.assertEqual(self.lease.url, kwargs['json']['document'])
        self.assertEqual('ready-id', bot._cached_file('key')['file_id'])
        self.lease.close.assert_called_once()

    def test_file_id_never_creates_or_uploads_lease(self):
        self.assertTrue(self.send(reuse_id='existing-id'))
        self.publisher.publish.assert_not_called()
        self.assertEqual('existing-id', self.session.post.call_args.kwargs['json']['document'])
        self.lease.close.assert_not_called()

    def test_explicit_url_rejection_falls_back_to_multipart_once(self):
        self.response.json.side_effect = [
            {'ok': False, 'error_code': 400, 'description': 'Bad Request: failed to get HTTP URL content'},
            {'ok': True, 'result': {'document': {'file_id': 'upload-id'}}}]
        self.assertTrue(self.send())
        self.assertEqual(2, self.session.post.call_count)
        fallback = self.session.post.call_args.kwargs
        self.assertNotIn('document', fallback['data'])
        self.assertEqual(b'pdf!', fallback['files']['document'][1].getvalue())
        self.assertEqual('upload-id', bot._cached_file('key')['file_id'])
        # Idempotent lease revocation is permitted after a confirmed fetch rejection.
        self.lease.close.assert_called()

    def test_unacknowledged_url_send_does_not_retry_or_revoke_early(self):
        self.session.post.side_effect = requests.ReadTimeout('unacknowledged ' + self.lease.url)
        with self.assertLogs(bot.log, level='WARNING') as logs:
            self.assertFalse(self.send())
        self.session.post.assert_called_once()
        self.lease.close.assert_not_called()
        self.assertIsNone(bot._cached_file('key'))
        self.assertNotIn('SECRET', '\n'.join(logs.output))

    def test_non_json_response_is_uncertain_and_does_not_duplicate(self):
        self.response.json.side_effect = ValueError('upstream response')
        self.assertFalse(self.send())
        self.session.post.assert_called_once()
        self.lease.close.assert_not_called()

    def test_429_honors_full_wait_before_retrying_same_url(self):
        self.response.json.side_effect = [
            {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 61}},
            {'ok': True}]
        with patch.object(bot.time, 'sleep') as sleep:
            self.assertTrue(self.send())
        sleep.assert_called_once_with(61)
        self.publisher.publish.assert_called_once()
        self.lease.close.assert_called_once()

    def test_forbidden_response_revokes_without_fallback(self):
        self.response.json.return_value = {'ok': False, 'error_code': 403, 'description': 'Forbidden'}
        self.assertFalse(self.send())
        self.session.post.assert_called_once()
        self.lease.close.assert_called_once()

    def test_server_error_keeps_lease_until_expiry_without_retry(self):
        self.response.json.return_value = {'ok': False, 'error_code': 500}
        self.assertFalse(self.send())
        self.session.post.assert_called_once()
        self.lease.close.assert_not_called()

    def test_gateway_capacity_failure_uses_multipart(self):
        self.publisher.publish.side_effect = ValueError('capacity')
        self.assertTrue(self.send())
        self.assertIn('files', self.session.post.call_args.kwargs)
        self.lease.close.assert_not_called()

    def test_large_zip_retains_direct_upload_support(self):
        with patch.object(bot, 'MAX_BYTES', 3):
            self.assertTrue(bot.send_cleaned(111, b'zip!', 'batch.zip', 'Verified'))
        self.publisher.publish.assert_not_called()
        self.assertEqual('application/zip', self.session.post.call_args.kwargs['files']['document'][2])

    def test_api_description_and_token_are_redacted(self):
        self.response.json.return_value = {
            'ok': False, 'error_code': 400, 'description': 'Bad Request: wrong HTTP URL ' + self.lease.url}
        with patch.object(bot, 'BOT_TOKEN', 'SENSITIVE'), self.assertLogs(bot.log, level='WARNING') as logs:
            self.assertFalse(self.send())
            safe = bot._safe_error('https://api.telegram.org/botSENSITIVE/sendDocument ' + self.lease.url)
        self.assertNotIn('SECRET', '\n'.join(logs.output))
        self.assertNotIn('SENSITIVE', safe)
        self.assertNotIn('SECRET', safe)
        self.assertNotIn('SECRET', bot._safe_error('failed /-bilet-download/SECRET/a.pdf'))


if __name__ == '__main__':
    unittest.main()
