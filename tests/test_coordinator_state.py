# tests/test_coordinator_state.py
#
# Covers gaps in coordinator/state.py not already in tests/test_coordinator.py.
#
# Already tested elsewhere (skipped here):
#   - build_world_state() full integration
#   - LRU cache max size (test_coordinator.py has state_cache tests)

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import coordinator.state as state_module
from coordinator.state import (
    WorkItem,
    WorldState,
    _get_cache,
    _is_cache_fresh,
    _set_cache,
    _refresh_build_pipeline,
    _refresh_customer_signals,
    _refresh_financial,
    _refresh_infra_health,
    _load_pending_work,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_cache():
    """Reset module-level cache between tests."""
    state_module._cache.clear()


# ---------------------------------------------------------------------------
# TestCacheHelpers
# ---------------------------------------------------------------------------

class TestCacheHelpers:
    """_is_cache_fresh, _set_cache, _get_cache."""

    def setup_method(self):
        _clear_cache()

    def teardown_method(self):
        _clear_cache()

    def test_missing_key_is_not_fresh(self):
        assert _is_cache_fresh("nonexistent", 3600) is False

    def test_fresh_entry_is_fresh(self):
        _set_cache("test_key", "value")
        assert _is_cache_fresh("test_key", 3600) is True

    def test_stale_entry_is_not_fresh(self):
        _set_cache("test_key", "value")
        # Patch time to be far in the future
        with patch("coordinator.state.time") as mock_time:
            mock_time.time.return_value = time.time() + 7200
            assert _is_cache_fresh("test_key", 3600) is False

    def test_get_cache_returns_value(self):
        _set_cache("mykey", {"data": 42})
        assert _get_cache("mykey") == {"data": 42}

    def test_get_cache_missing_key_returns_none(self):
        assert _get_cache("nope") is None


    def test_set_cache_overwrites_existing_key(self):
        _set_cache("k", "old")
        _set_cache("k", "new")
        assert _get_cache("k") == "new"

    def test_lru_eviction_at_max_size(self):
        """When cache exceeds _MAX_CACHE_SIZE, oldest entry is evicted."""
        max_size = state_module._MAX_CACHE_SIZE
        for i in range(max_size):
            _set_cache(f"key_{i}", i)
        # Cache is now full; adding one more should evict key_0
        _set_cache("overflow", "x")
        assert _get_cache("key_0") is None
        assert _get_cache("overflow") == "x"


# ---------------------------------------------------------------------------
# TestRefreshBuildPipeline
# ---------------------------------------------------------------------------

class TestRefreshBuildPipeline:
    """_refresh_build_pipeline() maps DB rows to WorldState fields."""

    def test_counts_queued_and_active_builds(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        # First fetchall: status counts; second fetchall: failed builds
        mock_cur.fetchall.side_effect = [
            [("queued", 3), ("building", 1), ("testing", 2), ("preview", 4)],
            [],  # no failed builds
        ]

        state = WorldState()
        _refresh_build_pipeline(mock_conn, state)

        assert state.queued_builds == 3
        assert state.active_builds == 3  # building + testing
        assert state.preview_awaiting == 4

    def test_failed_builds_24h_populated(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        import uuid
        build_id = uuid.uuid4()
        mock_cur.fetchall.side_effect = [
            [],  # no status counts
            [(build_id, "my-app", "some error", "build", 2, "code_error")],
        ]

        state = WorldState()
        _refresh_build_pipeline(mock_conn, state)

        assert len(state.failed_builds_24h) == 1
        assert state.failed_builds_24h[0]["app_name"] == "my-app"
        assert state.failed_builds_24h[0]["error_category"] == "code_error"

    def test_unknown_status_not_counted(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        mock_cur.fetchall.side_effect = [
            [("live", 5)],  # status not in tracked set
            [],
        ]
        state = WorldState()
        _refresh_build_pipeline(mock_conn, state)
        assert state.queued_builds == 0
        assert state.active_builds == 0

    def test_empty_db_leaves_state_defaults(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.side_effect = [[], []]

        state = WorldState()
        _refresh_build_pipeline(mock_conn, state)
        assert state.queued_builds == 0
        assert state.active_builds == 0
        assert state.failed_builds_24h == []


# ---------------------------------------------------------------------------
# TestRefreshCustomerSignals
# ---------------------------------------------------------------------------

class TestRefreshCustomerSignals:
    """_refresh_customer_signals() reads abandoned intakes + pending feedback."""

    def test_sets_abandoned_intakes_and_feedback(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.side_effect = [(7,), (3,)]

        state = WorldState()
        _refresh_customer_signals(mock_conn, state)

        assert state.abandoned_intakes_24h == 7
        assert state.pending_feedback == 3

    def test_zero_counts_accepted(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.side_effect = [(0,), (0,)]

        state = WorldState()
        _refresh_customer_signals(mock_conn, state)
        assert state.abandoned_intakes_24h == 0
        assert state.pending_feedback == 0


# ---------------------------------------------------------------------------
# TestRefreshInfraHealth
# ---------------------------------------------------------------------------

class TestRefreshInfraHealth:
    """_refresh_infra_health() separates down vs degraded apps."""

    def test_down_app_goes_to_monitored_apps_down(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            ("app-one", "https://app-one.up.railway.app", 5, "down"),
        ]

        state = WorldState()
        _refresh_infra_health(mock_conn, state)

        assert len(state.monitored_apps_down) == 1
        assert state.monitored_apps_down[0]["app_name"] == "app-one"
        assert len(state.monitored_apps_degraded) == 0

    def test_degraded_app_goes_to_monitored_apps_degraded(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            ("app-two", "https://app-two.up.railway.app", 2, "degraded"),
        ]

        state = WorldState()
        _refresh_infra_health(mock_conn, state)

        assert len(state.monitored_apps_degraded) == 1
        assert len(state.monitored_apps_down) == 0

    def test_mixed_apps_separated(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            ("a", "https://a.railway.app", 10, "down"),
            ("b", "https://b.railway.app", 1, "degraded"),
            ("c", "https://c.railway.app", 7, "down"),
        ]

        state = WorldState()
        _refresh_infra_health(mock_conn, state)
        assert len(state.monitored_apps_down) == 2
        assert len(state.monitored_apps_degraded) == 1


# ---------------------------------------------------------------------------
# TestRefreshFinancial
# ---------------------------------------------------------------------------

class TestRefreshFinancial:
    """_refresh_financial() aggregates build + agent costs through the ledger
    query the budget also uses (spend_tracker.get_daily_spend), whose rows are
    (sum, count)."""

    def test_sums_build_and_agent_cost(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        # build_cost = 5.0, agent_cost = 2.0
        mock_cur.fetchone.side_effect = [(5.0, 3), (2.0, 4)]

        state = WorldState()
        _refresh_financial(mock_conn, state, daily_budget=20.0)

        assert state.api_cost_today_usd == pytest.approx(7.0)
        assert state.daily_budget_remaining_usd == pytest.approx(13.0)

    def test_remaining_clamps_to_zero_on_overspend(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.side_effect = [(18.0, 1), (5.0, 1)]  # total = 23 > 20

        state = WorldState()
        _refresh_financial(mock_conn, state, daily_budget=20.0)
        assert state.daily_budget_remaining_usd == 0.0

    def test_zero_spend(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.side_effect = [(0.0, 0), (0.0, 0)]

        state = WorldState()
        _refresh_financial(mock_conn, state, daily_budget=20.0)
        assert state.api_cost_today_usd == pytest.approx(0.0)
        assert state.daily_budget_remaining_usd == pytest.approx(20.0)

    def test_world_state_and_budget_read_the_ledger_with_one_query(self):
        """The world state's figure and the budget's are the same statements,
        so they cannot disagree about what counts as today's spend."""
        from coordinator import spend_tracker

        def statements(reader):
            conn = MagicMock()
            cur = conn.cursor.return_value
            cur.fetchone.side_effect = [(1.0, 1), (1.0, 1)]
            reader(conn)
            return [call.args[0] for call in cur.execute.call_args_list]

        from_state = statements(lambda conn: _refresh_financial(conn, WorldState(), daily_budget=20.0))
        from_budget = statements(spend_tracker.get_daily_spend)
        assert from_state == from_budget and len(from_state) == 2


# ---------------------------------------------------------------------------
# TestLoadPendingWork
# ---------------------------------------------------------------------------

class TestLoadPendingWork:
    """_load_pending_work() populates state.pending_work with WorkItem objects."""

    def test_populates_work_items(self):
        import uuid
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        task_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        mock_cur.fetchall.return_value = [
            (task_id, "build_failure", "backend_engineer", "Fix build", {"build_id": "abc"}, "hash123", now),
        ]

        state = WorldState()
        _load_pending_work(mock_conn, state)

        assert len(state.pending_work) == 1
        item = state.pending_work[0]
        assert isinstance(item, WorkItem)
        assert item.category == "build_failure"
        assert item.target_agent == "backend_engineer"
        assert item.context_hash == "hash123"

    def test_empty_table_leaves_empty_list(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []

        state = WorldState()
        _load_pending_work(mock_conn, state)
        assert state.pending_work == []

    def test_null_context_defaults_to_empty_dict(self):
        import uuid
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        now = datetime.now(timezone.utc)
        mock_cur.fetchall.return_value = [
            (uuid.uuid4(), "cost_projection", "documentation", None, None, None, now),
        ]

        state = WorldState()
        _load_pending_work(mock_conn, state)
        item = state.pending_work[0]
        assert item.context == {}
        assert item.context_hash == ""
        assert item.description == ""
