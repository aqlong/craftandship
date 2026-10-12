# tests/test_sync_staging_docs_superset.py
#
# Integration tests for the --prefer-staging-on-docs-superset flag added to
# scripts/sync_staging_from_master.sh.
#
# The flag opts in to auto-resolving add/add merge conflicts STRICTLY under
# docs/plans/** when staging's version contains every line of master's
# version (line-wise subset; tolerant of reordering / appended notes). On any
# non-docs path conflict, or any failed superset check, it bails as today.
#
# These tests build a tiny throwaway origin + working repo per case so we can
# drive the script through the same code path the founder hits in real life.

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "sync_staging_from_master.sh"


def _run(*args: str, cwd: Path | None = None, env: dict | None = None,
         check: bool = False) -> subprocess.CompletedProcess:
    e = os.environ.copy()
    # Force a stable git config to avoid host hooks/templates leaking in.
    e["GIT_CONFIG_GLOBAL"] = "/dev/null"
    e["GIT_CONFIG_SYSTEM"] = "/dev/null"
    if env:
        e.update(env)
    r = subprocess.run(args, cwd=str(cwd) if cwd else None, env=e,
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(
            f"command failed ({r.returncode}): {args}\n"
            f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
        )
    return r


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return _run("git", *args, cwd=cwd, check=check)


def _setup_repo() -> tuple[Path, Path, Path]:
    """Create a bare origin and a working clone with master + staging branches.

    Layout:
      master:  has docs/plans/sections/section-a.md (v1)
      staging: master's commit, then a follow-on commit appending notes to
               section-a.md and adding section-b.md unrelated to master.
    Then master gets a NEW commit modifying section-a.md, creating an add/add
    conflict at merge time (well, modify/modify; the add/add framing in the
    description applies to files added on both branches with different
    content, same conflict shape).
    """
    tmp = Path(tempfile.mkdtemp(prefix="cas-sync-superset-"))
    origin = tmp / "origin.git"
    work = tmp / "work"
    origin.mkdir()
    _run("git", "init", "--bare", "--initial-branch=master", str(origin), check=True)
    _run("git", "clone", str(origin), str(work), check=True)
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    _git("config", "commit.gpgsign", "false", cwd=work)
    return tmp, origin, work


def _commit(work: Path, path: str, content: str, msg: str) -> None:
    p = work / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git("add", path, cwd=work)
    _git("commit", "-m", msg, cwd=work)


# Master version: small section.
MASTER_V1 = textwrap.dedent(
    """\
    # Section A

    ## Goal

    Implement the thing.

    ## Plan

    Step 1.
    Step 2.
    Step 3.
    """
)

# Staging version: master's content verbatim, plus appended implementation notes.
STAGING_SUPERSET = MASTER_V1 + textwrap.dedent(
    """\

    ## Implementation Notes (actuals)

    Shipped 2026-04-11.
    Caveats: none.
    """
)

# Master gets a non-trivial follow-up: adds a new "Risks" section.
MASTER_V2 = MASTER_V1 + textwrap.dedent(
    """\

    ## Risks

    None known.
    """
)


def _bootstrap_master_with_section(work: Path) -> None:
    _commit(work, "README.md", "init\n", "init")
    _commit(work, "docs/plans/sections/section-a.md", MASTER_V1, "section-a v1")
    _git("push", "origin", "master", cwd=work)


def _create_staging_with_appended_notes(work: Path) -> None:
    _git("checkout", "-b", "staging", cwd=work)
    _commit(work, "docs/plans/sections/section-a.md", STAGING_SUPERSET,
            "staging: append impl notes to section-a")
    _git("push", "origin", "staging", cwd=work)
    _git("checkout", "master", cwd=work)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class TestArgParsing:
    def test_unknown_flag_still_rejected(self):
        # Sanity: unknown flags still bail with exit 2.
        r = _run("bash", str(SCRIPT), "--definitely-not-a-flag")
        assert r.returncode == 2
        assert "unknown arg" in r.stderr

    def test_new_flag_recognized(self, tmp_path):
        # The new flag should not trigger "unknown arg". It will fail later
        # because we're not in a real CAS repo, but the parser must accept it.
        r = _run("bash", str(SCRIPT), "--prefer-staging-on-docs-superset",
                 "--dry-run", cwd=tmp_path)
        assert "unknown arg" not in r.stderr


# ---------------------------------------------------------------------------
# End-to-end auto-resolve happy path
# ---------------------------------------------------------------------------

@pytest.mark.requires_tools("git")
class TestAutoResolveHappyPath:
    def test_resolves_when_staging_is_strict_superset(self):
        tmp, origin, work = _setup_repo()
        try:
            _bootstrap_master_with_section(work)
            _create_staging_with_appended_notes(work)

            # Master moves forward with a non-superset change.
            _commit(work, "docs/plans/sections/section-a.md", MASTER_V2,
                    "section-a v2: add Risks")
            _git("push", "origin", "master", cwd=work)

            # WITHOUT the flag, the script should bail on the conflict.
            r = _run("bash", str(SCRIPT), "--no-push", cwd=work)
            assert r.returncode != 0, f"expected bail without flag\n{r.stdout}\n{r.stderr}"
            assert "merge conflict" in r.stderr or "conflict" in r.stderr.lower()
            # Branch must be restored.
            head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=work).stdout.strip()
            assert head == "master"

            # WITH the flag, the staging-superset case should NOT auto-resolve
            # because master added a new "Risks" section that staging is missing.
            r = _run("bash", str(SCRIPT), "--prefer-staging-on-docs-superset",
                     "--no-push", cwd=work)
            assert r.returncode != 0
            assert "NOT a strict superset" in r.stderr
            head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=work).stdout.strip()
            assert head == "master"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_resolves_when_staging_truly_supersets_master(self):
        """Master modifies section-a in a way whose lines are ALL still present
        in staging's version (e.g. master's change is a strict subset of
        staging's; staging has staging-only notes appended). Auto-resolve
        should keep staging."""
        tmp, origin, work = _setup_repo()
        try:
            _bootstrap_master_with_section(work)

            # Make staging carry STAGING_SUPERSET (= master + notes).
            _create_staging_with_appended_notes(work)

            # Master makes a tiny edit whose every resulting line is already
            # in STAGING_SUPERSET. We do this by re-writing master's file to a
            # smaller subset of its own current content; every line still
            # exists on staging because staging contains MASTER_V1 verbatim
            # plus the notes.
            #
            # Concretely: master keeps lines 1..N of MASTER_V1 (a true
            # subset). Every line of master's new content is a line of
            # MASTER_V1 → which is a line of STAGING_SUPERSET. ✓
            master_subset = "\n".join(MASTER_V1.splitlines()[:5]) + "\n"
            _commit(work, "docs/plans/sections/section-a.md", master_subset,
                    "section-a: trim to subset")
            _git("push", "origin", "master", cwd=work)

            r = _run("bash", str(SCRIPT), "--prefer-staging-on-docs-superset",
                     "--no-push", cwd=work)
            assert r.returncode == 0, (
                f"expected auto-resolve to succeed\nstdout:\n{r.stdout}\n"
                f"stderr:\n{r.stderr}"
            )
            assert "auto-resolved (kept staging)" in r.stdout
            assert "docs/plans/sections/section-a.md" in r.stdout

            # Staging branch on origin should now have STAGING_SUPERSET content
            # (we kept --ours = staging's version). Check via git show.
            # Note: we used --no-push, so check local staging.
            content = _git("show", "staging:docs/plans/sections/section-a.md",
                           cwd=work).stdout
            assert content == STAGING_SUPERSET
            # And we should be back on master at exit.
            head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=work).stdout.strip()
            assert head == "master"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Auto-resolve refused on non-docs path
# ---------------------------------------------------------------------------

@pytest.mark.requires_tools("git")
class TestAutoResolveRefusedOutsideDocsPlans:
    def test_refuses_on_non_docs_conflict(self):
        tmp, origin, work = _setup_repo()
        try:
            _commit(work, "README.md", "init\n", "init")
            _commit(work, "scripts/foo.sh", "#!/bin/bash\necho a\n", "scripts: foo v1")
            _git("push", "origin", "master", cwd=work)

            _git("checkout", "-b", "staging", cwd=work)
            # Staging extends scripts/foo.sh (so it's a "superset" line-wise),
            # but the path is OUTSIDE docs/plans/**; gate 1 must refuse.
            _commit(work, "scripts/foo.sh",
                    "#!/bin/bash\necho a\necho b\n",
                    "staging: extend foo.sh")
            _git("push", "origin", "staging", cwd=work)
            _git("checkout", "master", cwd=work)

            # Master makes a conflicting change to scripts/foo.sh.
            _commit(work, "scripts/foo.sh",
                    "#!/bin/bash\necho z\n",
                    "master: replace foo.sh contents")
            _git("push", "origin", "master", cwd=work)

            r = _run("bash", str(SCRIPT), "--prefer-staging-on-docs-superset",
                     "--no-push", cwd=work)
            assert r.returncode != 0
            assert "non-docs path" in r.stderr
            head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=work).stdout.strip()
            assert head == "master"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Default behavior (no flag) still bails
# ---------------------------------------------------------------------------

@pytest.mark.requires_tools("git")
class TestDefaultBehaviorBailsOnConflict:
    def test_no_flag_still_aborts_on_conflict(self):
        tmp, origin, work = _setup_repo()
        try:
            _bootstrap_master_with_section(work)
            _create_staging_with_appended_notes(work)
            # Master appends a different "Risks" tail, conflicting with the
            # tail staging already appended (same insertion point → conflict).
            _commit(work, "docs/plans/sections/section-a.md", MASTER_V2,
                    "master: append Risks")
            _git("push", "origin", "master", cwd=work)

            # No flag → conflict aborts as today.
            r = _run("bash", str(SCRIPT), "--no-push", cwd=work)
            assert r.returncode != 0
            assert "auto-resolved" not in r.stdout
            head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=work).stdout.strip()
            assert head == "master"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
