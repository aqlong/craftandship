#!/usr/bin/env python3
"""Regenerate tests/golden/<SPEC>/artifact.snapshot.json deterministically.

Usage: python3 scripts/bless_golden.py <SPEC_NAME>

Canonical workflow: `make bless-golden SPEC=<SPEC_NAME>`.

What the script does:
  1. Refuses to run if the working tree is dirty (git status --porcelain
     non-empty). Prevents blessing WIP. Bootstrap escape hatch:
     BLESS_GOLDEN_ALLOW_DIRTY=1 in the environment suppresses the check,
     intended only for the first-ever bless where this script is itself
     the uncommitted change. Prints a loud warning in that path.
  2. Loads tests/golden/<SPEC>/spec.json.
  3. Builds an in-memory artifact and runs cli.pipeline.run_pipeline on the
     'build' phase with every processor EXCEPT _run_contracts (the contract
     gate expects full Claude-produced input; the golden snapshot is about
     pinning deterministic processor output, not asserting contract pass).
  4. For each file in artifact.files, computes sha256 (utf-8 encoded for
     strings, raw bytes otherwise) and writes a {filename: sha256} map to
     tests/golden/<SPEC>/artifact.snapshot.json with sort_keys=True,
     indent=2, and a trailing newline so diff tools behave.
  5. Compares against the previous snapshot (if any) and prints a summary
     line `unchanged=N changed=N added=N removed=N` to stdout.

Determinism guardrails:
  - sorted keys in both the written JSON and the stdout summary,
  - no timestamp in artifact.files (widgets metadata timestamps live on
    artifact.metadata, not files),
  - subset excludes _run_contracts so the run never raises on incomplete
    input; any other nondeterminism is a bug in the pipeline and must be
    fixed upstream.

See docs/internals/WIDGETS_AND_CONTRACTS.md for when to re-bless.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _check_dirty_tree() -> int:
    """Return 0 if clean, 1 if dirty (with a user-readable error).

    BLESS_GOLDEN_ALLOW_DIRTY=1 bypasses the check with a loud warning.
    """
    if os.environ.get("BLESS_GOLDEN_ALLOW_DIRTY") == "1":
        print(
            "WARNING: BLESS_GOLDEN_ALLOW_DIRTY=1 set. Skipping clean-tree "
            "check. Use ONLY for bootstrap (first-ever bless commit).",
            file=sys.stderr,
        )
        return 0
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(ROOT),
    )
    if result.stdout.strip():
        print(
            "ERROR: working tree is dirty. Commit or stash first, "
            "or set BLESS_GOLDEN_ALLOW_DIRTY=1 for bootstrap.",
            file=sys.stderr,
        )
        print(result.stdout, file=sys.stderr)
        return 1
    return 0


def _compute_snapshot(spec: dict) -> dict[str, str]:
    """Run the build-phase pipeline minus contracts; return {path: sha256}."""
    # cli/build.py mixes two import conventions: top-level `from pipeline
    # import processor` (needs cli/ on sys.path) and `from cli.widgets...`
    # inside processor bodies (needs repo root on sys.path). The runtime
    # worker sets both up; the bless script mirrors that.
    cli_dir = str(ROOT / "cli")
    root_dir = str(ROOT)
    if cli_dir not in sys.path:
        sys.path.insert(0, cli_dir)
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)

    import pipeline as _pipeline  # noqa: WPS433 (local import is intentional)
    import build  # noqa: F401, WPS433 (triggers @processor registrations)

    artifact = types.SimpleNamespace(
        spec=spec,
        files=[],
        widgets_snippets={},
        metadata={},
        build_dir="/tmp/bless-golden",  # nosec B108  # local dev script, single-user machine
    )
    order = _pipeline.pipeline_order("build")
    subset = {name for name in order if name != "_run_contracts"}
    _pipeline.run_pipeline(artifact, phase="build", subset=subset)

    snapshot: dict[str, str] = {}
    files = artifact.files
    iterable = files.items() if isinstance(files, dict) else files
    for entry in iterable:
        path, content = entry[0], entry[1]
        if isinstance(content, str):
            content = content.encode("utf-8")
        snapshot[path] = hashlib.sha256(content).hexdigest()
    return snapshot


def _write_snapshot(path: Path, snapshot: dict[str, str]) -> None:
    """Write {path: sha256} as JSON with sorted keys and trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    path.write_text(payload)


def _diff_summary(
    old: dict[str, str] | None, new: dict[str, str]
) -> tuple[int, int, int, int]:
    """Return (unchanged, changed, added, removed) counts."""
    if old is None:
        return 0, 0, len(new), 0
    old_keys = set(old)
    new_keys = set(new)
    unchanged = sum(1 for k in old_keys & new_keys if old[k] == new[k])
    changed = sum(1 for k in old_keys & new_keys if old[k] != new[k])
    added = len(new_keys - old_keys)
    removed = len(old_keys - new_keys)
    return unchanged, changed, added, removed


def main(spec_name: str) -> int:
    """Entry point. See module docstring."""
    if _check_dirty_tree() != 0:
        return 1

    spec_dir = ROOT / "tests" / "golden" / spec_name
    spec_path = spec_dir / "spec.json"
    snap_path = spec_dir / "artifact.snapshot.json"

    if not spec_path.exists():
        print(f"ERROR: {spec_path} does not exist.", file=sys.stderr)
        return 1

    spec = json.loads(spec_path.read_text())
    old: dict[str, str] | None = None
    if snap_path.exists():
        try:
            old = json.loads(snap_path.read_text())
        except json.JSONDecodeError:
            print(
                f"WARNING: existing {snap_path} is not valid JSON; "
                "treating as empty.",
                file=sys.stderr,
            )
            old = None

    new = _compute_snapshot(spec)
    _write_snapshot(snap_path, new)

    unchanged, changed, added, removed = _diff_summary(old, new)
    print(
        f"unchanged={unchanged} changed={changed} added={added} removed={removed}"
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(
            "usage: python3 scripts/bless_golden.py <SPEC_NAME>",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
