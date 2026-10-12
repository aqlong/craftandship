"""Integration tests for the npm-audit pre-push gate.

Covers:
  - scripts/run_dep_audit.sh exists, is executable, and honors
    CAS_SKIP_AUDIT=1 without needing npm/web/ present.
  - .githooks/pre-push invokes the audit script and propagates its exit
    code (blocks push on non-zero).
  - The audit script is wired into .github/workflows/test.yml so CI
    enforces the same gate.

Pattern mirrors tests/test_hook_golden_guard.py: shell out to the live
hook against a throwaway git repo seeded with a stub audit script. The
live worktree is never touched.
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
AUDIT_SRC = REPO_ROOT / "scripts" / "run_dep_audit.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"


def _init_tmp_repo() -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="cas-audit-test-"))
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


def _install_hooks(repo: Path, audit_exit_code: int) -> None:
    """Install pre-push + a stub run_dep_audit.sh with the given exit code.

    Also stubs run_web_tests.sh with a no-op so the pre-push test
    reaches the dep-audit gate but does not spawn Vitest."""
    hooks_dir = repo / ".githooks"
    hooks_dir.mkdir(exist_ok=True)
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(exist_ok=True)

    shutil.copy(PRE_PUSH_SRC, hooks_dir / "pre-push")
    (hooks_dir / "pre-push").chmod(0o755)

    web_tests_stub = scripts_dir / "run_web_tests.sh"
    web_tests_stub.write_text("#!/bin/sh\nexit 0\n")
    web_tests_stub.chmod(0o755)

    audit_stub = scripts_dir / "run_dep_audit.sh"
    audit_stub.write_text(
        "#!/bin/sh\n"
        f"echo 'audit stub called'\n"
        f"exit {audit_exit_code}\n"
    )
    audit_stub.chmod(0o755)

    # Stub the Python security audit gate too; these tests target the
    # dep-audit gate behavior, not the python-audit gate. The python-audit
    # gate has its own coverage in test_python_security_audit_gate.py.
    py_audit_stub = scripts_dir / "run_python_security_audit.sh"
    py_audit_stub.write_text("#!/bin/sh\nexit 0\n")
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
def test_pre_push_passes_when_audit_stub_exits_zero():
    repo = _init_tmp_repo()
    _install_hooks(repo, audit_exit_code=0)
    sha = _commit_noop(repo)
    result = _invoke_pre_push(repo, sha)
    assert result.returncode == 0, (
        f"expected rc=0, got {result.returncode} "
        f"stdout={result.stdout} stderr={result.stderr}"
    )
    assert "audit stub called" in (result.stdout + result.stderr), (
        "expected hook to call run_dep_audit.sh"
    )


@pytest.mark.requires_tools("git")
def test_pre_push_blocks_when_audit_stub_exits_nonzero():
    repo = _init_tmp_repo()
    _install_hooks(repo, audit_exit_code=1)
    sha = _commit_noop(repo)
    result = _invoke_pre_push(repo, sha)
    assert result.returncode != 0, (
        f"expected non-zero rc (audit failure), got rc=0 "
        f"stdout={result.stdout} stderr={result.stderr}"
    )


# ---------------------------------------------------------------------------
# run_dep_audit.sh self-tests
# ---------------------------------------------------------------------------


def test_run_dep_audit_is_executable():
    assert AUDIT_SRC.exists(), "scripts/run_dep_audit.sh missing"
    assert os.access(AUDIT_SRC, os.X_OK), "scripts/run_dep_audit.sh not executable"


@pytest.mark.requires_tools("git")
def test_run_dep_audit_respects_skip_env():
    """With CAS_SKIP_AUDIT=1 the script exits 0 before touching web/ or
    invoking npm. Proven by running it from a tmpdir that has no web/
    directory and no npm on PATH considerations, the skip short-circuit
    must fire first."""
    tmpdir = Path(tempfile.mkdtemp(prefix="cas-audit-skip-"))
    subprocess.run(["git", "init", "-q"], cwd=str(tmpdir), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "--allow-empty", "-m", "init"],
        cwd=str(tmpdir), check=True,
    )
    scripts_dir = tmpdir / "scripts"
    scripts_dir.mkdir()
    shutil.copy(AUDIT_SRC, scripts_dir / "run_dep_audit.sh")
    (scripts_dir / "run_dep_audit.sh").chmod(0o755)

    env = dict(os.environ)
    env["CAS_SKIP_AUDIT"] = "1"
    result = subprocess.run(
        ["sh", str(scripts_dir / "run_dep_audit.sh")],
        cwd=str(tmpdir), capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (
        f"CAS_SKIP_AUDIT=1 should exit 0; got rc={result.returncode} "
        f"stdout={result.stdout} stderr={result.stderr}"
    )
    assert "skipping" in (result.stdout + result.stderr).lower()


# ---------------------------------------------------------------------------
# CI wiring
# ---------------------------------------------------------------------------


def test_ci_workflow_invokes_run_dep_audit():
    """CI backstop: the unit job in test.yml must call run_dep_audit.sh so
    the gate still enforces even when a push bypasses local hooks."""
    content = CI_WORKFLOW.read_text()
    assert "run_dep_audit.sh" in content, (
        ".github/workflows/test.yml must invoke scripts/run_dep_audit.sh"
    )
