# tests/test_ops_scanner.py - Tests for ops agent failure scanner

import sys
from pathlib import Path
from unittest.mock import MagicMock
from datetime import datetime, timezone

_ROOT = str(Path(__file__).parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ops.scanner import classify_error, _normalize_error, scan_recent_failures


# ---------------------------------------------------------------------------
# classify_error()
# ---------------------------------------------------------------------------

class TestClassifyError:
    def test_playwright_version_mismatch(self):
        msg = "BrowserType.launch: Executable doesn't exist at /ms-playwright/chromium"
        assert classify_error(msg) == "version_mismatch"

    def test_please_update_docker_image(self):
        msg = "Looks like Playwright was just updated to 1.58.0. Please update docker image as well."
        assert classify_error(msg) == "version_mismatch"

    def test_node_version_mismatch(self):
        assert classify_error("error: engine incompatible with node 16") == "version_mismatch"

    def test_missing_module(self):
        assert classify_error("Cannot find module 'express'") == "missing_dep"

    def test_no_module_named(self):
        assert classify_error("No module named 'playwright'") == "missing_dep"

    def test_dockerfile_error(self):
        assert classify_error("apt-get install failed with exit code 100") == "dockerfile"

    def test_database_url_not_set(self):
        assert classify_error("DATABASE_URL is not set") == "env_config"

    def test_connection_refused_5432(self):
        assert classify_error("connection refused on port 5432") == "env_config"

    def test_oom_killed(self):
        assert classify_error("Process killed by signal 9 (OOM)") == "railway_crash"

    def test_real_crash_wordings_are_still_railway_crash(self):
        for message in (
            "container exited: OOM",
            "OOMKilled",
            "oom-kill event in the worker cgroup",
            "npm install was killed by signal SIGKILL",
            "the build process was killed",
            "process killed",
            "running npm ci\nKilled\n",
            "write failed: ENOSPC",
        ):
            assert classify_error(message) == "railway_crash", message

    def test_words_that_merely_contain_oom_or_killed_do_not_redeploy_the_worker(self):
        """railway_crash is auto-fixable and its playbook redeploys the worker
        service, so an ordinary app error must not land in it."""
        for message, expected in (
            ("Test suite failed; GET /rooms returned 500", "app_logic"),
            ("TypeError: cannot read 'zoom' of undefined", "app_logic"),
            ("seed row 'Showroom tour' rejected by the database", "unknown"),
            ("page 'Skilled trades' is missing a heading", "unknown"),
            ("Force-killed by admin", "unknown"),
            ("the mushroom page did not render", "unknown"),
        ):
            assert classify_error(message) == expected, message

    def test_errno2_missing_tool(self):
        assert classify_error("[Errno 2] No such file or directory: 'dropdb'") == "missing_tool"

    def test_command_not_found(self):
        assert classify_error("bash: createdb: command not found") == "missing_tool"

    def test_syntax_error_is_app_logic(self):
        assert classify_error("SyntaxError: unexpected token '}'") == "app_logic"

    def test_test_failure_is_app_logic(self):
        assert classify_error("Test suite failed; deployment aborted") == "app_logic"

    def test_unknown_error(self):
        assert classify_error("something completely random") == "unknown"

    def test_empty_string(self):
        assert classify_error("") == "unknown"

    def test_case_insensitive(self):
        assert classify_error("NO MODULE NAMED 'REQUESTS'") == "missing_dep"


# ---------------------------------------------------------------------------
# _normalize_error()
# ---------------------------------------------------------------------------

class TestNormalizeError:
    def test_strips_uuids(self):
        result = _normalize_error("Build 12345678-1234-4234-8234-123456789abc failed")
        assert "<UUID>" in result

    def test_strips_timestamps(self):
        result = _normalize_error("Error at 2024-03-20T15:30:00")
        assert "<TIMESTAMP>" in result

    def test_strips_paths(self):
        result = _normalize_error("File not found: /usr/local/bin/foo")
        assert "/<PATH>/" in result

    def test_empty_string(self):
        assert _normalize_error("") == ""


# ---------------------------------------------------------------------------
# scan_recent_failures()
# ---------------------------------------------------------------------------

class TestScanRecentFailures:
    def test_returns_empty_when_no_failures(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        result = scan_recent_failures(conn)
        assert result == []

    def test_groups_similar_errors(self):
        now = datetime.now(timezone.utc)
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ("id1", "[Errno 2] No such file: 'dropdb'", "app1", now),
            ("id2", "[Errno 2] No such file: 'dropdb'", "app2", now),
        ]
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        result = scan_recent_failures(conn, min_occurrences=2)
        assert len(result) == 1
        assert result[0]["count"] == 2
        assert result[0]["category"] == "missing_tool"

    def test_filters_below_min_occurrences(self):
        now = datetime.now(timezone.utc)
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ("id1", "Some unique error", "app1", now),
        ]
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        result = scan_recent_failures(conn, min_occurrences=2)
        assert result == []

    def test_skips_null_errors(self):
        now = datetime.now(timezone.utc)
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ("id1", None, "app1", now),
            ("id2", None, "app2", now),
        ]
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        result = scan_recent_failures(conn, min_occurrences=1)
        assert result == []
