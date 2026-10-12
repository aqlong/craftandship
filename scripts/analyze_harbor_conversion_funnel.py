#!/usr/bin/env python3
"""Periodic SQL analysis of the CAS Harbor keeper conversion funnel.

Computes drop-off patterns from `trial_keepers` + `companion_turns` and
writes summary deltas to `agent_learnings` so the executor / coordinator
can surface engagement thresholds without any Claude API calls.

Cadence: intended to run every 3 days via cron / launchd.

Metrics computed (over the last `--window-days` rolling window):
  - Conversion rate: handoff_build_id IS NOT NULL / total active trials
  - Converters: median turn count + median days from first turn to handoff
  - Non-converters:
      - median last turn count
      - email_captured rate
      - count + share that reached >= 8 turns
      - drop-off bucket histogram (1-3 / 4-7 / 8-15 / 16+ turns)

Each metric is written as one row in `agent_learnings`
(source_agent='qa_engineer', category='harbor_conversion_funnel'). Rows
include the analysis date so sequential runs accumulate trend lines
rather than collapsing into a single dedup hash.

Usage:
  python3 scripts/analyze_harbor_conversion_funnel.py
  python3 scripts/analyze_harbor_conversion_funnel.py --dry-run
  python3 scripts/analyze_harbor_conversion_funnel.py --window-days 14
  python3 scripts/analyze_harbor_conversion_funnel.py --db-url postgresql://...

Exit codes:
  0  success (may be no-op if no trials in window)
  1  DB connection error or SQL failure
  3  argument / config error
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from datetime import date, datetime, timezone
from statistics import median
from typing import Any

import psycopg2
import psycopg2.extras


_SOURCE_AGENT = "qa_engineer"
_CATEGORY = "harbor_conversion_funnel"

logger = logging.getLogger("analyze_harbor_conversion_funnel")


def _resolve_db_url(override: str | None) -> str | None:
    if override:
        return override
    return os.environ.get("PROD_DATABASE_URL") or os.environ.get("DATABASE_URL")


def fetch_trial_rows(conn, window_days: int) -> list[dict[str, Any]]:
    """Pull one row per trial keeper started in the rolling window.

    Each row carries: trial_id, started_at, handoff_build_id, handoff_at,
    email_captured, turn_count, first_turn_at.

    Window is anchored on `started_at` (not `last_seen_at`) so the same
    trial doesn't move in/out of the window across runs.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
              tk.trial_id,
              tk.started_at,
              tk.handoff_build_id,
              tk.handoff_at,
              tk.email_captured,
              COALESCE(t.turn_count, 0) AS turn_count,
              t.first_turn_at
            FROM trial_keepers tk
            LEFT JOIN (
              SELECT
                trial_id,
                COUNT(*) AS turn_count,
                MIN(created_at) AS first_turn_at
              FROM companion_turns
              WHERE trial_id IS NOT NULL
              GROUP BY trial_id
            ) t ON t.trial_id = tk.trial_id
            WHERE tk.started_at >= NOW() - (%s || ' days')::interval
            """,
            (str(window_days),),
        )
        return [dict(r) for r in cur.fetchall()]


def _bucket_for_turns(turns: int) -> str:
    if turns <= 0:
        return "0"
    if turns <= 3:
        return "1-3"
    if turns <= 7:
        return "4-7"
    if turns <= 15:
        return "8-15"
    return "16+"


def _pct(num: int, denom: int) -> int:
    if denom <= 0:
        return 0
    return round((num / denom) * 100)


def compute_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure function: roll rows up into the metrics dict written as learnings.

    Returned shape is stable for tests; do not change keys without updating
    callers + tests.
    """
    total = len(rows)
    converters = [r for r in rows if r.get("handoff_build_id") is not None]
    non_converters = [r for r in rows if r.get("handoff_build_id") is None]

    converter_turns = [int(r.get("turn_count") or 0) for r in converters]
    converter_days_to_handoff: list[float] = []
    for r in converters:
        first = r.get("first_turn_at")
        handoff = r.get("handoff_at")
        if first and handoff:
            delta = handoff - first
            converter_days_to_handoff.append(delta.total_seconds() / 86400.0)

    non_conv_turns = [int(r.get("turn_count") or 0) for r in non_converters]
    non_conv_email_capture = sum(
        1 for r in non_converters if (r.get("email_captured") or "").strip()
    )
    non_conv_reached_8 = sum(1 for t in non_conv_turns if t >= 8)

    bucket_counts: dict[str, int] = {}
    for t in non_conv_turns:
        b = _bucket_for_turns(t)
        bucket_counts[b] = bucket_counts.get(b, 0) + 1

    return {
        "total": total,
        "converted": len(converters),
        "non_converted": len(non_converters),
        "conversion_rate_pct": _pct(len(converters), total),
        "converter_median_turns": int(median(converter_turns)) if converter_turns else None,
        "converter_median_days_to_handoff": (
            round(median(converter_days_to_handoff), 2)
            if converter_days_to_handoff
            else None
        ),
        "non_converter_median_turns": (
            int(median(non_conv_turns)) if non_conv_turns else None
        ),
        "non_converter_email_capture_rate_pct": _pct(
            non_conv_email_capture, len(non_converters)
        ),
        "non_converter_reached_8_turns": non_conv_reached_8,
        "non_converter_reached_8_pct": _pct(
            non_conv_reached_8, len(non_converters)
        ),
        "non_converter_bucket_counts": bucket_counts,
    }


def render_learnings(metrics: dict[str, Any], window_days: int,
                     analysis_date: date) -> list[str]:
    """Render metrics into the human-readable learning strings stored in
    `agent_learnings.learning`. Each string is one row.

    The analysis_date is included so sequential runs across days produce
    distinct rows (the schema's UNIQUE(source_agent, category, learning_hash)
    would otherwise collapse identical text into one row).
    """
    if metrics["total"] == 0:
        return [
            f"[{analysis_date.isoformat()} | {window_days}d] No trial keepers "
            f"started in the window; nothing to learn from."
        ]

    prefix = f"[{analysis_date.isoformat()} | {window_days}d]"
    out: list[str] = []
    out.append(
        f"{prefix} Conversion rate: {metrics['conversion_rate_pct']}% "
        f"({metrics['converted']}/{metrics['total']} trials handed off)."
    )

    if metrics["converted"] > 0:
        med_turns = metrics["converter_median_turns"]
        med_days = metrics["converter_median_days_to_handoff"]
        days_part = (
            f", median {med_days} days from first turn to handoff"
            if med_days is not None
            else ""
        )
        out.append(
            f"{prefix} Converters reached handoff at median "
            f"{med_turns} turns{days_part}."
        )

    if metrics["non_converted"] > 0:
        out.append(
            f"{prefix} Non-converters: median "
            f"{metrics['non_converter_median_turns']} last turn count, "
            f"{metrics['non_converter_email_capture_rate_pct']}% captured email."
        )
        out.append(
            f"{prefix} Of non-converters, "
            f"{metrics['non_converter_reached_8_turns']} "
            f"({metrics['non_converter_reached_8_pct']}%) reached >=8 turns "
            f"but did not hand off; potential post-engagement drop-off cohort."
        )
        bucket_counts = metrics["non_converter_bucket_counts"]
        if bucket_counts:
            ordered = sorted(
                bucket_counts.items(),
                key=lambda kv: ["0", "1-3", "4-7", "8-15", "16+"].index(kv[0])
                if kv[0] in ["0", "1-3", "4-7", "8-15", "16+"]
                else 99,
            )
            histogram = ", ".join(f"{b}={c}" for b, c in ordered)
            out.append(
                f"{prefix} Non-converter turn-count histogram: {histogram}."
            )

    return out


def write_learnings(conn, learnings: list[str]) -> int:
    """INSERT each learning into agent_learnings, deduping on hash.

    Mirrors coordinator.tick._record_learning so behavior matches the
    rest of the learning system.
    Returns number of rows affected (insert or update).
    """
    inserted = 0
    with conn.cursor() as cur:
        for text in learnings:
            learning_hash = hashlib.sha256(
                f"{_SOURCE_AGENT}:{_CATEGORY}:{text}".encode()
            ).hexdigest()[:16]
            cur.execute(
                """
                INSERT INTO agent_learnings
                  (source_agent, category, learning, learning_hash)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (source_agent, category, learning_hash)
                DO UPDATE SET last_applied_at = NOW()
                """,
                (_SOURCE_AGENT, _CATEGORY, text, learning_hash),
            )
            inserted += 1
    return inserted


def run_analysis(conn, window_days: int, dry_run: bool,
                 analysis_date: date | None = None) -> dict[str, Any]:
    """Top-level orchestrator. Pulls rows, computes metrics, renders
    learnings, and (unless dry_run) writes them.

    Returns a summary dict suitable for logging and tests.
    """
    rows = fetch_trial_rows(conn, window_days)
    metrics = compute_metrics(rows)
    when = analysis_date or datetime.now(timezone.utc).date()
    learnings = render_learnings(metrics, window_days, when)

    written = 0
    if not dry_run and learnings:
        written = write_learnings(conn, learnings)
        conn.commit()

    return {
        "window_days": window_days,
        "analysis_date": when.isoformat(),
        "metrics": metrics,
        "learnings": learnings,
        "written": written,
        "dry_run": dry_run,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Analyze CAS Harbor keeper conversion funnel and "
                    "record drop-off patterns to agent_learnings.",
    )
    p.add_argument("--db-url", default=None,
                   help="Override DB URL (default: PROD_DATABASE_URL or DATABASE_URL).")
    p.add_argument("--window-days", type=int, default=30,
                   help="Rolling window in days. Default 30.")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute and print but do not write to agent_learnings.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Verbose logging.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.window_days <= 0:
        logger.error("--window-days must be positive (got %s)", args.window_days)
        return 3

    db_url = _resolve_db_url(args.db_url)
    if not db_url:
        logger.error(
            "No DB URL configured. Pass --db-url or set "
            "PROD_DATABASE_URL / DATABASE_URL."
        )
        return 3

    try:
        conn = psycopg2.connect(db_url)
    except psycopg2.Error as exc:
        logger.error("DB connection failed: %s", exc)
        return 1

    try:
        summary = run_analysis(conn, args.window_days, args.dry_run)
    except psycopg2.Error as exc:
        logger.error("Analysis SQL failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    logger.info(
        "Funnel analysis complete: total=%d converted=%d rate=%d%% "
        "learnings_written=%d dry_run=%s",
        summary["metrics"]["total"],
        summary["metrics"]["converted"],
        summary["metrics"]["conversion_rate_pct"],
        summary["written"],
        summary["dry_run"],
    )
    for line in summary["learnings"]:
        logger.info("  - %s", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
