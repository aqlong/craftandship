# tests/test_security_agent.py: Security Engineer agent tests

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from coordinator.agents import security_engineer
from coordinator.agents.security_engineer import SecurityEngineer
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
        target_agent="security",
        description=f"Test {category}",
        context=context or {},
        context_hash="testhash",
        created_at=datetime.now(timezone.utc),
    )


class TestPostDeployAudit:
    def test_clean_audit(self):
        agent = SecurityEngineer()
        state = _make_state()
        task = _make_task("post_deploy_audit", {
            "build_id": "build-123", "app_name": "test-app",
        })
        actuators = Actuators("security", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[{
            "live_url": "https://test-app.up.railway.app",
            "spec": {"pages": [], "data_models": []},
            "qa_results": [],
            "tier": "build",
        }])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert result.output["findings_count"] == 0

    def test_detects_http_url(self):
        agent = SecurityEngineer()
        state = _make_state()
        task = _make_task("post_deploy_audit", {
            "build_id": "build-123", "app_name": "test-app",
        })
        actuators = Actuators("security", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[{
            "live_url": "http://test-app.up.railway.app",
            "spec": {}, "qa_results": [], "tier": "build",
        }])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert result.output["high_count"] >= 1
        assert any("HTTPS" in f["issue"] for f in result.output["findings"])

    def test_detects_sensitive_fields(self):
        agent = SecurityEngineer()
        state = _make_state()
        task = _make_task("post_deploy_audit", {
            "build_id": "build-123", "app_name": "test-app",
        })
        actuators = Actuators("security", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[{
            "live_url": "https://test.up.railway.app",
            "spec": {
                "pages": [],
                "data_models": [{
                    "name": "User",
                    "fields": [
                        {"name": "email", "type": "string"},
                        {"name": "credit_card", "type": "string"},
                    ],
                }],
            },
            "qa_results": [], "tier": "build",
        }])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert result.output["high_count"] >= 1
        assert any("sensitive" in f["issue"].lower() for f in result.output["findings"])

    def test_build_not_found(self):
        agent = SecurityEngineer()
        state = _make_state()
        task = _make_task("post_deploy_audit", {"build_id": "missing"})
        actuators = Actuators("security", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert not result.success

    def test_escalates_on_many_high_findings(self):
        agent = SecurityEngineer()
        state = _make_state()
        task = _make_task("post_deploy_audit", {
            "build_id": "build-123", "app_name": "test-app",
        })
        actuators = Actuators("security", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[{
            "live_url": "http://insecure.example",  # high: no HTTPS
            "spec": {
                "pages": [],
                "data_models": [
                    {"name": "M1", "fields": [{"name": "password", "type": "str"}]},
                    {"name": "M2", "fields": [{"name": "secret_key", "type": "str"}]},
                    {"name": "M3", "fields": [{"name": "ssn", "type": "str"}]},
                ],
            },
            "qa_results": [{"passed": False, "name": "xss check"}],
            "tier": "build",
        }])

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert result.escalate  # > 2 high findings
        assert result.output["high_count"] > 2

    def test_unknown_category(self):
        agent = SecurityEngineer()
        state = _make_state()
        task = _make_task("unknown")
        actuators = Actuators("security", "test-task", dry_run=True)

        result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert not result.success


class TestDailyScan:
    def test_scans_live_apps(self):
        agent = SecurityEngineer()
        state = _make_state()
        task = _make_task("security_scan")
        actuators = Actuators("security", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[
            {"id": "1", "app_name": "app1", "live_url": "https://app1.test", "tier": "build", "qa_status": "passed"},
        ])

        # Stand in for the guarded session: no request leaves the test.
        with patch("monitor.ping.guarded_session") as guarded_session:
            head = guarded_session.return_value.head
            head.return_value = MagicMock(status_code=200)

            result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert result.output["total_apps"] == 1
        assert result.output["apps_with_issues"] == 0
        # One bounded exchange per app, and the session is closed afterwards.
        assert head.call_args.args == ("https://app1.test",)
        assert head.call_args.kwargs["timeout"] == security_engineer.PROBE_DEADLINE_S
        guarded_session.return_value.close.assert_called_once()

    def test_detects_unreachable_app(self):
        agent = SecurityEngineer()
        state = _make_state()
        task = _make_task("security_scan")
        actuators = Actuators("security", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[
            {"id": "1", "app_name": "dead-app", "live_url": "https://dead.test",
             "tier": "build", "qa_status": "passed"},
        ])

        with patch("monitor.ping.guarded_session") as guarded_session:
            guarded_session.return_value.head.side_effect = Exception("Connection refused")
            result = agent.execute(task, state, "claude-sonnet-4-6", actuators)

        assert result.success
        assert result.output["apps_with_issues"] == 1

    def test_an_app_that_answers_5xx_is_reported_and_4xx_is_not(self):
        agent = SecurityEngineer()
        actuators = Actuators("security", "test-task", dry_run=True)
        actuators.query_readonly = MagicMock(return_value=[
            {"id": "1", "app_name": "broken", "live_url": "https://broken.test", "tier": "build", "qa_status": "passed"},
            {"id": "2", "app_name": "private", "live_url": "https://private.test", "tier": "build", "qa_status": "passed"},
        ])
        answers = {"https://broken.test": 503, "https://private.test": 403}
        with patch("monitor.ping.guarded_session") as guarded_session:
            guarded_session.return_value.head.side_effect = (
                lambda url, **_kwargs: MagicMock(status_code=answers[url]))
            result = agent.execute(_make_task("security_scan"), _make_state(), "claude-sonnet-4-6", actuators)
        assert [finding["app"] for finding in result.output["findings"]] == ["broken"]

    def test_the_scan_stops_asking_when_its_budget_is_spent(self, monkeypatch):
        """The apps are probed one after another on one thread. Once the
        scan's budget is used up the rest are reported as not checked, not
        probed and not counted as down."""
        agent = SecurityEngineer()
        actuators = Actuators("security", "test-task", dry_run=True)
        actuators.query_readonly = MagicMock(return_value=[
            {"id": str(n), "app_name": f"app{n}", "live_url": f"https://app{n}.test",
             "tier": "build", "qa_status": "passed"}
            for n in range(5)
        ])
        clock = {"now": 1000.0}
        monkeypatch.setattr(security_engineer.time, "monotonic", lambda: clock["now"])

        def slow_head(url, **_kwargs):
            clock["now"] += security_engineer.SCAN_BUDGET_S / 2   # two apps use the budget up
            return MagicMock(status_code=200)

        with patch("monitor.ping.guarded_session") as guarded_session:
            guarded_session.return_value.head.side_effect = slow_head
            result = agent.execute(_make_task("security_scan"), _make_state(), "claude-sonnet-4-6", actuators)

        assert guarded_session.return_value.head.call_count == 2
        assert result.output["apps_not_checked"] == 3
        assert result.output["apps_with_issues"] == 0
        assert "Not checked" in result.output["findings"][-1]["issue"]


class TestRegistration:
    def test_register(self):
        with patch("coordinator.tick.register_agent") as mock_reg:
            from coordinator.agents.security_engineer import register
            register()
            mock_reg.assert_called_once_with("security", SecurityEngineer)
