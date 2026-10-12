"""Golden-file pinning tests for b-plus-c phase 1.

Phase 1 scope: tests/golden/medicine-waves-canonical/ only.
A phase 2 (one non-MW spec under tests/golden/non-mw-canonical/) was planned
but never added; the archetype gate has since flipped to on-by-default and
test_b_plus_c_integration.py covers a non-MW spec instead.

These tests guard the pipeline's deterministic output surface. If the
pipeline regresses, or a developer forgot to re-bless after an intended
change, the per-file sha256 comparison fails loudly.

Layered tests:

- test_golden_file_hash_matches_pipeline_output: end-to-end hash match.
- test_bless_golden_script_regenerates_deterministically: the bless script,
  run on a temporary copy of the project root, reproduces the committed
  snapshot and a second run produces byte-identical output.
- test_bless_golden_script_refuses_on_dirty_tree: dirty-tree guardrail
  verified via a throwaway tmp git repo (the live repo is never touched).
- test_golden_binary_assets_unchanged: tests/golden/mw-logo.png is
  byte-identical to cli/assets/mw_logo.png.
- test_mw_logo_sha256_matches_snapshot_entry: the pinned hash for
  public/images/mw_logo.png inside the snapshot matches the byte hash of
  tests/golden/mw-logo.png, proving the canonical logo flows through the
  pipeline unchanged.
- test_bless_golden_stdout_summary_format: summary line shape check.
"""

from __future__ import annotations

import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"
MW_CANONICAL = GOLDEN_DIR / "medicine-waves-canonical"
SPEC_PATH = MW_CANONICAL / "spec.json"
SNAPSHOT_PATH = MW_CANONICAL / "artifact.snapshot.json"
MW_LOGO_COPY = GOLDEN_DIR / "mw-logo.png"
MW_LOGO_SOURCE = REPO_ROOT / "cli" / "assets" / "mw_logo.png"
BLESS_SCRIPT = REPO_ROOT / "scripts" / "bless_golden.py"


def _compute_snapshot_in_process(spec: dict) -> dict[str, str]:
    """Run the pipeline in-process and return {path: sha256} mirroring
    scripts/bless_golden.py's logic. Used for the hash-match test so we
    avoid forking a subprocess on every test invocation."""
    cli_dir = str(REPO_ROOT / "cli")
    root_dir = str(REPO_ROOT)
    for path in (cli_dir, root_dir):
        if path not in sys.path:
            sys.path.insert(0, path)
    import pipeline as _pipeline
    import build  # noqa: F401 (triggers @processor registrations)

    artifact = types.SimpleNamespace(
        spec=spec,
        files=[],
        widgets_snippets={},
        metadata={},
        build_dir="/tmp/test-golden",
    )
    order = _pipeline.pipeline_order("build")
    subset = {name for name in order if name != "_run_contracts"}
    _pipeline.run_pipeline(artifact, phase="build", subset=subset)

    out: dict[str, str] = {}
    files = artifact.files
    iterable = files.items() if isinstance(files, dict) else files
    for entry in iterable:
        path, content = entry[0], entry[1]
        if isinstance(content, str):
            content = content.encode("utf-8")
        out[path] = hashlib.sha256(content).hexdigest()
    return out


def test_golden_file_hash_matches_pipeline_output():
    """The snapshot JSON must exactly reflect the current pipeline output.

    If this fails: either (a) you changed pipeline behavior intentionally
    and need to run `make bless-golden SPEC=medicine-waves-canonical` +
    commit with `bless:` prefix, or (b) something regressed and needs to
    be fixed before shipping.
    """
    spec = json.loads(SPEC_PATH.read_text())
    expected = json.loads(SNAPSHOT_PATH.read_text())
    actual = _compute_snapshot_in_process(spec)
    # Use a pair of set diffs + key-by-key mismatch list so pytest output
    # shows exactly which files drifted.
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    hash_drift = {
        k: (expected[k], actual[k])
        for k in set(expected) & set(actual)
        if expected[k] != actual[k]
    }
    assert not missing, f"pipeline no longer produces: {missing}"
    assert not extra, f"pipeline produces new files not in snapshot: {extra}"
    assert not hash_drift, (
        "hashes drifted for:\n"
        + "\n".join(f"  {k}: pinned={v[0][:12]} actual={v[1][:12]}"
                    for k, v in sorted(hash_drift.items()))
    )


@pytest.fixture()
def bless_sandbox(tmp_path):
    """A throwaway project root for scripts/bless_golden.py.

    The script resolves its project root from its own location and writes
    tests/golden/<spec>/artifact.snapshot.json under it. Running it on a
    copy (the script, the golden spec directory) with the code under test
    (cli/, compose/) symlinked in means a test run never rewrites the
    tracked snapshot, so it is safe next to a parallel reader of that file
    and leaves nothing to restore.
    """
    root = tmp_path / "bless-root"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(BLESS_SCRIPT, root / "scripts" / "bless_golden.py")
    shutil.copytree(MW_CANONICAL, root / "tests" / "golden" / MW_CANONICAL.name)
    for package in ("cli", "compose"):
        (root / package).symlink_to(REPO_ROOT / package, target_is_directory=True)
    return root


def _run_bless(root):
    """Run the sandbox copy of the bless script; BLESS_GOLDEN_ALLOW_DIRTY=1
    skips its git clean-tree check (the sandbox is not a git repository)."""
    env = dict(os.environ)
    env["BLESS_GOLDEN_ALLOW_DIRTY"] = "1"
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "bless_golden.py"), MW_CANONICAL.name],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
    )


def test_bless_golden_script_regenerates_deterministically(bless_sandbox):
    """Two back-to-back invocations of scripts/bless_golden.py must produce
    byte-identical snapshots, and the first must reproduce the committed one.

    Runs on a temporary copy (see bless_sandbox); the tracked snapshot under
    tests/golden/ is only read.
    """
    sandbox_snapshot = bless_sandbox / "tests" / "golden" / MW_CANONICAL.name / "artifact.snapshot.json"
    committed = SNAPSHOT_PATH.read_bytes()
    # Start from an empty file so the first run proves it writes the snapshot
    # rather than leaving the copied one in place.
    sandbox_snapshot.write_text("{}\n")

    first = _run_bless(bless_sandbox)
    assert first.returncode == 0, (
        f"first bless run failed: stdout={first.stdout} stderr={first.stderr}"
    )
    first_bytes = sandbox_snapshot.read_bytes()
    assert first_bytes == committed, (
        "regenerating the golden snapshot from the committed spec does not "
        "reproduce the committed tests/golden snapshot"
    )

    second = _run_bless(bless_sandbox)
    assert second.returncode == 0, f"second bless run failed: {second.stderr}"
    assert sandbox_snapshot.read_bytes() == first_bytes, (
        "snapshot bytes drifted between two invocations"
    )
    assert SNAPSHOT_PATH.read_bytes() == committed, "the tracked snapshot was modified"


def test_bless_golden_script_refuses_on_dirty_tree(tmp_path):
    """On a dirty git tree (without the BLESS_GOLDEN_ALLOW_DIRTY escape
    hatch) scripts/bless_golden.py exits non-zero and prints a readable
    error mentioning the dirty state.

    Isolation: we build a throwaway git repo in tmp_path, copy the bless
    script + a minimal cli/ + pipeline fixture into it, then dirty the
    tree and invoke the script. The live repo is never touched.
    """
    # Build a throwaway repo layout that the bless script expects.
    sandbox = tmp_path / "repo"
    (sandbox / "scripts").mkdir(parents=True)
    (sandbox / "tests" / "golden" / "sandbox-spec").mkdir(parents=True)
    shutil.copy(BLESS_SCRIPT, sandbox / "scripts" / "bless_golden.py")
    # Spec file can be empty JSON; we never reach pipeline execution
    # because the dirty-tree guard fires first.
    (sandbox / "tests" / "golden" / "sandbox-spec" / "spec.json").write_text(
        "{}\n"
    )

    subprocess.run(["git", "init", "-q"], cwd=str(sandbox), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "--allow-empty", "-m", "init"],
        cwd=str(sandbox), check=True,
    )
    # Dirty the tree: add a new file that is neither committed nor ignored.
    (sandbox / "dirty.txt").write_text("uncommitted\n")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(sandbox),
        capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip(), "precondition: tmp repo must be dirty"

    result = subprocess.run(
        [sys.executable, str(sandbox / "scripts" / "bless_golden.py"),
         "sandbox-spec"],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "dirty-tree run should have failed but returned 0"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "dirty" in combined, (
        f"expected 'dirty' in stderr/stdout; got stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def test_golden_binary_assets_unchanged():
    """tests/golden/mw-logo.png is byte-identical to cli/assets/mw_logo.png.

    Guards against accidental binary drift (e.g. someone running the image
    through a tool that silently re-encodes).
    """
    assert MW_LOGO_COPY.read_bytes() == MW_LOGO_SOURCE.read_bytes()


def test_mw_logo_sha256_matches_snapshot_entry():
    """The snapshot entry for public/images/mw_logo.png matches the byte
    hash of tests/golden/mw-logo.png.

    This is the bridge assertion: if the pipeline bundles the MW logo at
    public/images/mw_logo.png (FIX O), its hash in the pinned snapshot
    must equal the byte hash of the canonical copy we keep under
    tests/golden/. If they drift, either the bundling processor stopped
    writing the expected bytes or the tests/golden copy was accidentally
    re-encoded.
    """
    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    key = "public/images/mw_logo.png"
    assert key in snapshot, (
        f"snapshot missing {key!r}; pipeline may have stopped bundling "
        "the MW logo"
    )
    expected_hash = hashlib.sha256(MW_LOGO_COPY.read_bytes()).hexdigest()
    assert snapshot[key] == expected_hash


def test_bless_golden_stdout_summary_format(bless_sandbox):
    """scripts/bless_golden.py prints a single summary line of the form
    `unchanged=N changed=N added=N removed=N`. Runs on a temporary copy."""
    result = _run_bless(bless_sandbox)
    assert result.returncode == 0, result.stderr
    pattern = re.compile(
        r"unchanged=\d+ changed=\d+ added=\d+ removed=\d+"
    )
    match = None
    for line in result.stdout.splitlines():
        if pattern.fullmatch(line.strip()):
            match = line.strip()
            break
    assert match is not None, (
        f"summary line not found in stdout: {result.stdout!r}"
    )
    # The copy starts from the committed snapshot, so nothing may have changed.
    assert " changed=0 added=0 removed=0" in match, match
