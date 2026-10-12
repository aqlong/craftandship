"""What scripts/cas-roadmap-loop.sh leaves behind when it is stopped, and how
it keeps to its own limits. The real loop, run as in
test_executor_loop_behavior.py (whose sandbox, fake session and stand-in ops
API these tests use).

  - A signal or a kill at each step of the merge section: with the operator's
    edit in the pre-merge stash and nothing merged; after the merge; after the
    item's status commit; in the push before anything is sent; in the push
    once the remote has the commits. In each case the main checkout must end
    in a state the next cycle starts from: the operator's edit back in place,
    no stash, no unpushed merge that nothing accounts for, and the item either
    done on the remote or pending and worked again.
  - Giving up on a merge in the middle of a run (the status mark cannot be
    committed, the journal cannot be updated for the push): the merge is taken
    back only on the branch it was made on, only when it is the loop's own, and
    never over a person's edit; otherwise it stays, with its journal, and the
    next cycle starts nothing. A second merge is not stacked on an unsettled
    one.
  - A crash of the loop (an unbound variable, a failing command or pipeline)
    is written to the crash log with its phase and reported to the dashboard,
    and a loop that keeps crashing backs off for a window and then goes on.
  - The locks: of several executors started at the same moment on one state
    directory exactly one runs; a lock left by a dead process is taken over.
  - The time limits: the session's kill timer ends the whole process tree,
    a test that never returns fails the gate, and a start ends what a loop
    that was killed outright left running.

The loop is stopped at a chosen point with git's own hooks (Sandbox.hold_points),
not with a wrapper on PATH: the loop puts its own directories first on PATH.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time

import pytest

from tests.test_executor_loop_behavior import (  # noqa: F401  (sb is a fixture)
    FAKE_SLEEP,
    pytestmark,
    sb,
)


class _Loop:
    """One run of the loop with its output in a file. (A pipe would stay open
    for as long as any job the loop started is alive, and some of these tests
    kill the loop and leave its jobs running on purpose.)"""

    _count = 0

    def __init__(self, sb, mode, **env):
        _Loop._count += 1
        self.path = sb.root / f"loop-output-{_Loop._count}.txt"
        environment = {**sb.env, "RUN_ONCE": "1", "FAKE_MODE": mode, **env}
        with open(self.path, "w") as out:
            self.proc = subprocess.Popen(["bash", "scripts/cas-roadmap-loop.sh"], cwd=sb.repo,
                                         env=environment, stdout=out, stderr=subprocess.STDOUT)

    def poll(self):
        return self.proc.poll()

    def wait(self, timeout):
        return self.proc.wait(timeout=timeout)

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()

    @property
    def returncode(self):
        return self.proc.returncode

    def output(self):
        return self.path.read_text(errors="replace")


def _operator_edit(sb):
    """An uncommitted edit of the config, as scripts/cas-exec makes: the loop
    tolerates it and sets it aside in a stash for the merge."""
    config = sb.repo / "scripts" / "cas-executor-config.json"
    cfg = json.loads(config.read_text())
    cfg["interval_minutes"] = 7
    config.write_text(json.dumps(cfg, indent=2) + "\n")


def _operator_edit_is_back(sb):
    config = sb.repo / "scripts" / "cas-executor-config.json"
    return json.loads(config.read_text())["interval_minutes"] == 7 \
        and sb.dirty() == ["scripts/cas-executor-config.json"]


def _run_until_held(sb, point, mode="ok_hold"):
    """Start a cycle whose session commits a change and arms the hold point;
    return once the loop's git is waiting there."""
    proc = _Loop(sb, mode, HOLD_AT=point)
    reached = sb.wait_for(lambda: sb.held_at(point) is not None or proc.poll() is not None, timeout=90)
    if not reached or proc.poll() is not None:
        proc.kill()
        pytest.fail(f"the loop never reached {point}:\n{proc.output()}")
    return proc


def _parent_of(pid):
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else None


def _gone(pid):
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _stop(sb, proc, point, how, foreground):
    """Stop the loop while it is held at `point`. `how` is SIGTERM or SIGKILL.
    `foreground` says the held git is one the loop's shell is waiting for (a
    trapped SIGTERM is then acted on when that git returns, so the hold is
    released after the signal is sent); otherwise it is a job the loop waits
    for, and the trap runs at once."""
    held = sb.held_at(point)
    git_pid = _parent_of(held)
    os.kill(sb.loop_pid(), how)
    if how == signal.SIGKILL:
        proc.wait(timeout=30)
        # What the dead loop was waiting for is still there. End the hook and
        # let the git command that ran it finish by itself.
        try:
            os.kill(held, signal.SIGKILL)
        except ProcessLookupError:
            pass
        sb.release(point)
        assert sb.wait_for(lambda: _gone(git_pid), timeout=30), "the held git command never finished"
        assert sb.wait_for(lambda: not (sb.repo / ".git" / "index.lock").exists(), timeout=30)
    else:
        if foreground:
            time.sleep(0.5)
            sb.release(point)
        proc.wait(timeout=60)
        sb.release(point)
    return proc.output()


# point, is the held git a foreground command of the loop's shell
STEPS = {
    "before the merge": ("upload-pack", False),
    "after the merge, before the status commit": ("post-merge", True),
    "after the status commit, before the push": ("post-commit", True),
    "during the push, nothing sent yet": ("pre-push", False),
}


@pytest.mark.parametrize("step", sorted(STEPS))
@pytest.mark.parametrize("how", [signal.SIGTERM, signal.SIGKILL], ids=["SIGTERM", "SIGKILL"])
def test_a_loop_stopped_in_the_merge_section_leaves_a_checkout_the_next_cycle_recovers(sb, step, how):
    point, foreground = STEPS[step]
    sb.hold_points()
    _operator_edit(sb)
    before = sb.head()
    proc = _run_until_held(sb, point)
    assert sb.journal() is not None, "the merge section is entered with a journal"
    assert sb.stashes(), "the operator's edit is in the pre-merge stash at this point"
    _stop(sb, proc, point, how, foreground)
    assert sb.origin_head() == before, "nothing reached the remote"

    if how == signal.SIGTERM and point != "pre-push":
        # Settled on the way out: nothing of this needs the network.
        assert sb.head() == before, "the unpushed merge is taken back"
        assert sb.stashes() == [] and _operator_edit_is_back(sb)
        assert sb.journal() is None
        assert sb.status("item-a") == "pending"
    elif point == "pre-push":
        # Whether the push got through cannot be known without asking the
        # remote, so nothing is taken back yet and the journal says so.
        assert sb.journal()["phase"] == "push"

    # The next cycle: settles what is left, then works the item again.
    second = sb.run("ok")
    assert second.returncode == 0, second.stdout
    assert sb.journal() is None, second.stdout
    assert sb.stashes() == [], second.stdout
    assert _operator_edit_is_back(sb), sb.dirty()
    assert sb.committed_item("item-a", ref="refs/heads/master")["status"] == "completed", second.stdout
    assert sb.origin_head() == sb.head()
    assert sb.invocations() == 2, "the item was worked again"
    # one merge of the item on the branch, not two
    merges = [line for line in sb.subjects("refs/heads/master") if line.startswith("Merge auto/item-a")]
    assert len(merges) == 1, sb.subjects("refs/heads/master")
    assert not sb.crash_log_nonempty() or how == signal.SIGKILL


def test_sigterm_once_the_remote_has_the_push_keeps_it_and_does_not_work_the_item_again(sb):
    # The remote has updated its branch and is still in its post-receive hook;
    # the client has not been told. SIGTERM ends the push. The checkout holds
    # the merge and the status commit, and cannot tell from its own refs that
    # the remote has them.
    sb.hold_points()
    _operator_edit(sb)
    before = sb.head()
    proc = _run_until_held(sb, "post-receive")
    _stop(sb, proc, "post-receive", signal.SIGTERM, foreground=False)
    pushed = sb.origin_head()
    assert pushed != before and pushed == sb.head(), "the remote took the merge"
    assert sb.journal()["phase"] == "push", "not settled offline: the local refs do not show the push"

    second = sb.run("ok")
    assert second.returncode == 0, second.stdout
    assert "Interrupted merge of item-a settled: pushed" in second.stdout + sb.logs()
    assert sb.journal() is None and sb.stashes() == [] and _operator_edit_is_back(sb)
    # the first merge stays, and the cycle went on to the next item
    assert sb.git("merge-base", "--is-ancestor", pushed, "refs/heads/master", check=False).returncode == 0
    assert sb.committed_item("item-a", ref="refs/heads/master")["status"] == "completed"
    assert sb.committed_item("item-b", ref="refs/heads/master")["status"] == "completed"
    assert sb.origin_head() == sb.head()
    merges = [line for line in sb.subjects("refs/heads/master") if line.startswith("Merge auto/item-a")]
    assert len(merges) == 1


def test_an_interrupted_merge_is_settled_before_the_config_is_read(sb):
    # The committed config says disabled; the operator enabled the executor
    # with an uncommitted edit. When the loop is stopped in the push, that
    # edit is in the pre-merge stash and the file in the checkout says
    # disabled. A cycle that read "enabled" first would never run again, and
    # never put the edit back.
    sb.set_config(enabled=False)
    config = sb.repo / "scripts" / "cas-executor-config.json"
    cfg = json.loads(config.read_text())
    cfg["enabled"] = True
    config.write_text(json.dumps(cfg, indent=2) + "\n")
    sb.hold_points()
    before = sb.head()
    proc = _run_until_held(sb, "pre-push")
    _stop(sb, proc, "pre-push", signal.SIGTERM, foreground=False)
    assert json.loads(config.read_text())["enabled"] is False, "the enabling edit is in the stash"
    assert sb.head() != before and sb.origin_head() == before

    second = sb.run("ok")
    assert json.loads(config.read_text())["enabled"] is True, second.stdout
    assert sb.stashes() == [] and sb.journal() is None
    assert sb.invocations() == 2, "the cycle ran once the edit was back"
    assert sb.origin_head() == sb.head()
    assert sb.committed_item("item-a", ref="refs/heads/master")["status"] == "completed"


def test_commits_the_loop_did_not_make_are_not_taken_back(sb):
    # After a kill in the push, a person commits on the target branch. The
    # journal's merge is no longer the tip: the loop must not reset somebody's
    # work away. It yields, with the reason, until a person has decided.
    sb.hold_points()
    proc = _run_until_held(sb, "pre-push")
    _stop(sb, proc, "pre-push", signal.SIGKILL, foreground=False)
    (sb.repo / "human.txt").write_text("made after the crash\n")
    sb.git("add", "human.txt")
    sb.git("commit", "-qm", "a person's commit on top")
    with_human = sb.head()

    second = sb.run("ok")
    assert second.returncode == 0, second.stdout
    assert sb.head() == with_human, "nothing was reset"
    assert sb.journal() is not None
    assert sb.invocations() == 1, "no session while the merge is unsettled"
    reasons = [h.get("skipped_reason") or "" for h in sb.ops.heartbeats()]
    assert any("commits this loop did not make" in reason for reason in reasons), reasons


SLOW_SET_STATUS = """import os, sys, time
if "--set-status" in sys.argv:
    open(os.path.join(os.environ["CAS_EXECUTOR_STATE_DIR"], "set-status-running"), "w").close()
    time.sleep(3)
"""


def test_sigterm_in_the_middle_of_a_status_write_does_not_leave_the_roadmap_uncommitted(sb):
    # Outside a merge: an item that failed for the last time is marked
    # deferred. The picker writes the status, the picker compares the file,
    # git commits it: three commands. A loop that stopped after the first
    # would leave roadmap.json changed and uncommitted, and every later cycle
    # would yield on it until a person stepped in. Here the signal arrives
    # while the first of the three is running (it is slowed down from outside,
    # through a sitecustomize on PYTHONPATH, to give the signal time to land).
    sb.set_config(max_total_failures_before_defer=1)
    slow = sb.root / "slow-picker"
    slow.mkdir()
    (slow / "sitecustomize.py").write_text(SLOW_SET_STATUS)
    # Not a single cycle: left alone, this loop would go on to the next item.
    proc = _Loop(sb, "fail", PYTHONPATH=str(slow), RUN_ONCE="0")
    try:
        assert sb.wait_for(lambda: (sb.state / "set-status-running").exists() or proc.poll() is not None,
                           timeout=60)
        assert proc.poll() is None, proc.output()
        os.kill(sb.loop_pid(), signal.SIGTERM)
        proc.wait(timeout=60)
    finally:
        proc.kill()

    assert sb.dirty() == [], "roadmap.json was left changed and uncommitted"
    item = sb.committed_item("item-a", ref="refs/heads/master")
    assert item["status"] == "deferred", proc.output()
    # The signal was held, not lost: once the write was whole the loop
    # stopped, before it started on another item.
    assert sb.invocations() == 1
    assert sb.committed_item("item-b", ref="refs/heads/master")["status"] == "pending"
    assert not sb.crash_log_nonempty()
    # and the next cycle is not stopped by anything the first left behind
    second = sb.run("ok")
    assert sb.committed_item("item-b", ref="refs/heads/master")["status"] == "completed", second.stdout


def test_an_interrupted_merge_is_not_settled_while_the_checkout_is_on_another_branch(sb):
    # After the kill a person switches the checkout to a branch of their own.
    # The journal is about the target branch: taking the merge back here would
    # move the person's branch and leave the target branch as it is.
    sb.hold_points()
    proc = _run_until_held(sb, "pre-push")
    _stop(sb, proc, "pre-push", signal.SIGKILL, foreground=False)
    merged = sb.head()
    sb.git("checkout", "-q", "-b", "side")

    second = sb.run("ok")
    assert second.returncode == 0, second.stdout
    assert sb.head() == merged, "the target branch was not touched"
    assert sb.git("rev-parse", "refs/heads/side").stdout.strip() == merged, "and neither was the person's"
    assert sb.journal() is not None
    assert sb.invocations() == 1
    assert any("is not on master" in (h.get("skipped_reason") or "") for h in sb.ops.heartbeats())

    sb.git("checkout", "-q", "master")
    third = sb.run("ok")
    assert sb.journal() is None, third.stdout
    assert sb.committed_item("item-a", ref="refs/heads/master")["status"] == "completed"
    assert sb.origin_head() == sb.head()


def test_an_edit_made_after_the_crash_survives_the_recovery(sb):
    # The loop is killed in the push. Before it starts again the operator
    # edits the failure KB (a file the loop tolerates as dirty, and one the
    # interrupted merge did not touch). Taking the merge back must not take
    # that edit with it.
    sb.hold_points()
    _operator_edit(sb)
    before = sb.head()
    proc = _run_until_held(sb, "pre-push")
    _stop(sb, proc, "pre-push", signal.SIGKILL, foreground=False)
    kb = sb.repo / "cli" / "failure_kb.json"
    kb.write_text('{"written": "after the crash"}\n')

    second = sb.run("ok")
    assert "Interrupted merge of item-a settled: taken-back" in second.stdout, second.stdout
    assert json.loads(kb.read_text()) == {"written": "after the crash"}
    assert json.loads((sb.repo / "scripts" / "cas-executor-config.json").read_text())["interval_minutes"] == 7
    assert sb.stashes() == [] and sb.journal() is None
    assert sb.dirty() == ["cli/failure_kb.json", "scripts/cas-executor-config.json"]
    assert sb.committed_item("item-a", ref="refs/heads/master")["status"] == "completed"
    assert sb.origin_head() == sb.head() != before


def test_a_push_that_timed_out_after_the_remote_took_it_is_not_taken_back(sb):
    # The remote accepts the push and then sits in its post-receive hook past
    # the loop's time limit, so the first attempt is ended by the timer. The
    # two retries do not get out of the client at all (a pre-push hook that
    # lets only the first attempt through stands in for a connection that
    # went away). The push has failed three times and the remote has the
    # commits all the same. Resetting the local branch would leave it behind
    # the remote, with the item marked pending.
    hook = sb.origin / "hooks" / "post-receive"
    hook.write_text("#!/bin/sh\ncat > /dev/null\nexec /bin/sleep 120\n")
    hook.chmod(0o755)
    hooks = sb.root / "client-hooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text(
        '#!/bin/sh\ncat > /dev/null\ncount="$CAS_EXECUTOR_STATE_DIR/push-attempts"\n'
        'n=$(( $(cat "$count" 2>/dev/null || echo 0) + 1 ))\necho "$n" > "$count"\n[ "$n" -eq 1 ]\n')
    (hooks / "pre-push").chmod(0o755)
    sb.git("config", "core.hooksPath", str(hooks))
    before = sb.head()
    result = sb.run("ok", timeout=120, CAS_EXECUTOR_PUSH_TIMEOUT_SEC="2.5")
    assert (sb.state / "push-attempts").read_text().strip() == "3"
    assert "git push FAILED after retries" in result.stdout, result.stdout
    assert "The remote has the merge after all" in result.stdout, result.stdout
    assert sb.origin_head() == sb.head() != before
    assert sb.status("item-a") == "completed"
    assert sb.journal() is None and sb.dirty() == []
    assert [r["status"] for r in sb.ops.results()] == ["completed"]


def test_a_failed_push_is_not_taken_back_while_the_remote_cannot_be_asked(sb):
    # The push fails, and so does the question to the remote: from the first
    # push attempt on, the connection is gone in both directions (the client's
    # pre-push hook refuses and marks the remote as down; the remote's side of
    # a fetch then refuses too). Nothing is reset on a guess: the merge stays,
    # the journal stays, and the next cycle starts no session until the remote
    # has answered.
    hooks = sb.root / "client-hooks"
    hooks.mkdir()
    pre_push = hooks / "pre-push"
    pre_push.write_text('#!/bin/sh\ncat > /dev/null\n: > "$CAS_EXECUTOR_STATE_DIR/remote-is-down"\nexit 1\n')
    pre_push.chmod(0o755)
    upload_pack = sb.root / "upload-pack"
    upload_pack.write_text('#!/bin/sh\n[ -e "$CAS_EXECUTOR_STATE_DIR/remote-is-down" ] && exit 1\n'
                           'exec git upload-pack "$@"\n')
    upload_pack.chmod(0o755)
    sb.git("config", "core.hooksPath", str(hooks))
    sb.git("config", "remote.origin.uploadpack", str(upload_pack))
    before = sb.head()

    first = sb.run("ok")
    assert "git push FAILED after retries" in first.stdout, first.stdout
    assert "Not settled" in first.stdout, first.stdout
    merged = sb.head()
    assert merged != before and sb.origin_head() == before
    assert sb.journal()["phase"] == "push"
    assert [r["result"] for r in sb.ops.results()] == ["Push unsettled"]
    assert "item-a" not in sb.failures(), "an outage is not counted against the item"

    second = sb.run("ok")
    assert sb.head() == merged and sb.journal() is not None, second.stdout
    assert sb.invocations() == 1, "no session while the merge is unsettled"
    assert any("interrupted merge not settled" in (h.get("skipped_reason") or "") for h in sb.ops.heartbeats())

    # The remote is back, and it does not have the merge: it is taken back and
    # the item is worked again.
    (sb.state / "remote-is-down").unlink()
    pre_push.write_text("#!/bin/sh\ncat > /dev/null\nexit 0\n")
    third = sb.run("ok")
    assert "Interrupted merge of item-a settled: taken-back" in third.stdout, third.stdout
    assert sb.journal() is None
    assert sb.invocations() == 2
    assert sb.committed_item("item-a", ref="refs/heads/master")["status"] == "completed"
    assert sb.origin_head() == sb.head()
    assert merged not in sb.git("rev-list", "refs/heads/master").stdout.split()


# ---------------------------------------------------------------------------
# Giving up on a merge in the middle of a run
# ---------------------------------------------------------------------------
# Two places in execute_item take the merge back without a signal: when the
# item's "completed" mark cannot be committed, and when the journal cannot be
# told that the push is starting. Both go through take_back_merge, the routine
# the settle step uses. Each test below stops the loop at one of the two
# moments (a git hook holds it there), changes the checkout the way a person
# working in it can, and then lets it go on.

def _refuse_roadmap_commits(sb):
    """A pre-commit hook next to the hold hooks that refuses the commit that
    marks an item completed (the commit of the session's own change, in its
    worktree, is not refused)."""
    hook = sb.root / "hold-hooks" / "pre-commit"
    hook.write_text('#!/bin/sh\nif git diff --cached --name-only | grep -qx coordinator/roadmap.json; then\n'
                    '  echo "sandbox hook: no roadmap commits"; exit 1\nfi\n')
    hook.chmod(0o755)


def _block_journal_update(sb):
    """Make the loop's next replacement of the merge journal fail: the
    temporary file it writes first is a directory."""
    (sb.repo / "logs" / "executor" / f"merge-in-progress.tmp.{sb.loop_pid()}").mkdir()


def _nothing(sb):
    return None


# what is done before the cycle starts, the hold point, what is done there, and
# the result the loop reports to the dashboard when it does take the merge back
GIVING_UP = {
    "the status mark cannot be committed": (_refuse_roadmap_commits, "post-merge", _nothing, "Roadmap mark failed"),
    "the journal cannot be updated for the push": (_nothing, "post-commit", _block_journal_update, "Journal failed"),
}


@pytest.mark.parametrize("giving_up", sorted(GIVING_UP))
def test_a_merge_the_loop_gives_up_on_is_taken_back_without_discarding_a_persons_edit(sb, giving_up):
    # A person edits a tracked file (one the merge does not touch) while the
    # loop is between its merge and its push. Taking the merge back used to be
    # `git reset --hard`, which took the edit with it.
    prepare, point, at_hold, result = GIVING_UP[giving_up]
    sb.hold_points()
    prepare(sb)
    before = sb.head()
    proc = _run_until_held(sb, point)
    (sb.repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n# my edit, made during the run\n")
    at_hold(sb)
    sb.release(point)
    proc.wait(timeout=90)

    assert sb.head() == before and sb.origin_head() == before, proc.output()
    assert "my edit" in (sb.repo / "tests" / "test_ok.py").read_text(), "the edit was discarded"
    assert sb.dirty() == ["tests/test_ok.py"]
    assert sb.journal() is None and sb.stashes() == []
    assert [r["result"] for r in sb.ops.results()] == [result]
    assert sb.status("item-a") == "pending"


@pytest.mark.parametrize("giving_up", sorted(GIVING_UP))
def test_a_merge_the_loop_gives_up_on_is_not_taken_back_over_an_edit_it_would_overwrite(sb, giving_up):
    # The session's change is to tests/test_ok.py, and a person edits that same
    # file after the merge. Putting the branch back would overwrite the edit,
    # so the merge stays, the journal stays, the loop says so, and the next
    # cycle starts no session until a person has dealt with the edit.
    prepare, point, at_hold, _ = GIVING_UP[giving_up]
    sb.hold_points()
    prepare(sb)
    before = sb.head()
    proc = _run_until_held(sb, point, mode="edit_hold")
    test_file = sb.repo / "tests" / "test_ok.py"
    test_file.write_text(test_file.read_text() + "# my edit, made during the run\n")
    at_hold(sb)
    sb.release(point)
    proc.wait(timeout=90)
    out = proc.output()

    assert "NOT TAKEN BACK" in out and "git reset --keep refused" in out, out
    assert "my edit" in test_file.read_text(), "the edit was discarded"
    assert sb.head() != before and sb.origin_head() == before, "the merge stays where it is, unpushed"
    assert sb.journal() is not None
    assert [r["result"] for r in sb.ops.results()] == ["Merge not taken back"]
    assert "item-a" not in sb.failures(), "this is not the item's failure"
    assert not (sb.state / "cas-executor-merge.lock").exists(), "an item waiting for the merge lock is not left waiting"

    second = sb.run("ok")
    assert second.returncode == 0, second.stdout
    assert sb.invocations() == 1, "no session while the merge is unsettled"
    assert any("could not be taken back" in (h.get("skipped_reason") or "") for h in sb.ops.heartbeats())

    # The person has dealt with the edit: the next cycle takes the merge back
    # and works the item again.
    sb.git("checkout", "--", "tests/test_ok.py")
    (sb.root / "hold-hooks" / "pre-commit").unlink(missing_ok=True)
    third = sb.run("ok")
    assert "Interrupted merge of item-a settled: taken-back" in third.stdout, third.stdout
    assert sb.journal() is None
    assert sb.invocations() == 2
    assert sb.committed_item("item-a", ref="refs/heads/master")["status"] == "completed"
    assert sb.origin_head() == sb.head()


@pytest.mark.parametrize("giving_up", sorted(GIVING_UP))
def test_a_merge_the_loop_gives_up_on_is_not_taken_back_on_a_branch_that_is_not_the_target(sb, giving_up):
    # Between its merge and its push the loop finds the checkout on another
    # branch: a person switched to a branch of their own, with a commit the
    # target branch does not have. `git reset` moves whatever branch is
    # checked out, and the loop used to reset that branch to the commit the
    # target branch had before the merge, dropping the person's commit.
    prepare, point, at_hold, _ = GIVING_UP[giving_up]
    sb.hold_points()
    prepare(sb)
    sb.git("checkout", "-q", "-b", "mine")
    (sb.repo / "mine.txt").write_text("my own work\n")
    sb.git("add", "mine.txt")
    sb.git("commit", "-qm", "my own work")
    mine = sb.git("rev-parse", "refs/heads/mine").stdout.strip()
    sb.git("checkout", "-q", "master")
    before = sb.head()
    proc = _run_until_held(sb, point)
    sb.git("checkout", "-q", "mine")
    at_hold(sb)
    sb.release(point)
    proc.wait(timeout=90)
    out = proc.output()

    assert sb.git("rev-parse", "refs/heads/mine").stdout.strip() == mine, "the person's branch was moved"
    assert sb.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/mine"
    assert (sb.repo / "mine.txt").read_text() == "my own work\n"
    assert "NOT TAKEN BACK" in out and "is not on master" in out, out
    assert sb.head() != before and sb.origin_head() == before, "the merge stays on master, unpushed"
    assert sb.journal() is not None
    assert [r["result"] for r in sb.ops.results()] == ["Merge not taken back"]
    assert not (sb.state / "cas-executor-merge.lock").exists(), "an item waiting for the merge lock is not left waiting"

    second = sb.run("ok")
    assert second.returncode == 0, second.stdout
    assert sb.invocations() == 1, "no session while the merge is unsettled"
    assert any("is not on master" in (h.get("skipped_reason") or "") for h in sb.ops.heartbeats())
    assert sb.git("rev-parse", "refs/heads/mine").stdout.strip() == mine

    sb.git("checkout", "-q", "master")
    (sb.root / "hold-hooks" / "pre-commit").unlink(missing_ok=True)
    third = sb.run("ok")
    assert "Interrupted merge of item-a settled: taken-back" in third.stdout, third.stdout
    assert sb.journal() is None
    assert sb.committed_item("item-a", ref="refs/heads/master")["status"] == "completed"
    assert sb.origin_head() == sb.head()
    assert sb.git("rev-parse", "refs/heads/mine").stdout.strip() == mine


@pytest.mark.parametrize("giving_up", sorted(GIVING_UP))
def test_a_merge_is_not_taken_back_on_a_new_branch_made_at_the_merge(sb, giving_up):
    # The case the ownership check cannot see: a person starts a branch at the
    # merge commit (git checkout -b), so HEAD is the loop's own merge. Putting
    # that branch back would leave the target branch with the unpushed merge
    # and no journal to say so.
    prepare, point, at_hold, _ = GIVING_UP[giving_up]
    sb.hold_points()
    prepare(sb)
    before = sb.head()
    proc = _run_until_held(sb, point)
    merged = sb.head()
    sb.git("checkout", "-q", "-b", "side")
    at_hold(sb)
    side = sb.git("rev-parse", "refs/heads/side").stdout.strip()
    sb.release(point)
    proc.wait(timeout=90)
    out = proc.output()

    assert sb.git("rev-parse", "refs/heads/side").stdout.strip() == side, "the person's branch was moved"
    assert sb.head() == merged != before, "the merge stays on master"
    assert "NOT TAKEN BACK" in out and "is not on master" in out, out
    assert sb.journal() is not None and sb.origin_head() == before


def test_a_loop_killed_while_writing_the_status_mark_has_that_write_undone_with_the_merge(sb):
    # The kill lands after the merge, while the picker is about to write the
    # item's "completed" mark into roadmap.json: the picker finishes on its own
    # (it is not a child the kill reaches), so the file ends up changed and
    # uncommitted with the merge behind it. Taking the merge back has to put
    # the file back too, or every later cycle yields on a roadmap that differs
    # from HEAD.
    slow = sb.root / "slow-picker"
    slow.mkdir()
    (slow / "sitecustomize.py").write_text(SLOW_SET_STATUS)
    before = sb.head()
    proc = _Loop(sb, "ok", PYTHONPATH=str(slow))
    try:
        assert sb.wait_for(lambda: (sb.state / "set-status-running").exists() or proc.poll() is not None, timeout=90)
        assert proc.poll() is None, proc.output()
        assert sb.journal() is not None, "the status write of a merge runs inside the merge section"
        os.kill(sb.loop_pid(), signal.SIGKILL)
        proc.wait(timeout=30)
    finally:
        proc.kill()
    assert sb.wait_for(lambda: sb.dirty() == ["coordinator/roadmap.json"], timeout=30), \
        "the picker never wrote the mark, so this run proved nothing"
    assert sb.origin_head() == before

    second = sb.run("ok", PYTHONPATH="")
    assert "Interrupted merge of item-a settled: taken-back" in second.stdout, second.stdout
    assert sb.journal() is None and sb.stashes() == []
    assert sb.invocations() == 2, "the item was worked again, so nothing yielded on a changed roadmap"
    assert sb.committed_item("item-a", ref="refs/heads/master")["status"] == "completed"
    assert sb.origin_head() == sb.head()
    assert sb.dirty() == []


def test_a_second_merge_is_not_stacked_on_one_that_is_unsettled(sb):
    # With several items running at once, the next to reach the merge section
    # finds the journal of an earlier merge the loop could not settle. It must
    # not merge on top of that merge and replace the journal that says what is
    # in flight; here the journal appears while the session runs.
    before = sb.head()
    result = sb.run("plant_journal")
    assert "Not merging item-a: an earlier merge is unsettled" in result.stdout, result.stdout
    assert sb.head() == before and sb.origin_head() == before
    assert sb.journal()["item"] == "other", "the journal that was there is the one that is left"
    assert [r["result"] for r in sb.ops.results()] == ["Merge unsettled"]
    assert "item-a" not in sb.failures(), "an earlier merge's state is not this item's failure"
    assert not sb.auto_branches() and not list((sb.repo / ".worktrees").glob("auto-*"))

    # and the next cycle starts nothing while the journal cannot be settled
    second = sb.run("ok")
    assert sb.invocations() == 1, second.stdout
    assert any("interrupted merge not settled" in (h.get("skipped_reason") or "") for h in sb.ops.heartbeats())


def test_a_merge_that_is_not_the_loops_own_is_not_taken_back_when_the_status_mark_fails(sb):
    # A person commits on the target branch while the loop waits between its
    # merge and its status mark. The mark then fails (the pre-commit hook
    # refuses it). The branch holds a commit the loop did not make on top of
    # its merge, so the loop must not put the branch back and drop it.
    sb.hold_points()
    _refuse_roadmap_commits(sb)
    before = sb.head()
    proc = _run_until_held(sb, "post-merge")
    (sb.repo / "human.txt").write_text("a person's commit\n")
    sb.git("add", "human.txt")
    sb.git("commit", "-qm", "a person's commit on top of the merge")
    with_human = sb.head()
    sb.release("post-merge")
    proc.wait(timeout=90)
    out = proc.output()

    assert sb.head() == with_human, "the branch was reset"
    assert (sb.repo / "human.txt").exists()
    assert "NOT TAKEN BACK" in out and "commits this loop did not make" in out, out
    assert sb.origin_head() == before and sb.journal() is not None
    second = sb.run("ok")
    assert sb.head() == with_human and sb.invocations() == 1, second.stdout


# ---------------------------------------------------------------------------
# A loop that crashes says so
# ---------------------------------------------------------------------------
# Under launchd's KeepAlive a crashed loop is started again at once, so a crash
# that leaves no trace is a loop that fails in silence for as long as nobody
# looks. These tests put a fault into a copy of the loop, in the main program
# right after the cycle's phase is set, and run it.

CYCLE_START = '    ITERATION_PHASE="preflight"\n'

# what is put there, and which of the loop's `set` options it needs to take effect
FAULTS = {
    "an unbound variable (set -u)": ': "${CAS_TEST_UNSET_VARIABLE}"',
    "a failing command (set -e)": "false",
    "a failing pipeline (set -o pipefail)": "false | true",
}


def _put_a_fault_in_the_loop(sb, fault):
    script = sb.repo / "scripts" / "cas-roadmap-loop.sh"
    original = script.read_text()
    assert original.count(CYCLE_START) == 1
    script.write_text(original.replace(CYCLE_START, CYCLE_START + "    " + fault + "\n"))
    return original


def _crash_log(sb):
    path = sb.repo / "logs" / "executor" / "crash.log"
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_a_loop_that_crashes_writes_a_crash_log_line_with_its_phase_and_tells_the_dashboard(sb, fault):
    _put_a_fault_in_the_loop(sb, FAULTS[fault])
    sb.run("ok")
    lines = _crash_log(sb)
    assert len(lines) == 1, lines
    # the epoch comes first: the crash-loop detector reads it with awk
    assert re.fullmatch(r"\d{10} ts=.+ pid=\d+ exit=\d+ phase=preflight last_cmd=.*", lines[0]), lines[0]
    assert [h.get("status") for h in sb.ops.heartbeats()].count("crashed") == 1, sb.ops.heartbeats()
    assert not (sb.state / "cas-executor.lock").exists(), "a crashed loop still lets go of its lock"
    assert sb.invocations() == 0


def test_a_loop_that_keeps_crashing_backs_off_for_a_window_and_then_goes_on(sb):
    # Three crashes make the fourth start wait. The back-off is one sleep of
    # the whole window (the sandbox's sleep returns at once and writes down
    # what it was asked for), it is marked in the crash log and on stderr, and
    # the loop then runs: it does not exit, because KeepAlive would only start
    # it again.
    sleeper = sb.bin / "sleep"
    sleeper.write_text('#!/bin/bash\necho "$@" >> "$CAS_EXECUTOR_STATE_DIR/sleeps"\n' + FAKE_SLEEP.split("\n", 1)[1])
    original = _put_a_fault_in_the_loop(sb, ": \"${CAS_TEST_UNSET_VARIABLE}\"")
    for _ in range(3):
        sb.run("ok")
    assert len(_crash_log(sb)) == 3
    assert not (sb.state / "sleeps").exists(), "no back-off before the limit"
    (sb.repo / "scripts" / "cas-roadmap-loop.sh").write_text(original)

    result = sb.run("ok")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CRASH-LOOP DETECTED: 3 crashes in last 900s (limit=3)" in result.stderr, result.stderr
    assert any("CRASH-LOOP DETECTED" in line for line in _crash_log(sb))
    assert "900" in (sb.state / "sleeps").read_text().split(), "it slept one full window"
    assert sb.status("item-a") == "completed", "and then ran the cycle"
    assert len([l for l in _crash_log(sb) if "pid=" in l]) == 3, "the back-off is not itself a crash"


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

BARRIER_TR = """#!/bin/sh
# Holds every caller until the go file exists, then is tr. The loop's last
# external command before it takes the main lock is a tr (check_crash_loop
# counting crash-log lines), so every racer waits here and all are let go at
# the same instant: they idle until "ready" and then spin until "go".
: > "$CAS_EXECUTOR_STATE_DIR/at-barrier-$$"
while [ ! -e "$CAS_EXECUTOR_STATE_DIR/ready" ] && [ ! -e "$CAS_EXECUTOR_STATE_DIR/go" ]; do /bin/sleep 0.02; done
while [ ! -e "$CAS_EXECUTOR_STATE_DIR/go" ]; do :; done
exec /usr/bin/tr "$@"
"""


SLOW_LOCK_RM = """#!/bin/bash
case " $* " in
  *"cas-executor.lock "*) /bin/sleep "0.$(( RANDOM % 4 ))$(( RANDOM % 10 ))" ;;
esac
exec /bin/rm "$@"
"""


@pytest.mark.parametrize("stale_lock", [False, True], ids=["no lock yet", "started_at_once_over_a_stale_lock"])
def test_of_many_executors_started_at_once_exactly_one_runs(sb, stale_lock):
    # With a stale lock in place every racer first sees a dead owner. Taking
    # the lock over must be as exclusive as taking it fresh: a racer that
    # removes "the stale lock" a moment after another has replaced it would
    # be removing a live one. To make that moment wide enough to hit every
    # time, the rm that removes the lock file is slowed by a random fraction
    # of a second (SLOW_LOCK_RM): every racer has seen the dead owner long
    # before the first removal lands.
    # With no lock yet, the moment between "there is no lock" and "now there
    # is mine" cannot be widened from outside, so the race is simply run
    # several times: a lock that checks and then writes lets two racers
    # through in most rounds, not in every one.
    racers, rounds = 12, (1 if stale_lock else 4)
    sb.set_config(max_items_per_day=20)
    if stale_lock:
        (sb.bin / "rm").write_text(SLOW_LOCK_RM)
        (sb.bin / "rm").chmod(0o755)
    (sb.bin / "tr").write_text(BARRIER_TR)
    (sb.bin / "tr").chmod(0o755)
    crash_log = sb.repo / "logs" / "executor" / "crash.log"
    crash_log.parent.mkdir(parents=True, exist_ok=True)
    crash_log.write_text("1 ts=long-ago pid=1 exit=1 phase=startup last_cmd=x\n")
    for round_number in range(1, rounds + 1):
        for leftover in [*sb.state.glob("at-barrier-*"), sb.state / "ready", sb.state / "go"]:
            leftover.unlink(missing_ok=True)
        if stale_lock:
            (sb.state / "cas-executor.lock").write_text(f"{_dead_pid()}\n")
        procs = [_Loop(sb, "sleepy", FAKE_SLEEP_SECS="4", SIM_TIMER_SECS="120") for _ in range(racers)]
        try:
            at_barrier = sb.wait_for(lambda: len(list(sb.state.glob("at-barrier-*"))) >= racers, timeout=60)
            (sb.state / "ready").write_text("")
            time.sleep(0.3)
            (sb.state / "go").write_text("")
            for proc in procs:
                proc.wait(timeout=120)
            outputs = [proc.output() for proc in procs]
        finally:
            (sb.state / "go").write_text("")
            for proc in procs:
                proc.kill()
        assert at_barrier, "the racers were not lined up at the barrier, so this proved nothing"
        refused = [out for proc, out in zip(procs, outputs)
                   if proc.returncode == 1 and "Another executor is running" in out]
        ran = [out for proc, out in zip(procs, outputs) if "CAS Roadmap Executor started" in out]
        assert len(ran) == 1, f"round {round_number}: {len(ran)} executors believed they held the lock"
        assert len(refused) == racers - 1, [proc.returncode for proc in procs]
        assert sb.invocations() == round_number, "one session per round"
        assert not (sb.state / "cas-executor.lock").exists(), "the winner released the lock"
        leftovers = [p.name for p in sb.state.iterdir() if p.name.startswith("cas-executor.lock")]
        assert leftovers == [], leftovers


def _dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


@pytest.mark.parametrize("content", ["dead", "", "not-a-pid\n", "0\n"])
def test_a_lock_whose_owner_is_not_running_is_taken_over(sb, content):
    lock = sb.state / "cas-executor.lock"
    lock.write_text(f"{_dead_pid()}\n" if content == "dead" else content)
    result = sb.run("ok")
    assert result.returncode == 0, result.stdout
    assert sb.status("item-a") == "completed", result.stdout
    assert not lock.exists()


def test_a_lock_whose_owner_is_running_is_left_alone(sb):
    lock = sb.state / "cas-executor.lock"
    lock.write_text(f"{os.getpid()}\n")  # this test process: alive, and not an executor
    result = sb.run("ok")
    assert result.returncode == 1
    assert f"Another executor is running (PID {os.getpid()})" in result.stdout
    assert lock.read_text() == f"{os.getpid()}\n"
    assert sb.invocations() == 0
    assert not sb.crash_log_nonempty()


def test_a_guard_directory_left_by_a_dead_lock_breaker_does_not_wedge_the_start(sb):
    # FILE.breaking is held for milliseconds; one that is minutes old was
    # left by a process that died holding it.
    lock = sb.state / "cas-executor.lock"
    lock.write_text(f"{_dead_pid()}\n")
    guard = sb.state / "cas-executor.lock.breaking"
    guard.mkdir()
    long_ago = time.time() - 600
    os.utime(guard, (long_ago, long_ago))
    result = sb.run("ok")
    assert result.returncode == 0, result.stdout
    assert sb.status("item-a") == "completed", result.stdout
    assert not guard.exists()


# ---------------------------------------------------------------------------
# Time limits and what they end
# ---------------------------------------------------------------------------

def _sleeper(sb):
    """A program the fake session starts at several depths. It does not exec,
    so its own path stays on its command line for pgrep to find."""
    path = sb.state / "sleeper"
    path.write_text('#!/bin/bash\n/bin/sleep "$1"\n')
    path.chmod(0o755)
    return path


def _running(pattern):
    return subprocess.run(["pgrep", "-fl", pattern], capture_output=True, text=True).stdout.strip()


def test_the_kill_timer_ends_the_whole_process_tree_of_the_session(sb):
    # The session's tree: claude, a shell, a shell below it, the sleeper, its
    # sleep; one sleeper that put itself in a session of its own; and one
    # whose parent has exited. A kill that reaches the session and its
    # children leaves the rest running; a walk down the process tree misses
    # the re-parented one; the process group alone misses the one that left it.
    sleeper = _sleeper(sb)
    try:
        result = sb.run("deep_tree", SIM_TIMER_SECS="2")
        assert result.returncode == 0, result.stdout
        assert "timeout=1" in result.stdout
        assert sb.wait_for(lambda: not _running(str(sleeper)), timeout=10), _running(str(sleeper))
        assert not _running("sleep 3133[789]")
        assert not _running(str(sb.bin / "claude"))
        assert not [p.name for p in sb.state.iterdir() if p.name.startswith("cas-exec-job-")], \
            "job records are removed when the job has ended"
    finally:
        # when the kill did not reach them: the sleeps carry no path the
        # fixture's cleanup could find them by
        subprocess.run(["pkill", "-f", "sleep 3133[789]"], check=False)


def test_a_test_that_never_returns_fails_the_gate_and_is_not_left_running(sb):
    before = sb.head()
    result = sb.run("hanging_test", timeout=150, CAS_EXECUTOR_GATE_TIMEOUT_SEC="12.5")
    assert "Python tests failed" in result.stdout, result.stdout
    assert "gave up after 12.5s" in sb.logs()
    assert sb.head() == before and sb.origin_head() == before
    assert sb.status("item-a") == "pending"
    assert not _running(str(sb.repo / ".worktrees" / "gate-item-a")), "the gate's pytest is still running"
    assert not list((sb.repo / ".worktrees").glob("gate-*"))


def test_a_start_ends_the_session_a_killed_loop_left_running(sb):
    # SIGKILL gives the loop no chance to stop its session. The session is a
    # process group of its own and goes on (its timer, a separate process,
    # would still end it at the limit). The next start must not work the same
    # item next to it.
    proc = _Loop(sb, "sleepy", FAKE_SLEEP_SECS="120", SIM_TIMER_SECS="300")
    try:
        assert sb.wait_for(lambda: sb.invocations() == 1 and sb.loop_pid() is not None)
        os.kill(sb.loop_pid(), signal.SIGKILL)
        proc.wait(timeout=30)
        assert _running(str(sb.bin / "claude")), "the session ended with the loop, so this proved nothing"
        second = sb.run("fail")
        assert "Stopping a job a previous executor left running" in second.stdout, second.stdout
        assert not _running(str(sb.bin / "claude"))
        assert not [p.name for p in sb.state.iterdir() if p.name.startswith("cas-exec-job-")]
    finally:
        proc.kill()


def test_sigterm_in_parallel_mode_stops_every_session(sb):
    sb.set_config(max_parallel=2)
    proc = _Loop(sb, "sleepy", FAKE_SLEEP_SECS="60", SIM_TIMER_SECS="100")
    try:
        assert sb.wait_for(lambda: sb.invocations() == 2 and sb.loop_pid() is not None)
        os.kill(sb.loop_pid(), signal.SIGTERM)
        proc.wait(timeout=30)
        assert not _running(str(sb.bin / "claude")), "a session was orphaned"
        assert not list((sb.repo / ".worktrees").glob("auto-*"))
        assert not sb.crash_log_nonempty(), "SIGTERM is a sanctioned exit, not a crash"
        assert not [p.name for p in sb.state.iterdir() if p.name.startswith("cas-exec-job-")]
    finally:
        proc.kill()


# ---------------------------------------------------------------------------
# A checkout with an unmerged path (a stash pop that conflicted)
# ---------------------------------------------------------------------------
#
# The merge needs a stash of the operator's edits, and git refuses both that
# stash and the merge while a path is unmerged. A conflicted pop leaves one
# behind, in cli/failure_kb.json, a file DIRTY_OK_RE tolerates. Before the
# preflight checked for unmerged paths, every later cycle ran a paid session
# and then failed it at the stash, and the failure was charged to the item.
# These tests pin three things: the preflight refuses while a path is
# unmerged; a pop that conflicts says so, with the stash ref, and keeps the
# stash; and a checkout that goes unmerged during a run charges nothing to the
# item.

def _failure_kb_conflict(sb):
    """Make cli/failure_kb.json on master and on a branch 'side' differ from the
    base on the same line, so that merging 'side' conflicts. Leaves the main
    checkout on master, clean and pushed."""
    sb.git("checkout", "-q", "-b", "side")
    (sb.repo / "cli" / "failure_kb.json").write_text('{"side": 1}\n')
    sb.git("commit", "-qam", "side")
    sb.git("checkout", "-q", "master")
    (sb.repo / "cli" / "failure_kb.json").write_text('{"master": 1}\n')
    sb.git("commit", "-qam", "master moves")
    sb.git("push", "-q", "origin", "master")


def _unmerged(sb):
    return sb.git("ls-files", "-u").stdout.strip()


def test_a_conflicted_stash_pop_stops_the_next_cycle_before_any_session(sb):
    # The operator's edit is stashed; the same line changes on master; the pop
    # conflicts, so the stash stays on the stack and the file is unmerged.
    (sb.repo / "cli" / "failure_kb.json").write_text('{"operator": 1}\n')
    sb.git("stash", "push", "-q", "-m", "operator-kb")
    (sb.repo / "cli" / "failure_kb.json").write_text('{"master": 1}\n')
    sb.git("commit", "-qam", "master moves")
    sb.git("push", "-q", "origin", "master")
    assert sb.git("stash", "pop", check=False).returncode == 1
    assert _unmerged(sb), "the pop left an unmerged path"
    stashes = sb.stashes()
    assert len(stashes) == 1
    origin_before = sb.origin_head()

    result = sb.run("ok")

    assert result.returncode == 0, result.stdout + result.stderr
    assert sb.invocations() == 0, "no paid session starts while a path is unmerged"
    assert sb.failures() == "", "the checkout's state is charged to no item"
    assert sb.stashes() == stashes, "the stash is still listed"
    assert sb.status("item-a") == "pending"
    assert sb.origin_head() == origin_before, "nothing is pushed"
    assert any("unmerged paths" in (beat.get("skipped_reason") or "") for beat in sb.ops.heartbeats()), \
        sb.ops.heartbeats()


def test_a_stash_pop_that_conflicts_after_a_merge_is_reported_and_the_next_cycle_yields(sb):
    (sb.repo / "cli" / "failure_kb.json").write_text('{"operator": 1}\n')

    first = sb.run("kb_change")

    assert first.returncode == 0, first.stdout + first.stderr
    assert "stash pop conflicted; stash kept as stash@{0}" in first.stdout, first.stdout
    assert sb.status("item-a") == "completed", "the merge itself went through:\n" + first.stdout
    assert sb.origin_head() == sb.head()
    assert len(sb.stashes()) == 1
    assert _unmerged(sb)
    invocations, failures = sb.invocations(), sb.failures()
    assert invocations == 1

    second = sb.run("ok")

    assert second.returncode == 0, second.stdout + second.stderr
    assert sb.invocations() == invocations, "the next cycle starts no session"
    assert sb.failures() == failures, "and charges nothing"
    assert sb.status("item-b") == "pending"
    assert len(sb.stashes()) == 1, "the stash is still listed"


def test_a_checkout_left_unmerged_while_a_run_is_in_progress_charges_no_item(sb):
    # The session itself starts a merge in the main checkout and leaves it
    # unfinished. The checkout was clean when the cycle began, so the session
    # runs; the stash the merge step needs is then refused.
    _failure_kb_conflict(sb)
    origin_before = sb.origin_head()

    result = sb.run("main_conflict")

    assert result.returncode == 0, result.stdout + result.stderr
    assert sb.invocations() == 1, "the cycle began on a clean checkout, so its session ran"
    assert sb.failures() == "", "an unmerged checkout is not a failure of the item"
    assert sb.status("item-a") == "pending"
    assert "git refused the stash because the checkout has unmerged paths" in result.stdout, result.stdout
    assert sb.origin_head() == origin_before, "nothing is pushed"
    assert _unmerged(sb)


def test_a_checkout_that_goes_unmerged_before_the_merge_charges_no_item(sb):
    # The main checkout is made unmerged while the loop waits in its pull,
    # after the session and the gate and before the merge. The merge step
    # refuses, and that is the checkout's state, not the item's.
    _failure_kb_conflict(sb)
    sb.hold_points()
    origin_before = sb.origin_head()
    proc = _run_until_held(sb, "upload-pack")

    sb.git("merge", "-q", "--no-commit", "--no-ff", "side", check=False)
    assert _unmerged(sb), "the merge in the main checkout left an unmerged path"
    sb.release("upload-pack")

    assert proc.wait(timeout=120) == 0, proc.output()
    output = proc.output()
    assert sb.invocations() == 1
    assert sb.failures() == "", "an unmerged checkout is not a failure of the item"
    assert sb.status("item-a") == "pending"
    assert sb.origin_head() == origin_before, "nothing is pushed"
    assert sb.journal() is None, "the merge section is closed"
    assert "Merge: the checkout has unmerged paths" in output, output
