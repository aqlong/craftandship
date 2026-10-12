# coordinator/scorer.py: Priority scoring for work items
#
# Pure functions, no API calls. Deterministic scoring based on category
# weights, urgency multipliers, and cost penalties.

from __future__ import annotations

import logging
from datetime import datetime, timezone

from coordinator import config
from coordinator.state import DRY_RUN_RESULT_KEY, WorkItem, WorldState, budget_remaining_usd

logger = logging.getLogger("coordinator.scorer")

# One entry per category a work-detection trigger emits
# (tests/test_coordinator_routing.py compares the two lists).
DEFAULT_CATEGORY_WEIGHT = 0.20

CATEGORY_WEIGHTS = {
    # P0: Keep the lights on
    "app_down":           0.95,
    "backup_overdue":     0.85,

    # P1: Customer impact
    "build_failure":      0.80,
    "customer_feedback":  0.65,

    # P2: Quality
    "post_deploy_audit":  0.55,
    "security_scan":      0.50,
    "cost_report":        0.40,

    # P2.5: Proactive pipeline health
    "recurring_qa_warning":  0.40,

    # P2.5: Architecture review
    "spec_review":           0.45,
    "post_build_review":     0.40,
    "architecture_qa_review": 0.42,

    # P3: Improvement (only when idle)
    "cost_projection":    0.28,

    # P3: Beta readiness (continuous pipeline testing)
    "test_build":         0.35,

    # P3: Budget alert
    "budget_alert":       0.35,

    # P3: the coordinator's own housekeeping
    "coordinator_stalled":   DEFAULT_CATEGORY_WEIGHT,
    "reconciliation_sweep":  DEFAULT_CATEGORY_WEIGHT,

    # P3.5: Proactive initiative (lowest priority, fills idle time)
    "agent_initiative":   0.15,
}

# Agent type -> (default_model, upgrade_model, upgrade_condition).
# Most agents are rule-based and ignore the model they are handed (only
# customer_service drafts text with a model); the tier here still sets the cost
# reserved for the task in tick.py and the cap on upgrades. An agent without an
# entry is never dispatched.
AGENT_MODELS = {
    "infra_engineer":   ("claude-sonnet-4-6", "claude-opus-4-6",   "diagnostics"),
    "security":         ("claude-sonnet-4-6", "claude-opus-4-6",   "vuln_analysis"),
    "backend_engineer": ("claude-sonnet-4-6", None,                 None),
    "qa_test":          ("claude-haiku-4-5",  "claude-sonnet-4-6", "analysis"),
    "financial":        ("claude-haiku-4-5",  None,                 None),
    "customer_service": ("claude-haiku-4-5",  None,                 None),
    "documentation":    ("claude-haiku-4-5",  None,                 None),
    "cost_analyst":     ("claude-haiku-4-5",  None,                 None),
    "architect":        ("claude-sonnet-4-6", "claude-opus-4-6",   "deep_review"),
    # Makes no model calls; the entry exists because select_model() returns None
    # (task skipped) for an agent without one. Cheapest tier on purpose.
    "reconciliation":   ("claude-haiku-4-5",  None,                 None),
}


def score_task(task: WorkItem, state: WorldState, perf_cache=None) -> float:
    """Score a task from 0.0-1.0. Deterministic, no API calls.

    Args:
        perf_cache: optional dict from build_performance_cache() for performance multiplier.
                    None = backward compatible (no performance adjustment).
    """
    base = CATEGORY_WEIGHTS.get(task.category, DEFAULT_CATEGORY_WEIGHT)
    urgency = _urgency_multiplier(task, state)
    cost_pen = _cost_penalty(state)
    perf = _performance_multiplier_cached(task.target_agent, task.category, perf_cache)
    return min(1.0, base * urgency * cost_pen * perf)


def _urgency_multiplier(task: WorkItem, state: WorldState) -> float:
    """1.0-2.0. Based on age and severity. Tiered escalation prevents starvation."""
    now = datetime.now(timezone.utc)
    created = task.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_hours = (now - created).total_seconds() / 3600

    # Tiered escalation replacing single linear formula
    if age_hours <= 4:
        age_boost = min(1.5, 1.0 + (age_hours / 24) * 0.5)
    elif age_hours <= 8:
        age_boost = min(1.8, 1.0 + (age_hours / 12) * 0.5)
    else:
        age_boost = min(2.0, 1.0 + (age_hours / 8) * 0.5)

    # Down apps escalate with consecutive failures
    if task.category == "app_down":
        failures = task.context.get("consecutive_failures", 0)
        if failures > 10:
            return 2.0
        if failures > 5:
            return 1.5

    return age_boost


def _cost_penalty(state: WorldState) -> float:
    """0.1-1.0. Throttle when budget is low, and fully when today's spend is
    unknown (coordinator.state.budget_remaining_usd reads unknown as nothing left)."""
    remaining = budget_remaining_usd(state)
    daily = config.daily_budget_usd()
    pct = remaining / daily if daily > 0 else 0

    if pct > 0.5:
        return 1.0
    if pct > 0.2:
        return 0.6
    if pct > 0.05:
        return 0.3
    return 0.1  # emergency only


def select_model(agent_type: str, task: WorkItem, budget) -> str | None:
    """Select the best model for an agent given budget constraints.

    Returns model string or None if agent cannot run this tick.
    """
    entry = AGENT_MODELS.get(agent_type)
    if not entry:
        return None

    default, upgrade, condition = entry

    if default not in budget.allowed_models:
        # Downgrade to cheapest allowed
        for fallback in ["claude-haiku-4-5", "claude-sonnet-4-6"]:
            if fallback in budget.allowed_models:
                return fallback
        return None

    # Check upgrade
    if upgrade and condition and task.context.get("requires") == condition:
        if upgrade in budget.allowed_models and budget.max_opus_calls > 0:
            return upgrade

    return default


# Estimated cost per API call by model (for budget planning)
ESTIMATED_COST = {
    "claude-opus-4-6": 0.80,
    "claude-sonnet-4-6": 0.12,
    "claude-haiku-4-5": 0.05,
}


def estimate_task_cost(agent_type: str, model: str) -> float:
    """Estimate the cost of running an agent task."""
    return ESTIMATED_COST.get(model, 0.10)


def _performance_multiplier_cached(agent_type: str, category: str, perf_cache) -> float:
    """Adjust score based on agent performance history.

    Returns 1.0 (no change) for good performers, down to 0.7 floor for poor performers.
    The 0.7 floor prevents a death spiral where an agent never gets dispatched.
    """
    if perf_cache is None:
        return 1.0
    data = perf_cache.get((agent_type, category))
    if not data or data["total"] == 0:
        return 1.0
    rate = data["successful"] / data["total"]
    if rate > 0.80:
        return 1.0
    if rate >= 0.50:
        return 0.85
    return 0.7  # Floor prevents death spiral


def build_performance_cache(conn) -> dict:
    """Outcome counts per (agent, category), read once per tick from agent_tasks.

    The counts come from the task rows the tick itself writes: _dispatch_agent
    marks a task 'completed' when the agent reported success and 'failed'
    otherwise, and a task stuck in 'running' is failed by _expire_stale_work.
    There is no separate counter table to keep in step with them. Rows written
    under dry run are left out (coordinator.state.LIVE_OUTCOME_SQL): an agent
    that was refused everything in dry run has not shown how it does live, and
    its score must not start lower for it.

    On any failure we MUST rollback or psycopg2 leaves the connection in a
    failed-transaction state which cascades into the rest of the tick; the
    cache is then empty and no performance adjustment is applied.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agent, task_category, COUNT(*), "
                "       COUNT(*) FILTER (WHERE status = 'completed') "
                "FROM agent_tasks "
                "WHERE status IN ('completed', 'failed') "
                "  AND (result->>%s) IS DISTINCT FROM 'true' "
                "GROUP BY agent, task_category",
                (DRY_RUN_RESULT_KEY,),
            )
            return {(r[0], r[1]): {"total": r[2], "successful": r[3]} for r in cur.fetchall()}
    except Exception as exc:
        logger.warning("agent performance could not be read (%s); scoring without it this tick", exc)
        try:
            conn.rollback()
        except Exception as rollback_exc:
            logger.warning("rollback after the failed performance read did not succeed: %s", rollback_exc)
        return {}
