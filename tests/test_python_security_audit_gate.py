"""Integration tests for the bandit + pip-audit pre-push gate.

Parallels tests/test_dep_audit_gate.py. Covers:
  - scripts/run_python_security_audit.sh exists, is executable, and
    honors CAS_SKIP_AUDIT=1.
  - .githooks/pre-push invokes the script and propagates its exit code.
  - The script is wired into .github/workflows/test.yml so CI enforces
    the same gate.
  - there is no bandit baseline file: bandit runs unfiltered and any
    finding fails the gate.

Pattern: shell out to the live hook against a throwaway git repo seeded
with stubs of every audit script. The live worktree is never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PRE_PUSH_SRC = REPO_ROOT / ".githooks" / "pre-push"
DEP_AUDIT_SRC = REPO_ROOT / "scripts" / "run_dep_audit.sh"
PY_AUDIT_SRC = REPO_ROOT / "scripts" / "run_python_security_audit.sh"
BANDIT_BASELINE = REPO_ROOT / "bandit-baseline.json"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"


def _init_tmp_repo() -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="cas-py-audit-test-"))
    subprocess.run(["git", "init", "-q"], cwd=str(tmpdir), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "config",
         "commit.gpgsign", "false"],
        cwd=str(tmpdir), check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "--allow-empty", "-m", "init"],
        cwd=str(tmpdir), check=True,
    )
    return tmpdir


def _install_hooks(repo: Path, py_audit_exit_code: int) -> None:
    """Install pre-push + a stub run_python_security_audit.sh with the
    given exit code. Stubs the dep-audit + parity scripts as no-ops so
    the pre-push test reaches the python-audit gate."""
    hooks_dir = repo / ".githooks"
    hooks_dir.mkdir(exist_ok=True)
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(exist_ok=True)

    shutil.copy(PRE_PUSH_SRC, hooks_dir / "pre-push")
    (hooks_dir / "pre-push").chmod(0o755)

    web_tests_stub = scripts_dir / "run_web_tests.sh"
    web_tests_stub.write_text("#!/bin/sh\nexit 0\n")
    web_tests_stub.chmod(0o755)

    dep_audit_stub = scripts_dir / "run_dep_audit.sh"
    dep_audit_stub.write_text("#!/bin/sh\nexit 0\n")
    dep_audit_stub.chmod(0o755)

    py_audit_stub = scripts_dir / "run_python_security_audit.sh"
    py_audit_stub.write_text(
        "#!/bin/sh\n"
        "echo 'python audit stub called'\n"
        f"exit {py_audit_exit_code}\n"
    )
    py_audit_stub.chmod(0o755)

    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"],
        cwd=str(repo), check=True,
    )


def _commit_noop(repo: Path) -> str:
    (repo / "touch.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "-m", "feat: noop"],
        cwd=str(repo), check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _invoke_pre_push(repo: Path, local_sha: str) -> subprocess.CompletedProcess:
    root_sha = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout.strip()
    stdin_line = (
        f"refs/heads/master {local_sha} refs/heads/master {root_sha}\n"
    )
    return subprocess.run(
        ["sh", str(repo / ".githooks" / "pre-push")],
        cwd=str(repo),
        input=stdin_line,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# pre-push wiring
# ---------------------------------------------------------------------------


@pytest.mark.requires_tools("git")
def test_pre_push_passes_when_python_audit_stub_exits_zero():
    repo = _init_tmp_repo()
    _install_hooks(repo, py_audit_exit_code=0)
    sha = _commit_noop(repo)
    result = _invoke_pre_push(repo, sha)
    assert result.returncode == 0, (
        f"expected rc=0, got {result.returncode} "
        f"stdout={result.stdout} stderr={result.stderr}"
    )
    assert "python audit stub called" in (result.stdout + result.stderr), (
        "expected hook to call run_python_security_audit.sh"
    )


@pytest.mark.requires_tools("git")
def test_pre_push_blocks_when_python_audit_stub_exits_nonzero():
    repo = _init_tmp_repo()
    _install_hooks(repo, py_audit_exit_code=1)
    sha = _commit_noop(repo)
    result = _invoke_pre_push(repo, sha)
    assert result.returncode != 0, (
        f"expected non-zero rc (python audit failure), got rc=0 "
        f"stdout={result.stdout} stderr={result.stderr}"
    )


# ---------------------------------------------------------------------------
# run_python_security_audit.sh self-tests
# ---------------------------------------------------------------------------


def test_run_python_security_audit_is_executable():
    assert PY_AUDIT_SRC.exists(), "scripts/run_python_security_audit.sh missing"
    assert os.access(PY_AUDIT_SRC, os.X_OK), (
        "scripts/run_python_security_audit.sh not executable"
    )


@pytest.mark.requires_tools("git")
def test_run_python_security_audit_respects_skip_env():
    """With CAS_SKIP_AUDIT=1 the script exits 0 before invoking bandit
    or pip-audit. Proven by running it from a tmpdir that has neither
    tool resolvable on a stripped PATH."""
    tmpdir = Path(tempfile.mkdtemp(prefix="cas-py-audit-skip-"))
    subprocess.run(["git", "init", "-q"], cwd=str(tmpdir), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "--allow-empty", "-m", "init"],
        cwd=str(tmpdir), check=True,
    )
    scripts_dir = tmpdir / "scripts"
    scripts_dir.mkdir()
    shutil.copy(PY_AUDIT_SRC, scripts_dir / "run_python_security_audit.sh")
    (scripts_dir / "run_python_security_audit.sh").chmod(0o755)

    env = dict(os.environ)
    env["CAS_SKIP_AUDIT"] = "1"
    # Strip PATH so missing-tools wouldn't matter even if the skip
    # short-circuit didn't fire: the test asserts skip wins.
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run(
        ["sh", str(scripts_dir / "run_python_security_audit.sh")],
        cwd=str(tmpdir), capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (
        f"CAS_SKIP_AUDIT=1 should exit 0; got rc={result.returncode} "
        f"stdout={result.stdout} stderr={result.stderr}"
    )
    assert "skipping" in (result.stdout + result.stderr).lower()


@pytest.mark.requires_tools("git")
def test_run_python_security_audit_warns_when_tools_missing_locally():
    """Local mode (CI=false / unset): missing bandit/pip-audit must NOT
    block, script warns and exits 0 so a fresh checkout without dev
    tools installed can still push. CI mode is covered separately."""
    tmpdir = Path(tempfile.mkdtemp(prefix="cas-py-audit-local-"))
    subprocess.run(["git", "init", "-q"], cwd=str(tmpdir), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "--allow-empty", "-m", "init"],
        cwd=str(tmpdir), check=True,
    )
    scripts_dir = tmpdir / "scripts"
    scripts_dir.mkdir()
    shutil.copy(PY_AUDIT_SRC, scripts_dir / "run_python_security_audit.sh")
    (scripts_dir / "run_python_security_audit.sh").chmod(0o755)

    env = {k: v for k, v in os.environ.items() if k not in ("CI", "GITHUB_ACTIONS")}
    env["PATH"] = "/usr/bin:/bin"  # bandit + pip-audit not on this PATH
    result = subprocess.run(
        ["sh", str(scripts_dir / "run_python_security_audit.sh")],
        cwd=str(tmpdir), capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (
        f"local + tools missing should warn + exit 0; got rc={result.returncode} "
        f"stdout={result.stdout} stderr={result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "skipping" in combined.lower() and "ci will enforce" in combined.lower()


@pytest.mark.requires_tools("git")
def test_run_python_security_audit_fails_when_tools_missing_in_ci():
    """CI mode: missing tools must fail the gate. The CI step installs
    them via requirements-dev.txt; tools-missing in CI means the install
    step regressed."""
    tmpdir = Path(tempfile.mkdtemp(prefix="cas-py-audit-ci-"))
    subprocess.run(["git", "init", "-q"], cwd=str(tmpdir), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "--allow-empty", "-m", "init"],
        cwd=str(tmpdir), check=True,
    )
    scripts_dir = tmpdir / "scripts"
    scripts_dir.mkdir()
    shutil.copy(PY_AUDIT_SRC, scripts_dir / "run_python_security_audit.sh")
    (scripts_dir / "run_python_security_audit.sh").chmod(0o755)

    env = dict(os.environ)
    env["CI"] = "true"
    env["PATH"] = "/usr/bin:/bin"  # bandit + pip-audit not on this PATH
    result = subprocess.run(
        ["sh", str(scripts_dir / "run_python_security_audit.sh")],
        cwd=str(tmpdir), capture_output=True, text=True, env=env,
    )
    assert result.returncode == 1, (
        f"CI + tools missing should exit 1; got rc={result.returncode} "
        f"stdout={result.stdout} stderr={result.stderr}"
    )


# ---------------------------------------------------------------------------
# CI wiring
# ---------------------------------------------------------------------------


def test_ci_workflow_invokes_run_python_security_audit():
    """CI backstop: the python job in test.yml must call the audit script
    so the gate still enforces even when a push bypasses local hooks."""
    content = CI_WORKFLOW.read_text()
    assert "run_python_security_audit.sh" in content, (
        ".github/workflows/test.yml must invoke "
        "scripts/run_python_security_audit.sh"
    )


# ---------------------------------------------------------------------------
# No baseline file
# ---------------------------------------------------------------------------


def test_bandit_runs_without_a_baseline():
    """The tree is clean at the gate's threshold, so the gate has no baseline
    to hide findings in: any finding fails, and an accepted one must carry an
    inline `# nosec BXXX  # reason` comment. A public baseline file would read
    as 'known findings suppressed'."""
    script = PY_AUDIT_SRC.read_text()
    assert "bandit-baseline" not in script
    assert " -b " not in script and "$BASELINE" not in script
    assert not BANDIT_BASELINE.exists(), (
        "bandit-baseline.json must not come back; fix the finding or add a "
        "justified `# nosec` comment instead"
    )
