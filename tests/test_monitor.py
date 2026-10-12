# tests/test_monitor.py - Test suite for the health monitor modules
#
# All external calls (requests, Railway API, subprocess pg_dump, Anthropic API)
# are mocked; no real network traffic is made.
# Registry tests use a throwaway PostgreSQL database created for the test
# session (the scratch_pg_url fixture in tests/conftest.py) and skip when no
# local server is reachable; they never touch an existing database.

import gzip
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from tests._fake_pg_tools import SAMPLE_DUMP, pg_run, write_dump

# Ensure the project root is on sys.path so imports work from any cwd.
sys.path.insert(0, str(Path(__file__).parent.parent))

from monitor import registry as reg_module
from monitor.ping import ping, ping_with_retry, check_app
from tests._guarded_session_mock import patched_probe_get
from monitor.backup import list_backups, snapshot, get_service_database_url

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS monitor_apps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_name VARCHAR(255) NOT NULL,
    live_url VARCHAR(500) NOT NULL,
    railway_project_id VARCHAR(255) NOT NULL UNIQUE,
    railway_service_id VARCHAR(255) NOT NULL,
    railway_environment_id VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'unknown',
    last_checked TIMESTAMP,
    last_healthy TIMESTAMP,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    baseline_latency_ms INTEGER,
    last_latency_ms INTEGER
)
"""


@pytest.fixture()
def isolated_registry(scratch_pg_url):
    """Point the registry at the session's scratch PostgreSQL; start each test empty.

    Not autouse: the ping, backup-listing and URL-lookup tests need no database.
    """
    import psycopg2

    def _test_conn():
        return psycopg2.connect(scratch_pg_url)

    conn = _test_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)
            cur.execute("DELETE FROM monitor_apps")
        conn.commit()
    finally:
        conn.close()

    with patch.object(reg_module, "_get_conn", _test_conn):
        yield


@pytest.fixture()
def sample_app(isolated_registry):
    """Register a sample app and return its dict."""
    app_id = reg_module.register_app(
        app_name="test-app",
        live_url="https://test-app.up.railway.app",
        railway_project_id="proj-123",
        railway_service_id="svc-456",
        railway_environment_id="env-789",
    )
    return reg_module.get_app(app_id)


# ---------------------------------------------------------------------------
# ping() tests
# ---------------------------------------------------------------------------

class TestPing:
    def test_ping_success_returns_correct_structure(self):
        """ping() returns ok=True with status_code, latency_ms, error=None on HTTP 200."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patched_probe_get("monitor.ping", return_value=mock_response) as mock_get:
            result = ping("https://myapp.up.railway.app")

        mock_get.assert_called_once_with(
            "https://myapp.up.railway.app/health", timeout=10, allow_redirects=False
        )
        assert result["ok"] is True
        assert result["status_code"] == 200
        assert isinstance(result["latency_ms"], int)
        assert result["latency_ms"] >= 0
        assert result["error"] is None

    def test_ping_non_200_returns_ok_false(self):
        """ping() returns ok=False and an error string when HTTP status is not 200."""
        mock_response = MagicMock()
        mock_response.status_code = 503

        with patched_probe_get("monitor.ping", return_value=mock_response):
            result = ping("https://myapp.up.railway.app")

        assert result["ok"] is False
        assert result["status_code"] == 503
        assert "503" in result["error"]

    def test_ping_connection_error_returns_ok_false(self):
        """ping() handles a ConnectionError gracefully, returning ok=False."""
        import requests as req

        with patched_probe_get("monitor.ping", side_effect=req.exceptions.ConnectionError("refused"),
        ):
            result = ping("https://unreachable.example.com")

        assert result["ok"] is False
        assert result["status_code"] == 0
        assert result["latency_ms"] == 0
        assert result["error"] is not None
        assert "refused" in result["error"]

    def test_ping_timeout_returns_ok_false(self):
        """ping() handles a Timeout exception gracefully."""
        import requests as req

        with patched_probe_get("monitor.ping", side_effect=req.exceptions.Timeout(),
        ):
            result = ping("https://slow.example.com", timeout=5)

        assert result["ok"] is False
        assert result["status_code"] == 0
        assert "Timeout" in result["error"]

    def test_ping_appends_health_path(self):
        """ping() always appends /health to the base URL."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patched_probe_get("monitor.ping", return_value=mock_response) as mock_get:
            ping("https://myapp.up.railway.app/")

        args, _ = mock_get.call_args
        assert args[0] == "https://myapp.up.railway.app/health"


# ---------------------------------------------------------------------------
# ping_with_retry() tests
# ---------------------------------------------------------------------------

class TestPingWithRetry:
    def test_returns_ok_true_on_first_attempt(self):
        """ping_with_retry() returns ok=True with attempts=1 when Tier 1 passes."""
        ok_response = MagicMock()
        ok_response.status_code = 200

        with patched_probe_get("monitor.ping", return_value=ok_response):
            result = ping_with_retry("https://myapp.up.railway.app", retries=3, backoff=0)

        assert result["ok"] is True
        assert result["attempts"] == 1
        assert result["final_result"]["ok"] is True

    def test_retries_correct_number_of_times(self):
        """ping_with_retry() makes exactly `retries` attempts when all fail."""
        import requests as req

        with patched_probe_get("monitor.ping", side_effect=req.exceptions.ConnectionError("refused"),
        ) as mock_get:
            with patch("monitor.ping.time.sleep"):
                result = ping_with_retry(
                    "https://myapp.up.railway.app", retries=3, backoff=0
                )

        assert result["ok"] is False
        assert result["attempts"] == 3
        assert mock_get.call_count == 3

    def test_returns_ok_true_if_second_attempt_succeeds(self):
        """ping_with_retry() returns ok=True when the second attempt passes."""
        import requests as req

        fail_response = req.exceptions.ConnectionError("refused")
        ok_response = MagicMock()
        ok_response.status_code = 200

        with patched_probe_get("monitor.ping", side_effect=[fail_response, ok_response],
        ):
            with patch("monitor.ping.time.sleep"):
                result = ping_with_retry(
                    "https://myapp.up.railway.app", retries=3, backoff=1
                )

        assert result["ok"] is True
        assert result["attempts"] == 2

    def test_sleeps_between_attempts(self):
        """ping_with_retry() calls time.sleep between failed attempts."""
        import requests as req

        with patched_probe_get("monitor.ping", side_effect=req.exceptions.ConnectionError("refused"),
        ):
            with patch("monitor.ping.time.sleep") as mock_sleep:
                ping_with_retry("https://myapp.up.railway.app", retries=3, backoff=5.0)

        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(5.0)

    def test_no_sleep_after_last_attempt(self):
        """ping_with_retry() does not sleep after the final failed attempt."""
        import requests as req

        with patched_probe_get("monitor.ping", side_effect=req.exceptions.ConnectionError("refused"),
        ):
            with patch("monitor.ping.time.sleep") as mock_sleep:
                ping_with_retry("https://myapp.up.railway.app", retries=1, backoff=99)

        assert mock_sleep.call_count == 0


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_register_app_returns_uuid(self, isolated_registry):
        """register_app() returns a non-empty UUID string."""
        app_id = reg_module.register_app(
            "my-app", "https://my-app.up.railway.app", "p1", "s1", "e1"
        )
        assert isinstance(app_id, str)
        assert len(app_id) == 36  # UUID format

    def test_get_app_returns_correct_fields(self, sample_app):
        """get_app() returns a dict with all expected fields."""
        required_fields = {
            "id", "app_name", "live_url", "railway_project_id",
            "railway_service_id", "railway_environment_id",
            "status", "last_checked", "last_healthy",
            "consecutive_failures", "created_at",
        }
        assert required_fields.issubset(set(sample_app.keys()))
        assert sample_app["app_name"] == "test-app"
        assert sample_app["live_url"] == "https://test-app.up.railway.app"
        assert sample_app["status"] == "unknown"
        assert sample_app["consecutive_failures"] == 0

    def test_get_app_raises_key_error_for_unknown_id(self, isolated_registry):
        """get_app() raises KeyError for an unregistered ID."""
        with pytest.raises(KeyError):
            reg_module.get_app("00000000-0000-0000-0000-000000000000")

    def test_list_apps_returns_all(self, isolated_registry):
        """list_apps() returns all registered apps."""
        reg_module.register_app("app-a", "https://a.example.com", "p1", "s1", "e1")
        reg_module.register_app("app-b", "https://b.example.com", "p2", "s2", "e2")
        apps = reg_module.list_apps()
        assert len(apps) == 2

    def test_list_apps_filters_by_status(self, isolated_registry):
        """list_apps(status=...) returns only apps with that status."""
        app_id_a = reg_module.register_app("app-a", "https://a.example.com", "p1", "s1", "e1")
        app_id_b = reg_module.register_app("app-b", "https://b.example.com", "p2", "s2", "e2")
        reg_module.update_status(app_id_a, "healthy")
        reg_module.update_status(app_id_b, "down")

        healthy = reg_module.list_apps(status="healthy")
        down = reg_module.list_apps(status="down")

        assert len(healthy) == 1
        assert healthy[0]["app_name"] == "app-a"
        assert len(down) == 1
        assert down[0]["app_name"] == "app-b"

    def test_update_status_sets_healthy(self, sample_app):
        """update_status() sets status to healthy and clears consecutive_failures."""
        app_id = sample_app["id"]
        # First mark as down to set consecutive_failures > 0
        reg_module.update_status(app_id, "down")
        reg_module.update_status(app_id, "down")
        reg_module.update_status(app_id, "healthy")

        updated = reg_module.get_app(app_id)
        assert updated["status"] == "healthy"
        assert updated["consecutive_failures"] == 0
        assert updated["last_healthy"] is not None

    def test_update_status_counts_only_checks_in_which_the_app_did_not_answer(self, sample_app):
        """consecutive_failures counts 'down' check results. 'degraded' means
        the app answered on a retry, so it ends the streak instead of adding
        to it."""
        app_id = sample_app["id"]
        reg_module.update_status(app_id, "down")
        reg_module.update_status(app_id, "down")
        assert reg_module.get_app(app_id)["consecutive_failures"] == 2

        reg_module.update_status(app_id, "degraded")
        updated = reg_module.get_app(app_id)
        assert updated["status"] == "degraded"
        assert updated["consecutive_failures"] == 0

    def test_update_status_outside_a_check_leaves_the_counter_alone(self, sample_app):
        """check_result=False records a status without counting a cycle."""
        app_id = sample_app["id"]
        reg_module.update_status(app_id, "down")
        for status in ("down", "degraded", "unknown"):
            reg_module.update_status(app_id, status, check_result=False)
            updated = reg_module.get_app(app_id)
            assert (updated["status"], updated["consecutive_failures"]) == (status, 1)

    def test_one_failing_cycle_counts_once(self, sample_app):
        """A whole failing cycle as run_checks runs it: the check (Tier 1 and
        Tier 2 both fail) and then the diagnosis, which escalates. That is one
        failed cycle, not two; at two per cycle an app would be dropped from
        the registry, and from the daily backups, after 5 bad cycles, not 10."""
        from monitor import diagnose

        app_id = sample_app["id"]
        down = {"ok": False, "status_code": 0, "latency_ms": 0, "error": "connection refused"}
        not_fixable = {"fixable": False, "fix_type": "unknown", "diagnosis": "x", "action": None}
        with patch("monitor.ping.ping", return_value=down), \
             patch("monitor.ping.ping_with_retry",
                   return_value={"ok": False, "attempts": 3, "final_result": down}), \
             patch.object(diagnose, "_get_token", return_value="t"), \
             patch.object(diagnose, "tier3_diagnose", return_value=not_fixable), \
             patch.object(diagnose, "tier4_diagnose",
                          return_value={"fixable": False, "escalate": True}), \
             patch.object(diagnose, "_escalate"):
            for cycle in (1, 2, 3):
                assert check_app(app_id)["ok"] is False
                diagnose.run_diagnosis(app_id)
                updated = reg_module.get_app(app_id)
                assert (updated["status"], updated["consecutive_failures"]) == ("down", cycle)

    def test_an_app_that_answers_on_retry_never_reaches_the_deregistration_count(self, sample_app):
        """Tier 1 fails, Tier 2 gets an answer: 'degraded'. Twelve such cycles
        in a row must not add up to 12 "failures" and drop an app that is up."""
        app_id = sample_app["id"]
        slow = {"ok": False, "status_code": 0, "latency_ms": 0, "error": "Timeout after 10s"}
        answered = {"ok": True, "status_code": 200, "latency_ms": 900, "error": None}
        with patch("monitor.ping.ping", return_value=slow), \
             patch("monitor.ping.ping_with_retry",
                   return_value={"ok": True, "attempts": 2, "final_result": answered}):
            for _cycle in range(12):
                assert check_app(app_id)["ok"] is True
        updated = reg_module.get_app(app_id)
        assert (updated["status"], updated["consecutive_failures"]) == ("degraded", 0)

    def test_a_fix_applied_by_the_diagnosis_does_not_count_a_second_failure(self, sample_app):
        from monitor import backup, diagnose

        app_id = sample_app["id"]
        reg_module.update_status(app_id, "down")
        fixable = {"fixable": True, "fix_type": "restart", "diagnosis": "x", "action": "restart"}
        with patch.object(diagnose, "_get_token", return_value="t"), \
             patch.object(diagnose, "tier3_diagnose", return_value=fixable), \
             patch.object(backup, "snapshot", return_value="/tmp/x.sql.gz"), \
             patch.object(diagnose, "apply_fix", return_value=True):
            diagnose.run_diagnosis(app_id)
        updated = reg_module.get_app(app_id)
        assert (updated["status"], updated["consecutive_failures"]) == ("degraded", 1)

    def test_update_status_resets_failures_on_healthy(self, sample_app):
        """update_status() resets consecutive_failures to 0 when healthy."""
        app_id = sample_app["id"]
        reg_module.update_status(app_id, "down")
        reg_module.update_status(app_id, "down")
        assert reg_module.get_app(app_id)["consecutive_failures"] == 2

        reg_module.update_status(app_id, "healthy")
        assert reg_module.get_app(app_id)["consecutive_failures"] == 0

    def test_remove_app_deletes_record(self, sample_app):
        """remove_app() removes the app from the registry."""
        app_id = sample_app["id"]
        reg_module.remove_app(app_id)
        with pytest.raises(KeyError):
            reg_module.get_app(app_id)

    def test_remove_app_raises_for_unknown_id(self, isolated_registry):
        """remove_app() raises KeyError for an unregistered ID."""
        with pytest.raises(KeyError):
            reg_module.remove_app("00000000-0000-0000-0000-000000000000")


# ---------------------------------------------------------------------------
# backup.list_backups() tests
# ---------------------------------------------------------------------------

class TestListBackups:
    def test_list_backups_returns_newest_first(self, tmp_path, monkeypatch):
        """list_backups() returns files sorted newest first."""
        import monitor.backup as backup_module

        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)
        app_dir = tmp_path / "test-app"
        app_dir.mkdir()

        # Create files with distinct mtime by writing them sequentially.
        file_a = app_dir / "20240101T000000Z.sql.gz"
        file_b = app_dir / "20240102T000000Z.sql.gz"
        file_c = app_dir / "20240103T000000Z.sql.gz"
        file_a.touch()
        file_b.touch()
        file_c.touch()

        # Force distinct mtimes.
        os.utime(str(file_a), (1000, 1000))
        os.utime(str(file_b), (2000, 2000))
        os.utime(str(file_c), (3000, 3000))

        backups = backup_module.list_backups("test-app")
        assert len(backups) == 3
        assert backups[0] == str(file_c)
        assert backups[1] == str(file_b)
        assert backups[2] == str(file_a)

    def test_list_backups_returns_empty_for_missing_dir(self, tmp_path, monkeypatch):
        """list_backups() returns an empty list when no backups exist."""
        import monitor.backup as backup_module

        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)
        assert backup_module.list_backups("nonexistent-app") == []


# ---------------------------------------------------------------------------
# apply_fix() tests
# ---------------------------------------------------------------------------

class TestApplyFix:
    def test_apply_fix_restart_calls_railway_redeploy_mutation(self, sample_app):
        """apply_fix('restart') calls the serviceInstanceRedeploy GraphQL mutation."""
        from monitor.diagnose import apply_fix

        mock_gql = MagicMock(return_value={})
        with patch("monitor.diagnose._gql", mock_gql):
            result = apply_fix(sample_app, "restart", token="fake-token")

        assert result is True
        assert mock_gql.call_count == 1
        called_query = mock_gql.call_args[0][0]
        assert "serviceInstanceRedeploy" in called_query
        called_vars = mock_gql.call_args[0][1]
        assert called_vars["serviceId"] == sample_app["railway_service_id"]
        assert called_vars["environmentId"] == sample_app["railway_environment_id"]

    def test_apply_fix_redeploy_calls_railway_redeploy_mutation(self, sample_app):
        """apply_fix('redeploy') also calls the serviceInstanceRedeploy mutation."""
        from monitor.diagnose import apply_fix

        mock_gql = MagicMock(return_value={})
        with patch("monitor.diagnose._gql", mock_gql):
            result = apply_fix(sample_app, "redeploy", token="fake-token")

        assert result is True
        called_query = mock_gql.call_args[0][0]
        assert "serviceInstanceRedeploy" in called_query

    def test_apply_fix_env_var_missing_returns_false(self, sample_app):
        """apply_fix('env_var_missing') returns False (alert only; cannot infer value)."""
        from monitor.diagnose import apply_fix

        with patch("monitor.diagnose._gql") as mock_gql:
            result = apply_fix(sample_app, "env_var_missing", token="fake-token")

        assert result is False
        mock_gql.assert_not_called()

    def test_apply_fix_unsupported_returns_false(self, sample_app):
        """apply_fix() returns False for an unsupported fix_type."""
        from monitor.diagnose import apply_fix

        with patch("monitor.diagnose._gql") as mock_gql:
            result = apply_fix(sample_app, "schema_change", token="fake-token")

        assert result is False
        mock_gql.assert_not_called()

    def test_apply_fix_returns_false_on_gql_error(self, sample_app):
        """apply_fix() returns False and logs error if GQL call raises."""
        from monitor.diagnose import apply_fix

        with patch("monitor.diagnose._gql", side_effect=RuntimeError("API error")):
            result = apply_fix(sample_app, "restart", token="fake-token")

        assert result is False


# ---------------------------------------------------------------------------
# Snapshot ordering test: snapshot called before DB-touching fix
# ---------------------------------------------------------------------------

class TestSnapshotOrdering:
    def test_snapshot_called_before_restart_fix(self, sample_app, monkeypatch):
        """run_diagnosis() calls snapshot() before apply_fix() for restart."""
        from monitor import diagnose, backup

        call_order = []

        def fake_tier3_diagnose(app):
            return {
                "fixable": True,
                "fix_type": "restart",
                "diagnosis": "service crashed",
                "action": "restart",
            }

        def fake_snapshot(app, reason):
            call_order.append("snapshot")
            return "/tmp/fake-backup.sql.gz"

        def fake_apply_fix(app, fix_type, token):
            call_order.append("apply_fix")
            return True

        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")
        monkeypatch.setattr(diagnose, "tier3_diagnose", fake_tier3_diagnose)
        monkeypatch.setattr(backup, "snapshot", fake_snapshot)
        monkeypatch.setattr(diagnose, "apply_fix", fake_apply_fix)

        diagnose.run_diagnosis(sample_app["id"])

        assert call_order == ["snapshot", "apply_fix"], (
            f"Expected snapshot before apply_fix, got: {call_order}"
        )

    def test_fix_aborted_if_snapshot_fails(self, sample_app, monkeypatch):
        """run_diagnosis() aborts the fix and escalates if snapshot() raises."""
        from monitor import diagnose, backup

        apply_fix_called = []

        def fake_tier3_diagnose(app):
            return {
                "fixable": True,
                "fix_type": "restart",
                "diagnosis": "service crashed",
                "action": "restart",
            }

        def failing_snapshot(app, reason):
            raise RuntimeError("disk full")

        def fake_apply_fix(app, fix_type, token):
            apply_fix_called.append(True)
            return True

        def fake_tier4_diagnose(app, tier3_result):
            return {
                "fixable": False,
                "fix_applied": False,
                "fix_description": "escalated",
                "escalate": True,
            }

        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")
        monkeypatch.setattr(diagnose, "tier3_diagnose", fake_tier3_diagnose)
        monkeypatch.setattr(backup, "snapshot", failing_snapshot)
        monkeypatch.setattr(diagnose, "apply_fix", fake_apply_fix)
        monkeypatch.setattr(diagnose, "tier4_diagnose", fake_tier4_diagnose)

        diagnose.run_diagnosis(sample_app["id"])

        assert apply_fix_called == [], "apply_fix must NOT be called when snapshot fails"


# ---------------------------------------------------------------------------
# get_service_database_url() tests
# ---------------------------------------------------------------------------

class TestGetServiceDatabaseUrl:
    def test_returns_database_url_from_railway(self):
        """get_service_database_url() returns the DATABASE_URL from Railway variables."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "variables": {
                    "DATABASE_URL": "postgresql://user:pass@host:5432/db",
                    "NODE_ENV": "production",
                }
            }
        }
        mock_response.raise_for_status = MagicMock()

        with patch("monitor.backup.provider_post", return_value=mock_response):
            url = get_service_database_url("proj", "svc", "env", "fake-token")

        assert url == "postgresql://user:pass@host:5432/db"

    def test_raises_if_database_url_missing(self):
        """get_service_database_url() raises RuntimeError if DATABASE_URL is not set."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {"variables": {"NODE_ENV": "production"}}
        }
        mock_response.raise_for_status = MagicMock()

        with patch("monitor.backup.provider_post", return_value=mock_response):
            with pytest.raises(RuntimeError, match="DATABASE_URL not found"):
                get_service_database_url("proj", "svc", "env", "fake-token")


# ---------------------------------------------------------------------------
# snapshot() tests
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_calls_pg_dump_with_list_form(self, sample_app, tmp_path, monkeypatch):
        """snapshot() calls pg_dump using list-form subprocess (no shell=True)."""
        import monitor.backup as backup_module

        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)
        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")

        fake_db_url = "postgresql://user:pass@host:5432/db"

        with patch.object(backup_module, "get_service_database_url", return_value=fake_db_url):
            with patch("monitor.backup.shutil.which", return_value="/usr/bin/pg_dump"):
                mock_proc = MagicMock()
                mock_proc.returncode = 0
                mock_proc.stdout = SAMPLE_DUMP
                mock_proc.stderr = b""
                with patch("monitor.backup.subprocess.run", side_effect=pg_run(mock_proc)) as mock_run:
                    backup_path = backup_module.snapshot(sample_app, reason="test")

        # Verify list-form call (first positional arg is a list, not a string)
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert isinstance(cmd, list), "subprocess.run must be called with a list (no shell=True)"
        assert cmd[0].endswith("pg_dump")

        # Verify no shell=True
        kwargs = call_args[1]
        assert not kwargs.get("shell", False), "shell=True must never be used"

    def test_snapshot_creates_gzip_file(self, sample_app, tmp_path, monkeypatch):
        """snapshot() creates a .sql.gz file at the expected path."""
        import monitor.backup as backup_module

        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)
        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")

        fake_db_url = "postgresql://user:pass@host:5432/db"
        sql_content = SAMPLE_DUMP

        with patch.object(backup_module, "get_service_database_url", return_value=fake_db_url):
            with patch("monitor.backup.shutil.which", return_value="/usr/bin/pg_dump"):
                mock_proc = MagicMock()
                mock_proc.returncode = 0
                mock_proc.stdout = sql_content
                mock_proc.stderr = b""
                with patch("monitor.backup.subprocess.run", side_effect=pg_run(mock_proc)):
                    backup_path = backup_module.snapshot(sample_app, reason="test")

        assert backup_path.endswith(".sql.gz")
        assert Path(backup_path).exists()

        with gzip.open(backup_path, "rb") as gz:
            content = gz.read()
        assert content == sql_content

    def test_snapshot_file_permissions_are_restricted(self, sample_app, tmp_path, monkeypatch):
        """snapshot() creates backup files with mode 0o600."""
        import monitor.backup as backup_module

        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)
        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")

        fake_db_url = "postgresql://user:pass@host:5432/db"

        with patch.object(backup_module, "get_service_database_url", return_value=fake_db_url):
            with patch("monitor.backup.shutil.which", return_value="/usr/bin/pg_dump"):
                mock_proc = MagicMock()
                mock_proc.returncode = 0
                mock_proc.stdout = SAMPLE_DUMP
                mock_proc.stderr = b""
                with patch("monitor.backup.subprocess.run", side_effect=pg_run(mock_proc)):
                    backup_path = backup_module.snapshot(sample_app, reason="test")

        file_mode = stat.S_IMODE(os.stat(backup_path).st_mode)
        assert file_mode == 0o600, f"Expected 0o600, got {oct(file_mode)}"

    def test_snapshot_raises_if_pg_dump_not_available(self, sample_app, monkeypatch):
        """snapshot() raises RuntimeError if pg_dump is not installed."""
        import monitor.backup as backup_module

        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")
        fake_db_url = "postgresql://user:pass@host:5432/db"

        with patch.object(backup_module, "get_service_database_url", return_value=fake_db_url):
            with patch("monitor.backup.shutil.which", return_value=None):
                with pytest.raises(RuntimeError, match="pg_dump not found"):
                    backup_module.snapshot(sample_app, reason="test")

    def test_snapshot_raises_if_pg_dump_fails(self, sample_app, tmp_path, monkeypatch):
        """snapshot() raises RuntimeError if pg_dump exits non-zero."""
        import monitor.backup as backup_module

        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)
        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")

        fake_db_url = "postgresql://user:pass@host:5432/db"

        with patch.object(backup_module, "get_service_database_url", return_value=fake_db_url):
            with patch("monitor.backup.shutil.which", return_value="/usr/bin/pg_dump"):
                mock_proc = MagicMock()
                mock_proc.returncode = 1
                mock_proc.stdout = b""
                mock_proc.stderr = b"could not connect"
                with patch("monitor.backup.subprocess.run", side_effect=pg_run(mock_proc)):
                    with pytest.raises(RuntimeError, match="pg_dump failed"):
                        backup_module.snapshot(sample_app, reason="test")

    def test_snapshot_retries_on_auth_failure(self, sample_app, tmp_path, monkeypatch):
        """snapshot() retries with fresh credentials on password auth failure."""
        import monitor.backup as backup_module

        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)
        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")

        fake_db_url = "postgresql://user:pass@host:5432/db"

        # First call fails with auth error, second succeeds
        fail_proc = MagicMock()
        fail_proc.returncode = 1
        fail_proc.stdout = b""
        fail_proc.stderr = b'FATAL:  password authentication failed for user "railway"'

        ok_proc = MagicMock()
        ok_proc.returncode = 0
        ok_proc.stdout = SAMPLE_DUMP
        ok_proc.stderr = b""

        with patch.object(backup_module, "get_service_database_url", return_value=fake_db_url):
            with patch("monitor.backup.shutil.which", return_value="/usr/bin/pg_dump"):
                with patch("monitor.backup.subprocess.run", side_effect=pg_run(fail_proc, ok_proc)):
                    with patch("monitor.backup.time.sleep"):
                        backup_path = backup_module.snapshot(sample_app, reason="test")

        assert backup_path.endswith(".sql.gz")
        assert Path(backup_path).exists()
