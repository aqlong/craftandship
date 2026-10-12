"""Every outbound request is one bounded exchange: a deadline and a byte cap.

The probes the monitor and the worker send go to customer apps, and the app
decides how its answer arrives. Each case here starts a real listener on
loopback that answers the way a hostile or broken app can (a byte at a time,
without end, compressed to a fraction of its size, by a chain of slow
redirects, behind a name server that never answers) and sends the real probe
at it. No request leaves this machine.

What is asserted is what the caller meets: how long the call took, how many
bytes it kept, and what it reported.

  * TestProbesEndAtTheirDeadline: the timeout of a probe is the limit of the
    whole exchange (lookup, connect, headers, body, redirects).
  * TestProbesStopAtTheirByteCap: a body is cut at the cap, decoded size
    counted, and the rest of it is never read.
  * TestHttpsIsPinnedAndBounded: the same over TLS, against a listener with a
    certificate made for the test run. This is the class that fails when the
    https side of the guarded session is not pinned or not bounded.
  * TestProviderCalls: the two helpers that calls to Railway, Resend and the
    Anthropic cost report go through.
  * TestEveryOutboundCallIsBounded: a scan of the modules, so a new call
    site that goes around the bounded exchange fails here.
"""

import ast
import gzip
import http.server
import json
import socket
import ssl
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).parent.parent
for _p in (str(ROOT / "queue"), str(ROOT / "cli"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import deploy
import monitor.bounded_fetch as bounded
import monitor.ping as ping_mod
import watchdog_probe

MIB = 1024 * 1024
HEALTHY = b'{"status":"ok"}'


def _pause(seconds: float) -> None:
    """The listeners' own delay. Not time.sleep: several tests replace that
    (through deploy.time) to skip the product code's waits, and the listeners
    must stay slow while they do."""
    threading.Event().wait(seconds)


# ---------------------------------------------------------------------------
# Listeners
# ---------------------------------------------------------------------------

class _App(http.server.BaseHTTPRequestHandler):
    """One hostile behaviour per path. `stats` is shared per server class."""

    protocol_version = "HTTP/1.1"
    stats = None       # {"sent": bytes written to the socket, "paths": [...]}

    def _send(self, data: bytes) -> bool:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except OSError:
            return False
        self.stats["sent"] += len(data)
        return True

    def _head(self, length, extra=()):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(length))
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()

    def do_HEAD(self):
        self.stats["paths"].append(self.path)
        self._head(0)

    def do_GET(self):
        self.stats["paths"].append(self.path)
        path = self.path.split("?")[0]
        if path in ("/health", "/ok/health"):
            self._head(len(HEALTHY))
            self._send(HEALTHY)
        elif path == "/drip/health":
            # A body that arrives one byte at a time, each byte well inside
            # any per-read timeout.
            self._head(60)
            for _ in range(60):
                if not self._send(b" "):
                    return
                _pause(0.25)
        elif path == "/slowheaders/health":
            # The same, before the headers are complete.
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            self.wfile.flush()
            for index in range(60):
                if not self._send(f"X-Pad-{index}: x\r\n".encode()):
                    return
                _pause(0.25)
        elif path == "/huge/health":
            total = 40 * MIB
            self._head(total)
            chunk = b"x" * 65536
            for _ in range(total // len(chunk)):
                if not self._send(chunk):
                    return
        elif path == "/endless/health":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = b"1000\r\n" + b"y" * 4096 + b"\r\n"
            while self._send(chunk):
                pass
        elif path in ("/gzip/health", "/deflate/health"):
            plain = b"\0" * (24 * MIB)
            packed = gzip.compress(plain, 9) if path.startswith("/gzip") else zlib.compress(plain, 9)
            self.stats["packed"] = len(packed)
            self._head(len(packed), [("Content-Encoding", "gzip" if path.startswith("/gzip") else "deflate")])
            self._send(packed)
        elif path == "/smallgzip/health":
            packed = gzip.compress(HEALTHY)
            self._head(len(packed), [("Content-Encoding", "gzip")])
            self._send(packed)
        elif path == "/brotli/health":
            self._head(len(HEALTHY), [("Content-Encoding", "br")])
            self._send(HEALTHY)
        elif path.startswith("/hop/"):
            # A chain of redirects, each a little slow, each with a large body.
            step = int(path.split("/")[2])
            _pause(self.stats.get("hop_delay", 0.4))
            body = b"r" * (2 * MIB)
            self.send_response(302)
            self.send_header("Location", f"/hop/{step + 1}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._send(body)
        elif path == "/bigredirect":
            body = b"r" * (8 * MIB)
            self.send_response(302)
            self.send_header("Location", "/health")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._send(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        received = self.rfile.read(length)
        self.stats["paths"].append(self.path)
        if self.path == "/api/large":
            body = b'{"data": "' + b"z" * (2 * MIB) + b'"}'
        elif self.path == "/api/teapot":
            body = b'{"error": "short and stout"}'
            self.send_response(418)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._send(body)
            return
        elif self.path == "/api/moved":
            self.send_response(302)
            self.send_header("Location", "/api/echo")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif self.path == "/api/drip":
            self._head(40)
            for _ in range(40):
                if not self._send(b" "):
                    return
                _pause(0.25)
            return
        else:
            body = json.dumps({"echo": received.decode(), "auth": self.headers.get("Authorization")}).encode()
        self._head(len(body))
        self._send(body)

    def log_message(self, *args):
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """A probe that reached its deadline or its byte cap hangs up, and the
        listener's next write or read fails. These tests provoke exactly
        that, so the socket error is not printed; anything else still is."""
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)


@pytest.fixture()
def app():
    stats = {"sent": 0, "paths": []}
    server = _Server(("127.0.0.1", 0), type("App", (_App,), {"stats": stats}))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", stats
    server.shutdown()
    server.server_close()


@pytest.fixture()
def loopback_counts_as_public(monkeypatch):
    """Let the address policy accept loopback so the probes will connect to
    the listeners above. Every other address keeps its real verdict."""
    real = ping_mod.is_public_address
    monkeypatch.setattr(ping_mod, "is_public_address",
                        lambda ip: ip in ("127.0.0.1", "::1") or real(ip))


@pytest.fixture()
def name_service(monkeypatch):
    """Answers for chosen host names; a value of "never" blocks the lookup
    until the test ends, the way an unreachable name server does."""
    answers = {}
    lookups = []
    release = threading.Event()
    previous = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host in answers:
            lookups.append(host)
            if answers[host] == "never":
                release.wait(30)
                raise socket.gaierror("lookup abandoned")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))
                    for address in answers[host]]
        return previous(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    yield answers, lookups
    release.set()


def _timed(call):
    started = time.monotonic()
    try:
        result = call()
    except Exception as exc:  # the caller asserts on what was raised
        result = exc
    return result, time.monotonic() - started


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("loopback_counts_as_public")
class TestProbesEndAtTheirDeadline:
    def test_the_monitor_ping_gives_up_on_a_body_that_drips(self, app):
        """One byte per read used to hold ping(timeout=1) for as long as the
        app kept dripping (11 seconds in the review that found this), on the
        one thread that checks every customer's app."""
        base, _stats = app
        result, took = _timed(lambda: ping_mod.ping(base + "/drip", timeout=1))
        assert took < 2.5, f"ping(timeout=1) took {took:.1f}s"
        assert result["ok"] is False and result["error"] == "Timeout after 1s"

    def test_headers_that_drip_are_cut_off_the_same_way(self, app):
        base, _stats = app
        result, took = _timed(lambda: ping_mod.ping(base + "/slowheaders", timeout=1))
        assert took < 2.5, f"ping(timeout=1) took {took:.1f}s"
        assert result["ok"] is False

    def test_a_healthy_app_still_passes(self, app):
        base, _stats = app
        result = ping_mod.ping(base + "/ok", timeout=3)
        assert result == {"ok": True, "status_code": 200, "latency_ms": result["latency_ms"], "error": None}

    def test_the_deploy_health_poll_does_not_wait_for_a_dripping_app(self, app, monkeypatch):
        base, stats = app
        monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)
        result, took = _timed(lambda: deploy.verify_health(base + "/drip", timeout=2))
        # verify_health polls with a 5 second exchange deadline inside its own
        # window; a poll that started just before the window closed may run
        # to that deadline, and no further.
        assert took < 2 + 5 + 1.5, f"verify_health(timeout=2) took {took:.1f}s"
        assert result == "infra", "a dripping app answered nothing usable"
        assert stats["paths"].count("/drip/health") >= 1

    def test_the_zombie_probe_stays_inside_its_budget(self, app, monkeypatch):
        base, stats = app
        host_port = base.removeprefix("http://")

        class FakeDeploy:
            health_response_ok = staticmethod(deploy.health_response_ok)
            get_token = staticmethod(lambda: "not-" + "a-token")
            list_projects = staticmethod(lambda token, deadline=None: [
                {"id": "proj-1", "name": "sample-app", "createdAt": "2026-04-10T12:05:00+00:00"}])
            get_service_domain = staticmethod(lambda project_id, token, deadline=None: {
                "domain": "sample-app.example.test", "service_id": "svc-1"})

        # The probe builds "https://<domain>/health"; send that to the plain
        # listener's dripping path instead (the URL text is not what is
        # under test here, the exchange is).
        real_session = ping_mod.guarded_session

        def session_to_listener(**caps):
            session = real_session(**caps)
            real_get = session.get
            session.get = lambda url, **kwargs: real_get(f"http://{host_port}/drip/health", **kwargs)
            return session

        monkeypatch.setattr(ping_mod, "guarded_session", session_to_listener)
        monkeypatch.setitem(sys.modules, "deploy", FakeDeploy)
        from datetime import datetime, timezone
        started = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
        result, took = _timed(lambda: watchdog_probe.probe_for_zombie_deploy(
            "sample-app", started_at=started, hard_timeout_s=2))
        assert result is None
        assert took < 3.5, f"a probe with a 2 second budget took {took:.1f}s"
        assert stats["paths"] == ["/drip/health"]

    def test_redirect_hops_share_one_deadline(self, app):
        """Each hop answers in 0.4 seconds, inside any per-request timeout.
        The exchange as a whole is given one second."""
        base, stats = app
        session = ping_mod.guarded_session()
        outcome, took = _timed(lambda: session.get(base + "/hop/0", timeout=1))
        assert isinstance(outcome, requests.exceptions.RequestException), outcome
        assert took < 2.5, f"a redirect chain held a 1 second exchange for {took:.1f}s"
        assert len(stats["paths"]) <= 4

    def test_a_redirect_chain_is_cut_at_the_hop_limit(self, app):
        base, stats = app
        stats["hop_delay"] = 0
        session = ping_mod.guarded_session()
        with pytest.raises(requests.exceptions.TooManyRedirects):
            session.get(base + "/hop/0", timeout=30)
        assert len(stats["paths"]) == bounded.MAX_REDIRECTS + 1

    def test_a_name_lookup_that_never_answers_is_inside_the_deadline(self, name_service):
        """getaddrinfo takes no timeout. The lookup is part of the exchange,
        so it must not be able to outlast it."""
        answers, lookups = name_service
        answers["stuck.example.com"] = "never"
        result, took = _timed(lambda: ping_mod.ping("http://stuck.example.com", timeout=1))
        assert took < 2.5, f"ping(timeout=1) waited {took:.1f}s for a name lookup"
        assert result["ok"] is False
        assert lookups == ["stuck.example.com"]

    def test_the_certificate_check_socket_has_one_limit_for_lookup_and_connect(self, name_service):
        answers, _lookups = name_service
        answers["stuck.example.com"] = "never"
        outcome, took = _timed(lambda: ping_mod.connect_checked("stuck.example.com", 443, timeout=1))
        assert isinstance(outcome, ping_mod.ProbeDestinationRefused)
        assert took < 2.5

    def test_the_browser_guards_lookup_is_bounded_too(self, name_service, monkeypatch):
        answers, _lookups = name_service
        answers["stuck.example.com"] = "never"
        monkeypatch.setattr(bounded, "LOOKUP_DEADLINE_S", 0.5)
        outcome, took = _timed(lambda: ping_mod.host_resolution_problem("stuck.example.com"))
        assert outcome == "host does not resolve"
        assert took < 2.0

    def test_a_reused_connection_does_not_inherit_the_last_exchanges_remaining_time(self):
        """Two requests on one session, over one kept-alive connection. The
        first ends 0.2 seconds before its deadline, so the socket's timeout
        was down to that. The second has 5 seconds and an app that takes 1:
        it must get its own time, or a healthy app is reported down after any
        slow answer."""
        delays = [0.8, 1.0]
        connections = set()

        class Slow(_App):
            def do_GET(self):
                connections.add(self.client_address)
                _pause(delays.pop(0))
                self._head(len(HEALTHY))
                self._send(HEALTHY)

        server = _Server(("127.0.0.1", 0), type("S", (Slow,), {"stats": {"sent": 0, "paths": []}}))
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/health"
        try:
            with bounded.BoundedSession() as session:
                first = session.get(url, timeout=1)
                second, took = _timed(lambda: session.get(url, timeout=5))
        finally:
            server.shutdown()
            server.server_close()
        assert first.status_code == 200
        assert isinstance(second, requests.Response), second
        assert second.content == HEALTHY and 0.9 < took < 3
        assert len(connections) == 1, "the two requests did not share a connection, so this showed nothing"

    def test_a_timeout_pair_is_one_deadline(self, app):
        """requests accepts (connect, read); on a bounded session the pair is
        added up into the one deadline, it does not become two timeouts."""
        base, _stats = app
        session = ping_mod.guarded_session()
        outcome, took = _timed(lambda: session.get(base + "/drip/health", timeout=(0.5, 0.5)))
        assert isinstance(outcome, requests.exceptions.Timeout), outcome
        assert took < 2.5


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("loopback_counts_as_public")
class TestProbesStopAtTheirByteCap:
    def test_a_huge_body_is_cut_at_the_cap_and_not_read_to_its_end(self, app):
        base, stats = app
        session = ping_mod.guarded_session()
        response = session.get(base + "/huge/health", timeout=10)
        assert response.status_code == 200
        assert len(response.content) == ping_mod.HEALTH_BODY_MAX_BYTES
        assert response.body_truncated is True
        time.sleep(0.2)   # let the listener notice the closed connection
        # The listener could only hand over what fits in socket buffers after
        # the probe stopped reading: a small part of its 40 MiB.
        assert stats["sent"] < 8 * MIB, f"{stats['sent'] / MIB:.1f} MiB of 40 were taken from the app"

    def test_a_body_with_no_end_is_cut_at_the_cap(self, app):
        base, _stats = app
        response = ping_mod.guarded_session().get(base + "/endless/health", timeout=10)
        assert len(response.content) == ping_mod.HEALTH_BODY_MAX_BYTES and response.body_truncated

    @pytest.mark.parametrize("path", ["/gzip/health", "/deflate/health"])
    def test_a_compressed_body_cannot_inflate_past_the_cap(self, app, path):
        """About 24 KiB on the wire used to become 24 MiB in the worker."""
        base, stats = app
        response = ping_mod.guarded_session().get(base + path, timeout=10)
        assert stats["packed"] < 64 * 1024, "the listener sent a small compressed body"
        assert len(response.content) == ping_mod.HEALTH_BODY_MAX_BYTES
        assert response.body_truncated is True

    @pytest.mark.parametrize("path", ["/gzip/health", "/deflate/health"])
    def test_the_decompressor_never_produces_more_than_the_room_left(self, app, path, monkeypatch):
        """Cutting the body after it was inflated is too late: one 16 KiB
        chunk of a compressed body can inflate to megabytes. What the
        decompressor hands back in any one call is measured here."""
        base, _stats = app
        produced = []
        real_decompressobj = zlib.decompressobj

        class Measured:
            def __init__(self, *args):
                self._real = real_decompressobj(*args)

            def decompress(self, data, max_length=0):
                out = self._real.decompress(data, max_length)
                produced.append(len(out))
                return out

            @property
            def eof(self):
                return self._real.eof

        # Only this module's view of zlib is replaced: urllib3 builds its own
        # decoders from the real one.
        monkeypatch.setattr(bounded, "zlib", types.SimpleNamespace(
            decompressobj=Measured, MAX_WBITS=zlib.MAX_WBITS, error=zlib.error))
        cap = 1000
        response = ping_mod.guarded_session(max_body_bytes=cap).get(base + path, timeout=10)
        assert response.body_truncated and len(response.content) == cap
        assert produced, "the body did not go through the decompressor"
        assert max(produced) <= cap + 1, f"one call inflated {max(produced)} bytes for a cap of {cap}"
        assert sum(produced) <= cap + 1

    def test_the_cap_is_the_callers(self, app):
        base, _stats = app
        response = ping_mod.guarded_session(max_body_bytes=1000).get(base + "/gzip/health", timeout=10)
        assert len(response.content) == 1000 and response.body_truncated

    def test_a_small_compressed_answer_is_decoded(self, app):
        base, _stats = app
        response = ping_mod.guarded_session().get(base + "/smallgzip/health", timeout=5)
        assert response.content == HEALTHY and response.body_truncated is False
        assert deploy.health_response_ok(response.status_code, response.text)

    def test_an_encoding_that_cannot_be_bounded_is_refused(self, app):
        base, _stats = app
        with pytest.raises(requests.exceptions.ContentDecodingError, match="unsupported Content-Encoding"):
            ping_mod.guarded_session().get(base + "/brotli/health", timeout=5)

    def test_the_request_asks_for_an_unencoded_body(self, app):
        seen = {}

        class Echo(_App):
            def do_GET(self):
                seen["accept_encoding"] = self.headers.get("Accept-Encoding")
                self._head(len(HEALTHY))
                self._send(HEALTHY)

        server = _Server(("127.0.0.1", 0), type("E", (Echo,), {"stats": {"sent": 0, "paths": []}}))
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            ping_mod.guarded_session().get(f"http://127.0.0.1:{server.server_address[1]}/health", timeout=5)
        finally:
            server.shutdown()
            server.server_close()
        assert seen["accept_encoding"] == "identity"

    def test_a_cut_health_body_is_not_a_healthy_answer(self, app, monkeypatch):
        """verify_health reads the body through the capped session: an app
        that answers 200 with megabytes is not reported healthy and the
        megabytes are not read."""
        base, stats = app
        monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)
        assert deploy.verify_health(base + "/huge", timeout=1) == "failed"
        time.sleep(0.2)
        polls = stats["paths"].count("/huge/health")
        assert polls >= 1 and stats["sent"] < polls * 8 * MIB

    def test_a_redirect_with_a_large_body_costs_neither_memory_nor_the_answer(self, app):
        base, stats = app
        response = ping_mod.guarded_session().get(base + "/bigredirect", timeout=10)
        assert response.status_code == 200 and response.content == HEALTHY
        assert len(response.history) == 1
        assert len(response.history[0].content) == ping_mod.HEALTH_BODY_MAX_BYTES
        assert response.history[0].body_truncated is True

    def test_page_probes_get_the_page_cap(self, app):
        base, _stats = app
        session = deploy._same_origin_session(base)
        response = session.get(base + "/huge/health", timeout=10)
        assert len(response.content) == ping_mod.PAGE_BODY_MAX_BYTES and response.body_truncated


# ---------------------------------------------------------------------------
# HTTPS: the pin and the bounds on the TLS side
# ---------------------------------------------------------------------------

_OPENSSL_CONFIG = """\
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = pinned.example.com
[v3]
subjectAltName = DNS:pinned.example.com
"""


@pytest.fixture(scope="module")
def throwaway_certificate(tmp_path_factory):
    """A key and a self-signed certificate for pinned.example.com, made for
    this test run in pytest's temp directory and never stored anywhere else."""
    directory = tmp_path_factory.mktemp("tls")
    config = directory / "openssl.cnf"
    config.write_text(_OPENSSL_CONFIG)
    key, certificate = directory / "key.pem", directory / "cert.pem"
    made = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-keyout", str(key), "-out", str(certificate), "-config", str(config)],
        capture_output=True, text=True, timeout=60,
    )
    if made.returncode != 0:
        pytest.skip("openssl could not make a test certificate: " + made.stderr[-200:])
    return str(key), str(certificate)


class _TlsServer(_Server):
    """Counts TCP connections as they arrive, before any TLS, so "nothing
    connected" can be told from "the handshake failed"."""

    def __init__(self, address, handler, context):
        super().__init__(address, handler)
        self.context = context
        self.tcp_connections = []

    def get_request(self):
        sock, address = super().get_request()
        self.tcp_connections.append(address)
        return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), address


class _TlsApp(_App):
    def setup(self):
        try:
            self.request.settimeout(5)
            self.request.do_handshake()
            self.server.handshakes.append(self.request.server_hostname if hasattr(self.request, "server_hostname") else "")
        except (ssl.SSLError, OSError):
            self.server.failed_handshakes.append(True)
        super().setup()

    def handle(self):
        try:
            super().handle()
        except (ssl.SSLError, OSError):
            pass


@pytest.fixture()
def tls_app(throwaway_certificate):
    key, certificate = throwaway_certificate
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    sni = []
    context.sni_callback = lambda _sock, name, _ctx: sni.append(name)
    stats = {"sent": 0, "paths": []}
    server = _TlsServer(("127.0.0.1", 0), type("TlsApp", (_TlsApp,), {"stats": stats}), context)
    server.handshakes, server.failed_handshakes = [], []
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield {"port": server.server_address[1], "stats": stats, "server": server, "sni": sni,
           "ca": certificate}
    server.shutdown()
    server.server_close()


class _dripping_tls_listener:
    """A TCP listener that answers a TLS ClientHello with the start of a
    handshake record, one byte every 0.15 seconds, for as long as the client
    stays. Used as a context manager; yields its port."""

    def __enter__(self):
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self._done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()
        return self._listener.getsockname()[1]

    def _serve(self):
        while not self._done.is_set():
            try:
                peer, _address = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._drip, args=(peer,), daemon=True).start()

    def _drip(self, peer):
        # Handshake record, TLS 1.2, announcing 16 KiB of ServerHello that
        # never completes.
        record = b"\x16\x03\x03\x40\x00" + b"\x02\x00\x3f\xfc" + b"\x00" * 4096
        try:
            peer.settimeout(5)
            peer.recv(4096)
            for byte in record:
                if self._done.wait(0.15):
                    break
                peer.sendall(bytes([byte]))
        except OSError:
            pass
        finally:
            peer.close()

    def __exit__(self, *exc_info):
        self._done.set()
        self._listener.close()
        return False


@pytest.mark.requires_tools("openssl")
class TestHttpsIsPinnedAndBounded:
    def test_an_https_name_that_resolves_to_loopback_is_never_connected_to(self, tls_app, name_service):
        """No policy is relaxed here. The name passes the text check and
        resolves to loopback; over https, as over http, the connection must
        be refused before a single packet reaches the listener."""
        answers, lookups = name_service
        answers["internal.example.com"] = ["127.0.0.1"]
        session = ping_mod.guarded_session()
        with pytest.raises(requests.exceptions.ConnectionError) as refused:
            session.get(f"https://internal.example.com:{tls_app['port']}/health", timeout=3, verify=tls_app["ca"])
        assert ping_mod.refusal_reason(refused.value) == "internal.example.com resolves to a non-public address"
        assert tls_app["server"].tcp_connections == [], "an https probe reached a loopback listener"
        assert lookups == ["internal.example.com"]

    def test_the_https_monitor_ping_refuses_it_as_well(self, tls_app, name_service):
        answers, _lookups = name_service
        answers["internal.example.com"] = ["127.0.0.1"]
        result = ping_mod.ping(f"https://internal.example.com:{tls_app['port']}", timeout=3)
        assert result["error"] == "Refusing to probe URL: internal.example.com resolves to a non-public address"
        assert tls_app["server"].tcp_connections == []

    def test_one_lookup_then_tls_to_the_checked_address_under_the_host_name(
            self, tls_app, name_service, loopback_counts_as_public):
        """With loopback allowed, the pinned connection goes to the address
        that was checked (a second lookup would have answered 10.0.0.5), and
        TLS still names and verifies the HOST: the certificate is for
        pinned.example.com, not for 127.0.0.1."""
        answers, lookups = name_service
        answers["pinned.example.com"] = ["127.0.0.1"]
        session = ping_mod.guarded_session()
        response = session.get(f"https://pinned.example.com:{tls_app['port']}/health",
                               timeout=5, verify=tls_app["ca"])
        assert response.status_code == 200 and response.content == HEALTHY
        assert lookups == ["pinned.example.com"], "a second lookup is a second chance to lie"
        assert tls_app["sni"] == ["pinned.example.com"]
        assert len(tls_app["server"].tcp_connections) == 1

    def test_the_certificate_is_still_checked_against_the_name(
            self, tls_app, name_service, loopback_counts_as_public):
        answers, _lookups = name_service
        answers["other.example.com"] = ["127.0.0.1"]
        session = ping_mod.guarded_session()
        with pytest.raises(requests.exceptions.SSLError):
            session.get(f"https://other.example.com:{tls_app['port']}/health", timeout=5, verify=tls_app["ca"])
        assert tls_app["stats"]["paths"] == [], "a request was sent over a connection whose certificate did not match"

    def test_an_untrusted_certificate_is_refused(self, tls_app, name_service, loopback_counts_as_public):
        answers, _lookups = name_service
        answers["pinned.example.com"] = ["127.0.0.1"]
        with pytest.raises(requests.exceptions.SSLError):
            ping_mod.guarded_session().get(f"https://pinned.example.com:{tls_app['port']}/health", timeout=5)
        assert tls_app["stats"]["paths"] == []

    def test_the_deadline_holds_over_tls(self, tls_app, name_service, loopback_counts_as_public):
        answers, _lookups = name_service
        answers["pinned.example.com"] = ["127.0.0.1"]
        session = ping_mod.guarded_session()
        outcome, took = _timed(lambda: session.get(
            f"https://pinned.example.com:{tls_app['port']}/drip/health", timeout=1, verify=tls_app["ca"]))
        assert isinstance(outcome, requests.exceptions.Timeout), outcome
        assert took < 2.5, f"an https exchange with a 1 second deadline took {took:.1f}s"

    def test_the_byte_cap_holds_over_tls(self, tls_app, name_service, loopback_counts_as_public):
        answers, _lookups = name_service
        answers["pinned.example.com"] = ["127.0.0.1"]
        response = ping_mod.guarded_session().get(
            f"https://pinned.example.com:{tls_app['port']}/gzip/health", timeout=10, verify=tls_app["ca"])
        assert len(response.content) == ping_mod.HEALTH_BODY_MAX_BYTES and response.body_truncated

    def test_a_tls_handshake_that_never_answers_is_inside_the_deadline(self, name_service, loopback_counts_as_public):
        """A listener that accepts the TCP connection and then says nothing:
        the handshake is part of the exchange."""
        answers, _lookups = name_service
        answers["silent.example.com"] = ["127.0.0.1"]
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        try:
            session = ping_mod.guarded_session()
            outcome, took = _timed(lambda: session.get(
                f"https://silent.example.com:{listener.getsockname()[1]}/health", timeout=1))
        finally:
            listener.close()
        assert isinstance(outcome, requests.exceptions.RequestException), outcome
        assert took < 2.5, f"a silent TLS peer held a 1 second exchange for {took:.1f}s"

    def test_a_tls_handshake_that_drips_is_inside_the_deadline(self, name_service, loopback_counts_as_public):
        """A peer that answers the handshake a byte at a time: every byte
        arrives well inside a per-read timeout, and the handshake as a whole
        still has to end with the exchange."""
        answers, _lookups = name_service
        answers["drip.example.com"] = ["127.0.0.1"]
        with _dripping_tls_listener() as port:
            outcome, took = _timed(lambda: ping_mod.guarded_session().get(
                f"https://drip.example.com:{port}/health", timeout=1))
        assert isinstance(outcome, requests.exceptions.RequestException), outcome
        assert took < 2.5, f"a dripping TLS handshake held a 1 second exchange for {took:.1f}s"

    def test_the_certificate_check_ends_with_its_limit_when_the_handshake_drips(
            self, name_service, loopback_counts_as_public, monkeypatch):
        """monitor.diagnose._check_ssl_expiry, the one caller of
        connect_checked: the socket it gets back carries what is left of the
        limit, and the handshake it starts on it ends inside that."""
        import monitor.diagnose as diagnose

        answers, _lookups = name_service
        answers["drip.example.com"] = ["127.0.0.1"]
        with _dripping_tls_listener() as port:
            monkeypatch.setattr(
                diagnose, "connect_checked",
                lambda host, _port, timeout: ping_mod.connect_checked(host, port, timeout=1))
            outcome, took = _timed(lambda: diagnose._check_ssl_expiry("drip.example.com"))
        assert outcome["valid"] is False and outcome["error"], outcome
        assert took < 2.5, f"the certificate check waited {took:.1f}s on a 1 second limit"

    def test_the_handshake_gets_what_the_connect_left_not_a_second_full_timeout(
            self, name_service, loopback_counts_as_public, monkeypatch):
        """A connect that used most of the deadline, then a peer that never
        starts TLS. A socket that kept the timeout it was connected with
        would wait that long again for the handshake."""
        answers, _lookups = name_service
        answers["silent.example.com"] = ["127.0.0.1"]
        real_connect = bounded._urllib3_connection.create_connection

        def slow_connect(address, timeout, **kwargs):
            sock = real_connect(address, timeout, **kwargs)
            _pause(0.7)
            return sock

        monkeypatch.setattr(bounded._urllib3_connection, "create_connection", slow_connect)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        try:
            session = ping_mod.guarded_session()
            outcome, took = _timed(lambda: session.get(
                f"https://silent.example.com:{listener.getsockname()[1]}/health", timeout=1))
        finally:
            listener.close()
        assert isinstance(outcome, requests.exceptions.Timeout), outcome
        assert took < 1.4, f"connect 0.7s + handshake held a 1 second exchange for {took:.2f}s"


# ---------------------------------------------------------------------------
# Provider calls
# ---------------------------------------------------------------------------

class TestProviderCalls:
    def test_provider_post_sends_json_and_returns_the_answer(self, app):
        base, _stats = app
        response = bounded.provider_post(base + "/api/echo", json={"query": "q"},
                                         headers={"Authorization": "Bearer t"}, timeout=5)
        assert response.status_code == 200
        assert response.json() == {"echo": '{"query": "q"}', "auth": "Bearer t"}

    def test_provider_post_ends_at_its_deadline(self, app):
        base, _stats = app
        outcome, took = _timed(lambda: bounded.provider_post(base + "/api/drip", json={}, timeout=1))
        assert isinstance(outcome, requests.exceptions.Timeout), outcome
        assert took < 2.5

    def test_an_answer_over_the_cap_is_an_error_not_half_a_document(self, app):
        base, _stats = app
        with pytest.raises(bounded.ResponseTooLarge):
            bounded.provider_post(base + "/api/large", json={}, timeout=10, max_body_bytes=64 * 1024)

    def test_provider_post_does_not_follow_a_redirect(self, app):
        base, stats = app
        response = bounded.provider_post(base + "/api/moved", json={}, timeout=5)
        assert response.status_code == 302 and stats["paths"] == ["/api/moved"]

    def test_the_railway_call_reports_a_slow_answer_as_an_unknown_outcome(self, app, monkeypatch):
        """deploy.gql through the real bounded exchange: a mutation whose
        answer does not arrive inside the deadline is not sent again."""
        base, stats = app
        monkeypatch.setattr(deploy, "RAILWAY_API", base + "/api/drip")
        monkeypatch.setattr(deploy, "RAILWAY_CALL_DEADLINE_S", 1)
        monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)
        outcome, took = _timed(lambda: deploy.gql("mutation Create { x }", token="not-" + "a-token"))
        assert isinstance(outcome, deploy.RailwayOutcomeUnknown), outcome
        assert took < 2.5 and stats["paths"] == ["/api/drip"]

    def test_a_railway_query_stops_retrying_at_the_callers_deadline(self, app, monkeypatch):
        base, stats = app
        monkeypatch.setattr(deploy, "RAILWAY_API", base + "/api/drip")
        outcome, took = _timed(lambda: deploy.gql(
            "query Q { x }", token="not-" + "a-token", deadline=time.monotonic() + 1.5))
        assert isinstance(outcome, requests.exceptions.RequestException), outcome
        assert took < 3.5, f"a query with 1.5 seconds left ran for {took:.1f}s"
        assert len(stats["paths"]) == 1, "no second attempt fits in the time that was left"

    def test_bounded_urlopen_returns_status_and_body(self, app):
        base, _stats = app
        request = urllib.request.Request(base + "/api/echo", data=b'{"a": 1}', method="POST",
                                         headers={"Authorization": "Bearer t", "Content-Type": "application/json"})
        with bounded.bounded_urlopen(request, timeout=5) as answer:
            assert answer.status == 200
            assert json.loads(answer.read()) == {"echo": '{"a": 1}', "auth": "Bearer t"}

    def test_bounded_urlopen_reports_an_error_status_the_way_urllib_does(self, app):
        base, _stats = app
        request = urllib.request.Request(base + "/api/teapot", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as refused:
            bounded.bounded_urlopen(request, timeout=5)
        assert refused.value.code == 418
        assert json.loads(refused.value.read()) == {"error": "short and stout"}

    def test_bounded_urlopen_reports_a_timeout_and_an_oversized_answer_as_url_errors(self, app):
        base, _stats = app
        slow = urllib.request.Request(base + "/api/drip", data=b"{}", method="POST")
        outcome, took = _timed(lambda: bounded.bounded_urlopen(slow, timeout=1))
        assert isinstance(outcome, urllib.error.URLError) and took < 2.5
        large = urllib.request.Request(base + "/api/large", data=b"{}", method="POST")
        with pytest.raises(urllib.error.URLError, match="larger than"):
            bounded.bounded_urlopen(large, timeout=10, max_body_bytes=64 * 1024)

    def test_environment_proxies_are_not_used(self, monkeypatch, app):
        base, stats = app
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")   # the discard port: nothing answers
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        response = bounded.provider_post(base + "/api/echo", json={}, timeout=5)
        assert response.status_code == 200 and stats["paths"] == ["/api/echo"]

    def test_an_explicit_proxy_is_refused(self, app):
        base, _stats = app
        session = bounded.BoundedSession()
        with pytest.raises(requests.exceptions.ProxyError):
            session.get(base + "/health", timeout=5, proxies={"http": "http://127.0.0.1:9"})


# ---------------------------------------------------------------------------
# No call site goes around the bounded exchange
# ---------------------------------------------------------------------------

_SCANNED = [
    path for folder in ("monitor", "queue", "coordinator", "ops")
    for path in sorted((ROOT / folder).rglob("*.py"))
] + [ROOT / "cli" / "deploy.py"]

# The unbounded ways to make a request, by dotted call name.
_UNBOUNDED_CALLS = {
    "requests.get", "requests.post", "requests.put", "requests.patch", "requests.delete",
    "requests.head", "requests.request", "requests.Session", "requests.session",
    "urllib.request.urlopen", "urlopen", "http.client.HTTPConnection", "http.client.HTTPSConnection",
    "HTTPConnection", "HTTPSConnection", "socket.create_connection", "urllib3.PoolManager",
    "PoolManager", "guarded_urlopen", "url_guard.guarded_urlopen", "_url_guard.guarded_urlopen",
}
# Where one of those names is allowed, and why.
_ALLOWED_UNBOUNDED = {
    ("monitor/ping.py", "socket.create_connection"):
        "connect_checked: the certificate check's raw socket, under its own deadline",
}


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    parts.append(node.id if isinstance(node, ast.Name) else "()")
    return ".".join(reversed(parts))


class TestEveryOutboundCallIsBounded:
    def test_no_module_makes_a_request_with_an_unbounded_client(self):
        found = set()
        for path in _SCANNED:
            rel = path.relative_to(ROOT).as_posix()
            if rel == "monitor/bounded_fetch.py":
                continue   # the bounded exchange itself is built on these
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and _dotted(node.func) in _UNBOUNDED_CALLS:
                    found.add((rel, _dotted(node.func)))
        assert found == set(_ALLOWED_UNBOUNDED), (
            "a request is made without the bounded exchange (use monitor.ping.guarded_session for a "
            "customer app, monitor.bounded_fetch.provider_post or bounded_urlopen for a provider): "
            + repr(sorted(found - set(_ALLOWED_UNBOUNDED)))
        )

    def test_no_module_shells_out_to_an_http_client(self):
        offenders = []
        for path in _SCANNED:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and node.value in ("curl", "wget"):
                    offenders.append(path.relative_to(ROOT).as_posix())
        assert offenders == []

    def test_every_model_client_is_given_a_timeout_and_a_retry_limit(self):
        """The Anthropic SDK's defaults are ten minutes per request and two
        retries. Each client built in these modules states its own."""
        clients = []
        for path in _SCANNED:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and _dotted(node.func) in ("anthropic.Anthropic", "Anthropic"):
                    keywords = {keyword.arg for keyword in node.keywords}
                    clients.append((path.relative_to(ROOT).as_posix(), {"timeout", "max_retries"} <= keywords))
        assert clients, "the scan found no model client at all"
        assert [rel for rel, bounded_client in clients if not bounded_client] == []

    def test_every_guarded_session_request_is_given_a_deadline(self):
        """session.get/post/head on a probe session without timeout= would
        fall back to the default deadline; call sites state their own."""
        missing = []
        for path in _SCANNED:
            rel = path.relative_to(ROOT).as_posix()
            if rel == "monitor/bounded_fetch.py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in ("get", "post", "head") \
                        and "session" in _dotted(node.func).lower() \
                        and "timeout" not in {keyword.arg for keyword in node.keywords}:
                    missing.append((rel, node.lineno))
        assert missing == []
