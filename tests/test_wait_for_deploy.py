# tests/test_wait_for_deploy.py: Tests for scripts/wait_for_deploy.py
#
# Regression target: the script previously used `git rev-parse HEAD` as the
# "target" commit, but a push from a feature branch directly to master
# (e.g. `CAS_PROMOTE=1 git push origin branch:master`) doesn't advance
# LOCAL master. When the script ran after such a push, it would see the
# deployed services on the NEW commit and local HEAD on the OLD one, flag
# the services as "wrong", and trigger a spurious manual redeploy.
#
# Fix (2026-04-16): default to `origin/master` post-fetch, with fallback
# to local HEAD if the origin ref can't be resolved (offline case).

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).parent.parent
_SCRIPT = _ROOT / "scripts" / "wait_for_deploy.py"

# Load the script as a module (matches pattern in tests/test_classify_push.py)
_spec = importlib.util.spec_from_file_location("wait_for_deploy", _SCRIPT)
wait_for_deploy = importlib.util.module_from_spec(_spec)
sys.modules["wait_for_deploy"] = wait_for_deploy
_spec.loader.exec_module(wait_for_deploy)


def _make_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


# ---------------------------------------------------------------------------
# get_local_commit(): regression target for the double-redeploy bug
# ---------------------------------------------------------------------------


class TestGetLocalCommit:
    def test_default_uses_origin_master_after_fetch(self):
        """Default path: fetch origin master, rev-parse origin/master.
        This is what matters after a push, local HEAD may be stale if
        the user pushed from a feature branch to master without pulling."""
        calls = []

        def fake_run(cmd, **_):
            calls.append(cmd)
            if cmd[:2] == ["git", "fetch"]:
                return _make_completed(0)
            if cmd[:3] == ["git", "rev-parse", "origin/master"]:
                return _make_completed(0, stdout="0123456789abcdef0123456789abcdef01234567\n")
            # Unexpected
            return _make_completed(1, stderr="unexpected call")

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            sha = wait_for_deploy.get_local_commit()

        assert sha == "0123456789abcdef0123456789abcdef01234567"
        # Must fetch first, then rev-parse origin/master
        assert calls[0][:2] == ["git", "fetch"]
        assert "origin" in calls[0]
        assert "master" in calls[0]
        assert calls[1][:3] == ["git", "rev-parse", "origin/master"]

    def test_staging_ref_uses_origin_staging(self):
        """When targeting staging, fetch+rev-parse origin/staging."""
        calls = []

        def fake_run(cmd, **_):
            calls.append(cmd)
            if cmd[:2] == ["git", "fetch"]:
                return _make_completed(0)
            if cmd[:3] == ["git", "rev-parse", "origin/staging"]:
                return _make_completed(0, stdout="abc1234def5678\n")
            return _make_completed(1)

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            sha = wait_for_deploy.get_local_commit(ref="origin/staging")

        assert sha == "abc1234def5678"
        assert "staging" in calls[0]  # fetched staging
        assert calls[1][:3] == ["git", "rev-parse", "origin/staging"]

    def test_fetch_exception_tolerated_if_cached_ref_resolves(self):
        """A fetch failure (offline, network blip) must NOT fail the
        script when origin/master is already cached locally. Tolerated-fail
        behaviour for temporary network issues."""
        call_count = {"run": 0}

        def fake_run(cmd, **_):
            call_count["run"] += 1
            if cmd[:2] == ["git", "fetch"]:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=15)
            if cmd[:3] == ["git", "rev-parse", "origin/master"]:
                return _make_completed(0, stdout="cached12345\n")
            return _make_completed(1)

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            sha = wait_for_deploy.get_local_commit()

        assert sha == "cached12345"

    def test_falls_back_to_head_if_origin_ref_missing(self):
        """If origin/master can't be resolved (no origin remote, fresh
        clone without fetch, etc.), fall back to local HEAD. Prints a
        WARN to stderr so the fallback is visible in logs."""
        def fake_run(cmd, **_):
            if cmd[:2] == ["git", "fetch"]:
                return _make_completed(0)
            if cmd[:3] == ["git", "rev-parse", "origin/master"]:
                return _make_completed(128, stderr="unknown revision")
            if cmd[:3] == ["git", "rev-parse", "HEAD"]:
                return _make_completed(0, stdout="fallback123\n")
            return _make_completed(1)

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            sha = wait_for_deploy.get_local_commit()

        assert sha == "fallback123"

    def test_exits_1_if_both_ref_and_head_fail(self):
        """If neither origin/master NOR local HEAD resolves, exit 1.
        There's no target to wait for, the script can't function."""
        def fake_run(cmd, **_):
            if cmd[:2] == ["git", "fetch"]:
                return _make_completed(0)
            return _make_completed(128, stderr="not a git repo")

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            with pytest.raises(SystemExit) as exc_info:
                wait_for_deploy.get_local_commit()

        assert exc_info.value.code == 1

    def test_empty_stdout_treated_as_failure(self):
        """rev-parse that returns 0 but with empty stdout is treated as
        a failure. This catches edge cases like a detached HEAD or an
        ambiguous ref that silently resolves to nothing."""
        calls = []

        def fake_run(cmd, **_):
            calls.append(cmd)
            if cmd[:2] == ["git", "fetch"]:
                return _make_completed(0)
            if cmd[:3] == ["git", "rev-parse", "origin/master"]:
                # returncode=0 but no stdout: pathological case
                return _make_completed(0, stdout="\n")
            if cmd[:3] == ["git", "rev-parse", "HEAD"]:
                return _make_completed(0, stdout="realhead456\n")
            return _make_completed(1)

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            sha = wait_for_deploy.get_local_commit()

        assert sha == "realhead456"

    def test_non_origin_ref_skips_fetch(self):
        """If the ref doesn't start with 'origin/', skip the fetch step
        (e.g. caller passed a local branch or a specific SHA)."""
        calls = []

        def fake_run(cmd, **_):
            calls.append(cmd)
            if cmd[:2] == ["git", "fetch"]:
                return _make_completed(0)
            if cmd[:3] == ["git", "rev-parse", "HEAD"]:
                return _make_completed(0, stdout="directhead789\n")
            return _make_completed(1)

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            sha = wait_for_deploy.get_local_commit(ref="HEAD")

        assert sha == "directhead789"
        # Must NOT have called fetch: no origin/ prefix
        fetch_calls = [c for c in calls if c[:2] == ["git", "fetch"]]
        assert fetch_calls == []


# ---------------------------------------------------------------------------
# _is_descendant_or_equal(): tolerate executor-pushed descendants of target
# ---------------------------------------------------------------------------
#
# Regression target: when wait_for_deploy.py captures a target SHA and the
# autonomous executor pushes additional commits on top within the polling
# window, Railway auto-deploys each, so /health reports a SHA downstream of
# the original target. Previously the script saw "want X / got Y" and
# polled until timeout even though prod was on healthy newer-or-equal code.


_TARGET = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_DESCENDANT = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_DIVERGED = "cccccccccccccccccccccccccccccccccccccccc"


class TestIsDescendantOrEqual:
    def test_exact_match_returns_true_without_git_call(self):
        """Cheapest path: deployed SHA == target SHA. No git subprocess."""
        with patch.object(wait_for_deploy.subprocess, "run") as run:
            assert wait_for_deploy._is_descendant_or_equal(_TARGET, _TARGET)
        run.assert_not_called()

    def test_short_prefix_match_returns_true_without_git_call(self):
        """/health may report a short SHA. 7-char prefix match counts as equal."""
        short = _TARGET[:7]
        with patch.object(wait_for_deploy.subprocess, "run") as run:
            assert wait_for_deploy._is_descendant_or_equal(_TARGET, short)
            assert wait_for_deploy._is_descendant_or_equal(short, _TARGET)
        run.assert_not_called()

    def test_descendant_returns_true_via_merge_base(self):
        """deployed SHA differs from target but descends from it (executor push).
        Verified via `git merge-base --is-ancestor <target> <deployed>`."""
        seen = []

        def fake_run(cmd, **_):
            seen.append(cmd)
            if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
                # ancestor=target, descendant=deployed: return 0 (is ancestor)
                assert cmd[3] == _TARGET
                assert cmd[4] == _DESCENDANT
                return _make_completed(0)
            return _make_completed(1)

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            assert wait_for_deploy._is_descendant_or_equal(_TARGET, _DESCENDANT)

        # First call should be merge-base, no fetch needed because git found the SHA.
        assert seen[0][:3] == ["git", "merge-base", "--is-ancestor"]

    def test_diverged_returns_false(self):
        """deployed SHA is NOT a descendant of target. Must return False
        even with the fetch retry."""

        def fake_run(cmd, **_):
            if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
                return _make_completed(1)  # not an ancestor
            if cmd[:2] == ["git", "fetch"]:
                return _make_completed(0)
            return _make_completed(1)

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            assert not wait_for_deploy._is_descendant_or_equal(
                _TARGET, _DIVERGED, fetched={"done": False},
            )

    def test_fetches_once_when_descendant_unknown_locally(self):
        """When the descendant SHA isn't cached locally, merge-base errors
        on first call. Helper fetches origin once and retries.
        Subsequent calls in the same polling cycle reuse the fetched flag."""
        merge_base_calls = []
        fetch_calls = []

        def fake_run(cmd, **_):
            if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
                merge_base_calls.append(cmd)
                # First call: return error (SHA not cached); after fetch, succeed.
                if len(fetch_calls) == 0:
                    return _make_completed(128, stderr="bad object")
                return _make_completed(0)
            if cmd[:2] == ["git", "fetch"]:
                fetch_calls.append(cmd)
                return _make_completed(0)
            return _make_completed(1)

        fetched = {"done": False}
        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            assert wait_for_deploy._is_descendant_or_equal(
                _TARGET, _DESCENDANT, fetched=fetched,
            )

        assert len(merge_base_calls) == 2, "should retry merge-base after fetch"
        assert len(fetch_calls) == 1, "should fetch exactly once"
        assert fetched["done"] is True

    def test_does_not_refetch_when_already_done(self):
        """Once `fetched['done']` is True, helper must not fetch again
        even if merge-base still fails. Cheap fallback to False."""
        fetch_calls = []

        def fake_run(cmd, **_):
            if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
                return _make_completed(128, stderr="bad object")
            if cmd[:2] == ["git", "fetch"]:
                fetch_calls.append(cmd)
                return _make_completed(0)
            return _make_completed(1)

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            assert not wait_for_deploy._is_descendant_or_equal(
                _TARGET, _DIVERGED, fetched={"done": True},
            )

        assert fetch_calls == [], "must not fetch when fetched flag already set"

    def test_empty_inputs_return_false(self):
        """Defensive: missing target or deployed SHA -> not satisfied."""
        with patch.object(wait_for_deploy.subprocess, "run") as run:
            assert not wait_for_deploy._is_descendant_or_equal("", _TARGET)
            assert not wait_for_deploy._is_descendant_or_equal(_TARGET, "")
            assert not wait_for_deploy._is_descendant_or_equal("", "")
        run.assert_not_called()

    def test_subprocess_exception_returns_false(self):
        """If git is missing or subprocess raises, helper must NOT crash;
        return False so the caller stays in polling state."""

        def fake_run(cmd, **_):
            raise FileNotFoundError("git: command not found")

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            assert not wait_for_deploy._is_descendant_or_equal(
                _TARGET, _DIVERGED, fetched={"done": False},
            )


# ---------------------------------------------------------------------------
# check_web_health() / check_worker_deployed(): descendant tolerance
# ---------------------------------------------------------------------------


class TestHealthChecksTolerantOfDescendants:
    def _stub_health(self, monkeypatch, payload):
        def fake_fetch(url, headers=None, timeout=15):
            return payload
        monkeypatch.setattr(wait_for_deploy, "fetch_json", fake_fetch)

    def test_web_descendant_marked_ready(self, monkeypatch):
        """A deployed SHA that descends from target is treated as ready
        with an [OK descendant of …] info string."""
        self._stub_health(monkeypatch, {
            "status": "ok",
            "git_commit": _DESCENDANT,
        })

        def fake_run(cmd, **_):
            if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
                return _make_completed(0)
            if cmd[:2] == ["git", "fetch"]:
                return _make_completed(0)
            return _make_completed(1)

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            ready, info = wait_for_deploy.check_web_health(
                _TARGET, fetched={"done": False},
            )

        assert ready is True
        assert "OK descendant of" in info
        assert _TARGET[:7] in info
        assert _DESCENDANT[:7] in info

    def test_web_diverged_remains_not_ready(self, monkeypatch):
        """A deployed SHA that does NOT descend from target stays not-ready."""
        self._stub_health(monkeypatch, {
            "status": "ok",
            "git_commit": _DIVERGED,
        })

        def fake_run(cmd, **_):
            if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
                return _make_completed(1)
            if cmd[:2] == ["git", "fetch"]:
                return _make_completed(0)
            return _make_completed(1)

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            ready, info = wait_for_deploy.check_web_health(
                _TARGET, fetched={"done": False},
            )

        assert ready is False
        assert f"want {_TARGET[:7]}" in info

    def test_web_exact_match_still_works(self, monkeypatch):
        """Regression guard: exact-match path unchanged from prior behavior."""
        self._stub_health(monkeypatch, {
            "status": "ok",
            "git_commit": _TARGET,
        })

        with patch.object(wait_for_deploy.subprocess, "run") as run:
            ready, info = wait_for_deploy.check_web_health(
                _TARGET, fetched={"done": False},
            )

        assert ready is True
        assert "[OK]" in info
        assert "descendant" not in info
        # Exact match must not invoke any git subprocess.
        run.assert_not_called()

    def test_worker_descendant_marked_ready(self, monkeypatch):
        """Worker check tolerates descendants via the same path as web."""
        self._stub_health(monkeypatch, {
            "status": "ok",
            "git_commit": _TARGET,
            "worker": {
                "git_commit": _DESCENDANT,
                "started_at": "2026-05-04T00:00:00Z",
            },
        })

        def fake_run(cmd, **_):
            if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
                return _make_completed(0)
            if cmd[:2] == ["git", "fetch"]:
                return _make_completed(0)
            return _make_completed(1)

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            ready, info = wait_for_deploy.check_worker_deployed(
                _TARGET, fetched={"done": False},
            )

        assert ready is True
        assert "OK descendant of" in info

    def test_worker_diverged_remains_not_ready(self, monkeypatch):
        """Worker on a SHA neither equal nor descended from target stays
        not-ready (real deploy mismatch must still surface)."""
        self._stub_health(monkeypatch, {
            "status": "ok",
            "git_commit": _TARGET,
            "worker": {
                "git_commit": _DIVERGED,
                "started_at": "2026-05-04T00:00:00Z",
            },
        })

        def fake_run(cmd, **_):
            if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
                return _make_completed(1)
            if cmd[:2] == ["git", "fetch"]:
                return _make_completed(0)
            return _make_completed(1)

        with patch.object(wait_for_deploy.subprocess, "run", side_effect=fake_run):
            ready, info = wait_for_deploy.check_worker_deployed(
                _TARGET, fetched={"done": False},
            )

        assert ready is False
        assert f"want {_TARGET[:7]}" in info
