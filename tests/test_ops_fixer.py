# tests/test_ops_fixer.py - Tests for ops/fixer.py
#
# Tests cover: trigger_redeploy, upsert_env_var, the limits both apply to every
# caller, and the absence of any way to commit to a repository.
# The Railway GraphQL call is mocked via unittest.mock; one test sends it to a
# listener on loopback through the real bounded exchange.

import ast
import http.server
import json
import threading
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

_ROOT = str(Path(__file__).parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import ops.fixer as fixer


# The platform's own Railway ids, as the fixer reads them from the environment.
PROJECT, ENVIRONMENT, WEB, WORKER = "proj_platform", "env_platform", "svc_web", "svc_worker"


@pytest.fixture(autouse=True)
def _configured_platform(monkeypatch):
    """The Railway targets the fixer may act on are environment-driven too."""
    monkeypatch.setenv("RAILWAY_PROJECT_ID", PROJECT)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", ENVIRONMENT)
    monkeypatch.setenv("RAILWAY_WEB_SERVICE_ID", WEB)
    monkeypatch.setenv("RAILWAY_WORKER_SERVICE_ID", WORKER)


# ---------------------------------------------------------------------------
# trigger_redeploy
# ---------------------------------------------------------------------------


class TestTriggerRedeploy:
    def test_dry_run_returns_true_without_api_call(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")

        with patch.object(fixer, "_railway_gql") as mock_gql:
            result = fixer.trigger_redeploy(WORKER, ENVIRONMENT, dry_run=True)

        assert result is True
        mock_gql.assert_not_called()

    def test_returns_false_when_no_token(self, monkeypatch):
        monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)

        result = fixer.trigger_redeploy(WORKER, ENVIRONMENT)

        assert result is False

    def test_returns_true_on_successful_redeploy(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")

        with patch.object(fixer, "_railway_gql", return_value={"data": {"serviceInstanceRedeploy": True}}):
            result = fixer.trigger_redeploy(WEB, ENVIRONMENT, token="rw_token")

        assert result is True

    def test_returns_false_when_graphql_returns_errors(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")

        error_response = {"errors": [{"message": "Service not found"}]}
        with patch.object(fixer, "_railway_gql", return_value=error_response):
            result = fixer.trigger_redeploy(WORKER, ENVIRONMENT, token="rw_token")

        assert result is False

    def test_returns_false_on_network_error(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")

        with patch.object(fixer, "_railway_gql", side_effect=Exception("Connection refused")):
            result = fixer.trigger_redeploy(WORKER, ENVIRONMENT, token="rw_token")

        assert result is False

    def test_passes_correct_service_and_env_ids(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")

        with patch.object(fixer, "_railway_gql", return_value={"data": {}}) as mock_gql:
            fixer.trigger_redeploy(WORKER, ENVIRONMENT, token="rw_token")

        call_args = mock_gql.call_args
        variables = call_args[0][1]
        assert variables["serviceId"] == WORKER
        assert variables["environmentId"] == ENVIRONMENT

    def test_uses_env_var_token_when_no_explicit_token(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_from_env")

        with patch.object(fixer, "_railway_gql", return_value={"data": {}}) as mock_gql:
            fixer.trigger_redeploy(WORKER, ENVIRONMENT)

        mock_gql.assert_called_once()


# ---------------------------------------------------------------------------
# upsert_env_var
# ---------------------------------------------------------------------------


class TestUpsertEnvVar:
    def test_dry_run_returns_true_without_api_call(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")

        with patch.object(fixer, "_railway_gql") as mock_gql:
            result = fixer.upsert_env_var(
                PROJECT, WORKER, ENVIRONMENT, "ADMIN_EMAIL", "ops@example.test", dry_run=True
            )

        assert result is True
        mock_gql.assert_not_called()

    def test_returns_false_when_no_token(self, monkeypatch):
        monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)

        result = fixer.upsert_env_var(PROJECT, WORKER, ENVIRONMENT, "ADMIN_EMAIL", "ops@example.test")

        assert result is False

    def test_returns_true_on_success(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")

        with patch.object(fixer, "_railway_gql", return_value={"data": {"variableCollectionUpsert": True}}):
            result = fixer.upsert_env_var(
                PROJECT, WORKER, ENVIRONMENT, "ADMIN_EMAIL", "ops@example.test", token="rw_token"
            )

        assert result is True

    def test_returns_false_on_graphql_errors(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")

        with patch.object(fixer, "_railway_gql", return_value={"errors": [{"message": "Unauthorized"}]}):
            result = fixer.upsert_env_var(
                PROJECT, WORKER, ENVIRONMENT, "ADMIN_EMAIL", "ops@example.test", token="rw_token"
            )

        assert result is False

    def test_returns_false_on_network_error(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")

        with patch.object(fixer, "_railway_gql", side_effect=Exception("timeout")):
            result = fixer.upsert_env_var(
                PROJECT, WORKER, ENVIRONMENT, "ADMIN_EMAIL", "ops@example.test", token="rw_token"
            )

        assert result is False

    def test_sends_correct_input_structure(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")

        with patch.object(fixer, "_railway_gql", return_value={"data": {}}) as mock_gql:
            fixer.upsert_env_var(
                PROJECT, WORKER, ENVIRONMENT, "DATABASE_URL", "postgres://...", token="rw_token"
            )

        call_args = mock_gql.call_args
        variables = call_args[0][1]
        inp = variables["input"]
        assert inp["projectId"] == PROJECT
        assert inp["serviceId"] == WORKER
        assert inp["environmentId"] == ENVIRONMENT
        assert inp["variables"] == {"DATABASE_URL": "postgres://..."}

    def test_uses_env_var_token_when_no_explicit_token(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_env_token")

        with patch.object(fixer, "_railway_gql", return_value={"data": {}}) as mock_gql:
            fixer.upsert_env_var(PROJECT, WORKER, ENVIRONMENT, "ADMIN_EMAIL", "ops@example.test")

        mock_gql.assert_called_once()

    def test_handles_empty_string_value(self, monkeypatch):
        """Empty string is a valid value (clearing an env var)."""
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")

        with patch.object(fixer, "_railway_gql", return_value={"data": {}}) as mock_gql:
            result = fixer.upsert_env_var(
                PROJECT, WORKER, ENVIRONMENT, "ADMIN_EMAIL", "", token="rw_token"
            )

        assert result is True
        variables = mock_gql.call_args[0][1]
        assert variables["input"]["variables"]["ADMIN_EMAIL"] == ""


# ---------------------------------------------------------------------------
# _railway_gql (the one HTTP helper)
# ---------------------------------------------------------------------------


class TestHttpHelpers:
    def test_railway_gql_sends_post_with_auth(self):
        """Railway GQL wrapper sets Authorization header and sends query+variables."""
        fake_response_data = json.dumps({"data": {"ok": True}}).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read = MagicMock(return_value=fake_response_data)

        captured_requests = []

        def capture_urlopen(req, timeout=None, max_body_bytes=None):
            captured_requests.append((req, timeout, max_body_bytes))
            return mock_resp

        with patch("ops.fixer.bounded_urlopen", side_effect=capture_urlopen):
            result = fixer._railway_gql("query { me { id } }", {"var": "val"}, "rw_token")

        assert result == {"data": {"ok": True}}
        assert len(captured_requests) == 1
        req, timeout, max_body_bytes = captured_requests[0]
        assert timeout == fixer._RAILWAY_DEADLINE_S and max_body_bytes == fixer._RAILWAY_MAX_ANSWER_BYTES
        assert req.headers.get("Authorization") == "Bearer rw_token"
        body = json.loads(req.data.decode())
        assert body["query"] == "query { me { id } }"
        assert body["variables"] == {"var": "val"}


    def test_a_railway_call_that_never_finishes_ends_at_its_deadline(self, monkeypatch):
        """Through the real bounded exchange, against a listener that accepts
        the request and then drips its answer: the redeploy reports failure
        when the deadline passes instead of waiting."""
        import time as time_module

        class Drip(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Length", "40")
                self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        threading.Event().wait(0.25)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Drip)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        monkeypatch.setattr(fixer, "_RAILWAY_API", f"http://127.0.0.1:{server.server_address[1]}/graphql")
        monkeypatch.setattr(fixer, "_RAILWAY_DEADLINE_S", 1)
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")
        started = time_module.monotonic()
        try:
            assert fixer.trigger_redeploy(WORKER, ENVIRONMENT) is False
        finally:
            server.shutdown()
            server.server_close()
        assert time_module.monotonic() - started < 2.5


# ---------------------------------------------------------------------------
# The limits on live actions: what the fixer itself refuses
# ---------------------------------------------------------------------------
#
# Every automatic redeploy and variable write of the Python side ends in one
# of the two functions below (the ops playbooks call them, under the legacy
# ops agent and under the coordinator's run_playbook alike), so the limits are
# tested here, on the functions that make the call.


class TestRailwayTargetAllowlist:
    @pytest.mark.parametrize("service, environment", [
        ("svc_customer_app", ENVIRONMENT),
        (WORKER, "env_other"),
        ("", ENVIRONMENT),
        (WORKER, ""),
        (None, None),
    ])
    @pytest.mark.parametrize("dry_run", [False, True])
    def test_a_redeploy_of_anything_but_a_platform_service_is_refused(
            self, service, environment, dry_run, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")
        with patch.object(fixer, "_railway_gql") as mock_gql:
            assert fixer.trigger_redeploy(service, environment, dry_run=dry_run) is False
        mock_gql.assert_not_called()

    @pytest.mark.parametrize("unset", [
        ("RAILWAY_ENVIRONMENT_ID",),
        ("RAILWAY_WEB_SERVICE_ID", "RAILWAY_WORKER_SERVICE_ID"),
    ])
    def test_with_the_platform_ids_unset_nothing_is_a_platform_service(self, unset, monkeypatch):
        """Fails closed: an empty configuration must not match an empty argument."""
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")
        for name in unset:
            monkeypatch.delenv(name)
        with patch.object(fixer, "_railway_gql") as mock_gql:
            assert fixer.trigger_redeploy(WORKER, ENVIRONMENT) is False
            assert fixer.trigger_redeploy("", "") is False
            assert fixer.upsert_env_var(PROJECT, WORKER, ENVIRONMENT, "ADMIN_EMAIL", "a@example.test") is False
        mock_gql.assert_not_called()

    @pytest.mark.parametrize("project, service, environment, name", [
        ("proj_customer", WORKER, ENVIRONMENT, "ADMIN_EMAIL"),
        (PROJECT, "svc_customer_app", ENVIRONMENT, "ADMIN_EMAIL"),
        (PROJECT, WORKER, "env_other", "ADMIN_EMAIL"),
        (PROJECT, WORKER, ENVIRONMENT, "SESSION_SECRET"),
        (PROJECT, WORKER, ENVIRONMENT, "COORDINATOR_DRY_RUN"),
        (PROJECT, WORKER, ENVIRONMENT, "ENABLE_ORPHAN_SWEEP"),
        (PROJECT, WORKER, ENVIRONMENT, "admin_email"),
        ("", WORKER, ENVIRONMENT, "ADMIN_EMAIL"),
    ])
    @pytest.mark.parametrize("dry_run", [False, True])
    def test_a_variable_write_outside_the_platform_or_the_name_list_is_refused(
            self, project, service, environment, name, dry_run, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")
        with patch.object(fixer, "_railway_gql") as mock_gql:
            assert fixer.upsert_env_var(project, service, environment, name, "v", dry_run=dry_run) is False
        mock_gql.assert_not_called()

    def test_the_two_railway_playbooks_reach_only_the_configured_worker(self, monkeypatch):
        """The real playbooks, live, against a stubbed Railway call."""
        from ops.playbooks import env_config, railway_redeploy
        monkeypatch.setenv("RAILWAY_API_TOKEN", "rw_token")
        monkeypatch.setenv("ADMIN_EMAIL", "ops@example.test")
        with patch.object(fixer, "_railway_gql", return_value={"data": {}}) as mock_gql:
            assert railway_redeploy.fix({"dry_run": False}).success
            assert env_config.fix({"error_message": "ADMIN_EMAIL is not set", "dry_run": False}).success
        targets = [call.args[1] for call in mock_gql.call_args_list]
        assert targets[0] == {"serviceId": WORKER, "environmentId": ENVIRONMENT}
        assert targets[1]["input"]["projectId"] == PROJECT
        assert targets[1]["input"]["serviceId"] == WORKER
        assert list(targets[1]["input"]["variables"]) == ["ADMIN_EMAIL"]

    def test_the_variable_list_is_exactly_what_the_playbook_sets(self):
        from ops.playbooks import env_config
        assert {name for name, _why in env_config._KNOWN_VARS.values()} == set(fixer.SETTABLE_ENV_VARS)


# ---------------------------------------------------------------------------
# No path to a commit
# ---------------------------------------------------------------------------

class TestNothingCanCommit:
    """The three playbooks that committed Dockerfile.worker and
    requirements.worker.txt could not produce a commit against those files
    any more (each of their fixes was already in them), and were removed with
    the GitHub client. What is pinned here is that no such path is left."""

    ROOT = Path(__file__).parent.parent

    def test_the_fixer_has_no_commit_or_github_function(self):
        for gone in ("commit_file_changes", "get_file_content", "_github_request", "_github_headers",
                     "COMMITTABLE_PATHS", "_repo", "_branch"):
            assert not hasattr(fixer, gone), gone
        assert sorted(name for name in vars(fixer) if not name.startswith("_") and callable(vars(fixer)[name])
                      and getattr(vars(fixer)[name], "__module__", "") == "ops.fixer") == [
            "trigger_redeploy", "upsert_env_var"]

    def test_the_playbook_registry_is_the_two_railway_playbooks(self):
        from ops.playbook import PLAYBOOKS, load_fixer, match_playbook
        assert sorted(PLAYBOOKS) == ["env_config", "railway_crash"]
        assert sorted(p.name for p in (self.ROOT / "ops" / "playbooks").glob("*.py")) == [
            "__init__.py", "env_config.py", "railway_redeploy.py"]
        for category in PLAYBOOKS:
            assert callable(load_fixer(match_playbook(category)))
        for category in ("version_mismatch", "missing_dep", "missing_tool", "dockerfile", "app_logic", "unknown"):
            assert match_playbook(category) is None

    def test_no_module_of_the_ops_or_coordinator_side_reads_a_github_token_or_names_the_api(self):
        offenders = []
        for folder in ("ops", "coordinator", "monitor", "queue"):
            for source_path in sorted((self.ROOT / folder).rglob("*.py")):
                for node in ast.walk(ast.parse(source_path.read_text())):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                            node.value in ("GITHUB_TOKEN", "CAS_GITHUB_REPO", "CAS_GITHUB_BRANCH")
                            or "api.github.com" in node.value):
                        offenders.append((source_path.relative_to(self.ROOT).as_posix(), node.value[:40]))
        assert offenders == []

    def test_a_fix_result_carries_no_commit(self):
        from ops.playbook import FixResult
        assert set(FixResult.__dataclass_fields__) == {"success", "description", "requires_redeploy"}
