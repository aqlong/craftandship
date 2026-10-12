"""When scripts/cas-executor-watchdog.sh may change anything.

The watchdog removes stale lock files and orphan worktrees and restarts the
executor. It is live only when started with no argument (and no DRY_RUN in
its environment other than 0); every other start is a dry run. These tests
run the real script with bash against a throwaway git repository, like the
watchdog tests in tests/test_dev_scripts.py, and look at what is left on disk.

The script used to set DRY_RUN=1 only for a first argument of exactly
--dry-run, and then compared the variable with "1" in some places and with
"0" in others: a mistyped --dryrun was a live run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHDOG = REPO_ROOT / "scripts" / "cas-executor-watchdog.sh"
BASH = shutil.which("bash") or "/bin/bash"
LONG_AGO = 1_000_000_000

pytestmark = pytest.mark.requires_tools("git")


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Watchdog Test", "-c", "user.email=watchdog@example.com", *args],
        cwd=project, check=True, capture_output=True,
    )


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class _Project:
    """A repository with everything the watchdog would clean up in a live
    run: a merge lock held by a dead process, an orphan worktree directory
    and a tracked file that differs from HEAD (which a live run records in
    its dirty-tree snapshot)."""

    def __init__(self, root: Path):
        self.root = root
        (root / "scripts").mkdir()
        shutil.copy(WATCHDOG, root / "scripts" / "cas-executor-watchdog.sh")
        (root / "scripts" / "cas-executor-config.json").write_text('{"enabled": false}\n')
        (root / "notes.txt").write_text("one\n")
        (root / ".gitignore").write_text("state/\nlogs/\n.worktrees/\n")
        _git(root, "init", "-q", "-b", "master")
        _git(root, "add", "notes.txt", ".gitignore", "scripts")
        _git(root, "commit", "-q", "-m", "start")
        (root / "notes.txt").write_text("two\n")

        self.state = root / "state"
        self.state.mkdir()
        self.lock = self.state / "cas-executor-merge.lock"
        self.lock.write_text(f"{_dead_pid()}\n")
        self.snapshot = self.state / "cas-watchdog-dirty-snapshot"
        self.worktree = root / ".worktrees" / "auto-item-a"
        self.worktree.mkdir(parents=True)
        os.utime(self.worktree, (LONG_AGO, LONG_AGO))

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
            "CAS_EXECUTOR_STATE_DIR": str(self.state),
            **env,
        }
        return subprocess.run(
            [BASH, str(self.root / "scripts" / "cas-executor-watchdog.sh"), *args],
            cwd=self.root, env=environment, capture_output=True, text=True, timeout=90,
        )

    def untouched(self) -> bool:
        return self.lock.exists() and self.worktree.exists() and not self.snapshot.exists()

    def cleaned(self) -> bool:
        return not self.lock.exists() and not self.worktree.exists() and self.snapshot.exists()


@pytest.fixture
def project(tmp_path):
    return _Project(tmp_path)


def test_a_run_with_no_argument_is_live(project):
    result = project.run()
    assert result.returncode == 0
    assert project.cleaned(), result.stdout
    assert "(dry_run=0)" in result.stdout
    assert "DRY-RUN" not in result.stdout


def test_dry_run_changes_nothing(project):
    result = project.run("--dry-run")
    assert result.returncode == 0
    assert project.untouched(), result.stdout
    assert "DRY-RUN would: rm -f" in result.stdout
    assert "(dry_run=1)" in result.stdout


@pytest.mark.parametrize("arguments", [
    ("--dryrun",), ("--dry_run",), ("-n",), ("--dry-run=1",), ("dry-run",), ("--DRY-RUN",),
    ("--help",), ("",), ("--live",), ("extra", "--dry-run"), ("--dry-run", "extra"),
])
def test_an_argument_the_script_does_not_know_is_a_dry_run_that_says_so(project, arguments):
    result = project.run(*arguments)
    assert result.returncode == 0, "the watchdog always exits 0"
    assert project.untouched(), f"a run started with {arguments!r} changed something:\n{result.stdout}"
    assert "unrecognised argument(s)" in result.stdout
    assert "DRY-RUN would: rm -f" in result.stdout
    assert "(dry_run=1)" in result.stdout


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "garbage", " 0"])
def test_an_inherited_dry_run_that_is_not_zero_is_a_dry_run(project, value):
    result = project.run(DRY_RUN=value)
    assert result.returncode == 0
    assert project.untouched(), f"DRY_RUN={value!r} in the environment was a live run:\n{result.stdout}"
    assert "in the environment" in result.stdout


@pytest.mark.parametrize("value", ["0", ""])
def test_an_inherited_dry_run_of_zero_or_nothing_is_live(project, value):
    result = project.run(DRY_RUN=value)
    assert result.returncode == 0
    assert project.cleaned(), result.stdout


def test_the_mode_is_read_in_one_place():
    """Every branch asks _live. A second comparison of the variable is how
    "1" in one place and "0" in another came to disagree."""
    code = [line for line in WATCHDOG.read_text().splitlines() if not line.strip().startswith("#")]
    comparisons = [line.strip() for line in code if re.search(r'"\$\{?DRY_RUN\}?"\s*(=|!=|==)', line)]
    live_reader = '_live() { [ "$DRY_RUN" = "0" ]; }'
    assert live_reader in code
    # The environment read that decides the mode, and the reader itself.
    assert comparisons == ['elif [ -n "${DRY_RUN:-}" ] && [ "${DRY_RUN}" != "0" ]; then', live_reader]
