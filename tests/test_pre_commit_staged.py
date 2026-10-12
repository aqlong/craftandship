"""The pre-commit hook judges what is being committed, not the working tree.

.githooks/pre-commit has four checks that read a file: the destructive-DDL
check on web/src/db/schema.sql, the JSON check on cli/failure_kb.json, the
brand-voice lint on customer-facing copy and the SEO consistency lint. Each
used to open the file in the working tree. After `git add -p`, or after any
edit made since `git add`, the working tree is not what the commit holds: a
staged schema that drops a table went through when the working copy was
clean, and a clean staged schema was refused when the working copy was not.

Every test here runs the real hook, with the real scripts it calls, through
`git commit` in a throwaway repository, once in each direction: staged bad
and working tree good must be refused; staged good and working tree bad must
be committed, and the commit must hold the good content.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
SEO_FILES = ("web/src/app.ts", "web/src/services/structured-data.ts", "web/src/services/pricing-faq.ts",
             "web/views/about-ocean.ejs")
SCHEMA = "web/src/db/schema.sql"
FAILURE_KB = "cli/failure_kb.json"
VIEW = "web/views/page.ejs"
EM_DASH = chr(0x2014)

pytestmark = pytest.mark.skipif(not all(shutil.which(t) for t in ("git", "python3")),
                                reason="needs git and python3 on PATH")

GOOD_SCHEMA = "CREATE TABLE IF NOT EXISTS widgets (id SERIAL PRIMARY KEY);\n"
BAD_SCHEMA = GOOD_SCHEMA + "DROP\nTABLE widgets;\n"


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "TMPDIR": str(root.parent / "hook-tmp"),
        }
        (root.parent / "hook-tmp").mkdir()
        for directory in (".githooks", "scripts"):
            (root / directory).mkdir(parents=True)
        shutil.copy(REPO_ROOT / ".githooks" / "pre-commit", root / ".githooks" / "pre-commit")
        (root / ".githooks" / "pre-commit").chmod(0o755)
        for name in ("check_destructive_ddl.py", "check_brand_voice.py", "check_seo_consistency.py"):
            shutil.copy(SCRIPTS / name, root / "scripts" / name)
        for rel in SEO_FILES:
            if (REPO_ROOT / rel).is_file():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(REPO_ROOT / rel, root / rel)
        self.write(SCHEMA, GOOD_SCHEMA)
        self.write(FAILURE_KB, "{}\n")
        self.write(VIEW, "<p>Plain copy.</p>\n")
        self.git("init", "-q", "-b", "master")
        self.git("config", "core.hooksPath", ".githooks")
        self.git("add", "-A")
        first = self.git("commit", "-q", "-m", "start", check=False)
        assert first.returncode == 0, f"the hook refuses the tree as it is:\n{first.stdout}{first.stderr}"

    def git(self, *args, check=True):
        return subprocess.run(["git", *args], cwd=self.root, env=self.env, capture_output=True, text=True,
                              check=check, timeout=120)

    def write(self, rel, content):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def read(self, rel):
        return (self.root / rel).read_text()

    def stage_then_edit(self, rel, staged, working):
        """Stage `staged` as the content of `rel`, then leave `working` in the working tree."""
        self.write(rel, staged)
        self.git("add", rel)
        self.write(rel, working)

    def commit(self):
        return self.git("commit", "-q", "-m", "change", check=False)

    def committed(self, rel):
        return self.git("show", f"HEAD:{rel}").stdout

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def leftovers(self):
        return list((self.root.parent / "hook-tmp").iterdir())


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "repo")


def _refused(repo, rel, bad, good, message):
    before = repo.head()
    repo.stage_then_edit(rel, staged=bad, working=good)
    result = repo.commit()
    assert result.returncode != 0, "the commit holds the bad content and went through"
    assert message in result.stdout + result.stderr, result.stdout + result.stderr
    assert repo.head() == before
    assert repo.leftovers() == [], "the hook left its scratch directory behind"


def _accepted(repo, rel, bad, good):
    repo.stage_then_edit(rel, staged=good, working=bad)
    result = repo.commit()
    assert result.returncode == 0, result.stdout + result.stderr
    assert repo.committed(rel) == good
    assert repo.read(rel) == bad, "the working tree is left as it was"
    assert repo.leftovers() == []


def test_a_staged_schema_that_drops_a_table_is_refused_whatever_the_working_tree_says(repo):
    _refused(repo, SCHEMA, bad=BAD_SCHEMA, good=GOOD_SCHEMA + "-- edited back\n",
             message=f"DESTRUCTIVE DDL detected in {SCHEMA} (as staged)")


def test_a_clean_staged_schema_is_committed_whatever_the_working_tree_says(repo):
    _accepted(repo, SCHEMA, bad=BAD_SCHEMA, good=GOOD_SCHEMA + "ALTER TABLE widgets ADD COLUMN IF NOT EXISTS name TEXT;\n")


LIVE_BLOCK = "DO $m$\nBEGIN\n  UPDATE builds SET status = 'live' WHERE id IS NOT NULL;\nEND\n$m$;\n"
ROW_CHANGE_BLOCK = "DO $$\nBEGIN\n  {reason}UPDATE widgets SET name = 'x' WHERE name IS NULL;\nEND $$;\n"


def test_a_staged_tagged_block_that_sets_a_build_live_is_refused_whatever_the_working_tree_says(repo):
    # The case a reader that toggled on $$ alone passed: the body of a block
    # with a tag is opaque to it. The hook reads the staged file as SQL, and
    # nothing acknowledges the status live.
    _refused(repo, SCHEMA, bad=GOOD_SCHEMA + LIVE_BLOCK, good=GOOD_SCHEMA + "-- edited back\n",
             message="NAMES THE STATUS live")


def test_a_reason_does_not_settle_the_status_live(repo):
    reason = "  -- SAFE: backfill\n  UPDATE"
    _refused(repo, SCHEMA, bad=GOOD_SCHEMA + LIVE_BLOCK.replace("  UPDATE", reason), good=GOOD_SCHEMA,
             message="NAMES THE STATUS live")


def test_a_staged_row_change_without_a_reason_of_its_own_is_refused(repo):
    _refused(repo, SCHEMA, bad=GOOD_SCHEMA + ROW_CHANGE_BLOCK.format(reason=""), good=GOOD_SCHEMA,
             message="[UPDATE]")
    # a reason above the block does not speak for a statement inside it
    above_the_block = "-- SAFE: backfill\n" + ROW_CHANGE_BLOCK.format(reason="")
    _refused(repo, SCHEMA, bad=GOOD_SCHEMA + above_the_block, good=GOOD_SCHEMA, message="[UPDATE]")


def test_a_staged_row_change_with_a_reason_of_its_own_is_committed(repo):
    reason = "-- SAFE: one-time backfill, the guard runs it once\n  "
    _accepted(repo, SCHEMA, bad=GOOD_SCHEMA + ROW_CHANGE_BLOCK.format(reason=""),
              good=GOOD_SCHEMA + ROW_CHANGE_BLOCK.format(reason=reason))


def test_a_staged_failure_kb_that_is_not_json_is_refused(repo):
    _refused(repo, FAILURE_KB, bad='{"rules": [\n', good='{"rules": []}\n',
             message=f"{FAILURE_KB} (as staged) is invalid JSON")


def test_a_valid_staged_failure_kb_is_committed(repo):
    _accepted(repo, FAILURE_KB, bad='{"rules": [\n', good='{"rules": []}\n')


def test_a_staged_view_with_an_em_dash_is_refused(repo):
    _refused(repo, VIEW, bad=f"<p>Before {EM_DASH} after.</p>\n", good="<p>Before, and after.</p>\n",
             message="Brand-voice lint failed")


def test_a_staged_view_without_one_is_committed(repo):
    _accepted(repo, VIEW, bad=f"<p>Before {EM_DASH} after.</p>\n", good="<p>Before, and after.</p>\n")


def test_a_view_in_a_directory_with_a_space_is_still_linted(repo):
    # the staged paths are handed over NUL-separated, not split on blanks
    rel = "web/views/two words/page.ejs"
    _refused(repo, rel, bad=f"<p>One {EM_DASH} two.</p>\n", good="<p>One, two.</p>\n",
             message="Brand-voice lint failed")


def _seo_file(repo):
    rel = "web/src/services/pricing-faq.ts"
    if not (repo.root / rel).is_file():
        pytest.skip(f"{rel} is not in this tree")
    return rel, repo.read(rel)


def test_staged_seo_copy_with_a_banned_phrase_is_refused(repo):
    rel, text = _seo_file(repo)
    bad = text + "\n// export const note = 'we will never hold your website hostage';\nexport const note = 'hostage';\n"
    _refused(repo, rel, bad=bad, good=text + "\n// a harmless edit\n", message="SEO consistency check failed")


def test_clean_staged_seo_copy_is_committed(repo):
    rel, text = _seo_file(repo)
    _accepted(repo, rel, bad=text + "\nexport const note = 'hostage';\n", good=text + "\n// a harmless edit\n")


def test_the_seo_lint_reads_the_other_files_from_the_index_too(repo):
    # Only a view is staged. The lint compares app.ts with the FAQ source;
    # an uncommitted, unstaged edit of the FAQ source in the working tree is
    # not part of this commit and must not decide it.
    rel, text = _seo_file(repo)
    repo.write(rel, text + "\nexport const note = 'hostage';\n")
    repo.write(VIEW, "<p>Other plain copy.</p>\n")
    repo.git("add", VIEW)
    result = repo.commit()
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_commit_that_touches_none_of_the_checked_files_runs_no_check(repo):
    repo.write("notes.txt", "nothing the hook looks at\n")
    repo.git("add", "notes.txt")
    # every script the hook could call is gone: a check that ran would fail
    for script in (repo.root / "scripts").iterdir():
        script.unlink()
    assert repo.commit().returncode == 0


def test_a_commit_of_one_path_judges_that_path_as_it_is_committed(repo):
    # `git commit -- <path>` commits the working-tree content of that path
    # through a temporary index. The hook must read that index: the content
    # being committed here is the bad one, although nothing was staged.
    repo.write(SCHEMA, BAD_SCHEMA)
    result = repo.git("commit", "-q", "-m", "one path", "--", SCHEMA, check=False)
    assert result.returncode != 0
    assert "DESTRUCTIVE DDL detected" in result.stdout + result.stderr
