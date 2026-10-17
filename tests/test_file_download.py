"""Offline metadata integration, rate-limit handling and bounded downloads."""
import unittest
from unittest.mock import Mock, patch

import requests

from test_processing import bot


class DownloadTests(unittest.TestCase):
    def test_metadata_success_does_not_use_generic_telegram_retry_path(self):
        transport = Mock()
        transport.get_file.return_value = {'file_path': 'documents/a.pdf'}
        with patch.object(bot, 'get_file_transport', return_value=transport), patch.object(bot, 'tg') as tg:
            self.assertEqual('documents/a.pdf', bot.file_metadata('source-id')['file_path'])
        tg.assert_not_called()

    def test_terminal_rejection_has_no_fallback(self):
        for error in (bot.MetadataAPIError(400), bot.MetadataAPIError(403), bot.MetadataUnavailable('failure')):
            with self.subTest(error=type(error).__name__):
                transport = Mock()
                transport.get_file.side_effect = error
                with patch.object(bot, 'get_file_transport', return_value=transport), patch.object(bot, 'tg') as tg:
                    self.assertIsNone(bot.file_metadata('source-id'))
                transport.get_file.assert_called_once()
                tg.assert_not_called()

    def test_rate_limit_waits_full_interval_and_retries_only_metadata(self):
        transport = Mock()
        transport.get_file.side_effect = [bot.MetadataAPIError(429, retry_after=61),
                                          {'file_path': 'documents/a.pdf'}]
        with patch.object(bot, 'get_file_transport', return_value=transport), patch.object(bot.time, 'sleep') as sleep:
            self.assertTrue(bot.file_metadata('source-id'))
        sleep.assert_called_once_with(61)
        self.assertEqual(2, transport.get_file.call_count)

    def response(self, chunks, status=200):
        response = Mock()
        response.status_code = status
        response.iter_content.return_value = iter(chunks)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_exact_bytes_and_separate_metadata_and_body_timing(self):
        response = self.response([b'pdf', b'!'])
        with patch.object(bot, 'file_metadata', return_value={'file_path': 'documents/a.pdf'}), \
                patch.object(bot, 'http_session') as session, self.assertLogs(bot.log, 'INFO') as logs:
            session.return_value.get.return_value = response
            self.assertEqual(b'pdf!', bot.download_file('source-id'))
        self.assertEqual((3, 45), session.return_value.get.call_args.kwargs['timeout'])
        self.assertIn('File body download:', '\n'.join(logs.output))

    def test_oversized_body_is_stopped_before_extra_bytes_are_stored(self):
        response = self.response([b'pdf', b'oversized'])
        with patch.object(bot, 'MAX_BYTES', 4), \
                patch.object(bot, 'file_metadata', return_value={'file_path': 'documents/a.pdf'}), \
                patch.object(bot, 'http_session') as session:
            session.return_value.get.return_value = response
            self.assertIsNone(bot.download_file('source-id'))
        response.__exit__.assert_called_once()

    def test_missing_metadata_never_downloads_body(self):
        with patch.object(bot, 'file_metadata', return_value=None), patch.object(bot, 'http_session') as session:
            self.assertIsNone(bot.download_file('source-id'))
        session.assert_not_called()

    def test_download_error_does_not_log_bot_token(self):
        with patch.object(bot, 'BOT_TOKEN', 'TEST-SECRET'), \
                patch.object(bot, 'file_metadata', return_value={'file_path': 'documents/a.pdf'}), \
                patch.object(bot, 'http_session') as session, self.assertLogs(bot.log, 'WARNING') as logs:
            session.return_value.get.side_effect = requests.Timeout('https://api.telegram.org/file/botTEST-SECRET/documents/a.pdf')
            self.assertIsNone(bot.download_file('source-id'))
        self.assertNotIn('TEST-SECRET', '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
