# monitor/ping.py - Tier 1 and Tier 2 health checks
#
# Escalation path:
#   Tier 1: Single HTTP GET to /health  ->  instant pass/fail
#   Tier 2: Retry with backoff          ->  distinguishes transient from real outages
#   Tier 3/4: diagnose.run_diagnosis()  ->  called here when Tier 2 exhausted
#
# run_checks() is the main loop; call it to start continuous monitoring.

import ipaddress
import logging
import re
import socket
import time
from typing import Optional
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import LocationParseError, NewConnectionError
from urllib3.util import parse_url

from compose.lint_external_urls import is_public_address
from monitor.bounded_fetch import BoundedConnectionMixin, BoundedSession, resolve_bounded

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL guard (shared with cli/deploy.py and the coordinator agents that probe
# database-sourced URLs)
# ---------------------------------------------------------------------------
#
# Two layers, because a check on the text of a URL is only as good as the
# agreement between the code that checks and the code that connects:
#   1. probe_url_problem() reads the URL the way the sender does (urllib3, which
#      is what requests sends with), refuses any URL that the standard library
#      reads differently, and applies the name and literal-address policy.
#   2. guarded_session() sends through connections that resolve the host once,
#      refuse unless every address is public, and connect to the addresses that
#      were checked. Whatever a URL looked like, a probe sent through that
#      session cannot reach a loopback, private or link-local address.
#
# The app that answers a probe is customer code, so the session is also a
# bounded one (monitor/bounded_fetch.py): one deadline for the whole exchange
# and a cap on the decoded body. The caps used for probes:

# A /health answer is a few dozen bytes of JSON.
HEALTH_BODY_MAX_BYTES = 64 * 1024
# A rendered page of a generated site (the login form, a list view).
PAGE_BODY_MAX_BYTES = 2 * 1024 * 1024

_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")

# A final label made only of digits, or a 0x hex number, makes resolvers read
# the whole host as an IPv4 address in one of its legacy spellings:
# "2130706433", "127.1", "0x7f.0.0.1" and "017700000001" all mean 127.0.0.1.
_NUMERIC_LAST_LABEL_RE = re.compile(r"(?:[0-9]+|0[xX][0-9a-fA-F]*)")

# The only characters a probed host name may contain. Percent signs,
# underscores, backslashes and anything outside ASCII are refused: URL parsers
# disagree about what they mean (urllib3 decodes "%6c" in a host, the standard
# library does not; a browser reads a backslash as a slash).
_HOST_NAME_RE = re.compile(r"[A-Za-z0-9.-]+")


class ProbeDestinationRefused(NewConnectionError):
    """A guarded connection would have gone to an address that is not public."""

    def __init__(self, conn, reason: str):
        self.reason = reason
        super().__init__(conn, f"probe refused: {reason}")

    def __str__(self) -> str:
        return f"probe refused: {self.reason}"


def host_resolution_problem(host: str) -> Optional[str]:
    """Resolve `host`; return a reason unless EVERY address is public.

    A name that does not resolve is refused as well (nothing useful can be
    probed there, and "unknown" must not read as "safe").

    This is a check, not a pin: the caller's own connection performs a second
    lookup, and whoever controls the name's DNS can answer differently then
    (DNS rebinding). The requests-based probes do not rely on it; they send
    through guarded_session(), which connects to the addresses it checked. The
    one caller left is the browser guard in cli/deploy.py, because a browser's
    own lookup cannot be pinned from here.

    The lookup is given monitor.bounded_fetch.LOOKUP_DEADLINE_S; one that
    takes longer counts as "does not resolve".
    """
    try:
        infos = resolve_bounded(host, None)
    except (OSError, UnicodeError):
        return "host does not resolve"
    if not infos:
        return "host does not resolve"
    for info in infos:
        if not is_public_address(str(info[4][0])):
            return "host resolves to a non-public address"
    return None


def _host_and_port(url: str):
    """(scheme, host, port, has_credentials) as urllib3 reads `url`, or a reason string.

    urllib3 is the parser that decides where a requests call connects. The
    standard library's urlsplit is consulted as a second opinion: when the two
    disagree about the scheme, the host, the port or the presence of
    credentials, the URL is ambiguous and is refused instead of trusting either
    reading. ("http://127.0.0.1\\.evil.example" is the textbook case: urlsplit
    sees one long host name, urllib3 connects to 127.0.0.1.)
    """
    if not isinstance(url, str) or not url:
        return "unparseable URL"
    if any(ord(ch) <= 0x20 or ord(ch) >= 0x7f or ch == "\\" for ch in url):
        return "whitespace, backslash, control or non-ASCII character in URL"
    try:
        sent = parse_url(url)
        sent_host = (sent.host or "").lower()
        sent_view = ((sent.scheme or "").lower(), sent_host.strip("[]"), sent.port, bool(sent.auth))
    except (LocationParseError, ValueError, AttributeError):
        return "unparseable URL"
    try:
        std = urlsplit(url)
        std_view = (
            (std.scheme or "").lower(),
            (std.hostname or "").lower(),
            std.port,
            std.username is not None or std.password is not None,
        )
    except (ValueError, AttributeError):
        return "unparseable URL"
    if sent_view != std_view:
        return "ambiguous URL (parsers disagree about scheme, host, port or credentials)"
    return sent_view


def probe_url_problem(url: str) -> Optional[str]:
    """Return why `url` must not be probed, or None if it is acceptable.

    Probed URLs come from database rows (monitor_apps.live_url, builds.live_url
    and preview_url) and from Railway API responses. They are expected to be
    public https hostnames assigned by the hosting platform, so anything else
    is refused: non-http(s) schemes (file:, ftp:), embedded credentials, URLs
    that two parsers read differently (see _host_and_port), host names with
    characters outside letters, digits, dots and hyphens, unqualified and
    internal hostnames (a trailing dot does not help), legacy numeric spellings
    of an IPv4 address, and literal addresses that are not public unicast
    (loopback, private, link-local and cloud metadata, carrier-grade NAT, IPv6
    forms that wrap an IPv4 address). The address policy is
    compose.lint_external_urls.is_public_address.

    This is a check of the text only: a public-looking name that resolves to a
    private address passes it. What stops that is the connection, not this
    function: send through guarded_session() (requests) or
    connect_checked() (a raw socket), which resolve the name once, apply the
    same address policy to every answer and connect to what they checked.
    Redirects are the caller's job as well: guarded_session() checks every hop.
    """
    parsed = _host_and_port(url)
    if isinstance(parsed, str):
        return parsed
    scheme, host, _port, has_credentials = parsed
    host = host.rstrip(".")
    if scheme not in ("http", "https"):
        return f"scheme {scheme!r} not allowed"
    if not host:
        return "no host"
    if has_credentials:
        return "credentials in URL"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        return None if is_public_address(str(address)) else "non-public address"
    if not _HOST_NAME_RE.fullmatch(host):
        return "unexpected character in host name"
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return "internal hostname"
    labels = host.split(".")
    if _NUMERIC_LAST_LABEL_RE.fullmatch(labels[-1]):
        return "numeric host that is not a plain IP address"
    if len(labels) < 2:
        return "unqualified hostname"
    return None


def _checked_addresses(host: str, port, conn=None, seconds: Optional[float] = None) -> list:
    """Resolve `host` once and return its addresses if every one is public.

    Raises ProbeDestinationRefused when the name does not resolve or when any
    answer is not public: a name that answers with both a public and a private
    address is not given the benefit of the doubt.

    The lookup is bounded (monitor.bounded_fetch.resolve_bounded): by
    `seconds` when given, else by the time left in the exchange in progress.
    A lookup that does not finish in time is a name that does not resolve.
    """
    lookup_host = host.strip("[]") if host.startswith("[") else host
    try:
        infos = resolve_bounded(lookup_host, port, seconds)
    except (OSError, UnicodeError) as exc:
        raise ProbeDestinationRefused(conn, f"{host} does not resolve") from exc
    addresses = []
    for info in infos:
        address = str(info[4][0])
        if not is_public_address(address):
            raise ProbeDestinationRefused(conn, f"{host} resolves to a non-public address")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ProbeDestinationRefused(conn, f"{host} does not resolve")
    return addresses


def connect_checked(host: str, port: int, timeout: float = 10.0) -> socket.socket:
    """socket.create_connection for a probed host: resolve once, check, connect.

    For the probes that need a raw socket (the certificate check in
    monitor/diagnose.py). Raises ProbeDestinationRefused or OSError.

    `timeout` is one limit for the lookup and every connection attempt
    together, and the socket is returned with what is left of it as its
    timeout, so a TLS handshake the caller starts next ends inside the same
    limit.
    """
    deadline = time.monotonic() + timeout
    last_error: Optional[OSError] = None
    for address in _checked_addresses(host, port, seconds=timeout):
        left = deadline - time.monotonic()
        if left <= 0:
            last_error = socket.timeout(f"connection to {host} timed out")
            break
        try:
            sock = socket.create_connection((address, port), timeout=left)
        except OSError as exc:
            last_error = exc
            continue
        sock.settimeout(max(deadline - time.monotonic(), 0.001))
        return sock
    raise last_error if last_error is not None else OSError("connection failed")


class _PinnedConnectionMixin(BoundedConnectionMixin):
    """Bounded urllib3 connection that connects only to addresses it resolved
    and checked.

    BoundedConnectionMixin makes the connection (lookup, connect, handshake,
    request, response reads) under the exchange deadline and asks
    _addresses() where to connect; for https urllib3 then wraps the socket in
    TLS using the host NAME for SNI and for the certificate check. Overriding
    only _addresses() therefore pins the address without changing which
    certificate is expected.
    """

    def _addresses(self) -> list:
        return _checked_addresses(self._dns_host, self.port, self)


class _PinnedHTTPConnection(_PinnedConnectionMixin, HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnectionMixin, HTTPSConnection):
    pass


class _PinnedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _PinnedHTTPConnection


class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


_PINNED_POOL_CLASSES = {"http": _PinnedHTTPConnectionPool, "https": _PinnedHTTPSConnectionPool}


def guarded_session(max_body_bytes: int = HEALTH_BODY_MAX_BYTES) -> requests.Session:
    """A requests.Session for probing customer-influenced or database-sourced URLs.

    * Every request it sends, each redirect hop included, must pass
      probe_url_problem; a hop that does not raises requests InvalidURL.
    * Every connection it opens resolves the host once, refuses unless every
      address is public (ProbeDestinationRefused, which requests reports as a
      ConnectionError) and connects to those addresses. There is no second
      lookup for a rebinding DNS server to answer differently.
    * Environment proxy settings, .netrc credentials and CA-bundle overrides
      are ignored (trust_env is off): through a proxy the name would be
      resolved by the proxy, out of reach of the check above.
    * Every request is one bounded exchange (monitor.bounded_fetch): the
      `timeout` passed to get() or post() is the deadline of the whole
      exchange (lookup, connect, TLS, request, headers, body and every
      redirect hop; 10 seconds when omitted), and at most `max_body_bytes`
      of decoded body are kept. A response that was cut at the cap has
      `body_truncated` set, and the rest of its body is never read.
    """
    session = BoundedSession(max_body_bytes=max_body_bytes, pool_classes=_PINNED_POOL_CLASSES)
    real_send = session.send

    def _checked_send(request, **kwargs):
        problem = probe_url_problem(request.url)
        if problem:
            raise requests.exceptions.InvalidURL(f"probe refused: {problem}")
        return real_send(request, **kwargs)

    session.send = _checked_send
    return session


def refusal_reason(error) -> Optional[str]:
    """Why a guarded connection refused its destination, if `error` is that.

    requests wraps urllib3's errors (ConnectionError around MaxRetryError
    around the cause), so the refusal is looked for in the whole chain.
    """
    seen = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ProbeDestinationRefused):
            return current.reason
        pending.extend(arg for arg in getattr(current, "args", ()) if isinstance(arg, BaseException))
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException):
            pending.append(reason)
        pending.append(current.__cause__)
        pending.append(current.__context__)
    return None


# ---------------------------------------------------------------------------
# Tier 1: Single HTTP ping
# ---------------------------------------------------------------------------

def ping(url: str, timeout: int = 10) -> dict:
    """Perform a single HTTP GET to {url}/health and measure latency.

    This is Tier 1 in the escalation chain. It is cheap and fast; call it
    on every scheduled check cycle before considering deeper diagnostics.

    Args:
        url: Base URL of the app (e.g. https://myapp.up.railway.app).
             '/health' is appended automatically.
        timeout: Seconds before the request is abandoned. It is the deadline
            of the whole exchange, name lookup and body included, not a
            per-read timeout: an app that drips its answer is cut off here.

    Returns:
        Dict with keys:
            ok          (bool)    True if HTTP 200.
            status_code (int)     HTTP response code, or 0 on connection error.
            latency_ms  (int)     Round-trip time in milliseconds, or 0 on error.
            error       (str|None) Error message string, or None on success.
            An error that starts with "Refusing to probe" means the URL or the
            address it resolves to is not one the monitor will contact.
    """
    problem = probe_url_problem(url)
    if problem:
        logger.warning("ping: refusing to probe %r: %s", url, problem)
        return {"ok": False, "status_code": 0, "latency_ms": 0,
                "error": f"Refusing to probe URL: {problem}"}
    health_url = url.rstrip("/") + "/health"
    start = time.monotonic()
    session = guarded_session()
    try:
        # No redirects: the monitored app is customer code, and a healthy
        # /health answers 200 itself. The session resolves the host once and
        # connects to the address it checked (see guarded_session).
        response = session.get(health_url, timeout=timeout, allow_redirects=False)
        latency_ms = int((time.monotonic() - start) * 1000)
        ok = response.status_code == 200
        return {
            "ok": ok,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "error": None if ok else f"HTTP {response.status_code}",
        }
    except requests.exceptions.ConnectionError as error:
        refused = refusal_reason(error)
        if refused and "does not resolve" not in refused:
            # A name that resolves to a non-public address will not become
            # acceptable by waiting. (A failed lookup can be a DNS blip, so it
            # stays an ordinary connection error and Tier 2 retries it.)
            logger.warning("ping: refusing to probe %r: %s", url, refused)
            return {"ok": False, "status_code": 0, "latency_ms": 0,
                    "error": f"Refusing to probe URL: {refused}"}
        return {"ok": False, "status_code": 0, "latency_ms": 0, "error": str(error)}
    except requests.exceptions.Timeout:
        return {
            "ok": False,
            "status_code": 0,
            "latency_ms": int(timeout * 1000),
            "error": f"Timeout after {timeout}s",
        }
    except requests.RequestException as error:
        return {"ok": False, "status_code": 0, "latency_ms": 0, "error": str(error)}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Tier 2: Retry with backoff
# ---------------------------------------------------------------------------

def ping_with_retry(url: str, retries: int = 3, backoff: float = 120.0) -> dict:
    """Ping up to `retries` times, waiting `backoff` seconds between attempts.

    This is Tier 2 in the escalation chain. It distinguishes a transient
    blip (one ping fails but the next succeeds) from a genuine outage
    (all retries exhausted).

    Args:
        url: Base URL of the app.
        retries: Maximum number of ping attempts.
        backoff: Seconds to wait between attempts (default 2 min per spec).

    Returns:
        Dict with keys:
            ok           (bool)  True if any attempt succeeded.
            attempts     (int)   Number of attempts made.
            final_result (dict)  The result dict from the last ping() call.
    """
    final_result = {}
    for attempt in range(1, retries + 1):
        final_result = ping(url)
        if final_result["ok"]:
            logger.info("ping_with_retry: %s OK on attempt %d", url, attempt)
            return {"ok": True, "attempts": attempt, "final_result": final_result}
        if str(final_result.get("error", "")).startswith("Refusing to probe"):
            # A refused URL will not become acceptable by waiting.
            return {"ok": False, "attempts": attempt, "final_result": final_result}
        logger.warning(
            "ping_with_retry: %s failed attempt %d/%d: %s",
            url, attempt, retries, final_result.get("error"),
        )
        if attempt < retries:
            time.sleep(backoff)

    return {"ok": False, "attempts": retries, "final_result": final_result}


# ---------------------------------------------------------------------------
# Single-app check (Tier 1 + 2)
# ---------------------------------------------------------------------------

def check_app(app_id: str) -> dict:
    """Run Tier 1 then Tier 2 checks for a single registered app.

    Updates registry status:
    - 'healthy'  if Tier 1 passes.
    - 'degraded' if Tier 1 fails but Tier 2 eventually passes.
    - 'down'     if both Tier 1 and Tier 2 exhausted.

    This is the one place a check cycle is counted: 'down' adds one to the
    app's consecutive_failures, and 'healthy' and 'degraded' (the app
    answered) reset it. See monitor.registry.update_status.

    Does NOT trigger Tier 3/4 diagnosis; that is the caller's responsibility.

    Args:
        app_id: UUID of the app in the registry.

    Returns:
        Dict with keys:
            app_id      (str)
            ok          (bool)
            tier        (str)  '1' or '2' indicating which tier resolved it.
            result      (dict) Final ping result.
    """
    # Import here to avoid circular imports (diagnose imports registry too).
    from monitor.registry import get_app, update_status

    app = get_app(app_id)
    url = app["live_url"]

    tier1 = ping(url)
    if tier1["ok"]:
        latency = tier1.get("latency_ms", 0)
        status_info = update_status(app_id, "healthy", latency_ms=latency)
        if status_info.get("latency_degraded"):
            logger.warning(
                "check_app: %s latency degraded (%dms, >2x baseline)",
                app_id, latency,
            )
        return {"app_id": app_id, "ok": True, "tier": "1", "result": tier1,
                "latency_degraded": status_info.get("latency_degraded", False)}

    logger.warning("check_app: %s Tier 1 failed (%s), entering Tier 2", app_id, tier1.get("error"))
    tier2 = ping_with_retry(url)

    if tier2["ok"]:
        latency = tier2["final_result"].get("latency_ms", 0)
        update_status(app_id, "degraded", note="Tier 1 failed but Tier 2 recovered", latency_ms=latency)
        return {"app_id": app_id, "ok": True, "tier": "2", "result": tier2["final_result"]}

    update_status(app_id, "down", note=tier2["final_result"].get("error"))
    return {"app_id": app_id, "ok": False, "tier": "2", "result": tier2["final_result"]}


# ---------------------------------------------------------------------------
# Main check loop
# ---------------------------------------------------------------------------

# After a cycle that could not even read the registry, the loop waits this long
# before trying again, doubling on each further failure up to the normal
# interval. A database restart therefore costs minutes of monitoring, not all
# of it.
_REGISTRY_RETRY_INITIAL_S = 30.0


def run_checks(interval_seconds: int = 900, stop_event=None) -> None:
    """Continuously check all registered apps at a fixed interval.

    Escalation path per app per cycle:
      1. Tier 1 ping.
      2. If Tier 1 fails, Tier 2 retry with backoff.
      3. If Tier 2 exhausted, call diagnose.run_diagnosis() (Tier 3 + 4).

    Updates the registry after each check. Designed to run forever: an error
    while checking one app is logged and the cycle moves on to the next app,
    and an error while reading the registry (the database restarting, a
    dropped connection) is logged and retried after a backoff that starts at
    _REGISTRY_RETRY_INITIAL_S and doubles up to interval_seconds. Neither ends
    the loop. Interrupt with SIGINT / KeyboardInterrupt, or set stop_event.

    Args:
        interval_seconds: Seconds between full check cycles (default 900 = 15 min).
        stop_event: Optional threading.Event; the loop returns once it is set
            (checked between cycles; the waits between cycles end early).
    """
    # Import lazily to avoid circular dependency at module load time.
    from monitor.registry import list_apps
    from monitor import diagnose

    logger.info("run_checks: starting, interval=%ds", interval_seconds)

    def _wait(seconds: float) -> None:
        if stop_event is not None:
            stop_event.wait(seconds)
        else:
            time.sleep(seconds)

    registry_failures = 0
    while stop_event is None or not stop_event.is_set():
        cycle_start = time.monotonic()
        try:
            apps = list_apps()
        except Exception as error:
            registry_failures += 1
            retry_in = min(
                float(interval_seconds),
                _REGISTRY_RETRY_INITIAL_S * (2 ** (registry_failures - 1)),
            )
            logger.error(
                "run_checks: could not read the registry (failure %d in a row): %s; "
                "retrying in %.0fs",
                registry_failures, error, retry_in,
            )
            _wait(retry_in)
            continue
        registry_failures = 0
        logger.info("run_checks: checking %d app(s)", len(apps))

        for app in apps:
            app_id = app["id"]
            app_name = app["app_name"]
            consecutive = app.get("consecutive_failures", 0)
            try:
                # Auto-deregister after 10 consecutive check cycles in which the
                # app did not answer (one count per cycle; about 2.5 hours at
                # the default 15 minute interval). This stops escalation emails
                # for deleted Railway projects. It also ends the app's daily
                # database dumps, because monitor.backup.run_daily_backups
                # iterates this registry, so say so loudly.
                if consecutive >= 10:
                    logger.error(
                        "run_checks: %s (%s) did not answer in %d consecutive check cycles; "
                        "removing it from the registry. Health checks AND daily database "
                        "backups for this app stop until it is registered again.",
                        app_name, app_id, consecutive,
                    )
                    from monitor.registry import remove_app
                    remove_app(app_id)
                    continue

                result = check_app(app_id)
                if not result["ok"]:
                    logger.warning(
                        "run_checks: %s (%s) is down (failures=%d), escalating to Tier 3/4",
                        app_name, app_id, consecutive + 1,
                    )
                    diagnose.run_diagnosis(app_id)
                else:
                    logger.info(
                        "run_checks: %s OK (Tier %s, %dms)",
                        app_name, result["tier"], result["result"].get("latency_ms", 0),
                    )
            except Exception as error:
                logger.error("run_checks: error checking %s: %s", app_name, error)

        elapsed = time.monotonic() - cycle_start
        sleep_time = max(0.0, interval_seconds - elapsed)
        logger.info(
            "run_checks: cycle done in %.1fs, sleeping %.1fs", elapsed, sleep_time
        )
        _wait(sleep_time)
