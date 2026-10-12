# tests/test_coordinator_actuators.py
#
# Covers gaps in coordinator/actuators.py not already in tests/test_coordinator.py.
#
# Already tested elsewhere (skipped here):
#   - Permission checks for send_alert and query_readonly, and
#     queue_rebuild (dry run + permission)
#   - Action logging
#   - query_readonly mutation rejection

import json
import os
import tempfile
from pathlib import Path
from urllib.error import URLError
from unittest.mock import MagicMock, patch, call

import pytest

from coordinator.actuators import (
    Actuators,
    _esc,
    _send_escalation_email,
    AGENT_PERMISSIONS,
    NON_AGENT_PRINCIPALS,
    _ANALYSIS_ONLY_PERMISSIONS,
)


# ---------------------------------------------------------------------------
# TestQueueNewBuild
# ---------------------------------------------------------------------------

class TestQueueNewBuild:
    """Covers queue_new_build on Actuators."""

    def test_dry_run_returns_none(self):
        act = Actuators("backend_engineer", "task-01", dry_run=True)
        result = act.queue_new_build("cust-1", "my-app", {"description": "test"})
        assert result is None

    def test_dry_run_records_action(self):
        act = Actuators("backend_engineer", "task-01", dry_run=True)
        act.queue_new_build("cust-1", "my-app", {})
        assert any(a.action_type == "build_queue" for a in act.actions)

    def test_requires_build_queue_permission(self):
        act = Actuators("financial", "task-02", dry_run=True)
        with pytest.raises(PermissionError, match="build_queue"):
            act.queue_new_build("cust-1", "my-app", {})

    def test_returns_none_when_no_database_url(self):
        act = Actuators("backend_engineer", "task-03", dry_run=False)
        env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        with patch.dict(os.environ, env, clear=True):
            result = act.queue_new_build("cust-1", "my-app", {})
        assert result is None

    def test_normalizes_spec_version_to_0_1(self):
        """spec without spec_version gets spec_version='0.1' set before INSERT."""
        act = Actuators("backend_engineer", "task-04", dry_run=False)
        spec = {"description": "a cool app"}

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://localhost/test"}):
            with patch("psycopg2.connect", return_value=mock_conn):
                act.queue_new_build("cust-1", "my-app", spec)

        # After the call the spec object itself should have spec_version set
        assert spec.get("spec_version") == "0.1"

    def test_overwrites_wrong_spec_version(self):
        """spec with wrong spec_version gets corrected to '0.1'."""
        act = Actuators("backend_engineer", "task-05", dry_run=False)
        spec = {"description": "test", "spec_version": "0.2"}

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("new-build-id",)
        mock_conn.cursor.return_value = mock_cur

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://localhost/test"}):
            with patch("psycopg2.connect", return_value=mock_conn):
                act.queue_new_build("cust-1", "my-app", spec)

        assert spec["spec_version"] == "0.1"


# ---------------------------------------------------------------------------
# TestRecordFix
# ---------------------------------------------------------------------------

class TestRecordFix:
    """Covers record_fix on Actuators."""

    def test_requires_kb_record_permission(self):
        # "financial" lacks "kb_record" permission
        act = Actuators("financial", "task-r1", dry_run=True)
        with pytest.raises(PermissionError, match="kb_record"):
            act.record_fix("err", "desc", ["f.py"], "code_error", True)

    def test_dry_run_records_action_without_calling_kb(self):
        act = Actuators("infra_engineer", "task-r2", dry_run=True)
        with patch("cli.failure_kb.record_fix") as mock_kb:
            act.record_fix("err msg", "fix desc", ["routes/auth.js"], "code_error", True)

        mock_kb.assert_not_called()
        assert any(a.action_type == "kb_record" for a in act.actions)

    def test_live_run_calls_kb_record_fix_with_correct_args(self):
        act = Actuators("infra_engineer", "task-r3", dry_run=False)
        with patch("cli.failure_kb.record_fix") as mock_kb:
            act.record_fix("some error", "applied fix", ["server.js"], "transient", False)

        mock_kb.assert_called_once_with(
            "some error", "applied fix", ["server.js"], "transient", False
        )

    def test_a_kb_that_raises_is_reported_as_a_failure_not_as_a_record(self):
        """The failure is swallowed (the task goes on), but the answer is False
        and the action log says `kb_record_failed`: it must not say the entry
        was recorded."""
        act = Actuators("infra_engineer", "task-r4", dry_run=False)
        with patch("cli.failure_kb.record_fix", side_effect=OSError("disk full")):
            recorded = act.record_fix("err", "fix", [], "infra", True)

        assert recorded is False
        assert [a.action_type for a in act.actions] == ["kb_record_failed"]

    def test_the_answer_is_true_when_the_entry_was_written_and_on_a_dry_run(self):
        live = Actuators("infra_engineer", "task-r4a", dry_run=False)
        with patch("cli.failure_kb.record_fix"):
            assert live.record_fix("err", "fix", [], "infra", True) is True
        assert [a.action_type for a in live.actions] == ["kb_record"]
        dry = Actuators("infra_engineer", "task-r4b", dry_run=True)
        assert dry.record_fix("err", "fix", [], "infra", True) is True
        assert dry.actions[0].details["dry_run"] is True

    def test_action_details_include_category_and_success(self):
        act = Actuators("customer_service", "task-r5", dry_run=True)
        act.record_fix("err", "fix", [], "app_logic", True)
        action = next(a for a in act.actions if a.action_type == "kb_record")
        assert action.details["category"] == "app_logic"
        assert action.details["success"] is True


# ---------------------------------------------------------------------------
# TestEscHelper
# ---------------------------------------------------------------------------

class TestEscHelper:
    """Verify _esc() HTML-escaping helper."""

    def test_escapes_ampersand(self):
        assert _esc("a & b") == "a &amp; b"

    def test_escapes_angle_brackets(self):
        assert _esc("<script>") == "&lt;script&gt;"

    def test_escapes_double_quotes(self):
        assert _esc('"value"') == "&quot;value&quot;"

    def test_no_change_for_plain_text(self):
        assert _esc("hello world") == "hello world"

    def test_escapes_all_special_chars_together(self):
        assert _esc('<a href="foo&bar">') == "&lt;a href=&quot;foo&amp;bar&quot;&gt;"

    def test_empty_string(self):
        assert _esc("") == ""


# ---------------------------------------------------------------------------
# TestSendEscalationEmail
# ---------------------------------------------------------------------------

class TestSendEscalationEmail:
    """Verify _send_escalation_email() behavior."""

    def test_returns_false_when_no_resend_api_key(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("RESEND_API_KEY",)}
        with patch.dict(os.environ, env, clear=True):
            result = _send_escalation_email(
                "commit_production", "Test description", {}, "req-123"
            )
        assert result is False

    def test_sends_email_successfully(self):
        """When Resend returns 201, the function returns True."""
        mock_response = MagicMock()
        mock_response.status = 201
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {
            "RESEND_API_KEY": "re_test_key",
            "ADMIN_EMAIL": "admin@example.com",
        }):
            with patch("coordinator.actuators.bounded_urlopen", return_value=mock_response):
                result = _send_escalation_email(
                    "commit_production", "Some commit blocked", {}, "req-abc-123"
                )

        assert result is True

    def test_returns_false_on_url_error(self):
        """Network failures return False without raising."""
        with patch.dict(os.environ, {
            "RESEND_API_KEY": "re_test_key",
            "ADMIN_EMAIL": "admin@example.com",
        }):
            with patch("coordinator.actuators.bounded_urlopen",
                       side_effect=URLError("connection refused")):
                result = _send_escalation_email(
                    "commit_production", "Blocked commit", {}, "req-xyz"
                )

        assert result is False

    def test_returns_false_on_os_error(self):
        """OSError (a subclass of URLError sibling) also returns False."""
        with patch.dict(os.environ, {
            "RESEND_API_KEY": "re_test_key",
            "ADMIN_EMAIL": "admin@example.com",
        }):
            with patch("coordinator.actuators.bounded_urlopen", side_effect=OSError("broken pipe")):
                result = _send_escalation_email(
                    "escalation:infra_engineer", "no playbook for this failure", {}, "req-999"
                )

        assert result is False

    def test_returns_false_when_resend_returns_non_success_status(self):
        """If Resend returns 400/500, returns False."""
        mock_response = MagicMock()
        mock_response.status = 400
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {
            "RESEND_API_KEY": "re_test_key",
            "ADMIN_EMAIL": "admin@example.com",
        }):
            with patch("coordinator.actuators.bounded_urlopen", return_value=mock_response):
                result = _send_escalation_email(
                    "commit_production", "Bad request", {}, "req-bad"
                )

        assert result is False

    def test_escapes_context_values_in_html(self):
        """Context values containing HTML are escaped before sending."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        captured_payload = []

        def capture_urlopen(req, timeout=None, max_body_bytes=None):
            captured_payload.append(req.data.decode())
            return mock_response

        with patch.dict(os.environ, {
            "RESEND_API_KEY": "re_test_key",
            "ADMIN_EMAIL": "admin@example.com",
        }):
            with patch("coordinator.actuators.bounded_urlopen", side_effect=capture_urlopen):
                _send_escalation_email(
                    "test", "desc", {"key": "<script>alert(1)</script>"}, "req-1"
                )

        assert captured_payload, "urlopen should have been called"
        assert "&lt;script&gt;" in captured_payload[0]


# ---------------------------------------------------------------------------
# TestAnalysisOnlyMode
# ---------------------------------------------------------------------------

class TestAnalysisOnlyMode:
    """COORDINATOR_ANALYSIS_ONLY=true restricts all agents to analysis permissions."""

    def _make(self, agent: str = "backend_engineer") -> Actuators:
        return Actuators(agent, "task-ao", dry_run=True)

    def test_analysis_only_property_false_by_default(self):
        act = self._make()
        env = {k: v for k, v in os.environ.items() if k != "COORDINATOR_ANALYSIS_ONLY"}
        with patch.dict(os.environ, env, clear=True):
            assert act.analysis_only is False

    def test_analysis_only_property_true_when_set(self):
        act = self._make()
        with patch.dict(os.environ, {"COORDINATOR_ANALYSIS_ONLY": "true"}):
            assert act.analysis_only is True

    def test_analysis_only_property_true_for_1(self):
        act = self._make()
        with patch.dict(os.environ, {"COORDINATOR_ANALYSIS_ONLY": "1"}):
            assert act.analysis_only is True

    @pytest.mark.parametrize("category", ["railway_crash", "env_config"])
    def test_keeps_every_kind_of_playbook_dry_in_analysis_mode(self, category):
        """github_write, railway_deploy and railway_env are reachable only
        through run_playbook, which answers a refused permission by running
        the fixer dry instead of raising."""
        act = Actuators("infra_engineer", "task-ao", dry_run=False)
        seen = {}

        def fixer(context):
            seen.update(context)
            return MagicMock(success=True, description="would fix", commit_sha=None)

        with patch.dict(os.environ, {"COORDINATOR_ANALYSIS_ONLY": "true"}), \
                patch("ops.guardrails.CircuitBreaker.can_proceed", return_value=True), \
                patch("ops.audit.log_action") as audit:
            _result, ran_live = act.run_playbook(category, fixer, {"build_ids": []})
        assert ran_live is False and seen["dry_run"] is True
        audit.assert_not_called()

    def test_blocks_build_queue_in_analysis_mode(self):
        act = self._make("backend_engineer")
        with patch.dict(os.environ, {"COORDINATOR_ANALYSIS_ONLY": "true"}):
            with pytest.raises(PermissionError, match="COORDINATOR_ANALYSIS_ONLY"):
                act.queue_rebuild("build-id")

    def test_allows_kb_record_in_analysis_mode(self):
        """kb_record is an analysis permission: a live (not dry-run) actuator
        in analysis-only mode writes the failure KB."""
        act = Actuators("infra_engineer", "task-ao", dry_run=False)
        with patch.dict(os.environ, {"COORDINATOR_ANALYSIS_ONLY": "true"}):
            with patch("cli.failure_kb.record_fix") as mock_kb:
                # Must not raise
                act.record_fix("err", "fix", [], "code_error", True)
        mock_kb.assert_called_once()

    def test_allows_email_send_in_analysis_mode(self):
        act = self._make("infra_engineer")
        with patch.dict(os.environ, {"COORDINATOR_ANALYSIS_ONLY": "true"}):
            # dry_run=True so no real email; must not raise PermissionError
            result = act.send_alert("subject", "body")
        assert result is True

    def test_analysis_only_permissions_constant_correct(self):
        assert "kb_record" in _ANALYSIS_ONLY_PERMISSIONS
        assert "finding_write" in _ANALYSIS_ONLY_PERMISSIONS
        assert "email_send" in _ANALYSIS_ONLY_PERMISSIONS
        assert "github_write" not in _ANALYSIS_ONLY_PERMISSIONS
        assert "railway_deploy" not in _ANALYSIS_ONLY_PERMISSIONS
        assert "build_queue" not in _ANALYSIS_ONLY_PERMISSIONS

    def test_record_fix_honors_dry_run_in_analysis_mode(self):
        """Analysis-only narrows permissions; it does not turn a dry run into a
        live one. (This test used to pin the opposite: a KB write under dry
        run whenever analysis-only was set.)"""
        act = Actuators("infra_engineer", "task-ao2", dry_run=True)
        with patch.dict(os.environ, {"COORDINATOR_ANALYSIS_ONLY": "true"}):
            with patch("cli.failure_kb.record_fix") as mock_kb:
                act.record_fix("err", "fix", [], "code_error", True)
        mock_kb.assert_not_called()
        assert act.actions[-1].details["dry_run"] is True

    def test_record_fix_skips_kb_without_analysis_mode_on_dry_run(self):
        """Without analysis mode, dry_run=True skips the KB write."""
        act = Actuators("infra_engineer", "task-ao3", dry_run=True)
        env = {k: v for k, v in os.environ.items() if k != "COORDINATOR_ANALYSIS_ONLY"}
        with patch.dict(os.environ, env, clear=True):
            with patch("cli.failure_kb.record_fix") as mock_kb:
                act.record_fix("err", "fix", [], "code_error", True)
        mock_kb.assert_not_called()


# ---------------------------------------------------------------------------
# TestFindingAdd
# ---------------------------------------------------------------------------

class TestFindingAdd:
    """finding_add() inserts into coordinator_findings with ON CONFLICT dedup."""

    def _make(self, agent: str = "backend_engineer") -> Actuators:
        return Actuators(agent, "task-fa", dry_run=False)

    def _mock_conn(self, fetchone_value=("fake-uuid",)):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = fetchone_value
        conn.cursor.return_value = cur
        return conn, cur

    def test_finding_add_inserts_new_row(self):
        act = self._make()
        conn, cur = self._mock_conn(fetchone_value=("new-id",))
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            with patch("psycopg2.connect", return_value=conn):
                result = act.finding_add(
                    "key-1", "Finding title", "Detailed desc",
                    priority="high", category="security",
                )
        assert result is True
        assert cur.execute.called
        args = cur.execute.call_args[0]
        assert "INSERT INTO coordinator_findings" in args[0]
        assert "ON CONFLICT (finding_key) DO NOTHING" in args[0]
        params = args[1]
        assert params[3] == "key-1"
        assert params[4] == "Finding title"
        assert params[6] == "high"
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

    def test_finding_add_dedup_returns_true_without_insert(self):
        act = self._make()
        conn, cur = self._mock_conn(fetchone_value=None)
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            with patch("psycopg2.connect", return_value=conn):
                result = act.finding_add("dupe-key", "T", "d")
        assert result is True
        action = next(a for a in act.actions if a.action_type == "finding_add")
        assert action.details["inserted"] is False

    def test_finding_add_records_action_on_insert(self):
        act = self._make()
        conn, _ = self._mock_conn(fetchone_value=("new-id",))
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            with patch("psycopg2.connect", return_value=conn):
                act.finding_add("k", "Title", "d", priority="low")
        action = next(a for a in act.actions if a.action_type == "finding_add")
        assert action.details["finding_key"] == "k"
        assert action.details["inserted"] is True
        assert action.details["priority"] == "low"

    def test_finding_add_no_db_url_returns_false(self):
        act = self._make()
        env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        with patch.dict(os.environ, env, clear=True):
            result = act.finding_add("k", "T", "d")
        assert result is False

    def test_finding_add_handles_db_exception(self):
        act = self._make()
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            with patch("psycopg2.connect", side_effect=Exception("connection refused")):
                result = act.finding_add("k", "T", "d")
        assert result is False

    def test_finding_add_requires_finding_write_permission(self):
        original = AGENT_PERMISSIONS["qa_test"].copy()
        AGENT_PERMISSIONS["qa_test"] = original - {"finding_write"}
        try:
            act = Actuators("qa_test", "task-noperm", dry_run=False)
            env = {k: v for k, v in os.environ.items() if k != "COORDINATOR_ANALYSIS_ONLY"}
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(PermissionError):
                    act.finding_add("k", "T", "d")
        finally:
            AGENT_PERMISSIONS["qa_test"] = original

    def test_finding_add_allowed_in_analysis_mode(self):
        act = self._make("infra_engineer")
        conn, _ = self._mock_conn(fetchone_value=("id",))
        with patch.dict(os.environ, {
            "COORDINATOR_ANALYSIS_ONLY": "true",
            "DATABASE_URL": "postgres://test",
        }):
            with patch("psycopg2.connect", return_value=conn):
                result = act.finding_add("k", "T", "d")
        assert result is True

    def test_finding_write_is_held_by_the_agents_that_file_findings(self):
        """Not by every agent: a grant no handler uses is not kept. Which
        handler uses each one is in tests/test_coordinator_permission_use.py."""
        holders = {agent for agent, perms in AGENT_PERMISSIONS.items() if "finding_write" in perms}
        assert holders == {
            "infra_engineer", "security", "backend_engineer", "customer_service", "documentation", "architect",
        }
        assert not holders & NON_AGENT_PRINCIPALS

    def test_finding_write_in_analysis_only_permissions(self):
        assert "finding_write" in _ANALYSIS_ONLY_PERMISSIONS

    def test_source_task_id_passed_through(self):
        act = self._make()
        conn, cur = self._mock_conn(fetchone_value=("new-id",))
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}):
            with patch("psycopg2.connect", return_value=conn):
                act.finding_add("k", "T", "d", source_task_id="task-abc")
        params = cur.execute.call_args[0][1]
        assert params[0] == "task-abc"


# ---------------------------------------------------------------------------
# TestBackendEngineerAnalysisFallback
# ---------------------------------------------------------------------------

class TestBackendEngineerAnalysisFallback:
    """backend_engineer falls back to roadmap_add when write actions are blocked."""

    def _make_task(self, category: str, context: dict):
        from coordinator.state import WorkItem
        from datetime import datetime, timezone
        return WorkItem(
            category=category,
            target_agent="backend_engineer",
            description=f"Test {category}",
            context=context,
            context_hash="hash",
            created_at=datetime.now(timezone.utc),
        )

    def _make_state(self):
        from coordinator.state import WorldState
        from datetime import datetime, timezone
        return WorldState(
            tick_id="t", tick_number=1,
            tick_start=datetime.now(timezone.utc),
            queued_builds=0, active_builds=0,
            failed_builds_24h=[], preview_awaiting=0,
            abandoned_intakes_24h=0, pending_feedback=0,
            monitored_apps_down=[], monitored_apps_degraded=[],
            api_cost_today_usd=0.0, daily_budget_remaining_usd=20.0,
            agent_success_rates={}, agent_avg_cost={},
            pending_work=[], previous_tick_summary={},
        )

    def test_build_failure_calls_finding_add_in_analysis_mode(self):
        from coordinator.agents.backend_engineer import BackendEngineer

        agent = BackendEngineer()
        task = self._make_task("build_failure", {"build_id": "abc12345", "error_message": "crash"})
        state = self._make_state()

        act = Actuators("backend_engineer", "t", dry_run=True)

        finding_calls = []

        def fake_finding_add(finding_key, title, description, priority="medium", category="improvement", source_task_id=None):
            finding_calls.append({"key": finding_key, "title": title, "category": category})
            return True

        act.finding_add = fake_finding_add

        with patch.dict(os.environ, {"COORDINATOR_ANALYSIS_ONLY": "true"}):
            result = agent.execute(task, state, "claude-opus-4-6", act)

        assert result.success is False
        assert len(finding_calls) == 1
        assert "abc12345" in finding_calls[0]["key"]
        assert finding_calls[0]["category"] == "build_failure"


# ---------------------------------------------------------------------------
# TestInfraAgentAnalysisFallback
# ---------------------------------------------------------------------------

class TestInfraAgentAnalysisFallback:
    """infra_engineer records a finding when no playbook exists for a failure."""

    def test_no_playbook_adds_finding(self):
        from coordinator.agents.infra_engineer import InfraEngineer
        from coordinator.state import WorkItem, WorldState
        from datetime import datetime, timezone

        agent = InfraEngineer()
        task = WorkItem(
            category="build_failure",
            target_agent="infra_engineer",
            description="build failed",
            context={"build_id": "feed1234", "error_message": "novel error pattern"},
            context_hash="h",
            created_at=datetime.now(timezone.utc),
        )
        state = WorldState(
            tick_id="t", tick_number=1,
            tick_start=datetime.now(timezone.utc),
            queued_builds=0, active_builds=0,
            failed_builds_24h=[], preview_awaiting=0,
            abandoned_intakes_24h=0, pending_feedback=0,
            monitored_apps_down=[], monitored_apps_degraded=[],
            api_cost_today_usd=0.0, daily_budget_remaining_usd=20.0,
            agent_success_rates={}, agent_avg_cost={},
            pending_work=[], previous_tick_summary={},
        )
        act = Actuators("infra_engineer", "t", dry_run=True)

        finding_calls = []

        def fake_finding_add(finding_key, title, description, priority="medium", category="improvement", source_task_id=None):
            finding_calls.append({"key": finding_key, "title": title, "category": category})
            return True

        act.finding_add = fake_finding_add

        with patch("ops.scanner.classify_error", return_value="unknown_category"), \
             patch("ops.playbook.match_playbook", return_value=None):
            result = agent.execute(task, state, "claude-opus-4-6", act)

        assert result.success is False
        assert result.escalate is True
        assert len(finding_calls) == 1
        assert "feed1234" in finding_calls[0]["key"]
        assert finding_calls[0]["category"] == "no_playbook"
