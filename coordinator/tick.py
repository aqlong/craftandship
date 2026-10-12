# coordinator/tick.py: Tick lifecycle implementation
#
# Implements the full coordinator tick: guard check, snapshot, budget, detect
# work, budget gate, dispatch loop (with parallel dispatch) and the tick
# record. The budget is computed before work detection because detection's
# failure triage can call a model; triage and the dispatched agents draw their
# model calls from one per-tick meter (coordinator.budget.ModelCallMeter).
#
# What a task leaves behind is its agent_tasks row: status, result, the
# actions its actuators recorded and the notes its agent returned
# (AgentResult.learnings, stored in the row's `learnings` column). Nothing in
# the coordinator reads those notes back into a later task, and nothing in the
# coordinator writes or reads the agent_learnings table (the web tier does:
# its admin routes and weekly sweeps write rows, and its persona digest reads
# them).

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait as wait_for_futures
from datetime import datetime, timezone
from decimal import Decimal

import psycopg2

from coordinator.state import DRY_RUN_RESULT_KEY, build_world_state, customer_builds_active, WorkItem
from coordinator.scorer import score_task, select_model, estimate_task_cost
from coordinator.budget import compute_tick_budget, ModelCallMeter, RPM_SPACING, TickBudget
from coordinator.work_detection import detect_work, compute_context_hash
from coordinator.actuators import Actuators
from coordinator.envflags import arming_switch
from coordinator import config

logger = logging.getLogger("coordinator.tick")


# --- Agent timeout configuration ---

AGENT_TIMEOUTS = {
    "backend_engineer": 7200,   # 2 hours
    "architect":        7200,   # 2 hours
    "infra_engineer":   1800,   # 30 min
    "security":         1800,   # 30 min
    "qa_test":          1800,   # 30 min
    "financial":        900,    # 15 min
    "customer_service": 900,    # 15 min
    "documentation":    900,    # 15 min
    "cost_analyst":     900,    # 15 min
}

CATEGORY_TIMEOUTS = {
    "build_failure":         1800,
    "budget_alert":          300,
    "cost_report":           600,
}


def _get_agent_timeout(agent_type: str, category: str) -> int:
    """Get the timeout for an agent+category combo.

    Category timeout takes precedence over agent default.
    Falls back to 1800s (30 min) for unknown agents.
    """
    if category in CATEGORY_TIMEOUTS:
        return CATEGORY_TIMEOUTS[category]
    return AGENT_TIMEOUTS.get(agent_type, 1800)


class _DecimalEncoder(json.JSONEncoder):
    """JSON encoder that converts Decimal values to float.

    PostgreSQL returns Decimal from NUMERIC columns. Standard json.dumps
    raises TypeError on Decimal, so all serialization in the tick
    lifecycle uses this encoder.
    """

    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def _json_dumps(obj, **kwargs):
    """json.dumps wrapper that handles Decimal values from PostgreSQL."""
    kwargs.setdefault("cls", _DecimalEncoder)
    return json.dumps(obj, **kwargs)


# Registry of available agents (populated by agent modules)
_agent_registry: dict = {}

# Reviewer assignment table
REVIEWER_MAP = {
    "backend_engineer": "qa_test",
    "infra_engineer": "security",
    "security": "architect",
}

# Actions whose task gets review_status = 'pending' and a reviewer agent from
# REVIEWER_MAP (see _check_and_queue_review). This is bookkeeping only: the
# review is recorded after the action ran and nothing in this repository
# consumes it. The real gates are the permission table and dry run in
# coordinator/actuators.py and the service and variable allowlists in
# ops/fixer.py.
ACTIONS_REQUIRING_REVIEW = {"railway_deploy"}


def register_agent(name: str, agent_cls):
    """Register an agent class with the coordinator."""
    _agent_registry[name] = agent_cls


def get_agent_registry() -> dict:
    return dict(_agent_registry)


# Max number of agents dispatched concurrently within a single tick
# (COORDINATOR_MAX_PARALLEL; a value that is not a whole number of at least 1
# means 3).
COORDINATOR_MAX_PARALLEL = config.env_int("COORDINATOR_MAX_PARALLEL", 3, min_val=1)

# DRY_RUN_RESULT_KEY (coordinator/state.py): a task row written under dry run
# is marked in its `result` JSON by _task_result_json below, and the failure
# circuit (_failure_circuit) and the scorer's performance cache leave marked
# rows out.


# The key under which a task's side effects are stored in its `result` JSON
# (AgentResult.effects: for each alert, finding, KB entry, rebuild or approval
# request the agent asked for, whether it was sent or recorded, a dry run,
# refused by the permission table, or failed).
EFFECTS_RESULT_KEY = "effects"


def _task_result_json(payload, dry_run: bool, effects=None) -> str:
    """The JSON stored in agent_tasks.result: the agent's output (or an error
    record), marked when the tick ran under dry run, with the outcome of each
    side effect the agent asked for under EFFECTS_RESULT_KEY."""
    record = dict(payload) if isinstance(payload, dict) else {"output": payload}
    if effects:
        record[EFFECTS_RESULT_KEY] = list(effects)
    if dry_run:
        record[DRY_RUN_RESULT_KEY] = True
    else:
        record.pop(DRY_RUN_RESULT_KEY, None)   # only the tick says which mode a row ran in
    return _json_dumps(record)


def _task_charge(reported_cost, reported_calls, est_cost: float, default_calls: int = 1) -> tuple:
    """What a dispatched task costs the per-tick allowance: (dollars, calls).

    No agent in this repository reports token usage: AgentResult.cost_usd and
    AgentResult.api_calls stay at their zero defaults. A task that ran therefore
    keeps the reservation made for it, its estimated cost and one call. An
    agent that does report is charged what it reports, and never less than one
    call. This allowance is thus a cap on tasks per tick at their estimated
    cost, not a measurement of spend. The model calls a task actually makes
    are counted separately, one by one, by the tick's ModelCallMeter.

    A task that cannot make a model call (_task_may_call_model) is charged no
    call unless its agent reports one: the caller passes default_calls=0.
    """
    cost = reported_cost if reported_cost and reported_cost > 0 else est_cost
    calls = reported_calls if reported_calls and reported_calls > 0 else default_calls
    return cost, calls


def _task_may_call_model(agent_type: str, category: str) -> bool:
    """Whether a task of this agent and category may ask for a model call.

    Read from the agent class's `model_categories` (coordinator/agents/base.py):
    a frozenset of the categories whose handler can reach a model call. Anything
    else (None, a class that does not say, a stand-in object) is read as "any
    category may", and so is an unregistered agent. tests/test_coordinator_model_free_tasks.py checks
    each agent's declaration against its source: the categories declared
    model-free reach no model call, and the ones declared otherwise do.
    """
    agent_cls = _agent_registry.get(agent_type)
    declared = getattr(agent_cls, "model_categories", None)
    if not isinstance(declared, frozenset):
        return True        # nothing declared (None), or something that is not a declaration
    return category in declared


# What a task that makes no model call may use of the tick's budget. The
# budget in compute_tick_budget is an allowance of model calls and their
# estimated dollars, and a task that cannot make a call has nothing to draw on
# it, so it is neither held back by an empty allowance (a tick that could not
# read today's spend, or one whose day is spent: both leave nothing) nor does
# it reduce what the tasks that do call a model may use. Every model call, from
# any task, is still granted one at a time by the tick's ModelCallMeter, which
# an empty budget leaves with nothing to grant. The number of such tasks in a
# tick is the number of pending items.
_UNMETERED = 2 ** 31


def _create_thread_conn():
    """Create a new psycopg2 connection for use in a worker thread.

    Each parallel agent gets its own DB connection to avoid thread-safety
    issues with the shared tick connection.
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set; cannot create thread connection")
    return psycopg2.connect(db_url)


def _group_independent_tasks(scored_items: list) -> list:
    """Group scored tasks into batches of independent work.

    Tasks are independent when they target different agent types AND have
    different categories (no shared state).  Tasks sharing an agent or
    category are placed in separate batches so they run sequentially.

    Returns a list of batches, where each batch is a list of (score, task)
    tuples that can safely run in parallel.
    """
    batches: list[list] = []
    for item in scored_items:
        score, task = item
        placed = False
        for batch in batches:
            # Check if this task conflicts with anything already in the batch
            conflict = False
            for _, existing_task in batch:
                # Same agent -> must be sequential (agent instance not thread-safe)
                if existing_task.target_agent == task.target_agent:
                    conflict = True
                    break
                # Same category -> might share state
                if existing_task.category == task.category:
                    conflict = True
                    break
            if not conflict:
                batch.append(item)
                placed = True
                break
        if not placed:
            batches.append([item])
    return batches


class TaskClosedElsewhere(RuntimeError):
    """The task's row was no longer 'running' when its agent finished: the
    tick closed it at the deadline (or the stuck-task sweep did). The agent's
    result is discarded."""


def _dispatch_one_parallel(task, agent_type, model, state, dry_run, tick_id,
                           stagger_delay, shutdown_event, model_calls=None, abandoned=None):
    """Run a single agent dispatch in a worker thread with its own DB connection.

    model_calls is the tick's ModelCallMeter, shared with the other workers.
    abandoned is the event the tick sets if it closes the task at the round's
    deadline while this thread is still running.

    Returns a dict with dispatch results or error info.
    """
    # Stagger API calls to respect RPM limits
    if stagger_delay > 0:
        time.sleep(stagger_delay)

    if shutdown_event and shutdown_event.is_set():
        return {"task_id": task.id, "skipped": True, "reason": "shutdown"}

    thread_conn = None
    try:
        thread_conn = _create_thread_conn()
        result = _dispatch_agent(
            thread_conn, task, agent_type, model, state, dry_run, tick_id,
            model_calls=model_calls, abandoned=abandoned,
        )

        actual_cost = result.cost_usd if result else 0.0

        outcome = {
            "task_id": task.id,
            "decision": "dispatch",
            "agent": agent_type,
            "model": model,
            "actual_cost_usd": actual_cost,
            "api_calls": result.api_calls if result else 1,
            "success": result.success if result else False,
            "result": result,
        }

        # Queue review if needed
        if result and result.actions_taken:
            _check_and_queue_review(thread_conn, task, result, agent_type, tick_id)

        # Escalation check: agent requested approval or cost exceeded threshold
        if result:
            _check_escalation(task, result, agent_type, dry_run)

        # Record follow-up work
        if result and result.follow_up_work:
            for follow_up in result.follow_up_work:
                _persist_work_item(thread_conn, follow_up, tick_id, parent_task_id=task.id)

        if result and result.success:
            # Surface initiative findings into coordinator_findings for admin review.
            # Safe side-effect: uses finding_add's own DB connection, not thread_conn.
            _surface_initiative_findings(task, result, dry_run)

        thread_conn.commit()
        return outcome

    except TaskClosedElsewhere as closed:
        # Nobody is waiting for this outcome. Nothing of the late result is
        # kept: no review, no escalation, no follow-up work.
        logger.warning("Agent %s finished after its task was closed: %s", agent_type, closed)
        _rollback_quietly(thread_conn, "after a task that was closed elsewhere")
        return {"task_id": task.id, "decision": "abandoned", "agent": agent_type}

    except Exception as exc:
        # The task row already says what happened (_fail_task_after_exception).
        logger.error("Agent %s raised on '%s': %s", agent_type, task.category, exc)
        if thread_conn:
            _rollback_quietly(thread_conn, "after an agent raised")
        return {
            "task_id": task.id,
            "decision": "error",
            "agent": agent_type,
            "error": str(exc)[:500],
        }
    finally:
        if thread_conn:
            try:
                thread_conn.close()
            except Exception as close_exc:
                logger.debug("closing a dispatch thread's connection failed: %s", close_exc)


def _rollback_quietly(conn, when: str) -> None:
    """conn.rollback() where a failure of the rollback itself must not replace
    the outcome being reported; the failure is logged, not hidden."""
    try:
        conn.rollback()
    except Exception as exc:
        logger.warning("rollback %s did not succeed: %s", when, exc)


# How far back the first tick of a process looks when it takes its watermark
# from the database (see _previous_watermark).
WATERMARK_MAX_LOOKBACK_HOURS = 24


def _database_now(conn) -> datetime:
    """The database's clock, read in its own short transaction.

    The triggers compare this value with timestamps the database itself wrote
    (created_at, updated_at), so it has to come from the same clock. A reply
    that is not a timestamp (a stub connection in a unit test) falls back to
    this process's clock.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT NOW()")
        row = cur.fetchone()
    conn.rollback()  # a read: nothing to keep, and no transaction left open
    value = row[0] if isinstance(row, (tuple, list)) and row else None
    if not isinstance(value, datetime):
        return datetime.now(timezone.utc)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _previous_watermark(conn):
    """The watermark to resume from when this process has none of its own.

    That is the start of the last tick the database records as completed
    (coordinator_ticks.started_at is each tick's watermark), so what happened
    between that tick and a worker restart is still seen. It is never older
    than WATERMARK_MAX_LOOKBACK_HOURS, so a coordinator switched back on after
    a long pause does not treat weeks of rows as new. None when no tick ever
    completed.
    """
    with conn.cursor() as cur:
        # GREATEST skips NULLs, so the "no tick ever completed" case has to be
        # filtered out before it, or it would come back as "24 hours ago".
        cur.execute(
            "SELECT GREATEST(last.started_at, NOW() - make_interval(hours => %s)) "
            "FROM (SELECT MAX(started_at) AS started_at FROM coordinator_ticks "
            "       WHERE completed_at IS NOT NULL) last "
            "WHERE last.started_at IS NOT NULL",
            (WATERMARK_MAX_LOOKBACK_HOURS,),
        )
        row = cur.fetchone()
    conn.rollback()  # a read: nothing to keep, and no transaction left open
    value = row[0] if isinstance(row, (tuple, list)) and row else None
    return value if isinstance(value, datetime) else None


def run_tick(conn, tick_number: int, previous_summary: dict = None,
             last_tick_time: datetime = None, shutdown_event=None,
             dry_run: bool = True) -> dict:
    """Execute one full coordinator tick.

    last_tick_time is the previous tick's watermark, as that tick returned it
    in its summary ("watermark"). Every tick takes its own watermark from the
    database clock before it reads anything, and the triggers that look for
    new rows examine the half-open interval (previous watermark, this
    watermark]. Consecutive ticks therefore tile time: a row written while a
    tick runs belongs to the next tick, and none is skipped or seen twice.
    (What can still be missed is a row whose transaction began before a tick's
    watermark and committed after that tick's queries ran.) With no
    last_tick_time (the first tick of a process) the watermark of the last
    completed tick in the database is used, see _previous_watermark.

    Returns:
        Tick summary dict for logging and next-tick carry-forward. It carries
        "watermark" once the work this tick detected has been committed; the
        caller hands that value back as last_tick_time.
    """
    tick_start = datetime.now(timezone.utc)

    # 1. GUARD CHECK
    if shutdown_event and shutdown_event.is_set():
        return {"skipped": True, "reason": "shutdown_requested"}

    if last_tick_time is None:
        last_tick_time = _previous_watermark(conn)

    if last_tick_time:
        elapsed = (tick_start - last_tick_time).total_seconds()
        min_interval = config.tick_interval_s() * 0.5
        if elapsed < min(min_interval, 300):  # overlap protection: half the configured interval, capped at 5 min
            return {"skipped": True, "reason": "overlap_protection", "elapsed_s": elapsed}

    decisions = []
    errors = []
    total_cost = 0.0
    tasks_dispatched = 0
    tasks_skipped = 0
    tasks_completed = 0
    tasks_scored = 0
    # This tick's watermark, and whether what it detected is committed (only
    # then may the next tick start from it).
    watermark = None
    detection_committed = False

    try:
        watermark = _database_now(conn)

        # 2. SNAPSHOT
        state = build_world_state(conn, tick_number, previous_summary)
        tick_id = state.tick_id

        # Write placeholder tick record so agent_tasks FK is satisfied. Its
        # started_at is the watermark, which is what a restarted process
        # resumes from (_previous_watermark).
        _write_tick_placeholder(conn, tick_id, tick_number, watermark)

        # Expire stale work items
        _expire_stale_work(conn, tick_id)

        # Remind about, then deny, approval requests nobody resolved.
        # A timeout is a "no", never a "yes".
        try:
            _expire_stale_approval_requests(conn)
        except Exception as exc:
            logger.warning("expire_stale_approval_requests failed: %s", exc)
            _rollback_quietly(conn, "after expiring approval requests failed")

        # 3. BUDGET. Computed before anything in this tick may call a model:
        # work detection's failure triage does, so it draws from the same
        # allowance as the agents dispatched further down. The meter is the
        # tick's one counter of model calls; every Actuators instance that may
        # authorize one is handed it.
        budget = compute_tick_budget(state, conn=conn)
        model_calls = ModelCallMeter(budget)

        # 4. DETECT WORK
        new_items = detect_work(conn, state, last_tick_time, dry_run=dry_run,
                                model_calls=model_calls, tick_started_at=watermark)
        detection_calls, detection_usd = model_calls.granted()
        for item in new_items:
            _persist_work_item(conn, item, tick_id)

        # Reload pending work after adding new items
        conn.commit()
        detection_committed = True
        from coordinator.state import _load_pending_work
        _load_pending_work(conn, state)

        # Build performance cache once per tick
        from coordinator.scorer import build_performance_cache
        perf_cache = build_performance_cache(conn)

        # Score all pending items
        scored_items = []
        for item in state.pending_work:
            item_score = score_task(item, state, perf_cache=perf_cache)
            scored_items.append((item_score, item))
            tasks_scored += 1

        scored_items.sort(key=lambda x: x[0], reverse=True)

        # Tasks that cannot make a model call (see _task_may_call_model) draw on
        # a private, unmetered allowance, not on `remaining_budget`.
        free_task_ids = {task.id for _s, task in scored_items
                         if not _task_may_call_model(task.target_agent, task.category)}
        unmetered = TickBudget(max_api_calls=_UNMETERED, max_cost_usd=float(_UNMETERED),
                               max_opus_calls=_UNMETERED, allowed_models=[])

        def _budget_for(task):
            return unmetered if task.id in free_task_ids else remaining_budget

        # 5. BUDGET GATE. What the dispatch loop may spend on tasks is the
        # tick's budget less what failure triage was granted above (rounded to
        # the millionth, so a float residue cannot refuse a task that fits).
        remaining_budget = TickBudget(
            max_api_calls=budget.max_api_calls - detection_calls,
            max_cost_usd=round(budget.max_cost_usd - detection_usd, 6),
            max_opus_calls=budget.max_opus_calls,
            allowed_models=list(budget.allowed_models),
        )
        if detection_calls:
            decisions.append({
                "decision": "failure_triage", "model_calls": detection_calls,
                "estimated_cost_usd": detection_usd,
                "budget_after": {"remaining_usd": remaining_budget.max_cost_usd,
                                 "remaining_calls": remaining_budget.max_api_calls},
            })

        # With no budget left, only P0-scored work (>= 0.85) enters the loop below,
        # and each task there is still refused by the per-task gates unless the
        # remaining allowance covers its estimate and one call. (score_task's cost
        # penalty also scales scores down as the budget drains, so this is
        # normally the exit.)
        if (remaining_budget.max_cost_usd < 0.01 and not free_task_ids
                and not any(s >= 0.85 for s, _ in scored_items)):
            logger.info("[tick-%d] Budget exhausted ($%.2f), no P0 tasks; skipping dispatch",
                        tick_number, remaining_budget.max_cost_usd)
            decisions.append({
                "decision": "skip_all", "reason": "budget_exhausted",
                "budget_remaining_usd": remaining_budget.max_cost_usd,
            })
        else:
            # 6. DISPATCH LOOP (with parallel dispatch for independent tasks)

            # Pre-filter: skip tasks that cannot run (unregistered, disabled,
            # failure circuit open).  Collect dispatchable tasks with their
            # selected model and estimated cost.
            dispatchable = []  # [(score, task, model, est_cost)]
            probed = set()     # (agent, category) pairs given their one half-open probe this tick
            for score, task in scored_items:
                agent_type = task.target_agent

                if agent_type not in _agent_registry:
                    decisions.append({
                        "task_id": task.id, "decision": "skip",
                        "reason": f"Agent '{agent_type}' not registered",
                        "score": score,
                    })
                    tasks_skipped += 1
                    continue

                if not _is_agent_enabled(agent_type, dry_run):
                    decisions.append({
                        "task_id": task.id, "decision": "skip",
                        "reason": f"Agent '{agent_type}' disabled",
                        "score": score,
                    })
                    tasks_skipped += 1
                    continue

                # The failure circuit. P0 work (score >= 0.85) is never held
                # back by it. For the rest: closed lets the task through; open
                # holds it; half_open lets ONE task of the pair through per
                # tick as a probe; unknown (the history could not be read)
                # holds it, because "could not tell" is not "no failures".
                circuit, recent_failure_rate = _failure_circuit(conn, agent_type, task.category)
                pair = (agent_type, task.category)
                if score < 0.85 and (
                        circuit in ("open", "unknown") or (circuit == "half_open" and pair in probed)):
                    reason = (
                        f"Failure history of '{agent_type}' on '{task.category}' could not be read (non-P0)"
                        if circuit == "unknown" else
                        f"Agent '{agent_type}' has {recent_failure_rate:.0%} failure rate on "
                        f"'{task.category}' (non-P0); next probe "
                        f"{config.FAILURE_RETRY_HOURS:g}h after its last live attempt")
                    decisions.append({
                        "task_id": task.id, "decision": "skip", "reason": reason,
                        "agent": agent_type, "score": score, "failure_circuit": circuit,
                    })
                    tasks_skipped += 1
                    continue
                if circuit == "half_open" and score < 0.85:
                    probed.add(pair)
                    decisions.append({
                        "task_id": task.id, "decision": "probe",
                        "reason": f"Failure circuit of '{agent_type}' on '{task.category}' is half open: "
                                  f"one attempt after {config.FAILURE_RETRY_HOURS:g}h",
                        "agent": agent_type, "score": score, "failure_circuit": circuit,
                    })

                model = select_model(agent_type, task, remaining_budget)
                if model is None:
                    decisions.append({
                        "task_id": task.id, "decision": "skip",
                        "reason": f"No suitable model available (allowed: {remaining_budget.allowed_models})",
                        "agent": agent_type, "score": score,
                    })
                    tasks_skipped += 1
                    continue

                est_cost = estimate_task_cost(agent_type, model)
                dispatchable.append((score, task, model, est_cost))

            # Group independent tasks into parallel batches
            scored_for_grouping = [(s, t) for s, t, _m, _c in dispatchable]
            batches = _group_independent_tasks(scored_for_grouping)

            # Build a lookup from task id -> (model, est_cost, score)
            task_meta = {t.id: (m, c, s) for s, t, m, c in dispatchable}

            max_parallel = COORDINATOR_MAX_PARALLEL
            budget_exhausted = False

            for batch in batches:
                if shutdown_event and shutdown_event.is_set():
                    break
                if budget_exhausted:
                    # Only the tasks that make no model call go on.
                    batch = [(score, task) for score, task in batch if task.id in free_task_ids]
                    if not batch:
                        continue

                # Budget-gate each item in the batch before dispatching
                batch_to_dispatch = []
                for score, task in batch:
                    model, est_cost, _ = task_meta[task.id]
                    if est_cost > _budget_for(task).max_cost_usd:
                        decisions.append({
                            "task_id": task.id, "decision": "skip",
                            "reason": f"Estimated cost ${est_cost:.2f} > budget ${remaining_budget.max_cost_usd:.2f}",
                            "agent": task.target_agent, "model": model, "score": score,
                        })
                        tasks_skipped += 1
                        continue
                    # select_model() ran for every task BEFORE any reservation, so
                    # each saw the full per-tick opus and call allowance. Enforce
                    # the sub-caps here, where the allowance is actually spent.
                    if _budget_for(task).max_api_calls < 1:
                        decisions.append({
                            "task_id": task.id, "decision": "skip",
                            "reason": "Per-tick API call allowance exhausted",
                            "agent": task.target_agent, "model": model, "score": score,
                        })
                        tasks_skipped += 1
                        continue
                    if model == "claude-opus-4-6" and _budget_for(task).max_opus_calls < 1:
                        decisions.append({
                            "task_id": task.id, "decision": "skip",
                            "reason": "Per-tick opus call allowance exhausted",
                            "agent": task.target_agent, "model": model, "score": score,
                        })
                        tasks_skipped += 1
                        continue
                    # Reserve budget optimistically
                    _budget_for(task).max_cost_usd -= est_cost
                    _budget_for(task).max_api_calls -= 1
                    if model == "claude-opus-4-6":
                        _budget_for(task).max_opus_calls -= 1
                    batch_to_dispatch.append((score, task, model, est_cost))

                if not batch_to_dispatch:
                    continue

                # Every dispatch runs on a pool thread with a database
                # connection of its own and is waited for with a deadline, so a
                # hung agent cannot hang the tick. A batch of several
                # independent items is one round of up to
                # COORDINATOR_MAX_PARALLEL workers; otherwise the items go one
                # per round, in order. Without DATABASE_URL a worker cannot be
                # given its own connection, and the items run on this thread,
                # on the tick's connection, with no deadline. The worker
                # process cannot start without DATABASE_URL, so that branch is
                # reached only by code that drives a tick with a connection it
                # built itself.
                if os.environ.get("DATABASE_URL"):
                    if len(batch_to_dispatch) > 1 and max_parallel > 1:
                        rounds = [batch_to_dispatch[:max_parallel]]
                        # Budget was optimistically reserved for EVERY item in
                        # batch_to_dispatch (the reservation loop above), but
                        # only the first round is dispatched. Return the
                        # reservation for the dropped tail so remaining_budget
                        # isn't permanently under-counted; those tasks stay
                        # pending and are re-reserved next tick.
                        for score, task, model, est_cost in batch_to_dispatch[max_parallel:]:
                            _budget_for(task).max_cost_usd += est_cost
                            _budget_for(task).max_api_calls += 1
                            if model == "claude-opus-4-6":
                                _budget_for(task).max_opus_calls += 1
                    else:
                        rounds = [[entry] for entry in batch_to_dispatch]

                    for round_number, capped in enumerate(rounds):
                        if shutdown_event and shutdown_event.is_set():
                            break
                        if round_number:
                            # RPM spacing between one-item rounds
                            time.sleep(RPM_SPACING.get(rounds[round_number - 1][0][2], 1.0))
                        futures = {}
                        abandoned = {}  # task id -> the event that closes its actuators
                        outcomes = []  # (score, task, model, est_cost, outcome)
                        # Not a `with` block: its exit would join every worker, so one
                        # hung agent would hang the tick regardless of any timeout.
                        pool = ThreadPoolExecutor(max_workers=max(1, min(len(capped), max_parallel)))
                        try:
                            for idx, (score, task, model, est_cost) in enumerate(capped):
                                # Stagger starts to respect RPM limits
                                stagger = idx * RPM_SPACING.get(model, 1.0)
                                abandoned[task.id] = threading.Event()
                                fut = pool.submit(
                                    _dispatch_one_parallel,
                                    task, task.target_agent, model, state,
                                    dry_run, tick_id, stagger, shutdown_event,
                                    model_calls, abandoned[task.id],
                                )
                                futures[fut] = (score, task, model, est_cost)

                            # Wall-clock bound for the whole round: the longest
                            # per-agent timeout among its members. wait() hands
                            # back the futures that finished and those that did
                            # not, so nothing here depends on which exception
                            # class a timeout raises (concurrent.futures.TimeoutError
                            # is the builtin only from Python 3.11, and the worker
                            # image is built on Ubuntu 22.04, whose Python is 3.10).
                            round_timeout_s = max(
                                _get_agent_timeout(t.target_agent, t.category)
                                for _s, t, _m, _c in capped
                            )
                            finished, _unfinished = wait_for_futures(list(futures), timeout=round_timeout_s)
                            for fut, (score, task, model, est_cost) in futures.items():
                                if fut in finished:
                                    try:
                                        outcome = fut.result()
                                    except Exception as exc:  # worker already catches; belt and braces
                                        outcome = {"task_id": task.id, "decision": "error",
                                                   "agent": task.target_agent,
                                                   "error": str(exc)[:500]}
                                else:
                                    timeout_s = _get_agent_timeout(task.target_agent, task.category)
                                    logger.warning("Agent %s timed out on %s after %ds",
                                                   task.target_agent, task.category, timeout_s)
                                    fut.cancel()
                                    # The thread cannot be stopped. Refuse what it
                                    # asks the actuators for from here on, and close
                                    # its task so the row does not sit 'running'
                                    # (holding the work item's dedup slot) until the
                                    # stuck-task sweep.
                                    abandoned[task.id].set()
                                    _close_task_at_deadline(conn, task.id, round_timeout_s, dry_run)
                                    outcome = {
                                        "task_id": task.id, "decision": "error",
                                        "agent": task.target_agent, "timed_out": True,
                                        "error": f"Agent timed out after {timeout_s}s"}
                                outcomes.append((score, task, model, est_cost, outcome))
                        finally:
                            # Do not wait: a timed-out worker keeps running on its own
                            # DB connection but can no longer hold up this tick.
                            pool.shutdown(wait=False, cancel_futures=True)

                        for score, task, model, est_cost, outcome in outcomes:
                            if outcome.get("skipped"):
                                tasks_skipped += 1
                                # Return reserved budget
                                _budget_for(task).max_cost_usd += est_cost
                                _budget_for(task).max_api_calls += 1
                                if model == "claude-opus-4-6":
                                    _budget_for(task).max_opus_calls += 1
                                continue

                            if outcome.get("decision") == "error":
                                logger.error("[tick-%d] Agent %s failed: %s",
                                             tick_number, outcome["agent"], outcome.get("error"))
                                errors.append({
                                    "task_id": outcome["task_id"],
                                    "agent": outcome["agent"],
                                    "error": outcome.get("error", "")[:500],
                                })
                                tasks_skipped += 1
                                # Return the reservation, except for a worker that is
                                # still running: it may yet spend what it reserved.
                                if not outcome.get("timed_out"):
                                    _budget_for(task).max_cost_usd += est_cost
                                    _budget_for(task).max_api_calls += 1
                                    if model == "claude-opus-4-6":
                                        _budget_for(task).max_opus_calls += 1
                                continue

                            # Successful dispatch
                            actual_cost = outcome.get("actual_cost_usd", 0.0)
                            total_cost += actual_cost
                            # Adjust budget: return the reservation, then charge the
                            # task (its estimate when the agent reported nothing).
                            charged_cost, charged_calls = _task_charge(
                                actual_cost, outcome.get("api_calls", 0), est_cost,
                                default_calls=0 if task.id in free_task_ids else 1)
                            _budget_for(task).max_cost_usd += est_cost
                            _budget_for(task).max_cost_usd -= charged_cost
                            _budget_for(task).max_api_calls += 1
                            _budget_for(task).max_api_calls -= charged_calls
                            if model == "claude-opus-4-6":
                                _budget_for(task).max_opus_calls += 1
                                if outcome.get("success"):
                                    _budget_for(task).max_opus_calls -= 1

                            tasks_dispatched += 1
                            if outcome.get("success"):
                                tasks_completed += 1

                            decisions.append({
                                "task_id": task.id, "decision": "dispatch",
                                "agent": task.target_agent, "model": model,
                                "score": score, "parallel": len(capped) > 1,
                                "estimated_cost_usd": est_cost,
                                "actual_cost_usd": actual_cost,
                                "success": outcome.get("success", False),
                                "budget_after": {
                                    "remaining_usd": remaining_budget.max_cost_usd,
                                    "remaining_calls": remaining_budget.max_api_calls,
                                },
                            })
                else:
                    # No DATABASE_URL: on this thread, without a deadline.
                    for score, task, model, est_cost in batch_to_dispatch:
                        if shutdown_event and shutdown_event.is_set():
                            break

                        agent_type = task.target_agent
                        try:
                            result = _dispatch_agent(
                                conn, task, agent_type, model, state, dry_run, tick_id,
                                model_calls=model_calls,
                            )

                            actual_cost = result.cost_usd if result else 0.0
                            total_cost += actual_cost
                            # Adjust budget: return the reservation, then charge
                            # the task (its estimate when the agent reported nothing).
                            charged_cost, charged_calls = _task_charge(
                                actual_cost, result.api_calls if result else 0, est_cost,
                                default_calls=0 if task.id in free_task_ids else 1)
                            _budget_for(task).max_cost_usd += est_cost
                            _budget_for(task).max_cost_usd -= charged_cost
                            _budget_for(task).max_api_calls += 1
                            _budget_for(task).max_api_calls -= charged_calls
                            if model == "claude-opus-4-6":
                                # Return the reservation, then re-charge only on
                                # success so the opus sub-cap is actually
                                # enforced for sequential dispatches (mirrors the
                                # parallel path). The prior version returned the
                                # reservation but never re-decremented, so a
                                # successful sequential opus call never counted.
                                _budget_for(task).max_opus_calls += 1
                                if result and result.success:
                                    _budget_for(task).max_opus_calls -= 1

                            tasks_dispatched += 1
                            if result and result.success:
                                tasks_completed += 1

                            decisions.append({
                                "task_id": task.id, "decision": "dispatch",
                                "agent": agent_type, "model": model, "score": score,
                                "estimated_cost_usd": est_cost,
                                "actual_cost_usd": actual_cost,
                                "success": result.success if result else False,
                                "budget_after": {
                                    "remaining_usd": remaining_budget.max_cost_usd,
                                    "remaining_calls": remaining_budget.max_api_calls,
                                },
                            })

                            if result and result.actions_taken:
                                _check_and_queue_review(conn, task, result, agent_type, tick_id)

                            # Escalation check: agent requested approval or cost exceeded threshold
                            if result:
                                _check_escalation(task, result, agent_type, dry_run)

                            if result and result.follow_up_work:
                                for follow_up in result.follow_up_work:
                                    _persist_work_item(conn, follow_up, tick_id, parent_task_id=task.id)

                            if result and result.success:
                                # Surface initiative findings into coordinator_findings for admin review
                                _surface_initiative_findings(task, result, dry_run)

                            # Keep this task's review flag and follow-up
                            # work: a later task that fails rolls this
                            # connection back.
                            conn.commit()

                        except Exception as exc:
                            logger.error("[tick-%d] Agent %s failed: %s", tick_number, agent_type, exc)
                            errors.append({
                                "task_id": task.id, "agent": agent_type,
                                "error": str(exc)[:500],
                            })
                            tasks_skipped += 1
                            # Return reserved budget on failure
                            _budget_for(task).max_cost_usd += est_cost
                            _budget_for(task).max_api_calls += 1
                            if model == "claude-opus-4-6":
                                _budget_for(task).max_opus_calls += 1
                            _rollback_quietly(conn, "after an agent raised")

                        # RPM spacing between sequential dispatches
                        spacing = RPM_SPACING.get(model, 1.0)
                        time.sleep(spacing)

                # Budget exhausted check after each batch
                if remaining_budget.max_cost_usd < 0.01 or remaining_budget.max_api_calls <= 0:
                    logger.info("[tick-%d] Budget exhausted mid-dispatch", tick_number)
                    budget_exhausted = True

        # 7. LOG
        tick_end = datetime.now(timezone.utc)
        duration_s = (tick_end - tick_start).total_seconds()

        # Write tick record
        summary = {
            "tick_number": tick_number,
            "tick_id": tick_id,
            "duration_s": duration_s,
            "tasks_scored": tasks_scored,
            "tasks_dispatched": tasks_dispatched,
            "tasks_skipped": tasks_skipped,
            "tasks_completed": tasks_completed,
            "total_cost_usd": total_cost,
            "new_work_items": len(new_items),
            "active_builds": state.active_builds,
            "budget_mode": "haiku_only" if customer_builds_active(state) else "normal",
            "watermark": watermark,
        }

        _write_tick_record(
            conn, tick_id, tick_number, tick_start, tick_end, duration_s,
            state, budget, tasks_scored, tasks_dispatched, tasks_skipped,
            tasks_completed, total_cost, decisions, errors,
            model_calls=model_calls,
        )

        conn.commit()

        logger.info(
            "[tick-%d] Complete: scored=%d dispatched=%d skipped=%d completed=%d cost=$%.2f (%.1fs)",
            tick_number, tasks_scored, tasks_dispatched, tasks_skipped,
            tasks_completed, total_cost, duration_s,
        )

        return summary

    except Exception as exc:
        logger.error("[tick-%d] Tick failed: %s", tick_number, exc, exc_info=True)
        errors.append({"error": str(exc)[:500], "phase": "tick_lifecycle"})
        _rollback_quietly(conn, "after the tick failed")
        failed = {
            "tick_number": tick_number,
            "error": str(exc)[:500],
            "tasks_scored": tasks_scored,
            "tasks_dispatched": tasks_dispatched,
            "tasks_skipped": tasks_skipped,
            "tasks_completed": tasks_completed,
            "total_cost_usd": total_cost,
        }
        if detection_committed:
            # The work items are in the database; looking at the same
            # interval again would file them a second time.
            failed["watermark"] = watermark
        return failed


def _model_call_estimate(actions) -> float:
    """Estimated dollars of the model calls an Actuators instance granted."""
    return sum(
        estimate_task_cost("", a.details.get("model", ""))
        for a in actions
        if getattr(a, "action_type", "") == "model_call" and not a.details.get("dry_run")
    )


def _dispatch_agent(conn, task, agent_type, model, state, dry_run, tick_id, model_calls=None,
                    abandoned=None):
    """Dispatch a single agent task and return the AgentResult.

    model_calls is the tick's ModelCallMeter; without one the agent's
    actuators grant no model call. abandoned is the event that closes the
    actuators once the tick has given up on this task.

    Raises TaskClosedElsewhere when the task's row is no longer 'running' by
    the time the agent returns; nothing is written for it then.
    """
    agent_cls = _agent_registry[agent_type]
    agent = agent_cls()

    # The one Actuators this task's agent gets: this agent's permissions, the
    # tick's dry-run flag and the tick's model-call allowance. The agent does
    # not build another (Actuators.__init__ refuses it).
    actuators = Actuators(agent_name=agent_type, task_id=task.id, dry_run=dry_run,
                          model_calls=model_calls, abandoned=abandoned)

    # Estimated cost for budget tracking
    estimated_cost = estimate_task_cost(agent_type, model)

    # Mark task as running. The mode the tick runs in is written to the row
    # now, so a row that is never completed (the stuck-task sweep fails it
    # later) is still known to be a dry-run row.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET status = 'running', started_at = NOW(), "
            "tick_id = %s, model = %s, estimated_cost_usd = %s, result = %s WHERE id = %s",
            (tick_id, model, estimated_cost, _task_result_json({}, dry_run) if dry_run else None,
             task.id),
        )
    conn.commit()

    # Execute. An agent that raises must not leave its task 'running': until
    # _expire_stale_work fails it (COORDINATOR_STUCK_TASK_HOURS later) the row
    # would hold the dedup slot of its work item, and the event behind it
    # could not be filed again.
    start = time.time()
    try:
        result = agent.execute(task=task, state=state, model=model, actuators=actuators)
    except Exception as exc:
        _fail_task_after_exception(conn, task.id, exc, time.time() - start, dry_run)
        raise
    duration = time.time() - start

    # What the ledger records for this task: what the agent measured, if it
    # measured anything; otherwise the larger of the task's reservation and the
    # estimates of the model calls its actuators granted (a task that made ten
    # calls is not booked as one).
    if result.cost_usd > 0:
        actual_cost = result.cost_usd
    else:
        actual_cost = max(estimated_cost, _model_call_estimate(actuators.actions))

    # Update task record, but only a row that is still this run's: a task
    # the tick closed at the deadline (or the stuck-task sweep failed) keeps
    # what was written then, and this late result is dropped.
    status = "completed" if result.success else "failed"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET status = %s, completed_at = NOW(), duration_s = %s, "
            "actual_cost_usd = %s, result = %s, actions_taken = %s, learnings = %s, "
            "escalated = %s, escalation_reason = %s WHERE id = %s AND status = 'running'",
            (status, duration, actual_cost,
             _task_result_json(result.output, dry_run, result.effects),
             _json_dumps([a.__dict__ if hasattr(a, '__dict__') else str(a) for a in result.actions_taken]),
             _json_dumps(result.learnings),
             result.escalate, result.escalation_reason or None,
             task.id),
        )
        closed_elsewhere = cur.rowcount == 0
    if closed_elsewhere:
        conn.rollback()
        raise TaskClosedElsewhere(f"task {task.id} was not 'running' after {duration:.0f}s")
    conn.commit()

    unmet = [e for e in (result.effects or []) if e.get("outcome") in ("refused", "failed")]
    if unmet:
        logger.warning(
            "Task %s (%s): %d side effect(s) did not take place: %s",
            task.id, agent_type, len(unmet),
            "; ".join(f"{e.get('effect')} {e.get('outcome')}" for e in unmet),
        )

    return result


def _close_task_at_deadline(conn, task_id, timeout_s, dry_run: bool = False) -> None:
    """Mark a task failed because its agent ran past the round's deadline.
    Only a row that is still 'running' is written. Never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_tasks SET status = 'failed', completed_at = NOW(), "
                "duration_s = %s, result = %s WHERE id = %s AND status = 'running'",
                (timeout_s,
                 _task_result_json({"error": f"Agent timed out after {timeout_s}s", "timed_out": True}, dry_run),
                 task_id),
            )
        conn.commit()
    except Exception as record_exc:
        logger.warning("could not close task %s at its deadline: %s", task_id, record_exc)
        _rollback_quietly(conn, "after closing a task at its deadline failed")


def _fail_task_after_exception(conn, task_id, exc, duration_s: float, dry_run: bool = False) -> None:
    """Mark a dispatched task failed because its agent raised. Never raises."""
    try:
        _rollback_quietly(conn, "before marking a task failed")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_tasks SET status = 'failed', completed_at = NOW(), "
                "duration_s = %s, result = %s WHERE id = %s AND status = 'running'",
                (duration_s,
                 _task_result_json({"error": f"{type(exc).__name__}: {str(exc)[:500]}"}, dry_run),
                 task_id),
            )
        conn.commit()
    except Exception as record_exc:
        logger.warning("could not mark task %s failed after its agent raised: %s", task_id, record_exc)


def _persist_work_item(conn, item: WorkItem, tick_id: str, parent_task_id: str = None) -> bool:
    """Insert a new work item into agent_tasks. Returns True if inserted, False if duplicate."""
    if not item.context_hash:
        item.context_hash = compute_context_hash(item.context)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (id, tick_id, agent, task_category, task_description, "
            "context, context_hash, source_agent, parent_task_id, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending') "
            "ON CONFLICT (task_category, context_hash) "
            "WHERE status NOT IN ('completed', 'failed', 'approved', 'rejected', 'expired') "
            "DO NOTHING RETURNING id",
            (str(uuid.uuid4()), tick_id, item.target_agent, item.category,
             item.description, _json_dumps(item.context),
             item.context_hash, item.source, parent_task_id),
        )
        inserted = cur.fetchone()
    return inserted is not None


def _expire_stale_work(conn, tick_id: str):
    """Expire pending work items older than WORK_EXPIRY_HOURS and stuck running tasks older than STUCK_TASK_HOURS."""
    with conn.cursor() as cur:
        # Pending tasks that were never dispatched
        cur.execute(
            "UPDATE agent_tasks SET status = 'expired', expired_at = NOW() "
            "WHERE status = 'pending' AND created_at < NOW() - interval '1 hour' * %s "
            "RETURNING task_category",
            (config.WORK_EXPIRY_HOURS,),
        )
        expired = cur.fetchall()

        # Stuck running tasks (agent crashed or never completed)
        cur.execute(
            "UPDATE agent_tasks SET status = 'failed', completed_at = NOW() "
            "WHERE status = 'running' AND created_at < NOW() - interval '1 hour' * %s "
            "RETURNING task_category",
            (config.STUCK_TASK_HOURS,),
        )
        stuck = cur.fetchall()
    expired = list(expired) + list(stuck)

    if expired:
        from collections import Counter
        counts = Counter(r[0] for r in expired)
        logger.info("Expired %d stale work items: %s", len(expired),
                    ", ".join(f"{count} x {category}" for category, count in sorted(counts.items())))


def _check_and_queue_review(conn, task, result, agent_type, tick_id):
    """Check if any actions require review and queue if so."""
    reviewer = REVIEWER_MAP.get(agent_type)
    if not reviewer:
        return

    needs_review = any(
        hasattr(a, 'action_type') and a.action_type in ACTIONS_REQUIRING_REVIEW
        for a in result.actions_taken
    )

    if needs_review:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_tasks SET review_agent = %s, review_status = 'pending' "
                "WHERE id = %s",
                (reviewer, task.id),
            )


def _check_escalation(task, result, agent_type, dry_run):
    """File an approval request when the agent's result asks for escalation.

    The request is a note for a person at /admin/approvals: the task has
    already run, and marking the request approved executes nothing.
    """
    if not result.escalate:
        return

    description = result.escalation_reason or "Agent requested founder approval"
    context = {
        "agent": agent_type,
        "task_id": task.id,
        "task_category": task.category,
        "task_description": task.description[:300],
        "cost_usd": result.cost_usd,
        "escalate_flag": result.escalate,
    }
    if result.escalation_reason:
        context["escalation_reason"] = result.escalation_reason

    # Filed under the agent's own name: it needs the agent's approval_request
    # permission, is refused in analysis-only mode and files nothing under
    # dry run, like any other action of that agent.
    actuator = Actuators(agent_name=agent_type, task_id=task.id, dry_run=dry_run)
    try:
        request_id = actuator.create_approval_request(
            request_type=f"escalation:{agent_type}",
            description=description,
            context=context,
        )
    except PermissionError as refused:
        logger.warning(
            "[escalation] Approval request for task %s (%s) was not filed: %s",
            task.id[:8], agent_type, refused,
        )
        return
    logger.info(
        "[escalation] Approval request %s for task %s (%s): %s",
        "created" if request_id else "not created (dry run, duplicate or no database)",
        task.id[:8], agent_type, description[:120],
    )


def _write_tick_placeholder(conn, tick_id, tick_number, started_at):
    """Write a minimal tick record so agent_tasks FK is satisfied."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO coordinator_ticks "
            "(id, tick_number, started_at, world_state_summary, tick_budget) "
            "VALUES (%s, %s, %s, %s, %s)",
            (tick_id, tick_number, started_at, '{}', '{}'),
        )
    conn.commit()


def _write_tick_record(conn, tick_id, tick_number, started_at, completed_at,
                       duration_s, state, budget, tasks_scored, tasks_dispatched,
                       tasks_skipped, tasks_completed, total_cost, decisions, errors,
                       model_calls=None):
    """Update the tick record with final results.

    Rolls back any poisoned transaction state defensively before running the
    UPDATE. If an upstream helper caught an exception without rolling back,
    the conn may be in InFailedSqlTransaction state and the UPDATE would fail
    with "current transaction is aborted". The rollback discards any
    uncommitted upstream work (already best-effort) and guarantees the tick
    record gets its completed_at/tasks_scored/etc. written.

    model_calls is the tick's ModelCallMeter: the number of model calls it
    granted and their estimated cost are stored with the budget, so a tick's
    record shows what it was allowed and what it used.
    """
    _rollback_quietly(conn, "before writing the tick record")

    # A figure the tick could not read is stored as null, not as 0.
    world_summary = {
        "queued_builds": state.queued_builds,
        "active_builds": state.active_builds,
        "failed_builds_24h": None if state.failed_builds_24h is None else len(state.failed_builds_24h),
        "apps_down": None if state.monitored_apps_down is None else len(state.monitored_apps_down),
        "pending_feedback": state.pending_feedback,
        "api_cost_today_usd": state.api_cost_today_usd,
        "daily_budget_remaining_usd": state.daily_budget_remaining_usd,
        "spend_known": bool(getattr(state, "spend_known", True)),
    }
    budget_summary = {
        "max_api_calls": budget.max_api_calls,
        "max_cost_usd": budget.max_cost_usd,
        "max_opus_calls": budget.max_opus_calls,
        "allowed_models": budget.allowed_models,
    }
    if model_calls is not None:
        granted_calls, granted_usd = model_calls.granted()
        budget_summary["model_calls_granted"] = granted_calls
        budget_summary["model_calls_estimated_usd"] = round(granted_usd, 4)

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE coordinator_ticks SET "
            "completed_at = %s, duration_s = %s, "
            "world_state_summary = %s, tick_budget = %s, "
            "tasks_scored = %s, tasks_dispatched = %s, "
            "tasks_skipped = %s, tasks_completed = %s, "
            "total_cost_usd = %s, decisions = %s, errors = %s "
            "WHERE id = %s",
            (completed_at, duration_s,
             _json_dumps(world_summary), _json_dumps(budget_summary),
             tasks_scored, tasks_dispatched, tasks_skipped, tasks_completed,
             total_cost, _json_dumps(decisions), _json_dumps(errors),
             tick_id),
        )


def _failure_circuit(conn, agent_type: str, category: str) -> tuple:
    """(state, failure rate) of the failure circuit for an agent+category pair.

    The circuit looks at the pair's most recent config.FAILURE_RATE_WINDOW
    LIVE outcomes (rows written under dry run are left out, see
    DRY_RUN_RESULT_KEY), newest first:

      "closed"     fewer than 3 outcomes, or the newest one succeeded (one
                   success closes the circuit), or the failure rate is below
                   config.FAILURE_RATE_THRESHOLD. Tasks are dispatched.
      "open"       the newest outcome failed, the rate is at or above the
                   threshold, and that newest outcome is less than
                   config.FAILURE_RETRY_HOURS old. Non-P0 tasks are skipped.
      "half_open"  as "open", but the newest outcome is at least that old:
                   one task may be dispatched as a probe. If it succeeds the
                   circuit is closed; if it fails the circuit is open again
                   until the interval has passed once more.
      "unknown"    the history could not be read. Treated like "open".

    A skipped task writes no outcome, so without the half-open state an open
    circuit could never see the success that would close it.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, "
                "       COALESCE(completed_at, created_at) <= NOW() - make_interval(secs => %s) "
                "FROM agent_tasks "
                "WHERE agent = %s AND task_category = %s "
                "  AND status IN ('completed', 'failed') "
                "  AND (result->>%s) IS DISTINCT FROM 'true' "
                "ORDER BY COALESCE(completed_at, created_at) DESC LIMIT %s",
                (config.FAILURE_RETRY_HOURS * 3600.0, agent_type, category, DRY_RUN_RESULT_KEY,
                 config.FAILURE_RATE_WINDOW),
            )
            rows = cur.fetchall()
    except Exception as exc:
        # Say so, and clear the aborted transaction so later queries still run.
        logger.warning("failure history lookup failed for %s/%s: %s", agent_type, category, exc)
        _rollback_quietly(conn, "after the failure history lookup failed")
        return "unknown", 0.0

    if len(rows) < 3:
        return "closed", 0.0
    rate = sum(1 for row in rows if row[0] == "failed") / len(rows)
    newest_status, retry_due = rows[0][0], bool(rows[0][1])
    if newest_status != "failed" or rate < config.FAILURE_RATE_THRESHOLD:
        return "closed", rate
    return ("half_open" if retry_due else "open"), rate


def _surface_initiative_findings(task, result, dry_run: bool) -> int:
    """Route agent.execute() findings list into coordinator_findings for admin review.

    When an agent returns `result.output["findings"]` as a non-empty list of
    non-empty strings, each entry becomes a row in `coordinator_findings`
    visible at `/admin/findings`. Admin can then review/dismiss/promote each
    finding to a roadmap item.

    Stable dedup key: `init-{agent}-{sha256(text)[:12]}`. Same finding text
    from repeated initiative runs hits the unique index and is a no-op via
    `ON CONFLICT DO NOTHING`.

    Uses its own Actuators instance because `finding_add()` opens its own
    DB connection via DATABASE_URL: safe to call from either the parallel
    (thread_conn) or serial (main conn) dispatch paths.

    Returns the number of findings successfully surfaced (0 on empty input
    or any permission/DB error). Never raises; failure here must not break
    the tick.
    """
    if not result or not result.success:
        return 0
    output = result.output if isinstance(result.output, dict) else {}
    findings = output.get("findings")
    if not findings or not isinstance(findings, list):
        return 0

    import hashlib
    agent = task.target_agent
    surfaced = 0

    # Fresh actuators just for finding writes. Does not mutate the agent's
    # own actuators (those have already been committed to agent_tasks.result).
    initiative_actuators = Actuators(
        agent_name=agent, task_id=task.id, dry_run=dry_run,
    )

    for finding_text in findings:
        if not isinstance(finding_text, str):
            continue
        text = finding_text.strip()
        if not text:
            continue
        text_hash = hashlib.sha256(text.encode()).hexdigest()[:12]
        finding_key = f"init-{agent}-{text_hash}"
        title_preview = text[:100]
        try:
            ok = initiative_actuators.finding_add(
                finding_key=finding_key,
                title=f"[{agent}] {title_preview}",
                description=text,
                priority="medium",
                category=f"initiative_{agent}",
                source_task_id=task.id,
            )
            if ok:
                surfaced += 1
        except PermissionError:
            # An agent that returns findings holds finding_write
            # (tests/test_coordinator_permission_use.py), so this is a grant
            # that was removed without its handler. Log it; the tick goes on.
            logger.warning(
                "finding_write denied for %s while surfacing initiative finding",
                agent,
            )
        except Exception as exc:
            logger.warning("finding_add failed for %s: %s", finding_key, exc)

    if surfaced:
        logger.info(
            "Surfaced %d initiative finding(s) from %s into coordinator_findings",
            surfaced, agent,
        )
    return surfaced


def _expire_stale_approval_requests(conn):
    """Remind about stale approval requests, then deny them on timeout.

    - After config.APPROVAL_REMINDER_HOURS (default 3): one reminder email.
    - After config.APPROVAL_TIMEOUT_HOURS (default 4): status 'rejected',
      resolved_by 'timeout_auto_deny'.

    The timeout fails closed. An approval request guards an action the
    coordinator must not take on its own (a production commit, promoting a
    preview build, a flagged escalation); nobody answering is not consent.
    Approval requests are advisory in this codebase (nothing executes an
    approved request), so a denied one simply means "not done, ask a human".
    approval_requests.status only allows pending/approved/rejected, hence
    'rejected' rather than a separate 'expired' state.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, type, description FROM approval_requests "
            "WHERE status = 'pending' "
            "AND created_at < NOW() - INTERVAL '1 hour' * %s "
            "AND (context->>'reminder_sent') IS NULL",
            (config.APPROVAL_REMINDER_HOURS,))
        for row in cur.fetchall():
            _send_approval_reminder(row[0], row[1], row[2])
            cur.execute(
                "UPDATE approval_requests SET context = "
                "COALESCE(context, '{}'::jsonb) || '{\"reminder_sent\": true}'::jsonb "
                "WHERE id = %s", (row[0],))

        cur.execute(
            "UPDATE approval_requests SET status = 'rejected', "
            "resolved_at = NOW(), resolved_by = 'timeout_auto_deny' "
            "WHERE status = 'pending' "
            "AND created_at < NOW() - INTERVAL '1 hour' * %s "
            "RETURNING id, type",
            (config.APPROVAL_TIMEOUT_HOURS,))
        denied = cur.fetchall()
        if denied:
            logger.info("Denied %d approval request(s) unanswered after %dh",
                        len(denied), config.APPROVAL_TIMEOUT_HOURS)
    conn.commit()


def _send_approval_reminder(request_id, request_type, description):
    """Best-effort reminder email for pending approval requests."""
    try:
        from coordinator.actuators import _send_escalation_email
        _send_escalation_email(
            request_type=f"reminder:{request_type}",
            description=f"REMINDER: {description}",
            context={"original_request_id": str(request_id), "reminder": True},
            request_id=str(request_id),
        )
    except Exception as exc:
        logger.warning("Failed to send approval reminder for request %s: %s", request_id, exc)


def _is_agent_enabled(agent_type: str, global_dry_run: bool) -> bool:
    """Check if a specific agent is enabled via env vars."""
    env_map = {
        "infra_engineer": "AGENT_INFRA_ENABLED",
        "financial": "AGENT_FINANCIAL_ENABLED",
        "customer_service": "AGENT_CUSTOMER_ENABLED",
        "documentation": "AGENT_DOCS_ENABLED",
        "security": "AGENT_SECURITY_ENABLED",
        "qa_test": "AGENT_QA_ENABLED",
        "backend_engineer": "AGENT_BACKEND_ENABLED",
        "cost_analyst": "AGENT_COST_ENABLED",
        "architect": "AGENT_ARCHITECT_ENABLED",
    }
    # Agents that are OFF by default; require explicit opt-in via env var.
    # The reconciliation sweep is kept disabled until it has been validated on
    # staging so zombie recovery is never accidentally triggered on production.
    default_disabled = {"reconciliation"}

    env_key = env_map.get(agent_type)
    if not env_key:
        # reconciliation uses its own AGENT_RECONCILIATION_ENABLED var, the
        # same switch its trigger reads (work_detection._check_reconciliation_sweep).
        if agent_type in default_disabled:
            return arming_switch("AGENT_RECONCILIATION_ENABLED")
        return True  # Unknown agents default to enabled

    # Unset: the agent runs (under the tick's dry-run flag like every other).
    # Set: it runs only for an explicit ON word.
    return arming_switch(env_key, default=True)
