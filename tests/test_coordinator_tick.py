# tests/test_coordinator_tick.py
#
# Covers internal helpers in coordinator/tick.py.
# The full tick lifecycle is covered by tests/test_coordinator.py.
# These tests focus on pure/near-pure helpers not touched there.

import os
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from coordinator.state import WorkItem
from coordinator.tick import (
    AGENT_TIMEOUTS,
    CATEGORY_TIMEOUTS,
    ACTIONS_REQUIRING_REVIEW,
    REVIEWER_MAP,
    _DecimalEncoder,
    _json_dumps,
    _get_agent_timeout,
    _group_independent_tasks,
    _is_agent_enabled,
    _persist_work_item,
    _check_and_queue_review,
    register_agent,
    get_agent_registry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(category="build_failure", agent="backend_engineer", context=None):
    return WorkItem(
        id="task-abc",
        category=category,
        target_agent=agent,
        description="test",
        context=context or {},
        context_hash="deadbeef1234",
        created_at=datetime.now(timezone.utc),
    )


def _make_ctx_cursor_conn(rows=None):
    """Mock conn whose cursor() is a context manager returning a cursor with rows."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur.__exit__ = MagicMock(return_value=False)
    if rows is not None:
        mock_cur.fetchall.return_value = rows
    mock_conn.cursor.return_value = mock_cur
    return mock_conn, mock_cur


# ---------------------------------------------------------------------------
# TestDecimalEncoder
# ---------------------------------------------------------------------------

class TestDecimalEncoder:
    """_DecimalEncoder converts Decimal to float; _json_dumps uses it."""

    def test_decimal_serialized_as_float(self):
        import json
        result = json.dumps({"v": Decimal("3.14")}, cls=_DecimalEncoder)
        assert result == '{"v": 3.14}'

    def test_regular_values_unchanged(self):
        result = _json_dumps({"a": 1, "b": "hello", "c": [1, 2, 3]})
        assert '"a": 1' in result

    def test_nested_decimal(self):
        result = _json_dumps({"cost": Decimal("0.12345")})
        assert "0.12345" in result


# ---------------------------------------------------------------------------
# TestGetAgentTimeout
# ---------------------------------------------------------------------------

class TestGetAgentTimeout:
    """_get_agent_timeout() returns category timeout when set, otherwise agent timeout."""

    def test_category_timeout_takes_precedence(self):
        # budget_alert is in CATEGORY_TIMEOUTS at 300, well under the agent's own default
        assert _get_agent_timeout("backend_engineer", "budget_alert") == CATEGORY_TIMEOUTS["budget_alert"] == 300

    def test_agent_timeout_used_when_no_category_override(self):
        assert _get_agent_timeout("financial", "cost_report_not_in_cat") == AGENT_TIMEOUTS["financial"]

    def test_unknown_agent_falls_back_to_1800(self):
        assert _get_agent_timeout("ghost_agent", "unknown_category") == 1800

    def test_build_failure_uses_category_timeout(self):
        assert _get_agent_timeout("infra_engineer", "build_failure") == CATEGORY_TIMEOUTS["build_failure"]


# ---------------------------------------------------------------------------
# TestGroupIndependentTasks
# ---------------------------------------------------------------------------

class TestGroupIndependentTasks:
    """_group_independent_tasks() batches items that can safely run in parallel."""

    def test_single_task_forms_one_batch(self):
        task = _make_task()
        batches = _group_independent_tasks([(0.9, task)])
        assert len(batches) == 1
        assert len(batches[0]) == 1

    def test_same_agent_forces_separate_batches(self):
        t1 = _make_task(category="build_failure", agent="backend_engineer")
        t1.id = "task-1"
        t2 = _make_task(category="test_build", agent="backend_engineer")
        t2.id = "task-2"
        batches = _group_independent_tasks([(0.9, t1), (0.7, t2)])
        assert len(batches) == 2

    def test_same_category_forces_separate_batches(self):
        t1 = _make_task(category="build_failure", agent="infra_engineer")
        t1.id = "task-1"
        t2 = _make_task(category="build_failure", agent="backend_engineer")
        t2.id = "task-2"
        batches = _group_independent_tasks([(0.9, t1), (0.8, t2)])
        assert len(batches) == 2

    def test_different_agent_and_category_runs_in_parallel(self):
        t1 = _make_task(category="build_failure", agent="backend_engineer")
        t1.id = "task-1"
        t2 = _make_task(category="cost_report", agent="financial")
        t2.id = "task-2"
        batches = _group_independent_tasks([(0.9, t1), (0.5, t2)])
        assert len(batches) == 1
        assert len(batches[0]) == 2

    def test_three_tasks_two_independent_one_conflict(self):
        t1 = _make_task(category="build_failure", agent="backend_engineer")
        t1.id = "task-1"
        t2 = _make_task(category="cost_report", agent="financial")
        t2.id = "task-2"
        t3 = _make_task(category="build_failure", agent="infra_engineer")
        t3.id = "task-3"
        # t1 and t2 are independent; t3 conflicts on category with t1
        batches = _group_independent_tasks([(0.9, t1), (0.5, t2), (0.8, t3)])
        # t1+t2 in one batch, t3 in another
        assert len(batches) == 2

    def test_empty_input_returns_empty(self):
        assert _group_independent_tasks([]) == []


# ---------------------------------------------------------------------------
# TestIsAgentEnabled
# ---------------------------------------------------------------------------

class TestIsAgentEnabled:
    """_is_agent_enabled() reads env vars per-agent."""

    def test_unknown_agent_defaults_to_true(self):
        assert _is_agent_enabled("ghost_agent", False) is True

    def test_env_var_false_disables_agent(self):
        with patch.dict(os.environ, {"AGENT_INFRA_ENABLED": "false"}):
            assert _is_agent_enabled("infra_engineer", False) is False

    def test_env_var_true_enables_agent(self):
        with patch.dict(os.environ, {"AGENT_INFRA_ENABLED": "true"}):
            assert _is_agent_enabled("infra_engineer", False) is True

    def test_env_var_0_disables_agent(self):
        with patch.dict(os.environ, {"AGENT_BACKEND_ENABLED": "0"}):
            assert _is_agent_enabled("backend_engineer", False) is False

    def test_env_var_1_enables_agent(self):
        with patch.dict(os.environ, {"AGENT_BACKEND_ENABLED": "1"}):
            assert _is_agent_enabled("backend_engineer", False) is True

    def test_env_var_none_defaults_to_true(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENT_FINANCIAL_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            assert _is_agent_enabled("financial", False) is True

    @pytest.mark.parametrize("agent, env_key", [
        ("infra_engineer", "AGENT_INFRA_ENABLED"),
        ("financial", "AGENT_FINANCIAL_ENABLED"),
        ("customer_service", "AGENT_CUSTOMER_ENABLED"),
        ("documentation", "AGENT_DOCS_ENABLED"),
        ("security", "AGENT_SECURITY_ENABLED"),
        ("qa_test", "AGENT_QA_ENABLED"),
        ("backend_engineer", "AGENT_BACKEND_ENABLED"),
        ("cost_analyst", "AGENT_COST_ENABLED"),
        ("architect", "AGENT_ARCHITECT_ENABLED"),
    ])
    def test_each_agent_is_switched_by_its_own_env_var_and_no_other(self, agent, env_key):
        others = {k: "false" for k in (
            "AGENT_INFRA_ENABLED", "AGENT_FINANCIAL_ENABLED", "AGENT_CUSTOMER_ENABLED",
            "AGENT_DOCS_ENABLED", "AGENT_SECURITY_ENABLED", "AGENT_QA_ENABLED",
            "AGENT_BACKEND_ENABLED", "AGENT_COST_ENABLED", "AGENT_ARCHITECT_ENABLED",
        ) if k != env_key}
        with patch.dict(os.environ, {**others, env_key: "true"}):
            assert _is_agent_enabled(agent, False) is True
        with patch.dict(os.environ, {**others, env_key: "false"}):
            assert _is_agent_enabled(agent, False) is False

    def test_reconciliation_is_off_unless_explicitly_enabled(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENT_RECONCILIATION_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            assert _is_agent_enabled("reconciliation", False) is False
        with patch.dict(os.environ, {"AGENT_RECONCILIATION_ENABLED": "true"}):
            assert _is_agent_enabled("reconciliation", False) is True


# ---------------------------------------------------------------------------
# TestCheckAgentFailureRate
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# TestRecordLearning
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# TestBuildLearningContext
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# TestDecayLearningConfidence
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# TestPersistWorkItem
# ---------------------------------------------------------------------------

class TestPersistWorkItem:
    """_persist_work_item() deduplicates and returns bool."""

    def test_returns_true_on_insert(self):
        import uuid
        mock_conn, mock_cur = _make_ctx_cursor_conn()
        mock_cur.fetchone.return_value = (uuid.uuid4(),)  # row returned = inserted

        task = _make_task()
        result = _persist_work_item(mock_conn, task, tick_id="tick-1")
        assert result is True

    def test_returns_false_on_conflict(self):
        mock_conn, mock_cur = _make_ctx_cursor_conn()
        mock_cur.fetchone.return_value = None  # ON CONFLICT DO NOTHING → no row

        task = _make_task()
        result = _persist_work_item(mock_conn, task, tick_id="tick-1")
        assert result is False

    def test_computes_context_hash_when_missing(self):
        """If item has no context_hash, it should be computed before insert."""
        mock_conn, mock_cur = _make_ctx_cursor_conn()
        mock_cur.fetchone.return_value = None

        task = _make_task()
        task.context_hash = ""  # empty, should be filled in
        _persist_work_item(mock_conn, task, tick_id="tick-1")
        assert task.context_hash != ""

    def test_sql_uses_on_conflict_do_nothing(self):
        mock_conn, mock_cur = _make_ctx_cursor_conn()
        mock_cur.fetchone.return_value = None

        task = _make_task()
        _persist_work_item(mock_conn, task, tick_id="tick-1")

        call_args = mock_cur.execute.call_args
        assert "ON CONFLICT" in call_args.args[0]
        assert "DO NOTHING" in call_args.args[0]


# ---------------------------------------------------------------------------
# TestCheckAndQueueReview
# ---------------------------------------------------------------------------

class TestCheckAndQueueReview:
    """_check_and_queue_review() queues peer review for reviewable actions."""

    def _make_result_with_action(self, action_type: str):
        action = MagicMock()
        action.action_type = action_type
        result = MagicMock()
        result.actions_taken = [action]
        return result

    def test_queues_review_for_a_redeploy(self):
        mock_conn, mock_cur = _make_ctx_cursor_conn()
        task = _make_task(agent="backend_engineer")
        result = self._make_result_with_action("railway_deploy")

        _check_and_queue_review(mock_conn, task, result, "backend_engineer", "tick-1")

        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args.args[0]
        assert "review_agent" in sql

    def test_no_review_for_non_reviewable_action(self):
        mock_conn, mock_cur = _make_ctx_cursor_conn()
        task = _make_task(agent="backend_engineer")
        action = MagicMock()
        action.action_type = "kb_record"  # not in ACTIONS_REQUIRING_REVIEW
        result = MagicMock()
        result.actions_taken = [action]

        _check_and_queue_review(mock_conn, task, result, "backend_engineer", "tick-1")

        mock_cur.execute.assert_not_called()

    def test_no_review_for_agent_without_reviewer(self):
        """Agents not in REVIEWER_MAP skip review entirely."""
        mock_conn, mock_cur = _make_ctx_cursor_conn()
        task = _make_task(agent="financial")
        result = self._make_result_with_action("github_commit")

        _check_and_queue_review(mock_conn, task, result, "financial", "tick-1")
        mock_cur.execute.assert_not_called()

    def test_reviewer_map_coverage(self):
        """Spot-check REVIEWER_MAP entries are reasonable."""
        assert "backend_engineer" in REVIEWER_MAP
        assert "infra_engineer" in REVIEWER_MAP
        # All reviewers should be valid agent types (not blank)
        for k, v in REVIEWER_MAP.items():
            assert v, f"Reviewer for {k} is empty"


# ---------------------------------------------------------------------------
# TestRegisterAgent
# ---------------------------------------------------------------------------

class TestRegisterAgent:
    """register_agent() and get_agent_registry()."""

    def setup_method(self):
        # Snapshot the registry keys before each test so we can clean up additions.
        self._keys_before = set(get_agent_registry().keys())

    def teardown_method(self):
        # Remove any agents registered during the test to avoid polluting other tests.
        from coordinator import tick as _tick_mod
        for key in list(_tick_mod._agent_registry.keys()):
            if key not in self._keys_before:
                del _tick_mod._agent_registry[key]

    def test_registered_agent_appears_in_registry(self):
        class _FakeAgent:
            pass

        register_agent("_test_fake_agent", _FakeAgent)
        registry = get_agent_registry()
        assert "_test_fake_agent" in registry
        assert registry["_test_fake_agent"] is _FakeAgent

    def test_get_agent_registry_returns_copy(self):
        r1 = get_agent_registry()
        r1["injected"] = "bad"
        r2 = get_agent_registry()
        assert "injected" not in r2
