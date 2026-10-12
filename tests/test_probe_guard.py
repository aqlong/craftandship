"""The probes the worker and the monitor send to deployed sites.

Three things are checked here, each against a real listener on loopback (no
request leaves this machine; the root conftest's network guard is in force):

  * The URL guard reads a URL the way the sender does. A URL that the
    standard library and urllib3 read differently is refused, and the proof is
    on the wire: the old guard let "http://127.0.0.1:<port>\\.evil.example"
    through and the request arrived at the loopback listener.
  * The guarded session resolves a name once, refuses unless every address is
    public, and connects to the address it checked. The lookups are counted.
  * The post-deploy login probe signs in with the worker's own verification
    token by POST, so the token never appears in a URL (and therefore in no
    access log) and is used up afterwards.

Also here: the monitor loop survives a database error, and destroy_project
tells the truth about a delete that did not happen.
"""

import hashlib
import http.server
import secrets
import socket
import sys
import threading
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from urllib3.util import parse_url

ROOT = Path(__file__).parent.parent
for _p in (str(ROOT / "queue"), str(ROOT / "cli"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import deploy
import monitor.ping as ping_mod

BS = chr(92)  # one backslash, spelled so that no tool can turn it into an escape


# ---------------------------------------------------------------------------
# Loopback listeners
# ---------------------------------------------------------------------------

class _Recorder(http.server.BaseHTTPRequestHandler):
    """Answers 200 "ok" to anything and records what it was asked."""

    seen = None  # list of (method, path, Host header), shared per server class

    def _record(self):
        self.seen.append((self.command, self.path, self.headers.get("Host")))

    def do_GET(self):
        self._record()
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self._record()
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def _serve(handler_cls):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture()
def listener():
    seen = []
    server = _serve(type("Listener", (_Recorder,), {"seen": seen}))
    yield server.server_address[1], seen
    server.shutdown()
    server.server_close()


@pytest.fixture()
def loopback_counts_as_public(monkeypatch):
    """Let the address policy accept loopback, so that the rest of the guard
    (one lookup, pinned connection, per-hop URL check) can be exercised
    against a listener on this machine. Every other address keeps its real
    verdict."""
    real = ping_mod.is_public_address
    monkeypatch.setattr(
        ping_mod, "is_public_address",
        lambda ip: ip in ("127.0.0.1", "::1") or real(ip),
    )


@pytest.fixture()
def name_service(monkeypatch):
    """Answers for chosen host names, with a count of the lookups made.

    usage: answers["app.example.com"] = ["127.0.0.1"]   (a list per lookup is
    also accepted: [["127.0.0.1"], ["10.0.0.5"]] answers differently the
    second time). Other names go to the resolver that was in place."""
    answers = {}
    lookups = []
    previous = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host in answers:
            lookups.append(host)
            configured = answers[host]
            if configured and isinstance(configured[0], list):
                current = configured[min(lookups.count(host), len(configured)) - 1]
            else:
                current = configured
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))
                    for address in current]
        return previous(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return answers, lookups


# ---------------------------------------------------------------------------
# The URL guard reads what the sender reads
# ---------------------------------------------------------------------------

DIFFERENTIALS = [
    # urlsplit: one long host name. urllib3 and requests: host 127.0.0.1.
    "http://127.0.0.1" + BS + ".evil.example",
    "http://127.0.0.1" + BS + "@evil.example/",
    "http://evil.example" + BS + "@127.0.0.1/",
    "http://127.0.0.1:80" + BS + "@evil.example/",
    # urllib3 percent-decodes a host; urlsplit does not.
    "http://127.0.0.%31/",
    "http://%6c%6f%63%61%6c%68%6f%73%74.%6c%6f%63%61%6c/",
    "http://exa%6dple.com/",
    # Whitespace and control characters: one parser strips, another refuses.
    "http://127.0.0.1\t.evil.example/",
    "http://127.0.0.1 .evil.example/",
    " http://127.0.0.1/",
    "http://app.example.com/\n",
    # A host outside ASCII is rewritten (IDNA) by the sender only.
    "http://ex" + chr(0x430) + "mple.com/",
    # Not a host name at all.
    "http://my_app.example.com/",
]


class TestUrlGuardReadsWhatTheSenderReads:
    @pytest.mark.parametrize("url", DIFFERENTIALS)
    def test_urls_that_parsers_disagree_about_are_refused(self, url):
        assert ping_mod.probe_url_problem(url), url

    @pytest.mark.parametrize("url", DIFFERENTIALS + [
        "https://app.up.railway.app", "http://example.com/", "https://sub.example.org:8443/x",
        "https://8.8.8.8", "HTTP://APP.Example.com./health", "http://evil.example#@127.0.0.1/",
        "http://127.0.0.1#@evil.example/", "http://app.example.com:80@127.0.0.1/",
        "http://[::ffff:127.0.0.1]/", "http://2130706433/", "http://localhost./",
    ])
    def test_whatever_the_guard_accepts_is_where_requests_would_connect(self, url):
        """The property the guard exists for: if it says yes, the host it
        looked at is the host the request goes to."""
        if ping_mod.probe_url_problem(url) is not None:
            return
        prepared = requests.Request("GET", url).prepare()
        sent_host = (parse_url(prepared.url).host or "").strip("[]").lower()
        checked_host = (urllib.parse.urlsplit(url).hostname or "").lower()
        assert sent_host == checked_host
        assert ping_mod.probe_url_problem(prepared.url) is None

    @pytest.mark.parametrize("url", [
        "https://myapp.up.railway.app", "http://example.com/", "https://sub.example.org:8443/x",
        "https://8.8.8.8", "HTTP://APP.Example.com./health",
    ])
    def test_ordinary_public_urls_still_pass(self, url):
        assert ping_mod.probe_url_problem(url) is None


class TestTheBackslashBypassOnTheWire:
    """No policy is relaxed in this class: these are the real guard and the
    real probes against a listener on loopback, which they must never reach."""

    def test_the_monitor_ping_does_not_reach_loopback(self, listener):
        port, seen = listener
        url = f"http://127.0.0.1:{port}" + BS + ".evil.example"
        result = ping_mod.ping(url, timeout=3)
        assert seen == [], f"the probe arrived on loopback: {seen}"
        assert result["ok"] is False and result["error"].startswith("Refusing to probe URL")

    def test_the_deploy_health_probe_does_not_reach_loopback(self, listener):
        port, seen = listener
        url = f"http://127.0.0.1:{port}" + BS + ".evil.example"
        with patch("deploy.time.sleep"):
            assert deploy.verify_health(url, timeout=1) == "failed"
        assert seen == [], f"the probe arrived on loopback: {seen}"

    def test_a_percent_encoded_loopback_host_does_not_reach_loopback(self, listener):
        port, seen = listener
        result = ping_mod.ping(f"http://127.0.0.%31:{port}", timeout=3)
        assert seen == [] and result["ok"] is False


# ---------------------------------------------------------------------------
# The guarded session: one lookup, checked, pinned
# ---------------------------------------------------------------------------

class TestGuardedSessionPinsTheAddress:
    def test_a_public_looking_name_that_resolves_to_loopback_is_not_contacted(self, listener, name_service):
        """The requests-based probes used to resolve nothing: the name passed
        the text check and requests connected wherever it resolved."""
        port, seen = listener
        answers, lookups = name_service
        answers["internal.example.com"] = ["127.0.0.1"]

        result = ping_mod.ping(f"http://internal.example.com:{port}", timeout=3)

        assert seen == [], f"the probe arrived on loopback: {seen}"
        assert result["ok"] is False
        assert result["error"] == (
            "Refusing to probe URL: internal.example.com resolves to a non-public address")
        assert lookups == ["internal.example.com"]

    def test_the_deploy_health_probe_does_not_follow_a_name_to_loopback(self, listener, name_service):
        port, seen = listener
        answers, _lookups = name_service
        answers["internal.example.com"] = ["127.0.0.1"]
        with patch("deploy.time.sleep"):
            assert deploy.verify_health(f"http://internal.example.com:{port}", timeout=1) == "infra"
        assert seen == []

    def test_one_public_and_one_private_answer_is_refused(self, listener, name_service):
        port, seen = listener
        answers, _lookups = name_service
        answers["mixed.example.com"] = ["93.184.216.34", "127.0.0.1"]
        session = ping_mod.guarded_session()
        with pytest.raises(requests.exceptions.ConnectionError) as refused:
            session.get(f"http://mixed.example.com:{port}/health", timeout=3)
        assert ping_mod.refusal_reason(refused.value) == "mixed.example.com resolves to a non-public address"
        assert seen == []

    def test_the_name_is_resolved_once_and_the_checked_address_is_used(
            self, listener, name_service, loopback_counts_as_public):
        """A DNS server that answers with an acceptable address for the check
        and a private one for the connection (rebinding) gets one question."""
        port, seen = listener
        answers, lookups = name_service
        answers["pinned.example.com"] = [["127.0.0.1"], ["10.0.0.5"]]

        response = ping_mod.guarded_session().get(
            f"http://pinned.example.com:{port}/health", timeout=3, allow_redirects=False)

        assert response.status_code == 200
        assert lookups == ["pinned.example.com"], "a second lookup is a second chance to lie"
        assert seen == [("GET", "/health", f"pinned.example.com:{port}")]

    def test_every_redirect_hop_is_checked(self, name_service, loopback_counts_as_public):
        """The first hop is acceptable; it redirects to a literal private
        address, which the session refuses before sending anything."""
        answers, _lookups = name_service
        answers["app.example.com"] = ["127.0.0.1"]

        class Redirector(_Recorder):
            def do_GET(self):
                self._record()
                self.send_response(302)
                self.send_header("Location", "http://10.0.0.5/latest/meta-data")
                self.send_header("Content-Length", "0")
                self.end_headers()

        seen = []
        server = _serve(type("R", (Redirector,), {"seen": seen}))
        try:
            port = server.server_address[1]
            with pytest.raises(requests.exceptions.InvalidURL, match="probe refused: non-public address"):
                ping_mod.guarded_session().get(f"http://app.example.com:{port}/start", timeout=3)
            assert [entry[1] for entry in seen] == ["/start"]
        finally:
            server.shutdown()
            server.server_close()

    def test_environment_proxies_are_not_used(self, monkeypatch):
        """Through a proxy the proxy resolves the name, out of reach of the check."""
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:3128")
        session = ping_mod.guarded_session()
        assert session.trust_env is False
        assert session.merge_environment_settings(
            "https://app.example.com/", {}, None, None, None)["proxies"] == {}

    def test_the_certificate_check_socket_is_checked_too(self, listener, name_service):
        port, seen = listener
        answers, _lookups = name_service
        answers["internal.example.com"] = ["127.0.0.1"]
        with pytest.raises(ping_mod.ProbeDestinationRefused):
            ping_mod.connect_checked("internal.example.com", port, timeout=3)
        from monitor import diagnose
        info = diagnose._check_ssl_expiry("internal.example.com")
        assert info["valid"] is False and "non-public" in info["error"]
        assert seen == []


# ---------------------------------------------------------------------------
# The post-deploy login probe
# ---------------------------------------------------------------------------

class _GeneratedSite(http.server.BaseHTTPRequestHandler):
    """The login routes of a generated site, as the probe meets them.

    GET /login hands out a session cookie and a form with the session's CSRF
    value. POST /magic/consume (token and _csrf in the body) uses a token up
    and redirects; an unknown or used token is sent back to /login. GET and
    HEAD /magic accept the token in the query string without using it up,
    which is what the old probe relied on. Every request line is appended to
    an access log, query string included, the way the site's HTTP logger
    writes it.
    """

    state = None  # dict: tokens {sha256: used?}, sessions {sid: csrf}, access_log [str], consume_route bool

    def _log_line(self):
        self.state["access_log"].append(f"{self.command} {self.path}")

    def _cookie(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "sid":
                return value
        return None

    def _token_state(self, raw):
        return self.state["tokens"].get(hashlib.sha256(raw.encode()).hexdigest())

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._log_line()
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/login":
            sid = secrets.token_hex(8)
            csrf = secrets.token_hex(16)
            self.state["sessions"][sid] = csrf
            body = (f'<form method="post" action="/login"><input type="hidden" name="_csrf" '
                    f'value="{csrf}"><input name="email"></form>').encode()
            self.send_response(200)
            self.send_header("Set-Cookie", f"sid={sid}; Path=/; HttpOnly")
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/magic":
            token = urllib.parse.parse_qs(url.query).get("token", [""])[0]
            if self._token_state(token) is False:
                body = b"<form method=post action=/magic/consume>Sign in</form>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._redirect("/login?error=invalid-token")
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self._log_line()
        url = urllib.parse.urlsplit(self.path)
        token = urllib.parse.parse_qs(url.query).get("token", [""])[0]
        self.send_response(200 if url.path == "/magic" and self._token_state(token) is False else 410)
        self.end_headers()

    def do_POST(self):
        self._log_line()
        length = int(self.headers.get("Content-Length", "0"))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if self.path != "/magic/consume" or not self.state["consume_route"]:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        expected_csrf = self.state["sessions"].get(self._cookie())
        if not expected_csrf or form.get("_csrf", [""])[0] != expected_csrf:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        token = form.get("token", [""])[0]
        digest = hashlib.sha256(token.encode()).hexdigest()
        if self.state["tokens"].get(digest) is False:
            self.state["tokens"][digest] = True
            return self._redirect("/set-password")
        if self.state["tokens"].get(digest) is True:
            return self._redirect("/login?error=token-already-used")
        return self._redirect("/login?error=invalid-token")

    def log_message(self, *args):
        pass


@pytest.fixture()
def generated_site(loopback_counts_as_public):
    # The same two tokens the worker seeds: one to verify with, one for the customer.
    sys.path.insert(0, str(ROOT / "queue"))
    import worker_pg
    verify = worker_pg._generate_magic_link_token()
    customer = worker_pg._generate_magic_link_token()
    state = {
        "tokens": {verify["token_hash"]: False, customer["token_hash"]: False},
        "sessions": {}, "access_log": [], "consume_route": True,
    }
    server = _serve(type("Site", (_GeneratedSite,), {"state": state}))
    yield {
        "base": f"http://127.0.0.1:{server.server_address[1]}",
        "state": state, "verify": verify, "customer": customer,
    }
    server.shutdown()
    server.server_close()


class TestLoginProbeLeavesNoUsableToken:
    def test_the_probe_signs_in_and_the_token_is_in_no_log_line(self, generated_site):
        site = generated_site
        raw = site["verify"]["raw_token"]

        assert deploy.verify_magic_link_auth(site["base"], raw, timeout=5) == "ok"

        log = site["state"]["access_log"]
        assert log == ["GET /login", "POST /magic/consume"], log
        assert not any(raw in line for line in log), "a live login token was written to the access log"
        assert site["state"]["tokens"][site["verify"]["token_hash"]] is True, "the token was not used up"
        assert site["state"]["tokens"][site["customer"]["token_hash"]] is False, (
            "the customer's own token must still work")

    def test_after_the_probe_the_token_is_dead(self, generated_site):
        site = generated_site
        raw = site["verify"]["raw_token"]
        assert deploy.verify_magic_link_auth(site["base"], raw, timeout=5) == "ok"
        assert deploy.verify_magic_link_auth(site["base"], raw, timeout=5) == "failed"
        # ... also for the non-consuming check the old probe used.
        head = requests.head(f"{site['base']}/magic", params={"token": raw}, timeout=5)
        assert head.status_code == 410

    def test_a_token_the_site_does_not_know_fails_the_probe(self, generated_site):
        site = generated_site
        assert deploy.verify_magic_link_auth(site["base"], secrets.token_hex(32), timeout=5) == "failed"
        assert all(value is False for value in site["state"]["tokens"].values())

    def test_a_site_without_the_consume_route_fails_the_probe(self, generated_site):
        site = generated_site
        site["state"]["consume_route"] = False
        raw = site["verify"]["raw_token"]
        assert deploy.verify_magic_link_auth(site["base"], raw, timeout=5) == "failed"
        assert not any(raw in line for line in site["state"]["access_log"])

    def test_the_probe_is_skipped_without_a_token_or_when_disabled(self, generated_site, monkeypatch):
        site = generated_site
        assert deploy.verify_magic_link_auth(site["base"], None) == "skipped"
        monkeypatch.setenv("USE_MAGIC_LINK_AUTH", "false")
        assert deploy.verify_magic_link_auth(site["base"], site["verify"]["raw_token"]) == "skipped"
        assert site["state"]["access_log"] == []

    def test_an_internal_base_url_is_refused_without_a_request(self, listener):
        """No policy relaxed here: loopback is refused before anything is sent."""
        port, seen = listener
        assert deploy.verify_magic_link_auth(f"http://127.0.0.1:{port}", "ab" * 32, timeout=2) == "failed"
        assert seen == []


# ---------------------------------------------------------------------------
# The monitor loop
# ---------------------------------------------------------------------------

class _StopTheLoop(Exception):
    pass


class TestMonitorLoopSurvivesADatabaseError:
    def test_a_failed_registry_read_is_retried_with_backoff(self):
        """list_apps() raised on a database restart and, being outside any
        try, ended run_checks for good: no health checks until the worker was
        restarted. It is now logged and retried after 30s, then 60s."""
        import psycopg2

        waits = []

        def fake_sleep(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                raise _StopTheLoop()

        reads = MagicMock(side_effect=[
            psycopg2.OperationalError("the database system is starting up"),
            psycopg2.OperationalError("connection refused"),
            [],
        ])
        with patch("monitor.registry.list_apps", reads), \
             patch.object(ping_mod.time, "sleep", fake_sleep):
            with pytest.raises(_StopTheLoop):
                ping_mod.run_checks(interval_seconds=900)

        assert reads.call_count == 3
        assert waits[:2] == [30.0, 60.0]
        assert 890 < waits[2] <= 900, "after a good cycle the normal interval applies again"

    def test_the_backoff_never_exceeds_the_interval(self):
        waits = []

        def fake_sleep(seconds):
            waits.append(seconds)
            if len(waits) == 6:
                raise _StopTheLoop()

        with patch("monitor.registry.list_apps", side_effect=RuntimeError("db down")), \
             patch.object(ping_mod.time, "sleep", fake_sleep):
            with pytest.raises(_StopTheLoop):
                ping_mod.run_checks(interval_seconds=100)
        assert waits == [30.0, 60.0, 100.0, 100.0, 100.0, 100.0]

    def test_the_loop_ends_when_asked_to(self):
        stop = threading.Event()
        calls = []

        def read():
            calls.append(1)
            stop.set()
            return []

        with patch("monitor.registry.list_apps", read):
            ping_mod.run_checks(interval_seconds=900, stop_event=stop)
        assert calls == [1]

    def test_the_worker_restarts_a_monitor_loop_that_ended(self, monkeypatch):
        """The thread wrapper in the worker used to log "crashed" and return."""
        sys.path.insert(0, str(ROOT / "queue"))
        import worker_pg

        class FastEvent:
            def __init__(self):
                self.flag = False
                self.waits = []

            def is_set(self):
                return self.flag

            def wait(self, seconds=None):
                self.waits.append(seconds)
                return self.flag

        event = FastEvent()
        runs = []

        def run_checks(interval_seconds, stop_event=None):
            runs.append(interval_seconds)
            if len(runs) == 1:
                raise RuntimeError("monitor blew up")
            event.flag = True

        monkeypatch.setattr(worker_pg, "_shutdown_event", event)
        monkeypatch.setattr(ping_mod, "run_checks", run_checks)
        started = []
        real_thread = threading.Thread

        def capture_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            started.append(thread)
            return thread

        monkeypatch.setattr(worker_pg.threading, "Thread", capture_thread)
        worker_pg._start_monitor_thread()
        started[0].join(10)

        assert not started[0].is_alive()
        assert len(runs) == 2, "the loop was not started again after it crashed"
        assert event.waits == [60.0]


# ---------------------------------------------------------------------------
# destroy_project tells the truth
# ---------------------------------------------------------------------------

class TestDestroyProjectIsTruthful:
    def _gql(self, delete_result):
        def fake(query, variables=None, token=None):
            if "projectDelete" in query:
                if isinstance(delete_result, Exception):
                    raise delete_result
                return delete_result
            return {"project": {"id": variables["id"], "name": "customer-site"}}
        return fake

    def test_a_confirmed_delete_returns_true(self, monkeypatch):
        monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
        with patch.object(deploy, "gql", self._gql({"projectDelete": True})):
            assert deploy.destroy_project("proj-1", "tok") is True

    @pytest.mark.parametrize("outcome", [
        RuntimeError("GraphQL error: Not Authorized"),
        requests.exceptions.ConnectionError("network down"),
        {"projectDelete": False},
        {},
        None,
    ])
    def test_a_delete_that_did_not_happen_raises(self, monkeypatch, outcome):
        """The failed delete used to be printed and swallowed; every caller
        then logged "deleted" and dropped the project from the monitor."""
        monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
        with patch.object(deploy, "gql", self._gql(outcome)):
            with pytest.raises(RuntimeError, match="destroy_project FAILED"):
                deploy.destroy_project("proj-1", "tok")


# ---------------------------------------------------------------------------
# The browser that renders customer content
# ---------------------------------------------------------------------------

class TestBrowserGetsAScrubbedEnvironment:
    @pytest.mark.parametrize("probe", ["capture_deploy_screenshots", "verify_render"])
    def test_chromium_is_launched_without_the_workers_secrets(self, probe, tmp_path, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "not-a-token-" + secrets.token_hex(4))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-key-" + secrets.token_hex(4))
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/none")
        monkeypatch.setenv("USE_MAGIC_LINK_AUTH", "true")
        launched = {}

        class Chromium:
            def launch(self, **kwargs):
                launched.update(kwargs)
                return MagicMock()

        class Playwright:
            chromium = Chromium()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch("playwright.sync_api.sync_playwright", return_value=Playwright()):
            getattr(deploy, probe)("https://app.example.com", {}, str(tmp_path))

        env = launched.get("env")
        assert env is not None, "the browser inherited the worker's whole environment"
        assert not any(name in env for name in ("RAILWAY_API_TOKEN", "ANTHROPIC_API_KEY", "DATABASE_URL"))
        assert "PATH" in env
