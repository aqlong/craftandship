#!/usr/bin/env python3
"""Classify a pending push as fast-path or staging-path.

The Craft & Ship repo used two push routes:

  FAST-PATH (CAS_PROMOTE=1 git push origin master)
    Safe for docs, scripts, tests, CI workflows, .githooks, and the
    roadmap churn file. Bypasses the staging gate but
    still runs npm test via the pre-push hook.

  STAGING-PATH (push staging branch -> verify -> promote_staging.sh)
    Required for real code, rendered UI, generated-app templates, DB
    schema, pipeline modules, worker, coordinator (non-roadmap), monitor,
    ops, Dockerfiles, railway.toml, and any package.json change.

This classifier reads the set of files that would be pushed (commits in
origin/master..HEAD plus any staged/unstaged changes) and decides which
path is appropriate. It prints a JSON verdict to stdout.

Usage:
    # Classify what's currently ahead of origin/master
    python3 scripts/classify_push.py

    # Classify an explicit file list (from stdin, one per line)
    git diff --name-only origin/master..HEAD | python3 scripts/classify_push.py --stdin

    # Just return "fast" or "staging" as exit code convention
    python3 scripts/classify_push.py --terse

Output shape:
    {
      "verdict": "fast" | "staging" | "empty",
      "reason": "<human explanation>",
      "commits_ahead": <int>,
      "fast_safe": [<files>],
      "needs_staging": [<files>],
      "uncommitted": [<files>],
      "untracked": [<files>]
    }
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------
#
# A file is STAGING-required if ANY of its matchers hit. Prefix matching.
# Order matters: more specific rules before broader ones when relevant.

STAGING_PREFIXES: tuple[str, ...] = (
    # Real web app code + rendered UI + static assets
    "web/src/",
    "web/views/",
    "web/public/",
    # Dependency / build config that ships to Railway
    "web/package.json",
    "web/package-lock.json",
    "web/tsconfig.json",
    "web/vitest.config.ts",
    "web/playwright.config.ts",
    "web/railway.toml",
    # Pipeline modules (affect customer builds)
    "cli/build.py",
    "cli/deploy.py",
    "cli/deploy_fix.py",
    "cli/fix.py",
    "cli/review.py",
    "cli/design_review.py",
    "cli/design_fix.py",
    "cli/qa.py",
    "cli/qa_fix.py",
    "cli/ui_test.py",
    "cli/test.py",
    "cli/intake.py",
    "cli/validate.py",
    "cli/api_client.py",
    "cli/failure_kb.py",
    "cli/design_data.py",
    "cli/build_trace.py",
    # Generated-app templates (shipped into every customer build)
    "cli/templates/",
    # Stock photos (shipped into customer builds)
    "cli/stock_photos/",
    # Worker / coordinator / monitor / ops modules
    "queue/",
    "monitor/",
    "ops/",
    # Infrastructure
    "Dockerfile",
    "Dockerfile.worker",
    "railway.toml",
    "requirements.worker.txt",
    "requirements.txt",
    "package.json",
    "package-lock.json",
)

# coordinator/ is STAGING except roadmap.json (churn)
COORDINATOR_STAGING_EXCEPTIONS: tuple[str, ...] = (
    "coordinator/roadmap.json",
)

# A file is FAST-SAFE if it matches any of these prefixes AND is not
# claimed by STAGING_PREFIXES above. These are reviewed explicitly so
# unknown paths default to STAGING (safer).
FAST_SAFE_PREFIXES: tuple[str, ...] = (
    "docs/",
    "scripts/",
    "tests/",           # Python tests don't ship to prod
    "web/tests/",       # Vitest/Playwright tests don't ship to prod
    ".github/",         # CI workflows
    ".githooks/",       # Local hooks (CI hook-integrity catches drift)
    "memory/",          # Rare: memory files inside repo
    ".gitignore",
    ".editorconfig",
    "CLAUDE.md",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "version.json",
    # Churn files: auto-updated by the worker, executor, or pipeline.
    # Safe to ship on the fast-path; they never affect live code behavior.
    "coordinator/roadmap.json",
    "cli/failure_kb.json",
    "scripts/cas-executor-config.json",
    # Top-level docs / planning scratch
    "docs/plans/",
)


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(
            cmd,
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return ""


def _is_staging_path(path: str) -> bool:
    # coordinator/ is staging except the roadmap churn file
    if path.startswith("coordinator/"):
        return path not in COORDINATOR_STAGING_EXCEPTIONS
    for prefix in STAGING_PREFIXES:
        if path == prefix or path.startswith(prefix):
            return True
    return False


def _is_fast_safe_path(path: str) -> bool:
    for prefix in FAST_SAFE_PREFIXES:
        if path == prefix or path.startswith(prefix):
            return True
    return False


def classify_files(files: list[str]) -> tuple[str, list[str], list[str], list[str]]:
    """Return (verdict, fast_safe, needs_staging, unknown)."""
    fast_safe: list[str] = []
    needs_staging: list[str] = []
    unknown: list[str] = []

    for f in files:
        if not f:
            continue
        if _is_staging_path(f):
            needs_staging.append(f)
        elif _is_fast_safe_path(f):
            fast_safe.append(f)
        else:
            # Unknown paths are treated as STAGING by default (fail-safe).
            unknown.append(f)

    if not files:
        return "empty", [], [], []
    if needs_staging or unknown:
        return "staging", fast_safe, needs_staging, unknown
    return "fast", fast_safe, [], []


def get_commit_files() -> tuple[list[str], int]:
    """Files in origin/master..HEAD. Returns (files, commit_count)."""
    _run(["git", "fetch", "origin", "master"])
    count_str = _run(["git", "rev-list", "--count", "origin/master..HEAD"])
    try:
        commit_count = int(count_str) if count_str else 0
    except ValueError:
        commit_count = 0
    if commit_count == 0:
        return [], 0
    raw = _run(["git", "diff", "--name-only", "origin/master..HEAD"])
    return [line for line in raw.splitlines() if line], commit_count


def get_uncommitted_files() -> tuple[list[str], list[str]]:
    """Return (modified, untracked).

    Calls subprocess directly instead of _run() because _run().strip() would
    remove the leading space on the first porcelain line (e.g. " M foo.py"
    starts with a space for unstaged-modified), shifting column positions.
    """
    try:
        raw = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except subprocess.CalledProcessError:
        return [], []
    modified: list[str] = []
    untracked: list[str] = []
    for line in raw.splitlines():
        if not line or len(line) < 4:
            continue
        code = line[:2]
        path = line[3:]
        # Handle renamed files: "R  old -> new"
        if "->" in path:
            path = path.split("->", 1)[1].strip()
        if code.startswith("??"):
            untracked.append(path)
        else:
            modified.append(path)
    return modified, untracked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read file list from stdin instead of computing from git state",
    )
    parser.add_argument(
        "--terse",
        action="store_true",
        help="Print only the verdict (fast|staging|empty) to stdout",
    )
    parser.add_argument(
        "--include-uncommitted",
        action="store_true",
        help="Include staged/unstaged files when computing the verdict",
    )
    args = parser.parse_args()

    if args.stdin:
        files = [line.strip() for line in sys.stdin if line.strip()]
        commit_count = 0
        uncommitted: list[str] = []
        untracked: list[str] = []
    else:
        files, commit_count = get_commit_files()
        uncommitted, untracked = get_uncommitted_files()
        if args.include_uncommitted:
            files = files + uncommitted

    verdict, fast_safe, needs_staging, unknown = classify_files(files)

    # Build a human-friendly reason.
    if verdict == "empty":
        reason = "Nothing to push: branch is even with origin/master."
    elif verdict == "fast":
        reason = (
            f"All {len(fast_safe)} changed files are docs/scripts/tests/CI/hooks "
            f"(fast-path safe)."
        )
    else:
        bits = []
        if needs_staging:
            bits.append(f"{len(needs_staging)} file(s) require staging verification")
        if unknown:
            bits.append(
                f"{len(unknown)} unclassified path(s): defaulting to staging (fail-safe)"
            )
        reason = "; ".join(bits) + "."

    result = {
        "verdict": verdict,
        "reason": reason,
        "commits_ahead": commit_count,
        "fast_safe": fast_safe,
        "needs_staging": needs_staging + unknown,
        "uncommitted": uncommitted,
        "untracked": untracked,
    }

    if args.terse:
        print(verdict)
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
