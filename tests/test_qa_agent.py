# tests/test_qa_agent.py: QA/Test Engineer agent tests

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from coordinator.agents.qa_engineer import QAEngineer
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
        api_cost_today_usd=0, daily_budget_remaining_usd=20.0,
        agent_success_rates={}, agent_avg_cost={},
        pending_work=[], previous_tick_summary={},
    )
    defaults.update(overrides)
    return WorldState(**defaults)


def _make_task(category, context=None):
    return WorkItem(
        category=category,
        target_agent="qa_test",
        description=f"Test {category}",
        context=context or {},
        context_hash="testhash",
        created_at=datetime.now(timezone.utc),
    )


class TestRegressionCheck:
    def test_finds_regressions(self):
        agent = QAEngineer()
        state = _make_state()
        task = _make_task("agent_initiative")
        actuators = Actuators("qa_test", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[
            {"original_id": "b1", "app_name": "broke-app",
             "rebuild_request": "Login is broken", "rebuild_at": "2026-03-30"},
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["regressions_found"] == 1
        assert any("broke-app" in l for l in result.learnings)

    def test_no_regressions(self):
        agent = QAEngineer()
        state = _make_state()
        task = _make_task("agent_initiative")
        actuators = Actuators("qa_test", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["regressions_found"] == 0


class TestRegistration:
    def test_register(self):
        with patch("coordinator.tick.register_agent") as mock_reg:
            from coordinator.agents.qa_engineer import register
            register()
            mock_reg.assert_called_once_with("qa_test", QAEngineer)
