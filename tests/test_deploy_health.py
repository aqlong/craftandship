"""Tests for deploy.py health check and deployment ID parsing improvements."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)

from deploy import (
    DeploymentLogsUnreadable, verify_health, verify_auth, verify_data, get_deployment_logs,
    wait_for_deployment,
)
from tests._guarded_session_mock import patched_probe_get


# ---------------------------------------------------------------------------
# verify_health()
# ---------------------------------------------------------------------------

class TestVerifyHealth:
    def test_returns_ok_on_healthy_response(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"status":"ok"}'

        with patched_probe_get("deploy", return_value=mock_resp):
            result = verify_health("https://app.example.com", timeout=5)

        assert result == "ok"

    def test_returns_infra_on_timeout_never_responding(self):
        """When the URL never responds (all requests time out), return 'infra'
        so the caller routes through the propagation-retry loop instead of
        running destructive fix paths.

        Behavior change 2026-04-22: previously returned 'failed', which caused
        false-positive crashes on v2 apps when Railway's edge took >60s to
        propagate the URL despite the container already listening on port 3000.
        Genuine app crashes produce 502/503 from Railway's edge (caught by the
        non-200 case below), so silence is specifically a propagation signal.
        """
        with patched_probe_get("deploy", side_effect=requests.Timeout("timeout")):
            with patch("deploy.time.sleep"):
                result = verify_health("https://app.example.com", timeout=1)

        assert result == "infra"

    def test_returns_failed_on_non_200(self):
        """Got a response from Railway's edge but it wasn't 200 + 'ok', this
        means the URL IS propagated and the app is reachable but broken. A
        genuine app crash manifests here (Railway edge returns 502/503 with
        body like 'Bad Gateway'), so 'failed' is correct.
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_resp.text = "Bad Gateway"

        with patched_probe_get("deploy", return_value=mock_resp):
            with patch("deploy.time.sleep"):
                result = verify_health("https://app.example.com", timeout=1)

        assert result == "failed"

    def test_returns_failed_when_ok_not_in_body(self):
        """Got 200 but body doesn't contain 'ok', the /health endpoint
        deviated from the CAS contract. That's a code bug in the generated
        app, not propagation. Return 'failed'."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"error":"starting"}'

        with patched_probe_get("deploy", return_value=mock_resp):
            with patch("deploy.time.sleep"):
                result = verify_health("https://app.example.com", timeout=1)

        assert result == "failed"

    def test_does_not_raise(self):
        """verify_health should never raise, even on unexpected errors.

        When every attempt raises (unpredictable network conditions), we
        never got a response, treat as 'infra' (propagation not complete)
        per the 2026-04-22 fix, not 'failed'.
        """
        with patched_probe_get("deploy", side_effect=Exception("unexpected")):
            with patch("deploy.time.sleep"):
                result = verify_health("https://app.example.com", timeout=1)
        assert result == "infra"

    def test_returns_infra_on_connection_timeout_body(self):
        """App responds but DB has connection timeout -- infrastructure issue."""
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = '{"status":"degraded","error":"connection timeout to database"}'

        with patched_probe_get("deploy", return_value=mock_resp):
            with patch("deploy.time.sleep"):
                result = verify_health("https://app.example.com", timeout=1)

        assert result == "infra"

    def test_returns_infra_on_connection_terminated_body(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"status":"degraded","error":"Connection terminated unexpectedly"}'

        with patched_probe_get("deploy", return_value=mock_resp):
            with patch("deploy.time.sleep"):
                result = verify_health("https://app.example.com", timeout=1)

        assert result == "infra"

    def test_returns_infra_on_econnrefused_body(self):
        """ECONNREFUSED in body triggers 'infra' when casing matches the signal list."""
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        # Body must contain "connection refused" (lowercase) since verify_health
        # lowercases the body before matching against signal strings.
        mock_resp.text = '{"error":"connection refused to database at 127.0.0.1:5432"}'

        with patched_probe_get("deploy", return_value=mock_resp):
            with patch("deploy.time.sleep"):
                result = verify_health("https://app.example.com", timeout=1)

        assert result == "infra"

    def test_returns_infra_when_no_response_ever_received(self):
        """When the URL never responds (connection refused on every attempt),
        return 'infra', this is Railway edge propagation, not a code bug.

        Behavior change 2026-04-22: previously returned 'failed'. The
        rationale: a crashed app produces 502/503 from Railway's edge (so
        got_any_response=True → falls through to the 'failed' branch).
        Complete silence (got_any_response=False) is specifically the signal
        that the URL isn't routable yet. Routing this to 'infra' lets the
        worker's existing 3-minute propagation retry handle it cleanly
        instead of triggering destructive deploy_fix paths.
        """
        with patched_probe_get("deploy", side_effect=requests.ConnectionError("refused")):
            with patch("deploy.time.sleep"):
                result = verify_health("https://app.example.com", timeout=1)

        assert result == "infra"

    def test_default_timeout_is_120s(self):
        """Default timeout bumped 60→120s after 2026-04-22 Railway
        propagation investigation. v1 apps that pass health still pass
        quickly (first poll); v1 apps that crash hit the non-200 case
        equally fast; the extra 60s only extends the worst-case "URL not
        routable" window, which now correctly returns 'infra' anyway."""
        import inspect
        from deploy import verify_health as vh
        sig = inspect.signature(vh)
        assert sig.parameters["timeout"].default == 120, (
            "verify_health default timeout must be 120s (covers Railway edge "
            "propagation plus container boot with margin)"
        )


# ---------------------------------------------------------------------------
# verify_magic_link_auth()
# ---------------------------------------------------------------------------

from deploy import verify_magic_link_auth


class TestVerifyMagicLinkAuth:
    """verify_magic_link_auth with a mocked session: which requests it sends
    and how it reads the answers. The same probe against a listening site
    (cookie, CSRF, a token that is really used up, the access log) is in
    tests/test_probe_guard.py."""

    TOKEN = "ab" * 32

    @staticmethod
    def _login_page(csrf="c5rf-value"):
        page = MagicMock()
        page.status_code = 200
        page.text = f'<form><input type="hidden" name="_csrf" value="{csrf}"></form>'
        return page

    @staticmethod
    def _redirect(location):
        resp = MagicMock()
        resp.status_code = 302
        resp.headers = {"location": location}
        return resp

    @patch.dict("os.environ", {"USE_MAGIC_LINK_AUTH": "false"})
    def test_skipped_when_disabled(self):
        result = verify_magic_link_auth("https://app.example.com", "abc123")
        assert result == "skipped"

    def test_skipped_when_no_token(self):
        result = verify_magic_link_auth("https://app.example.com", None)
        assert result == "skipped"

    @patch("deploy.guarded_session")
    def test_ok_when_the_token_signs_in(self, mock_session_cls):
        """The token goes in the BODY of POST /magic/consume together with the
        CSRF value of the session. It is in no URL the probe requests."""
        session = mock_session_cls.return_value
        session.get.return_value = self._login_page()
        session.post.return_value = self._redirect("/set-password")

        result = verify_magic_link_auth("https://app.example.com", self.TOKEN, timeout=5)

        assert result == "ok"
        session.get.assert_called_once()
        assert session.get.call_args[0][0] == "https://app.example.com/login"
        session.post.assert_called_once()
        assert session.post.call_args[0][0] == "https://app.example.com/magic/consume"
        assert session.post.call_args.kwargs["data"] == {"token": self.TOKEN, "_csrf": "c5rf-value"}
        assert session.post.call_args.kwargs["allow_redirects"] is False
        session.head.assert_not_called()
        requested = [str(c) for c in session.get.call_args_list] + [session.post.call_args[0][0]]
        assert not any(self.TOKEN in url for url in requested), "the token must not be in a URL"

    @patch("deploy.guarded_session")
    @pytest.mark.parametrize("location", [
        "/login?error=invalid-token", "/login?error=token-already-used", "/login?error=token-expired",
        "https://app.example.com/login?error=invalid-token", "",
    ])
    def test_failed_when_the_site_sends_the_probe_back_to_login(self, mock_session_cls, location):
        session = mock_session_cls.return_value
        session.get.return_value = self._login_page()
        session.post.return_value = self._redirect(location)
        assert verify_magic_link_auth("https://app.example.com", self.TOKEN, timeout=5) == "failed"

    @patch("deploy.guarded_session")
    @pytest.mark.parametrize("status", [404, 405, 403, 500, 200])
    def test_failed_on_anything_but_a_redirect(self, mock_session_cls, status):
        """A missing route (a v1 app), a refused CSRF value, a server error
        and a page instead of a redirect all mean the login was not verified."""
        session = mock_session_cls.return_value
        session.get.return_value = self._login_page()
        answer = MagicMock()
        answer.status_code = status
        session.post.return_value = answer
        assert verify_magic_link_auth("https://app.example.com", self.TOKEN, timeout=5) == "failed"

    @patch("deploy.guarded_session")
    def test_failed_without_a_csrf_value_and_without_sending_the_token(self, mock_session_cls):
        session = mock_session_cls.return_value
        page = MagicMock()
        page.status_code = 200
        page.text = "<form></form>"
        session.get.return_value = page
        assert verify_magic_link_auth("https://app.example.com", self.TOKEN, timeout=5) == "failed"
        session.post.assert_not_called()


# ---------------------------------------------------------------------------
# verify_auth()
# ---------------------------------------------------------------------------

class TestVerifyAuth:
    """Tests for verify_auth, always runs, downgrades to warning in magic link mode."""

    @patch("deploy.guarded_session")
    @patch.dict(os.environ, {"USE_MAGIC_LINK_AUTH": "false"})
    def test_returns_ok_on_successful_login(self, mock_session_cls):
        """Successful 302 to non-login path returns 'ok'."""
        session = MagicMock()
        mock_session_cls.return_value = session
        # GET /login returns form with CSRF
        login_page = MagicMock()
        login_page.status_code = 200
        login_page.text = '<input name="_csrf" value="tok123">'
        # POST /login returns 302 to /dashboard
        post_resp = MagicMock()
        post_resp.status_code = 302
        post_resp.headers = {"location": "/dashboard"}
        session.get.return_value = login_page
        session.post.return_value = post_resp

        result = verify_auth("https://app.example.com", {"admin_email": "a@b.example"}, timeout=5)
        assert result == "ok"

    @patch("deploy.guarded_session")
    @patch.dict(os.environ, {"USE_MAGIC_LINK_AUTH": "false"})
    def test_returns_failed_on_redirect_to_login(self, mock_session_cls):
        """302 back to /login returns 'failed' (not magic link mode)."""
        session = MagicMock()
        mock_session_cls.return_value = session
        login_page = MagicMock()
        login_page.status_code = 200
        login_page.text = '<input name="_csrf" value="tok123">'
        post_resp = MagicMock()
        post_resp.status_code = 302
        post_resp.headers = {"location": "/login?error=bad"}
        session.get.return_value = login_page
        session.post.return_value = post_resp

        result = verify_auth("https://app.example.com", {"admin_email": "a@b.example"}, timeout=5)
        assert result == "failed"

    @patch("deploy.guarded_session")
    @patch.dict(os.environ, {"USE_MAGIC_LINK_AUTH": "true"})
    def test_returns_warning_in_magic_link_mode(self, mock_session_cls):
        """In magic link mode, failed auth downgrades to 'warning' instead of 'failed'."""
        session = MagicMock()
        mock_session_cls.return_value = session
        login_page = MagicMock()
        login_page.status_code = 200
        login_page.text = '<input name="_csrf" value="tok123">'
        post_resp = MagicMock()
        post_resp.status_code = 302
        post_resp.headers = {"location": "/login?error=bad"}
        session.get.return_value = login_page
        session.post.return_value = post_resp

        result = verify_auth("https://app.example.com", {"admin_email": "a@b.example"}, timeout=5)
        assert result == "warning"

    @patch("deploy.guarded_session")
    @patch.dict(os.environ, {"USE_MAGIC_LINK_AUTH": "true"})
    def test_ok_still_returns_ok_in_magic_link_mode(self, mock_session_cls):
        """Successful login returns 'ok' even in magic link mode."""
        session = MagicMock()
        mock_session_cls.return_value = session
        login_page = MagicMock()
        login_page.status_code = 200
        login_page.text = '<input name="_csrf" value="tok123">'
        post_resp = MagicMock()
        post_resp.status_code = 302
        post_resp.headers = {"location": "/dashboard"}
        session.get.return_value = login_page
        session.post.return_value = post_resp

        result = verify_auth("https://app.example.com", {"admin_email": "a@b.example"}, timeout=5)
        assert result == "ok"

    @patch("deploy.guarded_session")
    @patch.dict(os.environ, {"USE_MAGIC_LINK_AUTH": "false"})
    def test_returns_failed_on_500(self, mock_session_cls):
        """500 from login POST returns 'failed'."""
        session = MagicMock()
        mock_session_cls.return_value = session
        login_page = MagicMock()
        login_page.status_code = 200
        login_page.text = '<input name="_csrf" value="tok123">'
        post_resp = MagicMock()
        post_resp.status_code = 500
        session.get.return_value = login_page
        session.post.return_value = post_resp

        result = verify_auth("https://app.example.com", {"admin_email": "a@b.example"}, timeout=5)
        assert result == "failed"


# ---------------------------------------------------------------------------
# verify_data()
# ---------------------------------------------------------------------------

class TestVerifyData:
    """Tests for verify_data, checks seed data presence on CRUD pages."""

    @patch("deploy.guarded_session")
    def test_returns_ok_for_large_pages(self, mock_session_cls):
        """Pages with >2000 bytes of content pass."""
        session = MagicMock()
        mock_session_cls.return_value = session
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "x" * 5000
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.text = '<input name="_csrf" value="t">'
        session.get.side_effect = [login_resp, resp, resp]
        session.post.return_value = MagicMock(status_code=302)

        spec = {"pages": [{"path": "/events"}, {"path": "/photos"}], "data_models": [], "admin_email": "a@b.example"}
        result = verify_data("https://app.example.com", spec, timeout=5)
        assert result["status"] == "ok"
        assert result["pages_checked"] == 2
        assert result["pages_empty"] == 0

    @patch("deploy.guarded_session")
    def test_returns_warning_for_small_pages(self, mock_session_cls):
        """Pages with <2000 bytes get flagged as empty."""
        session = MagicMock()
        mock_session_cls.return_value = session
        small_resp = MagicMock()
        small_resp.status_code = 200
        small_resp.text = "<html><body>empty</body></html>"
        login_resp = MagicMock()
        login_resp.status_code = 404  # no login page
        session.get.side_effect = [login_resp, small_resp]

        spec = {"pages": [{"path": "/events"}], "data_models": [], "admin_email": "a@b.example"}
        result = verify_data("https://app.example.com", spec, timeout=5)
        assert result["status"] == "warning"
        assert result["pages_empty"] == 1

    def test_returns_ok_with_no_pages(self):
        """No pages in spec means nothing to check."""
        spec = {"pages": [], "data_models": []}
        result = verify_data("https://app.example.com", spec, timeout=5)
        assert result["status"] == "ok"
        assert result["pages_checked"] == 0

    @patch("deploy.guarded_session")
    def test_derives_model_paths(self, mock_session_cls):
        """CRUD paths derived from data_models when no pages specified."""
        session = MagicMock()
        mock_session_cls.return_value = session
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "x" * 5000
        login_resp = MagicMock()
        login_resp.status_code = 404
        session.get.side_effect = [login_resp, resp]

        spec = {"pages": [], "data_models": [{"name": "Event"}], "admin_email": "a@b.example"}
        result = verify_data("https://app.example.com", spec, timeout=5)
        assert result["pages_checked"] == 1


# ---------------------------------------------------------------------------
# get_deployment_logs(): DEPLOY_CRASH1 log capture
# ---------------------------------------------------------------------------

class TestGetDeploymentLogsLimit:
    """Verify the `limit` parameter wires through to the GraphQL query.

    Default is 20 (back-compat with monitor/diagnose). Worker's DEPLOY_CRASH1
    path bumps to 100 so a terminal deploy failure has enough context in
    build_logs to debug without re-querying Railway (which may have torn down
    the project).
    """

    @patch("deploy.gql")
    def test_default_limit_is_20(self, mock_gql):
        mock_gql.return_value = {"deploymentLogs": []}
        get_deployment_logs("dep-123", "tok")
        _query, variables, _tok = mock_gql.call_args[0]
        assert variables == {"id": "dep-123", "limit": 20}

    @patch("deploy.gql")
    def test_custom_limit_is_passed_through(self, mock_gql):
        mock_gql.return_value = {"deploymentLogs": []}
        get_deployment_logs("dep-456", "tok", limit=100)
        _query, variables, _tok = mock_gql.call_args[0]
        assert variables == {"id": "dep-456", "limit": 100}

    @patch("deploy.gql")
    def test_invalid_limit_falls_back_to_20(self, mock_gql):
        mock_gql.return_value = {"deploymentLogs": []}
        get_deployment_logs("dep-789", "tok", limit="not-a-number")
        _query, variables, _tok = mock_gql.call_args[0]
        assert variables == {"id": "dep-789", "limit": 20}

    @patch("deploy.gql")
    def test_limit_clamped_to_at_least_1(self, mock_gql):
        mock_gql.return_value = {"deploymentLogs": []}
        get_deployment_logs("dep-abc", "tok", limit=0)
        _query, variables, _tok = mock_gql.call_args[0]
        assert variables["limit"] == 1

    @patch("deploy.gql", side_effect=RuntimeError("Railway GraphQL 500"))
    def test_returns_empty_list_on_gql_error(self, _mock_gql):
        # Never raise: log capture must be fail-safe.
        assert get_deployment_logs("dep-xyz", "tok", limit=100) == []

    @patch("deploy.gql", side_effect=RuntimeError("Railway GraphQL 500 for token-value-abc"))
    def test_an_unreadable_log_raises_when_the_caller_asks_to_know(self, _mock_gql):
        """An empty list means "no lines"; the worker must be able to tell it
        from "could not read", and the exception carries no request detail."""
        with pytest.raises(DeploymentLogsUnreadable) as unreadable:
            get_deployment_logs("dep-xyz", "tok", limit=100, raise_on_error=True)
        assert "dep-xyz" in str(unreadable.value) and "RuntimeError" in str(unreadable.value)
        assert "token-value-abc" not in str(unreadable.value)

    @patch("deploy.gql")
    def test_a_log_with_no_lines_is_not_an_error_even_when_the_caller_asks_to_know(self, mock_gql):
        mock_gql.return_value = {"deploymentLogs": []}
        assert get_deployment_logs("dep-empty", "tok", raise_on_error=True) == []
        mock_gql.return_value = {"deploymentLogs": None}
        assert get_deployment_logs("dep-none", "tok", raise_on_error=True) == []

    @patch("deploy.gql")
    def test_extracts_message_field_only(self, mock_gql):
        mock_gql.return_value = {
            "deploymentLogs": [
                {"message": "line 1", "timestamp": "2026-01-01T00:00:00Z"},
                {"message": "line 2", "timestamp": "2026-01-01T00:00:01Z"},
            ]
        }
        result = get_deployment_logs("dep-ok", "tok", limit=100)
        assert result == ["line 1", "line 2"]


# ---------------------------------------------------------------------------
# wait_for_deployment(): transient CRASHED tolerance (2026-04-22)
# ---------------------------------------------------------------------------
# Railway frequently reports CRASHED briefly during the first container
# restart after schema migration, then stabilizes to SUCCESS. The prior
# implementation raised on the first CRASHED snapshot, causing deploy() to
# short-circuit with health_check="failed" before the app recovered. These
# tests lock in the new behavior: only raise after 2 consecutive CRASHED
# (or FAILED/REMOVED) polls.

class TestWaitForDeploymentCrashedTransient:
    def test_returns_on_success(self):
        """Happy path: SUCCESS on first poll returns immediately."""
        with patch("deploy.gql", return_value={"deployment": {"id": "d", "status": "SUCCESS"}}):
            with patch("deploy.time.sleep"):
                # Should not raise
                wait_for_deployment("dep-1", "tok", timeout=1)

    def test_tolerates_single_transient_crashed_then_success(self):
        """Regression 2026-04-22: Railway reports CRASHED briefly then SUCCESS.
        Prior impl raised on first CRASHED; new impl waits one more poll."""
        statuses = iter([
            {"deployment": {"id": "d", "status": "CRASHED"}},   # transient
            {"deployment": {"id": "d", "status": "SUCCESS"}},   # stabilized
        ])
        with patch("deploy.gql", side_effect=lambda *a, **k: next(statuses)):
            with patch("deploy.time.sleep"):
                # Should NOT raise: transient CRASHED is tolerated.
                wait_for_deployment("dep-2", "tok", timeout=60)

    def test_tolerates_single_transient_failed_then_success(self):
        """FAILED gets the same tolerance (same class of Railway flicker)."""
        statuses = iter([
            {"deployment": {"id": "d", "status": "FAILED"}},
            {"deployment": {"id": "d", "status": "SUCCESS"}},
        ])
        with patch("deploy.gql", side_effect=lambda *a, **k: next(statuses)):
            with patch("deploy.time.sleep"):
                wait_for_deployment("dep-2f", "tok", timeout=60)

    def test_raises_on_two_consecutive_crashed(self):
        """Genuine crash: 2 consecutive CRASHED polls → raise (the fix MUST
        still surface real failures; tolerance is only one poll wide)."""
        statuses = iter([
            {"deployment": {"id": "d", "status": "CRASHED"}},   # 1st
            {"deployment": {"id": "d", "status": "CRASHED"}},   # 2nd → raise
        ])
        with patch("deploy.gql", side_effect=lambda *a, **k: next(statuses)):
            with patch("deploy.time.sleep"):
                with patch("deploy.get_deployment_logs", return_value=[]):
                    with pytest.raises(RuntimeError, match="crashed"):
                        wait_for_deployment("dep-3", "tok", timeout=60)

    def test_intermediate_deploying_resets_transient_counter(self):
        """CRASHED → DEPLOYING → CRASHED should NOT raise (the flicker cleared;
        the second CRASHED is a fresh first observation, not consecutive)."""
        statuses = iter([
            {"deployment": {"id": "d", "status": "CRASHED"}},    # transient
            {"deployment": {"id": "d", "status": "DEPLOYING"}},  # resets counter
            {"deployment": {"id": "d", "status": "CRASHED"}},    # fresh first
            {"deployment": {"id": "d", "status": "SUCCESS"}},    # stabilized
        ])
        with patch("deploy.gql", side_effect=lambda *a, **k: next(statuses)):
            with patch("deploy.time.sleep"):
                # Should NOT raise: no 2 consecutive CRASHED.
                wait_for_deployment("dep-4", "tok", timeout=120)

    def test_raises_on_timeout(self):
        """Absolute timeout still fires if Railway never stabilizes."""
        # Always return DEPLOYING, never terminal
        with patch("deploy.gql", return_value={"deployment": {"id": "d", "status": "DEPLOYING"}}):
            with patch("deploy.time.sleep"):
                with patch("deploy.time.time", side_effect=[0, 100, 200, 300]):  # fast-forward past timeout
                    with pytest.raises(RuntimeError, match="timed out"):
                        wait_for_deployment("dep-5", "tok", timeout=1)

    def test_source_has_two_consecutive_guard(self):
        """Source-string guard: the constant must exist with value 2.
        Locks in the tolerance window documented in the function's comment
        block. If anyone widens to 3+ they should update the comment +
        this assertion together."""
        import pathlib
        src = pathlib.Path("cli/deploy.py").read_text()
        assert "_MAX_CONSECUTIVE_TRANSIENT_FAILURES = 2" in src, (
            "wait_for_deployment must enforce a 2-consecutive-failure tolerance "
            "(not 1, to allow Railway CRASHED→SUCCESS flicker; not 3+, to still "
            "surface real crashes promptly)"
        )
        assert "transient_failure_count" in src, (
            "wait_for_deployment must track transient_failure_count to implement "
            "the 2-consecutive rule"
        )
