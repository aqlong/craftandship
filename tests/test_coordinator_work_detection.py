# tests/test_coordinator_work_detection.py
#
# Covers gaps in coordinator/work_detection.py not already in tests/test_coordinator.py.
#
# Already tested elsewhere (skipped here):
#   - context_hash
#   - _check_failed_builds (basic + infra routing)
#   - _check_apps_down
#   - _check_budget_alert
#   - _check_agent_initiative

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coordinator.work_detection import (
    _check_new_feedback,
    _check_stale_backup,
    _check_new_live_apps,
    _check_reconciliation_sweep,
    _route_build_failure,
    _INFRA_CATEGORIES,
)
from coordinator.state import WorkItem


# ---------------------------------------------------------------------------
# Cursor-mocking helper
# ---------------------------------------------------------------------------

def _make_conn(rows):
    """Return a mock psycopg2 connection whose cursor yields the given rows."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = rows
    conn.cursor.return_value = cur
    return conn


def _make_conn_fetchone(row):
    """Return a mock psycopg2 connection whose cursor.fetchone returns row."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = row
    conn.cursor.return_value = cur
    return conn


# ---------------------------------------------------------------------------
# TestRouteBuildFailure (edge cases)
# ---------------------------------------------------------------------------

class TestReconciliationSweep:
    """_check_reconciliation_sweep interval gating (CO5 regression). The gate
    must compare against the last ACTUAL sweep time (from agent_tasks), not the
    prior tick time; using last_tick_time (~30 min) meant hours_since stayed
    under the interval and the sweep never fired after the first tick."""

    _NOW = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)

    def _enabled(self):
        return patch.dict(os.environ, {
            "AGENT_RECONCILIATION_ENABLED": "true",
            "COORDINATOR_RECONCILIATION_INTERVAL_H": "2",
        })

    def test_disabled_by_default(self):
        conn = _make_conn_fetchone((None,))
        with patch.dict(os.environ, {"AGENT_RECONCILIATION_ENABLED": "false"}):
            assert _check_reconciliation_sweep(conn, self._NOW, None) == []

    def test_fires_when_no_prior_sweep(self):
        conn = _make_conn_fetchone((None,))  # no prior reconciliation_sweep task
        with self._enabled():
            items = _check_reconciliation_sweep(
                conn, self._NOW, self._NOW - timedelta(minutes=30))
        assert len(items) == 1
        assert items[0].category == "reconciliation_sweep"

    def test_skips_when_within_interval(self):
        # Swept 30 min ago -> within the 2h interval -> must skip.
        conn = _make_conn_fetchone((self._NOW - timedelta(minutes=30),))
        with self._enabled():
            items = _check_reconciliation_sweep(
                conn, self._NOW, self._NOW - timedelta(minutes=30))
        assert items == []

    def test_fires_when_past_interval_even_with_recent_tick(self):
        # Swept 3h ago (> 2h interval) but last_tick_time is only 30 min ago.
        # The OLD code gated on last_tick_time and would (wrongly) skip; the
        # fix gates on the sweep time and fires.
        conn = _make_conn_fetchone((self._NOW - timedelta(hours=3),))
        with self._enabled():
            items = _check_reconciliation_sweep(
                conn, self._NOW, self._NOW - timedelta(minutes=30))
        assert len(items) == 1


class TestRouteBuildFailure:
    """_route_build_failure edge cases beyond what test_coordinator.py covers."""

    def test_routes_transient_to_infra_engineer(self):
        assert _route_build_failure("transient") == "infra_engineer"

    def test_routes_none_to_backend_engineer(self):
        assert _route_build_failure(None) == "backend_engineer"

    def test_routes_unknown_to_backend_engineer(self):
        assert _route_build_failure("unknown") == "backend_engineer"

    def test_routes_app_logic_to_backend_engineer(self):
        assert _route_build_failure("app_logic") == "backend_engineer"

    def test_routes_infra_to_infra_engineer(self):
        # Confirm "infra" itself is in _INFRA_CATEGORIES and routes correctly.
        assert "infra" in _INFRA_CATEGORIES
        assert _route_build_failure("infra") == "infra_engineer"

    def test_routes_code_error_to_backend_engineer(self):
        assert _route_build_failure("code_error") == "backend_engineer"


# ---------------------------------------------------------------------------
# TestCheckNewFeedback
# ---------------------------------------------------------------------------

class TestCheckNewFeedback:
    """_check_new_feedback trigger."""

    def test_returns_empty_when_last_tick_time_is_none(self):
        conn = MagicMock()
        result = _check_new_feedback(conn, None)
        assert result == []
        # cursor should NOT be called at all
        conn.cursor.assert_not_called()

    def test_returns_item_for_each_feedback_row(self):
        rows = [
            ("fb-id-1", "bug", "Something broke"),
            ("fb-id-2", "suggestion", "Would be nice if..."),
        ]
        conn = _make_conn(rows)
        last_tick = datetime.now(timezone.utc) - timedelta(minutes=10)
        result = _check_new_feedback(conn, last_tick)
        assert len(result) == 2

    def test_routes_to_customer_service(self):
        rows = [("fb-id-1", "bug", "broken login")]
        conn = _make_conn(rows)
        last_tick = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = _check_new_feedback(conn, last_tick)
        assert result[0].target_agent == "customer_service"

    def test_item_category_is_customer_feedback(self):
        rows = [("fb-id-1", "question", "How do I do X?")]
        conn = _make_conn(rows)
        last_tick = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = _check_new_feedback(conn, last_tick)
        assert result[0].category == "customer_feedback"

    def test_message_preview_truncated_to_200_chars(self):
        long_message = "A" * 300
        rows = [("fb-id-1", "bug", long_message)]
        conn = _make_conn(rows)
        last_tick = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = _check_new_feedback(conn, last_tick)
        assert len(result[0].context["message_preview"]) == 200

    def test_returns_empty_when_no_feedback_rows(self):
        conn = _make_conn([])
        last_tick = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = _check_new_feedback(conn, last_tick)
        assert result == []

    def test_context_includes_feedback_id(self):
        rows = [("fb-uuid-42", "suggestion", "Make it faster")]
        conn = _make_conn(rows)
        last_tick = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = _check_new_feedback(conn, last_tick)
        assert result[0].context["feedback_id"] == "fb-uuid-42"

    def test_context_includes_feedback_category(self):
        rows = [("fb-1", "other", "Random thought")]
        conn = _make_conn(rows)
        last_tick = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = _check_new_feedback(conn, last_tick)
        assert result[0].context["feedback_category"] == "other"


# ---------------------------------------------------------------------------
# TestCheckStaleBackup
# ---------------------------------------------------------------------------

class TestCheckStaleBackup:
    """_check_stale_backup trigger."""

    def _utc_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def test_returns_backup_overdue_when_stale(self):
        """50h-old backup exceeds 48h threshold -> one item returned."""
        old_backup = self._utc_now() - timedelta(hours=50)
        conn = _make_conn_fetchone((old_backup,))
        result = _check_stale_backup(conn)
        assert len(result) == 1
        assert result[0].category == "backup_overdue"

    def test_returns_empty_when_backup_recent(self):
        """10h-old backup is within 48h threshold -> empty list."""
        recent_backup = self._utc_now() - timedelta(hours=10)
        conn = _make_conn_fetchone((recent_backup,))
        result = _check_stale_backup(conn)
        assert result == []

    def test_returns_empty_when_no_backup_record(self):
        """If MAX(created_at) is NULL (no backup ever), return empty list."""
        conn = _make_conn_fetchone((None,))
        result = _check_stale_backup(conn)
        assert result == []

    def test_handles_naive_datetime(self):
        """Naive datetime (no tzinfo) 50h ago is treated as UTC and still triggers."""
        old_naive = (datetime.now(timezone.utc) - timedelta(hours=50)).replace(tzinfo=None)
        assert old_naive.tzinfo is None  # confirm naive
        conn = _make_conn_fetchone((old_naive,))
        result = _check_stale_backup(conn)
        assert len(result) == 1

    def test_routes_to_infra_engineer(self):
        old_backup = self._utc_now() - timedelta(hours=72)
        conn = _make_conn_fetchone((old_backup,))
        result = _check_stale_backup(conn)
        assert result[0].target_agent == "infra_engineer"

    def test_item_has_age_in_context(self):
        old_backup = self._utc_now() - timedelta(hours=50)
        conn = _make_conn_fetchone((old_backup,))
        result = _check_stale_backup(conn)
        age = result[0].context["last_backup_age_hours"]
        assert age >= 50.0

    def test_exactly_48h_is_not_overdue(self):
        """Backup just under 48h is not overdue (condition is strictly > 48h)."""
        # Use 47h59m to stay unambiguously below the threshold regardless of
        # microseconds elapsed between test setup and the function's datetime.now() call.
        just_under_48h = self._utc_now() - timedelta(hours=47, minutes=59)
        conn = _make_conn_fetchone((just_under_48h,))
        result = _check_stale_backup(conn)
        assert result == []


# ---------------------------------------------------------------------------
# TestCheckNewLiveApps
# ---------------------------------------------------------------------------

class TestCheckNewLiveApps:
    """_check_new_live_apps trigger."""

    def test_returns_empty_when_last_tick_time_is_none(self):
        conn = MagicMock()
        result = _check_new_live_apps(conn, None)
        assert result == []

    def test_returns_post_deploy_audit_item(self):
        rows = [("build-uuid-1", "myapp")]
        conn = _make_conn(rows)
        last_tick = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = _check_new_live_apps(conn, last_tick)
        assert len(result) == 1
        assert result[0].category == "post_deploy_audit"
        assert result[0].target_agent == "security"

    def test_returns_empty_when_no_new_live_apps(self):
        conn = _make_conn([])
        last_tick = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = _check_new_live_apps(conn, last_tick)
        assert result == []

    def test_context_includes_build_id_and_app_name(self):
        rows = [("build-uuid-99", "booking-app")]
        conn = _make_conn(rows)
        last_tick = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = _check_new_live_apps(conn, last_tick)
        assert result[0].context["build_id"] == "build-uuid-99"
        assert result[0].context["app_name"] == "booking-app"

    def test_returns_one_item_per_live_app(self):
        rows = [("b-1", "app1"), ("b-2", "app2"), ("b-3", "app3")]
        conn = _make_conn(rows)
        last_tick = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = _check_new_live_apps(conn, last_tick)
        assert len(result) == 3



