"""Security behavior of cli/deploy.py (used by both build engines).

Covers what a component-by-component review tends to miss in the Railway
deploy path:

  * the platform mail key is never copied into a customer app;
  * error text built from Railway responses and Railway CLI output cannot carry
    the API token, the project token or the customer app's generated secrets;
  * the Railway CLI child process does not inherit the worker's credentials;
  * the post-deploy probes cannot be pointed at internal addresses: not by the
    base URL, not by a spec-supplied route path, not by a redirect, and not by
    what the page loads inside the worker's browser;
  * project deletion fails closed when the protected-name check cannot run.

No network: Railway calls are mocked, and the redirect tests use two HTTP
servers on loopback.
"""

import http.server
import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

ROOT = Path(__file__).parent.parent
for _entry in (str(ROOT), str(ROOT / "cli")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import deploy
from tests._guarded_session_mock import patched_probe_get

# Test values are assembled at runtime so that nothing in this file has the
# shape of a real credential.
_API_TOKEN = "railway-account-" + "t" * 20
_PROJECT_TOKEN = "railway-project-" + "p" * 20
_PG_PASSWORD = "pg-" + "w" * 24
_SESSION_SECRET = "session-" + "s" * 24
_DB_URL = "postgresql://railway:" + _PG_PASSWORD + "@postgres.railway.internal:5432/railway"

# Names only; the values are deliberately dull.
_WORKER_ENV = {
    "ANTHROPIC_API_KEY": "dummy-value",
    "ANTHROPIC_ADMIN_KEY": "dummy-value",
    "RAILWAY_API_TOKEN": "dummy-value",
    "RAILWAY_PROJECT_ID": "platform-project",
    "RESEND_API_KEY": "dummy-value",
    "GITHUB_TOKEN": "dummy-value",
    "SENTRY_DSN": "dummy-value",
    "OPS_API_KEY": "dummy-value",
    "STRIPE_SECRET_KEY": "dummy-value",
    "DATABASE_URL": "postgres://worker@localhost:5432/worker_main",
}
_KEPT_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp/home", "LANG": "C"}


def _response(status_code=200, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body
    return resp


# ---------------------------------------------------------------------------
# Mail key
# ---------------------------------------------------------------------------

class TestGeneratedAppMailKey:
    def test_platform_key_is_not_handed_to_the_app(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "platform-mail-key-value")
        monkeypatch.delenv(deploy.GENERATED_APP_MAIL_KEY_ENV, raising=False)
        assert deploy._generated_app_mail_env() == {}

    def test_dedicated_key_is_what_the_app_receives(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "platform-mail-key-value")
        monkeypatch.setenv(deploy.GENERATED_APP_MAIL_KEY_ENV, "app-only-mail-key-value")
        assert deploy._generated_app_mail_env() == {"RESEND_API_KEY": "app-only-mail-key-value"}

    def test_blank_dedicated_key_means_no_key(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "platform-mail-key-value")
        monkeypatch.setenv(deploy.GENERATED_APP_MAIL_KEY_ENV, "   ")
        assert deploy._generated_app_mail_env() == {}

    @pytest.mark.parametrize("app_value", ["one-shared-mail-key-value", "  one-shared-mail-key-value  "])
    def test_reusing_the_platform_key_is_refused(self, monkeypatch, capsys, app_value):
        """Setting the app variable to the platform's own key must not hand
        that key over: the app gets no mail key at all, and the operator is
        told why (without the key being printed)."""
        monkeypatch.setenv("RESEND_API_KEY", "one-shared-mail-key-value")
        monkeypatch.setenv(deploy.GENERATED_APP_MAIL_KEY_ENV, app_value)
        assert deploy._generated_app_mail_env() == {}
        out = capsys.readouterr().out
        assert "Refused" in out and "one-shared-mail-key-value" not in out

    @pytest.mark.parametrize("app_key, expect_key", [
        ("one-shared-mail-key-value", False),   # same as the platform key: refused
        ("app-only-mail-key-value", True),      # a separate key: passed on
    ])
    def test_variables_sent_to_the_customer_service(self, monkeypatch, tmp_path, app_key, expect_key):
        """Through deploy() itself: what reaches Railway as the customer app's
        environment. The platform key must not be among the values."""
        monkeypatch.setenv("RESEND_API_KEY", "one-shared-mail-key-value")
        monkeypatch.setenv(deploy.GENERATED_APP_MAIL_KEY_ENV, app_key)
        build_dir = tmp_path / "app"
        build_dir.mkdir()
        (build_dir / "package.json").write_text('{"name": "app"}')
        sent = {}

        def capture_env(project_id, environment_id, service_id, variables, token):
            sent.update(variables)

        with patch("deploy.check_railway_cli"), patch("deploy.get_token", return_value="t"), \
             patch("deploy.create_project", return_value="proj-1"), \
             patch("deploy.get_environment_id", return_value="env-1"), \
             patch("deploy.create_service", return_value="svc-1"), \
             patch("deploy.provision_postgres", return_value=("db-1", _DB_URL)), \
             patch("deploy.set_env_vars", side_effect=capture_env), \
             patch("deploy.deploy_source", return_value="dep-1"), \
             patch("deploy.wait_for_deployment"), \
             patch("deploy.generate_domain", return_value="app.example.test"), \
             patch("deploy.verify_health", return_value="ok"), \
             patch("deploy.verify_auth", return_value="ok"), \
             patch("builtins.print"):
            deploy.deploy(str(build_dir))

        assert sent["DATABASE_URL"] == _DB_URL
        assert "one-shared-mail-key-value" not in sent.values()
        if expect_key:
            assert sent["RESEND_API_KEY"] == "app-only-mail-key-value"
        else:
            assert "RESEND_API_KEY" not in sent


class TestPasswordLoginProbe:
    """deploy() runs verify_auth (a POST of the v1 seed password to /login)
    only when asked to. The worker does not ask for it on v2 apps, which have
    no seeded password."""

    def _deploy(self, tmp_path, **kwargs):
        build_dir = tmp_path / "app"
        build_dir.mkdir()
        (build_dir / "package.json").write_text('{"name": "app"}')
        with patch("deploy.check_railway_cli"), patch("deploy.get_token", return_value="t"), \
             patch("deploy.create_project", return_value="proj-1"), \
             patch("deploy.get_environment_id", return_value="env-1"), \
             patch("deploy.create_service", return_value="svc-1"), \
             patch("deploy.provision_postgres", return_value=("db-1", _DB_URL)), \
             patch("deploy.set_env_vars"), \
             patch("deploy.deploy_source", return_value="dep-1"), \
             patch("deploy.wait_for_deployment"), \
             patch("deploy.generate_domain", return_value="app.example.test"), \
             patch("deploy.verify_health", return_value="ok"), \
             patch("deploy.verify_auth", return_value="warning") as verify_auth, \
             patch("builtins.print"):
            result = deploy.deploy(str(build_dir), **kwargs)
        return result, verify_auth

    def test_a_healthy_v2_app_is_reported_healthy_and_is_not_sent_a_password(self, tmp_path):
        result, verify_auth = self._deploy(tmp_path, password_login_probe=False)
        verify_auth.assert_not_called()
        assert result["health_check"] == "ok"

    def test_the_default_still_probes_and_fails_health_on_a_bad_login(self, tmp_path):
        """v1 behavior, unchanged: the probe runs and a result other than "ok"
        turns the health check into "failed"."""
        result, verify_auth = self._deploy(tmp_path)
        verify_auth.assert_called_once()
        assert result["health_check"] == "failed"

    def test_deploy_reads_no_other_mail_variable(self):
        """deploy() takes mail settings from _generated_app_mail_env() only."""
        import inspect
        source = inspect.getsource(deploy.deploy)
        assert "_generated_app_mail_env()" in source
        assert "RESEND_API_KEY" not in source


# ---------------------------------------------------------------------------
# Error text from Railway
# ---------------------------------------------------------------------------

class TestRailwayErrorTextIsScrubbed:
    _VARIABLES = {
        "input": {
            "projectId": "proj-1",
            "variables": {
                "NODE_ENV": "production",
                "DATABASE_URL": _DB_URL,
                "SESSION_SECRET": _SESSION_SECRET,
                "POSTGRES_PASSWORD": _PG_PASSWORD,
            },
        }
    }

    def _echo(self):
        """What a GraphQL server says when it rejects the variables it was sent."""
        return 'Variable "$input" got invalid value ' + repr(self._VARIABLES) + f" (Bearer {_API_TOKEN})"

    def _assert_clean(self, message):
        for secret in (_API_TOKEN, _PG_PASSWORD, _SESSION_SECRET, _DB_URL):
            assert secret not in message
        assert "***" in message

    def test_http_4xx_body_is_scrubbed(self):
        resp = _response(400, json_body={"errors": [{"message": self._echo()}]})
        with patch("deploy.provider_post", return_value=resp):
            with pytest.raises(RuntimeError) as excinfo:
                deploy.gql("mutation { x }", self._VARIABLES, token=_API_TOKEN)
        self._assert_clean(str(excinfo.value))
        assert "Railway API 400" in str(excinfo.value)

    def test_http_4xx_text_body_is_scrubbed(self):
        resp = _response(403, text=self._echo())
        with patch("deploy.provider_post", return_value=resp):
            with pytest.raises(RuntimeError) as excinfo:
                deploy.gql("mutation { x }", self._VARIABLES, token=_API_TOKEN)
        self._assert_clean(str(excinfo.value))

    def test_graphql_error_message_is_scrubbed(self):
        resp = _response(200, json_body={"errors": [{"message": self._echo()}]})
        with patch("deploy.provider_post", return_value=resp):
            with pytest.raises(RuntimeError) as excinfo:
                deploy.gql("mutation { x }", self._VARIABLES, token=_API_TOKEN)
        self._assert_clean(str(excinfo.value))
        assert str(excinfo.value).startswith("GraphQL error:")

    def test_error_text_is_bounded(self):
        resp = _response(200, json_body={"errors": [{"message": "x" * 50_000}]})
        with patch("deploy.provider_post", return_value=resp):
            with pytest.raises(RuntimeError) as excinfo:
                deploy.gql("query { x }", token=_API_TOKEN)
        assert len(str(excinfo.value)) < 1200

    @pytest.mark.parametrize("errors", [[], [None], "plain string", [{"no_message": 1}]])
    def test_odd_error_shapes_still_raise_runtime_error(self, errors):
        resp = _response(200, json_body={"errors": errors})
        with patch("deploy.provider_post", return_value=resp):
            with pytest.raises(RuntimeError, match="GraphQL error"):
                deploy.gql("query { x }", token=_API_TOKEN)

    def test_non_secret_variables_are_left_readable(self):
        assert deploy._sensitive_values({"input": {"name": "my-app", "projectId": "proj-12345678"}}) == set()

    def test_requests_use_tls_verification_and_a_timeout(self):
        resp = _response(200, json_body={"data": {"ok": True}})
        with patch("deploy.provider_post", return_value=resp) as post:
            deploy.gql("query { x }", token=_API_TOKEN)
        kwargs = post.call_args.kwargs
        assert kwargs["timeout"] == 30
        assert "verify" not in kwargs  # requests default: certificates are verified
        assert post.call_args.args[0].startswith("https://")


# ---------------------------------------------------------------------------
# Railway CLI child process
# ---------------------------------------------------------------------------

class TestRailwayCliChildProcess:
    def _run(self, completed):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            return completed

        with patch.dict(os.environ, {**_WORKER_ENV, **_KEPT_ENV}, clear=True), \
             patch("deploy.create_project_token", return_value=_PROJECT_TOKEN), \
             patch("deploy.subprocess.run", side_effect=fake_run):
            try:
                captured["result"] = deploy.deploy_source("/tmp/build", "proj", "env", "svc-1", _API_TOKEN)
            except RuntimeError as exc:
                captured["error"] = str(exc)
        return captured

    def test_cli_gets_the_project_token_and_no_worker_secret(self):
        completed = MagicMock(returncode=0, stdout="Build Logs: https://railway.com/x?id=" + "a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12, stderr="")
        captured = self._run(completed)
        env = captured["env"]
        assert env["RAILWAY_TOKEN"] == _PROJECT_TOKEN
        for name in _WORKER_ENV:
            assert name not in env, f"{name} leaked into the Railway CLI environment"
        assert env["PATH"] == _KEPT_ENV["PATH"] and env["HOME"] == _KEPT_ENV["HOME"]

    def test_cli_argv_is_a_list_without_tokens_and_has_a_timeout(self):
        completed = MagicMock(returncode=0, stdout="", stderr="")
        captured = self._run(completed)
        assert captured["argv"] == ["railway", "up", "--detach", "--service", "svc-1"]
        assert captured["timeout"] == 120
        assert not captured.get("shell")
        assert _PROJECT_TOKEN not in " ".join(captured["argv"])

    def test_cli_failure_output_is_scrubbed(self):
        completed = MagicMock(
            returncode=1, stdout="", stderr=f"Unauthorized: token {_PROJECT_TOKEN} rejected (account {_API_TOKEN})",
        )
        captured = self._run(completed)
        assert "railway up failed" in captured["error"]
        assert _PROJECT_TOKEN not in captured["error"] and _API_TOKEN not in captured["error"]

    def test_cli_success_output_is_scrubbed_before_printing(self, capsys):
        completed = MagicMock(returncode=0, stdout=f"using {_PROJECT_TOKEN}", stderr="")
        self._run(completed)
        assert _PROJECT_TOKEN not in capsys.readouterr().out


class TestChildEnvironmentHelper:
    def test_urls_with_passwords_are_dropped_whatever_the_name(self):
        base = {
            **_KEPT_ENV,
            "CACHE_ENDPOINT": "redis://default:" + "r" * 16 + "@cache.internal:6379/0",
            "UPSTREAM_PROXY": "http://user:" + "q" * 12 + "@proxy.example:3128",
            "PUBLIC_SITE": "https://example.com/path",
        }
        out = deploy.env_without_secrets(base)
        assert out == {**_KEPT_ENV, "PUBLIC_SITE": "https://example.com/path"}

    def test_psql_environment_is_scrubbed_but_keeps_the_connection(self):
        import test as test_module
        with patch.dict(os.environ, {**_WORKER_ENV, **_KEPT_ENV,
                                     "DATABASE_URL": "postgres://worker:" + "z" * 12 + "@dbhost:6543/worker_main"},
                        clear=True):
            env = test_module._pg_env()
        for name in _WORKER_ENV:
            assert name not in env, f"{name} leaked into the psql environment"
        assert env["PGHOST"] == "dbhost" and env["PGPORT"] == "6543" and env["PGUSER"] == "worker"
        assert env["PGPASSWORD"] == "z" * 12
        assert env["PATH"] == _KEPT_ENV["PATH"]


# ---------------------------------------------------------------------------
# Probe targets
# ---------------------------------------------------------------------------

BASE = "https://app.example.com"


class TestSpecPathsCannotChangeTheHost:
    @pytest.mark.parametrize("path", ["/", "/about", "/admin/events", "/a/b?c=d", "/@handle"])
    def test_route_paths_are_joined(self, path):
        assert deploy._app_url(BASE, path) == BASE + path

    @pytest.mark.parametrize("path", [
        "@169.254.169.254/latest/meta-data/",   # userinfo trick: host becomes the metadata address
        "@postgres.railway.internal:5432/",
        ".evil.example/x",                      # extends the host name
        ":8080@evil.example/",
        "evil.example/x",
        "",
        None,
        42,
        ["/about"],
        "/with space",
        "/tab\there",
        "/back\\slash",
        "/new\nline",
    ])
    def test_everything_else_is_refused(self, path):
        assert deploy._app_url(BASE, path) is None

    def test_internal_render_path_ignores_hostile_spec_paths(self):
        spec = {"pages": [
            {"path": "@169.254.169.254/latest/meta-data/"},
            {"path": ".evil.example/x"},
            "not a dict",
            {"path": "/services"},
        ]}
        assert deploy._pick_internal_render_path(spec) == "/services"

    def test_internal_render_path_falls_back_when_only_hostile_paths_exist(self):
        spec = {"pages": [{"path": "@10.0.0.5/admin"}]}
        assert deploy._pick_internal_render_path(spec) == "/contact"

    def test_verify_routes_never_requests_a_hostile_path(self):
        session = MagicMock()
        login = MagicMock(status_code=200, text='<input name="_csrf" value="tok">')
        session.get.return_value = login
        session.post.return_value = MagicMock(status_code=200)
        spec = {"pages": [{"path": "@169.254.169.254/latest"}, {"path": "/about"}]}
        with patch("deploy.guarded_session", return_value=session):
            result = deploy.verify_routes(BASE, spec, timeout=5)
        requested = [call.args[0] for call in session.get.call_args_list]
        assert all(url.startswith(BASE + "/") for url in requested), requested
        assert BASE + "/about" in requested
        assert result["total"] >= 1

    def test_verify_data_never_requests_a_hostile_path(self):
        session = MagicMock()
        session.get.return_value = MagicMock(status_code=200, text="x" * 5000)
        spec = {"pages": [{"path": "@10.0.0.5/secrets"}, {"path": "/menu"}], "data_models": []}
        with patch("deploy.guarded_session", return_value=session):
            result = deploy.verify_data(BASE, spec, timeout=5)
        requested = [call.args[0] for call in session.get.call_args_list]
        assert all(url.startswith(BASE + "/") for url in requested), requested
        assert result["pages_checked"] == 1


class TestProbesRefuseInternalBaseUrls:
    INTERNAL = [
        "http://169.254.169.254", "http://127.0.0.1:3000", "http://localhost:4000",
        "http://postgres.railway.internal:5432", "http://2130706433", "file:///etc/passwd",
        "http://10.0.0.5",
    ]

    @pytest.mark.parametrize("url", INTERNAL)
    def test_verify_health_refuses(self, url):
        with patched_probe_get("deploy") as get, patch("deploy.time.sleep"):
            assert deploy.verify_health(url, timeout=1) == "failed"
        get.assert_not_called()

    @pytest.mark.parametrize("url", INTERNAL)
    def test_http_probes_refuse(self, url):
        with patch("deploy.guarded_session") as session_cls, patch("deploy.time.sleep"):
            with patch.dict(os.environ, {"USE_MAGIC_LINK_AUTH": "false"}):
                assert deploy.verify_auth(url, {}, timeout=1) == "failed"
            with patch.dict(os.environ, {"USE_MAGIC_LINK_AUTH": "true"}):
                assert deploy.verify_magic_link_auth(url, "tok", timeout=1) == "failed"
            assert deploy.verify_routes(url, {"pages": [{"path": "/a"}]}, timeout=1)["status"] == "failed"
            assert deploy.verify_data(url, {"pages": [{"path": "/a"}]}, timeout=1)["status"] == "warning"
        session_cls.assert_not_called()

    @pytest.mark.parametrize("url", INTERNAL)
    def test_browser_probes_refuse(self, url, tmp_path):
        with patch("playwright.sync_api.sync_playwright") as playwright:
            assert deploy.capture_deploy_screenshots(url, {}, str(tmp_path)) == []
            result = deploy.verify_render(url, {}, str(tmp_path), timeout=10)
        playwright.assert_not_called()
        assert result["status"] == "warning" and result["screenshots"] == []

    def test_health_probe_does_not_follow_redirects(self):
        resp = MagicMock(status_code=200, text='{"status":"ok"}')
        with patched_probe_get("deploy", return_value=resp) as get:
            assert deploy.verify_health(BASE, timeout=5) == "ok"
        assert get.call_args.kwargs["allow_redirects"] is False


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Loopback server: counts requests, optionally redirects."""

    hits = None          # list shared per server class
    redirect_to = None   # absolute URL, or None

    def do_GET(self):
        self.hits.append(self.path)
        if self.path.startswith("/go") and self.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return
        if self.path == "/local-redirect":
            self.send_response(302)
            self.send_header("Location", "/landed")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def _serve(handler_cls):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class TestProbeSessionStaysOnTheAppsOrigin:
    @pytest.fixture(autouse=True)
    def _loopback_counts_as_public(self, monkeypatch):
        """The session under test also refuses non-public addresses (see
        tests/test_probe_guard.py). These tests are about the origin rule and
        use listeners on loopback, so the address policy lets loopback through
        here; everything else keeps its real verdict."""
        import monitor.ping as ping_mod
        real = ping_mod.is_public_address
        monkeypatch.setattr(ping_mod, "is_public_address",
                            lambda ip: ip in ("127.0.0.1", "::1") or real(ip))

    @pytest.fixture
    def servers(self):
        other_hits, app_hits = [], []
        other_cls = type("Other", (_Recorder,), {"hits": other_hits})
        other = _serve(other_cls)
        other_url = f"http://127.0.0.1:{other.server_address[1]}"
        app_cls = type("App", (_Recorder,), {"hits": app_hits, "redirect_to": other_url + "/internal"})
        app = _serve(app_cls)
        app_url = f"http://127.0.0.1:{app.server_address[1]}"
        yield app_url, app_hits, other_url, other_hits
        app.shutdown()
        other.shutdown()
        app.server_close()
        other.server_close()

    def test_redirect_to_another_origin_is_refused_before_it_is_sent(self, servers):
        app_url, app_hits, _other_url, other_hits = servers
        session = deploy._same_origin_session(app_url)
        with pytest.raises(requests.exceptions.InvalidURL):
            session.get(app_url + "/go", timeout=5)
        assert app_hits == ["/go"]
        assert other_hits == [], "the probe followed a redirect to a host that was never checked"

    def test_redirect_within_the_app_is_followed(self, servers):
        app_url, app_hits, _other_url, _other_hits = servers
        resp = deploy._same_origin_session(app_url).get(app_url + "/local-redirect", timeout=5)
        assert resp.status_code == 200
        assert app_hits == ["/local-redirect", "/landed"]

    def test_direct_request_to_another_origin_is_refused(self, servers):
        app_url, _app_hits, other_url, other_hits = servers
        with pytest.raises(requests.exceptions.InvalidURL):
            deploy._same_origin_session(app_url).get(other_url + "/x", timeout=5)
        assert other_hits == []

    def test_plain_requests_would_have_followed_the_redirect(self, servers):
        """Control: without the guard the same redirect reaches the other server."""
        app_url, _app_hits, _other_url, other_hits = servers
        requests.get(app_url + "/go", timeout=5)
        assert other_hits == ["/internal"]


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

class _FakeRoute:
    def __init__(self, url):
        self.request = MagicMock()
        self.request.url = url
        self.outcome = None

    def abort(self, reason=None):
        self.outcome = "aborted"

    def continue_(self):
        self.outcome = "continued"


def _installed_handler():
    page = MagicMock()
    deploy._guard_browser_page(page)
    pattern, handler = page.context.route.call_args.args
    assert pattern == "**/*"
    return handler


class TestBrowserGuard:
    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:5432/",
        "http://localhost:4000/admin",
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://web.railway.internal:4000/health",
        "http://2130706433/",
        "ftp://example.com/x",
        "chrome://settings",
        "https://user:pw@example.com/",
    ])
    def test_non_public_or_non_http_requests_are_aborted(self, url):
        handler = _installed_handler()
        route = _FakeRoute(url)
        with patch("deploy.host_resolution_problem", return_value=None):
            handler(route)
        assert route.outcome == "aborted", url

    @pytest.mark.parametrize("url", [
        "https://app.example.com/", "https://fonts.example.net/a.woff2",
        "data:image/png;base64,AAAA", "blob:https://app.example.com/1", "about:blank",
    ])
    def test_public_and_in_browser_urls_continue(self, url):
        handler = _installed_handler()
        route = _FakeRoute(url)
        with patch("deploy.host_resolution_problem", return_value=None):
            handler(route)
        assert route.outcome == "continued", url

    def test_public_looking_name_that_resolves_privately_is_aborted(self):
        handler = _installed_handler()
        with patch("deploy.host_resolution_problem",
                   return_value="host resolves to a non-public address") as resolve:
            first, second = _FakeRoute("https://rebind.example.com/a"), _FakeRoute("https://rebind.example.com/b")
            handler(first)
            handler(second)
        assert first.outcome == second.outcome == "aborted"
        assert resolve.call_count == 1, "the verdict for a host is cached for the page"

    def test_a_failing_check_blocks_the_request(self):
        handler = _installed_handler()
        route = _FakeRoute("https://app.example.com/")
        with patch("deploy.host_resolution_problem", side_effect=RuntimeError("resolver broke")):
            handler(route)
        assert route.outcome == "aborted"

    def _playwright(self, page):
        browser = MagicMock()
        browser.new_page.return_value = page
        pw = MagicMock()
        pw.__enter__ = MagicMock(return_value=pw)
        pw.__exit__ = MagicMock(return_value=False)
        pw.chromium.launch.return_value = browser
        return pw, browser

    def test_verify_render_opens_a_guarded_page_and_stays_on_the_app(self, tmp_path):
        page = MagicMock()
        page.goto.return_value = MagicMock(status=200)
        pw, browser = self._playwright(page)
        spec = {"pages": [{"path": "@169.254.169.254/latest/meta-data/"}, {"path": "/about"}]}
        with patch("playwright.sync_api.sync_playwright", return_value=pw):
            deploy.verify_render(BASE, spec, str(tmp_path), timeout=30)
        options = browser.new_page.call_args.kwargs
        assert options["accept_downloads"] is False and options["service_workers"] == "block"
        page.context.route.assert_called_once()
        visited = [call.args[0] for call in page.goto.call_args_list]
        assert visited == [BASE + "/", BASE + "/about"]

    def test_deploy_screenshots_open_a_guarded_page(self, tmp_path):
        page = MagicMock()
        pw, browser = self._playwright(page)
        with patch("playwright.sync_api.sync_playwright", return_value=pw), \
             patch.dict(os.environ, {"USE_MAGIC_LINK_AUTH": "true"}):
            deploy.capture_deploy_screenshots(BASE, {}, str(tmp_path))
        options = browser.new_page.call_args.kwargs
        assert options["accept_downloads"] is False and options["service_workers"] == "block"
        page.context.route.assert_called_once()


# ---------------------------------------------------------------------------
# Project deletion
# ---------------------------------------------------------------------------

class TestDestroyProjectFailsClosed:
    def _calls(self, name_lookup):
        calls = []

        def fake_gql(query, variables=None, token=None):
            calls.append(query)
            if "projectDelete" in query:
                return {"projectDelete": True}
            return name_lookup()

        return calls, fake_gql

    def test_name_lookup_error_blocks_the_delete(self):
        def boom():
            raise RuntimeError("Railway API 500")
        calls, fake_gql = self._calls(boom)
        with patch.dict(os.environ, {"RAILWAY_PROJECT_ID": "platform-project"}, clear=False), \
             patch("deploy.gql", side_effect=fake_gql):
            with pytest.raises(RuntimeError, match="BLOCKED"):
                deploy.destroy_project("some-project", token="t")
        assert not any("projectDelete" in query for query in calls)

    @pytest.mark.parametrize("payload", [{"project": None}, {}, {"project": {"id": "x"}}, {"project": {"name": ""}},
                                         {"project": {"name": ["craftandship-web"]}}])
    def test_unreadable_name_blocks_the_delete(self, payload):
        calls, fake_gql = self._calls(lambda: payload)
        with patch.dict(os.environ, {"RAILWAY_PROJECT_ID": "platform-project"}, clear=False), \
             patch("deploy.gql", side_effect=fake_gql):
            with pytest.raises(RuntimeError, match="BLOCKED"):
                deploy.destroy_project("some-project", token="t")
        assert not any("projectDelete" in query for query in calls)

    def test_checked_customer_project_is_still_deleted(self):
        calls, fake_gql = self._calls(lambda: {"project": {"id": "c1", "name": "garden-tracker"}})
        with patch.dict(os.environ, {"RAILWAY_PROJECT_ID": "platform-project"}, clear=False), \
             patch("deploy.gql", side_effect=fake_gql), patch("builtins.print"):
            deploy.destroy_project("c1", token="t")
        assert sum("projectDelete" in query for query in calls) == 1

    def test_failed_cleanup_does_not_hide_the_deploy_error(self, tmp_path):
        build_dir = tmp_path / "app"
        build_dir.mkdir()
        (build_dir / "package.json").write_text('{"name": "app"}')
        with patch("deploy.check_railway_cli"), patch("deploy.get_token", return_value="t"), \
             patch("deploy.create_project", return_value="proj-1"), \
             patch("deploy.get_environment_id", side_effect=RuntimeError("the real failure")), \
             patch("deploy.destroy_project", side_effect=RuntimeError("destroy_project BLOCKED: x")):
            with pytest.raises(RuntimeError, match="the real failure"):
                deploy.deploy(str(build_dir))


# ---------------------------------------------------------------------------
# Odds and ends
# ---------------------------------------------------------------------------

class TestDeployOutputAndInputs:
    def test_cli_entry_point_prints_the_database_url_redacted(self, capsys):
        result = {"live_url": BASE, "database_url": _DB_URL, "health_check": "ok"}
        with patch("deploy.deploy", return_value=result), patch.object(sys, "argv", ["deploy.py", "builds/x"]):
            deploy.main()
        out = capsys.readouterr().out
        assert _PG_PASSWORD not in out
        assert "postgresql://***@postgres.railway.internal" in out

    @pytest.mark.parametrize("package_json", ['{"name": ["a"]}', '{"name": 7}', '{"name": ""}', "not json", "[]"])
    def test_app_name_falls_back_to_the_directory_for_odd_package_json(self, tmp_path, package_json):
        build_dir = tmp_path / "garden-tracker"
        build_dir.mkdir()
        (build_dir / "package.json").write_text(package_json)
        assert deploy.read_app_name(str(build_dir)) == "garden-tracker"

    def test_every_railway_query_uses_variables_not_interpolation(self):
        """No GraphQL document in deploy.py is an f-string or built with format()."""
        import ast
        tree = ast.parse(Path(deploy.__file__).read_text())
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "gql" and node.args:
                first = node.args[0]
                is_literal = isinstance(first, ast.Constant) and isinstance(first.value, str)
                is_named_literal = isinstance(first, ast.Name)  # `query = """..."""` just above the call
                if not (is_literal or is_named_literal):
                    offenders.append(node.lineno)
        assert offenders == []
        assert not any(isinstance(n, ast.JoinedStr) and any(
            isinstance(v, ast.Constant) and isinstance(v.value, str) and ("mutation" in v.value or "query" in v.value)
            and "{" in v.value for v in n.values) for n in ast.walk(tree))


# ---------------------------------------------------------------------------
# cli/railway_cleanup.py (operator script that deletes same-named projects)
# ---------------------------------------------------------------------------

class TestDuplicateCleanupFailsClosed:
    PROJECTS = [
        {"id": "p-new", "name": "my-studio", "createdAt": "2026-05-02T10:00:00Z"},
        {"id": "p-old", "name": "my-studio", "createdAt": "2026-05-01T10:00:00Z"},
    ]

    def _run(self, argv, active_ids, destroy=None):
        import railway_cleanup
        destroy = destroy or MagicMock()
        with patch.object(railway_cleanup, "get_token", return_value="t"), \
             patch.object(railway_cleanup, "list_projects", return_value=list(self.PROJECTS)), \
             patch.object(railway_cleanup, "_active_project_ids", return_value=active_ids), \
             patch.object(railway_cleanup, "destroy_project", destroy), \
             patch.object(sys, "argv", ["railway_cleanup.py", *argv]):
            railway_cleanup.main()
        return destroy

    def test_unknown_usage_refuses_to_delete(self):
        """No DATABASE_URL or a failed query means "unknown", not "nothing in use":
        the older same-named project may be another customer's live app."""
        destroy = MagicMock()
        with pytest.raises(SystemExit) as excinfo:
            self._run([], active_ids=None, destroy=destroy)
        assert excinfo.value.code == 1
        destroy.assert_not_called()

    def test_unknown_usage_still_allows_a_dry_run(self, capsys):
        destroy = self._run(["--dry-run"], active_ids=None)
        destroy.assert_not_called()
        assert "unknown" in capsys.readouterr().out

    def test_project_in_use_is_skipped(self):
        destroy = self._run([], active_ids={"p-old"})
        destroy.assert_not_called()

    def test_unused_duplicate_is_deleted(self):
        destroy = self._run([], active_ids={"something-else"})
        destroy.assert_called_once_with("p-old", "t")

    def test_refused_delete_does_not_abort_the_run(self, capsys):
        destroy = MagicMock(side_effect=RuntimeError("destroy_project BLOCKED: could not read the name"))
        self._run([], active_ids=set(), destroy=destroy)
        assert "Not deleted" in capsys.readouterr().out

    def test_lookup_reports_unknown_without_a_database_url(self, monkeypatch):
        import railway_cleanup
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert railway_cleanup._active_project_ids() is None

    def test_lookup_reports_unknown_when_the_query_fails(self, monkeypatch):
        import psycopg2
        import railway_cleanup
        monkeypatch.setenv("DATABASE_URL", "postgres://worker@localhost:5432/worker_main")
        with patch.object(psycopg2, "connect", side_effect=psycopg2.OperationalError("down")):
            assert railway_cleanup._active_project_ids() is None


class TestEveryChildOfModelWrittenCodeIsScrubbed:
    def test_node_syntax_check_gets_the_scrubbed_environment(self, tmp_path):
        import qa_fix
        (tmp_path / "server.js").write_text("module.exports = {};\n")
        captured = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            return MagicMock()

        with patch.dict(os.environ, {**_WORKER_ENV, **_KEPT_ENV}, clear=True), \
             patch.object(qa_fix.subprocess, "Popen", side_effect=fake_popen):
            qa_fix._run_node_syntax_check(tmp_path)

        assert captured["argv"][:2] == ["node", "--check"]
        assert not (set(captured["env"]) & set(_WORKER_ENV))
        assert captured["env"]["PATH"] == _KEPT_ENV["PATH"]

    def test_no_subprocess_in_cli_inherits_the_worker_environment_implicitly(self):
        """Every subprocess call in the cli/ modules that run or touch generated
        code passes an explicit env (run_cmd in test.py forwards the one its
        callers give it)."""
        import ast
        offenders = []
        for name in ("deploy.py", "qa.py", "qa_fix.py", "test.py"):
            tree = ast.parse((ROOT / "cli" / name).read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("run", "Popen", "call", "check_call", "check_output")
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"):
                    if not any(kw.arg == "env" for kw in node.keywords):
                        offenders.append(f"{name}:{node.lineno}")
        assert offenders == []

    def test_run_cmd_callers_in_test_py_always_pass_an_env(self):
        import ast
        tree = ast.parse((ROOT / "cli" / "test.py").read_text())
        offenders = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "run_cmd"
            and not any(kw.arg == "env" for kw in node.keywords)
        ]
        assert offenders == []
