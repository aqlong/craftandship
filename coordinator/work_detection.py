# coordinator/work_detection.py: Event-driven work item creation
#
# Detects events (failed builds, apps down, new feedback, etc.) via cheap
# DB queries and creates deduplicated WorkItems for the coordinator to score.
# The triggers only read, with one exception: failure triage in
# _check_failed_builds, which acts through a gated Actuators instance (see
# detect_work).

#
# Every trigger defined here is called from detect_work, and every (category,
# agent) pair a trigger can emit is one that agent dispatches to a handler;
# tests/test_coordinator_routing.py checks both against the agents themselves.
#
# Proactive triggers (in addition to reactive events):
#   RECURRING_QA_WARNING: Same QA warning type in 3+ of the last 5 completed builds
#   COORDINATOR_STALLED: 3+ ticks in the window with nothing scored or dispatched
#   AGENT_INITIATIVE: When no reactive work is queued, agents take turns, each
#     at most once in config.INITIATIVE_INTERVAL_DAYS (7) days
#
# Triggers this module no longer has. Five were switched off while the system
# ran and are gone with their tests: PREVIEW_READY (promoting a preview is a
# person's decision), PIPELINE_IDLE (too noisy when no builds are queued),
# PLATFORM_IMPROVEMENT and WEEKLY_DOCS_SYNC (their agents were restricted to
# analysis and could not act on them), and ROADMAP_NEXT (roadmap items belong
# to the local executor, scripts/cas-roadmap-loop.sh; with both readers on,
# every item was dispatched twice). Four more fired but were routed to an
# agent with no handler for them, so each firing only recorded a failed task:
# KB_PATTERN_DEGRADING, LEARNING_VALIDATION, PERSONA_UPDATE_PROPOSAL and
# SELF_IMPROVEMENT.

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coordinator.actuators import Actuators
from coordinator.envflags import arming_switch
from coordinator.state import WorkItem, WorldState, budget_remaining_usd, pipeline_idle
from coordinator import config

logger = logging.getLogger("coordinator.work_detection")

# Upper bound on failure-triage model calls in one tick (see _check_failed_builds).
MAX_TRIAGE_CALLS_PER_TICK = 5


def detect_work(conn, state: WorldState, last_tick_time: datetime | None,
                dry_run: bool = True, model_calls=None,
                tick_started_at: datetime | None = None) -> list[WorkItem]:
    """Run all event triggers and return new WorkItems.

    The triggers that look for new rows, and the periodic ones, work on the
    interval (last_tick_time, tick_started_at]: the previous tick's watermark,
    exclusive, to this tick's, inclusive. Both come from the database clock
    (coordinator.tick), so consecutive ticks cover every moment exactly once.

    The triggers read. The one exception is failure triage inside
    _check_failed_builds (a cheap-tier model call that categorizes a failed
    build, plus two writes that record the answer); it acts only through an
    Actuators instance built here, so the permission table, analysis-only mode,
    dry run and the tick's model-call allowance apply to it like to any agent.

    Args:
        conn: psycopg2 connection
        state: current WorldState snapshot
        last_tick_time: the previous tick's watermark (None when there is no
            earlier tick to continue from)
        dry_run: the tick's dry-run flag; under dry run triage does nothing
        model_calls: the tick's ModelCallMeter (coordinator.budget); triage
            makes a model call only when it grants one, and none without it
        tick_started_at: this tick's watermark; the current time when omitted

    Returns:
        List of new, deduplicated WorkItems
    """
    items: list[WorkItem] = []
    now = tick_started_at or datetime.now(timezone.utc)
    triage = Actuators(agent_name="failure_triage", task_id="work-detection", dry_run=dry_run,
                       model_calls=model_calls)

    # Event-driven triggers (checked each tick)
    items.extend(_run_check(conn, _check_failed_builds, conn, state, last_tick_time, triage))
    items.extend(_check_apps_down(state))
    items.extend(_run_check(conn, _check_new_feedback, conn, last_tick_time, now))
    items.extend(_run_check(conn, _check_stale_backup, conn))
    items.extend(_check_budget_alert(state))
    items.extend(_run_check(conn, _check_new_live_apps, conn, last_tick_time, now))

    # Proactive pipeline health triggers
    items.extend(_run_check(conn, _check_recurring_qa_warnings, conn))
    items.extend(_run_check(conn, _check_coordinator_stalled, conn))

    # Architecture review triggers
    items.extend(_run_check(conn, _check_spec_review, conn, last_tick_time, now))
    items.extend(_run_check(conn, _check_post_build_review, conn, last_tick_time, now))
    items.extend(_run_check(conn, _check_architecture_qa_review, conn))

    # Automated test builds (beta readiness: continuous pipeline testing)
    items.extend(_run_check(conn, _check_test_build_needed, conn, state))

    # Autonomous agent initiatives (only when no reactive work queued)
    items.extend(_run_check(conn, _check_agent_initiative, conn))

    # Periodic triggers
    items.extend(_check_daily_security_scan(now, last_tick_time))
    items.extend(_check_daily_cost_report(now, last_tick_time))
    items.extend(_check_weekly_cost_projection(now, last_tick_time))
    items.extend(_run_check(conn, _check_reconciliation_sweep, conn, now, last_tick_time))

    # Deduplication happens at INSERT time via ON CONFLICT DO NOTHING
    # (see _persist_work_item in tick.py)
    return items


def _run_check(conn, check, *args) -> list[WorkItem]:
    """Run one trigger in its own read transaction and contain its failure.

    A failed query leaves a psycopg2 connection in an aborted transaction, and
    several triggers log and swallow their own errors without rolling back.
    Rolling back after every check (they only read) keeps one bad trigger from
    failing every trigger after it, and the persist step that follows.
    """
    try:
        return check(*args)
    except Exception as exc:
        logger.warning("work trigger %s failed: %s", getattr(check, "__name__", check), exc)
        return []
    finally:
        try:
            conn.rollback()
        except Exception as exc:
            logger.warning("rollback after work trigger %s did not succeed: %s",
                           getattr(check, "__name__", check), exc)


def compute_context_hash(context: dict) -> str:
    """SHA-256 hash of sorted-key JSON, truncated to 32 chars."""
    return hashlib.sha256(
        json.dumps(context, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]


def _make_item(category: str, target: str, description: str, context: dict, source: str) -> WorkItem:
    return WorkItem(
        category=category,
        target_agent=target,
        description=description,
        context=context,
        context_hash=compute_context_hash(context),
        source=source,
    )


def _upper_bound(now):
    """The inclusive end of the interval a row trigger reads: this tick's
    watermark, or the current time for a caller that passes none."""
    return now if now is not None else datetime.now(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    """`moment` in UTC (a naive value is taken to be UTC already)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _schedule_due(now: datetime, last_tick_time, hour: int, weekday: int | None = None) -> bool:
    """True when a daily (or, with `weekday`, weekly) HH:00 UTC slot falls in
    the interval (last_tick_time, now].

    The slot looked at is the most recent one at or before `now`. It is due
    when the previous watermark lies before it, however long ago that was, so
    a tick that starts late (a long tick before it, a restart) still runs the
    slot once. With no previous watermark at all the slot is due only within
    its own hour, so a first-ever tick does not fire every schedule at once.
    """
    now = _as_utc(now)
    slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=1)
    if weekday is not None:
        while slot.weekday() != weekday:
            slot -= timedelta(days=1)
    if last_tick_time is None:
        return now - slot < timedelta(hours=1)
    return _as_utc(last_tick_time) < slot


# --- Failure routing ---

_INFRA_CATEGORIES = {"infra", "transient"}  # DLQ error_category values


def _route_build_failure(error_category: str | None) -> str:
    """Route build failure to appropriate agent based on DLQ error category."""
    if error_category in _INFRA_CATEGORIES:
        return "infra_engineer"
    return "backend_engineer"


# --- Event-driven triggers ---

def _check_failed_builds(conn, state: WorldState, last_tick_time,
                         actuators: Actuators | None = None) -> list[WorkItem]:
    # Group failures by (error_category, target_agent) to avoid budget
    # starvation when many builds fail concurrently with the same root cause.
    # One task per unique error signature, not per build.
    #
    # A deterministic failure (ops.scanner.is_deterministic_failure, the rule
    # the worker's retry decision uses) gets no task: the only thing the
    # handlers do with a failed build is queue it again, and the same input
    # fails the same way. The customer and the admin were told when the build
    # failed; the fix is in the spec or the engine.
    from ops.scanner import classify_error, is_deterministic_failure
    if state.failed_builds_24h is None:
        # The pipeline could not be read this tick. No list is not an empty
        # list, but there is nothing to triage or to route either.
        logger.warning("failed builds are unknown this tick; no build_failure work is filed")
        return []
    groups: dict[tuple[str, str], list[dict]] = {}
    triage_calls = 0
    triage_allowed = actuators is not None
    for fb in state.failed_builds_24h:
        if is_deterministic_failure(fb.get("error_message")):
            continue
        error_category = fb.get("error_category")
        if not error_category:
            try:
                error_category = classify_error(fb.get("error_message", ""))
            except Exception as exc:
                logger.warning("classify_error failed for build %s: %s", fb.get("id"), exc)
                error_category = "unknown"
        # When the regex classifier returns 'unknown' (or no category exists),
        # ask Claude Haiku to classify using the last 500 chars of build_logs.
        # Each call is granted, one at a time, by the gated `actuators`:
        # nothing under dry run or without the permission, and nothing unless
        # the tick's model-call meter covers a Haiku call (so none when the
        # tick's budget is zero or today's spend could not be read). What it
        # grants is deducted from what the dispatch loop may spend. On top of
        # that, at most MAX_TRIAGE_CALLS_PER_TICK builds are tried per tick.
        # Also skipped without ANTHROPIC_API_KEY, so local/test environments
        # never call out.
        if (not error_category or error_category == "unknown") \
                and triage_allowed and triage_calls < MAX_TRIAGE_CALLS_PER_TICK:
            triage_calls += 1
            try:
                from coordinator.build_failure_categorizer import enrich_uncategorized
                error_category = enrich_uncategorized(
                    conn, fb, error_category, actuators=actuators)
            except Exception as exc:
                logger.warning("Haiku categorization enrichment failed: %s", exc)
                error_category = error_category or "unknown"
        target_agent = _route_build_failure(error_category)
        key = (error_category, target_agent)
        groups.setdefault(key, []).append({**fb, "_error_category": error_category})

    items = []
    for (error_category, target_agent), builds in groups.items():
        build_ids = [b["id"] for b in builds]
        app_names = list(dict.fromkeys(b["app_name"] for b in builds))  # unique, ordered
        # Use first non-empty error message as representative
        representative_error = ""
        for b in builds:
            msg = (b.get("error_message") or "")[:500]
            if msg:
                representative_error = msg
                break
        ctx = {
            "build_id": build_ids[0],       # backward compat for agents
            "app_name": app_names[0],        # backward compat for agents
            "build_ids": build_ids,
            "app_names": app_names,
            "error_message": representative_error,
            "error_category": error_category,
            "build_count": len(builds),
        }
        desc = (f"Build failed: {app_names[0]}"
                if len(app_names) == 1
                else f"{len(builds)} builds failed ({error_category})")
        items.append(_make_item(
            "build_failure", target_agent,
            desc, ctx, "NEW_FAILED_BUILD",
        ))
    return items


def _check_apps_down(state: WorldState) -> list[WorkItem]:
    if state.monitored_apps_down is None:
        logger.warning("the monitor registry is unknown this tick; no app_down work is filed")
        return []
    items = []
    for app in state.monitored_apps_down:
        ctx = {"app_name": app["app_name"], "live_url": app.get("live_url", ""),
               "consecutive_failures": app.get("consecutive_failures", 0)}
        items.append(_make_item(
            "app_down", "infra_engineer",
            f"App down: {app['app_name']}",
            ctx, "APP_DOWN",
        ))
    return items


def _check_new_feedback(conn, last_tick_time, now=None) -> list[WorkItem]:
    if last_tick_time is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, category, message FROM beta_feedback "
            "WHERE created_at > %s AND created_at <= %s",
            (last_tick_time, _upper_bound(now)),
        )
        items = []
        for row in cur.fetchall():
            ctx = {"feedback_id": str(row[0]), "feedback_category": row[1],
                   "message_preview": (row[2] or "")[:200]}
            items.append(_make_item(
                "customer_feedback", "customer_service",
                f"New feedback: {row[1]}",
                ctx, "NEW_FEEDBACK",
            ))
    return items


def _check_stale_backup(conn) -> list[WorkItem]:
    """Fire when the newest successful 'backup' row in ops_actions is over 48h old.

    monitor.backup.run_daily_backups writes those rows. With no row at all (no
    backup has ever run, or no apps are registered) this stays quiet: absence
    of a record is not treated as an overdue backup.
    """
    with conn.cursor() as cur:
        # Check ops_actions for last successful backup
        cur.execute(
            "SELECT MAX(created_at) FROM ops_actions "
            "WHERE action_type = 'backup' AND success = TRUE"
        )
        row = cur.fetchone()

    if row and row[0]:
        last_backup = row[0]
        if last_backup.tzinfo is None:
            last_backup = last_backup.replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - last_backup).total_seconds()
        if age_s > 48 * 3600:  # 48 hours
            return [_make_item(
                "backup_overdue", "infra_engineer",
                "Database backup is overdue (>48h)",
                {"last_backup_age_hours": round(age_s / 3600, 1)},
                "STALE_BACKUP",
            )]
    return []


def _check_budget_alert(state: WorldState) -> list[WorkItem]:
    if not getattr(state, "spend_known", True) or state.daily_budget_remaining_usd is None:
        # An alert states a figure ("$X remaining"). With today's spend
        # unknown there is no figure to state; the tick already gets no
        # budget, and build_world_state logged why.
        return []
    if state.daily_budget_remaining_usd < 5.0:
        return [_make_item(
            "budget_alert", "financial",
            f"Daily budget low: ${state.daily_budget_remaining_usd:.2f} remaining",
            {"remaining_usd": state.daily_budget_remaining_usd,
             "cost_today_usd": state.api_cost_today_usd},
            "BUDGET_ALERT",
        )]
    return []


def _check_new_live_apps(conn, last_tick_time, now=None) -> list[WorkItem]:
    if last_tick_time is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, app_name FROM builds "
            "WHERE status = 'live' AND updated_at > %s AND updated_at <= %s",
            (last_tick_time, _upper_bound(now)),
        )
        items = []
        for row in cur.fetchall():
            ctx = {"build_id": str(row[0]), "app_name": row[1]}
            items.append(_make_item(
                "post_deploy_audit", "security",
                f"Post-deploy audit: {row[1]}",
                ctx, "NEW_LIVE_APP",
            ))
    return items


# --- Proactive triggers ---

_RECURRING_QA_MIN_OCCURRENCES = 3
_RECURRING_QA_WINDOW = 5


def _check_test_build_needed(conn, state: WorldState) -> list[WorkItem]:
    """Queue automated test builds with realistic app specs for beta readiness.

    Fires when no builds are currently active and fewer than 2 builds have been
    queued in the last hour. Picks a spec from the pool that hasn't been built
    recently. This ensures continuous pipeline testing at 1-2 builds/hour.

    Gated by ENABLE_TEST_BUILDS env var (default: false). Set to "true" to enable.
    """
    if not arming_switch("ENABLE_TEST_BUILDS"):
        return []
    try:
        # Don't queue if builds are queued or active, or if that is unknown
        if not pipeline_idle(state):
            return []

        cur = conn.cursor()
        try:
            # Check how many builds were queued in the last hour
            cur.execute(
                "SELECT COUNT(*) FROM builds "
                "WHERE created_at > NOW() - INTERVAL '1 hour' "
                "AND status NOT IN ('abandoned')"
            )
            recent = cur.fetchone()[0]
            if recent >= 2:
                return []

            # Check budget: don't queue if low (unknown spend reads as nothing left)
            if budget_remaining_usd(state) < 5.0:
                return []

            return [_make_item(
                "test_build", "backend_engineer",
                "Queue automated test build for beta readiness",
                {"trigger": "continuous_testing", "hour_builds": recent},
                "TEST_BUILD",
            )]
        finally:
            cur.close()
    except Exception as exc:
        logger.warning("test_build check failed: %s", exc)
    return []


def _check_recurring_qa_warnings(conn) -> list[WorkItem]:
    """Detect if the same QA warning type recurs across recent builds.

    Scans the last _RECURRING_QA_WINDOW completed builds with qa_status='warned'.
    If a warning type (e.g. 'slow_response', 'missing_field') appears in
    _RECURRING_QA_MIN_OCCURRENCES or more of them, creates a task for backend_engineer.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT qa_results FROM builds "
            "WHERE qa_status = 'warned' AND completed_at IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT %s",
            (_RECURRING_QA_WINDOW,),
        )
        rows = cur.fetchall()
    except Exception as exc:
        logger.warning("recurring_qa_warnings check failed: %s", exc)
        cur.close()
        return []
    finally:
        cur.close()

    if not rows:
        return []

    # Count occurrences of each warning type across builds
    warning_counts: dict[str, int] = {}
    for (qa_results,) in rows:
        if not qa_results:
            continue
        results = qa_results if isinstance(qa_results, list) else []
        seen_in_build: set[str] = set()
        for result in results:
            if not isinstance(result, dict):
                continue
            warning_type = result.get("type") or result.get("name") or result.get("test", "")
            if warning_type and result.get("status") in ("warning", "failed", "warn"):
                seen_in_build.add(str(warning_type)[:80])
        for wt in seen_in_build:
            warning_counts[wt] = warning_counts.get(wt, 0) + 1

    items = []
    for warning_type, count in warning_counts.items():
        if count >= _RECURRING_QA_MIN_OCCURRENCES:
            ctx = {
                "warning_type": warning_type,
                "occurrences": count,
                "window": _RECURRING_QA_WINDOW,
            }
            items.append(_make_item(
                "recurring_qa_warning", "backend_engineer",
                f"QA warning '{warning_type}' in {count}/{_RECURRING_QA_WINDOW} recent builds",
                ctx, "RECURRING_QA_WARNING",
            ))
    return items


def _check_coordinator_stalled(conn) -> list[WorkItem]:
    """Detect when the coordinator has been ticking for 30+ minutes with zero dispatches.

    Checks the last N ticks (within _STALLED_MINUTES window). If all have
    tasks_dispatched=0 and tasks_scored=0, creates a diagnostic work item
    for the infra_engineer to investigate.
    """
    try:
        cur = conn.cursor()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=config.STALLED_MINUTES)
            cur.execute(
                "SELECT COUNT(*) as total_ticks, "
                "COALESCE(SUM(tasks_dispatched), 0) as total_dispatched, "
                "COALESCE(SUM(tasks_scored), 0) as total_scored, "
                "array_agg(errors) as all_errors "
                "FROM coordinator_ticks "
                "WHERE started_at > %s AND completed_at IS NOT NULL",
                (cutoff,),
            )
            row = cur.fetchone()
            if not row or row[0] < 3:
                return []  # Not enough ticks to judge

            total_ticks, total_dispatched, total_scored, all_errors = row
            if total_dispatched > 0 or total_scored > 0:
                return []  # Coordinator is dispatching work

            # Gather diagnostics
            error_messages = []
            if all_errors:
                for errs in all_errors:
                    if errs and isinstance(errs, list):
                        for e in errs:
                            if isinstance(e, dict) and e.get("error"):
                                error_messages.append(str(e["error"])[:200])

            ctx = {
                "ticks_checked": total_ticks,
                "window_minutes": config.STALLED_MINUTES,
                "total_dispatched": total_dispatched,
                "total_scored": total_scored,
                "recent_errors": error_messages[:5],
            }
            return [_make_item(
                "coordinator_stalled", "infra_engineer",
                f"Coordinator stalled: {total_ticks} ticks in {config.STALLED_MINUTES}min with 0 dispatches",
                ctx, "COORDINATOR_STALLED",
            )]
        finally:
            cur.close()
    except Exception as exc:
        logger.warning("coordinator_stalled check failed: %s", exc)
    return []


# --- Periodic triggers ---

def _check_daily_security_scan(now: datetime, last_tick_time) -> list[WorkItem]:
    if _schedule_due(now, last_tick_time, hour=6):
        return [_make_item(
            "security_scan", "security",
            "Daily security scan",
            {"date": _as_utc(now).strftime("%Y-%m-%d")},
            "DAILY_SECURITY_SCAN",
        )]
    return []


def _check_daily_cost_report(now: datetime, last_tick_time) -> list[WorkItem]:
    if _schedule_due(now, last_tick_time, hour=0):
        return [_make_item(
            "cost_report", "cost_analyst",
            "Daily cost report",
            {"date": _as_utc(now).strftime("%Y-%m-%d")},
            "DAILY_COST_REPORT",
        )]
    return []


# --- Architecture review triggers ---

def _check_spec_review(conn, last_tick_time, now=None) -> list[WorkItem]:
    """Create a spec_review task for newly queued builds.

    Fires when a build transitions to 'queued' status since the last tick,
    giving the architect agent a chance to review the spec before building starts.
    """
    if last_tick_time is None:
        return []
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT id, app_name FROM builds "
                "WHERE status = 'queued' AND updated_at > %s AND updated_at <= %s "
                "AND spec IS NOT NULL",
                (last_tick_time, _upper_bound(now)),
            )
            items = []
            for row in cur.fetchall():
                ctx = {"build_id": str(row[0]), "app_name": row[1]}
                items.append(_make_item(
                    "spec_review", "architect",
                    f"Spec review: {row[1]}",
                    ctx, "SPEC_REVIEW",
                ))
            return items
        finally:
            cur.close()
    except Exception as exc:
        logger.warning("spec_review check failed: %s", exc)
    return []


def _check_post_build_review(conn, last_tick_time, now=None) -> list[WorkItem]:
    """Create a post_build_review task for recently completed builds.

    Fires when a build reaches 'preview' or 'live' status since the last tick,
    so the architect can review the generated app's quality signals.
    """
    if last_tick_time is None:
        return []
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT id, app_name FROM builds "
                "WHERE status IN ('preview', 'live') "
                "AND updated_at > %s AND updated_at <= %s",
                (last_tick_time, _upper_bound(now)),
            )
            items = []
            for row in cur.fetchall():
                ctx = {"build_id": str(row[0]), "app_name": row[1]}
                items.append(_make_item(
                    "post_build_review", "architect",
                    f"Post-build architecture review: {row[1]}",
                    ctx, "POST_BUILD_REVIEW",
                ))
            return items
        finally:
            cur.close()
    except Exception as exc:
        logger.warning("post_build_review check failed: %s", exc)
    return []


def _check_architecture_qa_review(conn) -> list[WorkItem]:
    """Create an architecture_qa_review when recurring QA warnings indicate
    a systemic issue worth architect-level analysis.

    Fires when a QA warning type appears in 4+ of the last 8 completed builds,
    indicating a pattern severe enough for architectural investigation (higher
    threshold than the backend_engineer's recurring_qa_warning trigger).
    """
    _ARCH_QA_MIN_OCCURRENCES = 4
    _ARCH_QA_WINDOW = 8

    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT qa_results FROM builds "
            "WHERE qa_status = 'warned' AND completed_at IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT %s",
            (_ARCH_QA_WINDOW,),
        )
        rows = cur.fetchall()
    except Exception as exc:
        logger.warning("architecture_qa_review check failed: %s", exc)
        cur.close()
        return []
    finally:
        cur.close()

    if not rows:
        return []

    warning_counts: dict[str, int] = {}
    for (qa_results,) in rows:
        if not qa_results:
            continue
        results = qa_results if isinstance(qa_results, list) else []
        seen_in_build: set[str] = set()
        for result in results:
            if not isinstance(result, dict):
                continue
            warning_type = result.get("type") or result.get("name") or result.get("test", "")
            if warning_type and result.get("status") in ("warning", "failed", "warn"):
                seen_in_build.add(str(warning_type)[:80])
        for wt in seen_in_build:
            warning_counts[wt] = warning_counts.get(wt, 0) + 1

    items = []
    for warning_type, count in warning_counts.items():
        if count >= _ARCH_QA_MIN_OCCURRENCES:
            ctx = {
                "warning_type": warning_type,
                "occurrences": count,
                "window": _ARCH_QA_WINDOW,
            }
            items.append(_make_item(
                "architecture_qa_review", "architect",
                f"Architecture QA review: '{warning_type}' in {count}/{_ARCH_QA_WINDOW} builds",
                ctx, "ARCHITECTURE_QA_REVIEW",
            ))
    return items


def _check_weekly_cost_projection(now: datetime, last_tick_time) -> list[WorkItem]:
    """Weekly cost projection on Fridays at 10:00 UTC."""
    # Friday = 4
    if _schedule_due(now, last_tick_time, hour=10, weekday=4):
        return [_make_item(
            "cost_projection", "cost_analyst",
            "Weekly cost projection and scaling analysis",
            {"week": _as_utc(now).strftime("%Y-W%W")},
            "WEEKLY_COST_PROJECTION",
        )]
    return []


# --- Autonomous agent initiatives ---

# Path to roadmap JSON (allows patching in tests)
_ROADMAP_PATH = Path(__file__).parent / "roadmap.json"

# Agent rotation order for initiative work
_AGENT_ROTATION = [
    "backend_engineer", "security", "cost_analyst", "customer_service",
    "documentation", "qa_test", "financial", "infra_engineer", "architect",
]


def _check_agent_initiative(conn) -> list[WorkItem]:
    """When no reactive work is pending, have agents take turns doing autonomous work.

    Only triggers if:
      - No pending/running reactive tasks exist
      - No initiative task is already pending or running
      - some agent in the rotation is DUE: it has never had an initiative
        task, or its last one was filed at least
        config.INITIATIVE_INTERVAL_DAYS days ago.
    Of the agents that are due, the one whose last initiative is oldest goes
    (never-run agents first, in rotation order). So an agent's initiative
    work, which the handlers call weekly, runs at most once a week, and an
    idle coordinator files one initiative per tick only until every agent has
    had its turn. Without the "due" rule every idle tick would file one, and
    each agent's turn would come round every nine idle ticks.

    The interval is counted from when a task was filed, in dry run as well:
    a dry-run turn is a turn.
    """
    try:
        cur = conn.cursor()
        try:
            # Check for existing pending/running non-initiative work
            # Only count recent tasks (< 1 hour) to avoid stale pending tasks
            # blocking initiative work indefinitely
            cur.execute(
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE task_category <> 'agent_initiative' "
                "AND status IN ('pending', 'running') "
                "AND created_at > NOW() - INTERVAL '1 hour'"
            )
            reactive_pending = cur.fetchone()[0]
            if reactive_pending > 0:
                return []  # Reactive work takes priority

            # Check for running initiative tasks
            cur.execute(
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE task_category = 'agent_initiative' AND status IN ('pending', 'running')"
            )
            running_initiative = cur.fetchone()[0]
            if running_initiative > 0:
                return []  # Already have an initiative running

            # When each agent last had an initiative filed, and whether that
            # is long enough ago (judged by the database's own clock).
            cur.execute(
                "SELECT agent, MAX(created_at) AS last_initiative, "
                "       MAX(created_at) <= NOW() - make_interval(days => %s) AS due "
                "FROM agent_tasks "
                "WHERE task_category = 'agent_initiative' "
                "GROUP BY agent",
                (config.INITIATIVE_INTERVAL_DAYS,),
            )
            last_times = {row[0]: (row[1], bool(row[2])) for row in cur.fetchall()}
        finally:
            cur.close()

        # Pick the agent that is due and has waited longest: one that never
        # had an initiative first, else the oldest last initiative.
        next_agent = None
        oldest_time = None
        for agent in _AGENT_ROTATION:
            if agent not in last_times:
                next_agent = agent
                break
            last, due = last_times[agent]
            if not due:
                continue
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if oldest_time is None or last < oldest_time:
                oldest_time = last
                next_agent = agent

        if not next_agent:
            return []  # every agent had its turn inside the interval

        # Load roadmap for initiative descriptions
        initiatives = {}
        if _ROADMAP_PATH.exists():
            try:
                roadmap = json.loads(_ROADMAP_PATH.read_text())
                initiatives = roadmap.get("agent_initiatives", {})
            except (OSError, ValueError, AttributeError) as exc:
                logger.warning("agent_initiative: %s could not be read (%s); using the default description",
                               _ROADMAP_PATH.name, exc)

        initiative_desc = initiatives.get(next_agent, f"Autonomous initiative work for {next_agent}")

        return [_make_item(
            "agent_initiative", next_agent,
            f"Initiative: {next_agent} autonomous work",
            {
                "agent": next_agent,
                "initiative": initiative_desc,
                "rotation_position": _AGENT_ROTATION.index(next_agent),
            },
            "AGENT_INITIATIVE",
        )]

    except Exception as exc:
        logger.warning("agent_initiative check failed: %s", exc)
    return []


# --- Zombie deploy reconciliation sweep ---

def _check_reconciliation_sweep(conn, now: datetime, last_tick_time) -> list[WorkItem]:
    """Periodic trigger: scan failed builds for zombie deploys every N hours.

    The sweep is disabled by default. Set AGENT_RECONCILIATION_ENABLED=true on
    the coordinator worker to activate it.  The interval (hours) is configurable
    via COORDINATOR_RECONCILIATION_INTERVAL_H (default 2).
    """
    if not arming_switch("AGENT_RECONCILIATION_ENABLED"):
        return []

    interval_h = config.RECONCILIATION_INTERVAL_H

    # Gate on the last time a sweep ACTUALLY ran, not on last_tick_time. The
    # latter is the previous tick's watermark (about one interval ago), so
    # hours_since would always be well under interval_h and the sweep would
    # never fire after the first tick. Query the most recent
    # reconciliation_sweep task.
    last_sweep = _last_reconciliation_sweep_time(conn)
    if last_sweep is not None:
        hours_since = (_as_utc(now) - _as_utc(last_sweep)).total_seconds() / 3600
        if hours_since < interval_h:
            return []

    return [_make_item(
        "reconciliation_sweep", "reconciliation",
        "Zombie deploy reconciliation sweep",
        {"timestamp": _as_utc(now).strftime("%Y-%m-%dT%H:%M")},
        "RECONCILIATION_SWEEP",
    )]


def _last_reconciliation_sweep_time(conn):
    """Most recent reconciliation_sweep task creation time, or None if there
    has been none. Raises when the query fails: "when did a sweep last run"
    being unknown must not read as "never", which would file a sweep on
    every tick for as long as the query keeps failing. (_run_check turns the
    exception into "no work from this trigger" and rolls back.)"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(created_at) FROM agent_tasks "
            "WHERE task_category = 'reconciliation_sweep'"
        )
        row = cur.fetchone()
        return row[0] if row else None
