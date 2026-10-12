#!/usr/bin/env python3
"""Poll ops API until the CAS worker has no active builds.

Usage:
  python3 scripts/wait_for_worker.py           # poll until free (for pre-push hook)
  python3 scripts/wait_for_worker.py --once    # check once; exit 1 if busy (for CI/Claude hook)

Configuration (environment):
  CAS_PROD_URL     base URL of the deployed CAS web app (required); CAS_OPS_BASE_URL
                   overrides it when the ops API lives on a different host
  OPS_API_KEY      bearer key for /ops/api/v1/* (falls back to web/.env)

Exit codes:
  0 = worker is free (or only stuck/stale builds remain)
  1 = worker is busy (--once only), network error that should block the push,
      or CAS_PROD_URL / CAS_OPS_BASE_URL not set
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

_OPS_BASE = (os.environ.get("CAS_OPS_BASE_URL") or os.environ.get("CAS_PROD_URL") or "").rstrip("/")
OPS_URL = f"{_OPS_BASE}/ops/api/v1/builds" if _OPS_BASE else ""


def _get_ops_key() -> str:
    """Read OPS_API_KEY from environment, falling back to web/.env."""
    key = os.environ.get("OPS_API_KEY", "")
    if not key:
        env_path = os.path.join(os.path.dirname(__file__), "..", "web", ".env")
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("OPS_API_KEY=") and not line.startswith("#"):
                        key = line.split("=", 1)[1].strip().strip("'\"")
                        break
        except FileNotFoundError:
            pass
    return key


OPS_KEY = _get_ops_key()
ACTIVE_STATUSES = {"building", "testing", "deploying"}
POLL_INTERVAL = 30  # seconds between polls
MAX_WAIT_MINUTES = 60  # give up after this long

# If a build has been in an active status longer than this, consider it stuck
STALE_THRESHOLD_MINUTES = 45


def get_active_builds() -> list:
    req = urllib.request.Request(OPS_URL, headers={"Authorization": f"Bearer {OPS_KEY}"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310  # operator-configured ops API base URL from the environment; dev script
        data = json.load(resp)
    return [
        b for b in data.get("builds", [])
        if b.get("status") in ACTIVE_STATUSES
    ]


def is_stale(build: dict) -> bool:
    """Check if a build has been active longer than the stale threshold."""
    started = build.get("started_at")
    if not started:
        return False
    try:
        from datetime import datetime, timezone
        # Parse ISO timestamp (handles both Z and +00:00 suffixes)
        started_str = started.replace("Z", "+00:00")
        start_time = datetime.fromisoformat(started_str)
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds() / 60
        return elapsed > STALE_THRESHOLD_MINUTES
    except Exception:
        return False


def describe(builds: list) -> str:
    parts = []
    for b in builds:
        name = b.get("app_name", "?")
        status = b.get("status", "?")
        stale_tag = " [STALE]" if is_stale(b) else ""
        parts.append(f"{name} ({status}{stale_tag})")
    return ", ".join(parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                        help="Check once and exit 1 if busy instead of polling")
    args = parser.parse_args()

    if not OPS_URL:
        print("[worker-wait] ERROR: set CAS_PROD_URL (or CAS_OPS_BASE_URL) to the CAS web app base URL",
              file=sys.stderr)
        return 1

    deadline = time.time() + MAX_WAIT_MINUTES * 60
    first = True

    while True:
        # Retry network errors up to 3 times before blocking the push.
        # If we can't confirm the worker is free, assume it's busy; 
        # a false block costs 30s of waiting; a false allow costs $5-15 in wasted API spend.
        active = None
        for _net_attempt in range(3):
            try:
                active = get_active_builds()
                break
            except Exception as exc:
                if _net_attempt < 2:
                    print(f"[worker-check] Network error (attempt {_net_attempt+1}/3): {exc}. "
                          f"Retrying in 10s...", file=sys.stderr)
                    time.sleep(10)
                else:
                    print(f"[worker-check] Cannot reach ops API after 3 attempts: {exc}",
                          file=sys.stderr)
                    print("[worker-check] Push blocked (cannot confirm worker is free).",
                          file=sys.stderr)
                    return 1
        if active is None:
            # Should never happen (loop above always breaks or returns), but
            # if it does, block the push rather than crashing with AssertionError.
            print("[worker-check] Internal error: active builds list not set. Blocking push.",
                  file=sys.stderr)
            return 1

        # Filter out stale builds (stuck, will never finish)
        truly_active = [b for b in active if not is_stale(b)]

        if not truly_active:
            if active and not truly_active:
                stale_summary = describe(active)
                print(f"[worker-check] Only stale builds remain: {stale_summary}. Proceeding.")
            elif not first:
                print("[worker-check] Worker is free. Proceeding.")
            return 0

        summary = describe(truly_active)

        if args.once:
            print(f"[worker-check] Worker busy: {summary}", file=sys.stderr)
            return 1

        if time.time() > deadline:
            print(f"[worker-check] Timed out after {MAX_WAIT_MINUTES}m. Worker still busy: {summary}",
                  file=sys.stderr)
            print("[worker-check] Push aborted. Retry manually when builds complete.",
                  file=sys.stderr)
            return 1

        print(f"[worker-check] Worker busy: {summary}")
        print(f"[worker-check] Waiting {POLL_INTERVAL}s before retrying... (Ctrl-C to abort)")
        first = False
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
