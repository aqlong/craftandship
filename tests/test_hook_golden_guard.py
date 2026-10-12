"""Hook-integrity tests for the golden-file guard + failure_kb JSON guard.

Pattern: shell out to the live .githooks/pre-push and .githooks/pre-commit
scripts against a throwaway git repo seeded with controlled content.
The live worktree is never touched, every test writes to tempfile.mkdtemp()
and git-inits a fresh repo there, then copies the real hook in.

Each test stubs scripts/run_web_tests.sh inside the tmp repo with
a no-op exit 0 so the pre-push test targets only the bless-gate logic and
does not spawn Vitest.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")

REPO_ROOT = Path(__file__).resolve().parent.parent
PRE_PUSH_SRC = REPO_ROOT / ".githooks" / "pre-push"
PRE_COMMIT_SRC = REPO_ROOT / ".githooks" / "pre-commit"
CHECK_DDL_SRC = REPO_ROOT / "scripts" / "check_destructive_ddl.py"


def _init_tmp_repo() -> Path:
    """Create a throwaway git repo under a tmpdir and return its path."""
    tmpdir = Path(tempfile.mkdtemp(prefix="cas-hook-test-"))
    subprocess.run(["git", "init", "-q"], cwd=str(tmpdir), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "config",
         "commit.gpgsign", "false"],
        cwd=str(tmpdir), check=True,
    )
    # Initial empty commit so HEAD exists for pre-push ref arithmetic.
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "--allow-empty", "-m", "init"],
        cwd=str(tmpdir), check=True,
    )
    return tmpdir


def _install_hooks(repo: Path, *, include_pre_push: bool = True,
                   include_pre_commit: bool = True) -> None:
    """Copy the live hooks into repo/.githooks/ and point core.hooksPath
    at them. Also stubs scripts/run_web_tests.sh with a no-op so the
    pre-push test does not try to run Vitest."""
    hooks_dir = repo / ".githooks"
    hooks_dir.mkdir(exist_ok=True)
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(exist_ok=True)

    if include_pre_push:
        dst = hooks_dir / "pre-push"
        shutil.copy(PRE_PUSH_SRC, dst)
        dst.chmod(0o755)
    if include_pre_commit:
        dst = hooks_dir / "pre-commit"
        shutil.copy(PRE_COMMIT_SRC, dst)
        dst.chmod(0o755)
        # check_destructive_ddl.py is referenced by pre-commit when
        # web/src/db/schema.sql is staged; copy it in so any accidental
        # DDL path does not 127-crash.
        if CHECK_DDL_SRC.exists():
            ddl_dst = scripts_dir / "check_destructive_ddl.py"
            shutil.copy(CHECK_DDL_SRC, ddl_dst)
            ddl_dst.chmod(0o755)

    # No-op stub for the Vitest parity runner; the bless gate runs BEFORE
    # this so the stub only fires on tests where we want pre-push to
    # reach the unit-test phase and succeed.
    stub = scripts_dir / "run_web_tests.sh"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)

    # No-op stub for the npm-audit gate (scripts/run_dep_audit.sh); added
    # 2026-04-19 when the dep-audit pre-push gate landed. Tests here target
    # the bless gate, not the audit gate; they still need a green audit
    # stub so the hook's audit-gate short-circuit does not fire. The
    # dep-audit behavior has its own coverage in test_dep_audit_gate.py.
    audit_stub = scripts_dir / "run_dep_audit.sh"
    audit_stub.write_text("#!/bin/sh\nexit 0\n")
    audit_stub.chmod(0o755)

    # No-op stub for the Python security audit gate
    # (scripts/run_python_security_audit.sh): same rationale as above.
    # The python-audit behavior has its own coverage in
    # test_python_security_audit_gate.py.
    py_audit_stub = scripts_dir / "run_python_security_audit.sh"
    py_audit_stub.write_text("#!/bin/sh\nexit 0\n")
    py_audit_stub.chmod(0o755)

    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"],
        cwd=str(repo), check=True,
    )


def _commit_all(repo: Path, message: str) -> str:
    """Stage everything and commit with the given message. Returns the
    resulting commit SHA."""
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "-m", message],
        cwd=str(repo), check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout.strip()
    return sha


def _hook_env(**extra: str) -> dict:
    """The caller's environment minus the variables that switch hook gates
    off, so a shell that exports CAS_PROMOTE or CAS_SKIP_AUDIT cannot change
    what these tests observe."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CAS_PROMOTE", "CAS_EXECUTOR", "CAS_SKIP_AUDIT")}
    env.update(extra)
    return env


def _invoke_pre_push(repo: Path, local_sha: str, *,
                     cas_promote: str | None = None) -> subprocess.CompletedProcess:
    """Invoke .githooks/pre-push with the standard stdin protocol. The
    remote_sha is forced to the initial-empty-commit SHA so git log sees
    the full history above it as the push range."""
    # Determine the initial commit SHA (first commit in the repo) so the
    # pre-push range walks every later commit.
    root_sha = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout.strip()
    stdin_line = (
        f"refs/heads/master {local_sha} refs/heads/master {root_sha}\n"
    )
    env = _hook_env()
    if cas_promote is not None:
        env["CAS_PROMOTE"] = cas_promote
    return subprocess.run(
        ["sh", str(repo / ".githooks" / "pre-push")],
        cwd=str(repo),
        input=stdin_line,
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# pre-push: bless gate
# ---------------------------------------------------------------------------


def test_pre_push_rejects_golden_change_without_bless_prefix():
    """Commit touches tests/golden/** with a plain message; push blocked."""
    repo = _init_tmp_repo()
    _install_hooks(repo)
    # Commit a file inside tests/golden/ without a bless: prefix.
    (repo / "tests" / "golden" / "dummy-spec").mkdir(parents=True)
    (repo / "tests" / "golden" / "dummy-spec" / "spec.json").write_text("{}\n")
    sha = _commit_all(repo, "feat: add dummy golden fixture")
    result = _invoke_pre_push(repo, sha)
    assert result.returncode != 0, (
        f"expected blocked push, got rc=0 stdout={result.stdout} stderr={result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "bless:" in combined, f"expected 'bless:' guidance, got {combined!r}"


def test_pre_push_allows_golden_change_with_bless_prefix():
    """Commit touches tests/golden/** with a `bless:` prefix; push allowed."""
    repo = _init_tmp_repo()
    _install_hooks(repo)
    (repo / "tests" / "golden" / "dummy-spec").mkdir(parents=True)
    (repo / "tests" / "golden" / "dummy-spec" / "spec.json").write_text("{}\n")
    sha = _commit_all(repo, "bless: dummy-spec - initial capture")
    result = _invoke_pre_push(repo, sha)
    assert result.returncode == 0, (
        f"expected pass, got rc={result.returncode} stdout={result.stdout} "
        f"stderr={result.stderr}"
    )


def test_pre_push_allows_golden_change_with_cas_promote_escape_hatch():
    """CAS_PROMOTE=1 bypasses the bless gate for the current push."""
    repo = _init_tmp_repo()
    _install_hooks(repo)
    (repo / "tests" / "golden" / "dummy-spec").mkdir(parents=True)
    (repo / "tests" / "golden" / "dummy-spec" / "spec.json").write_text("{}\n")
    sha = _commit_all(repo, "chore: bundle release notes (incidental golden touch)")
    result = _invoke_pre_push(repo, sha, cas_promote="1")
    assert result.returncode == 0, (
        f"expected pass with CAS_PROMOTE=1, got rc={result.returncode} "
        f"stdout={result.stdout} stderr={result.stderr}"
    )


# ---------------------------------------------------------------------------
# pre-push: bless gate on a NEW ref (first push, new branch)
# ---------------------------------------------------------------------------

ZERO_SHA = "0" * 40


def _invoke_pre_push_new_ref(repo: Path, local_sha: str) -> subprocess.CompletedProcess:
    """Invoke the hook the way git does for a ref that does not exist on the
    remote yet (remote_sha is all zeros)."""
    stdin_line = f"refs/heads/feature {local_sha} refs/heads/feature {ZERO_SHA}\n"
    return subprocess.run(
        ["sh", str(repo / ".githooks" / "pre-push")],
        cwd=str(repo), input=stdin_line, capture_output=True, text=True,
        env=_hook_env(),
    )


def test_pre_push_first_push_to_empty_remote_skips_bless_gate():
    """The very first push has no origin/master to compare against; scanning
    the whole history would block the initial public release (its commits
    legitimately touch tests/golden/ without a `bless:` prefix)."""
    repo = _init_tmp_repo()
    _install_hooks(repo)
    (repo / "tests" / "golden" / "dummy-spec").mkdir(parents=True)
    (repo / "tests" / "golden" / "dummy-spec" / "spec.json").write_text("{}\n")
    sha = _commit_all(repo, "Initial commit")
    result = _invoke_pre_push_new_ref(repo, sha)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "bless-gate skipped" in result.stdout


def test_pre_push_new_branch_still_checks_commits_after_origin_master():
    """A new branch pushed when origin/master exists is scoped to the commits
    after the merge-base, and those must still carry the bless: prefix."""
    repo = _init_tmp_repo()
    _install_hooks(repo)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), check=True,
                          capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/master", base],
                   cwd=str(repo), check=True)
    (repo / "tests" / "golden" / "dummy-spec").mkdir(parents=True)
    (repo / "tests" / "golden" / "dummy-spec" / "spec.json").write_text("{}\n")
    sha = _commit_all(repo, "feat: add dummy golden fixture")
    result = _invoke_pre_push_new_ref(repo, sha)
    assert result.returncode != 0
    assert "bless:" in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# pre-push: staging workflow gate (direct pushes to the default branch)
# ---------------------------------------------------------------------------


def _staging_repo(config) -> tuple:
    """A throwaway repo whose scripts/cas-executor-config.json holds `config`
    (a dict, or a raw string for the malformed case). Returns (repo, sha)."""
    repo = _init_tmp_repo()
    _install_hooks(repo)
    text = config if isinstance(config, str) else json.dumps(config)
    (repo / "scripts" / "cas-executor-config.json").write_text(text)
    sha = _commit_all(repo, "chore: add executor config")
    return repo, sha


def _push_ref(repo: Path, sha: str, remote_ref: str, **env_extra: str) -> subprocess.CompletedProcess:
    root_sha = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout.strip()
    return subprocess.run(
        ["sh", str(repo / ".githooks" / "pre-push")],
        cwd=str(repo),
        input=f"{remote_ref} {sha} {remote_ref} {root_sha}\n",
        capture_output=True,
        text=True,
        env=_hook_env(**env_extra),
    )


@pytest.mark.parametrize("branch", ["master", "main"])
def test_pre_push_blocks_a_direct_push_to_the_default_branch_when_staging_is_enabled(branch):
    repo, sha = _staging_repo({"use_staging_workflow": True})
    result = _push_ref(repo, sha, f"refs/heads/{branch}")
    assert result.returncode != 0
    assert "BLOCKED: Direct push to master" in result.stdout + result.stderr


@pytest.mark.parametrize("bypass", ["CAS_PROMOTE", "CAS_EXECUTOR"])
def test_pre_push_staging_gate_accepts_the_two_documented_bypasses(bypass):
    repo, sha = _staging_repo({"use_staging_workflow": True})
    result = _push_ref(repo, sha, "refs/heads/master", **{bypass: "1"})
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("not_one", ["0", "true", ""])
def test_pre_push_staging_gate_bypass_requires_exactly_1(not_one):
    repo, sha = _staging_repo({"use_staging_workflow": True})
    result = _push_ref(repo, sha, "refs/heads/master", CAS_PROMOTE=not_one)
    assert result.returncode != 0


def test_pre_push_staging_gate_lets_other_branches_through():
    repo, sha = _staging_repo({"use_staging_workflow": True})
    assert _push_ref(repo, sha, "refs/heads/staging").returncode == 0
    assert _push_ref(repo, sha, "refs/heads/feature/x").returncode == 0


@pytest.mark.parametrize("config", [{"use_staging_workflow": False}, {}, "not json {"])
def test_pre_push_staging_gate_is_off_unless_the_config_turns_it_on(config):
    """Shipped state: use_staging_workflow is false, so a push to master is not blocked."""
    repo, sha = _staging_repo(config)
    result = _push_ref(repo, sha, "refs/heads/master")
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# pre-commit: failure_kb JSON guard
# ---------------------------------------------------------------------------


def _stage_failure_kb(repo: Path, content: str) -> None:
    """Create/overwrite cli/failure_kb.json with given content and stage it."""
    path = repo / "cli" / "failure_kb.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(
        ["git", "add", "cli/failure_kb.json"],
        cwd=str(repo), check=True,
    )


def _invoke_pre_commit(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(repo / ".githooks" / "pre-commit")],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


def test_pre_commit_rejects_invalid_failure_kb_json():
    """Invalid JSON (trailing comma) staged in cli/failure_kb.json blocks commit."""
    repo = _init_tmp_repo()
    _install_hooks(repo, include_pre_push=False)
    _stage_failure_kb(repo, '{"k": "v",}\n')  # trailing comma -> invalid
    result = _invoke_pre_commit(repo)
    assert result.returncode != 0, (
        f"expected reject, got rc=0 stdout={result.stdout} stderr={result.stderr}"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "invalid json" in combined or "json" in combined, (
        f"expected JSON error mention, got {combined!r}"
    )


def test_pre_commit_passes_on_valid_failure_kb_json():
    """Valid JSON staged in cli/failure_kb.json passes the pre-commit hook."""
    repo = _init_tmp_repo()
    _install_hooks(repo, include_pre_push=False)
    _stage_failure_kb(repo, '{"k": {"failure_count": 0}}\n')
    result = _invoke_pre_commit(repo)
    assert result.returncode == 0, (
        f"expected pass, got rc={result.returncode} stdout={result.stdout} "
        f"stderr={result.stderr}"
    )
