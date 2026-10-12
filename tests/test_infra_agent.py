# tests/test_infra_agent.py: Tests for the Infra Engineer agent
#
# Tests: adapter wraps ops scanner/playbook correctly, returns proper
# AgentResult, respects actuator permissions.

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = str(Path(__file__).parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from coordinator.agents.infra_engineer import InfraEngineer
from coordinator.agents.base import AgentResult
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators


class TestInfraEngineerBuildFailure:
    def setup_method(self):
        self.agent = InfraEngineer()
        self.state = WorldState()

    @patch("ops.playbook.load_fixer")
    @patch("ops.playbook.match_playbook")
    @patch("ops.scanner.classify_error")
    def test_successful_fix(self, mock_classify, mock_match, mock_load):
        mock_classify.return_value = "railway_crash"
        mock_entry = MagicMock()
        mock_match.return_value = mock_entry

        mock_fixer = MagicMock()
        mock_fixer.return_value = MagicMock(
            success=True, description="Triggered worker service redeployment",
            requires_redeploy=False,
        )
        mock_load.return_value = mock_fixer

        task = WorkItem(
            id="task-1", category="build_failure",
            context={"build_id": "b1", "error_message": "service crashed: OOMKilled"},
        )
        actuators = Actuators("infra_engineer", "task-1", dry_run=True)
        result = self.agent.execute(task, self.state, "claude-sonnet-4-6", actuators)

        assert isinstance(result, AgentResult)
        assert result.success is True
        assert result.output["category"] == "railway_crash"
        assert result.output["description"] == "Triggered worker service redeployment"
        assert "commit_sha" not in result.output and "files_changed" not in result.output
        mock_fixer.assert_called_once()
        assert mock_fixer.call_args.args[0]["dry_run"] is True

    @patch("ops.playbook.match_playbook")
    @patch("ops.scanner.classify_error")
    def test_no_playbook_escalates(self, mock_classify, mock_match):
        mock_classify.return_value = "unknown"
        mock_match.return_value = None

        task = WorkItem(
            id="task-2", category="build_failure",
            context={"build_id": "b2", "error_message": "Something weird happened"},
        )
        actuators = Actuators("infra_engineer", "task-2", dry_run=True)
        result = self.agent.execute(task, self.state, "claude-sonnet-4-6", actuators)

        assert result.success is False
        assert result.escalate is True
        assert "no playbook" in result.escalation_reason.lower()

    @patch("ops.playbook.load_fixer")
    @patch("ops.playbook.match_playbook")
    @patch("ops.scanner.classify_error")
    def test_fixer_load_failure(self, mock_classify, mock_match, mock_load):
        mock_classify.return_value = "version_mismatch"
        mock_match.return_value = MagicMock()
        mock_load.return_value = None  # Fixer failed to load

        task = WorkItem(
            id="task-3", category="build_failure",
            context={"build_id": "b3", "error_message": "version mismatch"},
        )
        actuators = Actuators("infra_engineer", "task-3", dry_run=True)
        result = self.agent.execute(task, self.state, "claude-sonnet-4-6", actuators)

        assert result.success is False
        assert result.escalate is True

    @patch("ops.playbook.load_fixer")
    @patch("ops.playbook.match_playbook")
    @patch("ops.scanner.classify_error")
    def test_records_learning_on_success(self, mock_classify, mock_match, mock_load):
        mock_classify.return_value = "env_config"
        mock_match.return_value = MagicMock()
        mock_fixer = MagicMock()
        mock_fixer.return_value = MagicMock(
            success=True, description="Set DATABASE_URL",
            files_changed=[], commit_sha=None, requires_redeploy=False,
        )
        mock_load.return_value = mock_fixer

        task = WorkItem(
            id="task-4", category="build_failure",
            context={"build_id": "b4", "error_message": "DATABASE_URL not set"},
        )
        actuators = Actuators("infra_engineer", "task-4", dry_run=True)
        result = self.agent.execute(task, self.state, "claude-sonnet-4-6", actuators)

        assert result.success is True
        assert len(result.learnings) > 0

    @patch("ops.scanner.classify_error")
    def test_exception_handled(self, mock_classify):
        mock_classify.side_effect = Exception("Import error")

        task = WorkItem(
            id="task-5", category="build_failure",
            context={"build_id": "b5", "error_message": "test"},
        )
        actuators = Actuators("infra_engineer", "task-5", dry_run=True)
        result = self.agent.execute(task, self.state, "claude-sonnet-4-6", actuators)

        assert result.success is False
        assert result.escalate is True


class TestInfraEngineerAppDown:
    def setup_method(self):
        self.agent = InfraEngineer()
        self.state = WorldState()

    def test_app_down_sends_alert(self):
        task = WorkItem(
            id="task-6", category="app_down",
            context={"app_name": "myapp", "consecutive_failures": 5},
        )
        actuators = Actuators("infra_engineer", "task-6", dry_run=True)
        result = self.agent.execute(task, self.state, "claude-sonnet-4-6", actuators)

        assert result.success is True
        # a dry run sends nothing, and says so
        assert result.output["action"] == "alert_dry_run"
        assert result.effects == [{"effect": "alert", "what": "[CAS] App down: myapp (5 failures)",
                                   "outcome": "dry_run"}]
        assert len(result.learnings) > 0

    def test_app_down_escalates_on_many_failures(self):
        task = WorkItem(
            id="task-7", category="app_down",
            context={"app_name": "myapp", "consecutive_failures": 15},
        )
        actuators = Actuators("infra_engineer", "task-7", dry_run=True)
        result = self.agent.execute(task, self.state, "claude-sonnet-4-6", actuators)

        assert result.escalate is True

    def test_app_down_no_escalation_few_failures(self):
        task = WorkItem(
            id="task-8", category="app_down",
            context={"app_name": "myapp", "consecutive_failures": 3},
        )
        actuators = Actuators("infra_engineer", "task-8", dry_run=True)
        result = self.agent.execute(task, self.state, "claude-sonnet-4-6", actuators)

        assert result.escalate is False


class TestInfraEngineerBackup:
    def test_backup_overdue(self):
        agent = InfraEngineer()
        task = WorkItem(
            id="task-9", category="backup_overdue",
            context={"last_backup_age_hours": 72},
        )
        actuators = Actuators("infra_engineer", "task-9", dry_run=True)
        result = agent.execute(task, WorldState(), "claude-haiku-4-5", actuators)

        assert result.success is True
        assert result.output["action"] == "alert_dry_run"

    def test_backup_very_overdue_escalates(self):
        agent = InfraEngineer()
        task = WorkItem(
            id="task-10", category="backup_overdue",
            context={"last_backup_age_hours": 120},
        )
        actuators = Actuators("infra_engineer", "task-10", dry_run=True)
        result = agent.execute(task, WorldState(), "claude-haiku-4-5", actuators)

        assert result.escalate is True


class TestInfraEngineerCoordinatorStalled:
    def test_diagnoses_stall(self):
        agent = InfraEngineer()
        task = WorkItem(
            id="task-stall", category="coordinator_stalled",
            context={
                "ticks_checked": 10, "window_minutes": 30,
                "total_dispatched": 0, "total_scored": 0,
                "recent_errors": ["FK constraint violation"],
            },
        )
        actuators = Actuators("infra_engineer", "task-stall", dry_run=True)
        actuators.query_readonly = MagicMock(side_effect=[
            [{"cnt": 0}],  # pending tasks
            [{"cnt": 3}],  # recent feedback
            [{"cnt": 1}],  # failed builds
        ])
        result = agent.execute(task, WorldState(), "claude-haiku-4-5", actuators)

        assert result.success is True
        assert result.output["action"] == "diagnosed"
        assert len(result.output["diagnostics"]) >= 3
        assert any("FK constraint" in d for d in result.output["diagnostics"])

    def test_stall_with_db_error(self):
        agent = InfraEngineer()
        task = WorkItem(
            id="task-stall2", category="coordinator_stalled",
            context={"ticks_checked": 5, "window_minutes": 30,
                     "total_dispatched": 0, "total_scored": 0, "recent_errors": []},
        )
        actuators = Actuators("infra_engineer", "task-stall2", dry_run=True)
        actuators.query_readonly = MagicMock(side_effect=Exception("DB gone"))
        result = agent.execute(task, WorldState(), "claude-haiku-4-5", actuators)

        assert result.success is True
        assert any("DB gone" in d for d in result.output["diagnostics"])


class TestInfraEngineerUnknownCategory:
    def test_unknown_category(self):
        agent = InfraEngineer()
        task = WorkItem(id="task-11", category="unknown_thing", context={})
        actuators = Actuators("infra_engineer", "task-11", dry_run=True)
        result = agent.execute(task, WorldState(), "claude-haiku-4-5", actuators)
        assert result.success is False
        assert len(result.learnings) > 0


class TestInfraEngineerPermissions:
    def test_cannot_use_qa_actuators(self):
        """InfraEngineer should not be able to use qa_test permissions."""
        agent = InfraEngineer()
        task = WorkItem(id="task-12", category="build_failure", context={})
        # Create actuators for a different agent type
        actuators = Actuators("qa_test", "task-12", dry_run=True)
        # The agent itself calls actuators methods which will check permissions
        # QA test agent can't record a fix in the failure KB
        with pytest.raises(PermissionError):
            actuators.record_fix("err", "fix", [], "code_error", True)

    def test_register_function(self):
        from coordinator.agents.infra_engineer import register
        from coordinator.tick import _agent_registry
        register()
        assert "infra_engineer" in _agent_registry
        assert _agent_registry["infra_engineer"] is InfraEngineer
