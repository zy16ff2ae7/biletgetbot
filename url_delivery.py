"""Bounded in-memory downloads behind an existing HTTPS reverse proxy.

Only unpredictable, short-lived URLs expose a file. No files or access logs are
written to disk; the listener is restricted to loopback.
"""
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import math
import re
import secrets
import socket
import threading
import time
from urllib.parse import quote, urlsplit
from typing import Optional


PREFIX = '/-bilet-download'
MAX_TTL = 600


@dataclass
class _Stats:
    gets: int = 0
    heads: int = 0
    bytes_sent: int = 0
    first_get_monotonic: Optional[float] = None
    last_get_finished_monotonic: Optional[float] = None


@dataclass
class _Entry:
    data: bytes
    path: str
    name: str
    expires: float
    users: int = 0
    revoked: bool = False
    stats: _Stats = field(default_factory=_Stats)


class Lease:
    """The caller releases access after Telegram has fetched the file."""

    def __init__(self, publisher, token, url, stats):
        self.url = url
        self._publisher = publisher
        self._token = token
        self._stats = stats

    def close(self):
        self._publisher._revoke(self._token)

    def stats(self):
        with self._publisher._lock:
            return asdict(self._stats)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, publisher, address, max_requests):
        self.publisher = publisher
        self._slots = threading.BoundedSemaphore(max_requests)
        self._connections = set()
        self._connection_lock = threading.Lock()
        self._connections_done = threading.Event()
        self._connections_done.set()
        self.request_started = threading.Event()
        super().__init__(address, _Handler)

    def process_request(self, request, client_address):
        request.settimeout(5)
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n'
                                b'Connection: close\r\nCache-Control: no-store\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        with self._connection_lock:
            self._connections.add(request)
            self._connections_done.clear()
        self.request_started.set()
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._connection_lock:
                self._connections.discard(request)
                if not self._connections:
                    self._connections_done.set()
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._connection_lock:
                self._connections.discard(request)
                if not self._connections:
                    self._connections_done.set()
            self._slots.release()

    def service_actions(self):
        # Expiry does not depend on the bot worker returning from a slow API call.
        with self.publisher._lock:
            self.publisher._prune()

    def close_connections(self):
        with self._connection_lock:
            connections = tuple(self._connections)
        for request in connections:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()

    def handle_error(self, _request, _client_address):
        # Unexpected handler errors must not log a private download path.
        pass


class _Handler(BaseHTTPRequestHandler):
    server_version = 'Download'
    sys_version = ''

    def log_message(self, *_args):
        pass

    def _headers(self, status, length=0, **headers):
        self.send_response(status)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'private, no-store, max-age=0')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Robots-Tag', 'noindex, noarchive')
        for name, value in headers.items():
            self.send_header(name.replace('_', '-'), value)
        self.end_headers()

    def do_HEAD(self):
        self._download(head=True)

    def do_GET(self):
        self._download(head=False)

    def _download(self, head):
        publisher = self.server.publisher
        token, entry = publisher._acquire(self.path)
        if entry is None:
            self._headers(404)
            return
        try:
            length = len(entry.data)
            start, end = 0, length - 1
            status = 200
            range_value = self.headers.get('Range')
            extra = {'Accept_Ranges': 'bytes'}
            if range_value:
                span = _range(range_value, length)
                if span is None:
                    self._headers(416, Content_Range=f'bytes */{length}')
                    return
                start, end = span
                status = 206
                extra['Content_Range'] = f'bytes {start}-{end}/{length}'
            # ASCII fallback contains no quoting or header-control characters.
            fallback = re.sub(r'[^A-Za-z0-9._ -]', '_', entry.name)[:120]
            extra.update(Content_Type='application/zip' if entry.name.lower().endswith('.zip')
                         else 'application/pdf',
                         Content_Disposition=f'attachment; filename="{fallback}"; '
                         f"filename*=UTF-8''{quote(entry.name, safe='')}")
            with publisher._lock:
                if head:
                    entry.stats.heads += 1
                else:
                    entry.stats.gets += 1
                    if entry.stats.first_get_monotonic is None:
                        entry.stats.first_get_monotonic = publisher._clock()
            self._headers(status, max(0, end - start + 1), **extra)
            if not head and length:
                # A view avoids a second in-memory copy for a partial download.
                self.wfile.write(memoryview(entry.data)[start:end + 1])
            if not head:
                with publisher._lock:
                    entry.stats.bytes_sent += max(0, end - start + 1)
                    entry.stats.last_get_finished_monotonic = publisher._clock()
        except (OSError, TimeoutError):
            pass
        finally:
            publisher._release(token, entry)


def _range(value, length):
    match = re.fullmatch(r'bytes=([0-9]{0,20})-([0-9]{0,20})', value)
    if not match or not length:
        return None
    first, last = match.groups()
    if not first:
        if not last or int(last) <= 0:
            return None
        return max(0, length - int(last)), length - 1
    start = int(first)
    end = min(int(last), length - 1) if last else length - 1
    return (start, end) if 0 <= start <= end < length else None


def _public_base(value):
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError('Invalid public download base')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError('Invalid public download base') from error
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in (PREFIX, PREFIX + '/')
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*(?::\d+)?', parsed.netloc)
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError('Invalid public download base')
    return 'https://' + parsed.netloc.lower() + PREFIX


class Publisher:
    def __init__(self, public_base, host='127.0.0.1', port=4313, *,
                 max_bytes=64 * 1024 * 1024, max_leases=64, max_requests=8,
                 clock=time.monotonic):
        self.public_base = _public_base(public_base)
        if host != '127.0.0.1':
            raise ValueError('The download listener must use loopback')
        if any(type(value) is not int or value <= 0
               for value in (max_bytes, max_leases, max_requests)):
            raise ValueError('Download limits must be positive integers')
        self.max_bytes, self.max_leases = max_bytes, max_leases
        self._clock = clock
        self._lock = threading.Lock()
        self._entries = {}
        self._bytes = 0
        self._closed = False
        self._server = _Server(self, (host, port), max_requests)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={'poll_interval': 0.1},
                                        name='ticket-downloads', daemon=True)
        self._thread.start()

    def publish(self, data, name, ttl=MAX_TTL):
        if not isinstance(data, bytes):
            raise TypeError('Downloads require immutable bytes')
        if (not isinstance(name, str) or not name or len(name) > 240
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or '/' in name or '\\' in name
                or not name.lower().endswith(('.pdf', '.zip'))):
            raise ValueError('Invalid download filename')
        if (not isinstance(ttl, (int, float)) or isinstance(ttl, bool)
                or not math.isfinite(ttl) or not 0 < ttl <= MAX_TTL):
            raise ValueError('Download lifetime must be at most 600 seconds')
        with self._lock:
            if self._closed:
                raise RuntimeError('Download publisher is closed')
            self._prune()
            if len(self._entries) >= self.max_leases or self._bytes + len(data) > self.max_bytes:
                raise ValueError('Download capacity exceeded')
            token = secrets.token_urlsafe(32)
            path = PREFIX + '/' + token + '/' + quote(name, safe='')
            entry = _Entry(data, path, name, self._clock() + ttl)
            self._entries[token] = entry
            self._bytes += len(data)
        return Lease(self, token, self.public_base + path[len(PREFIX):], entry.stats)

    def _prune(self):
        now = self._clock()
        for token, entry in tuple(self._entries.items()):
            if (entry.revoked or entry.expires <= now) and not entry.users:
                self._bytes -= len(entry.data)
                del self._entries[token]

    def _acquire(self, path):
        with self._lock:
            self._prune()
            parts = path.split('/')
            token = parts[2] if len(parts) == 4 and parts[1] == PREFIX[1:] else ''
            entry = self._entries.get(token)
            if (self._closed or entry is None or entry.path != path
                    or entry.revoked or entry.expires <= self._clock()):
                return None, None
            entry.users += 1
            return token, entry

    def _release(self, _token, entry):
        with self._lock:
            entry.users -= 1
            self._prune()

    def _revoke(self, token):
        with self._lock:
            entry = self._entries.get(token)
            if entry is not None:
                entry.revoked = True
                self._prune()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for entry in self._entries.values():
                entry.revoked = True
            self._prune()
        self._server.shutdown()
        self._server.close_connections()
        self._server._connections_done.wait(2)
        self._server.server_close()
        self._thread.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
