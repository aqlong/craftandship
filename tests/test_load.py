"""Minimal load/stress tests for worker infrastructure.

Tests repeated job claims, connection handling under pressure,
and retry/timeout behavior at scale. All DB calls are mocked.

Run with: python3 -m pytest tests/test_load.py -v
"""

import itertools
import sys
import time
import statistics
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

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
from tests._fake_pg_tools import write_dump  # noqa: E402

# The attempt token every guarded status write takes (never compared here: the
# database is mocked).
from datetime import datetime  # noqa: E402
started_at = datetime(2026, 1, 2, 3, 4, 5)


# ---------------------------------------------------------------------------
# Repeated claim stress test
# ---------------------------------------------------------------------------

class TestRepeatedClaims:
    """Repeated calls into the worker's claim, status and error-classification
    helpers, against a mocked database."""

    def test_repeated_claims_stop_at_the_concurrency_limit(self, monkeypatch):
        """Ten polls in a row against a queue that always has a build: exactly
        _MAX_CONCURRENT of them claim one and every later poll gets None. The
        active-build count is the number of builds claimed so far."""
        limit = 3
        monkeypatch.setattr(worker_pg, "_MAX_CONCURRENT", limit)
        monkeypatch.setattr(worker_pg, "_DAILY_SPEND_CAP", 0)  # spend guard off
        claimed = []

        queued_row = ("b-id", "build", "my-app", {}, "c-id", 0, None, None, "a@example.test", "A")
        # One claim reads three rows from the cursor, in this order: the advisory
        # lock, the queued build, and the started_at the UPDATE returns.
        cursor = MagicMock()
        cursor.fetchone.side_effect = itertools.cycle([(True,), queued_row, (started_at,)])
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        monkeypatch.setattr(worker_pg, "_active_build_count", lambda _conn: len(claimed))
        for _ in range(10):
            job = worker_pg._claim_next_job(conn)
            if job is not None:
                claimed.append(job)

        assert len(claimed) == limit
        assert conn.commit.call_count == limit

    def test_transient_error_detection_throughput(self):
        """_is_transient_error can handle 10,000+ checks per second."""
        errors = [
            "Connection refused", "timeout", "rate limit", "503",
            "SyntaxError", "build failed", "test suite failed",
            "Connection reset by peer",
        ] * 1250  # 10,000 total

        start = time.monotonic()
        results = [worker_pg._is_transient_error(e) for e in errors]
        elapsed = time.monotonic() - start

        transient_count = sum(results)
        # 5 of 8 test strings are transient: Connection refused, timeout, rate limit, 503, Connection reset
        assert transient_count == 6250  # 5 transient * 1250
        assert elapsed < 1.0, f"10,000 checks took {elapsed:.2f}s (should be <1s)"

    def test_set_status_latency_under_mock(self):
        """_set_status completes quickly even with 100 rapid calls.

        Thresholds are intentionally loose: this test guards the order of
        magnitude (real code with a network call would take 100ms+), not
        absolute microsecond performance. The original <1ms avg / <5ms p99
        bound was system-load dependent and flaked repeatedly on both
        GitHub Actions (3.9ms observed 2026-05-04) and local Mac
        (3.0ms observed 2026-05-26 on the executor's CORS-allowlist run).
        Loosening to <10ms avg / <50ms p99 still catches a real perf
        regression (orders of magnitude slower) without flaking on
        loaded CI runners.
        """
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        timings = []
        for i in range(100):
            start = time.monotonic()
            worker_pg._set_status(mock_conn, f"build-{i}", "building", attempt=started_at)
            timings.append(time.monotonic() - start)

        avg_ms = statistics.mean(timings) * 1000
        p99_ms = sorted(timings)[98] * 1000

        assert avg_ms < 10.0, f"Average latency {avg_ms:.3f}ms (should be <10ms)"
        assert p99_ms < 50.0, f"P99 latency {p99_ms:.3f}ms (should be <50ms)"
        assert mock_cursor.execute.call_count == 100


# ---------------------------------------------------------------------------
# Connection retry stress test
# ---------------------------------------------------------------------------

class TestConnectionRetryStress:
    def test_get_conn_retries_under_intermittent_failures(self):
        """_get_conn handles multiple transient failures before succeeding."""
        import psycopg2

        fail_count = [0]
        mock_conn = MagicMock()

        def side_effect(url, **kwargs):
            fail_count[0] += 1
            if fail_count[0] <= 3:
                raise psycopg2.OperationalError("connection refused")
            return mock_conn

        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://x"}):
            with patch("psycopg2.connect", side_effect=side_effect):
                with patch("time.sleep"):
                    conn = worker_pg._get_conn(retries=5, base_delay=0.01)

        assert conn is mock_conn
        assert fail_count[0] == 4  # 3 failures + 1 success


# ---------------------------------------------------------------------------
# Backup prune performance
# ---------------------------------------------------------------------------

class TestPrunePerformance:
    def test_prune_handles_large_backup_dir(self, tmp_path):
        """prune_backups handles 100 backup files efficiently."""
        import monitor.backup as backup_module

        with patch.object(backup_module, "BACKUPS_DIR", tmp_path):
            app_dir = tmp_path / "load-test-app"
            app_dir.mkdir()

            for i in range(100):
                f = write_dump(app_dir / f"2024{i:04d}T000000Z.sql.gz")
                import os
                os.utime(str(f), (1000 + i, 1000 + i))

            start = time.monotonic()
            deleted = backup_module.prune_backups("load-test-app", keep=7)
            elapsed = time.monotonic() - start

        assert len(deleted) == 93
        remaining = list(app_dir.glob("*.sql.gz"))
        assert len(remaining) == 7
        assert elapsed < 2.0, f"Pruning 93 files took {elapsed:.2f}s (should be <2s)"


# ---------------------------------------------------------------------------
# Summary report (prints to stdout)
# ---------------------------------------------------------------------------

class TestLoadReport:
    def test_generate_load_summary(self, capsys):
        """Generate and print a load test summary report."""
        # Run a quick batch of operations and time them
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Transient error detection
        errors_to_check = ["Connection refused", "timeout", "SyntaxError", "503"] * 250
        t0 = time.monotonic()
        for e in errors_to_check:
            worker_pg._is_transient_error(e)
        transient_time = time.monotonic() - t0

        # Status updates
        t0 = time.monotonic()
        for i in range(200):
            worker_pg._set_status(mock_conn, f"b-{i}", "building", attempt=started_at)
        status_time = time.monotonic() - t0

        # Requeue operations
        t0 = time.monotonic()
        for i in range(100):
            worker_pg._requeue_for_retry(mock_conn, f"b-{i}", 0, "timeout", attempt=started_at)
        requeue_time = time.monotonic() - t0

        report = (
            "\n"
            "=" * 60 + "\n"
            "  LOAD TEST RESULTS\n"
            "=" * 60 + "\n"
            f"  Transient error check:  1000 ops in {transient_time*1000:.1f}ms "
            f"({1000/transient_time:.0f} ops/sec)\n"
            f"  Status updates (mock):  200 ops in {status_time*1000:.1f}ms "
            f"({200/status_time:.0f} ops/sec)\n"
            f"  Requeue operations:     100 ops in {requeue_time*1000:.1f}ms "
            f"({100/requeue_time:.0f} ops/sec)\n"
            "=" * 60
        )
        print(report)

        # Assertions: all operations should be fast with mocked DB.
        # Generous thresholds (4x typical) so the test is not flaky under CI / executor
        # parallel load. Idle local runs are ~0.05-0.2s; the executor's parallel-Claude
        # contention pushed status_time to 0.52s on 2026-04-30 and tripped the prior
        # 0.5s gate, killing an otherwise-clean uuid bump merge.
        assert transient_time < 2.0, f"Transient checks too slow ({transient_time:.2f}s)"
        assert status_time < 2.0, f"Status updates too slow ({status_time:.2f}s)"
        assert requeue_time < 2.0, f"Requeue operations too slow ({requeue_time:.2f}s)"
