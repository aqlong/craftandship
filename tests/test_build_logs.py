# tests/test_build_logs.py
# Tests for the _append_log helper in worker_pg.py

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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

import worker_pg  # noqa: E402


class TestAppendLog:
    """Tests for _append_log: fire-and-forget build log insertion."""

    def test_inserts_log_row(self):
        """_append_log executes INSERT and commits."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        worker_pg._append_log(mock_conn, "build-123", "building", "Generating code")

        mock_cur.execute.assert_called_once()
        args = mock_cur.execute.call_args
        assert "INSERT INTO build_logs" in args[0][0]
        assert args[0][1] == ("build-123", "building", "Generating code")
        mock_conn.commit.assert_called_once()

    def test_does_not_raise_on_db_error(self):
        """_append_log swallows exceptions (fire-and-forget)."""
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = Exception("DB connection lost")

        # Should not raise
        worker_pg._append_log(mock_conn, "build-456", "testing", "Running tests")

    def test_logs_warning_on_failure(self):
        """_append_log logs warnings when it fails (initial + retry)."""
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = Exception("DB gone")

        with patch.object(worker_pg, "logger") as mock_logger:
            worker_pg._append_log(mock_conn, "build-789", "deploying", "Deploy started")
            # First warning for stale conn, second for fresh conn retry failure
            assert mock_logger.warning.call_count >= 1
            # Final warning mentions the build_id
            last_call = mock_logger.warning.call_args_list[-1]
            assert "build-789" in str(last_call)


class TestPersistDeployLogs:
    """_persist_deploy_logs, DEPLOY_CRASH1 log capture.

    Audit-driven (2026-04-14): an MW v8 build's attempt 2 and task-tracker
    both hit deploy_fix × 3 with no recoverable Railway logs in build_logs. This
    helper persists the raw lines to the DB so terminal crashes are debuggable
    after Railway auto-cleanup destroys the project.
    """

    def test_empty_logs_is_noop(self):
        """Empty/None input does NOT write a header or any rows."""
        mock_conn = MagicMock()
        with patch.object(worker_pg, "_append_log") as mock_append:
            worker_pg._persist_deploy_logs(mock_conn, "b-1", [])
            worker_pg._persist_deploy_logs(mock_conn, "b-1", None)
        mock_append.assert_not_called()

    def test_persists_each_line_as_build_log_row(self):
        """Header + one _append_log call per log line."""
        mock_conn = MagicMock()
        logs = ["Starting server", "Error: port 3000 in use", "Process exited 1"]
        with patch.object(worker_pg, "_append_log") as mock_append:
            worker_pg._persist_deploy_logs(
                mock_conn, "b-2", logs, reason="health-check-failed",
            )
        # 1 header + 3 lines = 4 calls
        assert mock_append.call_count == 4
        # Header first
        first = mock_append.call_args_list[0]
        assert first[0][2] == "deploy-check"
        assert "Captured 3" in first[0][3]
        assert "health-check-failed" in first[0][3]
        # Each subsequent call carries a log line
        messages = [c[0][3] for c in mock_append.call_args_list[1:]]
        assert messages == logs

    def test_header_omits_reason_when_blank(self):
        mock_conn = MagicMock()
        with patch.object(worker_pg, "_append_log") as mock_append:
            worker_pg._persist_deploy_logs(mock_conn, "b-3", ["one"])
        header = mock_append.call_args_list[0][0][3]
        assert "Captured 1" in header
        assert "reason" not in header

    def test_caps_lines_at_max_lines(self):
        """When len(logs) > max_lines, only the TAIL is persisted."""
        mock_conn = MagicMock()
        # 150 lines, cap at 100 → keep the last 100
        logs = [f"line {i}" for i in range(150)]
        with patch.object(worker_pg, "_append_log") as mock_append:
            worker_pg._persist_deploy_logs(
                mock_conn, "b-4", logs, max_lines=100,
            )
        # 1 header + 100 lines
        assert mock_append.call_count == 101
        # First line persisted should be "line 50" (tail of 100 from 150)
        first_line = mock_append.call_args_list[1][0][3]
        assert first_line == "line 50"
        # Header advertises 100
        assert "Captured 100" in mock_append.call_args_list[0][0][3]

    def test_truncates_long_lines_at_max_chars(self):
        mock_conn = MagicMock()
        long_line = "x" * 5000
        with patch.object(worker_pg, "_append_log") as mock_append:
            worker_pg._persist_deploy_logs(
                mock_conn, "b-5", [long_line], max_line_chars=2000,
            )
        persisted = mock_append.call_args_list[1][0][3]
        assert persisted.endswith("…(truncated)")
        assert len(persisted) <= 2000 + len("…(truncated)")

    def test_skips_empty_or_whitespace_lines(self):
        """None / empty / whitespace-only lines are dropped from the batch."""
        mock_conn = MagicMock()
        logs = ["real line", None, "", "   ", "\n", "another real line"]
        with patch.object(worker_pg, "_append_log") as mock_append:
            worker_pg._persist_deploy_logs(mock_conn, "b-6", logs)
        # Header + 2 real lines
        assert mock_append.call_count == 3
        # Header still reports the original tail length (6), not the filtered 2.
        # This is intentional: "we received 6 lines, 4 were empty" is useful
        # diagnostic info. The caller cares about the raw input size.
        header = mock_append.call_args_list[0][0][3]
        assert "Captured 6" in header
        messages = [c[0][3] for c in mock_append.call_args_list[1:]]
        assert messages == ["real line", "another real line"]

    def test_swallows_db_errors(self):
        """Exceptions from _append_log do NOT propagate (fire-and-forget)."""
        mock_conn = MagicMock()
        with patch.object(worker_pg, "_append_log", side_effect=Exception("boom")):
            with patch.object(worker_pg, "logger") as mock_logger:
                # Must not raise
                worker_pg._persist_deploy_logs(mock_conn, "b-7", ["any line"])
                # Warning is logged for diagnosis
                assert mock_logger.warning.call_count == 1
                assert "b-7" in str(mock_logger.warning.call_args)

    def test_tuple_input_accepted(self):
        """Accepts any iterable, not just lists (deploy logs return list but
        belt-and-suspenders for defensive callers)."""
        mock_conn = MagicMock()
        with patch.object(worker_pg, "_append_log") as mock_append:
            worker_pg._persist_deploy_logs(mock_conn, "b-8", ("l1", "l2"))
        # Header + 2 lines
        assert mock_append.call_count == 3


class TestDeployLogCaptureCallSites:
    """Static regression guard: both call sites in worker_pg.py must actually
    invoke _persist_deploy_logs. Without this, someone removing the persist
    call (e.g. during a refactor) would silently regress DEPLOY_CRASH1
    debuggability, it's a best-effort write with no failure signal.
    """

    def test_pre_fix_health_failure_persists_logs(self):
        """The pre-deploy-fix DB-crash probe site must persist logs."""
        source = (ROOT / "queue" / "worker_pg.py").read_text()
        # The pre-check block fetches _pre_dep_logs then should persist them.
        # Search for both the fetch and the persist with the expected reason.
        assert "_pre_dep_logs = get_deployment_logs(" in source
        assert 'reason="health-check-failed"' in source
        # Ensure the persist call is after the fetch in the same function.
        fetch_idx = source.index("_pre_dep_logs = get_deployment_logs(")
        persist_idx = source.index('reason="health-check-failed"')
        assert persist_idx > fetch_idx

    def test_deploy_fix_loop_persists_logs_per_iteration(self):
        """Each deploy_fix iteration must persist that iteration's logs."""
        source = (ROOT / "queue" / "worker_pg.py").read_text()
        assert 'reason=f"deploy-fix-iteration-' in source
        # Iteration counter must be the human-friendly 1-based form so the
        # customer log reads "iteration 1/2/3" not "0/1/2".
        assert "deploy_fix_attempt + 1" in source

    def test_worker_uses_limit_100_for_crash_debugging(self):
        """Worker must request 100 lines (not the default 20) for crash debug."""
        source = (ROOT / "queue" / "worker_pg.py").read_text()
        # Both call sites in worker_pg.py bump limit to 100.
        assert source.count("limit=100") >= 2
