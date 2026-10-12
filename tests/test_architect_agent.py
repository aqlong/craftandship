# tests/test_architect_agent.py: Architect agent tests

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from coordinator.agents.architect import Architect
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
        target_agent="architect",
        description=f"Test {category}",
        context=context or {},
        context_hash="testhash",
        created_at=datetime.now(timezone.utc),
    )


class TestSpecReview:
    def test_clean_spec(self):
        agent = Architect()
        state = _make_state()
        task = _make_task("spec_review", {
            "build_id": "build-123", "app_name": "test-app",
        })
        actuators = Actuators("architect", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[{
            "spec": {
                "app_name": "test-app",
                "description": "A simple task tracker",
                "pages": [
                    {"name": "Dashboard", "auth_required": True},
                    {"name": "Login", "auth_required": False},
                ],
                "data_models": [
                    {"name": "Task", "fields": [
                        {"name": "title", "type": "string"},
                        {"name": "created_at", "type": "datetime"},
                    ]},
                ],
            },
            "tier": "build",
        }])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        # Clean spec should have 0 high-severity recommendations
        assert result.output["high_count"] == 0

    def test_detects_nonexistent_belongs_to(self):
        agent = Architect()
        state = _make_state()
        task = _make_task("spec_review", {
            "build_id": "build-123", "app_name": "test-app",
        })
        actuators = Actuators("architect", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[{
            "spec": {
                "app_name": "test-app",
                "description": "App with bad refs",
                "pages": [{"name": "Home", "auth_required": True}],
                "data_models": [
                    {"name": "Comment", "fields": [
                        {"name": "text", "type": "string"},
                    ], "belongs_to": "NonExistentModel"},
                ],
            },
            "tier": "build",
        }])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert result.output["high_count"] >= 1
        assert any(
            "nonexistent" in r["message"].lower()
            for r in result.output["recommendations"]
        )

    def test_detects_missing_app_name(self):
        agent = Architect()
        state = _make_state()
        task = _make_task("spec_review", {
            "build_id": "build-123", "app_name": "test-app",
        })
        actuators = Actuators("architect", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[{
            "spec": {"description": "Missing app_name", "pages": [], "data_models": []},
            "tier": "build",
        }])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert any(
            "app_name" in r["message"]
            for r in result.output["recommendations"]
        )

    def test_build_not_found(self):
        agent = Architect()
        state = _make_state()
        task = _make_task("spec_review", {"build_id": "missing"})
        actuators = Actuators("architect", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert not result.success
        assert "not found" in result.output.get("error", "")

    def test_detects_tier_mismatch(self):
        """Build tier with many models and pages should flag tier mismatch."""
        agent = Architect()
        state = _make_state()
        task = _make_task("spec_review", {
            "build_id": "build-123", "app_name": "complex-app",
        })
        actuators = Actuators("architect", "test-task", dry_run=True)

        models = [
            {"name": f"Model{i}", "fields": [
                {"name": "name", "type": "string"},
                {"name": "created_at", "type": "datetime"},
            ]}
            for i in range(7)
        ]
        pages = [
            {"name": f"Page{i}", "auth_required": True}
            for i in range(12)
        ]

        actuators.query_readonly = MagicMock(return_value=[{
            "spec": {
                "app_name": "complex-app",
                "description": "A complex app",
                "pages": pages,
                "data_models": models,
            },
            "tier": "build",
        }])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert any(
            "tier" in r["message"].lower() and "grow" in r["message"].lower()
            for r in result.output["recommendations"]
        )


class TestPostBuildReview:
    def test_clean_build(self):
        agent = Architect()
        state = _make_state()
        task = _make_task("post_build_review", {
            "build_id": "build-123", "app_name": "test-app",
        })
        actuators = Actuators("architect", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[{
            "spec": {
                "data_models": [{"name": "Task"}],
                "pages": [{"name": "Tasks"}],
            },
            "tier": "build",
            "qa_status": "passed",
            "qa_results": [],
            "retry_count": 0,
            "stage_timings": {"building": {"duration_s": 120}},
            "total_api_cost_usd": 0.50,
        }])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert result.output["findings_count"] == 0

    def test_flags_high_retry_count(self):
        agent = Architect()
        state = _make_state()
        task = _make_task("post_build_review", {
            "build_id": "build-123", "app_name": "fragile-app",
        })
        actuators = Actuators("architect", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[{
            "spec": {"data_models": [], "pages": []},
            "tier": "build",
            "qa_status": "passed",
            "qa_results": [],
            "retry_count": 3,
            "stage_timings": {},
            "total_api_cost_usd": 1.0,
        }])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert any(
            "retri" in f["message"].lower()
            for f in result.output["findings"]
        )

    def test_flags_excessive_cost(self):
        agent = Architect()
        state = _make_state()
        task = _make_task("post_build_review", {
            "build_id": "build-123", "app_name": "expensive-app",
        })
        actuators = Actuators("architect", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[{
            "spec": {"data_models": [], "pages": []},
            "tier": "build",
            "qa_status": "passed",
            "qa_results": [],
            "retry_count": 0,
            "stage_timings": {},
            "total_api_cost_usd": 5.0,
        }])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert any(
            "cost" in f["message"].lower()
            for f in result.output["findings"]
        )


class TestArchitectureQAReview:
    def test_detects_multi_tier_pattern(self):
        agent = Architect()
        state = _make_state()
        task = _make_task("architecture_qa_review", {
            "warning_type": "slow_response",
            "occurrences": 5,
        })
        actuators = Actuators("architect", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[
            {
                "id": "1", "app_name": "app1",
                "spec": {"data_models": [{"name": "M1"}], "pages": [{"name": "P1"}]},
                "qa_results": [{"type": "slow_response", "status": "warning"}],
                "tier": "build",
            },
            {
                "id": "2", "app_name": "app2",
                "spec": {"data_models": [{"name": "M2"}], "pages": [{"name": "P2"}]},
                "qa_results": [{"type": "slow_response", "status": "warning"}],
                "tier": "grow",
            },
        ])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert len(result.output["affected_tiers"]) == 2
        assert result.output["high_count"] >= 1

    def test_handles_empty_qa_results(self):
        agent = Architect()
        state = _make_state()
        task = _make_task("architecture_qa_review", {
            "warning_type": "missing_field",
            "occurrences": 3,
        })
        actuators = Actuators("architect", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert result.output["affected_tiers"] == []


class TestInitiative:
    def test_proactive_review(self):
        agent = Architect()
        state = _make_state()
        task = _make_task("agent_initiative", {"initiative": "architecture review"})
        actuators = Actuators("architect", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[
            {"tier": "build", "qa_status": "passed", "retry_count": 0,
             "total_api_cost_usd": 0.5},
            {"tier": "build", "qa_status": "warned", "retry_count": 2,
             "total_api_cost_usd": 1.0},
        ])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert "findings" in result.output


class TestUnknownCategory:
    def test_unknown_category(self):
        agent = Architect()
        state = _make_state()
        task = _make_task("unknown_category")
        actuators = Actuators("architect", "test-task", dry_run=True)

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert not result.success
        assert "Unknown category" in result.output.get("error", "")


class TestRegistration:
    def test_register(self):
        with patch("coordinator.tick.register_agent") as mock_reg:
            from coordinator.agents.architect import register
            register()
            mock_reg.assert_called_once_with("architect", Architect)
