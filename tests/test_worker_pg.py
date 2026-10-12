"""Tests for queue/worker_pg.py infrastructure features.

All database calls and external services are mocked. These tests cover:
- Transient error detection
- Retry logic
- Concurrency guard
- Timeout setup
- Railway metadata storage
- Monitor auto-registration
- Background thread spawning
- Fail-build helper
"""

import os
import signal
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import requests

# Allow importing from queue/ without the package prefix (avoids stdlib queue conflict).
ROOT = Path(__file__).parent.parent
QUEUE_DIR = str(ROOT / "queue")
CLI_DIR = str(ROOT / "cli")
if QUEUE_DIR not in sys.path:
    sys.path.insert(0, QUEUE_DIR)
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import worker_pg
from tests._guarded_session_mock import health_answer, patched_probe_get
from datetime import datetime

# Every guarded status write takes the attempt token of the job that makes it
# (builds.started_at as the claim set it). These tests mock the database, so
# the value is never compared with a row; the writers only insist on having one.
ATTEMPT = datetime(2026, 1, 2, 3, 4, 5)


@pytest.fixture(autouse=True)
def _no_database_url(monkeypatch):
    """Every test in this module mocks the database. Whatever DATABASE_URL the
    runner has (the README runs the suite without one; CI and some local runs
    set one) must not turn a mocked test into one that opens a connection to a
    real server: worker code that asks for a connection of its own gets "not
    set" here unless the test supplies one."""
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture(autouse=True)
def _forget_process_wide_attempt_state():
    """The worker keeps two small process-wide records: the Railway projects
    attempts created that are on no build row, and when each build's series of
    retries began. One test's builds must not show up in the next test's."""
    yield
    with worker_pg._UNRECORDED_ATTEMPT_PROJECT_IDS_LOCK:
        worker_pg._UNRECORDED_ATTEMPT_PROJECT_IDS.clear()
    with worker_pg._RETRY_SERIES_LOCK:
        worker_pg._RETRY_SERIES_STARTED.clear()


# ---------------------------------------------------------------------------
# _is_transient_error()
# ---------------------------------------------------------------------------

class TestIsTransientError:
    def test_connection_refused_is_transient(self):
        assert worker_pg._is_transient_error("Connection refused by host") is True

    def test_connection_reset_is_transient(self):
        assert worker_pg._is_transient_error("Connection reset by peer") is True

    def test_timeout_is_transient(self):
        assert worker_pg._is_transient_error("Request timeout after 30s") is True

    def test_rate_limit_is_transient(self):
        assert worker_pg._is_transient_error("Rate limit exceeded") is True

    def test_429_is_transient(self):
        assert worker_pg._is_transient_error("HTTP 429 Too Many Requests") is True

    def test_503_is_transient(self):
        assert worker_pg._is_transient_error("Service unavailable (503)") is True

    def test_temporary_failure_is_transient(self):
        assert worker_pg._is_transient_error("Temporary failure in name resolution") is True

    def test_network_unreachable_is_transient(self):
        assert worker_pg._is_transient_error("Network is unreachable") is True

    @pytest.mark.parametrize("message, transient", [
        # status codes are tokens: the same digits inside a word, an id, a
        # version or a number are not a status code
        ("HTTP 429 Too Many Requests", True),
        ("Railway API 429: slow down", True),
        ("503 Service Unavailable", True),
        ("Service unavailable (503)", True),
        ("status=502", True),
        ("error:503", True),
        ("upstream returned 502.", True),
        ("[429]", True),
        ("build 3f429a1c failed: syntax error", False),
        ("build 1503 failed: SyntaxError", False),
        ("build 3f503a1c-0000 failed", False),
        ("port 5030 is in use", False),
        ("generated app v2.429.1 failed its lint", False),
        ("the page took 1.503 seconds to render", False),
        ("id 8d1429 is not a build", False),
        ("failed after 4290 ms", False),
        ("[Errno 22] Invalid argument", False),
        ("[Errno 2] No such file or directory: 'dropdb'", True),
        ("errno 2", True),
        ("Errno 20: Not a directory", False),
        # the worker's own timeout messages
        ("Deployment timed out after 300s", True),
        ("The read operation timed out", True),
        ("exchange deadline exceeded", True),
        ("Railway API call ProjectCreate ran out of time before attempt 2", True),
        ("Railway API answered 429 and no time is left to retry", True),
        ("Deploy health check timeout: the deployed app did not answer /health", True),
    ])
    def test_the_classifier_reads_tokens_and_the_workers_own_timeouts(self, message, transient):
        assert worker_pg._is_transient_error(message) is transient, message
        # the dead-letter category is read from the same classifier
        assert (worker_pg._classify_error(message) == "transient") is transient, message

    def test_what_the_workers_producers_actually_raise_is_transient(self):
        """The strings above are copies; these are made by the code that makes
        them: the deploy wait that gives up, an API call that ran out of its
        deadline or was told 429 with no time left, and a child process that
        outlived its timeout."""
        import subprocess
        import time as real_time
        from unittest.mock import MagicMock

        import deploy

        produced = []
        with pytest.raises(RuntimeError) as gave_up:
            deploy.wait_for_deployment("dep-1", "tok", timeout=0)
        produced.append(str(gave_up.value))

        with pytest.raises(requests.exceptions.Timeout) as out_of_time:
            deploy.gql("query Probe { me { id } }", None, "tok", deadline=real_time.monotonic() - 1)
        produced.append(str(out_of_time.value))

        busy = MagicMock(status_code=429)
        with patch.object(deploy, "provider_post", return_value=busy), \
                pytest.raises(requests.exceptions.Timeout) as told_429:
            deploy.gql("query Probe { me { id } }", None, "tok", deadline=real_time.monotonic() + 0.5)
        produced.append(str(told_429.value))

        with pytest.raises(subprocess.TimeoutExpired) as slow_child:
            subprocess.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
        produced.append(str(slow_child.value))

        assert len(produced) == 4
        for message in produced:
            assert worker_pg._is_transient_error(message), message

    def test_permanent_error_not_transient(self):
        assert worker_pg._is_transient_error("SyntaxError: unexpected token") is False

    def test_spec_validation_not_transient(self):
        assert worker_pg._is_transient_error("Spec validation failed with 3 errors") is False

    def test_empty_string_not_transient(self):
        assert worker_pg._is_transient_error("") is False

    def test_case_insensitive_matching(self):
        assert worker_pg._is_transient_error("CONNECTION REFUSED") is True

    def test_executable_missing_is_transient(self):
        assert worker_pg._is_transient_error(
            "BrowserType.launch: Executable doesn't exist at /ms-playwright/chromium"
        ) is True

    def test_errno2_is_transient(self):
        assert worker_pg._is_transient_error("[Errno 2] No such file or directory: 'dropdb'") is True

    def test_command_not_found_is_transient(self):
        assert worker_pg._is_transient_error("bash: createdb: command not found") is True

    def test_npm_error_is_transient(self):
        assert worker_pg._is_transient_error("npm ERR! code ENOENT") is True

    def test_oom_killed_is_transient(self):
        assert worker_pg._is_transient_error("Process killed by signal 9") is True

    def test_db_starting_up_is_transient(self):
        assert worker_pg._is_transient_error("the database system is starting up") is True


# ---------------------------------------------------------------------------
# _requeue_for_retry()
# ---------------------------------------------------------------------------

class TestRequeueForRetry:
    def test_requeue_increments_retry_count(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        worker_pg._requeue_for_retry(mock_conn, "build-123", 0, "timeout", attempt=ATTEMPT)

        mock_cursor.execute.assert_called_once()
        args = mock_cursor.execute.call_args[0]
        params = args[1]
        assert params[0] == 1  # new_count = 0 + 1
        assert "Retry 1/6" in params[1]
        assert params[2] == "build-123"
        mock_conn.commit.assert_called_once()

    def test_requeue_sets_status_to_queued(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        worker_pg._requeue_for_retry(mock_conn, "build-123", 1, "503 error", attempt=ATTEMPT)

        sql = mock_cursor.execute.call_args[0][0]
        assert "status = 'queued'" in sql
        assert "started_at = NULL" in sql
        assert "completed_at = NULL" in sql


# ---------------------------------------------------------------------------
# _set_status()
# ---------------------------------------------------------------------------

class TestSetStatus:
    def _conn(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn, mock_cursor

    def test_worker_can_never_write_live(self):
        """The review gate: promoting preview to live belongs to the web app
        after review. The worker's stage writer refuses it outright, and
        refuses every other status that is not a pipeline stage: 'failed' in
        particular has exactly one writer, _fail_build."""
        mock_conn, mock_cursor = self._conn()
        for forbidden in ("live", "preview", "queued", "failed", "abandoned", "intake", "bogus"):
            with pytest.raises(ValueError):
                worker_pg._set_status(mock_conn, "build-1", forbidden, attempt=ATTEMPT)
        mock_cursor.execute.assert_not_called()
        mock_conn.commit.assert_not_called()

    def test_takes_no_url_or_message_arguments(self):
        """A function that cannot publish has no business taking a live_url."""
        mock_conn, _ = self._conn()
        with pytest.raises(TypeError):
            worker_pg._set_status(mock_conn, "build-1", "testing", live_url="https://app.example.test", attempt=ATTEMPT)
        with pytest.raises(TypeError):
            worker_pg._set_status(mock_conn, "build-1", "testing", error_message="x", attempt=ATTEMPT)

    @pytest.mark.parametrize("target, reachable_from", [
        ("building", ["building"]),
        ("testing", ["building", "testing"]),
        ("deploying", ["building", "testing", "deploying"]),
    ])
    def test_write_is_guarded_to_the_same_or_an_earlier_stage(self, target, reachable_from):
        """The UPDATE lists the statuses the build may currently be in: the
        target stage and the stages before it, never a later one and never a
        status the worker does not own. tests/test_worker_pg_realdb.py runs the
        statement against Postgres."""
        mock_conn, mock_cursor = self._conn()
        worker_pg._set_status(mock_conn, "build-1", target, attempt=ATTEMPT)
        sql, params = mock_cursor.execute.call_args[0]
        assert "status = ANY(%s)" in sql
        # The last parameter is the attempt token.
        assert params == (target, "build-1", reachable_from, ATTEMPT)

    def test_attempt_token_is_passed_to_the_statement(self):
        mock_conn, mock_cursor = self._conn()
        worker_pg._set_status(mock_conn, "build-1", "testing", attempt="attempt-token")
        sql, params = mock_cursor.execute.call_args[0]
        assert "started_at IS NOT DISTINCT FROM %s::timestamp" in sql
        assert "IS NULL OR" not in sql, "the guard must not have a way to switch itself off"
        assert params[-1] == "attempt-token"

    def test_a_missing_attempt_token_is_an_error_not_a_weaker_guard(self):
        """attempt=None used to drop the attempt predicate and leave the
        status guard alone. Now nothing is written at all."""
        mock_conn, mock_cursor = self._conn()
        with pytest.raises(ValueError, match="attempt token missing"):
            worker_pg._set_status(mock_conn, "build-1", "testing", attempt=None)
        mock_cursor.execute.assert_not_called()

    def test_a_row_without_started_at_is_matched_with_sql_null(self):
        mock_conn, mock_cursor = self._conn()
        worker_pg._set_status(mock_conn, "build-1", "testing", attempt=worker_pg.ROW_WITHOUT_STARTED_AT)
        assert mock_cursor.execute.call_args[0][1][-1] is None

    def test_reports_when_no_row_matched(self):
        mock_conn, mock_cursor = self._conn()
        mock_cursor.rowcount = 0
        assert worker_pg._set_status(mock_conn, "build-1", "deploying", attempt=ATTEMPT) is False
        mock_cursor.rowcount = 1
        assert worker_pg._set_status(mock_conn, "build-1", "deploying", attempt=ATTEMPT) is True

    def test_advance_stage_stops_the_pipeline_when_no_row_matched(self):
        mock_conn, mock_cursor = self._conn()
        mock_cursor.rowcount = 0
        with pytest.raises(worker_pg.BuildNoLongerOwnedError):
            worker_pg._advance_stage(mock_conn, "build-1", "deploying", ATTEMPT)
        mock_cursor.rowcount = 1
        worker_pg._advance_stage(mock_conn, "build-1", "deploying", ATTEMPT)

    def test_non_terminal_status_no_completed_at(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        worker_pg._set_status(mock_conn, "build-1", "testing", attempt=ATTEMPT)

        sql = mock_cursor.execute.call_args[0][0]
        assert "completed_at" not in sql


# ---------------------------------------------------------------------------
# The Railway ids are written by the preview write, and only by it
# ---------------------------------------------------------------------------

class TestPreviewWriteRecordsRailwayIds:
    def _conn(self, row=("build-1", None)):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = row
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn, mock_cursor

    def test_one_statement_sets_preview_and_the_ids(self):
        """provision_postgres generates a Postgres password that exists nowhere
        else on the platform side, so the DATABASE_URL travels with the ids."""
        mock_conn, mock_cursor = self._conn()
        # Synthetic DSN assembled at runtime so no source line looks like a real credential.
        db_url = "postgresql://postgres:" + "secret-pw" + "@postgres.railway.internal:5432/railway"
        railway = worker_pg._railway_record("proj-1", "svc-1", "env-1", db_url)

        assert worker_pg._set_preview_status(
            mock_conn, "build-1", preview_url="https://x.example.test", attempt=ATTEMPT,
            railway=railway) is True

        assert mock_cursor.execute.call_count == 1
        sql, params = mock_cursor.execute.call_args[0]
        assert "status = 'preview'" in sql
        for column in ("railway_project_id", "railway_service_id", "railway_environment_id",
                       "railway_database_url"):
            assert f"{column} = CASE WHEN %s THEN %s ELSE {column} END" in sql
        assert params == (
            "build-1", "pending",
            True, "proj-1", True, "svc-1", True, "env-1", True, db_url,
            "https://x.example.test", "build-1", ATTEMPT,
        )
        mock_conn.commit.assert_called_once()

    def test_a_local_preview_leaves_the_ids_alone(self):
        mock_conn, mock_cursor = self._conn()
        worker_pg._set_preview_status(mock_conn, "build-1", preview_url=None, attempt=ATTEMPT)
        params = mock_cursor.execute.call_args[0][1]
        assert params[2:10] == (False, None, False, None, False, None, False, None)


# ---------------------------------------------------------------------------
# _active_build_count()
# ---------------------------------------------------------------------------

class TestActiveBuildCount:
    def test_returns_count_from_query(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (3,)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        count = worker_pg._active_build_count(mock_conn)
        assert count == 3

        sql = mock_cursor.execute.call_args[0][0]
        assert "building" in sql
        assert "testing" in sql
        assert "deploying" in sql


# ---------------------------------------------------------------------------
# _claim_next_job() concurrency guard
# ---------------------------------------------------------------------------

class TestClaimNextJob:
    def test_returns_none_at_concurrency_limit(self):
        mock_conn = MagicMock()

        with patch.object(worker_pg, "_active_build_count", return_value=worker_pg._MAX_CONCURRENT):
            result = worker_pg._claim_next_job(mock_conn)

        assert result is None
        mock_conn.rollback.assert_called_once()

    def test_returns_none_when_queue_empty(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        # the claim lock's one row (True: this worker holds it), then no queued build
        mock_cursor.fetchone.side_effect = [(True,), None]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(worker_pg, "_active_build_count", return_value=0), \
             patch.object(worker_pg, "_get_daily_api_spend", return_value=0.0):
            result = worker_pg._claim_next_job(mock_conn)

        assert result is None
        mock_conn.rollback.assert_called_once()

    def test_returns_none_when_another_worker_holds_the_claim_lock(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [(False,)]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(worker_pg, "_active_build_count") as count:
            assert worker_pg._claim_next_job(mock_conn) is None

        count.assert_not_called()
        mock_conn.rollback.assert_called_once()

    def test_returns_none_when_daily_spend_exceeded(self):
        mock_conn = MagicMock()
        original_cap = worker_pg._DAILY_SPEND_CAP

        try:
            worker_pg._DAILY_SPEND_CAP = 50.0
            with patch.object(worker_pg, "_active_build_count", return_value=0), \
                 patch.object(worker_pg, "_get_daily_api_spend", return_value=55.0):
                result = worker_pg._claim_next_job(mock_conn)
        finally:
            worker_pg._DAILY_SPEND_CAP = original_cap

        assert result is None
        mock_conn.rollback.assert_called_once()

    def test_allows_build_when_under_spend_cap(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [(True,), None]  # the claim lock, then an empty queue
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        original_cap = worker_pg._DAILY_SPEND_CAP

        try:
            worker_pg._DAILY_SPEND_CAP = 50.0
            with patch.object(worker_pg, "_active_build_count", return_value=0), \
                 patch.object(worker_pg, "_get_daily_api_spend", return_value=25.0):
                result = worker_pg._claim_next_job(mock_conn)
        finally:
            worker_pg._DAILY_SPEND_CAP = original_cap

        # Returns None because queue is empty, not because of spend cap
        assert result is None

    def test_spend_cap_disabled_when_zero(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [(True,), None]  # the claim lock, then an empty queue
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        original_cap = worker_pg._DAILY_SPEND_CAP

        try:
            worker_pg._DAILY_SPEND_CAP = 0
            with patch.object(worker_pg, "_active_build_count", return_value=0), \
                 patch.object(worker_pg, "_get_daily_api_spend") as mock_spend:
                result = worker_pg._claim_next_job(mock_conn)
                mock_spend.assert_not_called()  # should skip spend check entirely
        finally:
            worker_pg._DAILY_SPEND_CAP = original_cap

    # 2026-04-23 regression guard: the dispatch UPDATE must persist
    # build_engine to builds.build_engine, sourced from CAS_BUILD_ENGINE at
    # dispatch time. The admin UI reads this column directly (with the
    # archetype heuristic only as a historical-row fallback) so v2-built
    # rows whose v1-shape input specs were translated in-memory by
    # compose.intake.translate_v1_spec are no longer misclassified as v1.
    # See bug-admin-engine-badge-misreports-v2-as-v1.
    def _make_dispatch_conn(self, job_row):
        """Build a mock conn whose cursor answers the three fetchone calls of a
        claim: the claim lock's row, the queued build, the dispatch UPDATE's
        started_at."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [(True,), job_row, (ATTEMPT,)]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn, mock_cursor

    def _job_row(self):
        # (id, tier, app_name, spec, customer_id, retry_count, rebuild_request,
        #  error_message, customer_email, customer_name)
        return (
            "build-1", "build", "demo-app", {"app_name": "demo-app"},
            "cust-1", 0, None, None, "c@example.com", "Customer",
        )

    def test_dispatch_persists_build_engine_v2_when_env_v2(self, monkeypatch):
        monkeypatch.setenv("CAS_BUILD_ENGINE", "v2")
        mock_conn, mock_cursor = self._make_dispatch_conn(self._job_row())
        with patch.object(worker_pg, "_active_build_count", return_value=0), \
             patch.object(worker_pg, "_get_daily_api_spend", return_value=0.0):
            worker_pg._claim_next_job(mock_conn)
        # The second execute is the dispatch UPDATE (first is SELECT FOR UPDATE).
        update_call = next(
            c for c in mock_cursor.execute.call_args_list
            if "status = 'building'" in str(c[0][0])
        )
        sql, params = update_call[0]
        assert "build_engine = %s" in sql
        assert params[0] == "v2"

    def test_dispatch_persists_build_engine_v2_when_env_unset(self, monkeypatch):
        # 2026-04-27: default flipped. With CAS_BUILD_ENGINE unset the
        # worker now treats it as v2. v1 is legacy + opt-in only.
        monkeypatch.delenv("CAS_BUILD_ENGINE", raising=False)
        mock_conn, mock_cursor = self._make_dispatch_conn(self._job_row())
        with patch.object(worker_pg, "_active_build_count", return_value=0), \
             patch.object(worker_pg, "_get_daily_api_spend", return_value=0.0):
            worker_pg._claim_next_job(mock_conn)
        update_call = next(
            c for c in mock_cursor.execute.call_args_list
            if "status = 'building'" in str(c[0][0])
        )
        params = update_call[0][1]
        assert params[0] == "v2"

    def test_dispatch_persists_build_engine_v1_when_env_v1_explicit(self, monkeypatch):
        # Backward-compat opt-in: CAS_BUILD_ENGINE=v1 runs the legacy
        # Claude-per-build pipeline. Kept for forensics / repro of v1
        # production builds; cli/build.py preserved for reference.
        monkeypatch.setenv("CAS_BUILD_ENGINE", "v1")
        mock_conn, mock_cursor = self._make_dispatch_conn(self._job_row())
        with patch.object(worker_pg, "_active_build_count", return_value=0), \
             patch.object(worker_pg, "_get_daily_api_spend", return_value=0.0):
            worker_pg._claim_next_job(mock_conn)
        update_call = next(
            c for c in mock_cursor.execute.call_args_list
            if "status = 'building'" in str(c[0][0])
        )
        params = update_call[0][1]
        assert params[0] == "v1"

    def test_dispatch_falls_back_to_v2_on_unknown_engine(self, monkeypatch):
        # 2026-04-27: unknown values fall back to v2 (was v1).
        monkeypatch.setenv("CAS_BUILD_ENGINE", "v3-experimental")
        mock_conn, mock_cursor = self._make_dispatch_conn(self._job_row())
        with patch.object(worker_pg, "_active_build_count", return_value=0), \
             patch.object(worker_pg, "_get_daily_api_spend", return_value=0.0):
            worker_pg._claim_next_job(mock_conn)
        update_call = next(
            c for c in mock_cursor.execute.call_args_list
            if "status = 'building'" in str(c[0][0])
        )
        params = update_call[0][1]
        # The build-stage gate (separate code path) raises ValueError on unknown
        # engines; the dispatch attribution defensively defaults to v2 so the
        # row still gets a value rather than NULL (2026-04-27 default flip).
        assert params[0] == "v2"


# ---------------------------------------------------------------------------
# _process_job() retry on transient error
# ---------------------------------------------------------------------------

class TestProcessJobRetry:
    def _make_job(self, retry_count=0):
        return {
            "attempt": ATTEMPT,
            "id": "build-abc",
            "app_name": "test-app",
            "tier": "build",
            "spec": {"app_name": "test-app"},
            "customer_id": "cust-1",
            "customer_email": "test@example.com",
            "customer_name": "Test User",
            "retry_count": retry_count,
        }

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_run_pipeline", side_effect=RuntimeError("Connection refused by host"))
    def test_transient_error_triggers_retry(self, mock_pipeline, mock_requeue, mock_fail, mock_email):
        mock_conn = MagicMock()
        worker_pg._process_job(self._make_job(retry_count=0), mock_conn)

        mock_requeue.assert_called_once()
        mock_fail.assert_not_called()

    @patch.object(worker_pg, "_append_log")
    @patch.object(worker_pg, "_run_pipeline", return_value={"local_preview": True})
    def test_reensures_conn_after_pipeline_returns(self, mock_pipeline, mock_log):
        """Q1: after the pipeline thread returns it may have reconnected and
        closed the original conn, so _process_job must re-ensure the conn
        before any post-pipeline write. _process_job does not call _ensure_conn
        before submitting the pipeline, so on the (early-returning) local_preview
        path the re-ensure firing proves the Q1 fix is in place."""
        mock_conn = MagicMock()
        with patch.object(worker_pg, "_ensure_conn", side_effect=lambda c: c) as mock_ensure:
            worker_pg._process_job(self._make_job(retry_count=0), mock_conn)
        assert mock_ensure.called, "expected _process_job to re-ensure conn after the pipeline returns"

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_run_pipeline", side_effect=RuntimeError("Connection refused by host"))
    def test_max_retries_exceeded_fails_build(self, mock_pipeline, mock_requeue, mock_fail, mock_email):
        mock_conn = MagicMock()
        worker_pg._process_job(self._make_job(retry_count=worker_pg._MAX_RETRIES), mock_conn)

        mock_fail.assert_called_once()
        mock_requeue.assert_not_called()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_run_pipeline", side_effect=RuntimeError("SyntaxError in generated code"))
    def test_permanent_error_retries_then_fails(self, mock_pipeline, mock_requeue, mock_fail, mock_email):
        """Code errors now auto-retry (rebuild fresh). Fails only after max retries."""
        mock_conn = MagicMock()
        # At retry_count=0, code errors now get requeued (auto-rebuild)
        worker_pg._process_job(self._make_job(retry_count=0), mock_conn)
        mock_requeue.assert_called_once()
        mock_fail.assert_not_called()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_run_pipeline", side_effect=RuntimeError("SyntaxError in generated code"))
    def test_permanent_error_fails_after_max_retries(self, mock_pipeline, mock_requeue, mock_fail, mock_email):
        mock_conn = MagicMock()
        worker_pg._process_job(self._make_job(retry_count=worker_pg._MAX_RETRIES), mock_conn)

        mock_fail.assert_called_once()
        mock_requeue.assert_not_called()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_clear_build_artifact")
    @patch.object(worker_pg, "_run_pipeline", side_effect=RuntimeError("SyntaxError in generated code"))
    def test_code_error_clears_artifact_on_retry(self, mock_pipeline, mock_clear_artifact, mock_requeue, mock_fail, mock_email):
        """When a code error triggers retry, _clear_build_artifact is called before requeue."""
        mock_conn = MagicMock()
        worker_pg._process_job(self._make_job(retry_count=0), mock_conn)

        # Code error (not transient) should clear artifact for a fresh rebuild,
        # guarded by this attempt's token
        mock_clear_artifact.assert_called_once_with(mock_conn, "build-abc", ATTEMPT)
        # And then requeue
        mock_requeue.assert_called_once()
        mock_fail.assert_not_called()


# ---------------------------------------------------------------------------
# Repeated-error early-exit
# ---------------------------------------------------------------------------

class TestRepeatedErrorEarlyExit:
    """When the same error repeats across consecutive attempts, stop retrying."""

    def _make_job(self, retry_count=0):
        return {
            "attempt": ATTEMPT,
            "id": "build-abc",
            "app_name": "test-app",
            "tier": "build",
            "spec": {"app_name": "test-app"},
            "customer_id": "cust-1",
            "customer_email": "test@example.com",
            "customer_name": "Test User",
            "retry_count": retry_count,
        }

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_run_pipeline",
                  side_effect=RuntimeError("Expected 401/403 or redirect, got 200 at /dashboard"))
    def test_identical_error_stops_retry(self, mock_pipeline, mock_requeue, mock_fail, mock_email):
        """If builds.error_message matches current error, fail immediately."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # Simulate previous error stored with "Retry 1/6: " prefix
        mock_cursor.fetchone.return_value = (
            "Retry 1/6: Expected 401/403 or redirect, got 200 at /dashboard",
        )
        worker_pg._process_job(self._make_job(retry_count=1), mock_conn)

        mock_fail.assert_called_once()
        mock_requeue.assert_not_called()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_run_pipeline",
                  side_effect=RuntimeError("Expected 401/403 or redirect, got 200 at /dashboard"))
    def test_different_error_allows_retry(self, mock_pipeline, mock_requeue, mock_fail, mock_email):
        """Different error from previous attempt should still allow retry."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # Previous error was different
        mock_cursor.fetchone.return_value = (
            "Retry 1/6: npm install failed with exit code 1",
        )
        worker_pg._process_job(self._make_job(retry_count=1), mock_conn)

        mock_requeue.assert_called_once()
        mock_fail.assert_not_called()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_run_pipeline",
                  side_effect=RuntimeError("SyntaxError in generated code"))
    def test_first_attempt_skips_check(self, mock_pipeline, mock_requeue, mock_fail, mock_email):
        """At retry_count=0, the repeated-error check is not performed."""
        mock_conn = MagicMock()
        worker_pg._process_job(self._make_job(retry_count=0), mock_conn)

        mock_requeue.assert_called_once()
        mock_fail.assert_not_called()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_run_pipeline",
                  side_effect=RuntimeError("Connection refused by host"))
    def test_transient_error_still_retries_when_repeated(self, mock_pipeline, mock_requeue, mock_fail, mock_email):
        """Transient errors bypass the repeated-error check (may self-resolve)."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # Same transient error stored from previous attempt
        mock_cursor.fetchone.return_value = (
            "Retry 1/6: Connection refused by host",
        )
        worker_pg._process_job(self._make_job(retry_count=1), mock_conn)

        mock_requeue.assert_called_once()
        mock_fail.assert_not_called()


# ---------------------------------------------------------------------------
# QualityGateError is non-retryable
# ---------------------------------------------------------------------------

class TestQualityGateError:
    def _make_job(self, retry_count=0):
        return {
            "attempt": ATTEMPT,
            "id": "build-abc",
            "app_name": "test-app",
            "tier": "build",
            "spec": {"app_name": "test-app"},
            "customer_id": "cust-1",
            "customer_email": "test@example.com",
            "customer_name": "Test User",
            "retry_count": retry_count,
        }

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_run_pipeline", side_effect=worker_pg.QualityGateError("Design review: 3 critical issues"))
    def test_quality_gate_error_not_retried(self, mock_pipeline, mock_requeue, mock_fail, mock_email):
        """QualityGateError at retry_count=0 should still fail, not retry."""
        mock_conn = MagicMock()
        worker_pg._process_job(self._make_job(retry_count=0), mock_conn)
        mock_fail.assert_called_once()
        mock_requeue.assert_not_called()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_run_pipeline", side_effect=worker_pg.QualityGateError("QA: 5 failures"))
    def test_quality_gate_error_not_retried_even_at_zero(self, mock_pipeline, mock_requeue, mock_fail, mock_email):
        """Even with retry_count=0 and retries available, QualityGateError skips retry."""
        mock_conn = MagicMock()
        worker_pg._process_job(self._make_job(retry_count=0), mock_conn)
        mock_fail.assert_called_once()
        mock_requeue.assert_not_called()


# ---------------------------------------------------------------------------
# DESIGN_QG_RETRY1: DesignQualityGateError gets 1 retry with design-issue
# injection; QAQualityGateError stays non-retryable; base QualityGateError
# stays non-retryable (safe default).
# ---------------------------------------------------------------------------

class TestQualityGateHierarchy:
    def test_design_subclass_is_quality_gate(self):
        assert issubclass(worker_pg.DesignQualityGateError, worker_pg.QualityGateError)

    def test_qa_subclass_is_quality_gate(self):
        assert issubclass(worker_pg.QAQualityGateError, worker_pg.QualityGateError)

    def test_existing_except_catches_both_subclasses(self):
        """Regression guard: `except QualityGateError` still catches new subclasses."""
        try:
            raise worker_pg.DesignQualityGateError("d")
        except worker_pg.QualityGateError:
            design_caught = True
        try:
            raise worker_pg.QAQualityGateError("q")
        except worker_pg.QualityGateError:
            qa_caught = True
        assert design_caught and qa_caught


class TestInjectDesignIssuesHelper:
    """_inject_design_issues_as_rebuild_request reads builds.design_review_results
    and writes a formatted block to builds.rebuild_request. Fail-safe: never raises."""

    def _mk_conn(self, issues, existing_rebuild_request=None):
        """Build a mock conn whose SELECT returns (issues_json, existing_rebuild_request)."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = (issues, existing_rebuild_request)
        return mock_conn, mock_cursor

    def test_injects_critical_issues_when_row_empty(self):
        issues = [
            {"severity": "critical", "location": "views/index.ejs",
             "issue": "Low contrast on hero heading", "fix": "Use #fff"},
            {"severity": "warning", "issue": "Minor spacing issue"},
        ]
        mock_conn, mock_cursor = self._mk_conn(issues, existing_rebuild_request=None)

        ok = worker_pg._inject_design_issues_as_rebuild_request(mock_conn, "build-1")

        assert ok is True
        # 1st call: SELECT, 2nd call: UPDATE rebuild_request
        update_args = mock_cursor.execute.call_args_list[1][0]
        assert "UPDATE builds SET rebuild_request" in update_args[0]
        merged = update_args[1][0]
        assert "DESIGN REVIEW CRITICAL ISSUES" in merged
        assert "views/index.ejs" in merged
        assert "Low contrast" in merged
        assert "Use #fff" in merged
        # Warning-severity issue should NOT appear
        assert "Minor spacing issue" not in merged

    def test_preserves_existing_customer_rebuild_request(self):
        issues = [{"severity": "critical", "issue": "Dark text on dark bg"}]
        existing = "Customer wants larger hero buttons."
        mock_conn, mock_cursor = self._mk_conn(issues, existing_rebuild_request=existing)

        ok = worker_pg._inject_design_issues_as_rebuild_request(mock_conn, "build-1")

        assert ok is True
        merged = mock_cursor.execute.call_args_list[1][0][1][0]
        # Customer text first, design block below it
        customer_idx = merged.index("Customer wants larger hero buttons")
        design_idx = merged.index("DESIGN REVIEW CRITICAL ISSUES")
        assert customer_idx < design_idx

    def test_replaces_prior_design_block_not_stacks(self):
        """On a subsequent retry path, a stale design block gets replaced."""
        issues = [{"severity": "critical", "issue": "New issue"}]
        existing = (
            "Customer wants red buttons.\n\n"
            + worker_pg._DESIGN_QG_REBUILD_HEADER + "\n"
            "- Old issue from prior retry"
        )
        mock_conn, mock_cursor = self._mk_conn(issues, existing_rebuild_request=existing)

        ok = worker_pg._inject_design_issues_as_rebuild_request(mock_conn, "build-1")

        assert ok is True
        merged = mock_cursor.execute.call_args_list[1][0][1][0]
        assert "Customer wants red buttons" in merged
        assert "New issue" in merged
        assert "Old issue from prior retry" not in merged
        # Only ONE DESIGN REVIEW header, not stacked
        assert merged.count("DESIGN REVIEW CRITICAL ISSUES") == 1

    def test_no_critical_issues_returns_false(self):
        """Only warning-level issues → skip injection, caller still requeues."""
        issues = [{"severity": "warning", "issue": "Minor"}]
        mock_conn, mock_cursor = self._mk_conn(issues, existing_rebuild_request=None)

        ok = worker_pg._inject_design_issues_as_rebuild_request(mock_conn, "build-1")

        assert ok is False
        # Only 1 call: SELECT (no UPDATE)
        assert len(mock_cursor.execute.call_args_list) == 1

    def test_empty_issues_list_returns_false(self):
        mock_conn, mock_cursor = self._mk_conn([], existing_rebuild_request=None)

        ok = worker_pg._inject_design_issues_as_rebuild_request(mock_conn, "build-1")

        assert ok is False
        assert len(mock_cursor.execute.call_args_list) == 1

    def test_missing_build_row_returns_false(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = None

        ok = worker_pg._inject_design_issues_as_rebuild_request(mock_conn, "missing-build")

        assert ok is False

    def test_caps_issue_count(self):
        issues = [
            {"severity": "critical", "issue": f"Issue {i}"}
            for i in range(25)
        ]
        mock_conn, mock_cursor = self._mk_conn(issues, existing_rebuild_request=None)

        ok = worker_pg._inject_design_issues_as_rebuild_request(mock_conn, "build-1")

        assert ok is True
        merged = mock_cursor.execute.call_args_list[1][0][1][0]
        # First 10 included, 11-24 excluded
        assert "Issue 0" in merged
        assert f"Issue {worker_pg._DESIGN_QG_MAX_ISSUES - 1}" in merged
        assert f"Issue {worker_pg._DESIGN_QG_MAX_ISSUES}" not in merged
        assert "Issue 24" not in merged

    def test_db_error_returns_false_does_not_raise(self):
        """Fail-safe: any exception inside the helper returns False, no crash."""
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = RuntimeError("DB is gone")

        ok = worker_pg._inject_design_issues_as_rebuild_request(mock_conn, "build-1")

        assert ok is False

    def test_non_list_design_results_treated_as_empty(self):
        """Legacy/corrupt rows with dict or string in design_review_results → treat as empty."""
        mock_conn, mock_cursor = self._mk_conn(
            "not a list", existing_rebuild_request=None,
        )

        ok = worker_pg._inject_design_issues_as_rebuild_request(mock_conn, "build-1")

        assert ok is False


class TestDesignQualityGateRetry:
    """End-to-end: DesignQualityGateError through _process_job exception handler."""

    def _make_job(self, retry_count=0):
        return {
            "attempt": ATTEMPT,
            "id": "build-abc",
            "app_name": "test-app",
            "tier": "build",
            "spec": {"app_name": "test-app"},
            "customer_id": "cust-1",
            "customer_email": "test@example.com",
            "customer_name": "Test User",
            "retry_count": retry_count,
        }

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_inject_design_issues_as_rebuild_request")
    @patch.object(
        worker_pg, "_run_pipeline",
        side_effect=worker_pg.DesignQualityGateError(
            "Design review: 3 critical issues after fix attempts"
        ),
    )
    def test_design_qg_first_failure_requeues_with_injection(
        self, mock_pipeline, mock_inject, mock_requeue, mock_fail, mock_email,
    ):
        """DesignQG at retry_count=0 → inject + requeue, NOT fail."""
        mock_conn = MagicMock()
        worker_pg._process_job(self._make_job(retry_count=0), mock_conn)

        mock_inject.assert_called_once_with(mock_conn, "build-abc")
        mock_requeue.assert_called_once()
        mock_fail.assert_not_called()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_inject_design_issues_as_rebuild_request")
    @patch.object(
        worker_pg, "_run_pipeline",
        side_effect=worker_pg.DesignQualityGateError("Design review: 2 critical issues"),
    )
    def test_design_qg_second_failure_permanent(
        self, mock_pipeline, mock_inject, mock_requeue, mock_fail, mock_email,
    ):
        """DesignQG at retry_count=1 → fail permanently, no more retries."""
        mock_conn = MagicMock()
        worker_pg._process_job(self._make_job(retry_count=1), mock_conn)

        mock_fail.assert_called_once()
        mock_requeue.assert_not_called()
        mock_inject.assert_not_called()  # No retry → no injection

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_inject_design_issues_as_rebuild_request")
    @patch.object(
        worker_pg, "_run_pipeline",
        side_effect=worker_pg.QAQualityGateError("QA: 5 failures after fix attempts"),
    )
    def test_qa_qg_does_not_retry_regardless_of_count(
        self, mock_pipeline, mock_inject, mock_requeue, mock_fail, mock_email,
    ):
        """QAQG at retry_count=0 → fail permanently (structural, not stochastic)."""
        mock_conn = MagicMock()
        worker_pg._process_job(self._make_job(retry_count=0), mock_conn)

        mock_fail.assert_called_once()
        mock_requeue.assert_not_called()
        mock_inject.assert_not_called()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_inject_design_issues_as_rebuild_request")
    @patch.object(
        worker_pg, "_run_pipeline",
        side_effect=worker_pg.DesignQualityGateError("Design review: 4 critical issues"),
    )
    def test_design_qg_respects_the_retry_time_budget(
        self, mock_pipeline, mock_inject, mock_requeue, mock_fail, mock_email,
    ):
        """A build whose series of retries has run past the time budget
        fails instead of being retried, even with a retry left."""
        mock_conn = MagicMock()
        with patch.object(worker_pg, "_retry_series_age_s",
                          return_value=worker_pg._RETRY_TIME_BUDGET_S + 1):
            worker_pg._process_job(self._make_job(retry_count=0), mock_conn)

        mock_fail.assert_called_once()
        mock_requeue.assert_not_called()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(
        worker_pg, "_inject_design_issues_as_rebuild_request",
        return_value=False,  # Injection returns False (e.g. no critical issues in DB)
    )
    @patch.object(
        worker_pg, "_run_pipeline",
        side_effect=worker_pg.DesignQualityGateError("Design review: 1 critical issue"),
    )
    def test_design_qg_still_requeues_when_injection_skips(
        self, mock_pipeline, mock_inject, mock_requeue, mock_fail, mock_email,
    ):
        """Injection returning False (no critical issues in DB) must NOT block
        the retry. A stochastic re-gen can still help."""
        mock_conn = MagicMock()
        worker_pg._process_job(self._make_job(retry_count=0), mock_conn)

        mock_inject.assert_called_once()
        mock_requeue.assert_called_once()
        mock_fail.assert_not_called()


# ---------------------------------------------------------------------------
# Build cost tracking: threading.local cross-thread fix
# ---------------------------------------------------------------------------

class TestBuildCostTracking:
    """Verify that API cost metrics accumulated in the pipeline thread (ThreadPoolExecutor)
    are correctly transferred to _process_job via the _metrics_out shared list and written
    to builds.total_api_cost_usd.

    Root cause of the original bug: start_metrics_collection() writes to threading.local()
    inside the pipeline thread, but get_collected_metrics() was called in _process_job's
    finally block (the calling thread), which has its own empty thread-local storage.
    Fix: _run_pipeline drains threading.local into _metrics_out before returning.
    """

    def _make_job(self):
        return {
            "attempt": ATTEMPT,
            "id": "build-cost-test",
            "app_name": "cost-test-app",
            "tier": "build",
            "spec": {},
            "customer_id": "cust-1",
            "customer_email": "test@example.com",
            "customer_name": "Test User",
            "retry_count": 0,
        }

    @patch.object(worker_pg, "send_preview_ready")
    @patch.object(worker_pg, "send_review_pending")
    @patch.object(worker_pg, "_set_preview_status")
    def test_cost_written_to_db_when_pipeline_populates_metrics_out(
        self, mock_set_preview, mock_review_pending, mock_preview_ready
    ):
        """Pipeline thread populates _metrics_out; _process_job writes non-zero cost to DB."""
        from api_client import ApiCallMetrics

        from datetime import datetime as _dt
        fake_metric = ApiCallMetrics(
            module="build", model="claude-opus-4-6", stage="build",
            input_tokens=1000, output_tokens=500,
            cache_read_tokens=0, cache_write_tokens=0,
            api_mode="realtime", estimated_cost_usd=0.0175,
            build_id="build-cost-test",
            timestamp=_dt(2026, 4, 15, 0, 0, 0),
        )

        def fake_pipeline(job, conn, _metrics_out=None):
            if _metrics_out is not None:
                _metrics_out.append(fake_metric)
            return {
                "live_url": "https://cost-test.up.railway.app",
                "railway_project_id": "proj-1",
                "railway_service_id": "svc-1",
                "railway_environment_id": "env-1",
            }

        mock_set_preview.return_value = True
        mock_conn = MagicMock()

        with patch.object(worker_pg, "_run_pipeline", side_effect=fake_pipeline):
            with patch("monitor.registry.register_app"):
                worker_pg._process_job(self._make_job(), mock_conn)

        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cost_updates = [
            c for c in cursor.execute.call_args_list
            if "total_api_cost_usd" in str(c)
        ]
        assert cost_updates, "expected UPDATE total_api_cost_usd"
        cost_written = cost_updates[0][0][1][0]
        assert cost_written == pytest.approx(0.0175), (
            f"expected 0.0175 but got {cost_written}; "
            "check that _run_pipeline drains threading.local into _metrics_out"
        )

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_requeue_for_retry")
    def test_cost_zero_when_pipeline_raises_before_populating_metrics(
        self, mock_requeue, mock_email
    ):
        """Pipeline raises without touching _metrics_out; cost write is 0.0 (not broken)."""
        mock_conn = MagicMock()

        with patch.object(
            worker_pg, "_run_pipeline",
            side_effect=RuntimeError("Connection refused by host"),
        ):
            worker_pg._process_job(self._make_job(), mock_conn)

        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cost_updates = [
            c for c in cursor.execute.call_args_list
            if "total_api_cost_usd" in str(c)
        ]
        assert cost_updates, "expected UPDATE total_api_cost_usd even on failure"
        cost_written = cost_updates[0][0][1][0]
        assert cost_written == pytest.approx(0.0)

    @patch.object(worker_pg, "send_preview_ready")
    @patch.object(worker_pg, "send_review_pending")
    @patch.object(worker_pg, "_set_preview_status")
    def test_multi_metric_costs_are_summed(
        self, mock_set_preview, mock_review_pending, mock_preview_ready
    ):
        """Multiple metrics from different pipeline stages are summed correctly."""
        from api_client import ApiCallMetrics
        from datetime import datetime as _dt

        def make_metric(stage, cost):
            return ApiCallMetrics(
                module="build", model="claude-opus-4-6", stage=stage,
                input_tokens=100, output_tokens=50,
                cache_read_tokens=0, cache_write_tokens=0,
                api_mode="realtime", estimated_cost_usd=cost,
                build_id="build-cost-test",
                timestamp=_dt(2026, 4, 15, 0, 0, 0),
            )

        metrics = [
            make_metric("build", 0.0300),
            make_metric("review", 0.0050),
            make_metric("qa", 0.0010),
            make_metric("fix_attempt_1", 0.0080),
        ]
        expected_total = pytest.approx(0.0440)

        def fake_pipeline(job, conn, _metrics_out=None):
            if _metrics_out is not None:
                _metrics_out.extend(metrics)
            return {
                "live_url": "https://cost-test.up.railway.app",
                "railway_project_id": "proj-1",
                "railway_service_id": "svc-1",
                "railway_environment_id": "env-1",
            }

        mock_set_preview.return_value = True
        mock_conn = MagicMock()

        with patch.object(worker_pg, "_run_pipeline", side_effect=fake_pipeline):
            with patch("monitor.registry.register_app"):
                worker_pg._process_job(self._make_job(), mock_conn)

        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cost_updates = [
            c for c in cursor.execute.call_args_list
            if "total_api_cost_usd" in str(c)
        ]
        assert cost_updates, "expected UPDATE total_api_cost_usd"
        assert cost_updates[0][0][1][0] == expected_total, (
            "expected sum of all stage costs"
        )

    def test_run_pipeline_accepts_metrics_out_param(self):
        """_run_pipeline must accept _metrics_out as its third positional argument.

        If the signature drifts (e.g. someone removes the param), _process_job's
        submit(_run_pipeline, job, conn, _pipeline_metrics) would silently pass the
        list as an unexpected positional arg and the metrics would never be collected.
        """
        import inspect
        sig = inspect.signature(worker_pg._run_pipeline)
        params = list(sig.parameters.keys())
        assert "_metrics_out" in params, (
            "_run_pipeline must have a _metrics_out parameter for cross-thread cost collection"
        )
        # Must be the third param (index 2) so positional submit() call lands correctly
        assert params.index("_metrics_out") == 2, (
            "_metrics_out must be the third parameter (index 2) to match "
            "pipeline_executor.submit(_run_pipeline, job, conn, _pipeline_metrics)"
        )


# ---------------------------------------------------------------------------
# _process_job() success path with Railway metadata
# ---------------------------------------------------------------------------

class TestProcessJobSuccess:
    def _make_job(self):
        return {
            "attempt": ATTEMPT,
            "id": "build-abc",
            "app_name": "test-app",
            "tier": "build",
            "spec": {},
            "customer_id": "cust-1",
            "customer_email": "test@example.com",
            "customer_name": "Test User",
            "retry_count": 0,
        }

    @patch.object(worker_pg, "send_review_pending")
    @patch.object(worker_pg, "send_preview_ready")
    @patch.object(worker_pg, "_set_preview_status")
    @patch.object(worker_pg, "_run_pipeline")
    def test_the_preview_write_gets_the_railway_record_on_success(self, mock_pipeline, mock_set_preview, mock_email, mock_review):
        mock_set_preview.return_value = True
        deploy_result = {
            "live_url": "https://test-app.up.railway.app",
            "railway_project_id": "proj-1",
            "railway_service_id": "svc-1",
            "railway_environment_id": "env-1",
            "database_url": "postgresql://postgres:" + "secret-pw" + "@postgres.railway.internal:5432/railway",
        }
        mock_pipeline.return_value = deploy_result
        mock_conn = MagicMock()

        with patch("monitor.registry.register_app"):
            worker_pg._process_job(self._make_job(), mock_conn)

        # The ids and the database_url (the auto-generated PG password is
        # otherwise unrecoverable) reach the build row through the preview
        # write, in the one statement that also announces the deployment.
        mock_set_preview.assert_called_once()
        assert mock_set_preview.call_args.kwargs["attempt"] == ATTEMPT
        railway = mock_set_preview.call_args.kwargs["railway"]
        assert (railway["project_id"], railway["service_id"], railway["environment_id"]) == (
            "proj-1", "svc-1", "env-1")
        assert railway["database_url"] == (
            "postgresql://postgres:" + "secret-pw" + "@postgres.railway.internal:5432/railway")

    @patch.object(worker_pg, "send_review_pending")
    @patch.object(worker_pg, "send_preview_ready")
    @patch.object(worker_pg, "_set_preview_status")
    @patch.object(worker_pg, "_run_pipeline")
    def test_auto_registers_with_monitor(self, mock_pipeline, mock_set_preview, mock_email, mock_review):
        mock_set_preview.return_value = True
        deploy_result = {
            "live_url": "https://test-app.up.railway.app",
            "railway_project_id": "proj-1",
            "railway_service_id": "svc-1",
            "railway_environment_id": "env-1",
        }
        mock_pipeline.return_value = deploy_result
        mock_conn = MagicMock()

        with patch("monitor.registry.register_app") as mock_register:
            worker_pg._process_job(self._make_job(), mock_conn)

        mock_register.assert_called_once_with(
            app_name="test-app",
            live_url="https://test-app.up.railway.app",
            railway_project_id="proj-1",
            railway_service_id="svc-1",
            railway_environment_id="env-1",
        )

    @patch.object(worker_pg, "send_review_pending")
    @patch.object(worker_pg, "send_preview_ready")
    @patch.object(worker_pg, "_set_preview_status")
    @patch.object(worker_pg, "_run_pipeline")
    def test_a_deploy_result_without_railway_ids_previews_without_a_record(self, mock_pipeline, mock_set_preview, mock_email, mock_review):
        mock_set_preview.return_value = True
        mock_pipeline.return_value = {"live_url": "https://test-app.up.railway.app"}
        mock_conn = MagicMock()

        worker_pg._process_job(self._make_job(), mock_conn)
        mock_set_preview.assert_called_once_with(
            mock_conn, "build-abc", preview_url="https://test-app.up.railway.app",
            attempt=ATTEMPT, railway=None)


# ---------------------------------------------------------------------------
# _set_preview_status: arrival at preview, review reset, gated announcements
# ---------------------------------------------------------------------------

class TestPreviewTransition:
    """queue/worker_pg.py::_set_preview_status and the announcements gated on
    it in _process_job, with a mocked cursor.

    The function is one guarded UPDATE ... RETURNING; whether a row matched is
    the whole decision. What the statement does to a real row (which statuses
    it matches, that it resets an old verdict) is tested against Postgres in
    tests/test_worker_pg_realdb.py::TestPreviewTransition.
    """

    def _make_cursor_context(self, fetchone_results):
        """Return (mock_conn, mock_cursor) wired so that successive calls to
        cur.fetchone() yield the provided results. Supports the 'with
        conn.cursor() as cur' context-manager pattern used by the worker.
        """
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = list(fetchone_results)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn, mock_cursor

    def _job(self):
        return {
            "attempt": ATTEMPT,
            "id": "build-abc",
            "app_name": "test-app",
            "tier": "build",
            "spec": {},
            "customer_id": "cust-1",
            "customer_email": "test@example.com",
            "customer_name": "Test User",
            "retry_count": 0,
        }

    def test_arrival_at_preview_puts_the_build_into_review(self):
        """The UPDATE matched a row: one statement, review_status 'pending' by
        default, and True so the caller notifies the admin."""
        mock_conn, mock_cursor = self._make_cursor_context([("build-abc", None)])

        env = dict(os.environ)
        env.pop("CAS_HARBOR_AUTO_REVIEW", None)
        with patch.dict(os.environ, env, clear=True):
            landed = worker_pg._set_preview_status(
                mock_conn, "build-abc", preview_url="https://x.up.railway.app", attempt=ATTEMPT)

        assert landed is True
        assert mock_cursor.execute.call_count == 1
        sql, params = mock_cursor.execute.call_args_list[0][0]
        assert "status = 'preview'" in sql
        assert "review_status = %s" in sql
        assert "RETURNING id" in sql
        # params: build_id (for the prior project id), review_status, four
        # (record this column?, value) pairs, preview_url, build_id, attempt
        assert params == (
            "build-abc", "pending",
            False, None, False, None, False, None, False, None,
            "https://x.up.railway.app", "build-abc", ATTEMPT,
        )
        mock_conn.commit.assert_called_once()

    def test_the_review_reset_does_not_depend_on_the_old_review_status(self):
        """The statement must not be conditional on review_status: a rebuilt
        build that still carries an old verdict has to go back into review."""
        mock_conn, mock_cursor = self._make_cursor_context([("build-abc", None)])
        worker_pg._set_preview_status(mock_conn, "build-abc", preview_url="https://x.example.test", attempt=ATTEMPT)
        sql = mock_cursor.execute.call_args_list[0][0][0]
        # The statement opens with a CTE that reads the prior project id; the
        # UPDATE's own WHERE is the last one.
        where = sql.rsplit("WHERE", 1)[1]
        assert "review_status" not in where
        for column in ("reviewed_at = NULL", "reviewed_by = NULL", "review_notes = NULL"):
            assert column in sql

    def test_auto_review_queues_the_build_instead(self):
        """CAS_HARBOR_AUTO_REVIEW=true queues the build as 'ai_review_queued'."""
        mock_conn, mock_cursor = self._make_cursor_context([("build-abc", None)])

        with patch.dict(os.environ, {"CAS_HARBOR_AUTO_REVIEW": "true"}):
            landed = worker_pg._set_preview_status(
                mock_conn, "build-abc", preview_url="https://x.up.railway.app", attempt=ATTEMPT)

        assert landed is True
        params = mock_cursor.execute.call_args_list[0][0][1]
        assert params[1] == "ai_review_queued"

    def test_no_matching_row_writes_nothing_else(self):
        """No row matched (a duplicate call, or the build left the worker's
        hands): False, and no second statement that could touch the row."""
        mock_conn, mock_cursor = self._make_cursor_context([None])

        landed = worker_pg._set_preview_status(
            mock_conn, "build-abc", preview_url="https://x.up.railway.app", attempt=ATTEMPT)

        assert landed is False
        assert mock_cursor.execute.call_count == 1

    @patch.object(worker_pg, "send_review_pending")
    @patch.object(worker_pg, "send_preview_ready")
    @patch.object(worker_pg, "_cleanup_old_builds", return_value=0)
    @patch.object(worker_pg, "_set_preview_status")
    @patch.object(worker_pg, "_run_pipeline")
    def test_preview_transition_email_failure_does_not_block_build(
        self,
        mock_pipeline,
        mock_set_preview,
        mock_cleanup_builds,
        mock_preview_email,
        mock_review_email,
    ):
        """send_review_pending raises; worker must swallow the exception and
        continue processing (send_preview_ready still called, no re-raise)."""
        mock_pipeline.return_value = {"live_url": "https://x.up.railway.app"}
        mock_set_preview.return_value = True
        mock_review_email.side_effect = RuntimeError("resend down")
        mock_conn = MagicMock()

        # Must not raise even though send_review_pending blew up.
        worker_pg._process_job(self._job(), mock_conn)

        mock_review_email.assert_called_once()
        # Customer-facing preview email still fires after the failing admin email.
        mock_preview_email.assert_called_once()

    @patch.object(worker_pg, "send_review_pending")
    @patch.object(worker_pg, "send_preview_ready")
    @patch.object(worker_pg, "_cleanup_old_builds", return_value=0)
    @patch.object(worker_pg, "_set_preview_status", return_value=True)
    @patch.object(worker_pg, "_run_pipeline")
    def test_landing_at_preview_notifies_the_admin_with_the_build_details(
        self,
        mock_pipeline,
        mock_set_preview,
        mock_cleanup_builds,
        mock_preview_email,
        mock_review_email,
    ):
        mock_pipeline.return_value = {"live_url": "https://x.up.railway.app"}

        worker_pg._process_job(self._job(), MagicMock())

        mock_review_email.assert_called_once()
        # Keyword-arg shape is fixed by the call site contract.
        kwargs = mock_review_email.call_args.kwargs
        assert kwargs["build_id"] == "build-abc"
        assert kwargs["app_name"] == "test-app"
        assert kwargs["customer_email"] == "test@example.com"
        assert kwargs["tier"] == "build"
        assert kwargs["preview_url"] == "https://x.up.railway.app"
        mock_preview_email.assert_called_once()

    @patch.object(worker_pg, "_append_log")
    @patch.object(worker_pg, "send_review_pending")
    @patch.object(worker_pg, "send_preview_ready")
    @patch.object(worker_pg, "_cleanup_old_builds", return_value=0)
    @patch.object(worker_pg, "_set_preview_status", return_value=False)
    @patch.object(worker_pg, "_run_pipeline")
    def test_nothing_is_announced_when_the_preview_write_matched_no_row(
        self,
        mock_pipeline,
        mock_set_preview,
        mock_cleanup_builds,
        mock_preview_email,
        mock_review_email,
        mock_log,
    ):
        """An admin killed the build while it was deploying: the pipeline
        returns a URL, but the preview write matches no row. No "ready for
        review" log line, no login link in build_logs, no monitor
        registration, no admin email and no customer email."""
        mock_pipeline.return_value = {
            "live_url": "https://x.up.railway.app",
            "railway_project_id": "proj-1",
            "railway_service_id": "svc-1",
            "railway_environment_id": "env-1",
            "magic_link_raw_token": "ab" * 32,
        }

        with patch("monitor.registry.register_app") as register:
            worker_pg._process_job(self._job(), MagicMock())

        mock_set_preview.assert_called_once()
        mock_review_email.assert_not_called()
        mock_preview_email.assert_not_called()
        register.assert_not_called()
        mock_cleanup_builds.assert_not_called()
        logged = " | ".join(str(c.args[3]) for c in mock_log.call_args_list if len(c.args) > 3)
        assert "ready for review" not in logged
        assert "Magic link login URL" not in logged

    @patch.object(worker_pg, "sentry_sdk", MagicMock())
    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_requeue_for_retry")
    @patch.object(worker_pg, "_run_pipeline")
    def test_a_pipeline_that_lost_its_build_is_dropped_quietly(
        self, mock_pipeline, mock_requeue, mock_fail, mock_failed_email,
    ):
        """BuildNoLongerOwnedError is not a build failure: no retry, no
        terminal failure, no email, and nothing sent to Sentry."""
        mock_pipeline.side_effect = worker_pg.BuildNoLongerOwnedError("status write matched no row")

        worker_pg._process_job(self._job(), MagicMock())

        mock_requeue.assert_not_called()
        mock_fail.assert_not_called()
        mock_failed_email.assert_not_called()
        worker_pg.sentry_sdk.capture_exception.assert_not_called()


# ---------------------------------------------------------------------------
# _fail_build()
# ---------------------------------------------------------------------------

class TestFailBuild:
    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_cleanup_old_builds", return_value=0)
    def test_sets_status_and_sends_email(self, mock_cleanup_builds, mock_email):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        worker_pg._fail_build(mock_conn, "build-1", "kaboom", "user@test.example", "User", "my-app", "admin@test.example", attempt=ATTEMPT)

        # DLQ insert + status update, and nothing else: the build row's
        # Railway project is not even read here.
        assert mock_cursor.execute.call_count == 2
        # First call: DLQ insert
        dlq_sql = mock_cursor.execute.call_args_list[0][0][0]
        assert "dead_letter_builds" in dlq_sql
        # Second call: status update
        status_sql = mock_cursor.execute.call_args_list[1][0][0]
        assert "failed" in status_sql
        mock_conn.commit.assert_called_once()
        mock_email.assert_called_once_with("user@test.example", "User", "my-app", "admin@test.example", build_id="build-1")
        # Duplicate cleanup was called
        mock_cleanup_builds.assert_called_once()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_cleanup_old_builds", return_value=0)
    def test_status_flip_is_retried_alone_when_the_dead_letter_insert_fails(
            self, mock_cleanup_builds, mock_email):
        """The dead-letter insert errors (first execute raises). The status
        flip is retried on its own, lands, and the customer is told."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = [Exception("dead_letter_builds is broken"), None, None]
        mock_cursor.fetchone.return_value = (None,)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = worker_pg._fail_build(
            mock_conn, "build-1", "kaboom", "user@test.example", "User", "my-app", "admin@test.example", attempt=ATTEMPT)

        assert result is True
        retry_sql = mock_cursor.execute.call_args_list[1][0][0]
        assert retry_sql == worker_pg._MARK_FAILED_SQL
        mock_email.assert_called_once()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_cleanup_old_builds")
    def test_nothing_is_announced_when_no_write_succeeded(self, mock_cleanup_builds, mock_email):
        """Every statement errors, so the build is still in an active status.
        No failure email and no cleanup: orphan recovery will find the row."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception("DB error")
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("deploy.destroy_project") as destroy:
            result = worker_pg._fail_build(
                mock_conn, "build-1", "kaboom", "user@test.example", "User", "my-app", "admin@test.example", attempt=ATTEMPT)

        assert result is False
        mock_email.assert_not_called()
        mock_cleanup_builds.assert_not_called()
        destroy.assert_not_called()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_cleanup_old_builds", return_value=0)
    def test_a_terminal_failure_never_touches_railway(self, mock_cleanup_builds, mock_email):
        """The row records a project (whatever the cursor is asked, it answers
        with one). _fail_build used to read it and delete it; the project on a
        row is the deployment the customer was last told about."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("proj-on-the-row",)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("deploy.destroy_project") as destroy, \
             patch.dict(os.environ, {"RAILWAY_API_TOKEN": "not-a-token"}):
            result = worker_pg._fail_build(
                mock_conn, "build-1", "kaboom", "user@test.example", "User", "my-app", "admin@test.example",
                category="timeout", attempt=ATTEMPT)

        assert result is True
        dlq_params = mock_cursor.execute.call_args_list[0][0][1]
        assert dlq_params[1] == "timeout"
        mock_email.assert_called_once()
        destroy.assert_not_called()
        assert not any("railway_project_id" in str(c) for c in mock_cursor.execute.call_args_list)
        # The janitor (rows only) still runs.
        mock_cleanup_builds.assert_called_once()

    @patch.object(worker_pg, "send_build_failed")
    def test_truncates_long_error_messages(self, mock_email):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        long_error = "x" * 5000
        worker_pg._fail_build(mock_conn, "build-1", long_error, "user@test.example", "User", "my-app", "admin@test.example", attempt=ATTEMPT)

        # Error message should be truncated to 4000 chars
        dlq_args = mock_cursor.execute.call_args_list[0][0][1]
        assert len(dlq_args[2]) == 4000


# ---------------------------------------------------------------------------
# Background threads
# ---------------------------------------------------------------------------

class TestBackgroundThreads:
    @patch("worker_pg.threading.Thread")
    def test_start_monitor_thread_creates_daemon(self, mock_thread_cls):
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        worker_pg._start_monitor_thread()

        mock_thread_cls.assert_called_once()
        kwargs = mock_thread_cls.call_args[1]
        assert kwargs["daemon"] is True
        assert kwargs["name"] == "health-monitor"
        mock_thread.start.assert_called_once()

    @patch("worker_pg.threading.Thread")
    def test_start_backup_thread_creates_daemon(self, mock_thread_cls):
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        worker_pg._start_backup_thread()

        mock_thread_cls.assert_called_once()
        kwargs = mock_thread_cls.call_args[1]
        assert kwargs["daemon"] is True
        assert kwargs["name"] == "daily-backup"
        mock_thread.start.assert_called_once()


# ---------------------------------------------------------------------------
# Orphan recovery and stage resumption
# ---------------------------------------------------------------------------

def _orphan_conn(orphans, resume_count=0):
    """Mock connection for _recover_orphaned_builds.

    orphans: rows the candidate SELECT returns, as
        (build_id, app_name, status, retry_count, customer_email, customer_name)
        with started_at appended as None when it is left out.
    resume_count: what the 'recovery-resume' COUNT(*) returns.
    """
    cursor = MagicMock()
    cursor.fetchall.return_value = [tuple(row) + (None,) * (7 - len(row)) for row in orphans]
    cursor.fetchone.return_value = (resume_count,)
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def _requeue_updates(cursor):
    return [c for c in cursor.execute.call_args_list if "status = 'queued'" in str(c[0][0])]


class TestRecoverOrphanedBuilds:
    """Branching of _recover_orphaned_builds with a mocked cursor. The SQL
    itself (lease lookup, guards, dead-letter rows) runs against Postgres in
    tests/test_worker_pg_realdb.py::TestOrphanRecovery."""

    def test_recovers_stuck_builds_no_artifacts(self):
        """Builds without artifacts on disk are requeued and use a retry."""
        conn, cursor = _orphan_conn([
            ("build-1", "nonexistent-app", "building", 1, "a@example.test", "A"),
            ("build-2", "other-nonexistent", "testing", 1, "b@example.test", "B"),
        ])
        with patch.object(worker_pg, "_has_prior_timeout_failure", return_value=False), \
             patch.object(worker_pg, "_fail_build") as fail:
            count = worker_pg._recover_orphaned_builds(conn)
        assert count == 2
        fail.assert_not_called()
        updates = _requeue_updates(cursor)
        assert len(updates) == 2
        for update in updates:
            assert "retry_count = COALESCE(retry_count, 0) + 1" in update[0][0]

    def test_returns_zero_when_no_orphans(self):
        conn, cursor = _orphan_conn([])
        assert worker_pg._recover_orphaned_builds(conn) == 0

    def test_candidate_query_covers_the_three_worker_owned_statuses(self):
        conn, cursor = _orphan_conn([])
        worker_pg._recover_orphaned_builds(conn)
        sql = cursor.execute.call_args_list[0][0][0]
        assert "status IN ('building', 'testing', 'deploying')" in sql

    def test_a_requeue_that_matches_no_row_is_not_counted(self):
        """The build finished between the SELECT and the UPDATE."""
        conn, cursor = _orphan_conn([("build-1", "my-app", "building", 0, "a@example.test", "A")])
        cursor.rowcount = 0
        with patch.object(worker_pg, "_has_prior_timeout_failure", return_value=False), \
             patch.object(worker_pg, "_can_resume_build", return_value=False):
            assert worker_pg._recover_orphaned_builds(conn) == 0

    def test_no_retries_left_is_a_terminal_failure_with_the_customer_details(self):
        """retry_count is the one attempt counter. With all _MAX_RETRIES used,
        an interrupted build is failed through _fail_build (dead-letter row,
        customer email), with a message that says what happened."""
        conn, cursor = _orphan_conn([
            ("build-99", "stuck-app", "deploying", worker_pg._MAX_RETRIES, "owner@example.test", "Owner"),
        ])
        with patch.object(worker_pg, "_has_prior_timeout_failure", return_value=False), \
             patch.object(worker_pg, "_can_resume_build", return_value=False), \
             patch.object(worker_pg, "_fail_build") as fail, \
             patch.dict(os.environ, {"ADMIN_EMAIL": "admin@example.test"}):
            count = worker_pg._recover_orphaned_builds(conn)
        assert count == 0
        assert _requeue_updates(cursor) == []
        fail.assert_called_once()
        args, kwargs = fail.call_args
        assert args[1] == "build-99"
        assert "no retries left" in args[2]
        assert args[3:7] == ("owner@example.test", "Owner", "stuck-app", "admin@example.test")
        assert kwargs["retry_count"] == worker_pg._MAX_RETRIES
        assert kwargs["category"] == "infra"

    def test_one_retry_left_is_still_requeued(self):
        """Orphan recovery uses the same cap as every other requeue: a build
        with one retry left gets it, it is not failed one attempt early."""
        conn, cursor = _orphan_conn([
            ("build-98", "app", "building", worker_pg._MAX_RETRIES - 1, "o@example.test", "O"),
        ])
        with patch.object(worker_pg, "_has_prior_timeout_failure", return_value=False), \
             patch.object(worker_pg, "_can_resume_build", return_value=False), \
             patch.object(worker_pg, "_fail_build") as fail:
            assert worker_pg._recover_orphaned_builds(conn) == 1
        fail.assert_not_called()

    def test_null_retry_count_is_treated_as_zero(self):
        conn, cursor = _orphan_conn([("build-null", "my-app", "building", None, None, None)])
        with patch.object(worker_pg, "_has_prior_timeout_failure", return_value=False), \
             patch.object(worker_pg, "_can_resume_build", return_value=False), \
             patch.object(worker_pg, "_fail_build") as fail:
            assert worker_pg._recover_orphaned_builds(conn) == 1
        fail.assert_not_called()
        assert len(_requeue_updates(cursor)) == 1

    def test_recovers_with_resume_marker_when_artifacts_exist(self):
        """A build whose files are on disk is requeued with the marker that
        _resume_stage reads, and a 'recovery-resume' row is written."""
        conn, cursor = _orphan_conn([("build-1", "my-app", "testing", 1, "a@example.test", "A")])
        with patch.object(worker_pg, "_has_prior_timeout_failure", return_value=False), \
             patch.object(worker_pg, "_can_resume_build", return_value=True):
            count = worker_pg._recover_orphaned_builds(conn)

        assert count == 1
        updates = _requeue_updates(cursor)
        assert len(updates) == 1
        assert updates[0][0][1] == ("Recovered: resuming from testing stage", "build-1", None)
        markers = [c for c in cursor.execute.call_args_list if "INSERT INTO build_logs" in str(c[0][0])]
        assert len(markers) == 1 and markers[0][0][1][1] == "recovery-resume"

    def test_artifact_resume_does_not_increment_retry_count(self):
        """Artifact-based resumptions do not use a retry: no work is lost."""
        conn, cursor = _orphan_conn([("build-1", "my-app", "testing", 2, "a@example.test", "A")])
        with patch.object(worker_pg, "_has_prior_timeout_failure", return_value=False), \
             patch.object(worker_pg, "_can_resume_build", return_value=True):
            worker_pg._recover_orphaned_builds(conn)
        updates = _requeue_updates(cursor)
        assert len(updates) == 1
        assert "retry_count" not in updates[0][0][0]

    def test_a_resumable_build_with_no_retries_left_is_still_resumed(self):
        """The retry cap only applies to a requeue that uses a retry."""
        conn, cursor = _orphan_conn([
            ("build-1", "my-app", "deploying", worker_pg._MAX_RETRIES, "a@example.test", "A"),
        ])
        with patch.object(worker_pg, "_has_prior_timeout_failure", return_value=False), \
             patch.object(worker_pg, "_can_resume_build", return_value=True), \
             patch.object(worker_pg, "_fail_build") as fail:
            assert worker_pg._recover_orphaned_builds(conn) == 1
        fail.assert_not_called()

    def test_artifact_resume_caps_at_max_resumes(self):
        """A build already resumed _MAX_ARTIFACT_RESUMES times (for example one
        that kills the worker on every resume, which never moves retry_count)
        is failed permanently instead of resuming forever."""
        conn, cursor = _orphan_conn(
            [("build-1", "my-app", "testing", 1, "a@example.test", "A")],
            resume_count=worker_pg._MAX_ARTIFACT_RESUMES,
        )
        with patch.object(worker_pg, "_has_prior_timeout_failure", return_value=False), \
             patch.object(worker_pg, "_can_resume_build", return_value=True), \
             patch.object(worker_pg, "_fail_build") as fail:
            count = worker_pg._recover_orphaned_builds(conn)

        assert count == 0, "a capped build must NOT be counted as recovered"
        assert _requeue_updates(cursor) == []
        fail.assert_called_once()
        assert "infinite resume loop" in fail.call_args[0][2]

    def test_a_build_that_used_its_supervised_retry_is_failed_not_requeued(self):
        conn, cursor = _orphan_conn([("stuck-build-1", "stuck-app", "testing", 1, "a@example.test", "A")])
        with patch.object(worker_pg, "_has_prior_timeout_failure", return_value=True) as check, \
             patch.object(worker_pg, "_fail_build") as fail:
            count = worker_pg._recover_orphaned_builds(conn)
        check.assert_called_once_with(conn, "stuck-build-1")
        assert count == 0
        assert _requeue_updates(cursor) == []
        fail.assert_called_once()
        assert "prior supervisor timeout failure" in fail.call_args[0][2]

    def test_requeue_updates_recheck_the_active_status(self):
        conn, cursor = _orphan_conn([
            ("b-1", "my-app", "building", 1, "a@example.test", "A"),
            ("b-2", "my-app2", "testing", 2, "b@example.test", "B"),
        ])
        with patch.object(worker_pg, "_has_prior_timeout_failure", return_value=False), \
             patch.object(worker_pg, "_can_resume_build", side_effect=[False, True]):
            worker_pg._recover_orphaned_builds(conn)
        updates = [c[0][0] for c in cursor.execute.call_args_list if "UPDATE builds" in c[0][0]]
        assert len(updates) == 2
        for sql in updates:
            assert "status IN ('building', 'testing', 'deploying')" in sql, sql


class TestCanResumeBuild:
    """_can_resume_build against a real directory tree (builds root redirected)."""

    @pytest.fixture()
    def builds_root(self, tmp_path, monkeypatch):
        root = tmp_path / "builds"
        root.mkdir()
        monkeypatch.setattr(worker_pg, "_BUILDS_ROOT", root)
        return root

    def _make_build_dir(self, builds_root, name, files=("package.json", "server.js")):
        app_dir = builds_root / name
        app_dir.mkdir()
        for file_name in files:
            (app_dir / file_name).write_text("{}")
        return app_dir

    def test_returns_false_when_no_build_dir(self, builds_root):
        assert worker_pg._can_resume_build("my-app", "testing", build_id="12345678-aaaa") is False

    @pytest.mark.parametrize("stage", ["testing", "deploying"])
    def test_returns_true_for_a_later_stage_with_the_build_files(self, builds_root, stage):
        self._make_build_dir(builds_root, "my-app-12345678")
        assert worker_pg._can_resume_build("my-app", stage, build_id="12345678-aaaa") is True

    def test_returns_false_for_building_stage(self, builds_root):
        """Building stage cannot be resumed; the build must run again."""
        self._make_build_dir(builds_root, "my-app-12345678")
        assert worker_pg._can_resume_build("my-app", "building", build_id="12345678-aaaa") is False

    def test_returns_false_without_required_files(self, builds_root):
        """Missing package.json or server.js means no resumption."""
        self._make_build_dir(builds_root, "my-app-12345678", files=("README.md", "package.json"))
        assert worker_pg._can_resume_build("my-app", "testing", build_id="12345678-aaaa") is False

    def test_another_builds_directory_does_not_count(self, builds_root):
        self._make_build_dir(builds_root, "my-app-99999999")
        assert worker_pg._can_resume_build("my-app", "testing", build_id="12345678-aaaa") is False


class TestResumeStage:
    """_resume_stage: which stage a claimed job resumes from. The job dicts
    here have the shape _claim_next_job returns; that the claim really carries
    the marker is tested against Postgres in
    tests/test_worker_pg_realdb.py::TestStageResumeMarker."""

    BUILD_ID = "12345678-aaaa-bbbb-cccc-000000000000"
    DIR_NAME = "my-app-12345678"

    @pytest.fixture()
    def builds_root(self, tmp_path, monkeypatch):
        root = tmp_path / "builds"
        root.mkdir()
        monkeypatch.setattr(worker_pg, "_BUILDS_ROOT", root)
        return root

    def _job(self, error_message=None):
        return {"id": self.BUILD_ID, "app_name": "my-app", "error_message": error_message}

    def _conn(self, has_db_artifact=False):
        conn, cursor = _ctx_conn(fetchone=(has_db_artifact,))
        return conn

    def _files_on_disk(self, builds_root):
        build_dir = builds_root / self.DIR_NAME
        build_dir.mkdir()
        (build_dir / "package.json").write_text("{}")

    @pytest.mark.parametrize("marker, stage", [
        ("Recovered: resuming from testing stage", "testing"),
        ("Recovered: resuming from deploying stage", "deploying"),
        ("Requeued: resuming from deploying stage", "deploying"),
    ])
    def test_marker_with_files_on_disk_resumes_from_that_stage(self, builds_root, marker, stage):
        self._files_on_disk(builds_root)
        with patch.object(worker_pg, "_restore_build_artifact") as restore:
            assert worker_pg._resume_stage(self._conn(), self._job(marker), self.DIR_NAME) == stage
        restore.assert_not_called()

    def test_marker_without_files_restores_the_stored_artifact(self, builds_root):
        with patch.object(worker_pg, "_restore_build_artifact", return_value=True) as restore:
            stage = worker_pg._resume_stage(
                self._conn(), self._job("Recovered: resuming from deploying stage"), self.DIR_NAME)
        assert stage == "deploying"
        restore.assert_called_once()

    def test_marker_with_nothing_to_resume_from_is_a_full_run(self, builds_root):
        with patch.object(worker_pg, "_restore_build_artifact", return_value=False):
            stage = worker_pg._resume_stage(
                self._conn(), self._job("Recovered: resuming from testing stage"), self.DIR_NAME)
        assert stage is None

    def test_no_marker_and_no_artifact_is_a_full_run(self, builds_root):
        for message in (None, "", "Recovered: worker restarted during build", "Retry 2/6: boom"):
            assert worker_pg._resume_stage(
                self._conn(has_db_artifact=False), self._job(message), self.DIR_NAME) is None

    def test_no_marker_but_a_stored_artifact_resumes_from_testing(self, builds_root):
        """A requeued build (retry, manual requeue) keeps its stored artifact."""
        with patch.object(worker_pg, "_restore_build_artifact", return_value=True) as restore:
            stage = worker_pg._resume_stage(self._conn(has_db_artifact=True), self._job(None), self.DIR_NAME)
        assert stage == "testing"
        restore.assert_called_once()

    def test_a_stored_artifact_that_cannot_be_restored_is_a_full_run(self, builds_root):
        """Skipping the build with no files to test or deploy would only fail later."""
        with patch.object(worker_pg, "_restore_build_artifact", return_value=False):
            stage = worker_pg._resume_stage(self._conn(has_db_artifact=True), self._job(None), self.DIR_NAME)
        assert stage is None
        with patch.object(worker_pg, "_restore_build_artifact", side_effect=OSError("disk full")):
            stage = worker_pg._resume_stage(self._conn(has_db_artifact=True), self._job(None), self.DIR_NAME)
        assert stage is None

    def test_run_pipeline_skips_validate_and_build_when_resuming(self, builds_root, monkeypatch):
        """End of the chain: a claimed job that carries the marker reaches the
        deploy stage without the engine's build step being called."""
        self._files_on_disk(builds_root)
        monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
        monkeypatch.delenv("CAS_BUILD_ENGINE", raising=False)
        job = {
            "attempt": ATTEMPT,
            "id": self.BUILD_ID, "app_name": "my-app", "tier": "build",
            "spec": {"app_name": "my-app"}, "customer_email": "c@example.test",
            "customer_name": "C", "error_message": "Recovered: resuming from testing stage",
        }
        logged = []
        import compose.build as compose_build
        with patch.object(compose_build, "build_app") as build_app, \
             patch.object(worker_pg, "_append_log", side_effect=lambda c, b, s, m: logged.append(m)), \
             patch.object(worker_pg, "_set_status", return_value=True), \
             patch.object(worker_pg, "_set_preview_status", return_value=True), \
             patch.object(worker_pg, "_lint_v2_build_output", return_value={"checks": [], "errors": []}), \
             patch.object(worker_pg, "_inject_magic_link_token", return_value=False), \
             patch.object(worker_pg, "_ensure_conn", side_effect=lambda c: c), \
             patch.object(worker_pg, "send_build_progress"):
            result = worker_pg._run_pipeline(job, MagicMock())
        build_app.assert_not_called()
        assert "Resuming from testing stage" in logged
        assert not any("Validating app specification" in message for message in logged)
        assert result == {"live_url": None, "local_preview": True}


class TestBuildArtifactStore:
    """Tests for _store_build_artifact, _restore_build_artifact, _clear_build_artifact."""

    @staticmethod
    def _capturing_conn():
        captured = {}

        def fake_execute(sql, params):
            captured["sql"] = sql
            captured["artifact"] = bytes(params[0].adapted)  # psycopg2.Binary wraps the bytes

        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.execute.side_effect = fake_execute
        conn.cursor.return_value = cursor
        return conn, captured

    @staticmethod
    def _names(artifact: bytes) -> list:
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(artifact), mode="r:gz") as tar:
            return tar.getnames()

    def test_store_writes_bytea_to_db(self, tmp_path):
        app_dir = tmp_path / "my-app"
        app_dir.mkdir()
        (app_dir / "package.json").write_text('{"name":"my-app"}')
        (app_dir / "server.js").write_text("app.listen(3000);")

        conn, captured = self._capturing_conn()
        worker_pg._store_build_artifact(conn, "build-1", str(app_dir))

        assert "build_artifact" in captured["sql"]
        conn.commit.assert_called_once()
        assert sorted(self._names(captured["artifact"])) == ["my-app/package.json", "my-app/server.js"]

    def test_store_skips_missing_dir(self, tmp_path):
        conn = MagicMock()
        worker_pg._store_build_artifact(conn, "build-1", str(tmp_path / "nonexistent"))
        conn.cursor.assert_not_called()

    def test_every_entry_is_stored_exactly_once_and_node_modules_never(self, tmp_path):
        """tar.add() on a directory descends into it. The loop already visits
        every entry, so each nested file used to be stored once per ancestor
        directory (routes/deep/b.js three times), and a node_modules below
        the top level went in whole."""
        app_dir = tmp_path / "my-app"
        (app_dir / "routes" / "deep").mkdir(parents=True)
        (app_dir / "package.json").write_text("{}")
        (app_dir / "routes" / "a.js").write_text("a")
        (app_dir / "routes" / "deep" / "b.js").write_text("b")
        (app_dir / "node_modules" / "lodash").mkdir(parents=True)
        (app_dir / "node_modules" / "lodash" / "index.js").write_text("module.exports={}")
        (app_dir / "vendor" / "node_modules" / "left-pad").mkdir(parents=True)
        (app_dir / "vendor" / "node_modules" / "left-pad" / "index.js").write_text("x")
        (app_dir / "vendor" / "keep.js").write_text("k")

        conn, captured = self._capturing_conn()
        worker_pg._store_build_artifact(conn, "build-1", str(app_dir))

        names = self._names(captured["artifact"])
        assert sorted(names) == [
            "my-app/package.json", "my-app/routes", "my-app/routes/a.js",
            "my-app/routes/deep", "my-app/routes/deep/b.js",
            "my-app/vendor", "my-app/vendor/keep.js",
        ]
        assert len(names) == len(set(names)), "an entry was stored more than once"

    def test_restore_extracts_to_disk(self, tmp_path):
        import io
        import tarfile

        # Create a tar with a fake app
        app_dir = tmp_path / "src-app"
        app_dir.mkdir()
        (app_dir / "package.json").write_text('{"name":"test"}')

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(app_dir, arcname="src-app")
        artifact = buf.getvalue()

        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        # psycopg2 returns memoryview for BYTEA columns
        cursor.fetchone.return_value = (memoryview(artifact),)
        conn.cursor.return_value = cursor

        dest = tmp_path / "builds"
        dest.mkdir()
        with patch.object(worker_pg, "_BUILDS_ROOT", dest):
            result = worker_pg._restore_build_artifact(conn, "build-1", "src-app")

        assert result is True
        assert (dest / "src-app" / "package.json").read_text() == '{"name":"test"}'

    def test_restore_returns_false_when_no_artifact(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.return_value = (None,)
        conn.cursor.return_value = cursor

        result = worker_pg._restore_build_artifact(conn, "build-1", "my-app")
        assert result is False

    def test_restore_returns_false_when_no_row(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.return_value = None
        conn.cursor.return_value = cursor

        result = worker_pg._restore_build_artifact(conn, "build-1", "my-app")
        assert result is False

    def test_clear_sets_artifact_null_for_the_attempt_that_owns_the_build(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.rowcount = 1
        conn.cursor.return_value = cursor

        assert worker_pg._clear_build_artifact(conn, "build-1", ATTEMPT) is True

        sql, params = cursor.execute.call_args[0]
        assert "build_artifact = NULL" in sql
        assert "started_at IS NOT DISTINCT FROM" in sql and "status IN" in sql
        assert params == ("build-1", ATTEMPT)
        conn.commit.assert_called_once()

    def test_clear_reports_false_when_the_attempt_no_longer_owns_the_build(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.rowcount = 0
        conn.cursor.return_value = cursor

        assert worker_pg._clear_build_artifact(conn, "build-1", ATTEMPT) is False

    def test_clear_needs_an_attempt_token(self):
        with pytest.raises(ValueError):
            worker_pg._clear_build_artifact(MagicMock(), "build-1", None)


class TestTierTimeouts:
    def test_all_tiers_have_timeouts(self):
        assert "build" in worker_pg._TIER_TIMEOUTS
        assert "grow" in worker_pg._TIER_TIMEOUTS
        assert "scale" in worker_pg._TIER_TIMEOUTS

    def test_scale_has_longest_timeout(self):
        t = worker_pg._TIER_TIMEOUTS
        assert t["scale"] > t["grow"] > t["build"]

    def test_timeouts_are_positive_minutes(self):
        for tier, minutes in worker_pg._TIER_TIMEOUTS.items():
            assert minutes > 0, f"Timeout for {tier} must be positive"
            assert isinstance(minutes, int), f"Timeout for {tier} must be an integer"


# ---------------------------------------------------------------------------
# C2: _shutdown_event is a threading.Event (not a plain boolean)
# ---------------------------------------------------------------------------

class TestShutdownEvent:
    def test_shutdown_event_is_threading_event(self):
        assert isinstance(worker_pg._shutdown_event, threading.Event)

    def test_shutdown_event_initially_unset(self):
        """Verify the event is not set at import time (worker not shutting down)."""
        # Reset in case a previous test set it
        worker_pg._shutdown_event.clear()
        assert not worker_pg._shutdown_event.is_set()

    def test_sigterm_handler_sets_event(self):
        """SIGTERM handler must call _shutdown_event.set()."""
        worker_pg._shutdown_event.clear()
        # Call the handler directly (don't send real SIGTERM)
        with patch.object(worker_pg.sentry_sdk, "flush"):
            worker_pg._handle_sigterm(signal.SIGTERM, None)
        assert worker_pg._shutdown_event.is_set()
        # Clean up
        worker_pg._shutdown_event.clear()

    def test_shutdown_event_wait_is_interruptible(self):
        """Event.wait(timeout) returns immediately when set, unlike time.sleep."""
        worker_pg._shutdown_event.clear()
        import time
        start = time.monotonic()
        # Set the event from another thread after 50ms
        def _setter():
            time.sleep(0.05)
            worker_pg._shutdown_event.set()
        t = threading.Thread(target=_setter)
        t.start()
        # wait with long timeout; should return in ~50ms, not 10s
        worker_pg._shutdown_event.wait(10)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"wait() should have returned quickly, took {elapsed:.2f}s"
        t.join()
        worker_pg._shutdown_event.clear()


# ---------------------------------------------------------------------------
# C1: No SIGALRM in _process_job (thread-safe watchdog only)
# ---------------------------------------------------------------------------

class TestNoSigalrmInProcessJob:
    def test_process_job_source_has_no_sigalrm(self):
        """_process_job must not use signal.alarm or signal.SIGALRM."""
        import inspect
        source = inspect.getsource(worker_pg._process_job)
        assert "signal.alarm" not in source, "_process_job still uses signal.alarm"
        assert "signal.SIGALRM" not in source, "_process_job still uses signal.SIGALRM"
        assert "signal.signal(signal.SIGALRM" not in source, "_process_job still sets SIGALRM handler"

    def test_ctypes_watchdog_removed(self):
        """WATCHDOG1 Layer 2: the ctypes async-exception watchdog MUST be
        removed from _process_job. It could not interrupt threads blocked in
        C-level syscalls, which was the exact failure mode it was meant to
        cover. The outer supervisor (ThreadPoolExecutor + fut.result(timeout))
        is now the sole timeout mechanism."""
        import inspect
        process_job_src = inspect.getsource(worker_pg._process_job)
        module_src = inspect.getsource(worker_pg)
        # The old fast path lived inside _process_job. None of these may survive.
        assert "PyThreadState_SetAsyncExc" not in process_job_src, (
            "ctypes async-exception watchdog must be removed from _process_job"
        )
        assert "_watchdog(" not in process_job_src, (
            "inner _watchdog() closure must be removed from _process_job"
        )
        # And nothing at module level may import ctypes for this purpose.
        assert "import ctypes" not in module_src, (
            "worker_pg must not import ctypes anymore (watchdog removed)"
        )
        assert "os.kill(os.getpid(), signal.SIGALRM)" not in process_job_src, (
            "watchdog should not send SIGALRM"
        )

    def test_module_level_has_no_sigalrm_handler(self):
        """Module level should only register SIGTERM, not SIGALRM."""
        import inspect
        # Get only module-level code (not inside functions)
        source = inspect.getsource(worker_pg)
        # SIGALRM may appear in comments or _classify_error string matching; that's fine.
        # But signal.signal(signal.SIGALRM, ...) at module level should not exist.
        lines = source.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
                continue
            if "signal.signal(signal.SIGALRM" in stripped:
                pytest.fail(f"Module-level SIGALRM handler found at line {i+1}: {stripped}")


# ---------------------------------------------------------------------------
# M2: Build directory isolation per build_id
# ---------------------------------------------------------------------------

class TestBuildDirIsolation:
    def test_build_dir_includes_build_id(self):
        """_run_pipeline and _process_job must construct dirs with build_id prefix."""
        import inspect
        source = inspect.getsource(worker_pg._run_pipeline)
        # The new format: f"{app_name}-{build_id[:8]}"
        assert "build_id[:8]" in source, "_run_pipeline should use build_id[:8] in dir name"

    def test_resume_check_sees_only_the_directory_of_its_own_build(self, tmp_path, monkeypatch):
        """Two builds of the same app: the one whose per-build directory holds a
        finished build stage is resumable, the other is not. A build id other
        than the first eight characters of its own does not find the directory."""
        monkeypatch.setattr(worker_pg, "_BUILDS_ROOT", tmp_path)
        id_a = "aaaa1111-0000-0000-0000-000000000000"
        id_b = "bbbb2222-0000-0000-0000-000000000000"
        build_dir = tmp_path / "my-app-aaaa1111"
        build_dir.mkdir()
        (build_dir / "package.json").write_text("{}")
        (build_dir / "server.js").write_text("")

        assert worker_pg._can_resume_build("my-app", "testing", build_id=id_a) is True
        assert worker_pg._can_resume_build("my-app", "testing", build_id=id_b) is False

    def test_can_resume_build_uses_build_id(self):
        """_can_resume_build with build_id uses per-build dir, not shared app dir."""
        # With a build_id, it should look for app_name-build_id[:8]
        result = worker_pg._can_resume_build(
            "nonexistent-app", "testing", build_id="abcd1234-0000-0000-0000-000000000000"
        )
        assert result is False  # dir doesn't exist

    def test_can_resume_build_legacy_fallback(self):
        """_can_resume_build without build_id falls back to legacy app_name dir."""
        result = worker_pg._can_resume_build("nonexistent-app", "testing", build_id="")
        assert result is False  # dir doesn't exist, but it tried the legacy path


# ---------------------------------------------------------------------------
# M1: Cleanup advisory lock
# ---------------------------------------------------------------------------

class TestCleanupAdvisoryLock:
    def test_skips_cleanup_when_lock_not_acquired(self):
        """If another thread holds the advisory lock, cleanup deletes nothing."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # pg_try_advisory_lock returns False (another thread holds it)
        cursor.fetchone.return_value = (False,)

        result = worker_pg._cleanup_old_builds(conn, "build-123", "my-app")
        assert result == 0
        # Should not have attempted any DELETE
        delete_calls = [c for c in cursor.execute.call_args_list
                        if "DELETE" in str(c)]
        assert len(delete_calls) == 0

    def test_acquires_and_releases_lock(self):
        """Cleanup must release advisory lock even when no stale builds found."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # First call: pg_try_advisory_lock returns True
        # Second call: SELECT query returns no rows
        cursor.fetchone.side_effect = [(True,)]
        cursor.fetchall.return_value = []

        result = worker_pg._cleanup_old_builds(conn, "build-123", "my-app")
        assert result == 0
        # Verify lock was acquired and released
        execute_calls = [str(c) for c in cursor.execute.call_args_list]
        assert any("pg_try_advisory_lock" in c for c in execute_calls)
        assert any("pg_advisory_unlock" in c for c in execute_calls)


# ---------------------------------------------------------------------------
# M3: Startup spec temp file cleanup
# ---------------------------------------------------------------------------

class TestStartupSpecCleanup:
    def test_startup_cleanup_removes_spec_files(self, tmp_path):
        """_startup_disk_cleanup should clean /tmp/spec_*.json files."""
        import inspect
        source = inspect.getsource(worker_pg._startup_disk_cleanup)
        assert "spec_*.json" in source, "startup cleanup should handle leaked spec temp files"


class TestMagicLinkToken:
    def test_generate_returns_raw_and_hash(self):
        """_generate_magic_link_token returns dict with raw_token and token_hash."""
        result = worker_pg._generate_magic_link_token()
        assert "raw_token" in result
        assert "token_hash" in result
        assert len(result["raw_token"]) == 64
        assert len(result["token_hash"]) == 64

    def test_hash_is_sha256_of_raw(self):
        """token_hash is the SHA-256 hex digest of the raw token hex string."""
        import hashlib
        result = worker_pg._generate_magic_link_token()
        expected = hashlib.sha256(result["raw_token"].encode()).hexdigest()
        assert result["token_hash"] == expected

    def test_tokens_are_unique(self):
        """Each call generates unique tokens."""
        t1 = worker_pg._generate_magic_link_token()
        t2 = worker_pg._generate_magic_link_token()
        assert t1["raw_token"] != t2["raw_token"]

    def test_inject_appends_to_seed_sql(self, tmp_path):
        """_inject_magic_link_token appends INSERT to seed.sql."""
        db_dir = tmp_path / "db"
        db_dir.mkdir()
        seed = db_dir / "seed.sql"
        seed.write_text("INSERT INTO users (id, role) VALUES (gen_random_uuid(), 'admin');\n")
        worker_pg._inject_magic_link_token(str(tmp_path), "abc123hash")
        content = seed.read_text()
        assert "magic_link_tokens" in content
        assert "abc123hash" in content
        assert "24 hours" in content

    def test_inject_two_tokens(self, tmp_path):
        """_inject_magic_link_token with customer_token_hash injects two separate tokens."""
        db_dir = tmp_path / "db"
        db_dir.mkdir()
        seed = db_dir / "seed.sql"
        seed.write_text("INSERT INTO users (id, role) VALUES (gen_random_uuid(), 'admin');\n")
        worker_pg._inject_magic_link_token(str(tmp_path), "verify_hash", customer_token_hash="customer_hash")
        content = seed.read_text()
        assert "verify_hash" in content
        assert "customer_hash" in content
        # Two separate INSERT statements for magic_link_tokens
        assert content.count("INSERT INTO magic_link_tokens") == 2
        assert "deploy verification" in content
        assert "customer first login" in content

    def test_inject_single_token_no_customer_hash(self, tmp_path):
        """_inject_magic_link_token without customer_token_hash injects only one token."""
        db_dir = tmp_path / "db"
        db_dir.mkdir()
        seed = db_dir / "seed.sql"
        seed.write_text("INSERT INTO users (id, role) VALUES (gen_random_uuid(), 'admin');\n")
        worker_pg._inject_magic_link_token(str(tmp_path), "verify_hash_only")
        content = seed.read_text()
        assert "verify_hash_only" in content
        assert content.count("INSERT INTO magic_link_tokens") == 1

    def test_inject_skips_if_no_seed(self, tmp_path):
        """_inject_magic_link_token returns silently when seed.sql is missing."""
        worker_pg._inject_magic_link_token(str(tmp_path), "abc123hash")
        # No error raised

    def test_build_magic_link_url(self):
        """_build_magic_link_url constructs correct URL."""
        url = worker_pg._build_magic_link_url("https://myapp.up.railway.app", "tokenxyz")
        assert url == "https://myapp.up.railway.app/magic?token=tokenxyz"

    def test_build_magic_link_url_strips_trailing_slash(self):
        """_build_magic_link_url strips trailing slash from live_url."""
        url = worker_pg._build_magic_link_url("https://myapp.up.railway.app/", "tok")
        assert url == "https://myapp.up.railway.app/magic?token=tok"

    def test_hash_matches_nodejs_crypto(self):
        """Token hash must match what Node.js crypto.createHash('sha256').update(token).digest('hex') produces.

        This is critical: the worker generates the hash (Python) and the generated
        app validates it (Node.js). If the hash methods diverge, every magic link
        breaks with 'invalid or expired'.
        """
        import hashlib
        import subprocess
        result = worker_pg._generate_magic_link_token()
        raw = result["raw_token"]
        python_hash = result["token_hash"]

        # Verify Python hash uses string encoding (not bytes.fromhex)
        assert python_hash == hashlib.sha256(raw.encode()).hexdigest()
        # Verify it does NOT match bytes.fromhex (the old broken method)
        bytes_hash = hashlib.sha256(bytes.fromhex(raw)).hexdigest()
        assert python_hash != bytes_hash, \
            "Hash should NOT use bytes.fromhex; must hash the hex string directly"

        # Cross-check with Node.js if available
        try:
            node_result = subprocess.run(
                ["node", "-e",
                 f"console.log(require('crypto').createHash('sha256').update('{raw}').digest('hex'))"],
                capture_output=True, text=True, timeout=5,
            )
            if node_result.returncode == 0:
                node_hash = node_result.stdout.strip()
                assert python_hash == node_hash, \
                    f"Python hash {python_hash} != Node.js hash {node_hash}"
        except FileNotFoundError:
            pass  # Node.js not installed, skip cross-check


class TestSweepOrphanRailwayProjects:
    """Regression guard for the prod-deletion incident (2026-04-09 x2).

    The staging worker shares the same Railway API token but has an empty DB,
    so it would classify the prod project as an orphan and delete it on startup.

    Fix: ENABLE_ORPHAN_SWEEP=true explicit opt-in (prod worker only).
    Anything without that var set is safe by default, no sweep ever runs.
    """

    def _run_sweep(self, enable_sweep, mock_gql_return=None, active_pids=None,
                   destroy_effect=None, monitored=None):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = [(pid,) for pid in (active_pids or [])]
        conn.cursor.return_value = cur
        self.cursor = cur

        gql_return = mock_gql_return or {"projects": {"edges": []}}

        env = {"RAILWAY_PROJECT_ID": "cas-project-id"}
        if enable_sweep is not None:
            env["ENABLE_ORPHAN_SWEEP"] = enable_sweep
        else:
            env.pop("ENABLE_ORPHAN_SWEEP", None)

        with patch.dict("os.environ", env, clear=False), \
             patch("deploy.gql", return_value=gql_return) as mock_gql, \
             patch("deploy.destroy_project", side_effect=destroy_effect) as mock_destroy, \
             patch("monitor.registry.list_apps", return_value=list(monitored or [])), \
             patch("monitor.registry.remove_app") as mock_remove:
            import os as _os
            if enable_sweep is None:
                _os.environ.pop("ENABLE_ORPHAN_SWEEP", None)
            result = worker_pg._sweep_orphan_railway_projects(conn)
            self.removed_from_monitor = [c.args[0] for c in mock_remove.call_args_list]
            return result, mock_gql, mock_destroy

    @staticmethod
    def _node(project_id, name, age_hours=48):
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        created = (_dt.now(_tz.utc) - _td(hours=age_hours)).isoformat().replace("+00:00", "Z")
        return {"node": {"id": project_id, "name": name, "createdAt": created}}

    def test_skips_when_flag_absent(self):
        """Default (no ENABLE_ORPHAN_SWEEP) must never sweep, safe by default."""
        result, mock_gql, mock_destroy = self._run_sweep(None)
        assert result == 0
        mock_gql.assert_not_called()
        mock_destroy.assert_not_called()

    def test_skips_when_flag_false(self):
        """ENABLE_ORPHAN_SWEEP=false must not sweep, staging/dev safety."""
        result, mock_gql, mock_destroy = self._run_sweep("false")
        assert result == 0
        mock_gql.assert_not_called()
        mock_destroy.assert_not_called()

    def test_skips_when_flag_empty(self):
        """ENABLE_ORPHAN_SWEEP='' must not sweep."""
        result, mock_gql, mock_destroy = self._run_sweep("")
        assert result == 0
        mock_gql.assert_not_called()
        mock_destroy.assert_not_called()

    def test_runs_when_flag_true(self):
        """ENABLE_ORPHAN_SWEEP=true runs sweep and deletes orphan projects."""
        gql_return = {
            "projects": {
                "edges": [
                    self._node("cas-project-id", "craftandship-web"),
                    self._node("orphan-id-123", "old-customer-app"),
                ]
            }
        }
        result, mock_gql, mock_destroy = self._run_sweep(
            "true", mock_gql_return=gql_return, active_pids=[],
            monitored=[{"id": "mon-1", "app_name": "old-customer-app",
                        "railway_project_id": "orphan-id-123"}],
        )
        mock_gql.assert_called_once()
        mock_destroy.assert_called_once_with("orphan-id-123", token=None)
        assert result == 1
        assert self.removed_from_monitor == ["mon-1"]

    def test_a_project_recorded_on_any_build_row_is_not_an_orphan(self):
        """Whatever the status of the row: a recorded project is a deployment
        that was announced. The lookup has no status filter."""
        gql_return = {"projects": {"edges": [self._node("on-a-failed-row", "once-announced")]}}
        result, _gql, mock_destroy = self._run_sweep(
            "true", mock_gql_return=gql_return, active_pids=["on-a-failed-row"])
        mock_destroy.assert_not_called()
        assert result == 0
        lookup_sql = self.cursor.execute.call_args[0][0]
        assert "railway_project_id IS NOT NULL" in lookup_sql and "status" not in lookup_sql

    def test_a_young_project_may_belong_to_an_attempt_in_flight(self):
        """An attempt records its project on the row only at preview. Until
        then the project is on no row, and it must not be swept from under a
        build that another worker is still running."""
        gql_return = {"projects": {"edges": [
            self._node("fresh", "being-built", age_hours=1),
            {"node": {"id": "no-date", "name": "age-unknown"}},
        ]}}
        result, _gql, mock_destroy = self._run_sweep("true", mock_gql_return=gql_return, active_pids=[])
        mock_destroy.assert_not_called()
        assert result == 0

    def test_a_delete_that_failed_is_neither_counted_nor_deregistered(self):
        gql_return = {"projects": {"edges": [self._node("stubborn", "old-app")]}}
        result, _gql, mock_destroy = self._run_sweep(
            "true", mock_gql_return=gql_return, active_pids=[],
            destroy_effect=RuntimeError("destroy_project FAILED: Railway said no"),
            monitored=[{"id": "mon-9", "app_name": "old-app", "railway_project_id": "stubborn"}],
        )
        mock_destroy.assert_called_once()
        assert result == 0
        assert self.removed_from_monitor == []

    def test_protects_cas_project_even_when_enabled(self):
        """The CAS platform project (RAILWAY_PROJECT_ID) must never be deleted."""
        gql_return = {
            "projects": {
                "edges": [
                    self._node("cas-project-id", "craftandship-web"),
                    self._node("orphan-id-999", "old-app"),
                ]
            }
        }
        result, mock_gql, mock_destroy = self._run_sweep(
            "true", mock_gql_return=gql_return, active_pids=[]
        )
        # Only the orphan should be deleted, not the CAS project
        mock_destroy.assert_called_once_with("orphan-id-999", token=None)
        assert result == 1


# ---------------------------------------------------------------------------
# WATCHDOG1 Layer 1: outer supervisor for builds stuck in C-level syscalls
# ---------------------------------------------------------------------------

class TestWatchdogSupervisor:
    """Regression tests for the WATCHDOG1 Layer 1 supervisor.

    Contract: when _run_pipeline blocks past its tier deadline + grace period
    (e.g. thread stuck in subprocess.wait, socket recv, SSL_read, calls that
    ctypes PyThreadState_SetAsyncExc cannot interrupt), the supervisor must:
      (a) force-fail the DB row via a fresh connection,
      (b) return cleanly so the worker can claim the next job (capacity),
      (c) and on worker restart, orphan recovery must not re-queue the build
          (which would just hit the same C-level block again).
    """

    def _make_job(self, retry_count=0):
        return {
            "attempt": ATTEMPT,
            "id": "build-stuck-1",
            "app_name": "stuck-app",
            "tier": "build",
            "spec": {"app_name": "stuck-app"},
            "customer_id": "cust-1",
            "customer_email": "test@example.com",
            "customer_name": "Test User",
            "retry_count": retry_count,
        }

    def _fake_executor_that_times_out(self):
        """Return a fake ThreadPoolExecutor class whose future never finishes:
        waiting on it gives up at once and done() stays False, simulating a
        build thread stuck in a C-level syscall that cannot be interrupted."""
        import concurrent.futures
        fake_future = MagicMock()
        fake_future.exception.side_effect = concurrent.futures.TimeoutError()
        fake_future.result.side_effect = concurrent.futures.TimeoutError()
        fake_future.done.return_value = False
        fake_executor = MagicMock()
        fake_executor.submit.return_value = fake_future
        fake_executor_cls = MagicMock(return_value=fake_executor)
        return fake_executor_cls, fake_executor, fake_future

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_supervisor_force_fail", return_value=(True, "failed", None))
    def test_stuck_pipeline_is_force_failed_by_supervisor(
        self, mock_force_fail, mock_email
    ):
        """When _run_pipeline blocks past the deadline, _process_job hands the
        build to the supervisor together with the customer details. The
        supervisor's terminal branch goes through _fail_build, which sends the
        "build failed" email, so _process_job must not send a second one."""
        fake_exec_cls, _, _ = self._fake_executor_that_times_out()

        with patch.object(worker_pg, "ThreadPoolExecutor", fake_exec_cls), \
             patch.dict(os.environ, {"ADMIN_EMAIL": "admin@example.test"}):
            mock_conn = MagicMock()
            worker_pg._process_job(self._make_job(), mock_conn)

        # Supervisor fired with correct build metadata
        mock_force_fail.assert_called_once()
        args, kwargs = mock_force_fail.call_args
        assert args[0] == "build-stuck-1"
        assert args[1] == "build"  # tier
        assert args[2] == worker_pg._TIER_TIMEOUTS["build"]  # tier timeout (min)
        cancel_key = kwargs.pop("cancel_key")
        assert cancel_key.startswith("build-stuck-1#"), "the attempt's own cancel key"
        # The attempt's state goes along, so that the supervisor's preview
        # write is ordered against the thread's own clean-up; the thread has
        # been told that nobody waits for it any more.
        attempt_state = kwargs.pop("attempt_state")
        assert attempt_state["abandoned"] is True
        assert kwargs == {
            "customer_email": "test@example.com",
            "customer_name": "Test User",
            "admin_email": "admin@example.test",
            "attempt": ATTEMPT,
            "attempt_railway": None,
        }

        mock_email.assert_not_called()

    @patch.object(worker_pg, "sentry_sdk", MagicMock())
    def test_terminal_supervisor_verdict_emails_the_customer_once(self):
        """Second hang, with the real _supervisor_force_fail and _fail_build on
        a mocked connection: one dead-letter row with category 'timeout', one
        "build failed" email to the customer the caller named, and no Railway
        call at all (a terminal failure never deletes a project; the janitor
        that runs afterwards deletes rows only)."""
        conn, cursor = self._fresh_conn_cursor(("testing", 1, True, "stuck-app", None, None))
        with patch.object(worker_pg, "_get_conn", return_value=conn), \
             patch.object(worker_pg, "_supervisor_reality_probe", return_value=None), \
             patch.object(worker_pg, "send_build_failed") as email, \
             patch.object(worker_pg, "_cleanup_old_builds") as cleanup, \
             patch("deploy.destroy_project") as destroy:
            committed, verdict, _probe = worker_pg._supervisor_force_fail(
                "build-second-hang", "build", 45,
                customer_email="owner@example.test", customer_name="Owner",
                admin_email="admin@example.test", attempt=ATTEMPT)
        assert (committed, verdict) == (True, "failed")
        email.assert_called_once_with(
            "owner@example.test", "Owner", "stuck-app", "admin@example.test",
            build_id="build-second-hang",
        )
        dlq = [c for c in cursor.execute.call_args_list if "dead_letter_builds" in str(c[0][0])]
        assert len(dlq) == 1 and dlq[0][0][1][1] == "timeout"
        destroy.assert_not_called()
        cleanup.assert_called_once()

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_supervisor_force_fail", return_value=(True, "failed", None))
    def test_stuck_build_releases_capacity_within_deadline(
        self, mock_force_fail, mock_email
    ):
        """_process_job MUST return cleanly on supervisor timeout, a stuck
        build cannot block the worker loop forever. With a mocked executor
        whose future never finishes, the whole call should
        return in well under 5 seconds (proving the capacity-release path)."""
        import time as time_module

        fake_exec_cls, fake_executor, _ = self._fake_executor_that_times_out()

        with patch.object(worker_pg, "ThreadPoolExecutor", fake_exec_cls):
            mock_conn = MagicMock()
            start = time_module.monotonic()
            worker_pg._process_job(self._make_job(), mock_conn)
            elapsed = time_module.monotonic() - start

        # Capacity-release contract: supervisor timeout path exits fast
        assert elapsed < 5.0, (
            f"_process_job took {elapsed:.2f}s on supervisor timeout; "
            "capacity not released"
        )

        # Proof the timeout branch ran (not the happy path)
        mock_force_fail.assert_called_once()

        # Executor was shut down without waiting on the zombie sub-thread
        fake_executor.shutdown.assert_called_once()
        shutdown_kwargs = fake_executor.shutdown.call_args.kwargs
        assert shutdown_kwargs.get("wait") is False, (
            "Executor shutdown must use wait=False to avoid blocking on a "
            "zombie build thread"
        )

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_supervisor_force_fail", return_value=(True, "failed", None))
    def test_cost_not_written_to_db_on_supervisor_timeout(
        self, mock_force_fail, mock_email
    ):
        """When supervisor_timed_out=True, the cost-write block is skipped entirely.

        The zombie build thread still holds the original conn; touching it from
        the calling thread would corrupt psycopg2 state. No total_api_cost_usd
        UPDATE should fire.
        """
        fake_exec_cls, _, _ = self._fake_executor_that_times_out()

        with patch.object(worker_pg, "ThreadPoolExecutor", fake_exec_cls):
            mock_conn = MagicMock()
            worker_pg._process_job(self._make_job(), mock_conn)

        cursor = mock_conn.cursor.return_value.__enter__.return_value
        cost_updates = [
            c for c in cursor.execute.call_args_list
            if "total_api_cost_usd" in str(c)
        ]
        assert len(cost_updates) == 0, (
            "total_api_cost_usd must NOT be written when supervisor times out "
            "the original conn is owned by the zombie build thread"
        )

    # The orphan-recovery branches (supervised retry already used, no retries
    # left, resume cap) are covered by TestRecoverOrphanedBuilds above and,
    # against Postgres, by tests/test_worker_pg_realdb.py::TestOrphanRecovery.

    # -----------------------------------------------------------------
    # WATCHDOG1 Layer 2: section-07 §2.1 (12 tests) + §2.2 (3 tests)
    # -----------------------------------------------------------------

    def _fresh_conn_cursor(self, fetchone_row):
        """Build a MagicMock psycopg2 conn whose `with conn.cursor() as cur:`
        context manager yields a cursor where cur.fetchone() returns the
        supplied row tuple. Accepts the legacy 3-tuple
        (status, retry_count, supervisor_retry_used) and auto-pads to the
        6-tuple the new SELECT reads
        (status, retry_count, supervisor_retry_used, app_name, started_at,
        completed_at). All execute() calls are captured on
        cursor.execute.call_args_list.

        The supervisor acts only on the attempt it was called for, so a row
        that names no started_at is given ATTEMPT, the token these tests pass:
        the row is on the supervised attempt unless a test says otherwise."""
        if len(fetchone_row) == 3:
            fetchone_row = fetchone_row + (None, None, None)
        if fetchone_row[4] is None:
            fetchone_row = fetchone_row[:4] + (ATTEMPT,) + fetchone_row[5:]
        cursor = MagicMock()
        cursor.fetchone.return_value = fetchone_row
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn, cursor

    # -------- §2.1.1 supervisor first-hang branch --------------------

    @patch.object(worker_pg, "sentry_sdk", MagicMock())
    def test_supervisor_fires_on_hang_first_time(self):
        """First hang: status flips to 'queued', supervisor_retry_used=TRUE,
        retry_count increments, and NO dead_letter_builds row is written."""
        conn, cursor = self._fresh_conn_cursor(("building", 0, False))
        with patch.object(worker_pg, "_get_conn", return_value=conn), patch.object(
            worker_pg, "_supervisor_reality_probe", return_value=None
        ):
            committed, verdict, _probe = worker_pg._supervisor_force_fail(
                "build-first-hang", "build", 45, attempt=ATTEMPT)

        assert committed is True
        assert verdict == "retry"
        # UPDATE to status='queued' with supervisor_retry_used = TRUE
        queued_updates = [
            c for c in cursor.execute.call_args_list
            if "status = 'queued'" in str(c[0][0])
            and "supervisor_retry_used = TRUE" in str(c[0][0])
        ]
        assert len(queued_updates) == 1, (
            "first hang must requeue with supervisor_retry_used=TRUE"
        )
        # NO dead_letter_builds insert on first hang
        dlq_inserts = [
            c for c in cursor.execute.call_args_list
            if "dead_letter_builds" in str(c[0][0])
        ]
        assert len(dlq_inserts) == 0, (
            "first hang must NOT write a DLQ row"
        )

    @patch.object(worker_pg, "sentry_sdk", MagicMock())
    def test_supervisor_one_free_retry(self):
        """Second hang (supervisor_retry_used IS TRUE): status flips to
        'failed' and a dead_letter_builds row with error_category='timeout'
        is written. This is the terminal branch."""
        conn, cursor = self._fresh_conn_cursor(("testing", 1, True))
        with patch.object(worker_pg, "_get_conn", return_value=conn), patch.object(
            worker_pg, "_supervisor_reality_probe", return_value=None
        ):
            committed, verdict, _probe = worker_pg._supervisor_force_fail(
                "build-second-hang", "build", 45, attempt=ATTEMPT)

        assert committed is True
        assert verdict == "failed"
        # UPDATE to status='failed'
        failed_updates = [
            c for c in cursor.execute.call_args_list
            if "status = 'failed'" in str(c[0][0])
        ]
        assert len(failed_updates) == 1
        # DLQ row with error_category 'timeout' (a parameter of _fail_build's insert)
        dlq_inserts = [
            c for c in cursor.execute.call_args_list
            if "dead_letter_builds" in str(c[0][0])
            and c[0][1][1] == "timeout"
        ]
        assert len(dlq_inserts) == 1, (
            "second hang must write a DLQ row with error_category='timeout'"
        )

    @patch.object(worker_pg, "sentry_sdk", MagicMock())
    def test_supervisor_does_not_act_on_a_queued_build(self):
        """A build that is back in the queue is waiting for its next attempt.
        A supervisor left over from the previous attempt must neither spend
        its free retry nor fail it."""
        conn, cursor = self._fresh_conn_cursor(("queued", 0, False))
        with patch.object(worker_pg, "_get_conn", return_value=conn), patch.object(
            worker_pg, "_supervisor_reality_probe", return_value=None
        ) as probe:
            committed, verdict, _probe = worker_pg._supervisor_force_fail(
                "build-requeued", "build", 45, attempt=ATTEMPT)
        assert (committed, verdict) == (False, "failed")
        probe.assert_not_called()
        writes = [c for c in cursor.execute.call_args_list
                  if str(c[0][0]).lstrip().upper().startswith(("UPDATE", "INSERT"))]
        assert writes == []

    @patch.object(worker_pg, "sentry_sdk", MagicMock())
    def test_supervisor_holds_no_row_lock_and_no_open_transaction_while_probing(self):
        """The Railway probe is a network call of up to 20 seconds. The row is
        read without FOR UPDATE and the read transaction is committed before
        the probe runs; the outcome is a guarded UPDATE afterwards."""
        conn, cursor = self._fresh_conn_cursor(("building", 0, False))
        seen_at_probe = {}

        def probe(**_kwargs):
            seen_at_probe["commits"] = conn.commit.call_count
            seen_at_probe["sql"] = [str(c[0][0]) for c in cursor.execute.call_args_list]
            return None

        with patch.object(worker_pg, "_get_conn", return_value=conn), patch.object(
            worker_pg, "_supervisor_reality_probe", side_effect=probe
        ):
            committed, verdict, _probe = worker_pg._supervisor_force_fail(
                "build-first-hang", "build", 45, attempt=ATTEMPT)
        assert (committed, verdict) == (True, "retry")
        assert seen_at_probe["commits"] >= 1
        assert not any("FOR UPDATE" in sql for sql in seen_at_probe["sql"])
        assert not any(sql.lstrip().upper().startswith("UPDATE") for sql in seen_at_probe["sql"])
        requeue = next(c[0][0] for c in cursor.execute.call_args_list
                       if "status = 'queued'" in str(c[0][0]))
        assert "status IN ('building', 'testing', 'deploying')" in requeue

    @patch.object(worker_pg, "sentry_sdk", MagicMock())
    def test_supervisor_reports_uncommitted_when_its_write_matches_no_row(self):
        """The build left the worker-owned statuses while Railway was probed
        (an admin kill, for example): the guarded UPDATE matches nothing and
        the supervisor reports that it did not decide the outcome."""
        conn, cursor = self._fresh_conn_cursor(("building", 0, False))
        cursor.rowcount = 0
        with patch.object(worker_pg, "_get_conn", return_value=conn), patch.object(
            worker_pg, "_supervisor_reality_probe", return_value=None
        ):
            committed, verdict, _probe = worker_pg._supervisor_force_fail(
                "build-killed-meanwhile", "build", 45, attempt=ATTEMPT)
        assert (committed, verdict) == (False, "failed")

    # -------- §2.1.2 orphan recovery ---------------------------------

    def test_orphan_recovery_retries_first_hang(self):
        """A build whose supervisor_retry_used=FALSE is still eligible for
        orphan recovery, normal re-queue path."""
        conn, cursor = _orphan_conn([("first-hang-1", "first-app", "testing", 0, "a@example.test", "A")])

        with patch.object(
            worker_pg, "_has_prior_timeout_failure", return_value=False
        ) as mock_check, patch.object(
            worker_pg, "_can_resume_build", return_value=False
        ):
            count = worker_pg._recover_orphaned_builds(conn)

        mock_check.assert_called_once_with(conn, "first-hang-1")
        # The build WAS eligible for recovery (count >= 1)
        assert count >= 1

    # -------- §2.1.3 helper reads new column -------------------------

    def test_has_prior_timeout_failure_uses_new_column(self):
        """_has_prior_timeout_failure must query the `supervisor_retry_used`
        column on `builds`, NOT the dead_letter_builds table."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (True,)
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        result = worker_pg._has_prior_timeout_failure(conn, "build-xyz")
        assert result is True

        # Inspect the executed SQL: must reference supervisor_retry_used +
        # builds table, and must NOT reference dead_letter_builds.
        executed_sql = " ".join(
            str(c[0][0]) for c in cursor.execute.call_args_list
        )
        assert "supervisor_retry_used" in executed_sql
        assert "FROM builds" in executed_sql or "from builds" in executed_sql.lower()
        assert "dead_letter_builds" not in executed_sql, (
            "helper must read the new column, not the DLQ"
        )

    # -------- §2.1.4 cancellation fires BEFORE DB write --------------

    def test_supervisor_fires_cancel_event_before_db_write(self):
        """cli_cancellation.fire must be called, with the attempt's cancel key,
        BEFORE the first database statement, so any cooperative consumer
        inside the stuck thread sees the cancel signal as early as possible."""
        call_order: list[str] = []
        fired: list[str] = []

        def track_fire(key):
            fired.append(key)
            call_order.append("fire")

        def track_execute(*args, **kwargs):
            call_order.append("execute")

        cursor = MagicMock()
        cursor.fetchone.return_value = ("building", 0, False, "ordering-app", None, None)
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.execute.side_effect = track_execute
        conn = MagicMock()
        conn.cursor.return_value = cursor

        with patch.object(
            worker_pg.cli_cancellation, "fire", side_effect=track_fire
        ), patch.object(
            worker_pg, "_get_conn", return_value=conn
        ), patch.object(
            worker_pg, "sentry_sdk", MagicMock()
        ), patch.object(
            worker_pg, "_supervisor_reality_probe", return_value=None
        ):
            worker_pg._supervisor_force_fail(
                "build-ordering", "build", 45, attempt=ATTEMPT, cancel_key="build-ordering#k1")

        assert fired == ["build-ordering#k1"], "the attempt's own event must fire, by its key"
        assert "fire" in call_order, "cancel event must fire"
        assert "execute" in call_order, "DB writes must happen"
        # Strict ordering: fire() must come before the first execute()
        assert call_order.index("fire") < call_order.index("execute"), (
            "cli_cancellation.fire MUST run before any DB write"
        )

    # -------- §2.1.5 fix-loop budget classification ------------------

    def test_fix_loop_budget_triggers_code_error(self):
        """FixLoopBudgetExceeded.__str__ must not contain 'timeout' so
        _classify_error routes it as 'code_error' (not 'timeout'). Code
        errors are retryable; timeouts are DLQ-terminal in Layer 2's
        second-hang branch, so mis-classification would short-circuit
        retries."""
        exc = worker_pg.FixLoopBudgetExceeded("fix", 1800.0, 1800.0)
        msg = str(exc)
        assert "timeout" not in msg.lower(), (
            "FixLoopBudgetExceeded message must not contain 'timeout'"
        )
        category = worker_pg._classify_error(msg)
        assert category == "code_error", (
            f"expected 'code_error', got {category!r}"
        )

    def test_fix_loop_budget_per_loop_name(self):
        """Each fix-loop name has its own budget. Spec §3.2:
        fix=30min, qa_fix/design_fix=20min. deploy_fix is
        15min so the
        supervised retry has headroom under the supervisor deadline."""
        budgets = worker_pg._FIX_LOOP_BUDGETS_S
        assert budgets["fix"] == 30 * 60
        assert budgets["qa_fix"] == 20 * 60
        assert budgets["design_fix"] == 20 * 60
        # deploy_fix must be <=15 min (tightened from 20) but >=10 min
        # so a realistic two-iteration session can still complete.
        assert 10 * 60 <= budgets["deploy_fix"] <= 15 * 60
        # loop_name attribute survives on the exception itself
        exc = worker_pg.FixLoopBudgetExceeded("qa_fix", 1200.0, 1200.0)
        assert exc.loop_name == "qa_fix"

    def test_qa_fix_budget_exceeded_is_non_fatal(self):
        """FixLoopBudgetExceeded from the qa_fix loop must NOT propagate out of
        the QA section as a build-fatal error. QA is non-blocking; a qa_fix
        timeout should log a warning and let the build continue to deploy.

        Regression guard for an MW v6 build where qa_fix ran 1625s and
        killed the build even though QA failures are explicitly non-blocking.
        """
        # Verify the exception has elapsed_s / budget_s attributes (used in log)
        exc = worker_pg.FixLoopBudgetExceeded("qa_fix", 1625.0, 1200.0)
        assert exc.elapsed_s == 1625.0
        assert exc.budget_s == 1200.0
        assert exc.loop_name == "qa_fix"
        # The str representation is customer-facing; must not expose "qa_fix"
        # internal name (it's caught locally now, so this is informational only)
        assert "1625" in str(exc)
        assert "1200" in str(exc)

    # -------- §2.1.6 cancellation lifecycle in _process_job ----------

    @patch.object(worker_pg, "send_build_failed")
    @patch.object(worker_pg, "_supervisor_force_fail", return_value=(True, "failed", None))
    def test_process_job_registers_and_clears_cancellation(
        self, mock_force_fail, mock_email
    ):
        """_process_job registers the attempt's cancel event before it submits
        the pipeline, hands the same key to the pipeline (in the job) and to
        the supervisor, and does NOT drop the event when it returns on the
        timeout path: the stuck thread is the one that still has to see it.
        The event goes when the pipeline thread ends."""
        fake_exec_cls, fake_executor, fake_future = self._fake_executor_that_times_out()
        job = self._make_job()

        with patch.object(
            worker_pg, "ThreadPoolExecutor", fake_exec_cls
        ), patch.object(
            worker_pg.cli_cancellation, "register"
        ) as mock_reg, patch.object(
            worker_pg.cli_cancellation, "clear"
        ) as mock_clear, patch.object(
            worker_pg, "_after_supervisor_decision"
        ):
            worker_pg._process_job(job, MagicMock())

            key = job["cancel_key"]
            assert key.startswith("build-stuck-1#")
            mock_reg.assert_called_once_with(key)
            assert mock_force_fail.call_args.kwargs["cancel_key"] == key
            # The job handed to the pipeline is the one that carries the key.
            assert fake_executor.submit.call_args[0][1] is job
            mock_clear.assert_not_called()
            # The pipeline thread ends: its done-callback drops the event.
            thread_ended = fake_future.add_done_callback.call_args[0][0]
            thread_ended(fake_future)
            mock_clear.assert_called_once_with(key)

    def test_each_attempt_gets_its_own_cancel_event(self):
        """Two attempts of the same build must not share an event: the
        supervisor may requeue a build while the old attempt's thread is
        still running, and the next attempt must neither inherit a fired
        event nor replace (and so un-fire) the old thread's."""
        first = worker_pg._new_cancel_key("build-1")
        second = worker_pg._new_cancel_key("build-1")
        assert first != second and first.startswith("build-1#") and second.startswith("build-1#")
        worker_pg.cli_cancellation.register(first)
        worker_pg.cli_cancellation.register(second)
        try:
            worker_pg.cli_cancellation.fire(first)
            assert worker_pg.cli_cancellation.get(first).is_set()
            assert not worker_pg.cli_cancellation.get(second).is_set()
        finally:
            worker_pg.cli_cancellation.clear(first)
            worker_pg.cli_cancellation.clear(second)

    def test_process_job_clears_cancellation_on_exception(self):
        """When the pipeline ends by raising, its done-callback drops the
        attempt's event like on any other end of the thread."""
        fake_future = MagicMock()
        fake_future.result.side_effect = RuntimeError("boom")
        fake_executor = MagicMock()
        fake_executor.submit.return_value = fake_future
        fake_executor_cls = MagicMock(return_value=fake_executor)
        job = self._make_job()

        with patch.object(
            worker_pg, "ThreadPoolExecutor", fake_executor_cls
        ), patch.object(
            worker_pg.cli_cancellation, "register"
        ) as mock_reg, patch.object(
            worker_pg.cli_cancellation, "clear"
        ) as mock_clear, patch.object(
            worker_pg, "_fail_build"
        ), patch.object(
            worker_pg, "_requeue_for_retry", return_value=True
        ), patch.object(
            worker_pg, "sentry_sdk"
        ), patch.object(
            worker_pg, "_cleanup_build_node_modules"
        ):
            mock_conn = MagicMock()
            worker_pg._process_job(job, mock_conn)
            fake_future.add_done_callback.call_args[0][0](fake_future)

        mock_reg.assert_called_once_with(job["cancel_key"])
        mock_clear.assert_called_once_with(job["cancel_key"])

    # -------- §2.1.7 end-to-end supervisor hang recovery -------------

    def test_end_to_end_hang_recovered(self, monkeypatch):
        """With CAS_ALLOW_TEST_HOOKS=1 + ANTHROPIC_SIMULATE_HANG_SECONDS=N,
        api_client.call_api hangs until the supervisor fires the cancel
        event. The hang is cooperative: the supervisor's fire() must wake
        it and BuildCancelled must propagate out of api_client."""
        import cancellation as cli_cancel_mod

        monkeypatch.setenv("CAS_ALLOW_TEST_HOOKS", "1")
        monkeypatch.setenv("ANTHROPIC_SIMULATE_HANG_SECONDS", "120")

        build_id = "build-e2e-hang"
        cancel_event = cli_cancel_mod.register(build_id)
        try:
            # Fire immediately so the hang loop wakes on its first 1s tick.
            cli_cancel_mod.fire(build_id)
            assert cancel_event.is_set()
            # The hang loop polls cancel_event.wait(1.0); once fired it
            # raises BuildCancelled on the next iteration (<1s).
            # Just verify get() returns the same event object.
            assert cli_cancel_mod.get(build_id) is cancel_event
        finally:
            cli_cancel_mod.clear(build_id)

    # -------- §2.2 observability / customer copy ---------------------

    @patch.object(worker_pg, "sentry_sdk", MagicMock())
    def test_first_hang_emits_customer_copy(self):
        """First-hang branch must append the EXACT §5 customer copy line to
        build_logs: 'We hit an unexpected pause and are automatically
        retrying this build.'"""
        conn, cursor = self._fresh_conn_cursor(("building", 0, False))
        with patch.object(worker_pg, "_get_conn", return_value=conn), patch.object(
            worker_pg, "_supervisor_reality_probe", return_value=None
        ):
            worker_pg._supervisor_force_fail("build-copy-1", "build", 45, attempt=ATTEMPT)

        log_inserts = [
            c for c in cursor.execute.call_args_list
            if "INSERT INTO build_logs" in str(c[0][0])
        ]
        assert len(log_inserts) >= 1, "first hang must insert a build_logs row"
        # Params tuple: (build_id, stage, message)
        joined_messages = " ".join(str(c[0][1]) for c in log_inserts)
        assert (
            "We hit an unexpected pause and are automatically retrying "
            "this build." in joined_messages
        ), "first-hang customer copy missing or modified"

    @patch.object(worker_pg, "sentry_sdk", MagicMock())
    def test_terminal_hang_emits_customer_copy(self):
        """Terminal-hang branch must append the EXACT §5 customer copy line
        to build_logs: 'We were unable to complete this build after
        automatic retries. Please reach out to support.'"""
        conn, cursor = self._fresh_conn_cursor(("testing", 1, True))
        with patch.object(worker_pg, "_get_conn", return_value=conn), patch.object(
            worker_pg, "_supervisor_reality_probe", return_value=None
        ):
            worker_pg._supervisor_force_fail("build-copy-2", "build", 45, attempt=ATTEMPT)

        log_inserts = [
            c for c in cursor.execute.call_args_list
            if "INSERT INTO build_logs" in str(c[0][0])
        ]
        assert len(log_inserts) >= 1, "terminal hang must insert a build_logs row"
        joined_messages = " ".join(str(c[0][1]) for c in log_inserts)
        assert (
            "We were unable to complete this build after automatic "
            "retries. Please reach out to support." in joined_messages
        ), "terminal-hang customer copy missing or modified"

    def test_fix_loop_budget_emits_customer_copy(self):
        """Every fix loop must emit the EXACT §5 customer copy line BEFORE
        raising FixLoopBudgetExceeded. We verify this statically via source
        inspection, the 4 budget guards all live in _run_pipeline."""
        import inspect
        source = inspect.getsource(worker_pg._run_pipeline)
        marker = (
            "One of our fix steps took longer than expected. "
            "Retrying with a fresh attempt."
        )
        # 4 fix loops: fix, qa_fix, design_fix, deploy_fix
        assert source.count(marker) >= 4, (
            f"expected >=4 occurrences of fix-loop customer copy in "
            f"_run_pipeline, found {source.count(marker)}"
        )
        # And the FixLoopBudgetExceeded raise is right after each copy line
        assert source.count("raise FixLoopBudgetExceeded") >= 4, (
            "each fix loop must raise FixLoopBudgetExceeded after emitting "
            "the customer copy line"
        )

    # -----------------------------------------------------------------
    # §2.3 supervisor zombie reality probe (audit/7day-failed-builds §1)
    # -----------------------------------------------------------------
    # These tests cover _supervisor_reality_probe and the preview-verdict
    # branch in _supervisor_force_fail added to recover from the
    # ZOMBIE_THREAD_STATUS_OVERWRITE1 pattern: build thread gets
    # C-blocked past deadline but the (now-zombie) thread completed the
    # Railway deploy. Before writing status='failed', we probe Railway
    # for a live deployment matching app_name in the build's time window
    # and route to preview instead.

    def _make_probe_window(self):
        """Return (started_at, completed_at, created_in_window) as
        timezone-aware UTC datetimes useful for the probe."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        started = now - timedelta(minutes=30)
        completed = now
        # Project created during the build is inside the window
        created_in_window = now - timedelta(minutes=15)
        return started, completed, created_in_window

    def _fake_deploy(
        self,
        projects=None,
        domain_info=None,
        token="fake-token",
    ):
        """Build a MagicMock that stands in for cli/deploy.py, used by
        the lazy `import deploy` inside _supervisor_reality_probe."""
        import deploy as real_deploy
        fake = MagicMock()
        fake.health_response_ok = real_deploy.health_response_ok   # the real definition of healthy
        fake.get_token.return_value = token
        fake.list_projects.return_value = projects or []
        fake.get_service_domain.return_value = domain_info
        return fake

    def test_reality_probe_returns_live_url_on_200(self):
        """Happy path: Railway has a project whose name matches, createdAt
        is inside the window, service domain resolves, and /health returns
        200. Probe returns {live_url, project_id, service_id}."""
        started, completed, created = self._make_probe_window()
        created_iso = created.isoformat().replace("+00:00", "Z")
        projects = [{"id": "proj-xyz", "name": "zombie-app", "createdAt": created_iso}]
        domain_info = {"domain": "zombie-app.up.railway.app", "service_id": "svc-1"}
        fake_deploy = self._fake_deploy(projects=projects, domain_info=domain_info)

        fake_resp = health_answer(200, b'{"status":"ok"}')

        with patch.dict(sys.modules, {"deploy": fake_deploy}), patched_probe_get("monitor.ping", return_value=fake_resp
        ):
            result = worker_pg._supervisor_reality_probe(
                app_name="zombie-app",
                started_at=started,
                completed_at=completed,
            )

        assert result is not None
        assert result["live_url"] == "https://zombie-app.up.railway.app"
        assert result["project_id"] == "proj-xyz"
        assert result["service_id"] == "svc-1"

    def test_reality_probe_filters_projects_outside_time_window(self):
        """A project with matching name but createdAt outside the
        (started-5min, completed+30min) window must not be probed."""
        from datetime import timedelta
        started, completed, _ = self._make_probe_window()
        # Created 10 days ago: way outside the window
        too_old = (started - timedelta(days=10)).isoformat().replace("+00:00", "Z")
        projects = [{"id": "proj-old", "name": "zombie-app", "createdAt": too_old}]
        fake_deploy = self._fake_deploy(projects=projects)

        with patch.dict(sys.modules, {"deploy": fake_deploy}), patched_probe_get("monitor.ping"
        ) as mock_urlopen:
            result = worker_pg._supervisor_reality_probe(
                app_name="zombie-app",
                started_at=started,
                completed_at=completed,
            )

        assert result is None
        # Out-of-window project must NOT be probed
        fake_deploy.get_service_domain.assert_not_called()
        mock_urlopen.assert_not_called()

    def test_reality_probe_name_mismatch_returns_none(self):
        """No project in Railway matches app_name → probe returns None
        without attempting any HTTP calls."""
        started, completed, created = self._make_probe_window()
        created_iso = created.isoformat().replace("+00:00", "Z")
        projects = [
            {"id": "proj-a", "name": "some-other-app", "createdAt": created_iso},
            {"id": "proj-b", "name": "another-app", "createdAt": created_iso},
        ]
        fake_deploy = self._fake_deploy(projects=projects)

        with patch.dict(sys.modules, {"deploy": fake_deploy}), patched_probe_get("monitor.ping"
        ) as mock_urlopen:
            result = worker_pg._supervisor_reality_probe(
                app_name="zombie-app",
                started_at=started,
                completed_at=completed,
            )

        assert result is None
        fake_deploy.get_service_domain.assert_not_called()
        mock_urlopen.assert_not_called()

    def test_reality_probe_health_503_returns_none(self):
        """Project matches + domain resolves, but /health returns 503
        (app down). Probe must fall through to None, not route to preview."""
        started, completed, created = self._make_probe_window()
        created_iso = created.isoformat().replace("+00:00", "Z")
        projects = [{"id": "proj-xyz", "name": "zombie-app", "createdAt": created_iso}]
        domain_info = {"domain": "zombie-app.up.railway.app", "service_id": "svc-1"}
        fake_deploy = self._fake_deploy(projects=projects, domain_info=domain_info)

        http_err = health_answer(503, "Service Unavailable")

        with patch.dict(sys.modules, {"deploy": fake_deploy}), patched_probe_get("monitor.ping", return_value=http_err
        ):
            result = worker_pg._supervisor_reality_probe(
                app_name="zombie-app",
                started_at=started,
                completed_at=completed,
            )

        assert result is None

    def test_reality_probe_timeout_fails_safe_to_none(self):
        """The guarded session gives up at its deadline (a requests Timeout)
        and the probe returns None without raising. Fail-safe contract: the
        supervisor cannot afford to hang."""
        started, completed, created = self._make_probe_window()
        created_iso = created.isoformat().replace("+00:00", "Z")
        projects = [{"id": "proj-xyz", "name": "zombie-app", "createdAt": created_iso}]
        domain_info = {"domain": "zombie-app.up.railway.app", "service_id": "svc-1"}
        fake_deploy = self._fake_deploy(projects=projects, domain_info=domain_info)

        with patch.dict(sys.modules, {"deploy": fake_deploy}), patched_probe_get("monitor.ping", side_effect=requests.exceptions.Timeout("probe timeout")
        ):
            result = worker_pg._supervisor_reality_probe(
                app_name="zombie-app",
                started_at=started,
                completed_at=completed,
            )

        assert result is None

    def test_reality_probe_hard_deadline_enforced(self):
        """If the probe loop exceeds hard_timeout_s, return None without
        making further external calls. Simulated by monkey-patching
        time.monotonic so the deadline is exceeded immediately after the
        list_projects call."""
        started, completed, created = self._make_probe_window()
        created_iso = created.isoformat().replace("+00:00", "Z")
        projects = [{"id": "proj-xyz", "name": "zombie-app", "createdAt": created_iso}]
        fake_deploy = self._fake_deploy(projects=projects)

        # time.monotonic advances past the deadline after the first 2 calls
        # (probe reads it once at the start to compute deadline, then checks
        # before each step). Return sequence: 0.0 (start) → 9999.0 (deadline
        # check) → ...
        call_counter = {"n": 0}

        def fake_monotonic():
            call_counter["n"] += 1
            if call_counter["n"] <= 1:
                return 0.0
            return 9999.0  # Way past any hard_timeout_s

        with patch.dict(sys.modules, {"deploy": fake_deploy}), patch(
            "queue.worker_pg.time.monotonic"
            if False else "worker_pg.time.monotonic",
            side_effect=fake_monotonic,
        ), patched_probe_get("monitor.ping") as mock_urlopen:
            result = worker_pg._supervisor_reality_probe(
                app_name="zombie-app",
                started_at=started,
                completed_at=completed,
                hard_timeout_s=20,
            )

        assert result is None
        # No HTTP probe should have fired once the deadline blew
        mock_urlopen.assert_not_called()

    def test_supervisor_force_fail_preview_verdict_writes_preview_status(self):
        """When the reality probe returns a live URL, _supervisor_force_fail
        must write status='preview', review_status='pending', preview_url,
        live_url, and return (True, 'preview'), NOT status='failed'."""
        conn, cursor = self._fresh_conn_cursor(
            ("building", 1, True)  # supervisor_retry_used already TRUE
        )
        probe_result = {
            "live_url": "https://zombie-app.up.railway.app",
            "project_id": "proj-xyz",
            "service_id": "svc-1",
        }

        with patch.object(worker_pg, "_get_conn", return_value=conn), patch.object(
            worker_pg, "_supervisor_reality_probe", return_value=probe_result
        ), patch.object(worker_pg, "sentry_sdk", MagicMock()):
            committed, verdict, _probe = worker_pg._supervisor_force_fail(
                "build-zombie-1", "build", 45, attempt=ATTEMPT)

        assert committed is True
        assert verdict == "preview"

        executed_sql = " ".join(
            str(c[0][0]) for c in cursor.execute.call_args_list
        )
        assert "status = 'preview'" in executed_sql, (
            "preview verdict must UPDATE status to 'preview'"
        )
        assert "review_status" in executed_sql, (
            "preview verdict must set review_status"
        )
        flat_params_for_review = " ".join(str(c[0][1]) for c in cursor.execute.call_args_list)
        assert "'pending'" in executed_sql or "pending" in flat_params_for_review, (
            "review_status must be 'pending' on preview verdict"
        )
        # Must NOT also write status='failed' or a DLQ row
        assert "status = 'failed'" not in executed_sql, (
            "preview verdict must NOT write failed status"
        )
        assert "dead_letter_builds" not in executed_sql, (
            "preview verdict must NOT insert a DLQ row"
        )
        # The probed URL must end up in the UPDATE params as preview_url...
        flat_params = " ".join(str(c[0][1]) for c in cursor.execute.call_args_list)
        assert "zombie-app.up.railway.app" in flat_params
        # ...and live_url must stay unset: the web app sets it on approval.
        assert "live_url" not in executed_sql

    def test_supervisor_force_fail_failed_verdict_writes_failed_status(self):
        """When the reality probe returns None (no zombie recovery), the
        terminal-hang branch must run: status='failed' + DLQ row, and
        the function returns (True, 'failed')."""
        conn, cursor = self._fresh_conn_cursor(
            ("testing", 1, True)  # supervisor_retry_used TRUE → terminal
        )
        with patch.object(worker_pg, "_get_conn", return_value=conn), patch.object(
            worker_pg, "_supervisor_reality_probe", return_value=None
        ), patch.object(worker_pg, "sentry_sdk", MagicMock()):
            committed, verdict, _probe = worker_pg._supervisor_force_fail(
                "build-dead-1", "build", 45, attempt=ATTEMPT)

        assert committed is True
        assert verdict == "failed"

        executed_sql = " ".join(
            str(c[0][0]) for c in cursor.execute.call_args_list
        )
        assert "status = 'failed'" in executed_sql
        assert "dead_letter_builds" in executed_sql
        assert "status = 'preview'" not in executed_sql

    # -----------------------------------------------------------------
    # §2.3 expanded coverage (audit revision pass; "failures nearly
    # impossible"): started_at=None, multi-candidate fallback, empty
    # project list, retry-path-no-email, Sentry on zombie hit, ordering
    # invariant with probe, first-hang-with-zombie routing to preview,
    # probe_result pass-through.
    # -----------------------------------------------------------------

    def test_reality_probe_started_at_none_still_probes(self):
        """If ``started_at`` is None (e.g. orphan recovery before the
        build was ever started), the probe must still run against the
        (now - ∞, completed + 30min) window, missing a live zombie is
        worse than probing too broadly."""
        from datetime import datetime, timezone
        _started, completed, created = self._make_probe_window()
        created_iso = created.isoformat().replace("+00:00", "Z")
        projects = [{"id": "proj-xyz", "name": "zombie-app", "createdAt": created_iso}]
        domain_info = {"domain": "zombie-app.up.railway.app", "service_id": "svc-1"}
        fake_deploy = self._fake_deploy(projects=projects, domain_info=domain_info)

        fake_resp = health_answer(200, b'{"status":"ok"}')

        with patch.dict(sys.modules, {"deploy": fake_deploy}), patched_probe_get("monitor.ping", return_value=fake_resp
        ):
            result = worker_pg._supervisor_reality_probe(
                app_name="zombie-app",
                started_at=None,
                completed_at=completed,
            )

        assert result is not None
        assert result["live_url"] == "https://zombie-app.up.railway.app"

    def test_reality_probe_multi_candidate_fallback(self):
        """Two matching projects in the window. First /health returns
        503, second returns 200, probe must return the SECOND candidate,
        not bail on the first failure."""
        from datetime import timedelta
        started, completed, created = self._make_probe_window()
        # Fresher candidate first (sorted desc); put the 503 as freshest,
        # 200 as older-but-still-in-window.
        freshest_iso = created.isoformat().replace("+00:00", "Z")
        older = created - timedelta(minutes=5)
        older_iso = older.isoformat().replace("+00:00", "Z")
        projects = [
            {"id": "proj-dead", "name": "zombie-app", "createdAt": freshest_iso},
            {"id": "proj-live", "name": "zombie-app", "createdAt": older_iso},
        ]

        def domain_for(project_id, token, deadline=None):
            if project_id == "proj-dead":
                return {"domain": "dead.up.railway.app", "service_id": "svc-dead"}
            return {"domain": "live.up.railway.app", "service_id": "svc-live"}

        fake_deploy = self._fake_deploy(projects=projects)
        fake_deploy.get_service_domain.side_effect = domain_for

        http_503 = health_answer(503, "Service Unavailable")
        fake_ok = health_answer(200, b'{"status":"ok"}')

        def urlopen_side_effect(url, *args, **kwargs):
            if "dead.up.railway.app" in url:
                return http_503
            return fake_ok

        with patch.dict(sys.modules, {"deploy": fake_deploy}), patched_probe_get("monitor.ping", side_effect=urlopen_side_effect
        ):
            result = worker_pg._supervisor_reality_probe(
                app_name="zombie-app",
                started_at=started,
                completed_at=completed,
            )

        assert result is not None, (
            "probe must fall through first-candidate 503 to second-candidate 200"
        )
        assert result["project_id"] == "proj-live"
        assert result["live_url"] == "https://live.up.railway.app"

    def test_reality_probe_empty_project_list(self):
        """Railway returns no projects at all → probe returns None
        without attempting domain resolution or HTTP probes."""
        started, completed, _ = self._make_probe_window()
        fake_deploy = self._fake_deploy(projects=[])

        with patch.dict(sys.modules, {"deploy": fake_deploy}), patched_probe_get("monitor.ping"
        ) as mock_urlopen:
            result = worker_pg._supervisor_reality_probe(
                app_name="zombie-app",
                started_at=started,
                completed_at=completed,
            )

        assert result is None
        fake_deploy.get_service_domain.assert_not_called()
        mock_urlopen.assert_not_called()

    def test_reality_probe_empty_app_name_returns_none(self):
        """Empty/None app_name short-circuits before any Railway call.
        Prevents probing 'any project with an empty name' in pathological
        cases where app_name was never written to the row."""
        started, completed, _ = self._make_probe_window()
        fake_deploy = self._fake_deploy(projects=[{"id": "p", "name": "", "createdAt": "2026-04-14T00:00:00Z"}])

        with patch.dict(sys.modules, {"deploy": fake_deploy}):
            result = worker_pg._supervisor_reality_probe(
                app_name="",
                started_at=started,
                completed_at=completed,
            )

        assert result is None
        fake_deploy.list_projects.assert_not_called()

    def test_reality_probe_list_projects_raises_fails_safe(self):
        """If deploy.list_projects raises (network, auth, 5xx from
        Railway GraphQL), the probe MUST return None, it cannot let the
        supervisor's error propagate into a hang."""
        started, completed, _ = self._make_probe_window()
        fake_deploy = self._fake_deploy()
        fake_deploy.list_projects.side_effect = RuntimeError("Railway GraphQL 502")

        with patch.dict(sys.modules, {"deploy": fake_deploy}):
            result = worker_pg._supervisor_reality_probe(
                app_name="zombie-app",
                started_at=started,
                completed_at=completed,
            )

        assert result is None

    def test_supervisor_first_hang_with_zombie_routes_to_preview(self):
        """Zombie recovery must run on FIRST hang too, not just on
        terminal hangs. A build can hang on its very first attempt and
        still have a live deploy; we should not issue a free retry that
        will just re-deploy the same app."""
        conn, cursor = self._fresh_conn_cursor(
            ("building", 0, False)  # first hang: supervisor_retry_used=FALSE
        )
        probe_result = {
            "live_url": "https://first-hang-zombie.up.railway.app",
            "project_id": "proj-fh",
            "service_id": "svc-fh",
        }
        with patch.object(worker_pg, "_get_conn", return_value=conn), patch.object(
            worker_pg, "_supervisor_reality_probe", return_value=probe_result
        ), patch.object(worker_pg, "sentry_sdk", MagicMock()):
            committed, verdict, returned_probe = worker_pg._supervisor_force_fail(
                "build-first-hang-zombie", "build", 45, attempt=ATTEMPT)

        assert committed is True
        assert verdict == "preview", (
            "first-hang-with-live-deploy must route to preview, not retry"
        )
        # probe_result is returned through the 3-tuple unchanged
        assert returned_probe is probe_result
        executed_sql = " ".join(
            str(c[0][0]) for c in cursor.execute.call_args_list
        )
        assert "status = 'preview'" in executed_sql
        assert "status = 'queued'" not in executed_sql, (
            "first-hang-with-zombie must NOT requeue"
        )
        assert "supervisor_retry_used = TRUE" not in executed_sql, (
            "first-hang-with-zombie must NOT burn the free retry"
        )

    def test_supervisor_excludes_projects_recorded_on_a_build_row(self):
        """app_name is not unique, and the probe matches Railway projects by
        name. A project that a build row records (another build's, or an
        earlier, announced deployment of this very build) must be handed to
        the probe as excluded, so the stuck attempt can never adopt it."""
        conn, cursor = self._fresh_conn_cursor(("building", 0, False))
        cursor.fetchall.return_value = [("proj-of-another-build",), (None,), ("",)]
        with patch.object(worker_pg, "_get_conn", return_value=conn), patch.object(
            worker_pg, "_supervisor_reality_probe", return_value=None
        ) as probe, patch.object(worker_pg, "sentry_sdk", MagicMock()):
            worker_pg._supervisor_force_fail("build-ownership-1", "build", 45, attempt=ATTEMPT)

        assert probe.call_args.kwargs["exclude_project_ids"] == {"proj-of-another-build"}
        lookups = [c[0] for c in cursor.execute.call_args_list if "railway_project_id FROM builds" in str(c[0][0])]
        assert len(lookups) == 1
        sql, params = lookups[0]
        assert "app_name = %s" in sql and "railway_project_id IS NOT NULL" in sql
        assert "id <>" not in sql, "this build's own recorded project is excluded too"
        assert len(params) == 1

    def test_supervisor_excludes_fresh_projects_of_other_attempts_in_this_process(self):
        """A project an attempt has just created is on no build row until the
        build reaches preview. Another attempt's must not be adopted; the
        supervised attempt's own must stay a candidate."""
        conn, cursor = self._fresh_conn_cursor(("building", 0, False))
        cursor.fetchall.return_value = []
        other_job, own_job = {"id": "other"}, {"id": "own"}
        worker_pg._note_attempt_project(other_job, {"railway_project_id": "proj-other-attempt"})
        worker_pg._note_attempt_project(own_job, {"railway_project_id": "proj-own-attempt"})
        with patch.object(worker_pg, "_get_conn", return_value=conn), patch.object(
            worker_pg, "_supervisor_reality_probe", return_value=None
        ) as probe, patch.object(worker_pg, "sentry_sdk", MagicMock()):
            worker_pg._supervisor_force_fail(
                "own", "build", 45, attempt=ATTEMPT,
                attempt_railway=worker_pg._attempt_state(own_job)["railway"])
        assert probe.call_args.kwargs["exclude_project_ids"] == {"proj-other-attempt"}

    def test_reality_probe_forwards_exclusions_to_the_shared_probe(self):
        import watchdog_probe
        with patch.object(watchdog_probe, "probe_for_zombie_deploy", return_value=None) as shared:
            worker_pg._supervisor_reality_probe(
                app_name="zombie-app", started_at=None, exclude_project_ids={"proj-x"},
            )
        assert shared.call_args.kwargs["exclude_project_ids"] == {"proj-x"}

    def test_supervisor_preview_verdict_fires_sentry_capture(self):
        """When the reality probe detects a zombie deploy, the supervisor
        MUST emit a Sentry warning so the on-call engineer has visibility
       , even though the build was 'recovered'. Silent auto-recovery
        without an alert would hide a real infra problem
        (C-blocked thread) that needs investigating."""
        conn, _cursor = self._fresh_conn_cursor(("testing", 1, True))
        probe_result = {
            "live_url": "https://zombie-visible.up.railway.app",
            "project_id": "proj-v",
            "service_id": "svc-v",
        }
        fake_sentry = MagicMock()
        with patch.object(worker_pg, "_get_conn", return_value=conn), patch.object(
            worker_pg, "_supervisor_reality_probe", return_value=probe_result
        ), patch.object(worker_pg, "sentry_sdk", fake_sentry):
            worker_pg._supervisor_force_fail("build-sentry-1", "build", 45, attempt=ATTEMPT)

        assert fake_sentry.capture_message.called, (
            "zombie-recovery MUST emit sentry_sdk.capture_message for visibility"
        )
        # The message should identify the build and the live url for fast triage
        call_args = fake_sentry.capture_message.call_args
        message = str(call_args[0][0]) if call_args and call_args[0] else ""
        assert "build-sentry-1" in message
        assert "zombie-visible.up.railway.app" in message

    def test_supervisor_cancel_fires_before_probe(self):
        """Ordering invariant: cli_cancellation.fire(build_id) MUST run
        BEFORE _supervisor_reality_probe is called, so any cooperative-
        cancel-aware code path inside the zombie thread can unblock while
        the probe is still running. The probe itself takes up to 20s;
        firing cancel first means worst-case cooperative unblock starts
        immediately, not after the probe finishes."""
        call_order = []

        def track_fire(*args, **kwargs):
            call_order.append("fire")

        def track_probe(*args, **kwargs):
            call_order.append("probe")
            return None

        conn, _cursor = self._fresh_conn_cursor(("building", 0, False))
        with patch.object(
            worker_pg.cli_cancellation, "fire", side_effect=track_fire
        ), patch.object(
            worker_pg, "_get_conn", return_value=conn
        ), patch.object(
            worker_pg, "sentry_sdk", MagicMock()
        ), patch.object(
            worker_pg, "_supervisor_reality_probe", side_effect=track_probe
        ):
            worker_pg._supervisor_force_fail(
                "build-order-probe", "build", 45, attempt=ATTEMPT, cancel_key="build-order-probe#k1")

        assert "fire" in call_order
        assert "probe" in call_order
        assert call_order.index("fire") < call_order.index("probe"), (
            "cli_cancellation.fire MUST run before the reality probe"
        )

    def test_process_job_retry_verdict_skips_emails(self):
        """On the first-hang free-retry branch (verdict='retry'), the
        supervisor MUST NOT send either send_build_failed OR
        send_review_pending, the build is about to be retried
        automatically and emailing the customer 'failed' or 'preview
        ready' would be wrong."""
        fake_exec_cls, _, _ = self._fake_executor_that_times_out()

        with patch.object(
            worker_pg, "ThreadPoolExecutor", fake_exec_cls
        ), patch.object(
            worker_pg, "_supervisor_force_fail",
            return_value=(True, "retry", None),
        ), patch.object(
            worker_pg, "send_build_failed"
        ) as mock_failed, patch.object(
            worker_pg, "send_review_pending"
        ) as mock_review:
            worker_pg._process_job(self._make_job(), MagicMock())

        mock_failed.assert_not_called()
        mock_review.assert_not_called()

    def test_process_job_preview_verdict_sends_review_pending(self):
        """When the supervisor recovers a zombie deploy (verdict='preview'),
        _process_job MUST send send_review_pending with the probed
        live_url from probe_result, NOT send_build_failed, and NOT an
        empty URL."""
        fake_exec_cls, _, _ = self._fake_executor_that_times_out()
        probe_result = {
            "live_url": "https://probed-url.up.railway.app",
            "project_id": "proj-p",
            "service_id": "svc-p",
        }
        follow_up_conn = MagicMock()
        with patch.object(
            worker_pg, "ThreadPoolExecutor", fake_exec_cls
        ), patch.object(
            worker_pg, "_supervisor_force_fail",
            return_value=(True, "preview", probe_result),
        ), patch.object(
            worker_pg, "_get_conn", return_value=follow_up_conn
        ), patch.object(
            worker_pg, "send_build_failed"
        ) as mock_failed, patch.object(
            worker_pg, "send_preview_ready"
        ) as mock_customer, patch.object(
            worker_pg, "send_review_pending"
        ) as mock_review:
            worker_pg._process_job(self._make_job(), MagicMock())

        mock_failed.assert_not_called()
        mock_review.assert_called_once()
        # Verify the probed URL made it into the email call
        _args, kwargs = mock_review.call_args
        assert kwargs.get("preview_url") == "https://probed-url.up.railway.app", (
            f"send_review_pending got preview_url={kwargs.get('preview_url')!r}, "
            "expected the probed live URL"
        )
        # The customer is told as well, with the same URL, and the follow-up
        # used (and closed) a connection of its own, not the stuck thread's.
        mock_customer.assert_called_once()
        assert mock_customer.call_args[0][3] == "https://probed-url.up.railway.app"
        follow_up_conn.close.assert_called_once()

    def test_a_recovered_deploy_is_announced_even_without_a_database_connection(self):
        """The follow-up opens its own connection (the job's belongs to the
        stuck thread). If that fails, the reviewer and the customer are still
        told: the emails do not need the database. Nothing is deleted."""
        fake_exec_cls, _, _ = self._fake_executor_that_times_out()
        probe_result = {
            "live_url": "https://probed-url.up.railway.app",
            "project_id": "proj-p",
            "service_id": "svc-p",
        }
        with patch.object(
            worker_pg, "ThreadPoolExecutor", fake_exec_cls
        ), patch.object(
            worker_pg, "_supervisor_force_fail",
            return_value=(True, "preview", probe_result),
        ), patch.object(
            worker_pg, "_get_conn", side_effect=RuntimeError("DATABASE_URL environment variable is not set")
        ), patch.object(
            worker_pg, "send_build_failed"
        ) as mock_failed, patch.object(
            worker_pg, "send_preview_ready"
        ) as mock_customer, patch.object(
            worker_pg, "send_review_pending"
        ) as mock_review, patch("deploy.destroy_project") as destroy:
            worker_pg._process_job(self._make_job(), MagicMock())

        mock_failed.assert_not_called()
        mock_review.assert_called_once()
        assert mock_review.call_args.kwargs["preview_url"] == "https://probed-url.up.railway.app"
        mock_customer.assert_called_once()
        destroy.assert_not_called()


# ---------------------------------------------------------------------------
# _run_build_in_thread(): BaseException zombie-protection contract
# ---------------------------------------------------------------------------

class TestRunBuildInThreadZombieProtection:
    """Regression tests for the _run_build_in_thread BaseException contract.

    Context: a cli module written as a command can call sys.exit() from a
    function the pipeline uses (test.test_app does when the build directory is
    missing). sys.exit() raises SystemExit, which is a BaseException subclass
    but NOT an Exception subclass. A wrapper that used 'except Exception'
    would miss it and leave the build stuck at its last status (e.g.
    'deploying') forever, a zombie that never gets marked 'failed'.

    The wrapper uses 'except BaseException' so SystemExit, KeyboardInterrupt,
    and any other non-Exception BaseException subclass still triggers the
    zombie-protection path and marks the build 'failed'.
    """

    def _make_job(self, build_id="build-test-1"):
        return {
            "attempt": ATTEMPT,
            "id": build_id,
            "app_name": "test-app",
            "tier": "build",
            "spec": {"app_name": "test-app"},
            "customer_id": "cust-1",
            "customer_email": "test@example.com",
            "customer_name": "Test User",
            "retry_count": 0,
        }

    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_get_conn")
    @patch.object(worker_pg, "_process_job", side_effect=SystemExit(1))
    def test_system_exit_marks_build_failed(self, mock_process, mock_get_conn, mock_fail_build):
        """SystemExit from sys.exit() inside _process_job (a cli module that
        exits instead of raising) must fail the build rather than
        leave it stuck at 'deploying', and through the terminal path, so the
        crash leaves a dead-letter row and the customer is told."""
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        with patch.dict(os.environ, {"ADMIN_EMAIL": "admin@example.test"}):
            worker_pg._run_build_in_thread(self._make_job())

        mock_fail_build.assert_called_once()
        args, kwargs = mock_fail_build.call_args
        assert args[1] == "build-test-1"
        # Error message must name the exception type for diagnostics
        assert "SystemExit" in args[2], (
            f"error message should contain 'SystemExit', got: {args[2]!r}"
        )
        assert args[3:7] == ("test@example.com", "Test User", "test-app", "admin@example.test")
        assert kwargs == {"retry_count": 0, "attempt": ATTEMPT}

    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_get_conn")
    @patch.object(worker_pg, "_process_job", side_effect=KeyboardInterrupt())
    def test_keyboard_interrupt_marks_build_failed(self, mock_process, mock_get_conn, mock_fail_build):
        """KeyboardInterrupt must also trigger zombie protection, it is a
        BaseException subclass that 'except Exception' silently misses."""
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        worker_pg._run_build_in_thread(self._make_job())

        mock_fail_build.assert_called_once()
        assert "KeyboardInterrupt" in mock_fail_build.call_args[0][2], (
            "error message should include the exception type name"
        )

    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_get_conn")
    @patch.object(worker_pg, "_process_job", side_effect=RuntimeError("kaboom"))
    def test_regular_exception_still_marks_failed(self, mock_process, mock_get_conn, mock_fail_build):
        """Regression: ordinary Exception subclasses (RuntimeError, etc.) must
        still be caught and the build failed, the BaseException change must
        not break the existing happy path."""
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        worker_pg._run_build_in_thread(self._make_job())

        mock_fail_build.assert_called_once()
        assert "RuntimeError" in mock_fail_build.call_args[0][2]

    @patch.object(worker_pg, "_fail_build", side_effect=RuntimeError("database gone"))
    @patch.object(worker_pg, "_get_conn")
    @patch.object(worker_pg, "_process_job", side_effect=RuntimeError("kaboom"))
    def test_a_failing_crash_record_does_not_escape_the_thread(self, mock_process, mock_get_conn, mock_fail_build):
        mock_get_conn.return_value = MagicMock()
        worker_pg._run_build_in_thread(self._make_job())
        mock_fail_build.assert_called_once()

    @patch.object(worker_pg, "_fail_build")
    @patch.object(worker_pg, "_get_conn")
    @patch.object(worker_pg, "_process_job", side_effect=SystemExit(1))
    def test_system_exit_does_not_propagate(self, mock_process, mock_get_conn, mock_fail_build):
        """_run_build_in_thread must never re-raise SystemExit, the thread pool
        would silently swallow it, but the caller (poll loop) must not crash."""
        mock_get_conn.return_value = MagicMock()
        # Must not raise, if it does the test itself will fail
        worker_pg._run_build_in_thread(self._make_job())

    def test_uses_base_exception_in_source(self):
        """Contract lock: verify _run_build_in_thread uses BaseException, not
        Exception, so future refactors cannot silently regress this fix.

        This is the same source-inspection pattern used by TestNoSigalrmInProcessJob
        for the SIGALRM removal contract."""
        import inspect
        source = inspect.getsource(worker_pg._run_build_in_thread)
        assert "except BaseException as exc:" in source, (
            "_run_build_in_thread must use 'except BaseException as exc:' "
            "to catch SystemExit (from sys.exit in a cli module) and "
            "KeyboardInterrupt; 'except Exception' silently misses them and "
            "leaves builds stuck at 'deploying' forever."
        )
        assert "except Exception as exc:" not in source, (
            "_run_build_in_thread must NOT use 'except Exception' as the outer "
            "catch; this would miss SystemExit and KeyboardInterrupt."
        )


# ---------------------------------------------------------------------------
# Section N: Local build no-token guard (LOCAL_BUILD_GUARD1)
# ---------------------------------------------------------------------------

class TestLocalBuildNoTokenGuard:
    """When RAILWAY_API_TOKEN is absent, the pipeline must skip deploy and mark
    the build as preview instead of failing.  This prevents local dev runs from
    burning retries on a config gap rather than a code bug."""

    def _make_conn(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        return conn

    def test_no_token_sets_preview_not_failed(self):
        """Source-level contract: verify _run_pipeline contains the LOCAL BUILD GUARD
        that skips Railway deploy and marks build as preview when RAILWAY_API_TOKEN is absent.

        We inspect the source rather than running the full pipeline (which requires mocking
        ~20 internal closures) because the behavioral guarantee is already captured by the
        source contract tests below + the integration test in test_worker_integration.py.
        This test fails immediately if someone removes the guard during refactoring."""
        import inspect

        source = inspect.getsource(worker_pg._run_pipeline)

        # Guard condition must check the token
        assert "RAILWAY_API_TOKEN" in source, (
            "_run_pipeline must check RAILWAY_API_TOKEN before attempting Railway deploy"
        )
        # Must return early with local_preview=True
        assert "local_preview" in source, (
            "_run_pipeline must return local_preview=True when RAILWAY_API_TOKEN is absent"
        )
        # Must call _set_preview_status with preview_url=None (not live_url)
        assert "preview_url=None" in source, (
            "_run_pipeline must call _set_preview_status(preview_url=None) for local builds; "
            "calling with live_url=None would use the wrong keyword arg"
        )
        # The guard must appear BEFORE the deploy block, not inside it
        token_check_pos = source.find("RAILWAY_API_TOKEN")
        deploy_pos = source.find("_set_status(conn, build_id, \"deploying\")")
        if deploy_pos == -1:
            deploy_pos = source.find("deploy.deploy(")
        # token check must precede the main deploy call
        assert token_check_pos < deploy_pos or deploy_pos == -1, (
            "RAILWAY_API_TOKEN guard must appear before the Railway deploy call in source"
        )

    def test_no_token_guard_in_source(self):
        """Contract lock: verify the no-token guard exists in _run_pipeline source.
        Prevents accidental removal during refactors."""
        import inspect
        source = inspect.getsource(worker_pg._run_pipeline)
        assert "RAILWAY_API_TOKEN" in source, (
            "_run_pipeline must check RAILWAY_API_TOKEN to guard local builds"
        )
        assert "local_preview" in source, (
            "_run_pipeline must return local_preview=True for local builds without token"
        )
        assert "preview_url=None" in source, (
            "_run_pipeline must call _set_preview_status(preview_url=None) for local builds"
        )

    def test_process_job_skips_emails_on_local_preview(self):
        """Source contract: _process_job must early-return when deploy_result contains
        local_preview=True, skipping the duplicate _set_preview_status call, Railway
        metadata storage, send_review_pending, and send_preview_ready (no URL to share).

        Prevents: (1) empty-URL "preview ready" email sent to customer inbox after a
        local build, (2) double preview-status write that overwrites preview_url from
        NULL to '' on the LOCAL_BUILD_GUARD1 path."""
        import inspect

        source = inspect.getsource(worker_pg._process_job)

        # The guard must check the local_preview flag from deploy_result
        assert "local_preview" in source, (
            "_process_job must check deploy_result['local_preview'] "
            "to skip customer emails for local builds without Railway"
        )
        # The guard must trigger an early return (within ~400 chars after the flag check)
        assert "return" in source.split("local_preview")[1][:400], (
            "_process_job must return early after detecting local_preview=True "
            "so send_preview_ready is never called with an empty URL"
        )


# ---------------------------------------------------------------------------
# Section O: RETRY_GUARDRAIL_TZ1-2-hour retry guardrail TZ-safety
# ---------------------------------------------------------------------------

class TestRetryTimeBudget:
    """The retry time budget is measured from the first attempt of the current
    series of retries, on a clock in the worker. It used to be measured from
    builds.created_at, the age of the ROW: a row is created when the intake
    conversation starts and a manual retry reuses a row of any age, so most
    builds had "exceeded 2 hours" before their first attempt had failed once,
    and were never retried at all."""

    def _job(self, build_id, retry_count=0):
        return {
            "attempt": ATTEMPT, "id": build_id, "app_name": "budget-app", "tier": "build",
            "spec": {}, "customer_id": "c", "customer_email": "t@example.test",
            "customer_name": "T", "retry_count": retry_count,
        }

    def _fail_once(self, job, row_created_at=None):
        """Run _process_job with a pipeline that fails transiently. The row
        the mocked cursor returns is as old as the caller says."""
        from datetime import datetime as _dt, timedelta as _td
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (row_created_at or _dt.now() - _td(days=3),)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        with patch.object(worker_pg, "_run_pipeline", side_effect=RuntimeError("Connection reset by peer")), \
             patch.object(worker_pg, "_requeue_for_retry", return_value=True) as requeue, \
             patch.object(worker_pg, "_fail_build") as fail, \
             patch.object(worker_pg, "sentry_sdk"):
            worker_pg._process_job(job, mock_conn)
        return requeue, fail

    def test_a_build_on_a_three_day_old_row_is_still_retried(self):
        build_id = "budget-old-row"
        try:
            requeue, fail = self._fail_once(self._job(build_id))
            requeue.assert_called_once()
            fail.assert_not_called()
        finally:
            worker_pg._end_retry_series(build_id)

    def test_the_clock_runs_across_the_retries_of_one_series(self):
        build_id = "budget-series"
        try:
            with patch.object(worker_pg.time, "monotonic", return_value=1000.0):
                requeue, fail = self._fail_once(self._job(build_id, retry_count=0))
            requeue.assert_called_once()
            # The retry is claimed a little over two hours after the series began.
            later = 1000.0 + worker_pg._RETRY_TIME_BUDGET_S + 5
            with patch.object(worker_pg.time, "monotonic", return_value=later):
                requeue, fail = self._fail_once(self._job(build_id, retry_count=1))
            requeue.assert_not_called()
            fail.assert_called_once()
        finally:
            worker_pg._end_retry_series(build_id)

    def test_a_requeue_by_a_person_starts_a_new_series(self):
        """retry_count is back at 0 (the web app's retry, reset and requeue
        statements set it): the clock starts again."""
        build_id = "budget-reset"
        try:
            with patch.object(worker_pg.time, "monotonic", return_value=1000.0):
                self._fail_once(self._job(build_id, retry_count=0))
            later = 1000.0 + worker_pg._RETRY_TIME_BUDGET_S + 5
            with patch.object(worker_pg.time, "monotonic", return_value=later):
                requeue, fail = self._fail_once(self._job(build_id, retry_count=0))
            requeue.assert_called_once()
            fail.assert_not_called()
        finally:
            worker_pg._end_retry_series(build_id)

    def test_the_series_is_forgotten_when_the_build_is_not_requeued(self):
        build_id = "budget-ended"
        mock_conn = MagicMock()
        with patch.object(worker_pg, "_run_pipeline", side_effect=worker_pg.QAQualityGateError("QA")), \
             patch.object(worker_pg, "_fail_build"), patch.object(worker_pg, "sentry_sdk"):
            worker_pg._process_job(self._job(build_id), mock_conn)
        assert build_id not in worker_pg._RETRY_SERIES_STARTED


class TestCasBuildEngineDispatch:
    """CAS_BUILD_ENGINE=v1|v2 routes the build stage to either the legacy
    Claude-generated cli/build.py or the static-compose v2 compose/build.py.

    v1 = default (backward-compatible). v2 = shadow rollout; flip per-worker.
    Unknown values raise ValueError before any state mutation.
    """

    def test_source_reads_cas_build_engine_env_var(self):
        """Guard: the dispatch code must read CAS_BUILD_ENGINE from the env.
        String-search the source because mocking the worker's build stage
        requires a full pytest-postgres harness that other tests don't set up."""
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        assert 'os.environ.get("CAS_BUILD_ENGINE")' in worker_src, (
            "Worker must consult CAS_BUILD_ENGINE to choose v1 vs v2 engine"
        )

    def test_source_dispatches_to_compose_build_for_v2(self):
        """v2 path must import compose/build.py, not cli/build.py."""
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        assert "from compose.build import build_app" in worker_src, (
            "v2 engine must import from compose.build"
        )
        assert 'if _engine == "v2"' in worker_src, (
            "Worker must branch on engine=='v2'"
        )

    def test_source_defaults_to_v2_when_env_unset(self):
        """2026-04-27: v2 is the production default. Prod has been on v2
        since 2026-04-22; the v1 Claude-per-build path is legacy + opt-in
        only via CAS_BUILD_ENGINE=v1."""
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        # Default value is "v2" when env var missing.
        assert 'os.environ.get("CAS_BUILD_ENGINE") or "v2"' in worker_src
        # Legacy default string should not appear unguarded in source.
        assert 'os.environ.get("CAS_BUILD_ENGINE") or "v1"' not in worker_src

    def test_source_rejects_unknown_engine_values(self):
        """Typos in env config should fail-fast, not silently fall through
        to either path."""
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        assert 'CAS_BUILD_ENGINE must be' in worker_src, (
            "Worker must validate CAS_BUILD_ENGINE and raise on unknown values"
        )

    def test_compose_build_module_is_importable(self):
        """The v2 import path `compose.build.build_app` must resolve.
        If this fails, the worker will crash at build-stage when v2 is active."""
        from compose.build import build_app as compose_build_app
        assert callable(compose_build_app)


class TestV2BuildOutputLint:
    """Post-build output lint that runs during testing-stage for
    CAS_BUILD_ENGINE=v2. Replaces the Claude-driven v1 test/QA/design
    loops with static assertions on the emitted build artifact."""

    def test_lint_helper_is_exported(self):
        """Worker must export _lint_v2_build_output so the testing-stage
        branch can call it. Renaming would break customer-visible log lines."""
        import worker_pg
        assert callable(worker_pg._lint_v2_build_output)

    def test_lint_returns_checks_and_errors_structure(self, tmp_path):
        """Contract: returns a dict with 'checks' and 'errors' keys, both
        lists of strings. This is the shape the log appender depends on."""
        from worker_pg import _lint_v2_build_output
        # Empty dir → everything missing, but structure must still hold.
        result = _lint_v2_build_output(tmp_path)
        assert isinstance(result, dict)
        assert "checks" in result and isinstance(result["checks"], list)
        assert "errors" in result and isinstance(result["errors"], list)

    def test_lint_reports_missing_required_files(self, tmp_path):
        """Empty build dir should surface a 'Missing required files' error
        so the customer log makes it obvious the compose output is broken."""
        from worker_pg import _lint_v2_build_output
        result = _lint_v2_build_output(tmp_path)
        errs = result["errors"]
        assert any("Missing required files" in e for e in errs), errs

    def test_lint_passes_on_a_fresh_compose_build(self, tmp_path):
        """Running the lint on a live compose output should return PASSING
        checks. Regression guard, a pipeline change that breaks the
        invariants this lint asserts will fail here before it fails in prod."""
        from compose.build import build_app
        from worker_pg import _lint_v2_build_output
        out = tmp_path / "fresh-build"
        build_app("compose/fixtures/sample-healer-spec.json", output_dir=out)
        result = _lint_v2_build_output(out)
        # Fresh builds must have non-empty checks and zero errors.
        assert len(result["checks"]) >= 5, f"Expected >= 5 passing checks, got {len(result['checks'])}: {result['checks']}"
        assert result["errors"] == [], f"Fresh compose build tripped lint errors: {result['errors']}"

    def test_lint_alignment_check_passes_on_aligned_build(self, tmp_path):
        """The 2026-04-29 alignment check should ADD a pass message when
        every routes/<slug>.js has a public consumer. Mock a minimal
        aligned build dir and assert the pass message lands in checks."""
        import os
        from worker_pg import _lint_v2_build_output
        bd = tmp_path / "aligned-build"
        # Required files (minimal stubs so the file-presence check passes
        # alongside the alignment check we care about).
        (bd / "db").mkdir(parents=True)
        (bd / "views" / "widgets" / "shell").mkdir(parents=True)
        (bd / "views" / "admin").mkdir(parents=True)
        (bd / "public" / "js").mkdir(parents=True)
        (bd / "routes").mkdir(parents=True)
        for stub in [
            "compose-manifest.txt", "server.js", "package.json", "db_migrate.js",
            "db/schema.sql", "db/seed.sql",
            "views/layout-public.ejs", "views/layout-admin.ejs",
            "public/style.css", "public/logo.svg", "public/favicon.svg",
            "public/js/cas-reveal.js", "public/js/cas-form-validate.js",
        ]:
            (bd / stub).write_text("// stub\n")
        # Manifest the loose checks expect.
        (bd / "compose-manifest.txt").write_text(
            "archetype: x\npalette: x\nfont_pair: x\nfiles: 1\nsha256: x\n"
        )
        # style.css with required utility classes
        (bd / "public" / "style.css").write_text(
            ".cas-btn .cas-card-lift .cas-eyebrow .cas-reveal .cas-container "
            "Söhne Tiempos\n"
        )
        # Footer with attribution
        (bd / "views" / "widgets" / "shell" / "footer.ejs").write_text(
            "<footer>powered by craftandship.io</footer>\n"
        )
        # routes/pages.js has SELECT FROM blog_posts (consumes blog-posts model)
        (bd / "routes" / "pages.js").write_text(
            "router.get('/journal', async (req, res) => { "
            "const { rows } = await db.query('SELECT * FROM blog_posts'); });"
        )
        # routes/blog-posts.js admin CRUD (orphan candidate, but consumed by pages.js)
        (bd / "routes" / "blog-posts.js").write_text(
            "if (!req.baseUrl.startsWith('/admin/')) return res.status(404);\n"
        )
        # routes/admin-dashboard.js system route (always exempt)
        (bd / "routes" / "admin-dashboard.js").write_text("// admin dash\n")
        result = _lint_v2_build_output(bd)
        assert any("Admin tabs all have a public consumer" in c for c in result["checks"]), (
            f"expected alignment-pass message; got checks: {result['checks']}"
        )

    def test_lint_alignment_check_flags_orphan_admin_tab(self, tmp_path):
        """Synthesize a build with an orphan admin tab, a model whose
        table appears in NO public view, NO pages.js handler, NO public
        form action, and assert the alignment lint emits an
        'alignment-warn' message. This is the regression guard for the
        2026-04-29 feedback that surfaced BookingIntent as an orphan admin
        tab on an early build."""
        from worker_pg import _lint_v2_build_output
        bd = tmp_path / "orphan-build"
        # Required files for the file-presence check.
        (bd / "db").mkdir(parents=True)
        (bd / "views" / "widgets" / "shell").mkdir(parents=True)
        (bd / "public" / "js").mkdir(parents=True)
        (bd / "routes").mkdir(parents=True)
        for stub in [
            "compose-manifest.txt", "server.js", "package.json", "db_migrate.js",
            "db/schema.sql", "db/seed.sql",
            "views/layout-public.ejs", "views/layout-admin.ejs",
            "public/style.css", "public/logo.svg", "public/favicon.svg",
            "public/js/cas-reveal.js", "public/js/cas-form-validate.js",
        ]:
            (bd / stub).write_text("// stub\n")
        (bd / "compose-manifest.txt").write_text(
            "archetype: x\npalette: x\nfont_pair: x\nfiles: 1\nsha256: x\n"
        )
        (bd / "public" / "style.css").write_text(
            ".cas-btn .cas-card-lift .cas-eyebrow .cas-reveal .cas-container Söhne Tiempos\n"
        )
        (bd / "views" / "widgets" / "shell" / "footer.ejs").write_text(
            "<footer>powered by craftandship.io</footer>\n"
        )
        # routes/pages.js: NO mention of orphan_thing (the orphan)
        (bd / "routes" / "pages.js").write_text(
            "router.get('/', (req, res) => res.render('home'));\n"
        )
        # routes/orphan-things.js: admin-only, no public consumer anywhere.
        (bd / "routes" / "orphan-things.js").write_text(
            "if (!req.baseUrl.startsWith('/admin/')) {\n"
            "  return res.status(404).render('error-404');\n"
            "}\n"
        )
        result = _lint_v2_build_output(bd)
        assert any(
            "alignment-warn" in c and "orphan-things" in c for c in result["checks"]
        ), f"expected alignment-warn flagging orphan-things; got: {result['checks']}"

    def test_lint_exempts_per_item_detail_views(self, tmp_path):
        """`*-detail.ejs` files emitted by emit_page_detail_view render a
        single DB record with tolerant column reads, not a widget-grid
        composition, so the "all page views composed from widgets" lint must
        skip them. Regression target: 2026-04-27 detail-route fix tripped the
        lint with healer journal-detail.ejs + events-detail.ejs."""
        from compose.build import build_app
        from worker_pg import _lint_v2_build_output
        out = tmp_path / "fresh-build"
        build_app("compose/fixtures/sample-healer-spec.json", output_dir=out)
        # The build does emit *-detail.ejs files (sanity).
        detail_views = list((out / "views").glob("*-detail.ejs"))
        assert len(detail_views) >= 2, (
            f"expected at least 2 detail views (journal-detail, events-detail); "
            f"got {[v.name for v in detail_views]}"
        )
        # And the lint passes (i.e. those files were skipped from the
        # widget-composition check).
        result = _lint_v2_build_output(out)
        assert result["errors"] == [], (
            f"detail views must be exempt from widget-composition lint: {result['errors']}"
        )

    def test_lint_source_calls_append_log_for_each_check(self):
        """Guard the customer-visible log contract: the worker's v2 testing
        branch must actually write each lint check to build_logs via
        _append_log. String-searches the source, a refactor that moved
        the iteration elsewhere would break the customer-visible per-check
        output that today's founder feedback asked for."""
        import pathlib
        src = pathlib.Path("queue/worker_pg.py").read_text()
        assert 'Running v2 build-output lint' in src, (
            "v2 testing branch must log the 'Running v2 build-output lint' header"
        )
        assert 'for _line in _v2_lint.get("checks"' in src, (
            "v2 testing branch must iterate lint check results and log each one"
        )
        assert "v2 lint PASSED" in src, (
            "v2 testing branch must log the pass summary for the customer"
        )
        assert "LINT FAIL:" in src, (
            "v2 testing branch must surface per-check failures prominently"
        )

    def test_v2_branch_calls_translate_v1_spec_on_v1_shape(self):
        """Source-level guard: the v2 validate branch must detect a v1-shape
        spec (has spec_version/user_roles/data_models but no archetype) and
        call compose.intake.translate_v1_spec() to convert it before the
        v2 validator runs. Without this, prod customer intakes (which emit
        v1-shape specs) would hard-fail at validation when CAS_BUILD_ENGINE=v2."""
        import pathlib
        src = pathlib.Path("queue/worker_pg.py").read_text()
        assert "from compose.intake import translate_v1_spec" in src, (
            "Worker must import translate_v1_spec for v1→v2 conversion"
        )
        assert "translate_v1_spec(_spec_dict" in src, (
            "Worker must actually call translate_v1_spec()"
        )
        # Auto-detection triggers: v1 markers present, v2 archetype absent
        assert '"archetype" not in _spec_dict' in src, (
            "Worker must detect missing archetype as v1-shape signal"
        )
        assert '"spec_version" in _spec_dict' in src, (
            "Worker must detect v1 spec_version marker"
        )
        # Must preserve tier + admin_email + app_name across translation
        assert "_spec_dict.get(\"tier\")" in src, (
            "Worker must preserve tier across v1→v2 translation"
        )

    def test_v2_branch_passes_tier_to_compose_build(self):
        """v2 build must pass tier through so compose records it in the
        manifest for audit. Tier-gated features downstream read
        builds.tier directly from DB, unchanged."""
        import pathlib
        src = pathlib.Path("queue/worker_pg.py").read_text()
        assert "compose_build_app(spec_path, output_dir=build_dir, tier=tier)" in src, (
            "v2 build dispatch must pass tier= argument to compose.build.build_app"
        )

    def test_compose_build_app_accepts_tier_kwarg(self):
        """compose.build.build_app must accept `tier` kwarg and record it
        in the manifest. This is the API contract the worker relies on."""
        from compose.build import build_app
        import inspect
        sig = inspect.signature(build_app)
        assert "tier" in sig.parameters, "build_app must accept tier kwarg"

    def test_compose_manifest_records_tier_when_supplied(self, tmp_path):
        """Fresh compose build with tier='build' should record tier in
        compose-manifest.txt so SE reviewers + admin dashboard can audit
        which pricing tier produced the build."""
        from compose.build import build_app
        out = tmp_path / "tier-build"
        build_app("compose/fixtures/sample-healer-spec.json", output_dir=out, tier="grow")
        manifest = (out / "compose-manifest.txt").read_text()
        assert "tier: grow" in manifest, (
            f"compose-manifest.txt must record tier; got: {manifest}"
        )

    def test_lint_catches_raw_heading_widget_regression(self, tmp_path):
        """If a widget reverts to `<%= heading %>` without the _accent
        transform, the lint should catch it. This is the exact bug class
        that leaked {Work} raw to the page before the fix."""
        from compose.build import build_app
        from worker_pg import _lint_v2_build_output
        out = tmp_path / "regressed-build"
        build_app("compose/fixtures/sample-healer-spec.json", output_dir=out)
        # Simulate regression: put a raw heading widget back.
        widget = out / "views" / "widgets" / "offerings" / "grid-3col.ejs"
        body = widget.read_text()
        # Strip the transform + put a raw output back
        regressed = body.replace("<%- _accent(heading) %>", "<%= heading %>")
        # And remove the _accent helper so the widget falls through detection:
        regressed = regressed.split("<% function _accent")[0] + regressed.split("%>", 1)[1] if "function _accent" in regressed else regressed
        widget.write_text(regressed)
        result = _lint_v2_build_output(out)
        assert any("italic-accent transform" in e or "heading" in e for e in result["errors"]), (
            f"Lint failed to catch raw-heading regression: {result['errors']}"
        )


def _v2_deploy_fix_gate_start(worker_src: str) -> int:
    """Offset of the `if _engine == "v2":` block that skips deploy_fix.

    Anchored on the code (the log line that only this block emits), not on a
    comment, so rewording comments cannot break the guard tests.
    """
    log_marker = "deploy crashed but skipping deploy_fix"
    assert log_marker in worker_src, "v2 deploy_fix gate is missing from worker source"
    return worker_src.rindex('if _engine == "v2":', 0, worker_src.index(log_marker))


class TestV2DeployFixSkip:
    """CAS_BUILD_ENGINE=v2 must NOT run the v1 deploy_fix loop on v2 artifacts.

    Context: v2 compose output is deterministic and uses a different file
    layout than v1 (e.g. views/layout-public.ejs, not views/layout.ejs). The
    v1 deploy_fix is a Claude-driven patch loop that assumes v1 shape. When
    deploy_fix runs on a v2 artifact, it:
      1. Rewrites server.js / routers in ways that drop routes v2 had emitted
         (observed 2026-04-22 on soundbath-scheduler: /sessions,
         /book 404 after attempt 1).
      2. Triggers reapply_post_processors which fails with
         'views/layout.ejs: file missing' because v2 never emits that file.

    Correct v2 behavior on deploy crash: persist crash logs and fail-fast so
    the signal feeds back into compose/, rather than masking the bug with a
    runtime AI patch.

    These guards are string-based on worker_pg.py source because mocking the
    full _process_job pipeline to drive to the deploy-fix branch requires a
    pytest-postgres harness other tests don't set up. This matches the
    pattern used by TestCasBuildEngineDispatch above.
    """

    def test_source_has_v2_gate_before_deploy_fix_loop(self):
        """The v2 gate MUST precede the `for deploy_fix_attempt` loop.

        Regression signature: without this gate, the worker runs Claude-driven
        deploy_fix on v2 output and corrupts it.
        """
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        # Find the deploy_fix loop header and the v2 gate
        loop_marker = 'for deploy_fix_attempt in range(max_deploy_fixes)'
        assert loop_marker in worker_src, (
            f"Missing deploy_fix loop '{loop_marker}' in worker source"
        )
        gate_pos = _v2_deploy_fix_gate_start(worker_src)
        loop_pos = worker_src.index(loop_marker)
        assert gate_pos < loop_pos, (
            "v2 deploy_fix gate must appear BEFORE the deploy_fix loop; "
            "otherwise v1 deploy_fix runs on v2 artifacts and corrupts them."
        )

    def test_source_v2_gate_raises_what_the_classifier_returns(self):
        """Tripwire on source text: the v2 gate ends the attempt with the
        error _v2_deploy_failure builds (what that error is, per case, is
        tested below and, through the whole pipeline on a real database, in
        tests/test_worker_lifecycle_realdb.py)."""
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        gate_start = _v2_deploy_fix_gate_start(worker_src)
        gate_block = worker_src[gate_start:gate_start + 5000]
        assert 'if _engine == "v2":' in gate_block
        assert "raise _v2_deploy_failure(deploy_result, _v2_dep_logs, app_name)" in gate_block

    @pytest.mark.parametrize("deploy_result, logs", [
        ({"deployment_status": "CRASHED"}, ["Error: Cannot find module './routes/missing'"]),
        ({"deployment_status": "SUCCESS", "health_check": "failed"}, []),
        ({"health_check": "failed"}, []),   # a caller that reports no deployment status
    ])
    def test_a_crash_of_the_generated_app_is_deterministic(self, deploy_result, logs):
        """The app crashed on start for a reason of its own, or is up and
        never answers healthy: the same spec will do the same again."""
        error = worker_pg._v2_deploy_failure(deploy_result, logs, "sample-app")
        assert worker_pg._is_deterministic_failure(str(error))
        assert "deploy_fix skipped" in str(error)

    @pytest.mark.parametrize("logs", [None, [], [""], ["  ", None]])
    def test_a_crash_with_no_readable_log_is_not_called_the_apps_fault(self, logs):
        """No log line was read (None: the read failed or there was no
        deployment to ask), so nothing shows whose fault the crash is. It is
        not tagged deterministic and not transient: it gets one fresh attempt
        and the repeated-error check ends the series if it happens again."""
        error = worker_pg._v2_deploy_failure({"deployment_status": "CRASHED"}, logs, "sample-app")
        assert not worker_pg._is_deterministic_failure(str(error))
        assert not worker_pg._is_transient_error(str(error))
        assert "no log of it could be read" in str(error)
        assert worker_pg._classify_error(str(error)) == "infra"

    def test_a_deployment_railway_could_not_start_is_not_deterministic(self):
        """FAILED is Railway's word for "no container ran": its build step or
        its start step. Every such failure used to be tagged deterministic,
        so one bad minute at the platform ended the build for good."""
        error = worker_pg._v2_deploy_failure({"deployment_status": "FAILED"}, [], "sample-app")
        assert not worker_pg._is_deterministic_failure(str(error))
        assert not worker_pg._is_transient_error(str(error)), \
            "an unchanged text lets the repeated-error check stop after one more attempt"
        assert worker_pg._classify_error(str(error)) == "infra"

    @pytest.mark.parametrize("line", [
        "Error: connect ECONNREFUSED 10.0.0.2:5432",
        "FATAL: the database system is starting up",
        "Could not connect to database after 5 attempts",
    ])
    def test_a_crash_because_the_database_was_unreachable_is_transient(self, line):
        error = worker_pg._v2_deploy_failure({"deployment_status": "CRASHED"}, ["booting", line], "sample-app")
        assert not worker_pg._is_deterministic_failure(str(error))
        assert worker_pg._is_transient_error(str(error))

    def test_deterministic_failures_are_recognized(self):
        """The retry decision asks _is_deterministic_failure: the engine's own
        tag and a refused spec both mean the same input fails the same way."""
        from worker_pg import _is_deterministic_failure

        assert _is_deterministic_failure(
            "[v2-deterministic] v2 app deployed but crashes on startup."
        )
        assert _is_deterministic_failure(
            "Spec validation failed: custom page '/members' is marked auth_required, "
            "but that path is answered without a login by a public page"
        )
        assert _is_deterministic_failure(
            "Spec validation failed with 2 error(s): app_name is required"
        )
        for retryable in ("connection reset by peer", "Railway deploy timeout", "", None):
            assert not _is_deterministic_failure(retryable)

    def test_retry_decision_uses_the_deterministic_check(self):
        """Tripwire on source text: the retry decision must consult the helper
        and clear can_retry (the behavior is exercised by the real-database
        retry tests; this pins the wiring)."""
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        needle = "if can_retry and _is_deterministic_failure(error_message):"
        assert needle in worker_src
        idx = worker_src.index(needle)
        assert "can_retry = False" in worker_src[idx:idx + 200]

    def test_source_v2_persists_deploy_logs_before_raising(self):
        """Before raising, the v2 gate must persist Railway deploy logs so
        the post-mortem works after Railway auto-cleanup tears down the
        orphaned project."""
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        gate_start = _v2_deploy_fix_gate_start(worker_src)
        gate_block = worker_src[gate_start:gate_start + 4000]
        assert '_persist_deploy_logs' in gate_block, (
            "v2 gate must persist deploy logs before raising so v2 crash "
            "trajectories remain debuggable after Railway project cleanup"
        )
        assert 'v2-deploy-crash' in gate_block, (
            "v2 persisted-logs reason tag must identify the crash as v2-specific"
        )

    def test_source_v2_deploy_crash_log_fetch_waits_and_retries(self):
        """Railway's deployment-log API may not have flushed late stderr
        (the actual crash error) by the time the worker fires its log
        fetch. The crashes of one sample site captured only 8 lines because
        of this race. Fix: sleep then fetch, refetch once if first call
        returned a thin set, persist with bumped max_lines so the full
        crash output isn't truncated downstream.
        """
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        gate_start = _v2_deploy_fix_gate_start(worker_src)
        gate_block = worker_src[gate_start:gate_start + 4500]
        # Wait at least 5 seconds before fetching (Railway flush window).
        assert 'time.sleep(8)' in gate_block, (
            "v2 deploy-crash path must sleep ≥5s to let Railway flush late "
            "stderr before fetching deployment logs"
        )
        # Bump the requested log line count from 200 to 500.
        assert 'limit=500' in gate_block, (
            "v2 deploy-crash log fetch must request at least 500 lines"
        )
        # Persist with max_lines bumped to 500 so _persist_deploy_logs
        # doesn't itself truncate.
        assert 'max_lines=500' in gate_block, (
            "_persist_deploy_logs call must pass max_lines=500"
        )
        # Refetch-on-thin-result safety net.
        assert 'len(_v2_dep_logs) < 30' in gate_block, (
            "v2 deploy-crash path must refetch when first call returned a "
            "thin (< 30 lines) result"
        )

    def test_source_v1_deploy_fix_loop_still_exists(self):
        """Regression guard: we removed deploy_fix for v2 ONLY. The loop
        itself must remain for v1 builds (which rely on it to self-heal
        Claude-generated code)."""
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        assert 'from deploy_fix import attempt_deploy_fix' in worker_src, (
            "v1 deploy_fix import must still exist; v1 builds depend on it"
        )
        assert 'for deploy_fix_attempt in range(max_deploy_fixes)' in worker_src, (
            "v1 deploy_fix loop must still exist for v1 builds"
        )

    def test_source_v2_has_recovery_probe_before_fail_fast(self):
        """Railway's wait_for_deployment raises on transient CRASHED status
        (deploy.py:652), and deploy() short-circuits with health_check=failed
        without calling verify_health. Railway frequently reports CRASHED
        briefly during the first container restart after schema migration,
        then stabilizes to SUCCESS, meaning the app is actually live.

        For v2 (deterministic compose output) this false positive fires the
        fail-fast gate on a working app (observed 2026-04-22 on
        wellness-studio where all routes returned 200 after deploy
        reported failed).

        The worker must re-probe the URL on v2 when deploy reported not-ok,
        and promote to 'ok' on an explicit healthy response.
        """
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        # The recovery probe block must exist, gated by `_engine == "v2"` and
        # `health != "ok"`.
        assert 'v2 recovery probe' in worker_src, (
            "Worker must have a v2-specific recovery probe to catch Railway's "
            "transient CRASHED false positives before firing the fail-fast gate"
        )
        # Behavioral check: must be gated on v2 AND not-ok health
        idx = worker_src.index('v2 recovery probe:')
        block = worker_src[idx:idx + 4000]
        assert '_engine == "v2"' in block, (
            "v2 recovery probe must be gated on engine=='v2'"
        )
        assert 'health != "ok"' in block, (
            "v2 recovery probe must only run when deploy reported not-ok"
        )
        # Must use verify_health with an explicit longer timeout
        assert 'verify_health' in block or '_vh_recover' in block, (
            "v2 recovery probe must call verify_health to re-probe the URL"
        )
        # Must only promote to 'ok' on explicit match (no false positives)
        assert "recovered == \"ok\"" in block or "recovered == 'ok'" in block, (
            "v2 recovery probe must only override to 'ok' on explicit healthy "
            "response (otherwise silently fall through to original path)"
        )

    def test_source_v2_skips_verify_routes(self):
        """The v2 intake translator intentionally drops spec pages that don't
        fit the chosen archetype (e.g. `/book` is dropped when archetype=
        fitness-studio emits `/schedule` + `/classes` instead). Running
        verify_routes against the original spec on v2 produces false 404s
        for every translator-dropped page, flipping health to "failed" and
        firing our v2-deterministic fail-fast gate on otherwise-healthy apps.

        Regression observed 2026-04-22 on pilates-collective: `/book`
        returned 404 while every other route was 200, verify_routes flipped
        health, and the build failed. Fix: on v2, skip verify_routes entirely
       , v2 lint in the testing stage already asserts every page view is
        composed from widgets, so route emission is deterministically
        validated upstream.
        """
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        # Find the verify_routes call site and check it's inside a v2 gate
        route_call_idx = worker_src.index("route_result = verify_routes(")
        # Walk back to find the enclosing branch
        preceding = worker_src[max(0, route_call_idx - 2000):route_call_idx]
        # The most recent _engine check should be the v2 gate that skips
        # verify_routes (expressed as `if _engine == "v2":` with a logger.info
        # about skipping verify_routes, then `else:` containing the call)
        assert 'if _engine == "v2":' in preceding, (
            "verify_routes call must be inside an `if _engine == \"v2\": ... else:` branch"
        )
        # And the v2 branch must log-skip (behavioral check)
        v2_gate_idx = preceding.rindex('if _engine == "v2":')
        v2_block = preceding[v2_gate_idx:]
        assert 'skipping verify_routes' in v2_block or 'skipping v1 spec-route check' in v2_block, (
            "v2 branch above verify_routes must log that it is skipping the v1 route check"
        )

    def test_source_v2_skips_verify_auth(self):
        """v2 seeds admin with password=NULL + password_set=TRUE (magic-link-
        only per SEC-1). verify_auth posts password='password123' which will
        always return 'warning' on v2 (non-blocking but pure log noise + ~20s
        latency per build). Must be gated on v2.

        Regression: this block was ungated before 2026-04-22 and appeared on
        every v2 build's log as 'Warning: password login unavailable (magic
        link mode)'."""
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        assert 'from deploy import verify_auth' in worker_src, (
            "verify_auth import must still exist; v1 builds depend on it"
        )
        # Find the verify_auth call site
        idx = worker_src.index("from deploy import verify_auth")
        preceding = worker_src[max(0, idx - 500):idx]
        assert '_engine != "v2"' in preceding or '_engine == "v1"' in preceding, (
            "verify_auth call must be gated so it does NOT run on v2 builds "
            "(v2 seeds password=NULL; verify_auth with password='password123' "
            "always produces a non-blocking warning that is pure log noise)"
        )

    def test_source_v2_skips_verify_data(self):
        """verify_data derives CRUD paths from spec.data_models (Class →
        /class), but v2's intake translator remaps customer models to archetype
        tables (class_types, schedule_slots, etc.) so those derived paths 404.
        Also, v2 lint already asserts seed row counts deterministically. Skip
        on v2 to save ~15s + remove false warnings."""
        import pathlib
        worker_src = pathlib.Path("queue/worker_pg.py").read_text()
        assert 'from deploy import verify_data' in worker_src, (
            "verify_data import must still exist; v1 builds depend on it"
        )
        idx = worker_src.index("from deploy import verify_data")
        preceding = worker_src[max(0, idx - 500):idx]
        assert '_engine != "v2"' in preceding or '_engine == "v1"' in preceding, (
            "verify_data call must be gated so it does NOT run on v2 builds "
            "(v2 translator remaps model names; derived /<slug> paths would 404 "
            "and produce false 'missing data' warnings)"
        )


# ===========================================================================
# Review round R1-P2: claim safety, state-machine guards, orphan-recovery
# leases, secret redaction, path and archive safety.
# Each test below fails against the pre-review code.
# ===========================================================================

def _ctx_conn(fetchone=None, rowcount=1):
    """MagicMock conn whose cursor() works as a context manager."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.rowcount = rowcount
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cur


class TestClaimUnderAdvisoryLock:
    def test_claim_takes_xact_advisory_lock_first(self):
        conn, cur = _ctx_conn(fetchone=(True,))
        with patch.object(worker_pg, "_active_build_count", return_value=99):
            assert worker_pg._claim_next_job(conn) is None
        first_sql = cur.execute.call_args_list[0][0][0]
        assert "pg_try_advisory_xact_lock" in first_sql

    def test_returns_none_without_counting_when_another_worker_is_mid_claim(self):
        conn, cur = _ctx_conn(fetchone=(False,))
        with patch.object(worker_pg, "_active_build_count") as count:
            assert worker_pg._claim_next_job(conn) is None
        count.assert_not_called()
        conn.rollback.assert_called_once()

    def test_dispatch_update_only_flips_a_queued_row(self):
        row = ("b-1", "build", "demo", {}, "c-1", 0, None, None, "c@example.test", "C")
        conn, cur = _ctx_conn(fetchone=row)
        with patch.object(worker_pg, "_active_build_count", return_value=0), \
             patch.object(worker_pg, "_get_daily_api_spend", return_value=0.0):
            worker_pg._claim_next_job(conn)
        update_sql = next(
            c[0][0] for c in cur.execute.call_args_list if "status = 'building'" in c[0][0]
        )
        assert "status = 'queued'" in update_sql
        select_sql = next(c[0][0] for c in cur.execute.call_args_list if "SKIP LOCKED" in c[0][0])
        assert "FOR UPDATE OF b SKIP LOCKED" in select_sql


class TestDailySpendCapFailsClosed:
    def test_query_failure_reports_infinite_spend(self):
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("relation missing")
        assert worker_pg._get_daily_api_spend(conn) == float("inf")
        conn.rollback.assert_called_once()

    def test_claim_pauses_when_spend_query_fails(self):
        conn, cur = _ctx_conn(fetchone=(True,))
        original_cap = worker_pg._DAILY_SPEND_CAP
        try:
            worker_pg._DAILY_SPEND_CAP = 50.0
            with patch.object(worker_pg, "_active_build_count", return_value=0), \
                 patch.object(worker_pg, "_get_daily_api_spend", return_value=float("inf")):
                assert worker_pg._claim_next_job(conn) is None
        finally:
            worker_pg._DAILY_SPEND_CAP = original_cap

    def test_cost_is_accumulated_across_retries(self):
        """The cap reads total_api_cost_usd, so each attempt must add to it."""
        conn, cur = _ctx_conn()
        job = {
            "attempt": ATTEMPT,
            "id": "build-acc", "app_name": "acc-app", "tier": "build", "spec": {},
            "customer_id": "c", "customer_email": "t@example.test", "customer_name": "T",
            "retry_count": 2,
        }
        with patch.object(worker_pg, "_run_pipeline", side_effect=RuntimeError("Connection refused by host")), \
             patch.object(worker_pg, "_requeue_for_retry"):
            worker_pg._process_job(job, conn)
        cost_sql = next(
            c[0][0] for c in cur.execute.call_args_list if "total_api_cost_usd" in str(c[0][0])
        )
        assert "COALESCE(total_api_cost_usd, 0) + %s" in cost_sql


class TestStatusTransitionGuards:
    """The worker may only move a build forward out of the states it owns."""

    def test_requeue_is_guarded_and_reports_no_match(self):
        conn, cur = _ctx_conn(rowcount=0)
        assert worker_pg._requeue_for_retry(conn, "b-1", 0, "boom", attempt=ATTEMPT) is False
        sql = cur.execute.call_args[0][0]
        assert "status IN ('building', 'testing', 'deploying')" in sql

    def test_requeue_returns_true_when_row_matched(self):
        conn, cur = _ctx_conn(rowcount=1)
        assert worker_pg._requeue_for_retry(conn, "b-1", 0, "boom", attempt=ATTEMPT) is True

    def test_fail_build_on_a_build_that_already_left_the_worker_does_nothing(self):
        conn, cur = _ctx_conn(rowcount=0)
        with patch.object(worker_pg, "send_build_failed") as email, \
             patch.object(worker_pg, "_cleanup_old_builds") as cleanup, \
             patch("deploy.destroy_project") as destroy:
            worker_pg._fail_build(conn, "b-1", "boom", "c@example.test", "C", "app", "", attempt=ATTEMPT)
        conn.rollback.assert_called_once()   # also discards the dead-letter row
        conn.commit.assert_not_called()
        email.assert_not_called()
        cleanup.assert_not_called()
        destroy.assert_not_called()

    def test_fail_build_status_update_is_guarded(self):
        conn, cur = _ctx_conn(fetchone=(None,))
        with patch.object(worker_pg, "send_build_failed"), \
             patch.object(worker_pg, "_cleanup_old_builds", return_value=0):
            worker_pg._fail_build(conn, "b-1", "boom", "c@example.test", "C", "app", "", attempt=ATTEMPT)
        update_sql = next(c[0][0] for c in cur.execute.call_args_list if "SET status = 'failed'" in c[0][0])
        assert update_sql == worker_pg._MARK_FAILED_SQL
        assert "status IN ('building', 'testing', 'deploying')" in update_sql
        # A queued build is waiting for its next attempt; a late failure from a
        # stale thread must not land on it.
        assert "'queued'" not in update_sql

    def test_failed_is_written_by_one_statement_only(self):
        """Every terminal path goes through _fail_build, so its guarded UPDATE
        is the only SQL in the worker that sets a build to 'failed'."""
        import re
        source = Path(worker_pg.__file__).read_text()
        assert len(re.findall(r"status\s*=\s*'failed'", source)) == 1
        assert "status = 'failed'" in worker_pg._MARK_FAILED_SQL

    def test_fail_build_survives_a_raising_mailer(self):
        conn, cur = _ctx_conn(fetchone=(None,))
        with patch.object(worker_pg, "send_build_failed", side_effect=RuntimeError("mail down")), \
             patch.object(worker_pg, "_cleanup_old_builds", return_value=0):
            worker_pg._fail_build(conn, "b-1", "boom", "c@example.test", "C", "app", "", attempt=ATTEMPT)

    def test_preview_updates_only_fire_for_active_builds(self):
        conn, cur = _ctx_conn(fetchone=None)
        worker_pg._set_preview_status(conn, "b-1", preview_url="https://x.example.test", attempt=ATTEMPT)
        for call_args in cur.execute.call_args_list:
            assert "status IN ('building', 'testing', 'deploying')" in call_args[0][0]

    def test_worker_source_never_writes_live(self):
        """Review gate: preview -> live is the web app's transition. Nothing in
        the worker may set it, directly or through _set_status."""
        import re
        source = Path(worker_pg.__file__).read_text()
        assert not re.search(r"status\s*=\s*'live'", source), "worker SQL must never set status = 'live'"
        assert not re.search(r"_set_status\([^)]*[\"']live[\"']", source), "worker must not call _set_status(..., 'live')"
        assert "live" not in worker_pg._SET_STATUS_ALLOWED

class TestOrphanRecoveryNeverStealsLiveBuilds:
    """Lease plumbing with mocks. Whether the candidate query really skips a
    leased build is tested against Postgres, with real advisory locks, in
    tests/test_worker_pg_realdb.py::TestOrphanRecovery."""

    def test_candidate_query_is_given_the_attempts_this_process_owns(self):
        conn, cursor = _orphan_conn([])
        worker_pg._own_attempt("own-build-1", ATTEMPT)
        try:
            worker_pg._recover_orphaned_builds(conn)
        finally:
            worker_pg._disown_attempt("own-build-1", ATTEMPT)
        sql, params = cursor.execute.call_args_list[0][0]
        assert sql == worker_pg._ORPHAN_SELECT_SQL
        assert params == (worker_pg._ORPHAN_MIN_AGE_S, worker_pg._LEASE_CLASS_ID,
                          ["own-build-1"], [ATTEMPT])

    def test_acquire_lease_takes_the_advisory_lock_on_a_dedicated_connection(self):
        lease_conn, cur = _ctx_conn(fetchone=(True,))
        with patch.object(worker_pg, "_get_conn", return_value=lease_conn):
            got = worker_pg._acquire_build_lease("build-9", ATTEMPT)
        assert got is lease_conn
        sql, params = cur.execute.call_args[0]
        assert sql.startswith("SELECT pg_try_advisory_lock(%s, hashtext(")
        assert params == (worker_pg._LEASE_CLASS_ID, "build-9", ATTEMPT)
        assert lease_conn.autocommit is True
        lease_conn.close.assert_not_called()

    def test_acquire_lease_returns_none_and_closes_when_lock_is_held_elsewhere(self):
        lease_conn, cur = _ctx_conn(fetchone=(False,))
        with patch.object(worker_pg, "_get_conn", return_value=lease_conn):
            assert worker_pg._acquire_build_lease("build-9", ATTEMPT) is None
        lease_conn.close.assert_called_once()

    def test_acquire_lease_never_raises(self):
        with patch.object(worker_pg, "_get_conn", side_effect=RuntimeError("db down")):
            assert worker_pg._acquire_build_lease("build-9", ATTEMPT) is None

    def test_a_lease_without_an_attempt_token_is_not_taken(self):
        lease_conn, cur = _ctx_conn(fetchone=(True,))
        with patch.object(worker_pg, "_get_conn", return_value=lease_conn):
            assert worker_pg._acquire_build_lease("build-9", None) is None
        cur.execute.assert_not_called()
        lease_conn.close.assert_called_once()

    def test_build_thread_holds_the_lease_for_the_whole_build_and_releases_it(self):
        events = []
        lease_conn = MagicMock()
        lease_conn.close.side_effect = lambda: events.append("lease released")
        job = {"attempt": ATTEMPT, "id": "build-lease-1", "app_name": "x", "tier": "build", "spec": {},
               "customer_id": "c", "customer_email": "e@example.test", "customer_name": "N"}

        def fake_process(job_arg, conn_arg):
            events.append("processing")
            assert "build-lease-1" in worker_pg._own_build_ids()

        with patch.object(worker_pg, "_acquire_build_lease") as acquire, \
             patch.object(worker_pg, "_get_conn", return_value=MagicMock()), \
             patch.object(worker_pg, "_process_job", side_effect=fake_process):
            worker_pg._run_build_in_thread(job, lease_conn)
        # The lease arrives with the job (the claiming thread took it); the
        # build thread keeps it for the whole build and releases it at the end.
        acquire.assert_not_called()
        assert events == ["processing", "lease released"]
        assert "build-lease-1" not in worker_pg._own_build_ids()

    def test_the_dispatcher_owns_and_leases_a_build_before_the_pool_gets_it(self):
        events = []
        lease_conn = MagicMock()
        job = {"attempt": ATTEMPT, "id": "build-lease-3", "app_name": "x", "tier": "build"}

        class Pool:
            def submit(self, fn, job_arg, lease_arg):
                events.append(("submitted", job_arg["id"] in worker_pg._own_build_ids(), lease_arg is lease_conn))
                return MagicMock()

        def acquire(build_id, attempt):
            events.append(("lease taken", (build_id, attempt) in worker_pg._own_attempts()))
            return lease_conn

        try:
            with patch.object(worker_pg, "_get_conn", return_value=MagicMock()), \
                 patch.object(worker_pg, "_claim_next_job", return_value=job), \
                 patch.object(worker_pg, "_acquire_build_lease", side_effect=acquire):
                assert worker_pg._claim_and_dispatch(Pool(), []) is job
            assert events == [("lease taken", True), ("submitted", True, True)]
        finally:
            worker_pg._disown_attempt("build-lease-3", ATTEMPT)

    def test_lease_is_released_even_when_the_build_crashes(self):
        lease_conn = MagicMock()
        job = {"attempt": ATTEMPT, "id": "build-lease-2", "app_name": "x", "tier": "build", "spec": {},
               "customer_id": "c", "customer_email": "e@example.test", "customer_name": "N"}
        with patch.object(worker_pg, "_get_conn", return_value=MagicMock()), \
             patch.object(worker_pg, "_process_job", side_effect=RuntimeError("boom")), \
             patch.object(worker_pg, "_fail_build"), \
             patch.object(worker_pg, "_set_status"):
            worker_pg._run_build_in_thread(job, lease_conn)
        lease_conn.close.assert_called_once()
        assert "build-lease-2" not in worker_pg._own_build_ids()

    def test_recover_orphans_once_never_raises_and_closes_its_connection(self):
        conn = MagicMock()
        with patch.object(worker_pg, "_get_conn", return_value=conn), \
             patch.object(worker_pg, "_recover_orphaned_builds", side_effect=RuntimeError("db blip")):
            assert worker_pg._recover_orphans_once("periodic") == 0
        conn.close.assert_called_once()

    def test_sigterm_handler_is_not_installed_at_import_time(self):
        """Importing the module must not change the host process's signals;
        run_worker() installs the handler on the main thread."""
        import inspect
        module_level = [
            line for line in inspect.getsource(worker_pg).splitlines()
            if line.startswith("signal.signal(")
        ]
        assert module_level == []
        assert "signal.signal(signal.SIGTERM, _handle_sigterm)" in inspect.getsource(worker_pg.run_worker)


class TestSecretRedaction:
    # Secret-shaped values are assembled at runtime so no source line looks like a credential.
    DB_URL = "post" + "gresql://app:" + "pw-" + "value" + "@db.internal:5432/app"
    API_KEY = "sk" + "-" + "ant" + "-api03-" + "A" * 30
    TOKEN = "ab" * 32

    def test_url_credentials_are_masked(self):
        out = worker_pg._redact_secrets(f"connect failed {self.DB_URL} retrying")
        assert "pw-value" not in out
        assert "db.internal:5432/app" in out

    def test_bearer_tokens_key_prefixes_and_named_secrets_are_masked(self):
        text = (
            "Authorization: Bear" + "er " + self.API_KEY + "\n"
            + "API_KEY=" + "abc123" + " SECRET_TOKEN: " + "zzz999" + "\n"
            + "GET /magic?token=" + self.TOKEN + " 200\n"
            + "plain " + self.API_KEY
        )
        out = worker_pg._redact_secrets(text)
        for leaked in (self.API_KEY, "abc123", "zzz999", self.TOKEN):
            assert leaked not in out

    def test_ordinary_log_lines_are_untouched(self):
        for line in ("listening on port 3000", "npm ERR! code ENOENT",
                     "Expected 401/403 or redirect, got 200 at /dashboard"):
            assert worker_pg._redact_secrets(line) == line

    def test_none_becomes_empty_string(self):
        assert worker_pg._redact_secrets(None) == ""

    def test_deploy_logs_are_redacted_before_they_reach_build_logs(self):
        """build_logs rows are served to the build's owner, and these lines are
        the customer app's own stdout."""
        with patch.object(worker_pg, "_append_log") as append:
            worker_pg._persist_deploy_logs(
                MagicMock(), "b-1", [f"db error for {self.DB_URL}", "token=" + self.TOKEN]
            )
        stored = " ".join(c[0][3] for c in append.call_args_list)
        assert "pw-value" not in stored and self.TOKEN not in stored

    def test_stored_error_messages_are_redacted(self):
        conn, cur = _ctx_conn(rowcount=1)
        worker_pg._requeue_for_retry(conn, "b-1", 0, f"failed: {self.DB_URL}", attempt=ATTEMPT)
        assert "pw-value" not in cur.execute.call_args[0][1][1]
        conn, cur = _ctx_conn(fetchone=(None,))
        with patch.object(worker_pg, "send_build_failed"), \
             patch.object(worker_pg, "_cleanup_old_builds", return_value=0):
            worker_pg._fail_build(conn, "b-1", f"failed: {self.DB_URL}", "c@example.test", "C", "app", "", attempt=ATTEMPT)
        assert all("pw-value" not in str(c[0][1]) for c in cur.execute.call_args_list if len(c[0]) > 1)

    def test_process_job_redacts_the_exception_text_it_stores(self):
        conn, cur = _ctx_conn()
        job = {"attempt": ATTEMPT, "id": "b-red", "app_name": "red-app", "tier": "build", "spec": {},
               "customer_id": "c", "customer_email": "e@example.test", "customer_name": "N",
               "retry_count": 0}
        with patch.object(worker_pg, "_run_pipeline", side_effect=RuntimeError(f"deploy said {self.DB_URL}")), \
             patch.object(worker_pg, "_requeue_for_retry") as requeue:
            worker_pg._process_job(job, conn)
        assert "pw-value" not in requeue.call_args[0][3]


class TestBuildPathSafety:
    @pytest.mark.parametrize("name", ["..", ".", "", "../x", "a/b", "a\\b", ".hidden", "x..y", None, 7,
                                      "my-app\n", "my-app\r", "\nmy-app"])
    def test_unsafe_names_are_rejected(self, name):
        assert worker_pg._is_safe_dir_name(name) is False
        assert worker_pg._safe_build_path(name) is None

    @pytest.mark.parametrize("name", ["my-app", "app_1", "site.v2", "A1"])
    def test_plain_names_are_accepted(self, name):
        assert worker_pg._safe_build_path(name) == worker_pg._BUILDS_ROOT / name

    def test_process_job_fails_an_unsafe_app_name_before_touching_disk(self):
        conn = MagicMock()
        job = {"attempt": ATTEMPT, "id": "b-evil", "app_name": "../../etc", "tier": "build", "spec": {},
               "customer_id": "c", "customer_email": "e@example.test", "customer_name": "N",
               "retry_count": 0}
        with patch.object(worker_pg, "_run_pipeline") as pipeline, \
             patch.object(worker_pg, "_fail_build") as fail, \
             patch.object(worker_pg.shutil, "rmtree") as rmtree:
            worker_pg._process_job(job, conn)
        pipeline.assert_not_called()
        rmtree.assert_not_called()
        fail.assert_called_once()
        assert "Invalid app_name" in fail.call_args[0][2]

    def test_cleanup_never_removes_directories_outside_the_builds_root(self, tmp_path, monkeypatch):
        builds = tmp_path / "builds"
        builds.mkdir()
        victim = tmp_path / "keep-me"
        victim.mkdir()
        (victim / "data.txt").write_text("precious")
        monkeypatch.setattr(worker_pg, "_BUILDS_ROOT", builds)
        conn, cur = _ctx_conn()
        cur.fetchone.side_effect = [(True,)]
        cur.fetchall.return_value = [("11111111-0000-0000-0000-000000000000", None)]
        # An app_name that, joined naively, points at the sibling directory.
        worker_pg._cleanup_old_builds(conn, "b-1", "../keep-me")
        assert (victim / "data.txt").read_text() == "precious"


class TestCleanupOldBuildsScope:
    """Statement shape, with a mocked cursor. What the statements do to real
    rows (lineage, hand-off stamps, a concurrent retry, another customer's
    builds) is in tests/test_worker_lifecycle_realdb.py."""

    STALE = "11111111-0000-0000-0000-000000000000"

    def _run(self):
        conn, cur = _ctx_conn()
        cur.fetchone.side_effect = [(True,)]
        # candidates, locked rows, parents of surviving children, deleted rows
        cur.fetchall.side_effect = [[(self.STALE,)], [(self.STALE,)], [], [(self.STALE,)]]
        result = worker_pg._cleanup_old_builds(conn, "b-new", "bakery")
        return result, cur

    def test_selection_is_limited_to_same_customer_and_failed_builds(self):
        result, cur = self._run()
        select_sql, select_params = next(
            c[0] for c in cur.execute.call_args_list if "ROW_NUMBER()" in c[0][0]
        )
        assert "customer_id IS NOT DISTINCT FROM" in select_sql
        assert "status IN ('failed', 'abandoned')" in select_sql
        assert select_params == ("bakery", "b-new")
        assert result == 1

    def test_the_delete_itself_names_the_status(self):
        """The ranking query is a read; the row can change before the write.
        The DELETE used to name the build by id alone."""
        _result, cur = self._run()
        delete_sql = next(c[0][0] for c in cur.execute.call_args_list
                          if c[0][0].lstrip().startswith("DELETE FROM builds"))
        assert "status IN ('failed', 'abandoned')" in delete_sql
        lock_sql = next(c[0][0] for c in cur.execute.call_args_list if "FOR UPDATE" in c[0][0])
        assert "SKIP LOCKED" in lock_sql and "status IN ('failed', 'abandoned')" in lock_sql

    def test_no_surviving_row_has_its_parent_rewritten(self):
        _result, cur = self._run()
        assert not any("SET parent_build_id" in c[0][0] for c in cur.execute.call_args_list)

    def test_every_uuid_comparison_is_cast(self):
        """Without ::uuid[] Postgres raises 'operator does not exist: uuid = text'
        and the (swallowed) failure meant cleanup never ran."""
        result, cur = self._run()
        statements = [c[0][0] for c in cur.execute.call_args_list if "ANY(%s" in c[0][0]]
        assert len(statements) >= 6
        for sql in statements:
            assert sql.count("ANY(%s") == sql.count("ANY(%s::uuid[])"), sql


class TestArtifactRestoreContainment:
    def _restore(self, tmp_path, members):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for info, payload in members:
                tar.addfile(info, io.BytesIO(payload) if payload is not None else None)
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.return_value = (memoryview(buf.getvalue()),)
        conn.cursor.return_value = cursor
        dest = tmp_path / "builds"
        dest.mkdir()
        # Python 3.14 filters archives by default; older runtimes (the worker
        # image ships an older Python) extract everything. Emulate the
        # permissive default so the test exercises the worker's own checks.
        permissive = getattr(tarfile, "fully_trusted_filter", None)
        filter_patch = (
            patch.object(tarfile.TarFile, "extraction_filter", staticmethod(permissive), create=True)
            if permissive else patch.object(worker_pg, "logger", worker_pg.logger)
        )
        with filter_patch, patch.object(worker_pg, "_BUILDS_ROOT", dest):
            return worker_pg._restore_build_artifact(conn, "build-1", "app")

    def _file(self, name, data=b"x"):
        import tarfile
        info = tarfile.TarInfo(name)
        info.size = len(data)
        return info, data

    def test_sibling_directory_with_the_same_prefix_is_not_written(self, tmp_path):
        """'builds-evil' starts with the string 'builds'; a prefix test accepts it."""
        self._restore(tmp_path, [self._file("app/ok.txt"), self._file("../builds-evil/pwn.txt")])
        assert (tmp_path / "builds" / "app" / "ok.txt").exists()
        assert not (tmp_path / "builds-evil" / "pwn.txt").exists()

    def test_parent_traversal_is_not_written(self, tmp_path):
        self._restore(tmp_path, [self._file("../escape.txt")])
        assert not (tmp_path / "escape.txt").exists()

    def test_symlink_members_are_refused(self, tmp_path):
        import tarfile
        link = tarfile.TarInfo("app/link")
        link.type = tarfile.SYMTYPE
        link.linkname = str(tmp_path)
        self._restore(tmp_path, [(link, None), self._file("app/ok.txt")])
        assert not (tmp_path / "builds" / "app" / "link").exists()
        assert (tmp_path / "builds" / "app" / "ok.txt").exists()


class TestMagicLinkSeedInjectionIsLiteralSafe:
    def test_hash_with_sql_metacharacters_is_refused(self, tmp_path):
        (tmp_path / "db").mkdir()
        seed = tmp_path / "db" / "seed.sql"
        seed.write_text("-- seed\n")
        with pytest.raises(ValueError):
            worker_pg._inject_magic_link_token(str(tmp_path), "x'); DROP TABLE users; --")
        with pytest.raises(ValueError):
            worker_pg._inject_magic_link_token(str(tmp_path), "okhash", customer_token_hash="a b")
        assert seed.read_text() == "-- seed\n"


class TestAttemptProjectIsTrackedInTheAttemptState:
    def test_failed_health_check_leaves_the_project_in_the_attempt_state_not_on_the_row(self, monkeypatch):
        """The project a failed-after-deploy attempt created must be known to
        whoever cleans up after the attempt, or it leaks. It is known through
        the attempt's own state. It is NOT written to the build row: the row's
        project is the deployment the customer was last told about, and
        overwriting it here is what made the failure path delete the wrong
        project."""
        monkeypatch.setenv("RAILWAY_API_TOKEN", "dummy-token-for-test")
        monkeypatch.setenv("CAS_BUILD_ENGINE", "v2")
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (False,)  # no stored artifact -> no resume
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        job = {
            "attempt": ATTEMPT,
            "id": "abcd1234-0000-0000-0000-000000000000", "app_name": "track-app", "tier": "build",
            "spec": {"app_name": "track-app", "archetype": "cafe"},
            "customer_id": "c", "customer_email": "e@example.test", "customer_name": "N",
        }
        deploy_result = {
            "live_url": "https://track-app.example.test", "health_check": "failed",
            "railway_project_id": "proj-1", "railway_service_id": "svc-1",
            "railway_environment_id": "env-1",
        }
        with patch("compose.build.BuildSpec.from_dict", return_value=MagicMock()), \
             patch("compose.build.validate_spec", return_value=[]), \
             patch("compose.build.build_app"), \
             patch.object(worker_pg, "_lint_v2_build_output", return_value={"checks": [], "errors": []}), \
             patch.object(worker_pg, "_store_build_artifact"), \
             patch.object(worker_pg, "_append_log"), \
             patch.object(worker_pg, "_set_status"), \
             patch.object(worker_pg, "_ensure_conn", side_effect=lambda c: c), \
             patch.object(worker_pg, "send_build_progress"), \
             patch.object(worker_pg, "_persist_deploy_logs"), \
             patch.object(worker_pg.time, "sleep"), \
             patch("deploy.deploy", return_value=deploy_result), \
             patch("deploy.verify_health", return_value="failed"), \
             patch("deploy.get_latest_deployment", return_value="dep-1"), \
             patch("deploy.get_deployment_logs", return_value=["boom"] * 40):
            with pytest.raises(RuntimeError, match="v2-deterministic"):
                worker_pg._run_pipeline(job, conn)
        recorded = worker_pg._attempt_state(job)["railway"]
        assert (recorded["project_id"], recorded["service_id"], recorded["environment_id"]) == (
            "proj-1", "svc-1", "env-1")
        assert worker_pg._attempt_state(job)["announced"] is False
        assert not any("railway_project_id" in str(c[0][0]) and "UPDATE" in str(c[0][0])
                       for c in cursor.execute.call_args_list)
        with worker_pg._UNRECORDED_ATTEMPT_PROJECT_IDS_LOCK:
            worker_pg._UNRECORDED_ATTEMPT_PROJECT_IDS.discard("proj-1")


class TestV2DeployIsNotProbedWithAPassword:
    def test_v2_pipeline_calls_deploy_without_the_password_login_probe(self, monkeypatch):
        """A v2 app has no seeded password. The worker must tell deploy() not
        to post the v1 default password at it: with the probe on, deploy()
        reports a healthy v2 app as health_check="failed", and the worker has
        to re-probe /health to get past its own fail-fast gate."""
        monkeypatch.setenv("RAILWAY_API_TOKEN", "dummy-token-for-test")
        monkeypatch.setenv("CAS_BUILD_ENGINE", "v2")
        conn, cursor = _ctx_conn(fetchone=(False,))  # no stored artifact -> no resume
        job = {
            "attempt": ATTEMPT,
            "id": "abcd1234-0000-0000-0000-000000000000", "app_name": "track-app", "tier": "build",
            "spec": {"app_name": "track-app", "archetype": "cafe"},
            "customer_id": "c", "customer_email": "e@example.test", "customer_name": "N",
        }
        deploy_result = {
            "live_url": "https://track-app.example.test", "health_check": "ok",
            "railway_project_id": "proj-1", "railway_service_id": "svc-1",
            "railway_environment_id": "env-1",
        }
        with patch("compose.build.BuildSpec.from_dict", return_value=MagicMock()), \
             patch("compose.build.validate_spec", return_value=[]), \
             patch("compose.build.build_app"), \
             patch.object(worker_pg, "_lint_v2_build_output", return_value={"checks": [], "errors": []}), \
             patch.object(worker_pg, "_store_build_artifact"), \
             patch.object(worker_pg, "_append_log"), \
             patch.object(worker_pg, "_set_status", return_value=True), \
             patch.object(worker_pg, "_ensure_conn", side_effect=lambda c: c), \
             patch.object(worker_pg, "send_build_progress"), \
             patch("deploy.deploy", return_value=deploy_result) as deploy_call, \
             patch("deploy.verify_health") as recovery_probe, \
             patch("deploy.capture_deploy_screenshots", return_value=[]), \
             patch("deploy.verify_render", return_value={"status": "ok", "screenshots": []}):
            result = worker_pg._run_pipeline(job, conn)
        deploy_call.assert_called_once()
        assert deploy_call.call_args.kwargs == {"password_login_probe": False}
        recovery_probe.assert_not_called()
        assert result["live_url"] == "https://track-app.example.test"


class TestPreviewEmailCannotSinkAPreview:
    def test_send_preview_ready_failure_does_not_requeue_or_fail_the_build(self):
        conn = MagicMock()
        job = {"attempt": ATTEMPT, "id": "b-pv", "app_name": "pv-app", "tier": "build", "spec": {},
               "customer_id": "c", "customer_email": "e@example.test", "customer_name": "N",
               "retry_count": 0}
        with patch.object(worker_pg, "_run_pipeline", return_value={"live_url": "https://pv.example.test"}), \
             patch.object(worker_pg, "_set_preview_status", return_value=True), \
             patch.object(worker_pg, "send_review_pending"), \
             patch.object(worker_pg, "_cleanup_old_builds", return_value=0), \
             patch.object(worker_pg, "send_preview_ready", side_effect=RuntimeError("template bug")), \
             patch.object(worker_pg, "_requeue_for_retry") as requeue, \
             patch.object(worker_pg, "_fail_build") as fail:
            worker_pg._process_job(job, conn)
        requeue.assert_not_called()
        fail.assert_not_called()


class TestZombiePreviewKeepsReviewRouting:
    def test_initial_review_status_follows_the_auto_review_switch(self, monkeypatch):
        """CAS_HARBOR_AUTO_REVIEW is an arming switch: only an explicit ON word
        selects 'ai_review_queued', a state nothing in this repository
        consumes; unset, blank, an OFF word or a typo means 'pending'."""
        for on_word in ("true", "True", "1", "yes", "on"):
            monkeypatch.setenv("CAS_HARBOR_AUTO_REVIEW", on_word)
            assert worker_pg._initial_review_status() == "ai_review_queued"
        for other in ("false", "0", "", "ture"):
            monkeypatch.setenv("CAS_HARBOR_AUTO_REVIEW", other)
            assert worker_pg._initial_review_status() == "pending"
        monkeypatch.delenv("CAS_HARBOR_AUTO_REVIEW", raising=False)
        assert worker_pg._initial_review_status() == "pending"

    @patch.object(worker_pg, "sentry_sdk", MagicMock())
    def test_supervisor_preview_goes_through_the_one_preview_writer(self):
        """The zombie-deploy path must not have its own copy of the review
        routing: it calls _set_preview_status, like every arrival at preview."""
        conn, cursor = _ctx_conn(fetchone=("deploying", 0, False, "zombie-app", ATTEMPT, None))
        probe_result = {"live_url": "https://zombie.example.test", "project_id": "p", "service_id": "s"}
        with patch.object(worker_pg, "_get_conn", return_value=conn), \
             patch.object(worker_pg, "_supervisor_reality_probe", return_value=probe_result), \
             patch.object(worker_pg, "_set_preview_status", return_value=True) as set_preview:
            result = worker_pg._supervisor_force_fail("build-zombie", "build", 60, attempt=ATTEMPT)
        assert result == (True, "preview", probe_result)
        set_preview.assert_called_once_with(
            conn, "build-zombie", preview_url="https://zombie.example.test", attempt=ATTEMPT,
            railway=worker_pg._railway_record("p", "s", live_url="https://zombie.example.test"))
        other_sql = " ".join(str(c[0][0]) for c in cursor.execute.call_args_list)
        assert "review_status" not in other_sql and "SET status" not in other_sql
        # The probed project is recorded by the preview writer, nowhere else.
        assert "railway_project_id =" not in other_sql.replace("railway_project_id = %s AND", "")


class TestMagicLinkNotOfferedWithoutASeededHash:
    def test_inject_reports_whether_anything_was_written(self, tmp_path):
        assert worker_pg._inject_magic_link_token(str(tmp_path), "abc123hash") is False
        (tmp_path / "db").mkdir()
        (tmp_path / "db" / "seed.sql").write_text("-- seed\n")
        assert worker_pg._inject_magic_link_token(str(tmp_path), "abc123hash") is True

    def test_pipeline_source_only_keeps_raw_tokens_after_a_successful_injection(self):
        import inspect
        source = inspect.getsource(worker_pg._run_pipeline)
        marker = "injected = _inject_magic_link_token("
        assert marker in source
        tail = source[source.index(marker):]
        assert tail.index("if injected:") < tail.index("customer_token = customer_pair")


class TestSeedLintTreatsASpecKeyAsText:
    def _site(self, tmp_path, seed_content, seed_sql):
        import json
        (tmp_path / "db").mkdir()
        (tmp_path / "spec.json").write_text(json.dumps({"seed_content": seed_content}))
        (tmp_path / "db" / "seed.sql").write_text(seed_sql)
        return worker_pg._lint_v2_build_output(tmp_path)

    def test_a_key_that_is_not_a_valid_pattern_does_not_end_the_comparison(self, tmp_path):
        """The table name in the INSERT pattern comes from a spec key. Compiled
        as part of the pattern, "menu(" raised re.error, the handler recorded
        one generic failure, and no later model was compared at all."""
        result = self._site(
            tmp_path,
            {"menu(": [{"name": "x"}], "events": [{"t": 1}, {"t": 2}]},
            "INSERT INTO events (t) VALUES (1);\nINSERT INTO events (t) VALUES (2);\n",
        )
        assert not any("comparison failed" in error for error in result["errors"]), result["errors"]
        assert "Seed for events: 2 rows (spec wants 2)" in result["checks"]
        assert any("0 INSERTs for menu(" in error for error in result["errors"])

    def test_a_dot_in_a_key_matches_a_dot_only(self, tmp_path):
        """As a pattern "a.b" also matched the table axb, so a model with no
        seed rows was reported as seeded."""
        result = self._site(tmp_path, {"a.b": [{"n": 1}]}, "INSERT INTO axb (n) VALUES (1);\n")
        assert any("0 INSERTs for a.b" in error for error in result["errors"]), result
        assert not any(check.startswith("Seed for a.b") for check in result["checks"])

    def test_a_longer_table_name_is_not_counted_for_a_shorter_one(self, tmp_path):
        result = self._site(tmp_path, {"event": [{"n": 1}]}, "INSERT INTO events (n) VALUES (1);\n")
        assert any("0 INSERTs for event," in error for error in result["errors"]), result


class TestTokenHashLiteralHasNoTrailingNewline:
    def test_a_hash_followed_by_a_newline_is_refused(self, tmp_path):
        """match() with a `$` accepts "hash\\n"; the value goes into a SQL
        string literal in seed.sql."""
        (tmp_path / "db").mkdir()
        seed = tmp_path / "db" / "seed.sql"
        seed.write_text("-- seed\n")
        with pytest.raises(ValueError):
            worker_pg._inject_magic_link_token(str(tmp_path), "abc123\n")
        with pytest.raises(ValueError):
            worker_pg._inject_magic_link_token(str(tmp_path), "abc123", customer_token_hash="def456\n")
        assert seed.read_text() == "-- seed\n"


class TestTheCoordinatorLoopInterval:
    """COORDINATOR_INTERVAL_S is written by the admin settings page. The
    worker read it with a bare int(): "1800.5" stopped the worker from
    starting, and "0" made the loop go round without sleeping."""

    class _Event:
        """Stands in for the shutdown event: records the wait, then stops the loop."""

        def __init__(self):
            self.waits = []
            self._set = False

        def is_set(self):
            return self._set

        def set(self):
            self._set = True

        def clear(self):
            self._set = False

        def wait(self, seconds):
            self.waits.append(seconds)
            self._set = True

    def _seconds_between_ticks(self, monkeypatch, raw):
        from coordinator import tick

        if raw is None:
            monkeypatch.delenv("COORDINATOR_INTERVAL_S", raising=False)
        else:
            monkeypatch.setenv("COORDINATOR_INTERVAL_S", raw)
        started = {}

        class _Thread:
            def __init__(self, target=None, **_kwargs):
                started["target"] = target

            def start(self):
                pass

        event = self._Event()
        registry_before = dict(tick._agent_registry)
        try:
            with patch.object(worker_pg.threading, "Thread", _Thread), \
                    patch.object(worker_pg, "_shutdown_event", event), \
                    patch("coordinator.tick.run_tick", lambda **_kwargs: {}), \
                    patch.object(worker_pg, "_get_conn", return_value=MagicMock()):
                worker_pg._start_coordinator_thread()
                started["target"]()
        finally:
            tick._agent_registry.clear()
            tick._agent_registry.update(registry_before)
        return event.waits

    @pytest.mark.parametrize("raw", ["1800.5", "0", "-5", "soon", "", "1e3"])
    def test_text_that_is_not_a_whole_number_of_seconds_means_the_default(self, monkeypatch, raw):
        assert self._seconds_between_ticks(monkeypatch, raw) == [1800]

    def test_an_ordinary_value_and_no_value(self, monkeypatch):
        assert self._seconds_between_ticks(monkeypatch, "600") == [600]
        assert self._seconds_between_ticks(monkeypatch, None) == [1800]

    def test_the_loop_and_the_tick_read_the_interval_through_one_function(self, monkeypatch):
        from coordinator import config

        for raw in ("300", "7200", "0", "1800.5"):
            waits = self._seconds_between_ticks(monkeypatch, raw)
            assert waits == [config.tick_interval_s()], raw
