# coordinator/state.py: WorldState snapshot builder
#
# Builds a point-in-time snapshot of the system for the coordinator tick to
# reason about. Everything comes from the platform database: build, customer,
# monitor and spend figures on every tick, agent performance once an hour
# (_PERF_CACHE_TTL).

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from coordinator import config

logger = logging.getLogger("coordinator.state")


@dataclass
class WorkItem:
    """A unit of work for the coordinator to score and dispatch."""
    id: str = ""
    category: str = ""
    target_agent: str = ""
    description: str = ""
    context: dict = field(default_factory=dict)
    context_hash: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = ""  # trigger that created this item


@dataclass
class WorldState:
    tick_id: str = ""
    tick_number: int = 0
    tick_start: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Refreshed every tick (cheap DB queries).
    #
    # "Unknown" is None, never 0 and never an empty list. build_world_state
    # sets each figure below to None before it reads anything, and a figure is
    # given a value only by the query that reads it. If that query fails, the
    # figure stays None for the whole tick. A reader that forgets to ask gets
    # a TypeError from None, which fails the tick; it cannot read an unknown
    # count as "no builds" or an unknown spend as "nothing spent".
    #
    # Code that would do MORE when a figure is low or empty (spend, start a
    # test build, treat the pipeline as idle) uses the three functions below
    # this class (customer_builds_active, pipeline_idle, budget_remaining_usd),
    # which answer the restrictive way when the figure is unknown.
    queued_builds: int | None = 0
    active_builds: int | None = 0
    failed_builds_24h: list | None = field(default_factory=list)
    preview_awaiting: int | None = 0
    abandoned_intakes_24h: int | None = 0
    pending_feedback: int | None = 0
    monitored_apps_down: list | None = field(default_factory=list)
    monitored_apps_degraded: list | None = field(default_factory=list)
    api_cost_today_usd: float | None = 0.0
    daily_budget_remaining_usd: float | None = 20.0
    # False when today's spend could not be read this tick; budget code then
    # treats the budget as exhausted (fail closed). build_world_state sets it.
    spend_known: bool = True

    # Refreshed hourly (moderate queries)
    agent_success_rates: dict = field(default_factory=dict)
    agent_avg_cost: dict = field(default_factory=dict)

    # Carried forward between ticks
    pending_work: list = field(default_factory=list)
    previous_tick_summary: dict = field(default_factory=dict)


# --- Task rows written under dry run ----------------------------------------
# A task row written while the tick ran under dry run carries this key in its
# `result` JSON (coordinator/tick.py writes it). Under dry run an agent is
# refused everything it tries to do, so what such a row says about success or
# failure says nothing about how the agent does live.
DRY_RUN_RESULT_KEY = "dry_run"
# The SQL predicate for "this agent_tasks row is a live outcome". Its one
# placeholder is bound to DRY_RUN_RESULT_KEY. The two statements that count
# outcomes spell it out in their own text: the failure circuit
# (coordinator/tick.py, _failure_circuit) and the scorer's performance cache
# (coordinator/scorer.py, build_performance_cache).
LIVE_OUTCOME_SQL = "(result->>%s) IS DISTINCT FROM 'true'"


# --- Restrictive readings: what to assume when a figure is unknown -----------

def customer_builds_active(state) -> bool:
    """True when a customer build is in progress, or when the pipeline counts
    could not be read. Customer builds have priority over agent work, and "we
    could not tell" must not read as "none are running"."""
    return state.active_builds is None or state.active_builds > 0


def pipeline_idle(state) -> bool:
    """True only when it is KNOWN that no build is queued or in progress."""
    if state.active_builds is None or state.queued_builds is None:
        return False
    return state.active_builds == 0 and state.queued_builds == 0


def budget_remaining_usd(state) -> float:
    """Dollars left today for scoring and thresholds: 0.0 when today's spend
    is unknown. (What a tick may SPEND is not this figure; see
    coordinator.budget.compute_tick_budget.)"""
    if not getattr(state, "spend_known", True) or state.daily_budget_remaining_usd is None:
        return 0.0
    return state.daily_budget_remaining_usd


# Cache for expensive queries. All access happens on the coordinator's own
# thread (build_world_state), so it is not locked. Entries expire by TTL only.
_cache: dict = {}
_MAX_CACHE_SIZE = 100
_PERF_CACHE_TTL = 3600     # 1 hour


def _is_cache_fresh(key: str, ttl: float) -> bool:
    entry = _cache.get(key)
    if entry is None:
        return False
    return (time.time() - entry["ts"]) < ttl


def _set_cache(key: str, value):
    """Set a cache entry, evicting oldest when max size exceeded."""
    if key in _cache:
        del _cache[key]
    while len(_cache) >= _MAX_CACHE_SIZE:
        oldest_key = next(iter(_cache))
        del _cache[oldest_key]
    _cache[key] = {"ts": time.time(), "value": value}


def _get_cache(key: str):
    entry = _cache.get(key)
    return entry["value"] if entry else None


def build_world_state(conn, tick_number: int, previous_summary: dict = None) -> WorldState:
    """Build a point-in-time WorldState snapshot.

    Args:
        conn: psycopg2 connection
        tick_number: monotonic tick counter
        previous_summary: summary from the previous tick

    Returns:
        Populated WorldState dataclass
    """
    daily_budget = config.daily_budget_usd()
    state = WorldState(
        tick_id=str(uuid.uuid4()),
        tick_number=tick_number,
        tick_start=datetime.now(timezone.utc),
        previous_tick_summary=previous_summary or {},
    )

    # --- Every tick: cheap DB queries ---
    # Every refreshed figure starts the tick unknown (None) and is given a
    # value only by the refresh that reads it. Each refresh is isolated: a
    # failed query aborts the psycopg2 transaction, so roll back before the
    # next one, and never let one failure skip the others. A refresh that
    # fails puts its figures back to None, so a count it had half filled in
    # is not left looking like an answer.
    refreshes = (
        (_refresh_build_pipeline, _BUILD_FIGURES, ()),
        (_refresh_customer_signals, _CUSTOMER_FIGURES, ()),
        (_refresh_infra_health, _INFRA_FIGURES, ()),
        (_refresh_financial, _SPEND_FIGURES, (daily_budget,)),
    )
    for _refresh, figures, _extra in refreshes:
        _forget(state, figures)
    state.spend_known = False
    for refresh, figures, extra in refreshes:
        try:
            refresh(conn, state, *extra)
            if figures is _SPEND_FIGURES:
                state.spend_known = True
        except Exception as exc:
            _forget(state, figures)
            logger.error("Error refreshing per-tick state (%s): %s; %s unknown for this tick",
                         getattr(refresh, "__name__", refresh), exc, ", ".join(figures))
            _safe_rollback(conn)

    # --- Hourly: agent performance ---
    if not _is_cache_fresh("agent_performance", _PERF_CACHE_TTL):
        try:
            _refresh_agent_performance(conn, state)
            _set_cache("agent_performance", True)
        except Exception as exc:
            logger.warning("Error refreshing agent performance: %s", exc)
    else:
        # Use cached values
        cached = _get_cache("agent_perf_data")
        if cached:
            state.agent_success_rates = cached.get("success_rates", {})
            state.agent_avg_cost = cached.get("avg_cost", {})

    # Load pending work from agent_tasks
    try:
        _load_pending_work(conn, state)
    except Exception as exc:
        logger.warning("Error loading pending work: %s", exc)

    return state


def _forget(state: WorldState, figures) -> None:
    """Mark the named figures unknown."""
    for name in figures:
        setattr(state, name, None)


def _safe_rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception as exc:
        # The connection is beyond use; the caller's next statement will say so.
        logger.warning("rollback after a failed refresh did not succeed: %s", exc)


def _refresh_build_pipeline(conn, state: WorldState):
    """Refresh build pipeline state from DB."""
    cur = conn.cursor()

    # Queued and active builds. The counts are assigned only after the query
    # has answered: a status with no rows is a count of zero that was read.
    cur.execute(
        "SELECT status, COUNT(*) FROM builds "
        "WHERE status IN ('queued', 'building', 'testing', 'deploying', 'preview') "
        "GROUP BY status"
    )
    queued = active = preview = 0
    for row in cur.fetchall():
        status, count = row
        if status == 'queued':
            queued = count
        elif status in ('building', 'testing', 'deploying'):
            active += count
        elif status == 'preview':
            preview += count
    state.queued_builds, state.active_builds, state.preview_awaiting = queued, active, preview

    # Failed builds in last 24h. error_category is read from the DLQ row when
    # present, else from builds.error_category (the Haiku categorizer persists
    # it there for failures without a DLQ entry, per schema.sql:749). Reading
    # only the DLQ column left builds.error_category unread, so the categorizer
    # re-fired a Haiku call for the same build on every tick.
    cur.execute(
        "SELECT b.id, b.app_name, b.error_message, b.tier, b.retry_count, "
        "       COALESCE(dlq.error_category, b.error_category) "
        "FROM builds b "
        "LEFT JOIN dead_letter_builds dlq ON dlq.build_id = b.id "
        "WHERE b.status = 'failed' AND b.updated_at > NOW() - INTERVAL '24 hours'"
    )
    state.failed_builds_24h = [
        {"id": str(r[0]), "app_name": r[1], "error_message": r[2],
         "tier": r[3], "retry_count": r[4], "error_category": r[5]}
        for r in cur.fetchall()
    ]

    cur.close()


def _refresh_customer_signals(conn, state: WorldState):
    """Refresh customer signal metrics."""
    cur = conn.cursor()

    # Abandoned intakes in 24h
    cur.execute(
        "SELECT COUNT(*) FROM builds "
        "WHERE status = 'abandoned' AND abandoned_at > NOW() - INTERVAL '24 hours'"
    )
    state.abandoned_intakes_24h = cur.fetchone()[0]

    # Pending feedback (since reasonable window: last 24h)
    cur.execute(
        "SELECT COUNT(*) FROM beta_feedback "
        "WHERE created_at > NOW() - INTERVAL '24 hours'"
    )
    state.pending_feedback = cur.fetchone()[0]

    cur.close()


def _refresh_infra_health(conn, state: WorldState):
    """Refresh infrastructure health from monitor_apps."""
    cur = conn.cursor()

    cur.execute(
        "SELECT app_name, live_url, consecutive_failures, status "
        "FROM monitor_apps WHERE status IN ('down', 'degraded')"
    )
    down, degraded = [], []
    for row in cur.fetchall():
        entry = {
            "app_name": row[0], "live_url": row[1],
            "consecutive_failures": row[2],
        }
        (down if row[3] == 'down' else degraded).append(entry)
    state.monitored_apps_down, state.monitored_apps_degraded = down, degraded

    cur.close()


def _refresh_financial(conn, state: WorldState, daily_budget: float):
    """Refresh the ledger figures on the world state.

    Reads the worker's ledger with the same query the budget uses
    (spend_tracker.get_daily_spend), so the two cannot disagree about what
    "spent today" means. These figures feed scoring, the budget alert and the
    tick record. They are not what a tick may spend: that is
    coordinator.budget.compute_tick_budget, which reads the configured spend
    source itself (the Anthropic cost report when ANTHROPIC_ADMIN_KEY is set).
    """
    from coordinator import spend_tracker

    tracked = spend_tracker.get_daily_spend(conn)
    state.api_cost_today_usd = tracked["total_tracked_usd"]
    state.daily_budget_remaining_usd = max(0, daily_budget - state.api_cost_today_usd)


# The WorldState figures each per-tick refresh is the only writer of.
# build_world_state uses the names to mark a figure unknown.
_BUILD_FIGURES = ("queued_builds", "active_builds", "preview_awaiting", "failed_builds_24h")   # _refresh_build_pipeline
_CUSTOMER_FIGURES = ("abandoned_intakes_24h", "pending_feedback")                              # _refresh_customer_signals
_INFRA_FIGURES = ("monitored_apps_down", "monitored_apps_degraded")                            # _refresh_infra_health
_SPEND_FIGURES = ("api_cost_today_usd", "daily_budget_remaining_usd")                          # _refresh_financial


def _refresh_agent_performance(conn, state: WorldState):
    """Refresh agent performance metrics (hourly)."""
    cur = conn.cursor()

    # 7-day rolling success rate per agent
    cur.execute(
        "SELECT agent, "
        "  COUNT(*) FILTER (WHERE status = 'completed' AND (result->>'success')::boolean = true) * 1.0 / "
        "  NULLIF(COUNT(*) FILTER (WHERE status IN ('completed', 'failed')), 0) AS success_rate, "
        "  AVG(actual_cost_usd) AS avg_cost "
        "FROM agent_tasks "
        "WHERE created_at > NOW() - INTERVAL '7 days' "
        "  AND status IN ('completed', 'failed') "
        "GROUP BY agent"
    )
    success_rates = {}
    avg_costs = {}
    for row in cur.fetchall():
        agent, rate, cost = row
        if rate is not None:
            success_rates[agent] = float(rate)
        if cost is not None:
            avg_costs[agent] = float(cost)

    state.agent_success_rates = success_rates
    state.agent_avg_cost = avg_costs

    _set_cache("agent_perf_data", {
        "success_rates": success_rates,
        "avg_cost": avg_costs,
    })

    cur.close()


def _load_pending_work(conn, state: WorldState):
    """Load pending work items from agent_tasks table."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, task_category, agent, task_description, context, context_hash, created_at "
        "FROM agent_tasks WHERE status = 'pending' ORDER BY priority_score DESC NULLS LAST"
    )
    items = []
    for row in cur.fetchall():
        items.append(WorkItem(
            id=str(row[0]),
            category=row[1],
            target_agent=row[2],
            description=row[3] or "",
            context=row[4] or {},
            context_hash=row[5] or "",
            created_at=row[6],
        ))
    state.pending_work = items
    cur.close()
