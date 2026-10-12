# tests/test_cost_analyst_agent.py: Cost Analyst agent tests

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from coordinator.agents.cost_analyst import CostAnalyst, MODEL_PRICING, AVG_TOKENS_PER_BUILD
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
        target_agent="cost_analyst",
        description=f"Test {category}",
        context=context or {},
        context_hash="testhash",
        created_at=datetime.now(timezone.utc),
    )


class TestCostProjection:
    def test_generates_projections(self):
        agent = CostAnalyst()
        state = _make_state()
        task = _make_task("cost_projection")
        actuators = Actuators("cost_analyst", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(side_effect=[
            [{"total": 10, "live": 7, "failed": 2, "avg_cost": 1.50, "total_cost": 15.0}],
            [{"agent_cost": 2.0, "agent_tasks": 20}],
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert "projections" in result.output
        assert "current" in result.output["projections"]
        assert "100_customers" in result.output["projections"]
        assert result.output["weekly_builds"] == 10
        assert result.output["avg_build_cost_usd"] == 1.50

    def test_handles_zero_builds(self):
        agent = CostAnalyst()
        state = _make_state()
        task = _make_task("cost_projection")
        actuators = Actuators("cost_analyst", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(side_effect=[
            [{"total": 0, "live": 0, "failed": 0, "avg_cost": 0, "total_cost": 0}],
            [{"agent_cost": 0, "agent_tasks": 0}],
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["weekly_builds"] == 0

    def test_sends_email_report(self):
        agent = CostAnalyst()
        state = _make_state()
        task = _make_task("cost_projection")
        actuators = Actuators("cost_analyst", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(side_effect=[
            [{"total": 5, "live": 3, "failed": 1, "avg_cost": 2.0, "total_cost": 10.0}],
            [{"agent_cost": 1.0, "agent_tasks": 10}],
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert any(a.action_type == "email_send" for a in actuators.actions)

    def test_handles_db_error(self):
        agent = CostAnalyst()
        state = _make_state()
        task = _make_task("cost_projection")
        actuators = Actuators("cost_analyst", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(side_effect=Exception("DB error"))

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert not result.success


class TestCostReport:
    """The daily report reads the ledger with the ledger's own two statements
    (the same ones the budget runs on); tests/test_coordinator_tick_realdb.py
    runs it against real rows. Here the two answers are canned."""

    @pytest.fixture(autouse=True)
    def _no_admin_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
        monkeypatch.setenv("COORDINATOR_DAILY_BUDGET", "20")

    def _run(self, answers, context=None):
        from coordinator import spend_tracker
        actuators = Actuators("cost_analyst", "test-task", dry_run=True)
        asked = []

        def query(sql, params=()):
            asked.append(sql)
            return answers[sql]

        actuators.query_readonly = MagicMock(side_effect=query)
        result = CostAnalyst().execute(_make_task("cost_report", context), _make_state(),
                                       "claude-haiku-4-5", actuators)
        return result, actuators, asked, spend_tracker

    def test_daily_report(self):
        from coordinator import spend_tracker
        result, actuators, asked, _ = self._run({
            spend_tracker.LEDGER_BUILDS_SQL: [{"usd": 6.0, "n": 5}],
            spend_tracker.LEDGER_AGENT_TASKS_SQL: [{"usd": 1.5, "n": 12}],
        }, {"date": "2026-03-31"})

        assert result.success
        assert asked == [spend_tracker.LEDGER_BUILDS_SQL, spend_tracker.LEDGER_AGENT_TASKS_SQL]
        assert result.output["builds_today"] == 5
        assert result.output["build_pipeline_usd"] == 6.0
        assert result.output["agent_tasks_usd"] == 1.5, "the agents' own cost used to be reported as 0"
        assert result.output["total_tracked_usd"] == 7.5
        assert result.output["budget_remaining_usd"] == 12.5
        assert result.output["spend_source"] == "internal_tracking"
        assert result.output["provider_failed"] is False
        body = actuators.actions[-1].details
        assert body["subject"] == "[CAS] Daily Cost Report - $7.50 spent"

    def test_handles_empty_day(self):
        from coordinator import spend_tracker
        result, _actuators, _asked, _ = self._run({
            spend_tracker.LEDGER_BUILDS_SQL: [{"usd": 0, "n": 0}],
            spend_tracker.LEDGER_AGENT_TASKS_SQL: [{"usd": 0, "n": 0}],
        })
        assert result.success
        assert result.output["builds_today"] == 0 and result.output["total_tracked_usd"] == 0.0

    def test_a_ledger_that_does_not_answer_fails_the_report_instead_of_reporting_zero(self):
        """query_readonly answers [] when there is no database. That is not a
        day on which nothing was spent."""
        from coordinator import spend_tracker
        result, actuators, _asked, _ = self._run({
            spend_tracker.LEDGER_BUILDS_SQL: [],
            spend_tracker.LEDGER_AGENT_TASKS_SQL: [],
        })
        assert not result.success
        assert "Could not read the spend ledger" in result.output["error"]
        assert not [a for a in actuators.actions if a.action_type == "email_send"]

    def test_the_cost_report_figure_leads_when_the_admin_key_is_set(self, monkeypatch):
        from coordinator import spend_tracker
        monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "not-" + "an-admin-key")
        monkeypatch.setattr(spend_tracker, "get_anthropic_spend",
                            lambda: {"today_usd": 15.0, "month_to_date_usd": 200.0, "source": "anthropic_admin_api"})
        result, _actuators, _asked, _ = self._run({
            spend_tracker.LEDGER_BUILDS_SQL: [{"usd": 6.0, "n": 5}],
            spend_tracker.LEDGER_AGENT_TASKS_SQL: [{"usd": 1.5, "n": 12}],
        })
        assert result.output["spend_source"] == "anthropic_api"
        assert result.output["anthropic_today_usd"] == 15.0
        assert result.output["budget_remaining_usd"] == 5.0

    def test_an_unreadable_cost_report_is_said_not_papered_over(self, monkeypatch):
        from coordinator import spend_tracker
        monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "not-" + "an-admin-key")
        monkeypatch.setattr(spend_tracker, "get_anthropic_spend", lambda: None)
        result, actuators, _asked, _ = self._run({
            spend_tracker.LEDGER_BUILDS_SQL: [{"usd": 6.0, "n": 5}],
            spend_tracker.LEDGER_AGENT_TASKS_SQL: [{"usd": 1.5, "n": 12}],
        })
        assert result.success and result.output["provider_failed"] is True
        assert result.output["spend_source"] == "internal_tracking"


class TestUnknownCategory:
    def test_returns_failure(self):
        agent = CostAnalyst()
        state = _make_state()
        task = _make_task("unknown_thing")
        actuators = Actuators("cost_analyst", "test-task", dry_run=True)

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert not result.success


class TestPricingConstants:
    def test_all_models_have_pricing(self):
        for model in MODEL_PRICING:
            pricing = MODEL_PRICING[model]
            assert "input" in pricing
            assert "output" in pricing
            assert pricing["input"] > 0
            assert pricing["output"] > 0

    def test_tokens_per_build_have_models(self):
        for stage, tokens in AVG_TOKENS_PER_BUILD.items():
            assert tokens["model"] in MODEL_PRICING
            assert tokens["input"] > 0
            assert tokens["output"] > 0

    def test_opus_pricing_matches_api_client(self):
        # claude-opus-4-6 is $5/$25 per 1M tokens, NOT the old Claude 3 Opus rate ($15/$75)
        opus = MODEL_PRICING["claude-opus-4-6"]
        assert opus["input"] == 5.00, f"Opus input should be $5/1M, got ${opus['input']}"
        assert opus["output"] == 25.00, f"Opus output should be $25/1M, got ${opus['output']}"

    def test_haiku_pricing_matches_api_client(self):
        # claude-haiku-4-5 is $1/$5 per 1M tokens
        haiku = MODEL_PRICING["claude-haiku-4-5"]
        assert haiku["input"] == 1.00, f"Haiku input should be $1/1M, got ${haiku['input']}"
        assert haiku["output"] == 5.00, f"Haiku output should be $5/1M, got ${haiku['output']}"

    def test_pricing_consistent_with_api_client(self):
        # MODEL_PRICING in cost_analyst must stay in sync with PRICING in api_client.py
        # to prevent cost estimates from diverging between reporting and actual spend
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))
        from api_client import PRICING
        for model_id, data in MODEL_PRICING.items():
            api_input = PRICING.get((model_id, "realtime", "input"))
            if api_input is not None:
                expected_per_1m = api_input * 1_000_000
                assert abs(data["input"] - expected_per_1m) < 0.01, (
                    f"{model_id} input: cost_analyst has ${data['input']}/1M "
                    f"but api_client has ${expected_per_1m:.2f}/1M"
                )


class TestRegistration:
    def test_register(self):
        with patch("coordinator.tick.register_agent") as mock_reg:
            from coordinator.agents.cost_analyst import register
            register()
            mock_reg.assert_called_once_with("cost_analyst", CostAnalyst)
