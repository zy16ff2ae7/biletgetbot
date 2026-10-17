"""Bounded hedging only for read-only Telegram getFile metadata requests.

Polling, payments and message delivery never use this transport. Two fixed
workers have one slot each; a slow losing request cannot create more workers or
an unbounded work queue. HTTPS verifies Telegram's hostname on both edges.
"""
from dataclasses import dataclass, field
import json
import math
import re
import threading
import time
from typing import Optional

import requests
import urllib3


API_HOST = 'api.telegram.org'
EDGES = ('149.154.166.110', '149.154.167.220')
MAX_METADATA_BYTES = 64 * 1024


class MetadataUnavailable(Exception):
    """A bounded network or server failure when obtaining file metadata."""


class MetadataAPIError(Exception):
    """A semantic API rejection: do not retry through another endpoint."""

    def __init__(self, error_code, description='Telegram rejected file metadata', retry_after=None):
        self.error_code = error_code
        self.description = description
        self.retry_after = (retry_after if type(retry_after) is int and retry_after > 0 else None)
        super().__init__(f'Telegram getFile error {error_code}: {description}')


def _redact(value, token, file_id):
    description = str(value).replace(token, '<token>').replace(file_id, '<file>')
    return re.sub(r'https?://\S+', '<url>', description)[:300]


def _header_retry(response):
    value = response.headers.get('Retry-After')
    return int(value) if isinstance(value, str) and re.fullmatch(r'[0-9]{1,9}', value) else None


class _HTTPSFetcher:
    def __init__(self):
        self._pools = {
            edge: urllib3.HTTPSConnectionPool(
                edge, port=443, cert_reqs='CERT_REQUIRED', ca_certs=requests.certs.where(),
                assert_hostname=API_HOST, server_hostname=API_HOST, maxsize=1, block=True)
            for edge in EDGES
        }

    def __call__(self, edge, token, file_id, timeout):
        response = None
        try:
            response = self._pools[edge].request(
                'POST', f'/bot{token}/getFile',
                body=json.dumps({'file_id': file_id}).encode('utf-8'),
                headers={'Host': API_HOST, 'Content-Type': 'application/json'},
                timeout=urllib3.Timeout(connect=timeout[0], read=timeout[1]),
                pool_timeout=1, retries=False, redirect=False, preload_content=False)
            raw = response.read(MAX_METADATA_BYTES + 1, decode_content=True)
            if len(raw) > MAX_METADATA_BYTES:
                response.close()
                if 400 <= response.status < 500:
                    raise MetadataAPIError(response.status, 'Telegram rejected file metadata',
                                           _header_retry(response))
                raise MetadataUnavailable('Telegram file metadata response is too large')
            try:
                packet = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                if 400 <= response.status < 500:
                    raise MetadataAPIError(response.status, 'Telegram rejected file metadata',
                                           _header_retry(response)) from None
                raise MetadataUnavailable('Invalid Telegram file metadata response') from None
            if not isinstance(packet, dict):
                if 400 <= response.status < 500:
                    raise MetadataAPIError(response.status, 'Telegram rejected file metadata',
                                           _header_retry(response))
                raise MetadataUnavailable('Invalid Telegram file metadata response')
            if response.status == 200 and packet.get('ok') is True:
                result = packet.get('result')
                if isinstance(result, dict) and isinstance(result.get('file_path'), str) and result['file_path']:
                    return result
                raise MetadataUnavailable('Telegram file metadata has no download path')
            code = packet.get('error_code', response.status)
            code = code if type(code) is int else response.status
            if 400 <= response.status < 500 and not 400 <= code < 500:
                code = response.status
            if 400 <= code < 500:
                parameters = packet.get('parameters')
                json_retry = parameters.get('retry_after') if isinstance(parameters, dict) else None
                delays = [delay for delay in (json_retry, _header_retry(response))
                          if type(delay) is int and delay > 0]
                retry_after = max(delays) if delays else None
                raise MetadataAPIError(code, _redact(packet.get('description', 'Rejected'), token, file_id),
                                       retry_after=retry_after)
            raise MetadataUnavailable('Telegram file metadata server is unavailable')
        except (MetadataAPIError, MetadataUnavailable):
            raise
        except Exception:
            # urllib3 errors can include the token-bearing request path.
            raise MetadataUnavailable('Telegram file metadata request failed') from None
        finally:
            if response is not None:
                try:
                    response.release_conn()
                except Exception:
                    # Cleanup cannot turn a known API rejection into a retriable error.
                    pass

    def close(self):
        for pool in self._pools.values():
            pool.close()


@dataclass
class _Call:
    file_id: str
    launched: set = field(default_factory=set)
    outcomes: list = field(default_factory=list)
    started: Optional[float] = None
    done: bool = False


class TelegramFileTransport:
    def __init__(self, token, *, hedge_after=1.0, max_wait=45.0, transport=None,
                 clock=time.monotonic):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9:_-]+', token):
            raise ValueError('Invalid Telegram token')
        if (not isinstance(hedge_after, (int, float)) or not math.isfinite(hedge_after)
                or hedge_after < 0 or not isinstance(max_wait, (int, float))
                or not math.isfinite(max_wait) or max_wait <= 0):
            raise ValueError('Invalid metadata timing limits')
        self._token = token
        self._fetch = transport if transport is not None else _HTTPSFetcher()
        self._owns_fetch = transport is None
        self._clock = clock
        self._hedge_after, self._max_wait = hedge_after, max_wait
        self._condition = threading.Condition()
        self._busy = set()
        self._jobs = {}
        self._preferred = EDGES[0]
        self._cooldown_until = 0.0
        self._closed = False
        self._workers = [threading.Thread(target=self._worker, args=(edge,),
                                          name=f'telegram-file-{index}', daemon=True)
                         for index, edge in enumerate(EDGES)]
        for worker in self._workers:
            worker.start()

    def _start(self, call, edge, timeout):
        # Caller holds the condition. The map contains only the two worker slots.
        if edge in self._busy or edge in call.launched:
            return False
        self._busy.add(edge)
        call.launched.add(edge)
        call.started = call.started if call.started is not None else self._clock()
        self._jobs[edge] = (call, timeout)
        self._condition.notify_all()
        return True

    def _worker(self, edge):
        while True:
            with self._condition:
                while not self._closed and edge not in self._jobs:
                    self._condition.wait()
                if self._closed:
                    return
                call, timeout = self._jobs.pop(edge)
                cooldown = max(0, self._cooldown_until - self._clock())
                skip = call.done
            result = error = None
            try:
                if cooldown:
                    raise MetadataAPIError(429, 'Telegram requested a cooldown', math.ceil(cooldown))
                if not skip:
                    result = self._fetch(edge, self._token, call.file_id, timeout)
                    if not isinstance(result, dict) or not isinstance(result.get('file_path'), str) or not result['file_path']:
                        raise MetadataUnavailable('Telegram file metadata has no download path')
            except MetadataAPIError as failure:
                error = MetadataAPIError(failure.error_code,
                                         _redact(failure.description, self._token, call.file_id),
                                         failure.retry_after)
            except Exception:
                error = MetadataUnavailable('Telegram file metadata request failed')
            with self._condition:
                if isinstance(error, MetadataAPIError) and error.error_code == 429:
                    self._cooldown_until = max(self._cooldown_until,
                                               self._clock() + (error.retry_after or 1))
                if not call.done:
                    call.outcomes.append((edge, result, error))
                self._busy.discard(edge)
                self._condition.notify_all()

    def get_file(self, file_id):
        if (not isinstance(file_id, str) or not file_id or len(file_id) > 4096
                or any(ord(char) < 32 for char in file_id)):
            raise ValueError('Invalid Telegram file identifier')
        call = _Call(file_id)
        deadline = self._clock() + self._max_wait
        try:
            with self._condition:
                while True:
                    if self._closed:
                        raise MetadataUnavailable('Telegram file metadata transport is closed')
                    # Once a semantic error is known, never evade it through the hedge.
                    for _edge, _result, error in call.outcomes:
                        if isinstance(error, MetadataAPIError):
                            raise error
                    for edge, result, error in call.outcomes:
                        if error is None and result is not None:
                            self._preferred = edge
                            return result
                    now = self._clock()
                    if now < self._cooldown_until:
                        raise MetadataAPIError(429, 'Telegram requested a cooldown',
                                               math.ceil(self._cooldown_until - now))
                    if len(call.outcomes) == len(EDGES):
                        raise MetadataUnavailable('Both Telegram file metadata endpoints failed')
                    if now >= deadline:
                        raise MetadataUnavailable('Telegram file metadata deadline exceeded')
                    order = (self._preferred,) + tuple(edge for edge in EDGES if edge != self._preferred)
                    if not call.launched:
                        for edge in order:
                            if self._start(call, edge, (3, 35)):
                                break
                    # A quick transient failure skips the delay; a healthy primary
                    # has time to finish without generating a second API request.
                    hedge_due = (call.started is not None and
                                 (bool(call.outcomes) or now >= call.started + self._hedge_after))
                    if hedge_due and len(call.launched) < len(EDGES):
                        for edge in order:
                            if self._start(call, edge, (3, 12)):
                                break
                    delay = deadline - now
                    if call.started is not None and not hedge_due:
                        delay = min(delay, max(0.001, call.started + self._hedge_after - now))
                    self._condition.wait(delay)
        finally:
            with self._condition:
                call.done = True
                self._condition.notify_all()

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._jobs.clear()
            self._condition.notify_all()
        if self._owns_fetch:
            self._fetch.close()
        # Requests have bounded timeouts. Closing must not wait on a slow loser.
        for worker in self._workers:
            worker.join(timeout=0.1)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
