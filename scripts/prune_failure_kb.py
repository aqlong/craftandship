#!/usr/bin/env python3
"""TTL pruner for cli/failure_kb.json.

The failure KB grows monotonically: every fix attempt adds an entry,
and nothing prunes them. Audit on 2026-05-04 found entries last seen
>300 days ago with `count == 0`, polluting fuzzy-match space and
inflating prompt-rule emission cost.

Drop policy (any one of these triggers prune):
  1. `count == 0` (success + failure both zero; the entry was created
     but never produced a recorded outcome; a stale stub).
  2. `last_seen` older than `--stale-days` (default 60d) AND
     `failure_count <= --max-failures` (default 2).

Always preserves entries with `seeded=True` (human-curated rules in
`get_build_prompt_rules()` bypass the threshold and are load-bearing
for the build prompt).

Usage:
    python3 scripts/prune_failure_kb.py            # apply prune in-place
    python3 scripts/prune_failure_kb.py --dry-run  # report only
    python3 scripts/prune_failure_kb.py --stale-days 30 --max-failures 1

Wire into a weekly cron / coordinator daily job; cheap (no API calls,
no network, ~0.5s on a 200-entry KB).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli.failure_kb import _get_kb_path, _load_kb, _save_kb  # noqa: E402

DEFAULT_STALE_DAYS = 60
DEFAULT_MAX_FAILURES = 2


def is_stale(entry: dict, *, now: float, stale_seconds: float,
             max_failures: int) -> bool:
    """Decide whether `entry` should be pruned.

    Returns True if the entry is a candidate for removal. Seeded
    (human-curated) entries are always preserved.
    """
    if entry.get("seeded"):
        return False
    success = int(entry.get("success_count", 0) or 0)
    failure = int(entry.get("failure_count", 0) or 0)
    total = success + failure
    if total == 0:
        return True
    last_seen = float(entry.get("last_seen", 0) or 0)
    age_seconds = now - last_seen
    if age_seconds > stale_seconds and failure <= max_failures:
        return True
    return False


def prune_kb(kb: dict, *, now: float, stale_days: int,
             max_failures: int) -> tuple[dict, list[str]]:
    """Return (pruned_kb, dropped_signatures)."""
    stale_seconds = stale_days * 86400.0
    dropped: list[str] = []
    kept: dict = {}
    for sig, entry in kb.items():
        if is_stale(entry, now=now, stale_seconds=stale_seconds,
                    max_failures=max_failures):
            dropped.append(sig)
        else:
            kept[sig] = entry
    return kept, dropped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stale-days", type=int, default=DEFAULT_STALE_DAYS,
        help="Drop entries last seen more than N days ago "
             "(default %(default)s)",
    )
    parser.add_argument(
        "--max-failures", type=int, default=DEFAULT_MAX_FAILURES,
        help="Only drop stale entries with failure_count <= N "
             "(default %(default)s); higher counts represent recurring "
             "patterns worth keeping",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be pruned without writing",
    )
    args = parser.parse_args()

    kb_path = _get_kb_path()
    kb = _load_kb()
    before = len(kb)
    if before == 0:
        print(f"failure_kb empty at {kb_path}; nothing to prune")
        return 0

    pruned, dropped = prune_kb(
        kb,
        now=time.time(),
        stale_days=args.stale_days,
        max_failures=args.max_failures,
    )
    after = len(pruned)

    print(f"failure_kb path: {kb_path}")
    print(f"before: {before}, after: {after}, dropped: {len(dropped)}")
    if dropped:
        print(f"sample drops (first 5):")
        for sig in dropped[:5]:
            entry = kb[sig]
            print(f"  - succ={entry.get('success_count', 0)} "
                  f"fail={entry.get('failure_count', 0)} "
                  f"last_seen={entry.get('last_seen', 0)} "
                  f"{sig[:80]}")

    if args.dry_run:
        print("dry-run: not writing")
        return 0

    if dropped:
        _save_kb(pruned)
        print(f"wrote {after} entries")
    else:
        print("no changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
