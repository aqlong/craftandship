# monitor/bounded_fetch.py - one bounded HTTP exchange for every outbound request
#
# The `timeout` of requests and of urllib is a limit on ONE socket operation.
# A peer that sends a byte just inside each timeout can hold a request open
# for as long as it likes, and a body is read to its end however large it is
# (and, when it is compressed, however large it inflates). The monitor's
# health loop and the worker's deploy probes send requests to customer apps
# from a single thread, so one app that answers slowly or enormously would
# stall or bloat the process for every other customer.
#
# Every HTTP request that monitor/, queue/, coordinator/, ops/ and
# cli/deploy.py send themselves therefore goes through BoundedSession
# (tests/test_bounded_fetch.py scans those modules for a call that does not).
# Model calls are the exception: they go through the Anthropic SDK, whose
# clients are built with an explicit timeout and retry count where they are
# created. BoundedSession gives one exchange two hard bounds:
#
#   * Time. One monotonic deadline covers the whole exchange: the name
#     lookup, every connection attempt, the TLS handshake, the request, the
#     response headers, the body, and every redirect hop. Each blocking step
#     is armed with the time that is left, not with a fresh timeout. Past the
#     deadline the exchange fails with a requests Timeout and nothing is
#     returned.
#   * Size. At most `max_body_bytes` of DECODED body are kept. The body is
#     streamed, a gzip or deflate body is inflated through a decompressor
#     that is never asked for more than the room that is left, and any other
#     Content-Encoding is refused. At the cap the body is cut, the
#     connection is dropped (not read to its end) and the response is marked
#     `body_truncated`. Redirect responses are bounded the same way.
#
# On a BoundedSession the `timeout` argument of get()/post() is the deadline
# of the whole exchange, in seconds (requests' own meaning, a per-operation
# timeout, is what this module exists to replace).
#
# Two kinds of caller:
#   * probes of customer apps use monitor.ping.guarded_session(), which is a
#     BoundedSession whose connections also refuse non-public addresses;
#   * calls to an API the platform chose (Railway, Resend, the Anthropic cost
#     report) use provider_post() or bounded_urlopen() below.
#
# What is not bounded here: the name lookup runs on a helper thread that is
# abandoned at the deadline (the C resolver cannot be interrupted), so a
# stuck lookup holds one idle daemon thread until the resolver gives up.

from __future__ import annotations

import http.client
import io
import socket
import threading
import time
import urllib.error
import zlib
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
from urllib3.exceptions import HTTPError as _Urllib3Error
from urllib3.exceptions import ReadTimeoutError
from urllib3.util import connection as _urllib3_connection

# Deadline of an exchange whose caller passed no timeout.
DEFAULT_DEADLINE_S = 10.0
# Decoded-body cap of a session that was not given one.
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
# Decoded-body cap for an answer from a provider API (a Railway project list,
# a month of cost buckets). A larger answer is an error, not a truncation.
PROVIDER_MAX_BODY_BYTES = 4 * 1024 * 1024
# Redirect hops one exchange may follow; all of them share its deadline.
MAX_REDIRECTS = 5
# A name lookup made outside an exchange (the browser guard) gets this long.
LOOKUP_DEADLINE_S = 5.0

_CHUNK_BYTES = 16 * 1024

# The deadline (a time.monotonic() value) of the exchange in progress on this
# thread, or unset. BoundedSession.send sets it for the outermost request and
# every layer below consults it: the connection (lookup, connect, handshake,
# request), the response reader (headers, body) and the redirect hops, which
# re-enter send on the same thread.
_exchange = threading.local()


class ResponseTooLarge(requests.exceptions.RequestException):
    """A provider API answered with more than the caller's byte cap."""


def exchange_deadline() -> Optional[float]:
    """The monotonic deadline of the exchange in progress on this thread, or None."""
    return getattr(_exchange, "deadline", None)


def _time_left() -> Optional[float]:
    """Seconds left before the deadline (may be negative), or None outside an exchange."""
    deadline = exchange_deadline()
    return None if deadline is None else deadline - time.monotonic()


def _deadline_seconds(timeout, default: float) -> float:
    """The exchange deadline, in seconds, that a requests `timeout` asks for.

    None means `default`. A (connect, read) pair means their sum: the pair
    bounded two phases separately and the deadline bounds both together.
    """
    if timeout is None:
        return float(default)
    if isinstance(timeout, (tuple, list)):
        parts = [float(part) for part in timeout if part is not None]
        return sum(parts) if parts else float(default)
    return float(timeout)


def _arm(sock) -> None:
    """Give the next blocking operation on `sock` only the time that is left.

    Raises socket.timeout once the deadline has passed. Outside an exchange
    the socket keeps the timeout it has.
    """
    left = _time_left()
    if left is None:
        return
    if left <= 0:
        raise socket.timeout("exchange deadline exceeded")
    current = sock.gettimeout()
    sock.settimeout(left if current is None else min(current, left))


def resolve_bounded(host: str, port, seconds: Optional[float] = None) -> list:
    """socket.getaddrinfo(host, port) that returns or raises within a time limit.

    The limit is `seconds`, else the time left in the exchange in progress,
    else LOOKUP_DEADLINE_S. getaddrinfo itself takes no timeout and cannot be
    interrupted, so it runs on a daemon thread; when the limit passes first
    this raises socket.timeout and the thread is left to finish on its own.
    """
    if seconds is None:
        seconds = _time_left()
    if seconds is None:
        seconds = LOOKUP_DEADLINE_S
    if seconds <= 0:
        raise socket.timeout("exchange deadline exceeded")
    outcome: dict = {}

    def _lookup():
        try:
            outcome["infos"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except BaseException as exc:  # handed to the caller, which decides what it means
            outcome["error"] = exc

    worker = threading.Thread(target=_lookup, name="bounded-lookup", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise socket.timeout(f"name lookup for {host} did not finish in {seconds:.1f}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["infos"]


class _DeadlineReader(io.RawIOBase):
    """Raw reader over a connected socket that stops at the exchange deadline.

    http.client reads the status line, the headers and the body through a
    buffered file over the socket, and each low-level receive waits up to the
    socket timeout. This reader arms the socket with the time left before
    EVERY receive, so the sum of the waits cannot pass the deadline.

    `stock_file` is the file object http.client made for the socket. It is
    never read; it is kept so that closing this reader releases the socket
    the way closing the stock file would.
    """

    def __init__(self, sock, stock_file):
        super().__init__()
        self._sock = sock
        self._stock_file = stock_file

    def readable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._sock.fileno()

    def readinto(self, buffer) -> int:
        _arm(self._sock)
        return self._sock.recv_into(buffer)

    def close(self) -> None:
        if not self.closed:
            try:
                self._stock_file.close()
            finally:
                super().close()


class _DeadlineResponse(http.client.HTTPResponse):
    """HTTPResponse whose every read honors the exchange deadline."""

    def __init__(self, sock, *args, **kwargs):
        super().__init__(sock, *args, **kwargs)
        self.fp = io.BufferedReader(_DeadlineReader(sock, self.fp))


class BoundedConnectionMixin:
    """urllib3 connection whose lookup, connect, handshake, request and
    response reads all draw on the exchange deadline.

    urllib3 calls _new_conn() for the TCP connection and, for https, wraps
    the returned socket in TLS using the host NAME for SNI and for the
    certificate check, so overriding it changes neither. A subclass that
    wants a say in WHERE the connection goes overrides _addresses().
    """

    response_class = _DeadlineResponse

    def _addresses(self) -> list:
        """The addresses to try, in order. Raises OSError when there are none."""
        host = self._dns_host
        lookup_host = host.strip("[]") if host.startswith("[") else host
        found = []
        for info in resolve_bounded(lookup_host, self.port):
            address = str(info[4][0])
            if address not in found:
                found.append(address)
        if not found:
            raise OSError(f"no addresses for {host!r}")
        return found

    def _new_conn(self):
        try:
            addresses = self._addresses()
        except socket.timeout as exc:
            raise ConnectTimeoutError(self, f"Connection to {self.host} timed out: {exc}") from exc
        except NewConnectionError:
            raise
        except OSError as exc:
            raise NewConnectionError(self, f"Failed to resolve {self.host}: {exc}") from exc
        last_error: Optional[OSError] = None
        for address in addresses:
            budget = self.timeout if isinstance(self.timeout, (int, float)) else None
            left = _time_left()
            if left is not None:
                if left <= 0:
                    last_error = socket.timeout("exchange deadline exceeded")
                    break
                budget = left if budget is None else min(budget, left)
            try:
                sock = _urllib3_connection.create_connection(
                    (address, self.port),
                    budget,
                    source_address=self.source_address,
                    socket_options=self.socket_options,
                )
            except OSError as exc:
                last_error = exc
                continue
            # For https the TLS handshake comes next, on this socket: it gets
            # what is left of the exchange, not a fresh connect timeout.
            try:
                _arm(sock)
            except socket.timeout as exc:
                sock.close()
                last_error = exc
                break
            return sock
        if isinstance(last_error, socket.timeout):
            raise ConnectTimeoutError(
                self, f"Connection to {self.host} timed out. (connect timeout={self.timeout})"
            ) from last_error
        raise NewConnectionError(
            self, f"Failed to establish a new connection: {last_error}"
        ) from last_error

    def send(self, data):
        # http.client writes the request line, headers and body through here.
        if self.sock is None:
            self.connect()
        _arm(self.sock)
        return super().send(data)


class BoundedHTTPConnection(BoundedConnectionMixin, HTTPConnection):
    pass


class BoundedHTTPSConnection(BoundedConnectionMixin, HTTPSConnection):
    pass


class BoundedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = BoundedHTTPConnection


class BoundedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = BoundedHTTPSConnection


BOUNDED_POOL_CLASSES = {"http": BoundedHTTPConnectionPool, "https": BoundedHTTPSConnectionPool}

_INFLATABLE = frozenset({"gzip", "x-gzip", "deflate"})


def _read_body(raw, content_encoding: str, max_bytes: int) -> tuple:
    """(decoded body cut at `max_bytes`, truncated?) from a urllib3 response.

    Reads undecoded chunks and inflates gzip/deflate here, asking the
    decompressor for at most the room left plus one byte, so a small
    compressed body cannot become a large one in memory. The read stops at
    the cap; the rest of the body is never requested.
    """
    encoding = (content_encoding or "").strip().lower()
    inflater = None
    if encoding in _INFLATABLE:
        inflater = zlib.decompressobj(32 + zlib.MAX_WBITS)  # gzip or zlib header, detected
    elif encoding not in ("", "identity"):
        raise requests.exceptions.ContentDecodingError(
            f"unsupported Content-Encoding {encoding[:40]!r}")
    body = bytearray()
    compressed_seen = False
    while True:
        chunk = raw.read(_CHUNK_BYTES, decode_content=False)
        if not chunk:
            if compressed_seen and not inflater.eof:
                raise requests.exceptions.ContentDecodingError("compressed body ended early")
            return bytes(body), False
        if inflater is not None:
            compressed_seen = True
            # max_length is the room left plus one byte: one byte over the
            # cap is enough to know the body is too large, and the
            # decompressor produces nothing beyond it.
            chunk = inflater.decompress(chunk, max_bytes - len(body) + 1)
        body += chunk
        if len(body) > max_bytes:
            del body[max_bytes:]
            return bytes(body), True
        if inflater is not None and inflater.eof:
            return bytes(body), False


class BoundedAdapter(HTTPAdapter):
    """requests transport adapter that makes every hop a bounded exchange.

    It is the one place a response passes through before requests looks at
    it (redirect resolution included), so the bounds are applied here: the
    request is streamed whatever the caller asked for, the body is read
    under the deadline and the byte cap, and the result is handed up as an
    ordinary Response whose content is already in memory.
    """

    def __init__(self, *, max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
                 default_deadline_s: float = DEFAULT_DEADLINE_S, pool_classes=None):
        self._max_body_bytes = max(0, int(max_body_bytes))
        self._default_deadline_s = float(default_deadline_s)
        self._pool_classes = dict(pool_classes or BOUNDED_POOL_CLASSES)
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = dict(self._pool_classes)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        # A proxy would open the connection (and resolve the name) itself,
        # outside the bounded connection classes.
        raise requests.exceptions.ProxyError("bounded sessions do not send through a proxy")

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        started_here = exchange_deadline() is None
        if started_here:
            _exchange.deadline = time.monotonic() + _deadline_seconds(timeout, self._default_deadline_s)
        try:
            left = _time_left()
            if left <= 0:
                raise requests.exceptions.ReadTimeout("exchange deadline exceeded", request=request)
            # Ask for an unencoded body. A server that compresses anyway is
            # handled in _read_body.
            request.headers["Accept-Encoding"] = "identity"
            response = super().send(request, stream=True, timeout=left, verify=verify,
                                    cert=cert, proxies=proxies)
            try:
                body, truncated = _read_body(
                    response.raw, response.headers.get("Content-Encoding", ""), self._max_body_bytes)
            except requests.exceptions.RequestException:
                # Raised by _read_body itself (an encoding it will not decode).
                # First, because requests' decoding error is also a urllib3 one.
                response.raw.close()
                raise
            except ReadTimeoutError as exc:
                response.raw.close()
                raise requests.exceptions.ReadTimeout(exc, request=request) from exc
            except socket.timeout as exc:
                response.raw.close()
                raise requests.exceptions.ReadTimeout(exc, request=request) from exc
            except zlib.error as exc:
                response.raw.close()
                raise requests.exceptions.ContentDecodingError(exc) from exc
            except (_Urllib3Error, http.client.HTTPException, OSError) as exc:
                response.raw.close()
                raise requests.exceptions.ConnectionError(exc, request=request) from exc
            if truncated:
                response.raw.close()        # the rest of the body is not wanted
            else:
                response.raw.release_conn()
            response._content = body
            response._content_consumed = True
            response.body_truncated = truncated
            return response
        finally:
            if started_here:
                _exchange.deadline = None


class BoundedSession(requests.Session):
    """A requests.Session on which every request is a bounded exchange.

    `timeout` on get()/post()/request() is the deadline of the whole
    exchange in seconds (None: `default_deadline_s`). `max_body_bytes` caps
    the decoded body of every response; a response that was cut has
    `body_truncated` True. Environment proxy settings, .netrc credentials
    and CA-bundle overrides are ignored (trust_env is off), and at most
    MAX_REDIRECTS redirects are followed inside the one deadline.
    """

    def __init__(self, *, max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
                 default_deadline_s: float = DEFAULT_DEADLINE_S, pool_classes=None):
        super().__init__()
        self.trust_env = False
        self.max_redirects = MAX_REDIRECTS
        self._default_deadline_s = float(default_deadline_s)
        adapter = BoundedAdapter(max_body_bytes=max_body_bytes,
                                 default_deadline_s=default_deadline_s,
                                 pool_classes=pool_classes)
        self.mount("http://", adapter)
        self.mount("https://", adapter)

    def send(self, request, **kwargs):
        if exchange_deadline() is not None:
            # A redirect hop of the exchange in progress: same deadline.
            return super().send(request, **kwargs)
        _exchange.deadline = time.monotonic() + _deadline_seconds(
            kwargs.get("timeout"), self._default_deadline_s)
        try:
            return super().send(request, **kwargs)
        finally:
            _exchange.deadline = None


# ---------------------------------------------------------------------------
# Provider APIs (Railway, Resend, the Anthropic cost report)
# ---------------------------------------------------------------------------

def provider_post(url: str, *, json=None, headers=None, timeout: float,
                  max_body_bytes: int = PROVIDER_MAX_BODY_BYTES) -> requests.Response:
    """POST to an API whose URL the platform chose; returns the Response.

    `timeout` is the deadline of the whole exchange. No redirect is followed
    (a provider API that answers 3xx is answered like any other non-2xx
    status, by the caller). Raises ResponseTooLarge when the answer is
    larger than `max_body_bytes`: half a JSON document is not an answer.
    """
    with BoundedSession(max_body_bytes=max_body_bytes) as session:
        response = session.post(url, json=json, headers=headers, timeout=timeout,
                                allow_redirects=False)
    if response.body_truncated:
        raise ResponseTooLarge(f"response larger than {max_body_bytes} bytes", response=response)
    return response


class _OpenedResponse:
    """What bounded_urlopen returns: the parts of urllib's response its
    callers use (status, headers, read(), use as a context manager)."""

    def __init__(self, response: requests.Response):
        self.status = response.status_code
        self.headers = response.headers
        self._body = io.BytesIO(response.content)

    def read(self, amount: int = -1) -> bytes:
        return self._body.read(amount)

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def bounded_urlopen(request, timeout: float = DEFAULT_DEADLINE_S,
                    max_body_bytes: int = PROVIDER_MAX_BODY_BYTES) -> _OpenedResponse:
    """urllib.request.urlopen for a provider API, as one bounded exchange.

    Takes a urllib.request.Request and reports failure the way urlopen does,
    so a caller written against urllib keeps its error handling:
    urllib.error.HTTPError for a status of 300 or above (no redirect is
    followed), urllib.error.URLError for a transport failure, a timeout or
    an answer larger than `max_body_bytes`. `timeout` is the deadline of the
    whole exchange.
    """
    url = request.full_url
    try:
        with BoundedSession(max_body_bytes=max_body_bytes) as session:
            response = session.request(
                request.get_method(), url, data=request.data,
                headers=dict(request.header_items()), timeout=timeout, allow_redirects=False)
    except requests.exceptions.RequestException as exc:
        raise urllib.error.URLError(exc) from exc
    if response.body_truncated:
        raise urllib.error.URLError(f"response larger than {max_body_bytes} bytes")
    if response.status_code >= 300:
        raise urllib.error.HTTPError(
            url, response.status_code, response.reason or "", response.headers,
            io.BytesIO(response.content))
    return _OpenedResponse(response)
