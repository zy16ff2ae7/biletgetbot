"""Local-only checks for short-lived, private Telegram download URLs."""
import http.client
import socket
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from url_delivery import Publisher, _Handler


class PublisherTests(unittest.TestCase):
    def publisher(self, **kwargs):
        publisher = Publisher('https://tickets.example/-bilet-download', port=0, **kwargs)
        self.addCleanup(publisher.close)
        return publisher

    def request(self, publisher, path, method='GET', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', publisher.port, timeout=2)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_exact_pdf_bytes_head_and_private_headers(self):
        publisher = self.publisher()
        data = b'%PDF-1.4\n' + bytes(range(256)) * 20
        lease = publisher.publish(data, 'Билет "новый".pdf')
        path = urlsplit(lease.url).path
        status, headers, body = self.request(publisher, path)
        self.assertEqual((200, data), (status, body))
        self.assertEqual(str(len(data)), headers['Content-Length'])
        self.assertEqual('application/pdf', headers['Content-Type'])
        self.assertIn("filename*=UTF-8''", headers['Content-Disposition'])
        self.assertNotIn('\r', headers['Content-Disposition'])
        self.assertIn('no-store', headers['Cache-Control'])
        self.assertIn('private', headers['Cache-Control'])
        self.assertEqual('nosniff', headers['X-Content-Type-Options'])
        self.assertEqual('noindex, noarchive', headers['X-Robots-Tag'])
        status, headers, body = self.request(publisher, path, 'HEAD')
        self.assertEqual((200, b''), (status, body))
        self.assertEqual(str(len(data)), headers['Content-Length'])

    def test_zip_and_single_byte_ranges(self):
        publisher = self.publisher()
        lease = publisher.publish(b'0123456789', 'tickets.zip')
        path = urlsplit(lease.url).path
        status, headers, body = self.request(publisher, path, headers={'Range': 'bytes=2-5'})
        self.assertEqual((206, b'2345'), (status, body))
        self.assertEqual('application/zip', headers['Content-Type'])
        self.assertEqual('bytes 2-5/10', headers['Content-Range'])
        self.assertEqual('4', headers['Content-Length'])
        self.assertEqual(b'789', self.request(publisher, path, headers={'Range': 'bytes=-3'})[2])
        self.assertEqual(b'89', self.request(publisher, path, headers={'Range': 'bytes=8-'})[2])
        for value in ('bytes=20-', 'bytes=5-2', 'bytes=0-1,4-5', 'bytes=-0', 'bytes=a-b'):
            with self.subTest(range=value):
                status, headers, body = self.request(publisher, path, headers={'Range': value})
                self.assertEqual((416, b''), (status, body))
                self.assertEqual('bytes */10', headers['Content-Range'])

    def test_expired_or_revoked_lease_cannot_be_downloaded_and_frees_capacity(self):
        now = [100.0]
        publisher = self.publisher(clock=lambda: now[0], max_bytes=10, max_leases=1)
        lease = publisher.publish(b'0123456789', 'a.pdf', ttl=2)
        path = urlsplit(lease.url).path
        now[0] = 102.0
        self.assertEqual(404, self.request(publisher, path)[0])
        replacement = publisher.publish(b'abcdefghij', 'a.pdf')
        self.assertNotEqual(lease.url, replacement.url)
        replacement.close()
        replacement.close()
        self.assertEqual(404, self.request(publisher, urlsplit(replacement.url).path)[0])
        publisher.publish(b'1234567890', 'a.pdf')

    def test_paths_are_exact_and_do_not_offer_listing_or_traversal(self):
        publisher = self.publisher()
        lease = publisher.publish(b'pdf', 'a.pdf')
        path = urlsplit(lease.url).path
        for wrong in ('/', '/-bilet-download', '/-bilet-download/', path + '?x=1',
                      path.replace('/a.pdf', '/b.pdf'), path.replace('/a.pdf', '/..%2Fa.pdf'),
                      '/-bilet-download/unknown/a.pdf', path.replace('/-bilet-download/', '/other/')):
            with self.subTest(path=wrong):
                self.assertEqual((404, b''), self.request(publisher, wrong)[::2])

    def test_unsafe_filename_and_payload_rejected(self):
        publisher = self.publisher()
        for name in ('../a.pdf', 'dir/a.pdf', 'dir\\a.pdf', 'a.pdf\r\nInjected: x',
                     'a.pdf\x00', 'a.txt', '', 'a.pdf\t', 'a.pdf\x7f'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    publisher.publish(b'pdf', name)
        with self.assertRaises(TypeError):
            publisher.publish(bytearray(b'pdf'), 'a.pdf')
        for ttl in (0, -1, 601, float('nan'), float('inf')):
            with self.subTest(ttl=ttl):
                with self.assertRaises(ValueError):
                    publisher.publish(b'pdf', 'a.pdf', ttl=ttl)

    def test_memory_and_lease_limits(self):
        publisher = self.publisher(max_bytes=10, max_leases=2)
        first = publisher.publish(b'123456', 'a.pdf')
        with self.assertRaises(ValueError):
            publisher.publish(b'12345', 'b.pdf')
        second = publisher.publish(b'1234', 'b.pdf')
        with self.assertRaises(ValueError):
            publisher.publish(b'', 'c.pdf')
        first.close()
        publisher.publish(b'123456', 'c.pdf')
        self.assertEqual(b'1234', self.request(publisher, urlsplit(second.url).path)[2])

    def test_active_download_stays_in_memory_budget_after_lease_is_revoked(self):
        publisher = self.publisher(max_bytes=4, max_leases=1)
        lease = publisher.publish(b'pdf!', 'a.pdf')
        started, release = threading.Event(), threading.Event()
        original_headers = _Handler._headers
        received = []
        def headers(handler, status, *args, **kwargs):
            if status == 200:
                started.set()
                release.wait(2)
            original_headers(handler, status, *args, **kwargs)
        with patch.object(_Handler, '_headers', headers):
            client = threading.Thread(target=lambda: received.append(
                self.request(publisher, urlsplit(lease.url).path)))
            client.start()
            try:
                self.assertTrue(started.wait(1))
                lease.close()
                with self.assertRaises(ValueError):
                    publisher.publish(b'new!', 'b.pdf')
            finally:
                release.set()
                client.join(2)
        self.assertFalse(client.is_alive())
        self.assertEqual(b'pdf!', received[0][2])
        publisher.publish(b'new!', 'b.pdf')

    def test_background_expiry_reclaims_memory_without_another_request(self):
        now = [100.0]
        publisher = self.publisher(clock=lambda: now[0], max_bytes=4, max_leases=1)
        publisher.publish(b'pdf!', 'a.pdf', ttl=1)
        now[0] = 101.0
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with publisher._lock:
                if not publisher._entries:
                    self.assertEqual(0, publisher._bytes)
                    break
            threading.Event().wait(0.01)
        else:
            self.fail('Expired bytes were not reclaimed while the bot worker was idle')

    def test_fetch_stats_survive_lease_close_without_retaining_pdf(self):
        now = [100.0]
        publisher = self.publisher(clock=lambda: now[0])
        lease = publisher.publish(b'0123456789', 'a.pdf')
        path = urlsplit(lease.url).path
        self.request(publisher, path, 'HEAD')
        self.request(publisher, path, headers={'Range': 'bytes=2-5'})
        self.request(publisher, path)
        deadline = time.monotonic() + 1
        while lease.stats()['bytes_sent'] != 14 and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertEqual({'heads': 1, 'gets': 2, 'bytes_sent': 14,
                          'first_get_monotonic': 100.0,
                          'last_get_finished_monotonic': 100.0}, lease.stats())
        lease.close()
        self.assertFalse(publisher._entries)
        self.assertEqual(14, lease.stats()['bytes_sent'])

    def test_bounded_request_threads_reject_new_connection(self):
        publisher = self.publisher(max_requests=1)
        connection = socket.create_connection(('127.0.0.1', publisher.port), timeout=2)
        self.addCleanup(connection.close)
        connection.sendall(b'GET / HTTP/1.0\r\n')  # Wait for the rest of the headers.
        self.assertTrue(publisher._server.request_started.wait(1))
        self.assertEqual(503, self.request(publisher, '/')[0])

    def test_close_is_idempotent_cleans_leases_and_stops_listener(self):
        publisher = self.publisher()
        publisher.publish(b'pdf', 'a.pdf')
        publisher.close()
        publisher.close()
        self.assertFalse(publisher._thread.is_alive())
        self.assertFalse(publisher._entries)
        with self.assertRaises(RuntimeError):
            publisher.publish(b'pdf', 'a.pdf')
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', publisher.port), timeout=0.5)

    def test_invalid_public_base_never_starts_listener(self):
        for base in ('http://tickets.example/-bilet-download', 'https://u:p@tickets.example/-bilet-download',
                     'https://tickets.example/-bilet-download?x=1', 'https://tickets.example/other',
                     'https://tickets.example/-bilet-download#fragment', 'https://tickets.example /-bilet-download',
                     'https://tickets.example/-bilet-download/../other', 'https://tickets.example:bad/-bilet-download'):
            with self.subTest(base=base):
                with self.assertRaises(ValueError):
                    Publisher(base, port=0)


if __name__ == '__main__':
    unittest.main()
