"""compose.lint_external_urls: pre-deploy dead-link check for external URLs.

Walks the resolved spec for URL-bearing fields (cta_href, image_url,
amazon_url, social_*_url, _harbor_meta.copy_source_url, etc.), filters to
absolute http(s) URLs, and HEAD-pings each one. Any response that's a 4xx
(or a clearly broken host) is returned as a warning, which build_app
records as an engine warning, so an SE reviewer sees the dead link before
the build ships.

Why it exists: retailer product links go stale. A product that is listed
again under a new id leaves the old URL answering 404, and a site that
links to many of them (a book catalog, for one) is the most exposed. The
same check catches any external URL pasted during intake that has died by
the time of the build.

Config knob: CAS_LINT_EXTERNAL_URLS env var, default "warn".
  * "skip"  → no linting; emit no warnings (test / offline mode).
  * "warn"  → HEAD-ping all external URLs; emit warnings for failures.
  * "error" → like "warn", and raise ExternalUrlLintError if any URL fails.

The linter is fail-open: network errors against the linter itself (DNS
outage, sandbox blocks egress, etc.) collapse to a single warning, never a
hard build failure. The point is to surface customer-visible 404s, not to
gate builds on the linter's own network reach.

Server-side request forgery
===========================
This module makes the BUILD WORKER send HTTP requests to URLs that come out
of a customer conversation, so every URL is hostile input. The defenses:

  * Only http/https, default ports, no embedded credentials, bounded length
    (`_url_precheck`).
  * Every TCP connection the checker opens goes through
    `_guarded_create_connection`, which resolves the host and refuses to
    connect unless EVERY resolved address is a public unicast address. The
    check is made on the address actually connected to, in the same call,
    so there is no window for a DNS answer to change between "check" and
    "use" (DNS rebinding), and it applies to redirect targets too because
    they open connections the same way.
  * Redirects: at most `MAX_REDIRECTS`, and every redirect target passes
    the same `_url_precheck` as the first URL (http/https, default ports,
    no credentials), because the remote server chooses it.
  * Environment proxy settings are ignored (a proxy would connect on the
    checker's behalf and the address check would be made on the proxy).
  * Response bodies are never read, including the body of a redirect
    response (the stock urllib handler reads that one in full); only the
    status line and headers are.
  * At most `MAX_URLS` URLs and `TOTAL_BUDGET_SECONDS` of wall time per
    build, so a spec cannot stall the worker with thousands of slow hosts.
    The deadline is enforced inside a check, not only between checks:
    every connect and every read of response headers is given no more
    than the time left, and stops when none is left, so one host that
    trickles its answer cannot run past it.

What it does NOT do: it still lets a spec make the worker send one HEAD
(and possibly one GET) to arbitrary public hosts, and the status code of
each request ends up in the build's warning list. The time budget does not
cover the name lookup (`socket.getaddrinfo` has no timeout of its own; the
system resolver bounds it) and an operation already in flight when the
deadline passes (a TLS handshake or the request send) still runs to its own
per-request timeout, so a build can overrun the budget by about one lookup
plus one such operation.
"""

from __future__ import annotations

import http.client
import io
import ipaddress
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple


class ExternalUrlLintError(RuntimeError):
    """Raised when CAS_LINT_EXTERNAL_URLS=error and any URL fails."""


# Field names whose values are URLs. Used to short-circuit walking so we
# only ping actual URL fields, not free-text bodies. Extended over time as
# new archetypes / data_models add URL columns.
_URL_FIELD_NAMES: Set[str] = {
    # cta / button targets
    "cta_href",
    "cta_url",
    "cta_primary",
    "href",
    "url",
    "link",
    "external_url",
    "registration_url",
    "rsvp_url",
    "booking_url",
    "buy_url",
    "watch_url",
    "listen_url",
    # images / media
    "image_url",
    "photo_url",
    "avatar_url",
    "cover_url",
    "cover_image_url",
    "thumbnail_url",
    "logo_url",
    "hero_image_url",
    "nav_logo_url",
    "about_photo_url",
    "favicon_url",
    "og_image_url",
    "video_url",
    "audio_url",
    # retailer / book links (the original driver: ASIN churn)
    "amazon_url",
    "goodreads_url",
    "local_bookstore_url",
    "barnesnoble_url",
    "netgalley_url",
    "audible_url",
    "apple_books_url",
    "kobo_url",
    # social
    "instagram_url",
    "facebook_url",
    "twitter_url",
    "x_url",
    "linkedin_url",
    "youtube_url",
    "tiktok_url",
    "pinterest_url",
    "spotify_url",
    "soundcloud_url",
    "vimeo_url",
    # harbor source crawl
    "copy_source_url",
    "source_url",
    "site_url",
}


def _is_external_http_url(value: Any) -> bool:
    """True iff `value` is a string that's an absolute http(s) URL."""
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return False
    return s.startswith("http://") or s.startswith("https://")


def _extract_from_node(
    node: Any,
    path: str,
    out: List[Tuple[str, str]],
) -> None:
    """Walk a spec subtree, appending (field_path, url) pairs to `out`.

    Only emits URLs found in fields whose name is in _URL_FIELD_NAMES.
    This avoids HEAD-pinging URLs that appear in free-text bodies (which
    we don't render as live links anyway).

    Iterative, in document order: the spec is JSON that a model wrote, so
    a nesting a recursive walk cannot survive is a spec, not a bug. A
    position is kept as a chain of labels and written out only for a URL
    that is found, so the cost of the walk does not grow with the depth.
    A container met a second time is not walked again (a value built in
    memory can refer to itself; JSON cannot).
    """
    def render(where: Optional[Tuple[str, Any]], leaf: str) -> str:
        labels = [leaf]
        while where is not None:
            label, where = where
            labels.append(label)
        text = "".join(reversed(labels))
        # A key directly under an empty path has no dot in front of it.
        return text[1:] if not path and text.startswith(".") else text

    # Entries are ("node", container, position) or ("hit", position, url).
    stack: List[Tuple[str, Any, Any]] = [("node", node, (path, None))]
    seen_containers: Set[int] = set()
    while stack:
        kind, first, second = stack.pop()
        if kind == "hit":
            out.append((first, second))
            continue
        if id(first) in seen_containers:
            continue
        if isinstance(first, dict):
            seen_containers.add(id(first))
            entries: List[Tuple[str, Any, Any]] = []
            for key, val in first.items():
                if isinstance(val, str):
                    if key in _URL_FIELD_NAMES and _is_external_http_url(val):
                        entries.append(("hit", render(second, f".{key}"), val.strip()))
                elif isinstance(val, (dict, list)):
                    entries.append(("node", val, (f".{key}", second)))
            # Reversed, so that the first entry is the next one popped.
            stack.extend(reversed(entries))
        elif isinstance(first, list):
            seen_containers.add(id(first))
            stack.extend(
                ("node", item, (f"[{index}]", second))
                for index, item in reversed(list(enumerate(first)))
            )


def extract_external_urls(spec: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return a deduped list of (field_path, url) tuples from the spec.

    Pure function. No network. Safe to call in unit tests.

    Dedupes by URL, if the same URL appears in 5 different fields, we
    only HEAD-ping it once. First-seen field_path is kept for the
    warning detail.
    """
    raw: List[Tuple[str, str]] = []
    _extract_from_node(spec, "", raw)
    seen: Set[str] = set()
    deduped: List[Tuple[str, str]] = []
    for path, url in raw:
        if url in seen:
            continue
        seen.add(url)
        deduped.append((path, url))
    return deduped


def check_url(url: str, timeout: float = 5.0) -> Dict[str, Any]:
    """HEAD-ping `url` (with GET fallback). Return a result dict.

    Result shape:
      {
        "url": str,
        "ok": bool,           # True iff HTTP 2xx/3xx (treat 3xx as healthy)
        "status": int|None,   # HTTP status code, or None on network error
        "error": str|None,    # short error label when ok=False
      }

    Strategy:
      1. HEAD request first (cheap, no body).
      2. If 405 Method Not Allowed (Amazon, Cloudflare, some CDNs), retry GET.
      3. 4xx → ok=False, status set.
      4. 5xx → ok=False (treated as broken from the customer's perspective).
      5. Network error (DNS, timeout, refused) → ok=False, status=None.

    Inside `lint_external_urls` the build's time budget applies: when it
    runs out before or during this check, the result carries
    `"unchecked": True` (the URL was not judged, which is different from
    judged dead).
    """
    problem = _url_precheck(url)
    if problem:
        return {"url": url, "ok": False, "status": None, "error": f"not checked: {problem}"}
    return _check_url_via(url, timeout, opener=_guarded_open)


# ---------------------------------------------------------------------------
# SSRF guard (see the module docstring)
# ---------------------------------------------------------------------------

MAX_URLS = 40
MAX_URL_LENGTH = 2000
MAX_REDIRECTS = 3
TOTAL_BUDGET_SECONDS = 60.0

# NAT64 well-known prefix: an IPv6 address in it is a wrapper around an
# IPv4 address, which ipaddress.is_global does not look inside.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class BlockedDestinationError(OSError):
    """The URL's host resolves to an address the checker will not contact.

    The message names the host and never the address. It becomes part of a
    build warning (`_check_url_via` puts the exception text into the
    result), the warning goes into the build report, and the worker copies
    report lines into the build log, which the site's owner can read: what
    a name resolves to inside the worker's network is not theirs to learn.
    The refused address stays on the exception (`address`) for a caller
    that needs it; nothing in this module prints it.
    """

    def __init__(self, host: Any, address: Any):
        super().__init__(f"{host} resolves to a non-public address")
        self.host = host
        self.address = address


def _is_public_address(ip: str) -> bool:
    """True only for a public unicast address.

    Refuses loopback, RFC 1918 / unique-local, link-local (which includes
    the 169.254.169.254 cloud metadata address), carrier-grade NAT,
    multicast, unspecified, documentation and reserved ranges, and any IPv6
    form that embeds an IPv4 address (v4-mapped, 6to4, Teredo, NAT64).
    """
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return _is_public_address(str(addr.ipv4_mapped))
        if addr.sixtofour is not None or addr.teredo is not None or addr in _NAT64_PREFIX:
            return False
    return bool(addr.is_global) and not addr.is_multicast


def is_public_address(ip: str) -> bool:
    """The address policy above, for callers outside this module.

    monitor.ping.probe_url_problem (health probes of deployed apps, and the
    browser the worker drives for deploy screenshots) uses it so the repo has
    one definition of "an address the worker may contact".
    """
    return _is_public_address(ip)


class LinkCheckBudgetExceeded(TimeoutError):
    """The build's link-check time budget ran out during a check."""


# The deadline (a time.monotonic() value) of the lint run in progress on
# this thread, or unset. lint_external_urls sets it around its loop; the
# connection guard and the response reader below consult it. Thread-local
# because the worker may compose several builds at once, and because the
# other users of guarded_urlopen (health probes) run with no deadline.
_budget = threading.local()


def _budget_deadline() -> Optional[float]:
    return getattr(_budget, "deadline", None)


def _within_budget(timeout):
    """`timeout` reduced to the time left in the budget, if one is set.
    Raises LinkCheckBudgetExceeded when nothing is left."""
    deadline = _budget_deadline()
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LinkCheckBudgetExceeded("link-check time budget exhausted")
    if timeout is None or timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
        return remaining
    return min(timeout, remaining)


def _guarded_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                               source_address=None, **_ignored):
    """Drop-in for socket.create_connection that refuses non-public hosts.

    Resolves `address`, rejects the destination if ANY returned address is
    non-public (a name that answers with both a public and a private
    address is not given the benefit of the doubt), then connects to the
    addresses that were checked. Nothing is resolved a second time.
    """
    host, port = address
    _within_budget(timeout)  # nothing is resolved once the budget is spent
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise OSError(f"no addresses for {host!r}")
    for _family, _type, _proto, _canon, sockaddr in infos:
        if not _is_public_address(sockaddr[0]):
            raise BlockedDestinationError(host, sockaddr[0])
    last_error: Optional[OSError] = None
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            # Each address tried gets at most the time left in the budget.
            attempt_timeout = _within_budget(timeout)
            if attempt_timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(attempt_timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error if last_error is not None else OSError("connection failed")


class _BudgetedReader(io.RawIOBase):
    """Raw reader over a connected socket that honors the budget deadline.

    http.client reads the status line and headers through a buffered file
    whose every low-level read waits up to the socket timeout. A server
    that sends one byte just inside each timeout can therefore hold a
    request open for a very long time. This reader re-arms the socket
    timeout with the time left before EVERY receive, and refuses to
    receive once none is left.

    `stock_file` is the file object http.client made for the socket. It is
    never read; it is kept only so that closing this reader releases the
    socket the way closing the stock file would.
    """

    def __init__(self, sock, stock_file, deadline: float):
        super().__init__()
        self._sock = sock
        self._stock_file = stock_file
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._sock.fileno()

    def readinto(self, buffer) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise LinkCheckBudgetExceeded("link-check time budget exhausted")
        current = self._sock.gettimeout()
        self._sock.settimeout(remaining if current is None else min(current, remaining))
        return self._sock.recv_into(buffer)

    def close(self) -> None:
        if not self.closed:
            try:
                self._stock_file.close()
            finally:
                super().close()


class _BudgetedResponse(http.client.HTTPResponse):
    """HTTPResponse whose reads stop at the budget deadline, when a lint
    run has set one on this thread. Without a deadline it is the stock
    response."""

    def __init__(self, sock, *args, **kwargs):
        super().__init__(sock, *args, **kwargs)
        deadline = _budget_deadline()
        if deadline is not None:
            self.fp = io.BufferedReader(_BudgetedReader(sock, self.fp, deadline))


class _GuardedHTTPConnection(http.client.HTTPConnection):
    response_class = _BudgetedResponse

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # http.client connects through this attribute (HTTPSConnection
        # inherits the same connect path and then wraps the socket in TLS,
        # still verifying the certificate against the host NAME).
        # test_checker_refuses_loopback_without_connecting fails if a future
        # Python stops honoring it.
        self._create_connection = _guarded_create_connection


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    response_class = _BudgetedResponse

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _guarded_create_connection


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__()
        self._guard_context = ssl.create_default_context()

    def https_open(self, req):
        return self.do_open(_GuardedHTTPSConnection, req, context=self._guard_context)


class _LimitedRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect target is chosen by the remote server, so it gets the
        # precheck the first URL got: http(s) only (the stock handler also
        # follows ftp://), default ports, no embedded credentials, bounded
        # length. Without it a public host could point the checker at any
        # port of any other public host. Every hop still passes the
        # connection guard as well.
        problem = _url_precheck(newurl)
        if problem:
            raise urllib.error.HTTPError(newurl, code, f"redirect refused: {problem}", headers, fp)
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is not None:
            # The stock handler reads the redirect response's body to the
            # end before it follows the redirect. Close it unread: its
            # later read() of a closed response returns nothing, so a
            # redirect with an endless or enormous body costs no time and
            # no memory.
            fp.close()
        return new_request


_opener: Optional[urllib.request.OpenerDirector] = None


def _guarded_open(req, timeout=None):
    """urlopen replacement built from an explicit handler list.

    urllib.request.build_opener would also install the proxy, ftp, file and
    data handlers; none of those belong in a link checker fed hostile URLs.
    """
    global _opener
    if _opener is None:
        opener = urllib.request.OpenerDirector()
        for handler in (
            _GuardedHTTPHandler(),
            _GuardedHTTPSHandler(),
            urllib.request.HTTPDefaultErrorHandler(),
            _LimitedRedirectHandler(),
            urllib.request.HTTPErrorProcessor(),
        ):
            opener.add_handler(handler)
        _opener = opener
    return _opener.open(req, timeout=timeout)


def guarded_urlopen(req, timeout=None):
    """urllib.request.urlopen for a URL the worker did not choose itself.

    For callers outside this module that probe a deployed customer app (the
    supervisor's zombie-deploy probe, the coordinator's health checks). Same
    opener as the link checker: every connection, redirect hops included, is
    refused unless the host resolves only to public addresses, at most
    MAX_REDIRECTS redirects are followed, each redirect target must pass
    `_url_precheck` (http(s), default port, no credentials) and its body is
    not read, and proxy settings from the environment are ignored. Callers
    still run their own URL-level check first
    (monitor.ping.probe_url_problem) and set a timeout. The lint's time
    budget does not apply to these callers.
    """
    return _guarded_open(req, timeout=timeout)


def _url_precheck(url: Any) -> Optional[str]:
    """Reason a URL will not be requested at all, or None if it may be."""
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        return "URL is too long"
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7f for ch in url):
        return "URL contains whitespace or control characters"
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return "URL does not parse"
    if parts.scheme.lower() not in ("http", "https"):
        return "not an http(s) URL"
    if not parts.hostname:
        return "URL has no host"
    if parts.username is not None or parts.password is not None:
        return "URL carries credentials"
    if port not in (None, 80, 443):
        return "non-default port"
    return None


# What a warning says about a URL whose host (or the host of a redirect on
# the way) the connection guard refused. No address, no host name.
BLOCKED_DESTINATION_TEXT = "refused: resolves to a non-public address"


def _budget_spent_result(url: str) -> Dict[str, Any]:
    return {"url": url, "ok": False, "status": None,
            "error": "not checked: time budget exhausted", "unchecked": True}


def _check_url_via(url: str, timeout: float, opener) -> Dict[str, Any]:
    """Internals split so tests can inject a fake opener."""

    def _try(method: str) -> Dict[str, Any]:
        deadline = _budget_deadline()
        if deadline is not None and time.monotonic() >= deadline:
            return _budget_spent_result(url)
        req = urllib.request.Request(url, method=method)
        # Use a recognizable UA so origin servers don't dump us into a
        # bot-blocker tier that masks the real link health. This matches
        # other CAS server-side fetchers.
        req.add_header(
            "User-Agent",
            "Mozilla/5.0 (compatible; CASLinkChecker/1.0; +https://craftandship.io)",
        )
        try:
            resp = opener(req, timeout=timeout)
            if resp is None:
                return {"url": url, "ok": False, "status": None, "error": "no handler for URL"}
            try:
                status = getattr(resp, "status", None) or resp.getcode()
            finally:
                # Only the status is needed. Close without reading so a
                # huge or never-ending body costs nothing.
                close = getattr(resp, "close", None)
                if callable(close):
                    close()
            return {"url": url, "ok": 200 <= status < 400, "status": status, "error": None}
        except urllib.error.HTTPError as e:
            try:
                e.close()
            except Exception:
                pass
            return {
                "url": url,
                "ok": False,
                "status": e.code,
                "error": f"HTTP {e.code}",
            }
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError,
                http.client.HTTPException, ValueError) as e:
            # A check cut short by the build's deadline was not judged; it
            # is reported as unchecked, not as a dead link. (urllib wraps
            # an error raised while connecting in URLError, hence both
            # tests.)
            if isinstance(e, LinkCheckBudgetExceeded) or isinstance(
                getattr(e, "reason", None), LinkCheckBudgetExceeded
            ) or (deadline is not None and time.monotonic() >= deadline):
                return _budget_spent_result(url)
            # A destination the connection guard refused is reported with a
            # fixed text: neither the address the name resolved to nor the
            # exception's own wording reaches the warning (this text is
            # copied into the build log; see BlockedDestinationError).
            if isinstance(e, BlockedDestinationError) or isinstance(
                getattr(e, "reason", None), BlockedDestinationError
            ):
                return {"url": url, "ok": False, "status": None, "error": BLOCKED_DESTINATION_TEXT}
            return {
                "url": url,
                "ok": False,
                "status": None,
                "error": f"{type(e).__name__}: {e}"[:120],
            }

    head = _try("HEAD")
    # Amazon + a number of CDNs answer HEAD with 405 / 403; retry as GET
    # before declaring the URL dead.
    if not head["ok"] and head["status"] in (403, 405):
        return _try("GET")
    return head


def lint_mode(mode: Optional[str] = None) -> str:
    """The mode a lint run uses: "skip", "warn" or "error".

    `mode` if given, else the CAS_LINT_EXTERNAL_URLS environment variable,
    else "warn". An unknown value is "warn" rather than a crash. One
    function, so that the lint and the build report that says whether it
    ran agree on what the mode is.
    """
    if mode is None:
        mode = (os.environ.get("CAS_LINT_EXTERNAL_URLS") or "warn").strip().lower()
    if mode not in {"skip", "warn", "error"}:
        mode = "warn"
    return mode


def lint_external_urls(
    spec: Dict[str, Any],
    mode: str = None,
    check_fn=None,
) -> List[Dict[str, str]]:
    """Lint a spec's external URLs and return warning dicts.

    Each warning is {"reason": "external_url_unreachable", "detail": "<...>"},
    the shape of an entry of BuildSpec.engine_warnings (build_app records
    each one there).

    `mode` overrides the CAS_LINT_EXTERNAL_URLS env var. Default behavior:
      * "skip"  → return [] without pinging anything
      * "warn"  → ping + return warnings (don't raise)
      * "error" → ping + return warnings + raise ExternalUrlLintError if
                  any URL failed

    `check_fn` is injectable for tests. Defaults to check_url.
    """
    mode = lint_mode(mode)
    if mode == "skip":
        return []

    if check_fn is None:
        check_fn = check_url

    urls = extract_external_urls(spec)
    warnings: List[Dict[str, str]] = []
    failures = 0

    # Bound the work a single spec can demand from the worker: a URL count
    # cap and a wall-clock budget. Whatever is left over is reported, not
    # silently skipped.
    unchecked = max(0, len(urls) - MAX_URLS)
    urls = urls[:MAX_URLS]
    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS

    # The deadline is published for this thread so that the real checker
    # also stops INSIDE a check (see _budget above); an injected check_fn
    # keeps the plain one-argument call.
    previous_deadline = _budget_deadline()
    _budget.deadline = deadline
    try:
        results = []
        for index, (field_path, url) in enumerate(urls):
            if time.monotonic() > deadline:
                unchecked += len(urls) - index
                break
            results.append((field_path, url, check_fn(url)))
    finally:
        _budget.deadline = previous_deadline

    for field_path, url, result in results:
        if result.get("ok"):
            continue
        if result.get("unchecked"):
            unchecked += 1
            continue
        failures += 1
        status = result.get("status")
        error = result.get("error") or "unreachable"
        status_str = str(status) if status is not None else "no-response"
        # Bounded on every part: the field path is built from spec keys and
        # the URL and the error text come from the spec and the network.
        detail = f"{field_path[:200]}={url[:300]} (status={status_str}, {str(error)[:160]})"
        warnings.append({
            "reason": "external_url_unreachable",
            "detail": detail,
        })

    if unchecked:
        warnings.append({
            "reason": "external_url_lint_truncated",
            "detail": (
                f"{unchecked} external URL(s) were not checked (limit: "
                f"{MAX_URLS} URLs / {int(TOTAL_BUDGET_SECONDS)}s per build)"
            ),
        })

    if mode == "error" and failures:
        raise ExternalUrlLintError(
            f"{failures} external URL(s) failed pre-deploy lint"
        )

    return warnings


__all__ = [
    "ExternalUrlLintError",
    "extract_external_urls",
    "check_url",
    "lint_external_urls",
    "lint_mode",
]
