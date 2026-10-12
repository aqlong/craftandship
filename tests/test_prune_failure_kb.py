# tests/test_prune_failure_kb.py - Tests for scripts/prune_failure_kb.py
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# scripts/ has hyphenated filenames in some cases, but prune_failure_kb.py
# is plain Python, so a normal import works.
import prune_failure_kb as prune_mod  # noqa: E402


SCRIPT_PATH = ROOT / "scripts" / "prune_failure_kb.py"
DAY = 86400.0


def _entry(*, success=0, failure=0, last_seen=None, seeded=False, **extra):
    """Build a minimal failure_kb entry."""
    e = {
        "success_count": success,
        "failure_count": failure,
        "last_seen": last_seen if last_seen is not None else time.time(),
    }
    if seeded:
        e["seeded"] = True
    e.update(extra)
    return e


class TestIsStale:
    def test_zero_count_is_stale(self):
        entry = _entry(success=0, failure=0, last_seen=time.time())
        assert prune_mod.is_stale(
            entry, now=time.time(),
            stale_seconds=60 * DAY, max_failures=2,
        ) is True

    def test_old_with_few_failures_is_stale(self):
        now = time.time()
        entry = _entry(success=0, failure=2, last_seen=now - 90 * DAY)
        assert prune_mod.is_stale(
            entry, now=now, stale_seconds=60 * DAY, max_failures=2,
        ) is True

    def test_old_with_many_failures_is_kept(self):
        """High recurring-failure count = the pattern matters; keep it."""
        now = time.time()
        entry = _entry(success=0, failure=10, last_seen=now - 90 * DAY)
        assert prune_mod.is_stale(
            entry, now=now, stale_seconds=60 * DAY, max_failures=2,
        ) is False

    def test_recent_with_outcomes_is_kept(self):
        now = time.time()
        entry = _entry(success=3, failure=1, last_seen=now - 5 * DAY)
        assert prune_mod.is_stale(
            entry, now=now, stale_seconds=60 * DAY, max_failures=2,
        ) is False

    def test_seeded_entry_never_pruned_even_if_zero_count(self):
        now = time.time()
        # Zero count + ancient: would be pruned if not seeded
        entry = _entry(success=0, failure=0,
                       last_seen=now - 365 * DAY, seeded=True)
        assert prune_mod.is_stale(
            entry, now=now, stale_seconds=60 * DAY, max_failures=2,
        ) is False

    def test_missing_last_seen_treated_as_epoch(self):
        """An entry with no last_seen is ancient by default, but only
        prunable if it also has few failures (or zero count)."""
        now = time.time()
        # success_count=1, failure_count=0, no last_seen → very stale, 0 failures → prune
        entry = {"success_count": 1, "failure_count": 0}
        assert prune_mod.is_stale(
            entry, now=now, stale_seconds=60 * DAY, max_failures=2,
        ) is True


class TestPruneKb:
    def test_drops_stale_keeps_fresh(self):
        now = time.time()
        kb = {
            "old_zero": _entry(last_seen=now - 100 * DAY),
            "old_few_fails": _entry(failure=1, last_seen=now - 100 * DAY),
            "old_many_fails": _entry(failure=10, last_seen=now - 100 * DAY),
            "fresh": _entry(success=2, last_seen=now - 1 * DAY),
            "seeded_old": _entry(last_seen=now - 365 * DAY, seeded=True),
        }
        pruned, dropped = prune_mod.prune_kb(
            kb, now=now, stale_days=60, max_failures=2,
        )
        assert set(dropped) == {"old_zero", "old_few_fails"}
        assert set(pruned.keys()) == {"old_many_fails", "fresh", "seeded_old"}

    def test_empty_kb_returns_empty(self):
        pruned, dropped = prune_mod.prune_kb(
            {}, now=time.time(), stale_days=60, max_failures=2,
        )
        assert pruned == {}
        assert dropped == []

    def test_all_fresh_drops_nothing(self):
        now = time.time()
        kb = {
            f"sig_{i}": _entry(success=1, last_seen=now - i * DAY)
            for i in range(5)
        }
        pruned, dropped = prune_mod.prune_kb(
            kb, now=now, stale_days=60, max_failures=2,
        )
        assert dropped == []
        assert pruned == kb


class TestCli:
    """End-to-end CLI invocation against a tmp KB via FAILURE_KB_PATH."""

    def _run(self, kb_path: Path, *args, env_extra=None):
        env = os.environ.copy()
        env["FAILURE_KB_PATH"] = str(kb_path)
        env.pop("RAILWAY_ENVIRONMENT", None)
        if env_extra:
            env.update(env_extra)
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            env=env, capture_output=True, text=True, timeout=30,
        )
        return result

    def test_dry_run_does_not_modify(self, tmp_path):
        now = time.time()
        kb = {
            "stale": _entry(last_seen=now - 200 * DAY),
            "fresh": _entry(success=1, last_seen=now - 1 * DAY),
        }
        kb_path = tmp_path / "failure_kb.json"
        kb_path.write_text(json.dumps(kb))

        result = self._run(kb_path, "--dry-run")
        assert result.returncode == 0, result.stderr
        assert "dry-run" in result.stdout
        # File untouched
        assert json.loads(kb_path.read_text()) == kb

    def test_apply_prunes_in_place(self, tmp_path):
        now = time.time()
        kb = {
            "stale": _entry(last_seen=now - 200 * DAY),
            "fresh": _entry(success=1, last_seen=now - 1 * DAY),
            "seeded": _entry(last_seen=now - 999 * DAY, seeded=True),
        }
        kb_path = tmp_path / "failure_kb.json"
        kb_path.write_text(json.dumps(kb))

        result = self._run(kb_path)
        assert result.returncode == 0, result.stderr
        loaded = json.loads(kb_path.read_text())
        assert "stale" not in loaded
        assert "fresh" in loaded
        assert "seeded" in loaded

    def test_empty_kb_is_noop(self, tmp_path):
        kb_path = tmp_path / "failure_kb.json"
        kb_path.write_text("{}")
        result = self._run(kb_path)
        assert result.returncode == 0
        assert "empty" in result.stdout

    def test_no_drops_means_no_write(self, tmp_path):
        """When nothing qualifies for pruning, the file should be unchanged."""
        now = time.time()
        kb = {"fresh": _entry(success=2, last_seen=now - 1 * DAY)}
        kb_path = tmp_path / "failure_kb.json"
        kb_path.write_text(json.dumps(kb))
        original_mtime = kb_path.stat().st_mtime

        # Sleep briefly to ensure mtime would differ if rewritten
        time.sleep(0.05)
        result = self._run(kb_path)
        assert result.returncode == 0
        assert "no changes" in result.stdout
        assert kb_path.stat().st_mtime == original_mtime
