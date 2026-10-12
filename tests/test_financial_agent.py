# tests/test_financial_agent.py: Financial Manager agent tests

import json
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from coordinator.agents.financial_manager import FinancialManager
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
        target_agent="financial",
        description=f"Test {category}",
        context=context or {},
        context_hash="testhash",
        created_at=datetime.now(timezone.utc),
    )


class TestBudgetAlert:
    def test_sends_alert(self):
        agent = FinancialManager()
        state = _make_state(daily_budget_remaining_usd=3.0)
        task = _make_task("budget_alert", {"remaining_usd": 3.0, "cost_today_usd": 17.0})
        actuators = Actuators("financial", "test-task", dry_run=True)

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["action"] == "budget_alert_dry_run"
        assert any(a.action_type == "email_send" and a.details.get("dry_run") for a in actuators.actions)

    def test_includes_cost_info(self):
        agent = FinancialManager()
        state = _make_state()
        task = _make_task("budget_alert", {"remaining_usd": 4.5, "cost_today_usd": 15.5})
        actuators = Actuators("financial", "test-task", dry_run=True)

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.output["remaining_usd"] == 4.5
        assert len(result.learnings) > 0

    def test_unknown_category(self):
        agent = FinancialManager()
        state = _make_state()
        task = _make_task("unknown_thing")
        actuators = Actuators("financial", "test-task", dry_run=True)

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert not result.success
        assert "Unknown category" in result.output["error"]


class TestCostReport:
    def test_generates_report(self):
        agent = FinancialManager()
        state = _make_state()
        task = _make_task("agent_initiative")
        actuators = Actuators("financial", "test-task", dry_run=True)

        # Mock query_readonly to return test data
        actuators.query_readonly = MagicMock(side_effect=[
            # Build costs query
            [{"tier": "build", "count": 2, "total_cost": 3.50, "avg_cost": 1.75}],
            # Agent costs query
            [{"agent": "infra_engineer", "count": 1, "total_cost": 0.12}],
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["total_cost_24h"] == pytest.approx(3.62)
        assert result.output["build_cost"] == pytest.approx(3.50)
        assert result.output["agent_cost"] == pytest.approx(0.12)

    def test_handles_empty_data(self):
        agent = FinancialManager()
        state = _make_state()
        task = _make_task("agent_initiative")
        actuators = Actuators("financial", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(side_effect=[[], []])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["total_cost_24h"] == 0

    def test_handles_query_error(self):
        agent = FinancialManager()
        state = _make_state()
        task = _make_task("agent_initiative")
        actuators = Actuators("financial", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(side_effect=Exception("DB error"))

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert not result.success
        assert result.escalate

    def test_sends_alert_with_report(self):
        agent = FinancialManager()
        state = _make_state()
        task = _make_task("agent_initiative")
        actuators = Actuators("financial", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(side_effect=[
            [{"tier": "build", "count": 1, "total_cost": 2.0, "avg_cost": 2.0}],
            [],
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert any(a.action_type == "email_send" for a in actuators.actions)


class TestDecimalSerialization:
    """Verify that Decimal values from PostgreSQL NUMERIC columns serialize correctly."""

    def test_cost_report_with_decimal_values(self):
        """Financial agent returns Decimal from SQL; tick serialization must handle it."""
        agent = FinancialManager()
        state = _make_state()
        task = _make_task("agent_initiative")
        actuators = Actuators("financial", "test-task", dry_run=True)

        # Simulate PostgreSQL returning Decimal values (as it does for NUMERIC columns)
        actuators.query_readonly = MagicMock(side_effect=[
            [{"tier": "build", "count": 3, "total_cost": Decimal("5.2500"), "avg_cost": Decimal("1.7500")}],
            [{"agent": "infra_engineer", "count": 2, "total_cost": Decimal("0.3400")}],
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        # The output dict contains Decimal values from the SQL result breakdown
        # Verify _json_dumps from tick.py handles them
        from coordinator.tick import _json_dumps
        serialized = _json_dumps(result.output)
        parsed = json.loads(serialized)
        assert parsed["total_cost_24h"] == pytest.approx(5.59)
        assert parsed["build_cost"] == pytest.approx(5.25)
        assert parsed["agent_cost"] == pytest.approx(0.34)
        # Nested Decimal values in breakdown dicts
        assert parsed["build_breakdown"][0]["total_cost"] == pytest.approx(5.25)
        assert parsed["build_breakdown"][0]["avg_cost"] == pytest.approx(1.75)

    def test_json_dumps_decimal_encoder(self):
        """_json_dumps converts Decimal to float transparently."""
        from coordinator.tick import _json_dumps
        data = {
            "cost": Decimal("12.3456"),
            "nested": {"amount": Decimal("0.00")},
            "list": [Decimal("1.5"), Decimal("2.5")],
            "normal": "hello",
            "int_val": 42,
        }
        serialized = _json_dumps(data)
        parsed = json.loads(serialized)
        assert parsed["cost"] == pytest.approx(12.3456)
        assert parsed["nested"]["amount"] == pytest.approx(0.0)
        assert parsed["list"] == [pytest.approx(1.5), pytest.approx(2.5)]
        assert parsed["normal"] == "hello"
        assert parsed["int_val"] == 42

    def test_json_dumps_still_raises_on_unsupported_types(self):
        """_json_dumps should still raise TypeError for truly unsupported types."""
        from coordinator.tick import _json_dumps
        with pytest.raises(TypeError):
            _json_dumps({"bad": object()})

    def test_world_state_decimal_fields_serialize(self):
        """WorldState fields like api_cost_today_usd may be Decimal from DB."""
        from coordinator.tick import _json_dumps
        summary = {
            "api_cost_today_usd": Decimal("8.1234"),
            "daily_budget_remaining_usd": Decimal("11.8766"),
            "active_builds": 1,
        }
        serialized = _json_dumps(summary)
        parsed = json.loads(serialized)
        assert parsed["api_cost_today_usd"] == pytest.approx(8.1234)
        assert parsed["daily_budget_remaining_usd"] == pytest.approx(11.8766)


class TestRegistration:
    def test_register(self):
        with patch("coordinator.tick.register_agent") as mock_reg:
            from coordinator.agents.financial_manager import register
            register()
            mock_reg.assert_called_once_with("financial", FinancialManager)
