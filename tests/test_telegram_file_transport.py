"""Deterministic, offline getFile routing and resource limits."""
import json
import threading
import unittest
from unittest.mock import Mock, patch

from telegram_file_transport import (TelegramFileTransport, MetadataAPIError,
                                     MetadataUnavailable, _HTTPSFetcher, EDGES)


class TelegramFileTransportTests(unittest.TestCase):
    def transport(self, request, **kwargs):
        transport = TelegramFileTransport('123:test-secret', transport=request, **kwargs)
        self.addCleanup(transport.close)
        return transport

    def test_fast_primary_never_schedules_hedge(self):
        calls = []
        def request(edge, *_args):
            calls.append(edge)
            return {'file_path': 'documents/a.pdf'}
        transport = self.transport(request)
        self.assertEqual('documents/a.pdf', transport.get_file('private-id')['file_path'])
        self.assertEqual([EDGES[0]], calls)

    def test_slow_primary_hedges_then_prefers_winner_without_waiting_for_loser(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def request(edge, *_args):
            calls.append(edge)
            if edge == EDGES[0]:
                started.set()
                release.wait(2)
            return {'file_path': 'documents/a.pdf'}
        transport = self.transport(request, hedge_after=0.02)
        try:
            self.assertEqual('documents/a.pdf', transport.get_file('one')['file_path'])
            self.assertTrue(started.is_set())
            self.assertFalse(release.is_set())
            self.assertEqual(EDGES[1], transport._preferred)
            transport.get_file('two')
            self.assertEqual([EDGES[0], EDGES[1], EDGES[1]], calls)
        finally:
            release.set()

    def test_primary_failure_uses_other_edge_without_hedge_delay(self):
        calls = []
        def request(edge, *_args):
            calls.append(edge)
            if edge == EDGES[0]:
                raise MetadataUnavailable('Temporary connection failure')
            return {'file_path': 'documents/a.pdf'}
        transport = self.transport(request, hedge_after=10)
        self.assertTrue(transport.get_file('one'))
        self.assertEqual(list(EDGES), calls)

    def test_terminal_api_errors_do_not_trigger_another_edge(self):
        for code in (400, 401, 403, 429):
            with self.subTest(code=code):
                calls = []
                def request(edge, *_args):
                    calls.append(edge)
                    raise MetadataAPIError(code, 'Rejected', retry_after=61 if code == 429 else None)
                transport = self.transport(request, hedge_after=10)
                with self.assertRaises(MetadataAPIError) as caught:
                    transport.get_file('one')
                self.assertEqual(code, caught.exception.error_code)
                self.assertEqual([EDGES[0]], calls)
                if code == 429:
                    self.assertEqual(61, caught.exception.retry_after)
                    with self.assertRaises(MetadataAPIError) as second:
                        transport.get_file('two')
                    self.assertEqual(61, second.exception.retry_after)
                    self.assertEqual([EDGES[0]], calls)

    def test_both_network_failures_return_sanitized_unavailable(self):
        transport = self.transport(Mock(side_effect=OSError('https://bot123:test-secret/private-id')))
        with self.assertRaises(MetadataUnavailable) as caught:
            transport.get_file('private-id')
        self.assertNotIn('test-secret', str(caught.exception))
        self.assertNotIn('private-id', str(caught.exception))

    def test_late_rate_limit_from_loser_still_blocks_future_requests(self):
        release = threading.Event()
        rate_seen = threading.Event()
        calls = []
        def request(edge, *_args):
            calls.append(edge)
            if edge == EDGES[0]:
                release.wait(2)
                rate_seen.set()
                raise MetadataAPIError(429, 'Too many requests', retry_after=61)
            return {'file_path': 'documents/a.pdf'}
        transport = self.transport(request, hedge_after=0.01)
        try:
            self.assertTrue(transport.get_file('one'))
            release.set()
            self.assertTrue(rate_seen.wait(1))
            with transport._condition:
                self.assertTrue(transport._condition.wait_for(lambda: transport._cooldown_until > 0, timeout=1))
            with self.assertRaises(MetadataAPIError) as caught:
                transport.get_file('two')
            self.assertEqual(429, caught.exception.error_code)
            self.assertEqual(61, caught.exception.retry_after)
            self.assertEqual(list(EDGES), calls)
        finally:
            release.set()

    def test_deadline_does_not_spawn_extra_workers_or_leave_queued_work(self):
        release = threading.Event()
        calls = []
        def request(edge, *_args):
            calls.append(edge)
            release.wait(2)
            return {'file_path': 'documents/a.pdf'}
        transport = self.transport(request, hedge_after=0, max_wait=0.02)
        try:
            with self.assertRaises(MetadataUnavailable):
                transport.get_file('one')
            self.assertEqual(2, len(transport._workers))
            self.assertEqual(2, len(calls))
            self.assertFalse(transport._jobs)
        finally:
            release.set()

    def test_concurrent_callers_never_exceed_two_active_requests(self):
        release = threading.Event()
        active = maximum = 0
        lock = threading.Lock()
        two_started = threading.Event()
        results = []
        def request(edge, *_args):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    two_started.set()
            release.wait(2)
            with lock:
                active -= 1
            return {'file_path': 'documents/a.pdf'}
        transport = self.transport(request, hedge_after=0)
        callers = [threading.Thread(target=lambda: results.append(transport.get_file('one')))
                   for _ in range(6)]
        try:
            for caller in callers:
                caller.start()
            self.assertTrue(two_started.wait(1))
            self.assertEqual(2, len(transport._workers))
        finally:
            release.set()
            for caller in callers:
                caller.join(2)
        self.assertEqual(6, len(results))
        self.assertEqual(2, maximum)
        self.assertTrue(all(not caller.is_alive() for caller in callers))

    def test_close_wakes_pending_caller_and_prevents_new_work(self):
        entered, release = threading.Event(), threading.Event()
        failures = []
        def request(*_args):
            entered.set()
            release.wait(2)
            return {'file_path': 'documents/a.pdf'}
        transport = self.transport(request, hedge_after=10)
        def call():
            try:
                transport.get_file('one')
            except MetadataUnavailable:
                failures.append(True)
        caller = threading.Thread(target=call)
        caller.start()
        self.assertTrue(entered.wait(1))
        transport.close()
        caller.join(1)
        release.set()
        self.assertEqual([True], failures)
        with self.assertRaises(MetadataUnavailable):
            transport.get_file('two')

    def test_http_pool_checks_original_host_certificate_and_sni(self):
        pool = Mock()
        pool.request.return_value.status = 200
        pool.request.return_value.read.return_value = b'{"ok":true,"result":{"file_path":"documents/a.pdf"}}'
        with patch('telegram_file_transport.urllib3.HTTPSConnectionPool', return_value=pool) as factory:
            fetch = _HTTPSFetcher()
            self.addCleanup(fetch.close)
            self.assertTrue(fetch(EDGES[0], '123:test-secret', 'private-id', (3, 35)))
            for call in factory.call_args_list:
                self.assertEqual('api.telegram.org', call.kwargs['assert_hostname'])
                self.assertEqual('api.telegram.org', call.kwargs['server_hostname'])
                self.assertEqual('CERT_REQUIRED', call.kwargs['cert_reqs'])
                self.assertEqual(1, call.kwargs['maxsize'])
            kwargs = pool.request.call_args.kwargs
            self.assertFalse(kwargs['retries'])
            self.assertFalse(kwargs['redirect'])
            self.assertEqual('api.telegram.org', kwargs['headers']['Host'])
            pool.request.return_value.release_conn.assert_called_once()

    def test_http_errors_keep_full_retry_after_and_redact_secrets(self):
        pool = Mock()
        pool.request.return_value.status = 429
        pool.request.return_value.read.return_value = (
            b'{"ok":false,"error_code":429,"description":"123:test-secret private-id",'
            b'"parameters":{"retry_after":61}}')
        with patch('telegram_file_transport.urllib3.HTTPSConnectionPool', return_value=pool):
            fetch = _HTTPSFetcher()
            self.addCleanup(fetch.close)
            with self.assertRaises(MetadataAPIError) as caught:
                fetch(EDGES[0], '123:test-secret', 'private-id', (3, 35))
        self.assertEqual(61, caught.exception.retry_after)
        self.assertNotIn('test-secret', str(caught.exception))
        self.assertNotIn('private-id', str(caught.exception))

    def test_http_rate_limit_with_non_json_body_is_never_network_fallback(self):
        pool = Mock()
        pool.request.return_value.status = 429
        pool.request.return_value.headers = {'Retry-After': '61'}
        pool.request.return_value.read.return_value = b'<html>Rate limit</html>'
        with patch('telegram_file_transport.urllib3.HTTPSConnectionPool', return_value=pool):
            fetch = _HTTPSFetcher()
            self.addCleanup(fetch.close)
            with self.assertRaises(MetadataAPIError) as caught:
                fetch(EDGES[0], '123:test-secret', 'private-id', (3, 35))
        self.assertEqual((429, 61), (caught.exception.error_code, caught.exception.retry_after))

    def test_json_rate_limit_uses_full_header_delay_when_absent_or_longer(self):
        for parameters, expected in ((None, 61), ({'retry_after': 30}, 61), ({'retry_after': 70}, 70)):
            with self.subTest(parameters=parameters):
                pool = Mock()
                pool.request.return_value.status = 429
                pool.request.return_value.headers = {'Retry-After': '61'}
                packet = {'ok': False, 'error_code': 429, 'description': 'Too many requests'}
                if parameters is not None:
                    packet['parameters'] = parameters
                pool.request.return_value.read.return_value = json.dumps(packet).encode()
                with patch('telegram_file_transport.urllib3.HTTPSConnectionPool', return_value=pool):
                    fetch = _HTTPSFetcher()
                    self.addCleanup(fetch.close)
                    with self.assertRaises(MetadataAPIError) as caught:
                        fetch(EDGES[0], '123:test-secret', 'private-id', (3, 35))
                self.assertEqual(expected, caught.exception.retry_after)

    def test_http_rate_limit_with_invalid_json_shape_or_oversize_is_terminal(self):
        for body in (b'[]', b'x' * (65536 + 1)):
            with self.subTest(size=len(body)):
                pool = Mock()
                pool.request.return_value.status = 429
                pool.request.return_value.headers = {'Retry-After': '61'}
                pool.request.return_value.read.return_value = body
                with patch('telegram_file_transport.urllib3.HTTPSConnectionPool', return_value=pool):
                    fetch = _HTTPSFetcher()
                    self.addCleanup(fetch.close)
                    with self.assertRaises(MetadataAPIError) as caught:
                        fetch(EDGES[0], '123:test-secret', 'private-id', (3, 35))
                self.assertEqual((429, 61), (caught.exception.error_code, caught.exception.retry_after))


if __name__ == '__main__':
    unittest.main()
