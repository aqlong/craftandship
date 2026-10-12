#!/usr/bin/env python3
"""One-shot scrub of secrets from the live failure_kb.json + review_kb.json.

Walks the KB on the prod /builds volume (or wherever FAILURE_KB_PATH /
REVIEW_KB_PATH point), applies cli.failure_kb._redact_secrets() across
every entry, and rewrites the file atomically. Idempotent: re-running
on already-clean files is a no-op.

The save-time redaction in cli/failure_kb.py prevents NEW writes from
persisting secrets, but historical entries already on the volume need
this one-shot scrub. Today's KB is clean (audited 2026-05-09); this
script is the safety net for the next time a build accidentally emits a
key in stderr.

Usage:
    python3 scripts/prune_failure_kb_secrets.py            # apply in-place
    python3 scripts/prune_failure_kb_secrets.py --dry-run  # report only

Env:
    FAILURE_KB_PATH=/builds/failure_kb.json (override)
    REVIEW_KB_PATH=/builds/review_kb.json (override)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli.failure_kb import (  # noqa: E402
    _atomic_write_json,
    _get_kb_path,
    _get_review_kb_path,
    _redact_secrets,
)


def _scrub_path(path: Path, dry_run: bool) -> tuple[int, int]:
    """Return (entries_total, entries_with_redactions_applied).

    A file that does not exist is treated as a no-op (0, 0).
    """
    if not path.exists():
        print(f"  {path}: missing; nothing to scrub")
        return 0, 0
    try:
        kb = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  {path}: unreadable ({exc}); skipping")
        return 0, 0
    if not isinstance(kb, dict):
        print(f"  {path}: unexpected top-level shape; skipping")
        return 0, 0
    redacted = _redact_secrets(kb)
    changed = 0
    for sig, entry in kb.items():
        if json.dumps(entry, sort_keys=True) != json.dumps(
            redacted.get(sig), sort_keys=True
        ):
            changed += 1
    total = len(kb)
    if changed == 0:
        print(f"  {path}: clean ({total} entries, 0 redacted)")
        return total, 0
    print(f"  {path}: {changed}/{total} entries contain secret-shaped tokens")
    if dry_run:
        print(f"  {path}: dry-run, not writing")
        return total, changed
    _atomic_write_json(path, redacted)
    print(f"  {path}: wrote scrubbed copy")
    return total, changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be redacted without writing",
    )
    args = parser.parse_args()

    print("scrubbing secrets from failure_kb + review_kb")
    failure_total, failure_changed = _scrub_path(_get_kb_path(), args.dry_run)
    review_total, review_changed = _scrub_path(
        _get_review_kb_path(), args.dry_run,
    )
    print(
        f"summary: failure_kb {failure_changed}/{failure_total} entries scrubbed; "
        f"review_kb {review_changed}/{review_total} entries scrubbed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
