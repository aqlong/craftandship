# tests/test_backend_engineer_agent.py: Backend Engineer agent tests

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from coordinator.agents.backend_engineer import BackendEngineer
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators


def _make_state(**overrides):
    defaults = dict(
        tick_id="test-tick", tick_number=1,
        tick_start=datetime.now(timezone.utc),
        queued_builds=0, active_builds=0,
        failed_builds_24h=[], preview_awaiting=0,
        abandoned_intakes_24h=0, pending_feedback=0,
        monitored_apps_down=[], monitored_apps_degraded=[],
        api_cost_today_usd=5.0, daily_budget_remaining_usd=15.0,
        agent_success_rates={}, agent_avg_cost={},
        pending_work=[], previous_tick_summary={},
    )
    defaults.update(overrides)
    return WorldState(**defaults)


def _make_task(category, context=None):
    return WorkItem(
        category=category,
        target_agent="backend_engineer",
        description=f"Test {category}",
        context=context or {},
        context_hash="testhash",
        created_at=datetime.now(timezone.utc),
    )


class TestRecurringQA:
    def test_queries_affected_builds(self):
        agent = BackendEngineer()
        state = _make_state()
        task = _make_task("recurring_qa_warning", {
            "warning_type": "slow_response",
            "occurrences": 3,
        })
        actuators = Actuators("backend_engineer", "test-task", dry_run=True)
        actuators.query_readonly = MagicMock(return_value=[
            {"id": "build-1", "app_name": "test-app", "qa_results": [
                {"type": "slow_response", "status": "warning"}
            ]},
        ])

        result = agent.execute(task, state, "claude-opus-4-6", actuators)

        assert result.success
        assert result.output["warning_type"] == "slow_response"
        actuators.query_readonly.assert_called_once()

    _AFFECTED = [
        {"id": "build-1", "app_name": "app1", "qa_results": [
            {"type": "missing_field", "status": "warning"}
        ]},
        {"id": "build-2", "app_name": "app2", "qa_results": [
            {"type": "other", "status": "passed"}
        ]},
    ]

    def _recurring_qa_task(self):
        return _make_task("recurring_qa_warning", {"warning_type": "missing_field", "occurrences": 4})

    def test_queues_rebuilds_for_affected_builds(self):
        """Live: the one build that carries the warning is rebuilt, the other
        is not, and the result reports what was queued."""
        actuators = Actuators("backend_engineer", "test-task", dry_run=False)
        actuators.query_readonly = MagicMock(return_value=self._AFFECTED)
        actuators.queue_rebuild = MagicMock(return_value="new-build-1")
        actuators.send_alert = MagicMock(return_value=True)

        result = BackendEngineer().execute(self._recurring_qa_task(), _make_state(), "claude-opus-4-6", actuators)

        assert result.success
        actuators.queue_rebuild.assert_called_once_with(
            "build-1", rebuild_request="Fix recurring QA warning: missing_field")
        assert result.output["rebuilds_queued"] == 1
        assert result.output["rebuild_ids"] == ["new-build-1"]

    def test_a_refused_rebuild_is_not_counted_as_queued(self):
        """queue_rebuild answers None when it refuses (a cap, the customer's
        start limit, a deterministic failure)."""
        actuators = Actuators("backend_engineer", "test-task", dry_run=False)
        actuators.query_readonly = MagicMock(return_value=self._AFFECTED)
        actuators.queue_rebuild = MagicMock(return_value=None)
        actuators.send_alert = MagicMock(return_value=True)

        result = BackendEngineer().execute(self._recurring_qa_task(), _make_state(), "claude-opus-4-6", actuators)

        assert result.success
        assert result.output["rebuilds_queued"] == 0 and result.output["rebuild_ids"] == []

    def test_under_dry_run_the_rebuild_is_recorded_and_nothing_is_queued(self):
        actuators = Actuators("backend_engineer", "test-task", dry_run=True)
        actuators.query_readonly = MagicMock(return_value=self._AFFECTED)

        result = BackendEngineer().execute(self._recurring_qa_task(), _make_state(), "claude-opus-4-6", actuators)

        assert result.success
        assert result.output["rebuilds_queued"] == 0
        would_queue = [a.details for a in actuators.actions if a.action_type == "build_queue"]
        assert would_queue == [{"parent_build_id": "build-1", "dry_run": True}]

    def test_handles_db_error(self):
        agent = BackendEngineer()
        state = _make_state()
        task = _make_task("recurring_qa_warning", {"warning_type": "x", "occurrences": 3})
        actuators = Actuators("backend_engineer", "test-task", dry_run=True)
        actuators.query_readonly = MagicMock(side_effect=Exception("DB down"))

        result = agent.execute(task, state, "claude-opus-4-6", actuators)

        assert not result.success


class TestBuildFailure:
    def test_queues_rebuild(self):
        agent = BackendEngineer()
        state = _make_state()
        task = _make_task("build_failure", {
            "build_id": "build-fail-1",
            "error_message": "timeout during build",
        })
        actuators = Actuators("backend_engineer", "test-task", dry_run=True)

        result = agent.execute(task, state, "claude-opus-4-6", actuators)

        # Under dry run queue_rebuild only records what it would do. The result
        # says so (it used to read "rebuild_failed", as if a rebuild had been
        # tried and refused).
        assert not result.success
        assert result.output["action"] == "dry_run"

    def test_a_live_rebuild_that_is_refused_reads_as_failed(self):
        agent = BackendEngineer()
        task = _make_task("build_failure", {"build_id": "build-fail-1", "error_message": "timeout during build"})
        actuators = Actuators("backend_engineer", "test-task", dry_run=False)
        actuators.queue_rebuild = MagicMock(return_value=None)

        result = agent.execute(task, _make_state(), "claude-opus-4-6", actuators)

        assert not result.success
        assert result.output["action"] == "rebuild_failed"


class TestUnknownCategory:
    def test_returns_failure(self):
        agent = BackendEngineer()
        state = _make_state()
        task = _make_task("nonexistent_category")
        actuators = Actuators("backend_engineer", "test-task", dry_run=True)

        result = agent.execute(task, state, "claude-opus-4-6", actuators)

        assert not result.success
        assert "Unknown category" in result.output["error"]


class TestRegistration:
    def test_register(self):
        with patch("coordinator.tick.register_agent") as mock_reg:
            from coordinator.agents.backend_engineer import register
            register()
            mock_reg.assert_called_once_with("backend_engineer", BackendEngineer)
