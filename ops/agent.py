# ops/agent.py - Main ops agent loop
#
# Periodically scans the builds table for repeated failure patterns,
# matches them against known playbooks, checks guardrails, and applies
# fixes automatically. Sends alerts when guardrails trigger or when
# errors cannot be auto-fixed.
#
# This loop does not go through coordinator/actuators.py: it runs when the
# coordinator is off and has no permission table. What bounds it is its own
# set of limits, all checked in run_scan before a fixer is called (OPS_DRY_RUN,
# on by default; SeverityFilter; CircuitBreaker; CostGuard), and the limits
# inside ops/fixer.py that hold for every caller: a playbook can redeploy
# only the platform's own worker service and set only three named variables
# on it. It cannot commit, queue a build, touch a customer's service or
# write a review verdict.

import logging
import os
import sys
import time
from pathlib import Path

# Allow importing from parent directories
_OPS_DIR = str(Path(__file__).parent)
_ROOT_DIR = str(Path(__file__).parent.parent)
if _OPS_DIR not in sys.path:
    sys.path.insert(0, _OPS_DIR)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from coordinator.config import env_int
from coordinator.envflags import safety_switch
from ops.audit import log_action
from ops.guardrails import CircuitBreaker, CostGuard, SeverityFilter
from ops.scanner import scan_recent_failures
from ops.playbook import match_playbook, load_fixer
from ops.alerter import (
    alert_fix_applied,
    alert_circuit_breaker,
    alert_unfixable,
    alert_scan_summary,
    alert_hourly_digest,
)

logger = logging.getLogger("ops.agent")

_DEFAULT_SCAN_INTERVAL = 1800  # 30 minutes
_DEFAULT_DIGEST_INTERVAL = 3600  # 1 hour
_shutdown_requested = False


def _get_conn():
    """Get a psycopg2 connection using DATABASE_URL."""
    import psycopg2
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(db_url)


def run_scan(conn, *, dry_run: bool = False) -> dict:
    """Run a single scan cycle.

    Steps:
    1. Query builds for failure patterns
    2. For each pattern, classify, check guardrails, match playbook, apply fix
    3. Log all actions to audit
    4. Alert admin as appropriate

    Returns:
        Summary dict: {patterns_found, fixes_applied, fixes_skipped, escalations}
    """
    circuit_breaker = CircuitBreaker()
    cost_guard = CostGuard()
    severity_filter = SeverityFilter()

    summary = {
        "patterns_found": 0,
        "fixes_applied": 0,
        "fixes_skipped": 0,
        "escalations": 0,
    }
    scan_details: list = []  # Collected for summary email

    # Log the scan itself
    log_action("scan", "Ops agent scan started", dry_run=dry_run)

    # Step 1: Find failure patterns
    try:
        patterns = scan_recent_failures(conn)
    except Exception as exc:
        logger.error("Failed to scan for failures: %s", exc)
        log_action("scan", f"Scan failed: {exc}", success=False)
        return summary

    summary["patterns_found"] = len(patterns)

    if not patterns:
        logger.info("No recurring failure patterns found")
        return summary

    # Step 2: Process each pattern
    for pattern_info in patterns:
        category = pattern_info["category"]
        sample_error = pattern_info["sample_error"]
        build_ids = pattern_info["build_ids"]
        count = pattern_info["count"]

        logger.info(
            "Pattern: %s (%d occurrences, category=%s)",
            pattern_info["pattern"][:80], count, category,
        )

        # 2a: Severity filter
        if not severity_filter.is_auto_fixable(category):
            logger.info("Category '%s' is not auto-fixable; alerting admin", category)
            log_action(
                "fix_skipped",
                f"Category '{category}' not auto-fixable",
                category=category,
                build_ids=build_ids,
                dry_run=dry_run,
            )
            alert_unfixable(category, sample_error, build_ids, reason="Category not auto-fixable")
            scan_details.append({
                "category": category,
                "description": sample_error[:100],
                "action": "escalated",
                "build_count": count,
            })
            summary["escalations"] += 1
            continue

        # 2b: Circuit breaker
        if not circuit_breaker.can_proceed():
            logger.warning("Circuit breaker active; skipping fix for %s", category)
            log_action(
                "circuit_breaker",
                f"Circuit breaker blocked fix for {category}",
                category=category,
                build_ids=build_ids,
                dry_run=dry_run,
            )
            alert_circuit_breaker(
                circuit_breaker.limit - circuit_breaker.remaining(),
                circuit_breaker.limit,
                circuit_breaker.window_minutes,
            )
            scan_details.append({
                "category": category,
                "description": "Circuit breaker active",
                "action": "skipped",
                "build_count": count,
            })
            summary["fixes_skipped"] += 1
            continue

        # 2c: Cost guard (daily cap on fixer runs)
        if not cost_guard.can_call_claude():
            logger.warning("Cost guard: daily fixer-run cap reached; skipping fix for %s", category)
            log_action(
                "fix_skipped",
                f"Cost guard blocked fix for {category} (daily fixer-run cap reached)",
                category=category,
                build_ids=build_ids,
                dry_run=dry_run,
            )
            scan_details.append({
                "category": category,
                "description": "Daily fixer-run cap reached",
                "action": "skipped",
                "build_count": count,
            })
            summary["fixes_skipped"] += 1
            continue

        # 2d: Match playbook
        playbook_entry = match_playbook(category)
        if playbook_entry is None:
            logger.info("No playbook for category '%s'; alerting admin", category)
            log_action(
                "fix_skipped",
                f"No playbook for '{category}'",
                category=category,
                build_ids=build_ids,
                dry_run=dry_run,
            )
            alert_unfixable(category, sample_error, build_ids)
            scan_details.append({
                "category": category,
                "description": "No playbook available",
                "action": "escalated",
                "build_count": count,
            })
            summary["escalations"] += 1
            continue

        # 2d: Load and run the fixer
        fixer_fn = load_fixer(playbook_entry)
        if fixer_fn is None:
            logger.error("Failed to load fixer for '%s'", category)
            log_action(
                "fix_skipped",
                f"Fixer module failed to load for '{category}'",
                category=category,
                build_ids=build_ids,
                success=False,
            )
            scan_details.append({
                "category": category,
                "description": "Fixer module failed to load",
                "action": "skipped",
                "build_count": count,
            })
            summary["fixes_skipped"] += 1
            continue

        try:
            # Record the fixer run: the cost guard caps these per day. The row
            # type is historical ("claude_call"); playbooks make no model calls.
            log_action("claude_call", f"Fixer invoked for {category}", category=category,
                       build_ids=build_ids, dry_run=dry_run)

            context = {
                "error_message": sample_error,
                "build_ids": build_ids,
                "dry_run": dry_run,
            }
            result = fixer_fn(context)
        except Exception as exc:
            logger.error("Fixer for '%s' raised: %s", category, exc)
            log_action(
                "fix_applied",
                f"Fixer failed: {exc}",
                category=category,
                build_ids=build_ids,
                success=False,
            )
            scan_details.append({
                "category": category,
                "description": f"Fixer error: {str(exc)[:80]}",
                "action": "skipped",
                "build_count": count,
            })
            summary["fixes_skipped"] += 1
            continue

        if result.success:
            logger.info("Fix applied: %s", result.description)
            log_action(
                "fix_applied",
                result.description,
                category=category,
                build_ids=build_ids,
                dry_run=dry_run,
            )
            alert_fix_applied(category, result.description, dry_run=dry_run)
            scan_details.append({
                "category": category,
                "description": result.description,
                "action": "would_fix" if dry_run else "fixed",
                "build_count": count,
            })
            summary["fixes_applied"] += 1
        else:
            logger.info("Fix not applicable: %s", result.description)
            log_action(
                "fix_skipped",
                result.description,
                category=category,
                build_ids=build_ids,
                dry_run=dry_run,
            )
            # If the playbook matched but the fix itself failed, alert admin
            alert_unfixable(category, sample_error, build_ids, reason=result.description)
            scan_details.append({
                "category": category,
                "description": result.description,
                "action": "escalated",
                "build_count": count,
            })
            summary["escalations"] += 1

    # Send scan summary email if anything was found
    if scan_details:
        try:
            alert_scan_summary(summary, scan_details, dry_run=dry_run)
        except Exception as exc:
            logger.error("Failed to send scan summary email: %s", exc)

    return summary


def run_agent(
    interval_seconds: int = _DEFAULT_SCAN_INTERVAL,
    dry_run: bool = True,
) -> None:
    """Continuous agent loop. Runs as a daemon thread or standalone.

    Args:
        interval_seconds: Seconds between scan cycles (default 30 min).
        dry_run: If True, log what would happen but do not apply fixes.
    """
    global _shutdown_requested

    mode = "DRY RUN" if dry_run else "LIVE"
    digest_interval = env_int("OPS_DIGEST_INTERVAL", _DEFAULT_DIGEST_INTERVAL, min_val=1)
    logger.info("Ops agent starting (%s mode, scan=%ds, digest=%ds)", mode, interval_seconds, digest_interval)

    last_digest = 0  # epoch timestamp of last digest email

    while not _shutdown_requested:
        conn = None
        try:
            conn = _get_conn()
            summary = run_scan(conn, dry_run=dry_run)
            logger.info(
                "Scan complete: %d patterns, %d fixed, %d skipped, %d escalated",
                summary["patterns_found"],
                summary["fixes_applied"],
                summary["fixes_skipped"],
                summary["escalations"],
            )

            # Send hourly digest if enough time has passed
            now = time.time()
            if now - last_digest >= digest_interval:
                try:
                    alert_hourly_digest(conn)
                    last_digest = now
                except Exception as digest_exc:
                    logger.error("Hourly digest failed: %s", digest_exc)

        except Exception as exc:
            logger.error("Ops agent scan failed: %s", exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as close_exc:
                    logger.debug("closing the scan connection failed: %s", close_exc)

        # Sleep in short intervals to respond to shutdown quickly
        for _ in range(int(interval_seconds)):
            if _shutdown_requested:
                break
            time.sleep(1)

    logger.info("Ops agent stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    interval = env_int("OPS_SCAN_INTERVAL", _DEFAULT_SCAN_INTERVAL, min_val=1)
    dry_run = safety_switch("OPS_DRY_RUN")
    run_agent(interval_seconds=interval, dry_run=dry_run)
