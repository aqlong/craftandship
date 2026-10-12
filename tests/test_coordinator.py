# tests/test_coordinator.py: Tests for the Master Coordinator framework
#
# Tests: WorldState snapshot, scoring, budget computation, work detection,
# tick lifecycle, and actuator permissions.

import hashlib
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

_ROOT = str(Path(__file__).parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from coordinator.state import WorldState, WorkItem, build_world_state
from coordinator.scorer import (
    score_task, CATEGORY_WEIGHTS, _urgency_multiplier, _cost_penalty,
    select_model, estimate_task_cost, AGENT_MODELS,
    _performance_multiplier_cached, build_performance_cache,
)
from coordinator.budget import compute_tick_budget, TickBudget, RPM_SPACING
from coordinator.work_detection import (
    detect_work, compute_context_hash,
    _check_failed_builds, _check_apps_down, _check_budget_alert,
    _check_recurring_qa_warnings, _check_weekly_cost_projection,
    _check_agent_initiative, _check_test_build_needed,
    _route_build_failure, _INFRA_CATEGORIES,
    _make_item,
)
from coordinator.actuators import Actuators, AGENT_PERMISSIONS, Action
from coordinator.tick import (
    run_tick, register_agent, _persist_work_item,
    _expire_stale_work, _is_agent_enabled,
    _write_tick_record, _group_independent_tasks, _dispatch_one_parallel,
    COORDINATOR_MAX_PARALLEL, _get_agent_timeout,
    AGENT_TIMEOUTS, CATEGORY_TIMEOUTS,
    _expire_stale_approval_requests, _surface_initiative_findings,
)
from coordinator.state import _cache, _MAX_CACHE_SIZE
from coordinator import config


@pytest.fixture(autouse=True)
def _no_database_url(monkeypatch):
    """Every test in this module hands the tick a mocked connection. With a
    DATABASE_URL in the runner's environment (CI and some local runs set one)
    the tick opens a connection of its own for each dispatched agent, to a
    real server that knows nothing of the test, and the tests that count
    dispatched tasks then report on that database. A test that wants the
    variable sets it itself."""
    monkeypatch.delenv("DATABASE_URL", raising=False)


# ---------------------------------------------------------------------------
# WorldState
# ---------------------------------------------------------------------------

class TestWorldState:
    def test_defaults(self):
        state = WorldState()
        assert state.queued_builds == 0
        assert state.active_builds == 0
        assert state.failed_builds_24h == []
        assert state.daily_budget_remaining_usd == 20.0
        assert state.pending_work == []

    def test_tick_id_populated(self):
        state = WorldState(tick_id="test-123", tick_number=5)
        assert state.tick_id == "test-123"
        assert state.tick_number == 5

    @patch("coordinator.state._refresh_build_pipeline")
    @patch("coordinator.state._refresh_customer_signals")
    @patch("coordinator.state._refresh_infra_health")
    @patch("coordinator.state._refresh_financial")
    @patch("coordinator.state._load_pending_work")
    def test_build_world_state(self, mock_pending, mock_fin, mock_infra, mock_cust, mock_build):
        conn = MagicMock()
        state = build_world_state(conn, tick_number=1)
        assert state.tick_number == 1
        assert state.tick_id  # UUID generated
        mock_build.assert_called_once()
        mock_cust.assert_called_once()
        mock_infra.assert_called_once()
        mock_fin.assert_called_once()

    @patch("coordinator.state._refresh_build_pipeline")
    @patch("coordinator.state._refresh_customer_signals")
    @patch("coordinator.state._refresh_infra_health")
    @patch("coordinator.state._refresh_financial")
    @patch("coordinator.state._load_pending_work")
    def test_build_world_state_handles_errors(self, mock_pending, mock_fin, mock_infra, mock_cust, mock_build):
        mock_build.side_effect = Exception("DB error")
        conn = MagicMock()
        state = build_world_state(conn, tick_number=1)
        # Should not raise. The figures of the refresh that failed are unknown
        # (None), not 0: this assertion used to pin "queued_builds == 0".
        assert state.queued_builds is None and state.active_builds is None
        assert state.failed_builds_24h is None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class TestScorer:
    def test_category_weights_range(self):
        for cat, weight in CATEGORY_WEIGHTS.items():
            assert 0.0 < weight <= 1.0, f"Weight for {cat} out of range: {weight}"

    def test_app_down_highest_priority(self):
        assert CATEGORY_WEIGHTS["app_down"] > CATEGORY_WEIGHTS["build_failure"]
        assert CATEGORY_WEIGHTS["app_down"] > CATEGORY_WEIGHTS["agent_initiative"]

    def test_score_task_basic(self):
        state = WorldState(daily_budget_remaining_usd=20.0)
        task = WorkItem(category="build_failure", created_at=datetime.now(timezone.utc))
        score = score_task(task, state)
        assert 0.0 < score <= 1.0
        assert score == pytest.approx(0.80, abs=0.05)  # base weight * 1.0 urgency * 1.0 cost

    def test_score_task_capped_at_1(self):
        state = WorldState(daily_budget_remaining_usd=20.0)
        # Very old app_down with many failures -> should cap at 1.0
        task = WorkItem(
            category="app_down",
            context={"consecutive_failures": 15},
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        score = score_task(task, state)
        assert score <= 1.0

    def test_urgency_multiplier_fresh_task(self):
        state = WorldState()
        task = WorkItem(category="build_failure", created_at=datetime.now(timezone.utc))
        mult = _urgency_multiplier(task, state)
        assert mult == pytest.approx(1.0, abs=0.05)

    def test_urgency_multiplier_stale_task(self):
        state = WorldState()
        task = WorkItem(
            category="build_failure",
            created_at=datetime.now(timezone.utc) - timedelta(hours=24),
        )
        mult = _urgency_multiplier(task, state)
        # Tiered escalation: 24h task hits the 8h+ tier, capped at 2.0
        assert mult == pytest.approx(2.0, abs=0.05)

    def test_urgency_multiplier_app_down_many_failures(self):
        state = WorldState()
        task = WorkItem(
            category="app_down",
            context={"consecutive_failures": 12},
            created_at=datetime.now(timezone.utc),
        )
        mult = _urgency_multiplier(task, state)
        assert mult == 2.0

    def test_urgency_multiplier_app_down_moderate_failures(self):
        state = WorldState()
        task = WorkItem(
            category="app_down",
            context={"consecutive_failures": 7},
            created_at=datetime.now(timezone.utc),
        )
        mult = _urgency_multiplier(task, state)
        assert mult == 1.5

    def test_cost_penalty_high_budget(self):
        state = WorldState(daily_budget_remaining_usd=15.0)
        assert _cost_penalty(state) == 1.0

    def test_cost_penalty_medium_budget(self):
        state = WorldState(daily_budget_remaining_usd=6.0)
        assert _cost_penalty(state) == 0.6

    def test_cost_penalty_low_budget(self):
        state = WorldState(daily_budget_remaining_usd=2.0)
        assert _cost_penalty(state) == 0.3

    def test_cost_penalty_emergency(self):
        state = WorldState(daily_budget_remaining_usd=0.5)
        assert _cost_penalty(state) == 0.1

    def test_score_decreases_with_low_budget(self):
        task = WorkItem(category="cost_projection", created_at=datetime.now(timezone.utc))
        high_budget = WorldState(daily_budget_remaining_usd=20.0)
        low_budget = WorldState(daily_budget_remaining_usd=1.0)
        assert score_task(task, high_budget) > score_task(task, low_budget)


# ---------------------------------------------------------------------------
# Model Selection
# ---------------------------------------------------------------------------

class TestModelSelection:
    def test_select_model_normal(self):
        budget = TickBudget(
            max_api_calls=10, max_cost_usd=5.0, max_opus_calls=2,
            allowed_models=["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6"],
        )
        task = WorkItem(category="build_failure")
        model = select_model("infra_engineer", task, budget)
        assert model == "claude-sonnet-4-6"  # default for infra

    def test_select_model_haiku_only(self):
        budget = TickBudget(
            max_api_calls=1, max_cost_usd=0.10, max_opus_calls=0,
            allowed_models=["claude-haiku-4-5"],
        )
        task = WorkItem(category="build_failure")
        model = select_model("infra_engineer", task, budget)
        assert model == "claude-haiku-4-5"  # downgraded

    def test_select_model_upgrade(self):
        budget = TickBudget(
            max_api_calls=10, max_cost_usd=5.0, max_opus_calls=2,
            allowed_models=["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6"],
        )
        task = WorkItem(category="build_failure", context={"requires": "diagnostics"})
        model = select_model("infra_engineer", task, budget)
        assert model == "claude-opus-4-6"  # upgraded

    def test_select_model_no_opus_budget(self):
        budget = TickBudget(
            max_api_calls=10, max_cost_usd=5.0, max_opus_calls=0,
            allowed_models=["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6"],
        )
        task = WorkItem(category="build_failure", context={"requires": "diagnostics"})
        model = select_model("infra_engineer", task, budget)
        # Should NOT upgrade because max_opus_calls=0
        assert model == "claude-sonnet-4-6"

    def test_select_model_unknown_agent(self):
        budget = TickBudget(allowed_models=["claude-haiku-4-5"])
        task = WorkItem()
        assert select_model("nonexistent", task, budget) is None

    def test_estimate_task_cost(self):
        assert estimate_task_cost("infra_engineer", "claude-sonnet-4-6") == 0.12
        assert estimate_task_cost("financial", "claude-haiku-4-5") == 0.05
        assert estimate_task_cost("backend_engineer", "claude-opus-4-6") == 0.80


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

def _budget_with_ledger(state, spent_today: float):
    """compute_tick_budget against a connection whose ledger reports
    `spent_today` dollars, with the default $20 daily budget, no admin key
    (so the ledger is the spend source) and the clock at noon UTC (24 ticks
    of 30 minutes left)."""
    conn = MagicMock()
    conn.cursor.return_value.fetchone.side_effect = [(spent_today, 1), (0.0, 0)]
    env = {"COORDINATOR_DAILY_BUDGET": "20.0", "COORDINATOR_INTERVAL_S": "1800",
           "ANTHROPIC_ADMIN_KEY": ""}
    with patch.dict(os.environ, env), patch("coordinator.budget.datetime") as clock:
        clock.now.return_value = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        return compute_tick_budget(state, conn=conn)


class TestBudget:
    def test_active_builds_yields_haiku(self):
        state = WorldState(active_builds=2)
        budget = _budget_with_ledger(state, spent_today=5.0)
        assert budget.max_api_calls == 1
        assert budget.max_opus_calls == 0
        assert budget.allowed_models == ["claude-haiku-4-5"]
        assert budget.max_cost_usd == pytest.approx(0.10)

    def test_no_active_builds_full_budget(self):
        state = WorldState(active_builds=0)
        budget = _budget_with_ledger(state, spent_today=10.0)
        assert "claude-opus-4-6" in budget.allowed_models
        assert "claude-sonnet-4-6" in budget.allowed_models
        assert "claude-haiku-4-5" in budget.allowed_models

    def test_budget_math(self):
        state = WorldState(active_builds=0)
        budget = _budget_with_ledger(state, spent_today=8.0)
        # $12 left over 24 ticks: $0.50 a tick, ten Haiku calls at $0.05,
        # no Opus call at $0.80
        assert budget.max_cost_usd == pytest.approx(0.50)
        assert budget.max_api_calls == 10
        assert budget.max_opus_calls == 0

    def test_zero_budget_means_zero_calls(self):
        state = WorldState(active_builds=0)
        budget = _budget_with_ledger(state, spent_today=20.0)
        assert budget.max_cost_usd == 0.0
        assert budget.max_api_calls == 0
        assert budget.max_opus_calls == 0

    def test_zero_budget_means_zero_calls_while_builds_are_active(self):
        state = WorldState(active_builds=1)
        budget = _budget_with_ledger(state, spent_today=20.0)
        assert budget.max_cost_usd == 0.0
        assert budget.max_api_calls == 0

    def test_a_per_tick_figure_below_one_call_buys_no_call(self):
        # $0.96 left over 24 ticks is $0.04 a tick, less than one Haiku call
        state = WorldState(active_builds=0)
        budget = _budget_with_ledger(state, spent_today=19.04)
        assert 0 < budget.max_cost_usd < 0.05
        assert budget.max_api_calls == 0

    def test_no_connection_means_no_budget(self):
        state = WorldState(active_builds=0, daily_budget_remaining_usd=15.0)
        budget = compute_tick_budget(state, None)
        assert (budget.max_cost_usd, budget.max_api_calls) == (0.0, 0)

    def test_rpm_spacing_values(self):
        assert RPM_SPACING["claude-opus-4-6"] == 2.0
        assert RPM_SPACING["claude-sonnet-4-6"] == 1.0
        assert RPM_SPACING["claude-haiku-4-5"] == 0.5


# ---------------------------------------------------------------------------
# Work Detection
# ---------------------------------------------------------------------------

class TestWorkDetection:
    def test_context_hash_deterministic(self):
        ctx = {"build_id": "123", "app_name": "test"}
        h1 = compute_context_hash(ctx)
        h2 = compute_context_hash(ctx)
        assert h1 == h2
        assert len(h1) == 32

    def test_context_hash_different_for_different_context(self):
        h1 = compute_context_hash({"build_id": "123"})
        h2 = compute_context_hash({"build_id": "456"})
        assert h1 != h2

    def test_deduplication_at_persist_time(self):
        """Deduplication now happens at INSERT time via ON CONFLICT DO NOTHING."""
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        # Duplicate: fetchone returns None (no RETURNING id)
        cur.fetchone.return_value = None
        item = WorkItem(category="build_failure", target_agent="infra_engineer",
                        context={"id": "123"}, context_hash=compute_context_hash({"id": "123"}))
        result = _persist_work_item(conn, item, "tick-1")
        assert result is False

    def test_persist_work_item_returns_true_when_inserted(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        cur.fetchone.return_value = ("new-id",)
        item = WorkItem(category="build_failure", target_agent="infra_engineer",
                        context={"id": "456"}, context_hash=compute_context_hash({"id": "456"}))
        result = _persist_work_item(conn, item, "tick-1")
        assert result is True

    def test_check_failed_builds(self):
        state = WorldState(failed_builds_24h=[
            {"id": "b1", "app_name": "app1", "error_message": "npm ERR!",
             "tier": "build", "retry_count": 0, "error_category": "code_error"},
        ])
        items = _check_failed_builds(MagicMock(), state, None)
        assert len(items) == 1
        assert items[0].category == "build_failure"
        assert items[0].target_agent == "backend_engineer"  # code_error routes to backend

    def test_check_failed_builds_infra_routing(self):
        state = WorldState(failed_builds_24h=[
            {"id": "b2", "app_name": "app2", "error_message": "connection refused",
             "tier": "build", "retry_count": 0, "error_category": "infra"},
        ])
        items = _check_failed_builds(MagicMock(), state, None)
        assert len(items) == 1
        assert items[0].target_agent == "infra_engineer"

    def test_check_apps_down(self):
        state = WorldState(monitored_apps_down=[
            {"app_name": "myapp", "live_url": "https://myapp.up.railway.app", "consecutive_failures": 5},
        ])
        items = _check_apps_down(state)
        assert len(items) == 1
        assert items[0].category == "app_down"

    def test_check_budget_alert_triggered(self):
        state = WorldState(daily_budget_remaining_usd=3.0, api_cost_today_usd=17.0)
        items = _check_budget_alert(state)
        assert len(items) == 1
        assert items[0].category == "budget_alert"

    def test_check_budget_alert_not_triggered(self):
        state = WorldState(daily_budget_remaining_usd=15.0)
        items = _check_budget_alert(state)
        assert len(items) == 0

    def test_detect_work_full(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        # No existing tasks (dedup check). Also return a valid past datetime
        # for _check_stale_backup age computation.
        cur.fetchone.return_value = (datetime.now(timezone.utc) - timedelta(hours=1),)
        # No feedback, no live apps
        cur.fetchall.return_value = []

        state = WorldState(
            failed_builds_24h=[{"id": "b1", "app_name": "app1", "error_message": "err", "tier": "build", "retry_count": 0}],
            monitored_apps_down=[{"app_name": "down1", "live_url": "https://x", "consecutive_failures": 3}],
        )
        items = detect_work(conn, state, last_tick_time=None)
        categories = [i.category for i in items]
        assert "build_failure" in categories
        assert "app_down" in categories

    # --- Proactive trigger tests ---


    @patch.dict(os.environ, {"ENABLE_TEST_BUILDS": "false"})
    def test_test_build_disabled_by_default(self):
        """Test builds are disabled when ENABLE_TEST_BUILDS is not 'true'."""
        state = WorldState(active_builds=0, queued_builds=0, daily_budget_remaining_usd=50.0)
        conn = MagicMock()
        items = _check_test_build_needed(conn, state)
        assert len(items) == 0
        conn.cursor.assert_not_called()

    def test_test_build_enabled_with_env_var(self):
        """Test builds fire when ENABLE_TEST_BUILDS=true and conditions met."""
        state = WorldState(active_builds=0, queued_builds=0, daily_budget_remaining_usd=50.0)
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = (0,)  # 0 recent builds
        with patch.dict(os.environ, {"ENABLE_TEST_BUILDS": "true"}):
            items = _check_test_build_needed(conn, state)
        assert len(items) == 1
        assert items[0].category == "test_build"

    def test_test_build_skipped_when_active_builds(self):
        """Test builds skip when builds are already active."""
        state = WorldState(active_builds=1, queued_builds=0, daily_budget_remaining_usd=50.0)
        conn = MagicMock()
        with patch.dict(os.environ, {"ENABLE_TEST_BUILDS": "true"}):
            items = _check_test_build_needed(conn, state)
        assert len(items) == 0


    def test_recurring_qa_warnings_triggered(self):
        """Flags recurring QA warning when it appears in 3+ of last 5 builds."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        # 4 builds with the same warning type
        qa_results_with_warning = [
            {"type": "slow_response", "status": "warning", "message": "Page load exceeded 2s"}
        ]
        cur.fetchall.return_value = [
            (qa_results_with_warning,),
            (qa_results_with_warning,),
            (qa_results_with_warning,),
            ([],),
            (qa_results_with_warning,),
        ]
        items = _check_recurring_qa_warnings(conn)
        assert len(items) == 1
        assert items[0].category == "recurring_qa_warning"
        assert items[0].target_agent == "backend_engineer"
        assert items[0].context["warning_type"] == "slow_response"
        assert items[0].context["occurrences"] == 4

    def test_recurring_qa_warnings_below_threshold(self):
        """Does not flag warning that appears in only 2 builds."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        qa_results = [{"type": "slow_response", "status": "warning"}]
        cur.fetchall.return_value = [
            (qa_results,),
            (qa_results,),
            ([],),
        ]
        items = _check_recurring_qa_warnings(conn)
        assert len(items) == 0

    def test_recurring_qa_warnings_no_warned_builds(self):
        """Returns empty list when no builds have qa_status=warned."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchall.return_value = []
        items = _check_recurring_qa_warnings(conn)
        assert items == []

    def test_recurring_qa_warnings_handles_db_error(self):
        """Gracefully returns empty list on DB error."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.execute.side_effect = Exception("DB error")
        items = _check_recurring_qa_warnings(conn)
        assert items == []


    def test_scorer_handles_proactive_and_unweighted_categories(self):
        """A category with no entry in CATEGORY_WEIGHTS still scores (the
        default weight), without KeyError."""
        from coordinator.scorer import score_task
        from coordinator.state import WorldState as WS
        state = WS()
        new_categories = ["recurring_qa_warning", "coordinator_stalled", "a_category_with_no_weight"]
        for cat in new_categories:
            item = WorkItem(category=cat, target_agent="infra_engineer")
            score = score_task(item, state)
            assert 0.0 <= score <= 1.0, f"Score out of range for {cat}: {score}"


# ---------------------------------------------------------------------------
# Actuator Permissions
# ---------------------------------------------------------------------------

class TestActuatorPermissions:
    def test_backend_engineer_can_queue_a_rebuild(self):
        act = Actuators("backend_engineer", "task-123", dry_run=True)
        # Should not raise
        assert act.queue_rebuild("parent-1") is None  # dry run returns None

    def test_qa_test_cannot_queue_a_rebuild(self):
        act = Actuators("qa_test", "task-123", dry_run=True)
        with pytest.raises(PermissionError, match="not permitted"):
            act.queue_rebuild("parent-1")

    def test_financial_can_send_alert(self):
        act = Actuators("financial", "task-123", dry_run=True)
        result = act.send_alert("Test", "Body")
        assert result is True

    def test_financial_cannot_queue_a_build(self):
        act = Actuators("financial", "task-123", dry_run=True)
        with pytest.raises(PermissionError):
            act.queue_new_build("cust-1", "sample-app", {})

    def test_architect_readonly(self):
        act = Actuators("architect", "task-123", dry_run=True)
        with pytest.raises(PermissionError):
            act.queue_rebuild("parent-1")
        with pytest.raises(PermissionError):
            act.send_alert("test", "body")


    def test_query_readonly_rejects_mutations(self):
        act = Actuators("qa_test", "task-123", dry_run=True)
        with pytest.raises(PermissionError, match="does not allow mutations"):
            act.query_readonly("DELETE FROM builds WHERE id = %s", ("123",))
        with pytest.raises(PermissionError, match="does not allow mutations"):
            act.query_readonly("DROP TABLE builds", ())

    def test_actions_logged(self):
        act = Actuators("infra_engineer", "task-123", dry_run=True)
        act.record_fix("err", "fix", [], "code_error", True)
        act.send_alert("subj", "body")
        assert len(act.actions) == 2

    def test_all_agents_have_permissions_defined(self):
        # Each of these is checked by at least one Actuators method.
        # tests/test_coordinator_permission_use.py checks every grant against
        # a real handler; this only pins the vocabulary.
        known = {
            "railway_deploy", "railway_env", "email_send",
            "build_queue", "build_annotate", "kb_record", "model_call",
            "finding_write", "approval_request",
        }
        assert AGENT_PERMISSIONS, "the permission table is empty"
        for agent_type, granted in AGENT_PERMISSIONS.items():
            assert isinstance(granted, set), agent_type
            assert granted <= known, f"{agent_type} holds an unrecognised permission: {granted - known}"
        assert set().union(*AGENT_PERMISSIONS.values()) == known

    def test_dry_run_prevents_side_effects(self):
        act = Actuators("infra_engineer", "task-123", dry_run=True)
        with patch("ops.alerter.send_admin_alert") as mailer:
            result = act.send_alert("subject", "body")
        assert result is True
        mailer.assert_not_called()
        assert act.actions[0].details.get("dry_run") is True


# ---------------------------------------------------------------------------
# Tick Lifecycle
# ---------------------------------------------------------------------------

class TestTickLifecycle:
    def test_shutdown_guard(self):
        shutdown = threading.Event()
        shutdown.set()
        result = run_tick(MagicMock(), tick_number=1, shutdown_event=shutdown)
        assert result["skipped"] is True
        assert result["reason"] == "shutdown_requested"

    def test_overlap_protection(self):
        now = datetime.now(timezone.utc)
        result = run_tick(
            MagicMock(), tick_number=1,
            last_tick_time=now - timedelta(seconds=60),  # 1 min ago, < 5 min
        )
        assert result["skipped"] is True
        assert result["reason"] == "overlap_protection"

    @patch("coordinator.tick.build_world_state")
    @patch("coordinator.tick._expire_stale_work")
    @patch("coordinator.tick.detect_work", return_value=[])
    @patch("coordinator.tick._write_tick_record")
    def test_tick_runs_with_no_work(self, mock_write, mock_detect, mock_expire, mock_state):
        mock_state.return_value = WorldState(tick_id="test-tick")
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchall.return_value = []  # no pending work

        result = run_tick(conn, tick_number=1, dry_run=True)
        assert result.get("error") is None
        assert result["tasks_scored"] == 0
        mock_write.assert_called_once()

    @patch("coordinator.tick.build_world_state")
    @patch("coordinator.tick._expire_stale_work")
    @patch("coordinator.tick.detect_work", return_value=[])
    @patch("coordinator.tick._write_tick_record")
    @patch("coordinator.state._load_pending_work")
    def test_tick_dispatches_agent(self, mock_load, mock_write, mock_detect, mock_expire, mock_state):
        # Create a pending work item in state
        task = WorkItem(
            id=str(uuid.uuid4()), category="build_failure",
            target_agent="test_agent", description="test",
            context={}, context_hash="abc123",
            created_at=datetime.now(timezone.utc),
        )
        ws = WorldState(tick_id="test-tick", daily_budget_remaining_usd=20.0)
        ws.pending_work = [task]
        mock_state.return_value = ws

        # Register a mock agent
        from coordinator.agents.base import AgentResult
        mock_agent_cls = MagicMock()
        mock_agent_instance = MagicMock()
        mock_agent_cls.return_value = mock_agent_instance
        mock_agent_instance.execute.return_value = AgentResult(
            success=True, output={"test": True}, api_calls=1, cost_usd=0.05,
        )
        register_agent("test_agent", mock_agent_cls)

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchall.return_value = []

        result = run_tick(conn, tick_number=1, dry_run=True)
        assert result["tasks_dispatched"] >= 1 or result["tasks_scored"] >= 1

        # Cleanup
        from coordinator.tick import _agent_registry
        _agent_registry.pop("test_agent", None)

    def test_is_agent_enabled_default(self):
        # No env var set -> enabled
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_INFRA_ENABLED", None)
            assert _is_agent_enabled("infra_engineer", True) is True

    def test_is_agent_enabled_explicit_false(self):
        with patch.dict(os.environ, {"AGENT_INFRA_ENABLED": "false"}):
            assert _is_agent_enabled("infra_engineer", True) is False

    def test_is_agent_enabled_explicit_true(self):
        with patch.dict(os.environ, {"AGENT_INFRA_ENABLED": "true"}):
            assert _is_agent_enabled("infra_engineer", True) is True

    @patch("coordinator.tick.build_world_state")
    def test_tick_handles_exception(self, mock_state):
        mock_state.side_effect = Exception("snapshot failed")
        conn = MagicMock()
        result = run_tick(conn, tick_number=1, dry_run=True)
        assert "error" in result




    @patch("coordinator.tick.build_world_state")
    @patch("coordinator.tick._expire_stale_work")
    @patch("coordinator.tick.detect_work", return_value=[])
    @patch("coordinator.tick._write_tick_record")
    @patch("coordinator.state._load_pending_work")
    @patch("coordinator.tick.time.sleep")
    def test_failed_agent_does_not_poison_next_agent(
        self, mock_sleep, mock_load, mock_write, mock_detect, mock_expire, mock_state
    ):
        """A failed agent must rollback so subsequent agents get a clean connection."""
        from coordinator.agents.base import AgentResult
        from coordinator.tick import _agent_registry

        # Use real agent names so select_model finds them in AGENT_MODELS
        task_a = WorkItem(
            id=str(uuid.uuid4()), category="build_failure",
            target_agent="infra_engineer", description="will fail",
            context={}, context_hash="aaa",
            created_at=datetime.now(timezone.utc),
        )
        task_b = WorkItem(
            id=str(uuid.uuid4()), category="build_failure",
            target_agent="financial", description="should succeed",
            context={}, context_hash="bbb",
            created_at=datetime.now(timezone.utc),
        )

        ws = WorldState(tick_id="test-tick", daily_budget_remaining_usd=20.0)
        ws.pending_work = [task_a, task_b]
        mock_state.return_value = ws

        # Save originals to restore later
        orig_infra = _agent_registry.get("infra_engineer")
        orig_fin = _agent_registry.get("financial")

        # infra_engineer raises during dispatch (simulates a DB error mid-query)
        poison_cls = MagicMock()
        poison_cls.return_value.execute.side_effect = RuntimeError("DB column missing")

        # financial agent succeeds
        healthy_cls = MagicMock()
        healthy_cls.return_value.execute.return_value = AgentResult(
            success=True, output={"ok": True}, api_calls=1, cost_usd=0.02,
        )

        _agent_registry["infra_engineer"] = poison_cls
        _agent_registry["financial"] = healthy_cls

        try:
            conn = MagicMock()
            cur = MagicMock()
            conn.cursor.return_value = cur
            cur.fetchall.return_value = []  # no failure history, no pending work reload

            result = run_tick(conn, tick_number=1, dry_run=True)

            # conn.rollback() must have been called after infra_engineer failed,
            # clearing the failed transaction state before financial runs.
            conn.rollback.assert_called()

            # Both agents should have been attempted (one failed, one dispatched)
            assert result["tasks_dispatched"] >= 1
        finally:
            # Restore original registry entries
            if orig_infra is not None:
                _agent_registry["infra_engineer"] = orig_infra
            else:
                _agent_registry.pop("infra_engineer", None)
            if orig_fin is not None:
                _agent_registry["financial"] = orig_fin
            else:
                _agent_registry.pop("financial", None)


# ---------------------------------------------------------------------------
# WorkItem Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_persist_work_item(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        cur.fetchone.return_value = ("some-id",)  # INSERT...RETURNING id

        item = WorkItem(
            category="build_failure",
            target_agent="infra_engineer",
            description="Test item",
            context={"build_id": "123"},
        )
        _persist_work_item(conn, item, "tick-1")
        cur.execute.assert_called_once()
        sql = cur.execute.call_args[0][0]
        assert "INSERT INTO agent_tasks" in sql

    def test_expire_stale_work(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        cur.fetchall.return_value = [("build_failure",), ("build_failure",), ("cost_projection",)]

        _expire_stale_work(conn, "tick-1")
        sql = cur.execute.call_args_list[0][0][0]
        assert "UPDATE agent_tasks SET status = 'expired'" in sql


# ---------------------------------------------------------------------------
# TestTransactionPoisoningGuards: regression for: 0 completed ticks because
# silent exceptions in helpers left psycopg2 conn in InFailedSqlTransaction
# state, cascading into _write_tick_record and preventing `completed_at` from
# ever being written (82 ticks, 0 completions observed on prod 2026-04-18).
# ---------------------------------------------------------------------------

class TestTransactionPoisoningGuards:
    """Each helper that catches a SQL exception MUST rollback the conn or
    every downstream operation (including the final tick record UPDATE)
    fails with 'current transaction is aborted'."""

    def _failing_conn(self):
        """A conn whose first cursor().execute() throws, simulating a schema
        mismatch or constraint violation that poisons the transaction."""
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute.side_effect = Exception("simulated SQL error")
        conn.cursor.return_value = cur
        return conn

    def test_build_performance_cache_rolls_back_on_error(self):
        """build_performance_cache's bare-except must rollback or the next
        op (scoring / dispatch) fails with InFailedSqlTransaction."""
        from coordinator.scorer import build_performance_cache
        conn = self._failing_conn()
        result = build_performance_cache(conn)
        assert result == {}
        conn.rollback.assert_called_once()

    def test_write_tick_record_defensively_rolls_back_before_update(self):
        """_write_tick_record MUST rollback before running the UPDATE so a
        prior silent poisoning doesn't prevent the tick from completing."""
        from coordinator.tick import _write_tick_record
        from coordinator.budget import TickBudget
        from datetime import datetime, timezone

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur

        state = WorldState(
            tick_id="t", tick_number=1, tick_start=datetime.now(timezone.utc),
            queued_builds=0, active_builds=0, failed_builds_24h=[],
            preview_awaiting=0, abandoned_intakes_24h=0, pending_feedback=0,
            monitored_apps_down=[], monitored_apps_degraded=[],
            api_cost_today_usd=0.0, daily_budget_remaining_usd=20.0,
            agent_success_rates={}, agent_avg_cost={},
            pending_work=[], previous_tick_summary={},
        )
        budget = TickBudget(max_api_calls=10, max_cost_usd=5.0,
                            max_opus_calls=5, allowed_models=["claude-opus-4-6"])

        _write_tick_record(
            conn, "tick-id", 1, datetime.now(timezone.utc),
            datetime.now(timezone.utc), 1.5, state, budget,
            tasks_scored=0, tasks_dispatched=0, tasks_skipped=0,
            tasks_completed=0, total_cost=0.0, decisions=[], errors=[],
        )
        # The defensive rollback must happen BEFORE the UPDATE executes.
        assert conn.rollback.called, \
            "_write_tick_record must rollback before UPDATE to clear any poisoned txn state"



# ---------------------------------------------------------------------------
# TestSurfaceInitiativeFindings: agent initiative output.findings list must
# land in coordinator_findings so /admin/findings can review + promote to
# roadmap items. Without this, agent insights (QA warning rate, retry rate,
# cost projections) are only stored in agent_tasks.result JSONB, invisible.
# ---------------------------------------------------------------------------

class TestSurfaceInitiativeFindings:
    """_surface_initiative_findings routes AgentResult.output['findings'] list
    into coordinator_findings via Actuators.finding_add()."""

    def _make_task(self, agent="backend_engineer"):
        from datetime import datetime, timezone
        return WorkItem(
            category="agent_initiative",
            target_agent=agent,
            description=f"Initiative: {agent} autonomous work",
            context={"agent": agent},
            context_hash="abc123",
            created_at=datetime.now(timezone.utc),
            id="task-init-1",
        )

    def _make_result(self, findings=None, success=True, output_extra=None):
        from coordinator.agents.base import AgentResult
        output = {"findings": findings} if findings is not None else {}
        if output_extra:
            output.update(output_extra)
        return AgentResult(success=success, output=output)

    def test_empty_findings_list_surfaces_nothing(self):
        task = self._make_task()
        result = self._make_result(findings=[])
        with patch("coordinator.tick.Actuators") as MockAct:
            surfaced = _surface_initiative_findings(task, result, dry_run=True)
        assert surfaced == 0
        # Should NOT even construct an Actuators for empty findings
        MockAct.assert_not_called()

    def test_missing_findings_key_surfaces_nothing(self):
        task = self._make_task()
        result = self._make_result(output_extra={"agent_cost": 1.11})  # no 'findings' key
        with patch("coordinator.tick.Actuators") as MockAct:
            surfaced = _surface_initiative_findings(task, result, dry_run=True)
        assert surfaced == 0
        MockAct.assert_not_called()

    def test_findings_wrong_type_surfaces_nothing(self):
        """If findings is a dict/string/None (not a list), skip cleanly."""
        task = self._make_task()
        for bad_findings in [None, "just a string", {"not": "a list"}, 42]:
            result = self._make_result(output_extra={"findings": bad_findings})
            with patch("coordinator.tick.Actuators") as MockAct:
                surfaced = _surface_initiative_findings(task, result, dry_run=True)
            assert surfaced == 0, f"bad findings {bad_findings!r} should surface 0"

    def test_failed_agent_results_skipped(self):
        """Findings from a FAILED agent run shouldn't clutter the findings queue."""
        task = self._make_task()
        result = self._make_result(findings=["should not surface"], success=False)
        with patch("coordinator.tick.Actuators") as MockAct:
            surfaced = _surface_initiative_findings(task, result, dry_run=True)
        assert surfaced == 0
        MockAct.assert_not_called()

    def test_nonempty_findings_calls_finding_add_per_item(self):
        task = self._make_task("backend_engineer")
        findings = [
            "QA warnings: 3 builds warned in last 7 days",
            "Retry rate elevated: 0.8 avg",
        ]
        result = self._make_result(findings=findings)

        mock_act = MagicMock()
        mock_act.finding_add = MagicMock(return_value=True)
        with patch("coordinator.tick.Actuators", return_value=mock_act):
            surfaced = _surface_initiative_findings(task, result, dry_run=False)

        assert surfaced == 2
        assert mock_act.finding_add.call_count == 2
        # Verify each call has the right shape
        for i, call in enumerate(mock_act.finding_add.call_args_list):
            kwargs = call.kwargs
            assert kwargs["description"] == findings[i]
            assert kwargs["priority"] == "medium"
            assert kwargs["category"] == "initiative_backend_engineer"
            assert kwargs["source_task_id"] == "task-init-1"
            assert kwargs["finding_key"].startswith("init-backend_engineer-")
            assert kwargs["title"].startswith("[backend_engineer]")

    def test_finding_key_is_stable_across_runs(self):
        """Same finding text → same finding_key → unique-index dedup."""
        task = self._make_task("architect")
        result = self._make_result(findings=["QA warning rate elevated: 3/5 (60%)"])

        captured_keys = []
        def capture(**kwargs):
            captured_keys.append(kwargs["finding_key"])
            return True
        mock_act = MagicMock()
        mock_act.finding_add = capture

        with patch("coordinator.tick.Actuators", return_value=mock_act):
            _surface_initiative_findings(task, result, dry_run=False)
            _surface_initiative_findings(task, result, dry_run=False)

        assert len(captured_keys) == 2
        assert captured_keys[0] == captured_keys[1], \
            "Stable keys required for ON CONFLICT dedup"

    def test_different_text_produces_different_keys(self):
        task = self._make_task("architect")
        result = self._make_result(findings=["Finding A", "Finding B"])
        captured_keys = []
        def capture(**kwargs):
            captured_keys.append(kwargs["finding_key"])
            return True
        mock_act = MagicMock()
        mock_act.finding_add = capture

        with patch("coordinator.tick.Actuators", return_value=mock_act):
            _surface_initiative_findings(task, result, dry_run=False)
        assert len(set(captured_keys)) == 2, "Different findings must produce unique keys"

    def test_empty_string_findings_filtered(self):
        task = self._make_task()
        result = self._make_result(findings=["", "   ", "real finding", None, 42])
        mock_act = MagicMock()
        mock_act.finding_add = MagicMock(return_value=True)
        with patch("coordinator.tick.Actuators", return_value=mock_act):
            surfaced = _surface_initiative_findings(task, result, dry_run=False)
        assert surfaced == 1
        assert mock_act.finding_add.call_count == 1
        assert mock_act.finding_add.call_args.kwargs["description"] == "real finding"

    def test_finding_add_returning_false_counted_as_not_surfaced(self):
        """finding_add returns False only on unrecoverable error (no DATABASE_URL, etc)."""
        task = self._make_task()
        result = self._make_result(findings=["a finding"])
        mock_act = MagicMock()
        mock_act.finding_add = MagicMock(return_value=False)
        with patch("coordinator.tick.Actuators", return_value=mock_act):
            surfaced = _surface_initiative_findings(task, result, dry_run=False)
        assert surfaced == 0  # called but didn't succeed

    def test_permission_error_does_not_crash(self):
        """If an agent loses finding_write permission, surfacing must not break the tick."""
        task = self._make_task()
        result = self._make_result(findings=["a finding"])
        mock_act = MagicMock()
        mock_act.finding_add = MagicMock(side_effect=PermissionError("no finding_write"))
        with patch("coordinator.tick.Actuators", return_value=mock_act):
            # Must not raise
            surfaced = _surface_initiative_findings(task, result, dry_run=False)
        assert surfaced == 0

    def test_db_exception_does_not_crash(self):
        """Unexpected DB exception during finding_add must not bubble up."""
        task = self._make_task()
        result = self._make_result(findings=["a finding"])
        mock_act = MagicMock()
        mock_act.finding_add = MagicMock(side_effect=RuntimeError("db went away"))
        with patch("coordinator.tick.Actuators", return_value=mock_act):
            surfaced = _surface_initiative_findings(task, result, dry_run=False)
        assert surfaced == 0

    def test_title_truncated_to_100_chars(self):
        """Long finding text becomes a 100-char title + full description."""
        task = self._make_task()
        long = "A" * 200
        result = self._make_result(findings=[long])
        mock_act = MagicMock()
        mock_act.finding_add = MagicMock(return_value=True)
        with patch("coordinator.tick.Actuators", return_value=mock_act):
            _surface_initiative_findings(task, result, dry_run=False)
        kwargs = mock_act.finding_add.call_args.kwargs
        # Title is "[agent] " prefix + first 100 chars of finding
        assert kwargs["description"] == long  # full text preserved
        assert len(kwargs["title"]) <= len("[backend_engineer] ") + 100

    def test_dry_run_flag_passed_through_to_actuators(self):
        task = self._make_task()
        result = self._make_result(findings=["a finding"])
        with patch("coordinator.tick.Actuators") as MockAct:
            MockAct.return_value.finding_add = MagicMock(return_value=True)
            _surface_initiative_findings(task, result, dry_run=True)
        # Confirm the Actuators was instantiated with dry_run=True
        assert MockAct.call_args.kwargs["dry_run"] is True

    def test_category_uses_agent_name(self):
        """Category is initiative_<agent> so admin can filter by agent-specific findings."""
        for agent in ["security", "cost_analyst", "architect"]:
            task = self._make_task(agent)
            result = self._make_result(findings=["x"])
            mock_act = MagicMock()
            mock_act.finding_add = MagicMock(return_value=True)
            with patch("coordinator.tick.Actuators", return_value=mock_act):
                _surface_initiative_findings(task, result, dry_run=False)
            assert mock_act.finding_add.call_args.kwargs["category"] == f"initiative_{agent}"


# ---------------------------------------------------------------------------
# Actuator method: queue_rebuild
# ---------------------------------------------------------------------------

class TestActuatorQueueRebuild:
    def test_dry_run_returns_none(self):
        act = Actuators("backend_engineer", "task-1", dry_run=True)
        result = act.queue_rebuild("parent-123", "fix something")
        assert result is None
        assert any(a.action_type == "build_queue" for a in act.actions)

    def test_permission_denied_for_qa(self):
        act = Actuators("qa_test", "task-1", dry_run=True)
        with pytest.raises(PermissionError, match="not permitted"):
            act.queue_rebuild("parent-123")

    def test_backend_engineer_has_build_queue_permission(self):
        assert "build_queue" in AGENT_PERMISSIONS["backend_engineer"]

    def test_infra_does_not_have_build_queue(self):
        assert "build_queue" not in AGENT_PERMISSIONS["infra_engineer"]


# ---------------------------------------------------------------------------
# New Work Detection Triggers
# ---------------------------------------------------------------------------


class TestWeeklyCostProjection:
    def test_triggers_on_friday_10am(self):
        # Friday at 10:00 UTC
        friday_10am = datetime(2026, 4, 3, 10, 0, tzinfo=timezone.utc)
        last_tick = datetime(2026, 4, 3, 9, 30, tzinfo=timezone.utc)

        items = _check_weekly_cost_projection(friday_10am, last_tick)

        assert len(items) == 1
        assert items[0].category == "cost_projection"
        assert items[0].target_agent == "cost_analyst"

    def test_does_not_trigger_on_other_days(self):
        # Monday at 10:00 UTC
        monday = datetime(2026, 3, 30, 10, 0, tzinfo=timezone.utc)
        items = _check_weekly_cost_projection(monday, None)
        assert len(items) == 0

    def test_does_not_trigger_at_other_hours(self):
        # Friday at 15:00 UTC
        friday_3pm = datetime(2026, 4, 3, 15, 0, tzinfo=timezone.utc)
        items = _check_weekly_cost_projection(friday_3pm, None)
        assert len(items) == 0


# ---------------------------------------------------------------------------
# New Scorer Categories
# ---------------------------------------------------------------------------

class TestNewScorerCategories:


    def test_cost_projection_weight_exists(self):
        assert "cost_projection" in CATEGORY_WEIGHTS
        assert CATEGORY_WEIGHTS["cost_projection"] > 0

    def test_cost_analyst_model_exists(self):
        assert "cost_analyst" in AGENT_MODELS
        default_model = AGENT_MODELS["cost_analyst"][0]
        assert default_model == "claude-haiku-4-5"

    def test_cost_analyst_enabled_env_var(self):
        with patch.dict(os.environ, {"AGENT_COST_ENABLED": "true"}):
            assert _is_agent_enabled("cost_analyst", True) is True
        with patch.dict(os.environ, {"AGENT_COST_ENABLED": "false"}):
            assert _is_agent_enabled("cost_analyst", True) is False


    def test_initiative_lower_priority_than_reactive(self):
        assert CATEGORY_WEIGHTS["agent_initiative"] < CATEGORY_WEIGHTS["build_failure"]
        assert CATEGORY_WEIGHTS["agent_initiative"] == min(CATEGORY_WEIGHTS.values())


class TestAgentInitiative:
    def test_no_initiative_when_reactive_work_pending(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        # Reactive work exists
        cur.fetchone.return_value = (3,)

        items = _check_agent_initiative(conn)
        assert len(items) == 0

    def test_no_initiative_when_already_running(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        # No reactive work, but initiative already running
        cur.fetchone.side_effect = [(0,), (1,)]

        items = _check_agent_initiative(conn)
        assert len(items) == 0

    def test_creates_initiative_when_idle(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        # No reactive, no running initiative, no history
        cur.fetchone.side_effect = [(0,), (0,)]
        cur.fetchall.return_value = []

        items = _check_agent_initiative(conn)
        assert len(items) == 1
        assert items[0].category == "agent_initiative"
        assert items[0].target_agent in [
            "backend_engineer", "security", "cost_analyst",
            "customer_service", "documentation", "qa_test",
            "financial", "infra_engineer",
        ]

    def test_handles_db_error(self):
        conn = MagicMock()
        conn.cursor.side_effect = Exception("DB error")

        items = _check_agent_initiative(conn)
        assert len(items) == 0


# ---------------------------------------------------------------------------
# Parallel Dispatch
# ---------------------------------------------------------------------------

class TestGroupIndependentTasks:
    """Tests for _group_independent_tasks() batching logic."""

    def _make_item(self, agent, category, task_id=None):
        task = WorkItem(
            id=task_id or str(uuid.uuid4()),
            category=category,
            target_agent=agent,
            description="test",
            context={},
            context_hash="h",
            created_at=datetime.now(timezone.utc),
        )
        return (0.5, task)

    def test_different_agents_same_batch(self):
        items = [
            self._make_item("infra_engineer", "build_failure"),
            self._make_item("security", "security_scan"),
        ]
        batches = _group_independent_tasks(items)
        assert len(batches) == 1
        assert len(batches[0]) == 2

    def test_same_agent_separate_batches(self):
        items = [
            self._make_item("infra_engineer", "build_failure"),
            self._make_item("infra_engineer", "app_down"),
        ]
        batches = _group_independent_tasks(items)
        assert len(batches) == 2
        assert len(batches[0]) == 1
        assert len(batches[1]) == 1

    def test_same_category_separate_batches(self):
        items = [
            self._make_item("infra_engineer", "build_failure"),
            self._make_item("security", "build_failure"),
        ]
        batches = _group_independent_tasks(items)
        assert len(batches) == 2

    def test_empty_items(self):
        batches = _group_independent_tasks([])
        assert batches == []

    def test_single_item(self):
        items = [self._make_item("infra_engineer", "build_failure")]
        batches = _group_independent_tasks(items)
        assert len(batches) == 1
        assert len(batches[0]) == 1

    def test_three_independent_agents(self):
        items = [
            self._make_item("infra_engineer", "build_failure"),
            self._make_item("security", "security_scan"),
            self._make_item("financial", "cost_report"),
        ]
        batches = _group_independent_tasks(items)
        assert len(batches) == 1
        assert len(batches[0]) == 3

    def test_mixed_independent_and_dependent(self):
        items = [
            self._make_item("infra_engineer", "build_failure"),
            self._make_item("security", "security_scan"),
            self._make_item("infra_engineer", "app_down"),  # conflicts with first (same agent)
        ]
        batches = _group_independent_tasks(items)
        # First batch: infra + security; second batch: infra (app_down)
        assert len(batches) == 2
        assert len(batches[0]) == 2
        assert len(batches[1]) == 1


class TestCoordinatorMaxParallel:
    def test_default_value(self):
        assert COORDINATOR_MAX_PARALLEL >= 1

    def test_env_var_override(self):
        with patch.dict(os.environ, {"COORDINATOR_MAX_PARALLEL": "5"}):
            # Re-import to pick up the env var
            import importlib
            import coordinator.tick as tick_mod
            importlib.reload(tick_mod)
            assert tick_mod.COORDINATOR_MAX_PARALLEL == 5
            # Restore
            importlib.reload(tick_mod)


class TestDispatchOneParallel:
    """Tests for _dispatch_one_parallel() thread worker."""

    def test_skips_on_shutdown(self):
        shutdown = threading.Event()
        shutdown.set()
        task = WorkItem(
            id="task-1", category="test", target_agent="test_agent",
            description="d", context={}, context_hash="h",
            created_at=datetime.now(timezone.utc),
        )
        result = _dispatch_one_parallel(
            task, "test_agent", "claude-haiku-4-5", None, True, "tick-1",
            stagger_delay=0, shutdown_event=shutdown,
        )
        assert result["skipped"] is True

    @patch("coordinator.tick._create_thread_conn")
    @patch("coordinator.tick._dispatch_agent")
    def test_successful_dispatch(self, mock_dispatch, mock_conn):
        from coordinator.agents.base import AgentResult
        mock_thread_conn = MagicMock()
        mock_conn.return_value = mock_thread_conn

        mock_dispatch.return_value = AgentResult(
            success=True, output={"ok": True}, api_calls=1, cost_usd=0.05,
        )

        task = WorkItem(
            id="task-1", category="build_failure", target_agent="infra_engineer",
            description="d", context={}, context_hash="h",
            created_at=datetime.now(timezone.utc),
        )

        result = _dispatch_one_parallel(
            task, "infra_engineer", "claude-haiku-4-5", None, True, "tick-1",
            stagger_delay=0, shutdown_event=None,
        )

        assert result["decision"] == "dispatch"
        assert result["success"] is True
        assert result["actual_cost_usd"] == 0.05
        mock_thread_conn.commit.assert_called()
        mock_thread_conn.close.assert_called()

    @patch("coordinator.tick._create_thread_conn")
    @patch("coordinator.tick._dispatch_agent")
    def test_dispatch_error_returns_error_outcome(self, mock_dispatch, mock_conn):
        mock_thread_conn = MagicMock()
        mock_conn.return_value = mock_thread_conn
        mock_dispatch.side_effect = RuntimeError("agent crashed")

        task = WorkItem(
            id="task-1", category="build_failure", target_agent="infra_engineer",
            description="d", context={}, context_hash="h",
            created_at=datetime.now(timezone.utc),
        )

        result = _dispatch_one_parallel(
            task, "infra_engineer", "claude-haiku-4-5", None, True, "tick-1",
            stagger_delay=0, shutdown_event=None,
        )

        assert result["decision"] == "error"
        assert "agent crashed" in result["error"]
        mock_thread_conn.rollback.assert_called()
        mock_thread_conn.close.assert_called()

    @patch("coordinator.tick._create_thread_conn")
    @patch("coordinator.tick._dispatch_agent")
    def test_connection_closed_on_success(self, mock_dispatch, mock_conn):
        from coordinator.agents.base import AgentResult
        mock_thread_conn = MagicMock()
        mock_conn.return_value = mock_thread_conn
        mock_dispatch.return_value = AgentResult(
            success=True, output={}, api_calls=1, cost_usd=0.01,
        )

        task = WorkItem(
            id="t1", category="test", target_agent="a",
            description="d", context={}, context_hash="h",
            created_at=datetime.now(timezone.utc),
        )
        _dispatch_one_parallel(
            task, "a", "claude-haiku-4-5", None, True, "tick-1",
            stagger_delay=0, shutdown_event=None,
        )
        mock_thread_conn.close.assert_called_once()


class TestParallelTickIntegration:
    """Integration tests: run_tick dispatches independent tasks in parallel."""

    @patch("coordinator.tick.build_world_state")
    @patch("coordinator.tick._expire_stale_work")
    @patch("coordinator.tick.detect_work", return_value=[])
    @patch("coordinator.tick._write_tick_record")
    @patch("coordinator.state._load_pending_work")
    @patch("coordinator.tick._create_thread_conn")
    @patch.dict(os.environ, {"DATABASE_URL": "postgresql://test@localhost/test"})
    def test_parallel_dispatch_for_independent_tasks(
        self, mock_thread_conn, mock_load, mock_write, mock_detect, mock_expire, mock_state,
    ):
        """Two tasks with different agents dispatch in parallel."""
        from coordinator.agents.base import AgentResult

        task1 = WorkItem(
            id=str(uuid.uuid4()), category="build_failure",
            target_agent="agent_a", description="fix build",
            context={}, context_hash="h1",
            created_at=datetime.now(timezone.utc),
        )
        task2 = WorkItem(
            id=str(uuid.uuid4()), category="security_scan",
            target_agent="agent_b", description="scan",
            context={}, context_hash="h2",
            created_at=datetime.now(timezone.utc),
        )

        ws = WorldState(tick_id="test-tick", daily_budget_remaining_usd=20.0)
        ws.pending_work = [task1, task2]
        mock_state.return_value = ws

        # Mock thread connections
        thread_conn = MagicMock()
        thread_conn.cursor.return_value = MagicMock(fetchall=MagicMock(return_value=[]))
        mock_thread_conn.return_value = thread_conn

        # Register two mock agents and add them to AGENT_MODELS
        mock_cls_a = MagicMock()
        mock_cls_a.return_value.execute.return_value = AgentResult(
            success=True, output={}, api_calls=1, cost_usd=0.05,
        )
        mock_cls_b = MagicMock()
        mock_cls_b.return_value.execute.return_value = AgentResult(
            success=True, output={}, api_calls=1, cost_usd=0.03,
        )
        register_agent("agent_a", mock_cls_a)
        register_agent("agent_b", mock_cls_b)

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchall.return_value = []

        try:
            with patch.dict(AGENT_MODELS, {
                "agent_a": ("claude-haiku-4-5", None, None),
                "agent_b": ("claude-haiku-4-5", None, None),
            }):
                result = run_tick(conn, tick_number=1, dry_run=True)
            assert result.get("error") is None
            assert result["tasks_dispatched"] == 2
            assert result["tasks_completed"] == 2
        finally:
            from coordinator.tick import _agent_registry
            _agent_registry.pop("agent_a", None)
            _agent_registry.pop("agent_b", None)

    @patch("coordinator.tick.build_world_state")
    @patch("coordinator.tick._expire_stale_work")
    @patch("coordinator.tick.detect_work", return_value=[])
    @patch("coordinator.tick._write_tick_record")
    @patch("coordinator.state._load_pending_work")
    def test_sequential_fallback_same_agent(
        self, mock_load, mock_write, mock_detect, mock_expire, mock_state,
    ):
        """Two tasks targeting the same agent dispatch sequentially."""
        from coordinator.agents.base import AgentResult

        task1 = WorkItem(
            id=str(uuid.uuid4()), category="build_failure",
            target_agent="seq_agent", description="fix 1",
            context={}, context_hash="h1",
            created_at=datetime.now(timezone.utc),
        )
        task2 = WorkItem(
            id=str(uuid.uuid4()), category="app_down",
            target_agent="seq_agent", description="fix 2",
            context={}, context_hash="h2",
            created_at=datetime.now(timezone.utc),
        )

        ws = WorldState(tick_id="test-tick", daily_budget_remaining_usd=20.0)
        ws.pending_work = [task1, task2]
        mock_state.return_value = ws

        mock_cls = MagicMock()
        mock_cls.return_value.execute.return_value = AgentResult(
            success=True, output={}, api_calls=1, cost_usd=0.05,
        )
        register_agent("seq_agent", mock_cls)

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchall.return_value = []

        try:
            with patch.dict(AGENT_MODELS, {
                "seq_agent": ("claude-haiku-4-5", None, None),
            }):
                result = run_tick(conn, tick_number=1, dry_run=True)
            assert result.get("error") is None
            assert result["tasks_dispatched"] == 2
        finally:
            from coordinator.tick import _agent_registry
            _agent_registry.pop("seq_agent", None)

    @patch("coordinator.tick.build_world_state")
    @patch("coordinator.tick._expire_stale_work")
    @patch("coordinator.tick.detect_work", return_value=[])
    @patch("coordinator.tick._write_tick_record")
    @patch("coordinator.state._load_pending_work")
    def test_budget_respected_across_parallel_tasks(
        self, mock_load, mock_write, mock_detect, mock_expire, mock_state,
    ):
        """Budget limits prevent dispatching tasks that exceed remaining budget."""
        from coordinator.agents.base import AgentResult

        task1 = WorkItem(
            id=str(uuid.uuid4()), category="build_failure",
            target_agent="budget_agent", description="expensive",
            context={}, context_hash="h1",
            created_at=datetime.now(timezone.utc),
        )

        ws = WorldState(tick_id="test-tick", daily_budget_remaining_usd=0.001)
        ws.pending_work = [task1]
        mock_state.return_value = ws

        mock_cls = MagicMock()
        mock_cls.return_value.execute.return_value = AgentResult(
            success=True, output={}, api_calls=1, cost_usd=0.05,
        )
        register_agent("budget_agent", mock_cls)

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchall.return_value = []

        try:
            result = run_tick(conn, tick_number=1, dry_run=True)
            # Task should be skipped due to budget
            assert result["tasks_skipped"] >= 1
        finally:
            from coordinator.tick import _agent_registry
            _agent_registry.pop("budget_agent", None)


# ---------------------------------------------------------------------------
# Escalation / Approval Requests
# ---------------------------------------------------------------------------

class TestEscalationEmail:
    """Tests for create_approval_request and _check_escalation."""

    def test_create_approval_request_dry_run(self):
        """Dry-run mode logs but does not insert or send email."""
        act = Actuators(agent_name="infra_engineer", task_id="task-1", dry_run=True)
        result = act.create_approval_request("budget_exceeded", "Cost too high", {"cost": 10})
        assert result is None
        assert len(act.actions) == 1
        assert act.actions[0].action_type == "approval_request"
        assert act.actions[0].details["dry_run"] is True

    @patch("coordinator.actuators._send_escalation_email", return_value=True)
    @patch("coordinator.actuators.psycopg2")
    def test_create_approval_request_inserts_and_emails(self, mock_pg, mock_email):
        """Live mode inserts a row and sends an escalation email."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_pg.connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
            act = Actuators(agent_name="infra_engineer", task_id="task-2", dry_run=False)
            req_id = act.create_approval_request(
                "risky_deploy", "Deploying unverified code", {"service": "web"}
            )

        assert req_id is not None
        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args[0][0]
        assert "INSERT INTO approval_requests" in sql
        mock_conn.commit.assert_called_once()
        mock_conn.close.assert_called_once()

        mock_email.assert_called_once()
        call_args = mock_email.call_args
        assert call_args[0][0] == "risky_deploy"
        assert "Deploying unverified code" in call_args[0][1]

    @patch("coordinator.actuators._send_escalation_email", return_value=True)
    @patch("coordinator.actuators.psycopg2")
    def test_create_approval_request_no_db_url(self, mock_pg, mock_email):
        """Returns None when DATABASE_URL is not set."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            act = Actuators(agent_name="infra_engineer", task_id="task-3", dry_run=False)
            req_id = act.create_approval_request("test", "desc", {})
        assert req_id is None
        mock_email.assert_not_called()

    @patch("coordinator.actuators._send_escalation_email", return_value=True)
    @patch("coordinator.actuators.psycopg2")
    def test_create_approval_request_db_error(self, mock_pg, mock_email):
        """Returns None on DB insert failure."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_pg.connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cur
        mock_cur.execute.side_effect = Exception("DB error")

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
            act = Actuators(agent_name="infra_engineer", task_id="task-4", dry_run=False)
            req_id = act.create_approval_request("test", "desc", {})
        assert req_id is None
        mock_conn.rollback.assert_called_once()
        mock_email.assert_not_called()

    def test_send_escalation_email_no_api_key(self):
        """Skips email when RESEND_API_KEY is not set."""
        from coordinator.actuators import _send_escalation_email
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RESEND_API_KEY", None)
            result = _send_escalation_email("test", "desc", {}, "req-id")
        assert result is False

    @patch("coordinator.actuators.bounded_urlopen")
    def test_send_escalation_email_success(self, mock_urlopen):
        """Sends email via Resend when API key is present."""
        from coordinator.actuators import _send_escalation_email
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test", "ADMIN_EMAIL": "admin@test.example",
                                     "BASE_URL": "https://cas.example.test/"}):
            result = _send_escalation_email(
                "budget_exceeded", "Cost is $10", {"cost": 10}, "req-123"
            )
        assert result is True
        mock_urlopen.assert_called_once()
        req_obj = mock_urlopen.call_args[0][0]
        body = json.loads(req_obj.data.decode())
        assert body["to"] == ["admin@test.example"]
        assert "budget_exceeded" in body["subject"]
        assert "Review in Admin" in body["html"]
        assert 'href="https://cas.example.test/admin/approvals"' in body["html"]

    @patch("coordinator.actuators.bounded_urlopen")
    def test_send_escalation_email_network_error(self, mock_urlopen):
        """Returns False on network failure."""
        from coordinator.actuators import _send_escalation_email
        from urllib.error import URLError
        mock_urlopen.side_effect = URLError("timeout")

        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test"}):
            result = _send_escalation_email("test", "desc", {}, "req-id")
        assert result is False


class TestCheckEscalation:
    """Tests for _check_escalation in tick.py."""

    def _make_task(self):
        return WorkItem(
            id=str(uuid.uuid4()), category="build_failure",
            target_agent="infra_engineer", description="Build failed",
            context={}, context_hash="hash1",
            created_at=datetime.now(timezone.utc),
        )

    @patch("coordinator.tick.Actuators")
    def test_escalation_on_agent_flag(self, MockActuators):
        """Creates approval request when result.escalate is True."""
        from coordinator.tick import _check_escalation
        from coordinator.agents.base import AgentResult

        task = self._make_task()
        result = AgentResult(
            success=True, cost_usd=0.50,
            escalate=True, escalation_reason="Security risk detected",
        )

        _check_escalation(task, result, "infra_engineer", dry_run=False)

        mock_instance = MockActuators.return_value
        mock_instance.create_approval_request.assert_called_once()
        kwargs = mock_instance.create_approval_request.call_args[1]
        assert kwargs["request_type"] == "escalation:infra_engineer"
        assert "Security risk detected" in kwargs["description"]


    @patch("coordinator.tick.Actuators")
    def test_a_result_that_does_not_ask_is_not_escalated(self, MockActuators):
        """The agent's own flag is the only thing that files a request. A cost
        figure on the result is not one: no agent reports a cost, and the
        threshold that compared it was a control nothing could trip."""
        from coordinator.tick import _check_escalation
        from coordinator.agents.base import AgentResult

        task = self._make_task()
        for cost in (0.0, 1.0, 10.0, 1000.0):
            _check_escalation(task, AgentResult(success=True, cost_usd=cost, escalate=False),
                              "infra_engineer", dry_run=False)

        MockActuators.assert_not_called()


    def test_esc_html_escaping(self):
        """_esc properly escapes HTML special characters."""
        from coordinator.actuators import _esc
        assert _esc('<script>alert("xss")</script>') == \
               '&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;'


# ---------------------------------------------------------------------------
# Section 02: Config Module
# ---------------------------------------------------------------------------

class TestConfigModule:
    def test_defaults(self):
        """Config returns default values when no env vars set."""
        assert config.FAILURE_RATE_WINDOW == 10
        assert config.FAILURE_RATE_THRESHOLD == 0.8
        assert config.STALLED_MINUTES == 30
        assert config.WORK_EXPIRY_HOURS == 2
        assert config.STUCK_TASK_HOURS == 4

    @patch.dict(os.environ, {"COORDINATOR_STALLED_MINUTES": "48"})
    def test_env_override(self):
        """Config reads from env vars when set."""
        from coordinator.config import _int
        assert _int("COORDINATOR_STALLED_MINUTES", 30, min_val=1) == 48

    def test_failure_rate_threshold_clamped(self):
        from coordinator.config import _float
        assert _float("TEST_KEY", 0.8, min_val=0.0, max_val=1.0) == 0.8

    @patch.dict(os.environ, {"TEST_NEG": "-5"})
    def test_stalled_minutes_rejects_negative(self):
        from coordinator.config import _int
        result = _int("TEST_NEG", 30, min_val=1)
        assert result == 30  # Falls back to default

    @patch.dict(os.environ, {"TEST_FLOAT_HIGH": "1.5"})
    def test_float_clamps_above_max(self):
        from coordinator.config import _float
        result = _float("TEST_FLOAT_HIGH", 0.8, min_val=0.0, max_val=1.0)
        assert result == 1.0  # Clamped to max

    @patch.dict(os.environ, {"TEST_BAD": "notanumber"})
    def test_invalid_value_returns_default(self):
        from coordinator.config import _int, _float
        assert _int("TEST_BAD", 42) == 42
        assert _float("TEST_BAD", 0.5) == 0.5


# ---------------------------------------------------------------------------
# Section 03: Deduplication Hardening
# ---------------------------------------------------------------------------

class TestDeduplicationHardening:
    def test_compute_context_hash_returns_32_chars(self):
        h = compute_context_hash({"test": "value"})
        assert len(h) == 32
        assert all(c in "0123456789abcdef" for c in h)

    def test_persist_work_item_sql_has_on_conflict(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        cur.fetchone.return_value = ("id",)
        item = WorkItem(category="test", target_agent="test_agent",
                        context={"k": "v"}, context_hash=compute_context_hash({"k": "v"}))
        _persist_work_item(conn, item, "tick-1")
        sql = cur.execute.call_args[0][0]
        assert "ON CONFLICT" in sql
        assert "DO NOTHING" in sql

    def test_detect_work_returns_all_items(self):
        """detect_work no longer pre-filters; dedup happens at INSERT time."""
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        cur.fetchall.return_value = []
        cur.fetchone.return_value = (0,)
        state = WorldState(failed_builds_24h=[
            {"id": "b1", "app_name": "a1", "error_message": "e",
             "tier": "build", "retry_count": 0, "error_category": None},
        ])
        items = detect_work(conn, state, None)
        # Should include the failed build item (not filtered out)
        assert any(i.category == "build_failure" for i in items)


# ---------------------------------------------------------------------------
# Section 06: Failure Routing
# ---------------------------------------------------------------------------

class TestFailureRouting:
    def test_route_infra(self):
        assert _route_build_failure("infra") == "infra_engineer"

    def test_route_transient(self):
        assert _route_build_failure("transient") == "infra_engineer"

    def test_route_code_error(self):
        assert _route_build_failure("code_error") == "backend_engineer"

    def test_route_unknown(self):
        assert _route_build_failure("unknown") == "backend_engineer"

    def test_route_none(self):
        assert _route_build_failure(None) == "backend_engineer"

    def test_infra_categories_set(self):
        assert "infra" in _INFRA_CATEGORIES
        assert "transient" in _INFRA_CATEGORIES
        assert "code_error" not in _INFRA_CATEGORIES

    def test_check_failed_builds_includes_error_category(self):
        state = WorldState(failed_builds_24h=[
            {"id": "b1", "app_name": "a1", "error_message": "e",
             "tier": "build", "retry_count": 0, "error_category": "transient"},
        ])
        items = _check_failed_builds(MagicMock(), state, None)
        assert items[0].context["error_category"] == "transient"
        assert items[0].target_agent == "infra_engineer"


# ---------------------------------------------------------------------------
# Section 07: Agent Execution Timeouts
# ---------------------------------------------------------------------------

class TestAgentTimeouts:
    def test_agent_timeouts_has_all_registered_agents(self):
        for agent in ["backend_engineer", "architect", "infra_engineer",
                       "security", "qa_test", "financial", "customer_service",
                       "documentation", "cost_analyst"]:
            assert agent in AGENT_TIMEOUTS

    def test_get_agent_timeout_category_takes_precedence(self):
        assert _get_agent_timeout("backend_engineer", "budget_alert") == 300

    def test_get_agent_timeout_agent_default(self):
        assert _get_agent_timeout("backend_engineer", "some_unknown_cat") == 7200

    def test_get_agent_timeout_unknown_agent(self):
        assert _get_agent_timeout("unknown_agent", "unknown_cat") == 1800


# ---------------------------------------------------------------------------
# Section 08: Learning System Validation
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Section 09: Adaptive Scoring
# ---------------------------------------------------------------------------

class TestAdaptiveScoring:
    def test_performance_multiplier_no_data(self):
        assert _performance_multiplier_cached("test", "cat", None) == 1.0

    def test_performance_multiplier_empty_cache(self):
        assert _performance_multiplier_cached("test", "cat", {}) == 1.0

    def test_performance_multiplier_good_rate(self):
        cache = {("test", "cat"): {"total": 10, "successful": 9}}
        assert _performance_multiplier_cached("test", "cat", cache) == 1.0

    def test_performance_multiplier_medium_rate(self):
        cache = {("test", "cat"): {"total": 10, "successful": 6}}
        assert _performance_multiplier_cached("test", "cat", cache) == 0.85

    def test_performance_multiplier_low_rate(self):
        cache = {("test", "cat"): {"total": 10, "successful": 3}}
        assert _performance_multiplier_cached("test", "cat", cache) == 0.7

    def test_performance_multiplier_floor(self):
        """Floor is 0.7, prevents death spiral."""
        cache = {("test", "cat"): {"total": 100, "successful": 0}}
        assert _performance_multiplier_cached("test", "cat", cache) == 0.7

    def test_score_task_with_perf_cache(self):
        task = WorkItem(category="build_failure", target_agent="infra_engineer",
                        created_at=datetime.now(timezone.utc))
        state = WorldState(daily_budget_remaining_usd=20.0)
        cache = {("infra_engineer", "build_failure"): {"total": 10, "successful": 3}}
        score_with = score_task(task, state, perf_cache=cache)
        score_without = score_task(task, state, perf_cache=None)
        assert score_with < score_without  # Low performance reduces score

    def test_urgency_multiplier_at_8_hours(self):
        task = WorkItem(category="test",
                        created_at=datetime.now(timezone.utc) - timedelta(hours=8))
        state = WorldState()
        mult_8h = _urgency_multiplier(task, state)

        task_4h = WorkItem(category="test",
                           created_at=datetime.now(timezone.utc) - timedelta(hours=4))
        mult_4h = _urgency_multiplier(task_4h, state)
        assert mult_8h > mult_4h

    def test_urgency_multiplier_caps_at_2(self):
        task = WorkItem(category="test",
                        created_at=datetime.now(timezone.utc) - timedelta(hours=100))
        state = WorldState()
        assert _urgency_multiplier(task, state) <= 2.0

    def test_build_performance_cache(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        cur.fetchall.return_value = [
            ("infra_engineer", "build_failure", 10, 8),
        ]
        cache = build_performance_cache(conn)
        assert ("infra_engineer", "build_failure") in cache
        assert cache[("infra_engineer", "build_failure")]["total"] == 10


# ---------------------------------------------------------------------------
# Section 10: Approval Gates
# ---------------------------------------------------------------------------

class TestApprovalGates:

    def test_queue_rebuild_exempt_from_approval(self):
        """queue_rebuild does NOT require approval (self-healing)."""
        actuator = Actuators(agent_name="backend_engineer", task_id="t1", dry_run=True)
        result = actuator.queue_rebuild("parent-id")
        assert result is None  # dry_run, but no approval gate

    def test_stale_approval_requests_are_denied_not_approved(self):
        """Fail closed: an unanswered request is rejected on timeout, never approved."""
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        # 3-hour reminder query returns nothing; the timeout UPDATE returns one row
        cur.fetchall.side_effect = [[], [("req-1", "test_type")]]
        with patch("coordinator.tick._send_approval_reminder"):
            _expire_stale_approval_requests(conn)
        conn.commit.assert_called_once()
        updates = [c.args[0] for c in cur.execute.call_args_list
                   if "UPDATE approval_requests SET status" in c.args[0]]
        assert len(updates) == 1
        assert "status = 'rejected'" in updates[0]
        assert "timeout_auto_deny" in updates[0]
        assert "'approved'" not in updates[0]
        assert "auto_approve" not in updates[0]


# ---------------------------------------------------------------------------
# Section 11: State Cache Improvements
# ---------------------------------------------------------------------------

class TestStateCacheImprovements:
    def setup_method(self):
        _cache.clear()

    def test_cache_evicts_oldest_when_full(self):
        from coordinator.state import _set_cache
        for i in range(_MAX_CACHE_SIZE + 5):
            _set_cache(f"key-{i}", f"value-{i}")
        assert len(_cache) <= _MAX_CACHE_SIZE


    def test_max_cache_size_is_100(self):
        assert _MAX_CACHE_SIZE == 100


# ---------------------------------------------------------------------------
# Section 04: Budget Fix
# ---------------------------------------------------------------------------

class TestBudgetFix:
    @patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0", "COORDINATOR_INTERVAL_S": "900",
                             "ANTHROPIC_ADMIN_KEY": ""})
    @patch("coordinator.budget.datetime")
    def test_ticks_left_uses_interval(self, mock_dt):
        """At noon with a 900s interval there are 48 ticks left."""
        mock_dt.now.return_value = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        state = WorldState(active_builds=0)
        conn = MagicMock()
        conn.cursor.return_value.fetchone.side_effect = [(8.0, 1), (0.0, 0)]
        budget = compute_tick_budget(state, conn=conn)
        # 12h remaining = 43200s / 900s = 48 ticks; $12 left / 48 = $0.25 a tick
        assert budget.max_cost_usd == pytest.approx(0.25)

    @patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0", "COORDINATOR_INTERVAL_S": "1800",
                             "ANTHROPIC_ADMIN_KEY": ""})
    @patch("coordinator.budget.datetime")
    def test_per_tick_cap_25_percent(self, mock_dt):
        """Per-tick cost capped at 25% of daily budget."""
        # Set time close to midnight so remaining budget per tick would be high
        mock_dt.now.return_value = datetime(2024, 1, 1, 23, 55, 0, tzinfo=timezone.utc)
        state = WorldState(active_builds=0)
        conn = MagicMock()
        conn.cursor.return_value.fetchone.side_effect = [(0.0, 0), (0.0, 0)]
        budget = compute_tick_budget(state, conn=conn)
        # one tick left and $20 unspent: the cap, 25% of $20, is what the tick gets
        assert budget.max_cost_usd == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Section 05: Cost Tracking
# ---------------------------------------------------------------------------

class TestCostTracking:
    def test_spend_tracker_comprehensive(self):
        """get_comprehensive_spend returns expected structure."""
        from coordinator.spend_tracker import get_comprehensive_spend
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = [(0.0, 0), (0.0, 0)]
        result = get_comprehensive_spend(conn)
        assert "total_today_usd" in result
        assert "source" in result
        assert result["source"] in ("anthropic_api", "internal_tracking")


# ---------------------------------------------------------------------------
# Thread Safety (Section 01) - cursor context manager verification
# ---------------------------------------------------------------------------

class TestCursorContextManagers:

    def test_persist_work_item_uses_context_manager(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        cur.fetchone.return_value = ("id",)
        item = WorkItem(category="test", target_agent="a", context={"k": "v"})
        _persist_work_item(conn, item, "tick")
        cur.__exit__.assert_called_once()




