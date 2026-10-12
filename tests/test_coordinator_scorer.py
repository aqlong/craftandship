# tests/test_coordinator_scorer.py
#
# Unit tests for coordinator/scorer.py.  All functions are pure / near-pure
# (no API calls, minimal I/O), so we can test them exhaustively.

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from coordinator.scorer import (
    AGENT_MODELS,
    CATEGORY_WEIGHTS,
    ESTIMATED_COST,
    _cost_penalty,
    _performance_multiplier_cached,
    _urgency_multiplier,
    build_performance_cache,
    estimate_task_cost,
    score_task,
    select_model,
)
from coordinator.state import WorkItem, WorldState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(
    category: str = "build_failure",
    target_agent: str = "backend_engineer",
    context: dict = None,
    age_hours: float = 0.0,
) -> WorkItem:
    created = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return WorkItem(
        id="task-1",
        category=category,
        target_agent=target_agent,
        description="test task",
        context=context or {},
        created_at=created,
    )


def _make_state(
    daily_budget_remaining: float = 20.0,
    active_builds: int = 0,
) -> WorldState:
    state = MagicMock(spec=WorldState)
    state.daily_budget_remaining_usd = daily_budget_remaining
    state.active_builds = active_builds
    return state


def _make_budget(allowed_models=None, max_opus_calls: int = 5):
    budget = MagicMock()
    budget.allowed_models = (
        ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6"]
        if allowed_models is None
        else allowed_models
    )
    budget.max_opus_calls = max_opus_calls
    return budget


# ---------------------------------------------------------------------------
# TestCategoryWeights
# ---------------------------------------------------------------------------

class TestCategoryWeights:
    """Basic structural checks on CATEGORY_WEIGHTS."""

    def test_all_weights_between_0_and_1(self):
        for cat, w in CATEGORY_WEIGHTS.items():
            assert 0.0 < w <= 1.0, f"{cat} weight {w} out of [0, 1]"

    def test_p0_categories_highest(self):
        """P0 categories must all score > 0.80."""
        p0 = ["app_down", "backup_overdue", "build_failure"]
        for cat in p0:
            assert CATEGORY_WEIGHTS[cat] >= 0.80, f"{cat} should be P0 (>=0.80)"

    def test_agent_initiative_is_lowest(self):
        assert CATEGORY_WEIGHTS["agent_initiative"] <= min(CATEGORY_WEIGHTS.values())


# ---------------------------------------------------------------------------
# TestScoreTask
# ---------------------------------------------------------------------------

class TestScoreTask:
    """score_task() returns correct ranges and respects roadmap boost."""

    def test_score_is_between_0_and_1(self):
        task = _make_task("build_failure")
        state = _make_state()
        score = score_task(task, state)
        assert 0.0 <= score <= 1.0

    def test_p0_task_scores_high(self):
        task = _make_task("app_down", age_hours=2)
        state = _make_state()
        score = score_task(task, state)
        assert score >= 0.85

    def test_low_priority_task_scores_low(self):
        task = _make_task("agent_initiative", age_hours=0)
        state = _make_state()
        score = score_task(task, state)
        assert score < 0.30

    def test_a_priority_in_the_context_does_not_change_the_score(self):
        """Only the category, the age, the budget and the agent's record score
        a task. (A boost keyed on a roadmap item's priority went with the
        roadmap category, which no trigger emitted.)"""
        state = _make_state()
        plain = score_task(_make_task("agent_initiative", context={}), state)
        for priority in ("low", "high", "critical", "bogus"):
            boosted = score_task(_make_task("agent_initiative", context={"priority": priority}), state)
            assert abs(boosted - plain) < 0.01, priority

    def test_score_capped_at_1(self):
        """app_down with high urgency and good budget should cap at 1.0."""
        task = _make_task("app_down", age_hours=24, context={"consecutive_failures": 15})
        state = _make_state(daily_budget_remaining=20.0)
        assert score_task(task, state) == 1.0

    def test_unknown_category_gets_default_weight(self):
        task = _make_task("totally_unknown_category")
        state = _make_state()
        score = score_task(task, state)
        assert score > 0.0  # doesn't crash

    def test_performance_multiplier_applied(self):
        task = _make_task("build_failure", target_agent="infra_engineer")
        state = _make_state()
        # Poor performer (50% success) => 0.85 multiplier
        poor_cache = {("infra_engineer", "build_failure"): {"total": 10, "successful": 5}}
        good_cache = {("infra_engineer", "build_failure"): {"total": 10, "successful": 9}}
        score_poor = score_task(task, state, perf_cache=poor_cache)
        score_good = score_task(task, state, perf_cache=good_cache)
        assert score_good > score_poor

    def test_none_perf_cache_treated_as_neutral(self):
        task = _make_task("build_failure")
        state = _make_state()
        s1 = score_task(task, state, perf_cache=None)
        s2 = score_task(task, state, perf_cache={})
        # Tiny urgency drift between two datetime.now() calls; tolerance covers it
        assert s1 == pytest.approx(s2, abs=1e-4)


# ---------------------------------------------------------------------------
# TestUrgencyMultiplier
# ---------------------------------------------------------------------------

class TestUrgencyMultiplier:
    """_urgency_multiplier() tiered escalation and app_down special cases."""

    def test_fresh_task_score_is_1(self):
        task = _make_task(age_hours=0)
        state = _make_state()
        mult = _urgency_multiplier(task, state)
        assert 1.0 <= mult <= 1.5

    def test_4h_tier_capped_at_1_5(self):
        task = _make_task(age_hours=3.9)
        state = _make_state()
        mult = _urgency_multiplier(task, state)
        assert mult <= 1.5

    def test_8h_tier_capped_at_1_8(self):
        task = _make_task(age_hours=7.9)
        state = _make_state()
        mult = _urgency_multiplier(task, state)
        assert mult <= 1.8

    def test_old_task_capped_at_2(self):
        task = _make_task(age_hours=100)
        state = _make_state()
        mult = _urgency_multiplier(task, state)
        assert mult == 2.0

    def test_app_down_many_failures_returns_2(self):
        task = _make_task("app_down", context={"consecutive_failures": 15}, age_hours=0)
        state = _make_state()
        assert _urgency_multiplier(task, state) == 2.0

    def test_app_down_moderate_failures_returns_1_5(self):
        task = _make_task("app_down", context={"consecutive_failures": 8}, age_hours=0)
        state = _make_state()
        assert _urgency_multiplier(task, state) == 1.5

    def test_app_down_few_failures_uses_age(self):
        task = _make_task("app_down", context={"consecutive_failures": 3}, age_hours=0)
        state = _make_state()
        mult = _urgency_multiplier(task, state)
        assert 1.0 <= mult <= 1.5  # age-based, not failure-based

    def test_naive_datetime_coerced_to_utc(self):
        """Tasks created with naive datetimes should not crash."""
        task = _make_task(age_hours=0)
        task.created_at = task.created_at.replace(tzinfo=None)  # strip tz
        state = _make_state()
        mult = _urgency_multiplier(task, state)
        assert mult >= 1.0

    def test_monotonically_increases_with_age(self):
        state = _make_state()
        ages = [0, 2, 4, 6, 8, 12, 24]
        mults = [_urgency_multiplier(_make_task(age_hours=h), state) for h in ages]
        for i in range(len(mults) - 1):
            assert mults[i] <= mults[i + 1], f"Multiplier not monotonic at index {i}"


# ---------------------------------------------------------------------------
# TestCostPenalty
# ---------------------------------------------------------------------------

class TestCostPenalty:
    """_cost_penalty() budget throttling."""

    def test_full_budget_returns_1(self):
        state = _make_state(daily_budget_remaining=15.0)
        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0"}):
            assert _cost_penalty(state) == 1.0

    def test_over_50pct_returns_1(self):
        state = _make_state(daily_budget_remaining=11.0)
        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0"}):
            assert _cost_penalty(state) == 1.0

    def test_between_20_and_50_pct_returns_0_6(self):
        # 4.5/20 = 22.5%, which is in the 20-50% band
        state = _make_state(daily_budget_remaining=4.5)
        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0"}):
            assert _cost_penalty(state) == 0.6

    def test_between_5_and_20_pct_returns_0_3(self):
        state = _make_state(daily_budget_remaining=1.5)  # 7.5%
        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0"}):
            assert _cost_penalty(state) == 0.3

    def test_below_5_pct_returns_0_1(self):
        state = _make_state(daily_budget_remaining=0.5)  # 2.5%
        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0"}):
            assert _cost_penalty(state) == 0.1

    def test_zero_budget_returns_0_1(self):
        state = _make_state(daily_budget_remaining=0.0)
        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0"}):
            assert _cost_penalty(state) == 0.1

    def test_zero_daily_budget_env_returns_0_1(self):
        """Guard against division by zero when COORDINATOR_DAILY_BUDGET=0."""
        state = _make_state(daily_budget_remaining=0.0)
        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "0"}):
            assert _cost_penalty(state) == 0.1


# ---------------------------------------------------------------------------
# TestPerformanceMultiplierCached
# ---------------------------------------------------------------------------

class TestPerformanceMultiplierCached:
    """_performance_multiplier_cached() adjustments and floor."""

    def test_no_cache_returns_1(self):
        assert _performance_multiplier_cached("infra_engineer", "build_failure", None) == 1.0

    def test_missing_agent_in_cache_returns_1(self):
        cache = {}
        assert _performance_multiplier_cached("infra_engineer", "build_failure", cache) == 1.0

    def test_zero_total_returns_1(self):
        cache = {("infra_engineer", "build_failure"): {"total": 0, "successful": 0}}
        assert _performance_multiplier_cached("infra_engineer", "build_failure", cache) == 1.0

    def test_high_success_rate_returns_1(self):
        cache = {("infra_engineer", "build_failure"): {"total": 10, "successful": 9}}
        assert _performance_multiplier_cached("infra_engineer", "build_failure", cache) == 1.0

    def test_exactly_80pct_success_returns_1(self):
        cache = {("infra_engineer", "build_failure"): {"total": 10, "successful": 8}}
        # rate = 0.80 which is NOT > 0.80, so falls to 0.85 tier
        assert _performance_multiplier_cached("infra_engineer", "build_failure", cache) == 0.85

    def test_50_to_80_pct_returns_0_85(self):
        cache = {("infra_engineer", "build_failure"): {"total": 10, "successful": 6}}
        assert _performance_multiplier_cached("infra_engineer", "build_failure", cache) == 0.85

    def test_below_50_pct_returns_0_7_floor(self):
        cache = {("infra_engineer", "build_failure"): {"total": 10, "successful": 3}}
        assert _performance_multiplier_cached("infra_engineer", "build_failure", cache) == 0.7

    def test_zero_success_returns_floor(self):
        cache = {("infra_engineer", "build_failure"): {"total": 10, "successful": 0}}
        assert _performance_multiplier_cached("infra_engineer", "build_failure", cache) == 0.7


# ---------------------------------------------------------------------------
# TestSelectModel
# ---------------------------------------------------------------------------

class TestSelectModel:
    """select_model() returns correct model or None under budget constraints."""

    def test_returns_default_model_when_available(self):
        task = _make_task(target_agent="infra_engineer")
        budget = _make_budget()
        result = select_model("infra_engineer", task, budget)
        assert result == "claude-sonnet-4-6"

    def test_returns_none_for_unknown_agent(self):
        task = _make_task(target_agent="nonexistent_agent")
        budget = _make_budget()
        assert select_model("nonexistent_agent", task, budget) is None

    def test_falls_back_to_haiku_when_sonnet_not_allowed(self):
        task = _make_task(target_agent="infra_engineer")
        budget = _make_budget(allowed_models=["claude-haiku-4-5"])
        result = select_model("infra_engineer", task, budget)
        assert result == "claude-haiku-4-5"

    def test_returns_none_when_no_models_allowed(self):
        task = _make_task(target_agent="infra_engineer")
        budget = _make_budget(allowed_models=[])
        assert select_model("infra_engineer", task, budget) is None

    def test_upgrades_to_opus_when_condition_met(self):
        task = _make_task(target_agent="infra_engineer", context={"requires": "diagnostics"})
        budget = _make_budget(max_opus_calls=3)
        result = select_model("infra_engineer", task, budget)
        assert result == "claude-opus-4-6"

    def test_no_upgrade_when_opus_calls_exhausted(self):
        task = _make_task(target_agent="infra_engineer", context={"requires": "diagnostics"})
        budget = _make_budget(max_opus_calls=0)
        result = select_model("infra_engineer", task, budget)
        assert result == "claude-sonnet-4-6"  # falls back to default

    def test_no_upgrade_when_condition_not_met(self):
        task = _make_task(target_agent="infra_engineer", context={"requires": "other"})
        budget = _make_budget()
        result = select_model("infra_engineer", task, budget)
        assert result == "claude-sonnet-4-6"  # stays at default

    def test_agent_with_no_upgrade_never_upgrades(self):
        task = _make_task(target_agent="financial", context={"requires": "anything"})
        budget = _make_budget()
        result = select_model("financial", task, budget)
        assert result == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# TestEstimateTaskCost
# ---------------------------------------------------------------------------

class TestEstimateTaskCost:
    """estimate_task_cost() returns correct values."""

    def test_opus_cost(self):
        assert estimate_task_cost("backend_engineer", "claude-opus-4-6") == pytest.approx(0.80)

    def test_sonnet_cost(self):
        assert estimate_task_cost("infra_engineer", "claude-sonnet-4-6") == pytest.approx(0.12)

    def test_haiku_cost(self):
        assert estimate_task_cost("financial", "claude-haiku-4-5") == pytest.approx(0.05)

    def test_unknown_model_returns_default(self):
        assert estimate_task_cost("infra_engineer", "unknown-model") == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# TestBuildPerformanceCache
# ---------------------------------------------------------------------------

class TestBuildPerformanceCache:
    """build_performance_cache() queries and error handling."""

    def test_returns_dict_keyed_by_agent_category(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = [
            ("infra_engineer", "build_failure", 10, 8),
            ("financial", "budget_alert", 5, 4),
        ]
        mock_conn.cursor.return_value = mock_cur

        result = build_performance_cache(mock_conn)

        assert ("infra_engineer", "build_failure") in result
        assert result[("infra_engineer", "build_failure")] == {"total": 10, "successful": 8}
        assert ("financial", "budget_alert") in result

    def test_returns_empty_dict_on_db_error(self):
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = RuntimeError("DB unavailable")
        result = build_performance_cache(mock_conn)
        assert result == {}

    def test_empty_table_returns_empty_dict(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur

        assert build_performance_cache(mock_conn) == {}
