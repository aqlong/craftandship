# tests/test_reconciliation_agent.py: Tests for coordinator/agents/reconciliation.py
#
# The agent reads (a read-only query, a Railway probe) and, for a failed build
# it finds running, files an approval request and sends one alert. It never
# writes a build row. Covers:
#   1. Query window includes 72h of failed builds
#   2. probe_for_zombie_deploy imported from queue.watchdog_probe (shared helper)
#   3. Live probe result: approval request and alert through the real
#      Actuators, and no connection that could write a build row
#   4. Dead/None probe result leaves build skipped
#   5. Probe error is caught, build added to errors list
#   6. Dry-run: nothing filed or sent, the find is still reported
#   7. The alert says what happened (a request was filed), not that the build
#      was flipped
#   8. AgentResult shape: success=True, output keys present
#   9. RECONCILIATION_SWEEP periodic trigger in work_detection
#  10. Trigger respects AGENT_RECONCILIATION_ENABLED=false (no item)
#  11. Trigger respects COORDINATOR_RECONCILIATION_INTERVAL_H env var
#  12. Agent disabled by default in _is_agent_enabled

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make coordinator and queue importable from the worktree root
_REPO_ROOT = str(Path(__file__).parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from coordinator.actuators import AGENT_PERMISSIONS, Actuators
from coordinator.agents.reconciliation import ReconciliationAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_actuators(dry_run=False):
    """A mock with the Actuators interface (spec'd, so an attribute the real
    class does not have, such as a database connection, cannot be invented)."""
    act = MagicMock(spec=Actuators)
    act.dry_run = dry_run
    act.query_readonly.return_value = []
    return act


def _make_row(
    build_id="aaaaaaaa-0000-0000-0000-000000000001",
    app_name="test-app",
    started_at=None,
    completed_at=None,
    error_message="supervisor timeout force-fail",
    railway_project_id=None,
):
    return (
        build_id,
        app_name,
        started_at or datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
        completed_at or datetime(2026, 4, 10, 13, 0, tzinfo=timezone.utc),
        error_message,
        railway_project_id,
    )


def _make_task():
    task = MagicMock()
    task.id = "task-001"
    return task


def _make_state():
    state = MagicMock()
    return state


_LIVE = {"live_url": "https://test-app.up.railway.app", "project_id": "proj-123", "service_id": "svc-456"}


# ---------------------------------------------------------------------------
# Shared probe import
# ---------------------------------------------------------------------------

class TestSharedProbeImport:
    """probe_for_zombie_deploy is the helper the worker's supervisor uses."""

    def test_probe_imported_from_watchdog_probe_module(self):
        """queue/watchdog_probe.py is loaded as the flat module 'watchdog_probe'
        (queue/ has no __init__.py, which avoids shadowing the stdlib queue),
        the same module worker_pg.py's _supervisor_reality_probe imports."""
        import coordinator.agents.reconciliation as rec_mod
        import watchdog_probe

        assert rec_mod.probe_for_zombie_deploy is watchdog_probe.probe_for_zombie_deploy


# ---------------------------------------------------------------------------
# SQL query window
# ---------------------------------------------------------------------------

class TestCandidateQuery:
    """The candidate query: failed builds of the last 72 hours whose error
    names a supervisor timeout. tests/test_coordinator_tick_realdb.py runs it
    on a real database; these pin its shape."""

    def test_query_covers_failed_builds_of_the_last_72_hours(self):
        sql, params = ReconciliationAgent._candidate_query()
        assert "status = 'failed'" in sql
        assert params[0] == 72

    def test_query_filters_zombie_indicators(self):
        _sql, params = ReconciliationAgent._candidate_query()
        assert params[1] == ["%supervisor%", "%timeout%", "%timed out%", "%force-fail%", "%watchdog%"]

    def test_query_text_has_no_percent_sign_that_is_not_a_placeholder(self):
        """query_readonly sends the statement with a parameter tuple, and the
        driver then reads every percent sign in the text as a placeholder. A
        pattern written into the SQL ('%supervisor%') made this query fail on
        every real run."""
        sql, params = ReconciliationAgent._candidate_query()
        assert "%" not in sql.replace("%s", "")
        assert sql.count("%s") == len(params)

    def test_query_is_a_read(self):
        assert ReconciliationAgent._candidate_query()[0].strip().upper().startswith("SELECT")

    def test_the_sweep_sends_the_query_with_its_parameters(self):
        actuators = _make_actuators()
        ReconciliationAgent()._run_sweep(actuators)
        assert actuators.query_readonly.call_args.args == ReconciliationAgent._candidate_query()


# ---------------------------------------------------------------------------
# Live probe result: a request and an alert, never a build write
# ---------------------------------------------------------------------------

class TestProcessRowsLive:
    def test_live_probe_files_an_approval_request_and_alerts(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators(dry_run=False)

        with patch("coordinator.agents.reconciliation.probe_for_zombie_deploy", return_value=_LIVE):
            result = agent._process_rows([_make_row()], actuators)

        assert result.success is True
        assert result.output["recovered"] == [{
            "build_id": "aaaaaaaa-0000-0000-0000-000000000001", "app_name": "test-app",
            "live_url": "https://test-app.up.railway.app", "project_id": "proj-123",
        }]
        assert result.output["skipped_count"] == 0 and result.output["error_count"] == 0
        request = actuators.create_approval_request.call_args.kwargs
        assert request["request_type"] == "reconciliation_flip_to_preview"
        assert request["context"]["build_id"] == "aaaaaaaa-0000-0000-0000-000000000001"
        actuators.send_alert.assert_called_once()

    def test_with_the_real_actuators_no_build_row_is_written(self, monkeypatch):
        """End to end through the real Actuators, live (not dry run), with a
        database configured: the only statements sent are the two reads and the
        approval_requests insert. Nothing updates builds."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/does_not_matter")
        monkeypatch.delenv("COORDINATOR_ANALYSIS_ONLY", raising=False)
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        conn = MagicMock()
        cur = conn.cursor.return_value
        cur.description = [(col,) for col in ReconciliationAgent._CANDIDATE_COLUMNS]
        cur.fetchall.side_effect = [[_make_row()], []]   # candidates, then the recorded-projects lookup
        cur.fetchone.return_value = ("request-id",)
        actuators = Actuators("reconciliation", "task-rec", dry_run=False)

        with patch("psycopg2.connect", return_value=conn), \
             patch("coordinator.agents.reconciliation.probe_for_zombie_deploy", return_value=_LIVE), \
             patch("ops.alerter.send_admin_alert", return_value=True) as alert:
            result = ReconciliationAgent().execute(_make_task(), _make_state(), "claude-haiku-4-5", actuators)

        assert result.success is True and len(result.output["recovered"]) == 1
        statements = [c.args[0] for c in cur.execute.call_args_list]
        writes = [s for s in statements if not s.lstrip().upper().startswith(("SELECT", "SET "))]
        assert len(writes) == 1 and "INSERT INTO approval_requests" in writes[0]
        assert not any("UPDATE" in s.upper() for s in statements)
        alert.assert_called_once()

    def test_no_permission_in_the_table_lets_the_agent_write_a_build(self):
        assert AGENT_PERMISSIONS["reconciliation"] == {"email_send", "approval_request"}

    def test_every_recorded_project_is_excluded_from_the_probe(self):
        """app_name is not unique, and the worker records a project on a build
        row only at the preview write. So a project on ANY row of this app
        name is an announced deployment: another build's, or an earlier one of
        the failed build being reconciled. None may be adopted as its zombie."""
        agent = ReconciliationAgent()
        actuators = _make_actuators(dry_run=False)
        actuators.query_readonly.return_value = [
            {"railway_project_id": "proj-of-another-build"}, {"railway_project_id": None},
            {"railway_project_id": "proj-own-earlier-deployment"},
        ]
        with patch(
            "coordinator.agents.reconciliation.probe_for_zombie_deploy", return_value=None,
        ) as probe:
            result = agent._process_rows(
                [_make_row(railway_project_id="proj-own-earlier-deployment")], actuators)

        assert probe.call_args.kwargs["exclude_project_ids"] == {
            "proj-of-another-build", "proj-own-earlier-deployment"}
        sql, params = actuators.query_readonly.call_args[0]
        # every row of the app name, this build's own row included
        assert "app_name = %s" in sql and "railway_project_id IS NOT NULL" in sql
        assert "id <>" not in sql and "id !=" not in sql
        assert params == ("test-app",)
        assert result.output["recovered"] == []

    def test_the_candidate_rows_own_project_is_excluded_even_if_the_lookup_returns_nothing(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators(dry_run=False)
        actuators.query_readonly.return_value = []
        with patch(
            "coordinator.agents.reconciliation.probe_for_zombie_deploy", return_value=None,
        ) as probe:
            agent._process_rows([_make_row(railway_project_id="proj-own-earlier-deployment")], actuators)
        assert probe.call_args.kwargs["exclude_project_ids"] == {"proj-own-earlier-deployment"}

    def test_failed_ownership_lookup_means_no_probe_and_no_request(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators(dry_run=False)
        actuators.query_readonly.side_effect = RuntimeError("db down")
        with patch(
            "coordinator.agents.reconciliation.probe_for_zombie_deploy", return_value=_LIVE,
        ) as probe:
            result = agent._process_rows([_make_row()], actuators)

        probe.assert_not_called()
        assert result.output["recovered"] == [] and result.output["error_count"] == 1
        actuators.create_approval_request.assert_not_called()


# ---------------------------------------------------------------------------
# Dead probe result skips build
# ---------------------------------------------------------------------------

class TestProcessRowsDead:
    """A None probe result means not live: the build is skipped."""

    def test_none_probe_result_skips_build(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators()

        with patch("coordinator.agents.reconciliation.probe_for_zombie_deploy", return_value=None):
            result = agent._process_rows([_make_row()], actuators)

        assert result.success is True
        assert len(result.output["recovered"]) == 0
        assert result.output["skipped_count"] == 1
        assert result.output["error_count"] == 0

    def test_probe_result_without_live_url_skips(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators()

        with patch(
            "coordinator.agents.reconciliation.probe_for_zombie_deploy",
            return_value={"project_id": "p1"},  # no live_url
        ):
            result = agent._process_rows([_make_row()], actuators)

        assert result.output["skipped_count"] == 1
        actuators.create_approval_request.assert_not_called()


# ---------------------------------------------------------------------------
# Probe error is caught
# ---------------------------------------------------------------------------

class TestProcessRowsProbeError:
    def test_probe_exception_added_to_errors(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators()

        with patch(
            "coordinator.agents.reconciliation.probe_for_zombie_deploy",
            side_effect=RuntimeError("network timeout"),
        ):
            result = agent._process_rows([_make_row()], actuators)

        assert result.success is True
        assert result.output["error_count"] == 1
        assert len(result.output["recovered"]) == 0


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

class TestDryRun:
    """In dry run the find is reported and nothing is filed or sent."""

    def test_dry_run_files_no_request_and_sends_no_alert(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators(dry_run=True)

        with patch("coordinator.agents.reconciliation.probe_for_zombie_deploy", return_value=_LIVE):
            result = agent._process_rows([_make_row()], actuators)

        actuators.create_approval_request.assert_not_called()
        actuators.send_alert.assert_not_called()
        assert len(result.output["recovered"]) == 1
        assert result.output["dry_run"] is True

    def test_dry_run_with_the_real_actuators_opens_only_read_connections(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/does_not_matter")
        conn = MagicMock()
        cur = conn.cursor.return_value
        cur.description = [(col,) for col in ReconciliationAgent._CANDIDATE_COLUMNS]
        cur.fetchall.side_effect = [[_make_row()], []]
        actuators = Actuators("reconciliation", "task-rec", dry_run=True)

        with patch("psycopg2.connect", return_value=conn), \
             patch("coordinator.agents.reconciliation.probe_for_zombie_deploy", return_value=_LIVE), \
             patch("ops.alerter.send_admin_alert") as alert:
            result = ReconciliationAgent().execute(_make_task(), _make_state(), "claude-haiku-4-5", actuators)

        assert len(result.output["recovered"]) == 1
        statements = [c.args[0] for c in cur.execute.call_args_list]
        assert all(s.lstrip().upper().startswith(("SELECT", "SET ")) for s in statements), statements
        conn.commit.assert_not_called()
        alert.assert_not_called()


# ---------------------------------------------------------------------------
# Alert email
# ---------------------------------------------------------------------------

class TestRecoveryAlert:
    def test_alert_names_the_build_and_says_nothing_was_changed(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators()

        agent._send_recovery_alert("build-abc", actuators)

        kwargs = actuators.send_alert.call_args.kwargs
        assert "build-abc" in kwargs["subject"] and "build-abc" in kwargs["body"]
        assert "Nothing was changed" in kwargs["body"]
        assert "approval request" in kwargs["body"]
        assert "has been flipped" not in kwargs["body"]

    def test_send_alert_exception_does_not_propagate(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators()
        actuators.send_alert.side_effect = RuntimeError("Resend down")

        # The alert is attempted once, and its failure stays inside the helper.
        assert agent._send_recovery_alert("build-xyz", actuators) is None
        actuators.send_alert.assert_called_once()
        assert "build-xyz" in actuators.send_alert.call_args.kwargs["subject"]


# ---------------------------------------------------------------------------
# AgentResult shape
# ---------------------------------------------------------------------------

class TestAgentResultShape:
    def test_execute_returns_agent_result_with_expected_keys(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators()

        result = agent.execute(
            task=_make_task(),
            state=_make_state(),
            model="claude-sonnet-4-6",
            actuators=actuators,
        )

        assert result.success is True
        assert result.output == {"recovered": [], "skipped_count": 0, "error_count": 0, "dry_run": False}

    def test_execute_returns_failure_on_exception(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators()

        with patch.object(agent, "_run_sweep", side_effect=Exception("DB gone")):
            result = agent.execute(
                task=_make_task(),
                state=_make_state(),
                model="claude-sonnet-4-6",
                actuators=actuators,
            )

        assert result.success is False
        assert "DB gone" in result.output.get("error", "")


# ---------------------------------------------------------------------------
# RECONCILIATION_SWEEP periodic trigger in work_detection
# ---------------------------------------------------------------------------

def _conn_with_last_sweep(value):
    """Mock conn whose cursor.fetchone() returns the last reconciliation_sweep
    time (value). CO5: the sweep now gates on the actual last sweep time queried
    from agent_tasks, not on last_tick_time."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (value,)
    conn.cursor.return_value = cur
    return conn


class TestReconciliationSweepTrigger:
    """Tests 11-13 spec: periodic trigger in work_detection."""

    def test_trigger_fires_when_enabled_and_interval_elapsed(self):
        """Trigger emits a WorkItem when AGENT_RECONCILIATION_ENABLED=true and
        enough time has passed since the LAST SWEEP (CO5: gated on the last
        sweep time, not last_tick_time)."""
        from coordinator.work_detection import _check_reconciliation_sweep

        now = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
        # last sweep 3 hours ago (> 2h default interval) -> should fire. The
        # recent last_tick (30 min) proves the gate ignores it now.
        conn = _conn_with_last_sweep(datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc))
        last_tick = datetime(2026, 4, 14, 11, 30, tzinfo=timezone.utc)

        with patch.dict(os.environ, {"AGENT_RECONCILIATION_ENABLED": "true"}, clear=False):
            items = _check_reconciliation_sweep(conn, now, last_tick)

        assert len(items) == 1
        assert items[0].category == "reconciliation_sweep"
        assert items[0].target_agent == "reconciliation"

    def test_trigger_suppressed_when_disabled(self):
        """When AGENT_RECONCILIATION_ENABLED is not 'true', no item emitted."""
        from coordinator.work_detection import _check_reconciliation_sweep

        now = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
        last_tick = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)

        for val in ("", "false", "0", "no"):
            with patch.dict(os.environ, {"AGENT_RECONCILIATION_ENABLED": val}, clear=False):
                items = _check_reconciliation_sweep(MagicMock(), now, last_tick)
            assert items == [], f"Expected no items when AGENT_RECONCILIATION_ENABLED={val!r}"

    def test_trigger_respects_interval_env_var(self):
        """COORDINATOR_RECONCILIATION_INTERVAL_H controls the minimum gap."""
        from coordinator.work_detection import _check_reconciliation_sweep

        now = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
        # last sweep only 1h ago -- within the 4h custom interval -> no fire.
        conn = _conn_with_last_sweep(datetime(2026, 4, 14, 11, 0, tzinfo=timezone.utc))
        last_tick = datetime(2026, 4, 14, 11, 30, tzinfo=timezone.utc)

        env = {
            "AGENT_RECONCILIATION_ENABLED": "true",
            "COORDINATOR_RECONCILIATION_INTERVAL_H": "4",
        }
        with patch.dict(os.environ, env, clear=False):
            items = _check_reconciliation_sweep(conn, now, last_tick)

        assert items == [], "Trigger must not fire within the custom interval"

    def test_trigger_fires_when_no_prior_sweep(self):
        """When no reconciliation_sweep has ever run (last sweep None), the
        trigger fires immediately (CO5: first-run gate via the sweep query)."""
        from coordinator.work_detection import _check_reconciliation_sweep

        now = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
        conn = _conn_with_last_sweep(None)  # no prior sweep task
        with patch.dict(os.environ, {"AGENT_RECONCILIATION_ENABLED": "true"}, clear=False):
            items = _check_reconciliation_sweep(conn, now, last_tick_time=None)

        assert len(items) == 1


# ---------------------------------------------------------------------------
# Test 14: Agent disabled by default in _is_agent_enabled
# ---------------------------------------------------------------------------

class TestAgentDisabledByDefault:
    """Test 14 spec: reconciliation agent disabled by default."""

    def test_reconciliation_disabled_by_default(self):
        from coordinator.tick import _is_agent_enabled

        # Unset the env var to test the default
        env = {}
        # Remove AGENT_RECONCILIATION_ENABLED if present
        with patch.dict(os.environ, env, clear=False):
            # Ensure the key is absent
            os.environ.pop("AGENT_RECONCILIATION_ENABLED", None)
            result = _is_agent_enabled("reconciliation", global_dry_run=False)

        assert result is False, (
            "ReconciliationAgent must be DISABLED by default; "
            "set AGENT_RECONCILIATION_ENABLED=true to enable"
        )

    def test_reconciliation_enabled_when_env_set(self):
        from coordinator.tick import _is_agent_enabled

        with patch.dict(os.environ, {"AGENT_RECONCILIATION_ENABLED": "true"}, clear=False):
            result = _is_agent_enabled("reconciliation", global_dry_run=False)

        assert result is True

    def test_reconciliation_disabled_when_env_false(self):
        from coordinator.tick import _is_agent_enabled

        with patch.dict(os.environ, {"AGENT_RECONCILIATION_ENABLED": "false"}, clear=False):
            result = _is_agent_enabled("reconciliation", global_dry_run=False)

        assert result is False


# ---------------------------------------------------------------------------
# Bonus: register() wires agent into tick registry
# ---------------------------------------------------------------------------

class TestRegister:
    def test_register_adds_reconciliation_to_registry(self):
        from coordinator.tick import get_agent_registry, register_agent
        from coordinator.agents.reconciliation import register, ReconciliationAgent

        register()
        registry = get_agent_registry()
        assert "reconciliation" in registry
        assert registry["reconciliation"] is ReconciliationAgent


# ---------------------------------------------------------------------------
# Integration: _run_sweep (rows come from Actuators.query_readonly as dicts)
# ---------------------------------------------------------------------------

class TestRunSweep:
    def test_full_path_live_probe(self):
        """query_readonly returns a row, the probe is live, an approval request is filed."""
        agent = ReconciliationAgent()
        actuators = _make_actuators(dry_run=False)
        row = dict(zip(ReconciliationAgent._CANDIDATE_COLUMNS, _make_row()))
        actuators.query_readonly.side_effect = [[row], []]

        with patch("coordinator.agents.reconciliation.probe_for_zombie_deploy",
                   return_value=_LIVE) as probe:
            result = agent._run_sweep(actuators)

        assert result.success is True
        assert len(result.output["recovered"]) == 1
        assert probe.call_args.kwargs["app_name"] == "test-app"
        actuators.create_approval_request.assert_called_once()

    def test_query_readonly_failure(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators()
        actuators.query_readonly.side_effect = RuntimeError("DB gone")

        result = agent._run_sweep(actuators)

        assert result.success is False
        assert "query_readonly failed" in result.output.get("error", "")

    def test_empty_rows_succeeds(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators()

        result = agent._run_sweep(actuators)

        assert result.success is True
        assert result.output["recovered"] == []
        assert result.output["skipped_count"] == 0


# ---------------------------------------------------------------------------
# Multi-row sweep: mixed results in a single pass
# ---------------------------------------------------------------------------

class TestMultiRowSweep:
    """One sweep with live + skipped + error rows in a single call."""

    def test_mixed_rows_counts_all_categories(self):
        agent = ReconciliationAgent()
        actuators = _make_actuators(dry_run=False)

        rows = [
            _make_row(build_id="b-live-0001", app_name="app-live"),
            _make_row(build_id="b-dead-0002", app_name="app-dead"),
            _make_row(build_id="b-err-0003",  app_name="app-err"),
        ]

        def fake_probe(app_name, started_at, completed_at):
            if app_name == "app-live":
                return {"live_url": "https://app-live.railway.app",
                        "project_id": "p1", "service_id": "s1"}
            if app_name == "app-dead":
                return None
            raise RuntimeError("probe network error")

        with patch("coordinator.agents.reconciliation.probe_for_zombie_deploy",
                   side_effect=fake_probe):
            result = agent._process_rows(rows, actuators)

        assert result.success is True
        assert len(result.output["recovered"]) == 1
        assert result.output["recovered"][0]["build_id"] == "b-live-0001"
        assert result.output["skipped_count"] == 1
        assert result.output["error_count"] == 1

    def test_all_skipped_no_approval_requests(self):
        """When all probes return None, no approval requests are made."""
        agent = ReconciliationAgent()
        actuators = _make_actuators(dry_run=False)
        rows = [
            _make_row(build_id="b-dead-0001"),
            _make_row(build_id="b-dead-0002"),
        ]
        with patch("coordinator.agents.reconciliation.probe_for_zombie_deploy",
                   return_value=None):
            agent._process_rows(rows, actuators)
        actuators.create_approval_request.assert_not_called()

    def test_learnings_populated_when_recovered(self):
        """AgentResult.learnings is non-empty when at least one zombie recovered."""
        agent = ReconciliationAgent()
        actuators = _make_actuators(dry_run=False)
        rows = [_make_row()]
        probe_result = {"live_url": "https://app.railway.app",
                        "project_id": "p1", "service_id": "s1"}

        with patch("coordinator.agents.reconciliation.probe_for_zombie_deploy",
                   return_value=probe_result):
            result = agent._process_rows(rows, actuators)

        assert result.learnings, "learnings should be non-empty when a zombie is recovered"

    def test_learnings_empty_when_no_recovery(self):
        """AgentResult.learnings is empty when nothing was recovered."""
        agent = ReconciliationAgent()
        actuators = _make_actuators()
        rows = [_make_row()]
        with patch("coordinator.agents.reconciliation.probe_for_zombie_deploy",
                   return_value=None):
            result = agent._process_rows(rows, actuators)
        assert result.learnings == []
