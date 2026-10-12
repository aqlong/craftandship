"""Regression tests for executor push + PR verification (2026-05-26).

Driver: 7 fictional in_review items observed on 2026-05-26 (since
reverted), weeks after the last PR that really merged. Root cause was the
HIGH-risk PR path in scripts/cas-roadmap-loop.sh using `|| true` after both
`git push origin "$branch"` and `gh pr create`, silently swallowing
failures and marking the roadmap item in_review regardless.

These tests are structural invariants on the shell script. They catch
accidental reintroduction of the `|| true` pattern OR removal of the
revert-on-failure block during refactors.
"""

import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "cas-roadmap-loop.sh"
)


@pytest.fixture(scope="module")
def script_text():
    return SCRIPT_PATH.read_text()


class TestPushPrSourcesCleanly:
    """The script must remain bash-parseable after the fix."""

    def test_bash_syntax_ok(self):
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"bash -n failed: {result.stderr}"
        )


class TestNoSilentPushFailure:
    """The exact failure-swallowing patterns must not reappear."""

    def test_no_push_origin_branch_or_true(self, script_text):
        """The exact offending push pattern from 2026-05-26 is gone."""
        offending = 'git push origin "$branch" >> "$log_file" 2>&1 || true'
        assert offending not in script_text, (
            "Reintroduced the silent-failure git push pattern that "
            "caused 7 fictional in_review items on 2026-05-26."
        )

    def test_no_gh_pr_create_or_true(self, script_text):
        """gh pr create must not be followed by `|| true` (would swallow PR
        failures and still mark the item in_review)."""
        # Find every gh pr create invocation and assert the line group does
        # not terminate with `|| true`. The invocation spans multiple lines
        # via backslash-continuation; we collapse continuations before checking.
        collapsed = script_text.replace("\\\n", " ")
        for line in collapsed.split("\n"):
            stripped = line.strip()
            if "gh pr create" not in stripped:
                continue
            assert "|| true" not in stripped, (
                f"gh pr create line must not swallow failures: {stripped}"
            )


class TestPushUsesCapturedExitCode:
    """The HIGH-risk path must capture exit codes for both push + PR create."""

    def test_push_ok_and_pr_ok_locals_declared(self, script_text):
        """The fix introduces local push_ok + pr_ok flags."""
        assert "local push_ok=0 pr_ok=0" in script_text, (
            "Expected `local push_ok=0 pr_ok=0` declaration in HIGH-risk path"
        )

    def test_push_uses_cas_executor_and_retry(self, script_text):
        """Push must use CAS_EXECUTOR=1 (bypass staging-workflow block on
        auto/* branches) + the retry wrapper (matches fast-path pattern), run
        under the push time limit, and push the tested commit by its hash,
        not the branch name."""
        assert (
            'CAS_EXECUTOR=1 retry 3 10 with_timeout "$PUSH_TIMEOUT_SEC" '
            'git push origin "$tested_sha:refs/heads/$branch"'
            in script_text
        ), (
            "Expected CAS_EXECUTOR=1 + retry on the HIGH-risk push, pushing "
            "the tested commit to the branch ref."
        )
        assert 'git push origin "$branch"' not in script_text, (
            "The draft-PR path must not push whatever the branch points at now."
        )

    def test_push_success_sets_push_ok(self, script_text):
        """The push-success branch must set push_ok=1."""
        assert "push_ok=1" in script_text

    def test_pr_create_gated_on_push_ok(self, script_text):
        """gh pr create must only run when push_ok=1 (avoid creating a PR
        for an unpushed branch)."""
        # The block structure: `if [ "$push_ok" -eq 1 ]; then` immediately
        # before the gh pr create invocation.
        idx_push_check = script_text.find('if [ "$push_ok" -eq 1 ]; then')
        idx_gh_pr = script_text.find("gh pr create", idx_push_check)
        assert idx_push_check != -1, "Expected push_ok gate before gh pr create"
        assert idx_gh_pr != -1, "Expected gh pr create after push_ok gate"
        # No other "if" or "fi" between them at the same level; the gh pr
        # create must be inside the push_ok block.
        between = script_text[idx_push_check:idx_gh_pr]
        # Count fi tokens: there should be 0 between the push_ok if and the
        # gh pr create call.
        assert "\nfi\n" not in between and not between.strip().endswith("fi"), (
            "gh pr create must be inside the push_ok=1 conditional block"
        )

    def test_pr_success_sets_pr_ok(self, script_text):
        """The PR-success branch must set pr_ok=1."""
        assert "pr_ok=1" in script_text


class TestRevertOnFailure:
    """On push or PR failure, the roadmap item must revert to pending +
    drift note, the failure must be recorded, and the worktree cleaned."""

    def test_failure_check_uses_both_flags(self, script_text):
        """Failure branch must trigger when either push_ok or pr_ok is 0."""
        assert (
            '[ "$push_ok" -ne 1 ] || [ "$pr_ok" -ne 1 ]' in script_text
        ), "Expected combined push_ok/pr_ok failure check"

    def test_failure_branch_reverts_status_to_pending(self, script_text):
        """The failure branch must record status 'pending' with its note
        (through record_roadmap_status, whose commit is tested by running the
        loop in test_executor_loop_behavior.py)."""
        assert 'record_roadmap_status_locked "$item_id" pending "$log_file"' in script_text, (
            "Failure branch must explicitly record the roadmap item as "
            "'pending' so it gets re-picked on the next cycle."
        )

    def test_failure_branch_sets_status_drift_note(self, script_text):
        """The failure branch must add a status_drift_note for operator audit."""
        assert "status_drift_note" in script_text
        assert "push_or_pr_failed at" in script_text, (
            "Expected drift-note text 'push_or_pr_failed at <timestamp>' "
            "matching the spec in the roadmap item description."
        )

    def test_failure_branch_emits_failed_heartbeat(self, script_text):
        """A 'failed' heartbeat must be posted so the operator dashboard
        shows the silent-failure event."""
        # The heartbeat goes through post_heartbeat, which sends the fields
        # the heartbeat route stores (current_item, skipped_reason).
        assert 'post_heartbeat "failed" "$item_id" "Push or PR creation failed"' in script_text, (
            "Expected a failed-status heartbeat naming the item in the failure branch"
        )
        assert "Push or PR creation failed" in script_text, (
            "Expected the heartbeat detail text matching the spec"
        )

    def test_failure_branch_calls_record_failure(self, script_text):
        """record_failure must be invoked so cas-pick-item's defer logic
        eventually skips a chronically-failing item."""
        # record_failure is called in several failure branches; the test
        # just asserts it's present somewhere in the script (it already is)
        # AND that the new failure branch references it. We approximate by
        # checking it's near the new failure detail string.
        idx_detail = script_text.find("Push or PR creation failed")
        assert idx_detail != -1
        # Search backward 1500 chars for record_failure
        window = script_text[max(0, idx_detail - 1500):idx_detail + 500]
        assert "record_failure" in window, (
            "record_failure must be invoked in the push/PR failure branch"
        )

    def test_failure_branch_cleans_up_worktree(self, script_text):
        """The worktree + branch must be cleaned up on failure (matches
        every other failure branch in execute_item)."""
        idx_detail = script_text.find("Push or PR creation failed")
        assert idx_detail != -1
        window = script_text[max(0, idx_detail - 2000):idx_detail + 500]
        assert "git worktree remove --force" in window
        assert "git branch -D" in window

    def test_failure_branch_returns_1(self, script_text):
        """Failure branch must return 1 (not 0) so the outer loop accounts
        for it as a failed item."""
        idx_detail = script_text.find("Push or PR creation failed")
        assert idx_detail != -1
        # The return 1 should be very close after the post_result call.
        tail = script_text[idx_detail:idx_detail + 600]
        assert "return 1" in tail


class TestSuccessPathUnchanged:
    """The success path (both push + PR ok) must still mark in_review and
    commit the roadmap update, the existing behavior is preserved."""

    def test_success_path_marks_in_review(self, script_text):
        """The in_review marker remains for the success path."""
        assert 'record_roadmap_status_locked "$item_id" in_review "$log_file"' in script_text

    def test_success_path_returns_review_result(self, script_text):
        """post_result with 'review' status remains on the success path."""
        assert (
            'post_result "$item_id" "$item_title" "review" "PR created for manual review"'
            in script_text
        )
