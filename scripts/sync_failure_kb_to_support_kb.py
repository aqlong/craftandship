#!/usr/bin/env python3
"""Weekly job: sync top failure_kb patterns into support_kb.

Loads the top-N entries from cli/failure_kb.json (ranked by total
occurrence count), asks Haiku to write a plain-English Q&A row for
each, and inserts into support_kb via the coordinator actuator
(WHERE NOT EXISTS on normalized question, so re-runs are idempotent).

Usage:
    python3 scripts/sync_failure_kb_to_support_kb.py \\
        [--top-n 10] [--min-count 2] [--model claude-haiku-4-5] \\
        [--dry-run]

Required env:
    DATABASE_URL        Postgres URL for the support_kb table
    ANTHROPIC_API_KEY   to draft the Q&A pairs

Run weekly as a cron job (LaunchAgent locally, or a Railway scheduled
job in prod). Idempotent: dedupe in support_kb_insert means re-runs
won't pile up duplicate rows.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coordinator.actuators import Actuators  # noqa: E402
from coordinator.budget import ModelCallMeter, TickBudget  # noqa: E402
from coordinator.scorer import estimate_task_cost  # noqa: E402
from coordinator.failure_kb_sync import (  # noqa: E402
    DEFAULT_TOP_N,
    MIN_TOTAL_COUNT,
    sync_failure_kb_to_support_kb,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help="Top-N failure-KB entries to sync (default %(default)s)")
    parser.add_argument("--min-count", type=int, default=MIN_TOTAL_COUNT,
                        help="Minimum total occurrence count to qualify "
                             "(default %(default)s)")
    parser.add_argument("--model", default=os.environ.get(
        "FAILURE_KB_SYNC_MODEL", "claude-haiku-4-5"),
        help="Anthropic model id for drafting (default %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Make no model call and no INSERT; report how many "
                             "patterns would be drafted (skipped_not_authorized)")
    parser.add_argument("--task-id", default="failure-kb-sync",
                        help="Task id stamped on actuator log entries")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable INFO-level logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # A run of this script is not part of a coordinator tick, so it brings its
    # own allowance: one model call per entry it may draft, at the per-call
    # estimate, for the model named on the command line and no other.
    allowance = ModelCallMeter(TickBudget(
        max_api_calls=max(0, args.top_n),
        max_cost_usd=max(0, args.top_n) * estimate_task_cost("", args.model),
        max_opus_calls=max(0, args.top_n),
        allowed_models=[args.model],
    ))
    actuators = Actuators(
        agent_name="infra_engineer",
        task_id=args.task_id,
        dry_run=args.dry_run,
        model_calls=allowance,
    )

    summary = sync_failure_kb_to_support_kb(
        actuators=actuators,
        model=args.model,
        top_n=args.top_n,
        min_count=args.min_count,
    )

    print(json.dumps(summary, indent=2, sort_keys=True))

    # Non-zero exit only when the actuator gate refused everything;
    # zero-considered (empty KB) and partial drafts are normal weekly
    # outcomes that shouldn't page anyone.
    if summary["considered"] > 0 and summary["drafted"] == 0 \
            and summary["skipped_no_draft"] == summary["considered"]:
        # All drafts failed: probably ANTHROPIC_API_KEY missing or
        # network down. Worth surfacing as a non-zero exit.
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
