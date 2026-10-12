"""Tests for executor hardening fixes (2026-04-12).

Covers:
1. Timeout mechanism: Claude PID capture, process tree kill, was_timeout snapshot
2. PR re-pick fix: in_review status update in roadmap.json + git commit
3. cas-pick-item.py: in_review items excluded from eligibility

These are regression guards, if any of these structural invariants break,
the executor can hang indefinitely (timeout bug) or waste cycles re-picking
items that already have PRs open (re-pick bug).
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import cas-pick-item.py (script, not a normal module)
# ---------------------------------------------------------------------------
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"

# Load cas-pick-item.py as a module despite the hyphenated filename
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "cas_pick_item", SCRIPTS_DIR / "cas-pick-item.py"
)
cas_pick_item = importlib.util.module_from_spec(_spec)
sys.modules["cas_pick_item"] = cas_pick_item
_spec.loader.exec_module(cas_pick_item)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_roadmap(items):
    """Create a minimal roadmap dict."""
    return {"items": items}


def _make_item(item_id, status="pending", priority="medium", **kwargs):
    """Create a minimal roadmap item."""
    item = {
        "id": item_id,
        "title": f"Test item {item_id}",
        "description": "A short test item",
        "status": status,
        "priority": priority,
    }
    item.update(kwargs)
    return item


# ===========================================================================
# 1. cas-pick-item.py: in_review filtering
# ===========================================================================

class TestPickerInReviewFiltering:
    """in_review items must be excluded from eligibility."""

    def test_in_review_in_skip_statuses(self):
        """in_review is explicitly listed in SKIP_STATUSES."""
        assert "in_review" in cas_pick_item.SKIP_STATUSES

    def test_in_review_item_not_eligible(self):
        """An in_review item should not appear in get_eligible()."""
        data = _make_roadmap([
            _make_item("item-a", status="in_review"),
            _make_item("item-b", status="pending"),
        ])
        with patch.object(cas_pick_item, "get_failed_today", return_value=set()), \
             patch.object(cas_pick_item, "get_total_failures", return_value=0), \
             patch.object(cas_pick_item, "load_config", return_value={"skip_complex_items": False}):
            eligible = cas_pick_item.get_eligible(data)
        ids = [i["id"] for i in eligible]
        assert "item-a" not in ids
        assert "item-b" in ids

    def test_pick_next_skips_in_review(self):
        """pick_next should skip in_review items and return the next pending."""
        data = _make_roadmap([
            _make_item("pr-item", status="in_review", priority="critical"),
            _make_item("pending-item", status="pending", priority="low"),
        ])
        with patch.object(cas_pick_item, "get_failed_today", return_value=set()), \
             patch.object(cas_pick_item, "get_total_failures", return_value=0), \
             patch.object(cas_pick_item, "load_config", return_value={"skip_complex_items": False}):
            item = cas_pick_item.pick_next(data)
        assert item["id"] == "pending-item"

    def test_all_in_review_returns_none(self):
        """When all items are in_review, pick_next returns None."""
        data = _make_roadmap([
            _make_item("a", status="in_review"),
            _make_item("b", status="in_review"),
        ])
        with patch.object(cas_pick_item, "get_failed_today", return_value=set()), \
             patch.object(cas_pick_item, "get_total_failures", return_value=0), \
             patch.object(cas_pick_item, "load_config", return_value={"skip_complex_items": False}):
            assert cas_pick_item.pick_next(data) is None

    def test_all_skip_statuses_excluded(self):
        """Every status in SKIP_STATUSES is excluded from eligibility."""
        items = [_make_item(f"item-{s}", status=s) for s in cas_pick_item.SKIP_STATUSES]
        items.append(_make_item("only-pending", status="pending"))
        data = _make_roadmap(items)
        with patch.object(cas_pick_item, "get_failed_today", return_value=set()), \
             patch.object(cas_pick_item, "get_total_failures", return_value=0), \
             patch.object(cas_pick_item, "load_config", return_value={"skip_complex_items": False}):
            eligible = cas_pick_item.get_eligible(data)
        assert len(eligible) == 1
        assert eligible[0]["id"] == "only-pending"


# ===========================================================================
# 2. Roadmap in_review update logic (Python snippet from bash)
# ===========================================================================

class TestRoadmapStatusUpdate:
    """cas_pick_item.set_item_status is what the loop calls (via --set-status)
    to mark items in_review / completed / deferred / pending."""

    @staticmethod
    def _write(items):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(_make_roadmap(items), f, indent=2)
        f.write("\n")
        f.close()
        return Path(f.name)

    def test_update_status_to_in_review(self):
        path = self._write([_make_item("target-item"), _make_item("other-item")])
        try:
            assert cas_pick_item.set_item_status("target-item", "in_review", path=path) is True
            result = json.loads(path.read_text())
            statuses = {i["id"]: i["status"] for i in result["items"]}
            assert statuses == {"target-item": "in_review", "other-item": "pending"}
            assert path.read_text().endswith("}\n")
        finally:
            path.unlink()

    def test_note_field_is_written(self):
        path = self._write([_make_item("x")])
        try:
            assert cas_pick_item.set_item_status(
                "x", "deferred", ("deferred_reason", "too many failures"), path=path)
            item = json.loads(path.read_text())["items"][0]
            assert item["status"] == "deferred"
            assert item["deferred_reason"] == "too many failures"
        finally:
            path.unlink()

    def test_nonexistent_item_leaves_file_untouched(self):
        path = self._write([_make_item("existing")])
        try:
            before = path.read_text()
            assert cas_pick_item.set_item_status("ghost-item", "in_review", path=path) is False
            assert path.read_text() == before
            assert not path.with_name(path.name + ".tmp").exists()
        finally:
            path.unlink()

    def test_rejects_unknown_status_and_note_key(self):
        path = self._write([_make_item("x")])
        try:
            with pytest.raises(ValueError):
                cas_pick_item.set_item_status("x", "banana", path=path)
            with pytest.raises(ValueError):
                cas_pick_item.set_item_status("x", "pending", ("title", "hijack"), path=path)
        finally:
            path.unlink()

    def test_write_is_atomic_replace(self, monkeypatch):
        """A crash while writing must not corrupt roadmap.json: content goes to
        <file>.tmp and is moved over the original with os.replace."""
        path = self._write([_make_item("x")])
        replaced = []
        real_replace = os.replace
        monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append((str(a), str(b))), real_replace(a, b))[1])
        try:
            cas_pick_item.set_item_status("x", "completed", path=path)
            assert replaced == [(str(path) + ".tmp", str(path))]
        finally:
            path.unlink()

    def test_cli_set_status_exit_codes(self):
        import subprocess
        path = self._write([_make_item("x")])
        try:
            code = (
                "import importlib.util, sys; "
                f"spec = importlib.util.spec_from_file_location('m', {str(SCRIPTS_DIR / 'cas-pick-item.py')!r}); "
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                f"m.ROADMAP = __import__('pathlib').Path({str(path)!r}); "
                "sys.argv = ['x'] + sys.argv[1:]; m.main()"
            )
            ok = subprocess.run([sys.executable, "-c", code, "--set-status", "x", "in_review"],
                                capture_output=True, text=True)
            assert ok.returncode == 0, ok.stderr
            missing = subprocess.run([sys.executable, "-c", code, "--set-status", "nope", "pending"],
                                     capture_output=True, text=True)
            assert missing.returncode == 1
            bad = subprocess.run([sys.executable, "-c", code, "--set-status", "x", "banana"],
                                 capture_output=True, text=True)
            assert bad.returncode == 2
        finally:
            path.unlink()


class TestTransitionVerification:
    """cas_pick_item.transition_problem is what stands between the loop and a
    commit of roadmap.json: the file must be HEAD's copy plus the one status
    change the loop has just made (--verify-transition on the command line).
    The loop-level behavior is in test_executor_loop_behavior.py."""

    @staticmethod
    def _doc():
        return {"version": 2, "updated_at": "then", "items": [
            _make_item("a", executor_eligible=True),
            _make_item("b", status="completed"),
            _make_item("c", executor_eligible=False),
        ], "agent_initiatives": {"enabled": False}}

    def _after(self, item_id="a", status="completed", note=None):
        """What set_item_status really writes, round-tripped through a file."""
        import copy
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(self._doc(), f)
        f.close()
        try:
            assert cas_pick_item.set_item_status(item_id, status, note, path=f.name)
            return copy.deepcopy(json.loads(Path(f.name).read_text()))
        finally:
            os.unlink(f.name)

    def test_the_change_set_item_status_makes_is_accepted(self):
        assert cas_pick_item.transition_problem(self._doc(), self._after(), "a", "completed") is None
        note = ("deferred_reason", "Auto-deferred after 4 total failures")
        assert cas_pick_item.transition_problem(
            self._doc(), self._after(status="deferred", note=note), "a", "deferred", note) is None
        # pending with a note: the status itself does not change
        note = ("status_drift_note", "push_or_pr_failed at 2026-01-01T00:00:00Z")
        assert cas_pick_item.transition_problem(
            self._doc(), self._after(status="pending", note=note), "a", "pending", note) is None

    def test_an_added_item_is_refused(self):
        after = self._after()
        after["items"].append(_make_item("planted", priority="critical"))
        assert "number of items" in cas_pick_item.transition_problem(self._doc(), after, "a", "completed")

    def test_a_removed_or_reordered_item_is_refused(self):
        after = self._after()
        after["items"].pop()
        assert cas_pick_item.transition_problem(self._doc(), after, "a", "completed")
        after = self._after()
        after["items"][1], after["items"][2] = after["items"][2], after["items"][1]
        assert cas_pick_item.transition_problem(self._doc(), after, "a", "completed")

    @pytest.mark.parametrize("field,value", [
        ("status", "pending"),            # a completed item put back in the queue
        ("executor_eligible", True),      # a human-only item made runnable
        ("description", "do something else entirely"),
        ("priority", "critical"),
    ])
    def test_a_change_to_another_item_is_refused(self, field, value):
        after = self._after()
        target = next(i for i in after["items"] if i["id"] == ("b" if field == "status" else "c"))
        target[field] = value
        problem = cas_pick_item.transition_problem(self._doc(), after, "a", "completed")
        assert problem and "not the item being marked" in problem

    @pytest.mark.parametrize("field,value", [
        ("description", "rewritten"), ("priority", "critical"), ("executor_eligible", False),
        ("blocked_by", []), ("deferred_reason", "a note nobody asked for"),
    ])
    def test_any_other_change_to_the_marked_item_is_refused(self, field, value):
        after = self._after()
        after["items"][0][field] = value
        problem = cas_pick_item.transition_problem(self._doc(), after, "a", "completed")
        assert problem and field in problem

    def test_a_removed_field_of_the_marked_item_is_refused(self):
        after = self._after()
        del after["items"][0]["executor_eligible"]
        assert "executor_eligible" in cas_pick_item.transition_problem(self._doc(), after, "a", "completed")

    def test_the_wrong_status_or_note_is_refused(self):
        assert cas_pick_item.transition_problem(self._doc(), self._after(status="deferred"), "a", "completed")
        note = ("deferred_reason", "expected text")
        after = self._after(status="deferred", note=("deferred_reason", "other text"))
        assert cas_pick_item.transition_problem(self._doc(), after, "a", "deferred", note)
        # a note in the file that the caller did not ask for
        assert cas_pick_item.transition_problem(self._doc(), after, "a", "deferred")

    def test_a_top_level_change_is_refused(self):
        after = self._after()
        after["agent_initiatives"] = {"enabled": True}
        assert "agent_initiatives" in cas_pick_item.transition_problem(self._doc(), after, "a", "completed")
        after = self._after()
        after["extra"] = 1
        assert cas_pick_item.transition_problem(self._doc(), after, "a", "completed")

    def test_an_unknown_item_or_a_duplicated_id_is_refused(self):
        assert cas_pick_item.transition_problem(self._doc(), self._doc(), "ghost", "completed")
        # a roadmap with the same id twice: set_item_status changes the first
        # only, and no outcome of that is "exactly one item changed"
        before = self._doc()
        before["items"].append(_make_item("a"))
        after = json.loads(json.dumps(before))
        after["items"][0]["status"] = "completed"
        assert cas_pick_item.transition_problem(before, after, "a", "completed")
        after["items"][-1]["status"] = "completed"
        assert "appears 2 times" in cas_pick_item.transition_problem(before, after, "a", "completed")

    def test_documents_that_are_not_roadmaps_are_refused(self):
        assert cas_pick_item.transition_problem([], [], "a", "completed")
        assert cas_pick_item.transition_problem({"items": {}}, {"items": {}}, "a", "completed")

    def test_cli_exit_codes(self, tmp_path):
        import subprocess
        before = tmp_path / "before.json"
        current = tmp_path / "roadmap.json"
        before.write_text(json.dumps(self._doc()))
        current.write_text(json.dumps(self._after()))
        code = (
            "import importlib.util, sys; "
            f"spec = importlib.util.spec_from_file_location('m', {str(SCRIPTS_DIR / 'cas-pick-item.py')!r}); "
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
            f"m.ROADMAP = __import__('pathlib').Path({str(current)!r}); "
            "sys.argv = ['x'] + sys.argv[1:]; m.main()"
        )

        def run(*args):
            return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True)

        assert run("--verify-transition", str(before), "a", "completed").returncode == 0
        refused = run("--verify-transition", str(before), "a", "deferred")
        assert refused.returncode == 1 and "not the expected single change" in refused.stderr
        tampered = self._after()
        tampered["items"].append(_make_item("planted"))
        current.write_text(json.dumps(tampered))
        assert run("--verify-transition", str(before), "a", "completed").returncode == 1
        assert run("--verify-transition", str(tmp_path / "missing.json"), "a", "completed").returncode == 2


class TestPickerRobustness:
    def test_ids_that_are_not_plain_slugs_are_never_eligible(self):
        """Ids become git branch names, worktree paths and shell arguments."""
        bad_ids = ["x'; touch /tmp/pwned; '", "../escape", "has space", "-leading", "a/b", ""]
        data = _make_roadmap([_make_item(i) for i in bad_ids] + [_make_item("fine-id_1.2")])
        with patch.object(cas_pick_item, "get_failed_today", return_value=set()), \
             patch.object(cas_pick_item, "get_total_failures", return_value=0), \
             patch.object(cas_pick_item, "load_config", return_value={"skip_complex_items": False}):
            eligible = cas_pick_item.get_eligible(data)
        assert [i["id"] for i in eligible] == ["fine-id_1.2"]

    def test_item_without_status_or_title_does_not_crash_the_queue(self):
        data = _make_roadmap([{"id": "no-status"}, {"id": "no-title", "status": "pending"},
                              _make_item("good")])
        with patch.object(cas_pick_item, "get_failed_today", return_value=set()), \
             patch.object(cas_pick_item, "get_total_failures", return_value=0), \
             patch.object(cas_pick_item, "load_config", return_value={"skip_complex_items": False}):
            eligible = cas_pick_item.get_eligible(data)
        assert [i["id"] for i in eligible] == ["good"]

    def test_fail_tracker_honors_state_dir_env(self):
        import subprocess
        code = ("import importlib.util; "
                f"spec = importlib.util.spec_from_file_location('m', {str(SCRIPTS_DIR / 'cas-pick-item.py')!r}); "
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(m.FAIL_TRACKER)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={**os.environ, "CAS_EXECUTOR_STATE_DIR": "/var/tmp/cas-x"})
        assert out.stdout.strip() == "/var/tmp/cas-x/cas-executor-failures"

    def test_fail_tracker_defaults_to_the_temp_dir_the_shell_scripts_use(self, tmp_path):
        """Without CAS_EXECUTOR_STATE_DIR the picker uses $TMPDIR (else /tmp),
        the same default the loop, watchdog and cas-exec resolve in shell."""
        import subprocess
        code = ("import importlib.util; "
                f"spec = importlib.util.spec_from_file_location('m', {str(SCRIPTS_DIR / 'cas-pick-item.py')!r}); "
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(m.FAIL_TRACKER)")
        env = {k: v for k, v in os.environ.items() if k != "CAS_EXECUTOR_STATE_DIR"}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={**env, "TMPDIR": str(tmp_path)})
        assert Path(out.stdout.strip()).resolve() == (tmp_path / "cas-executor-failures").resolve()


# ===========================================================================
# 3. Structural invariants in cas-roadmap-loop.sh
# ===========================================================================

class TestExecutorTimeoutStructure:
    """Structural checks on the timeout mechanism in cas-roadmap-loop.sh.

    That the timer ends a session's whole process tree, that a timeout is told
    from an ordinary failure, and that parallel items do not kill the
    supervisor are tested by running the loop (test_executor_loop_behavior.py
    and test_executor_loop_recovery.py). These only pin that there is one
    timer implementation and that the session goes through it.
    """

    @pytest.fixture(autouse=True)
    def _load_script(self):
        self.script = (SCRIPTS_DIR / "cas-roadmap-loop.sh").read_text()
        self.code = "\n".join(ln for ln in self.script.split("\n") if not ln.strip().startswith("#"))

    def test_the_session_runs_under_the_common_time_limit(self):
        assert 'with_timeout "$SESSION_TIMEOUT_SEC" "$claude_bin" -p "$prompt"' in self.code
        assert "SESSION_TIMEOUT_SEC=1200" in self.code

    def test_there_is_one_way_to_end_a_job(self):
        """Every kill of a job goes through stop_jobs (a process group, then
        SIGKILL): no pkill -P, which reaches one generation only."""
        assert "pkill" not in self.code
        assert self.code.count("stop_jobs() {") == 1
        assert "kill -s \"$sig\" -- \"-$pid\"" in self.code

    def test_no_signal_based_timeout(self):
        """`kill -USR1 $$` reaches the MAIN loop process, not the backgrounded
        job, when max_parallel > 1, and kills the supervisor. The timeout must
        be communicated through the marker file instead."""
        assert "USR1" not in self.code
        assert "timed_out" not in self.code

    def test_downstream_uses_was_timeout_not_timed_out(self):
        """The failure check must use was_timeout."""
        assert 'was_timeout="$TIMED_OUT"' in self.code
        assert '"$was_timeout" -eq 1' in self.code
        assert "(timeout=$was_timeout)" in self.code


class TestExecutorPRReviewStructure:
    """Structural checks on the roadmap status updates. What the commits
    contain is tested by running the loop (test_executor_loop_behavior.py)."""

    @pytest.fixture(autouse=True)
    def _load_script(self):
        self.script = (SCRIPTS_DIR / "cas-roadmap-loop.sh").read_text()
        self.code = [ln for ln in self.script.split("\n") if not ln.strip().startswith("#")]

    def _function(self, name):
        start = self.script.index(f"{name}() {{")
        return self.script[start:self.script.index("\n}\n", start)]

    def test_pr_path_updates_roadmap_status(self):
        """PR-created path must set item status to in_review, through the one
        function that writes and commits a status."""
        assert 'record_roadmap_status_locked "$item_id" in_review "$log_file"' in self.script

    def test_status_commits_are_made_in_one_place_and_through_the_hooks(self):
        """The loop has one `git commit`: the single-path commit in
        write_roadmap_status (what record_roadmap_status runs with SIGTERM and
        SIGINT held), after the picker has compared the file with HEAD's
        copy. Nothing stages files, and no commit skips the hooks."""
        wrapper = self._function("record_roadmap_status")
        assert wrapper.index("hold_signals") < wrapper.index('write_roadmap_status "$@"') \
            < wrapper.index("release_signals")
        body = self._function("write_roadmap_status")
        assert 'git commit -q -m "$subject" -- "$roadmap"' in body
        assert body.index("--verify-transition") < body.index('git commit -q -m "$subject"')
        joined = "\n".join(self.code)
        assert joined.count("git commit") == 1
        assert "git add" not in joined
        assert "--no-verify" not in joined

    def test_every_status_change_goes_through_the_committing_function(self):
        """--set-status is called from write_roadmap_status only (the body of
        record_roadmap_status): a status written anywhere else would be left
        uncommitted in the checkout."""
        body = self._function("write_roadmap_status")
        assert "\n".join(self.code).count("--set-status") == body.count("--set-status") == 2
        assert "\n".join(self.code).count("write_roadmap_status ") == 1, "and that body has one caller"

    def test_pr_path_has_error_handling(self):
        """A failed status update must be reported, not swallowed."""
        idx = self.script.index('record_roadmap_status_locked "$item_id" in_review')
        tail = self.script[idx:idx + 300]
        assert "Warning: could not update roadmap status to in_review" in tail

    def test_roadmap_updates_do_not_splice_values_into_python_source(self):
        """Item ids and notes reach Python as argv, never inside python3 -c
        source text (an id containing a quote would be code injection)."""
        for line in self.script.split("\n"):
            if "python3 -c" in line:
                assert "$item_id" not in line, line
                assert "$reason" not in line, line


# ===========================================================================
# 4. Complexity estimation (existing logic, regression guard)
# ===========================================================================

class TestComplexityEstimation:
    """Regression guards for the complexity filter that interacts with
    the eligibility logic."""

    def test_complex_items_skipped_by_default(self):
        data = _make_roadmap([
            _make_item("complex-one", title="Redesign the entire auth system"),
            _make_item("simple-one", title="Fix typo in footer"),
        ])
        with patch.object(cas_pick_item, "get_failed_today", return_value=set()), \
             patch.object(cas_pick_item, "get_total_failures", return_value=0), \
             patch.object(cas_pick_item, "load_config", return_value={"skip_complex_items": True}):
            eligible = cas_pick_item.get_eligible(data)
        ids = [i["id"] for i in eligible]
        assert "complex-one" not in ids
        assert "simple-one" in ids

    def test_complex_items_included_when_config_allows(self):
        data = _make_roadmap([
            _make_item("complex-one", title="Redesign the entire auth system"),
        ])
        with patch.object(cas_pick_item, "get_failed_today", return_value=set()), \
             patch.object(cas_pick_item, "get_total_failures", return_value=0), \
             patch.object(cas_pick_item, "load_config", return_value={"skip_complex_items": False}):
            eligible = cas_pick_item.get_eligible(data)
        assert len(eligible) == 1

    # 2026-05-18: human-language complexity aliases. A human who writes
    # `complexity: "small"` (or "easy" / "medium" / "standard") shouldn't
    # accidentally trip the title-keyword scan and get auto-skipped.
    # Drove bug-ops-api-costs-endpoint-underreports silently bouncing
    # off the picker.
    def test_complexity_alias_small_maps_to_simple(self):
        assert cas_pick_item.estimate_complexity({
            "title": "anything", "description": "anything", "complexity": "small"
        }) == "simple"

    def test_complexity_alias_easy_maps_to_simple(self):
        assert cas_pick_item.estimate_complexity({
            "title": "anything", "description": "anything", "complexity": "easy"
        }) == "simple"

    def test_complexity_alias_trivial_maps_to_simple(self):
        assert cas_pick_item.estimate_complexity({
            "title": "anything", "description": "anything", "complexity": "trivial"
        }) == "simple"

    def test_complexity_alias_medium_maps_to_moderate(self):
        assert cas_pick_item.estimate_complexity({
            "title": "anything", "description": "anything", "complexity": "medium"
        }) == "moderate"

    def test_complexity_alias_standard_maps_to_moderate(self):
        assert cas_pick_item.estimate_complexity({
            "title": "anything", "description": "anything", "complexity": "standard"
        }) == "moderate"

    def test_complexity_alias_bypasses_title_keyword_scan(self):
        """An item with `complexity: "small"` AND a title containing a
        COMPLEX_KEYWORD (e.g. "audit") MUST come back as "simple" because
        the explicit field wins. Previously "small" fell through to the
        keyword scan and the item was auto-skipped despite the human
        tagging it as small."""
        result = cas_pick_item.estimate_complexity({
            "title": "Audit emitted CSS for missing utility classes",
            "description": "anything",
            "complexity": "small",
        })
        assert result == "simple"

    def test_complexity_unknown_value_falls_through_to_keyword_scan(self):
        """Garbage values (typos, weird strings) should fall through to
        the title-keyword scan rather than silently mapping to anything."""
        result = cas_pick_item.estimate_complexity({
            "title": "Refactor the entire codebase comprehensively",
            "description": "anything",
            "complexity": "xyzfoobar",
        })
        assert result == "complex"  # "comprehensively" hits desc markers


class TestExecutorEligibleFlag:
    """The explicit `executor_eligible: false` flag opts items out of the
    autonomous picker (operator-only tasks, items needing human framing).
    Distinct from `complexity: complex` (auto-skip via keyword + flag).
    """

    def test_executor_eligible_false_skipped(self):
        data = _make_roadmap([
            _make_item("operator-task", executor_eligible=False, executor_skip_reason="operator_task"),
            _make_item("regular-task"),
        ])
        with patch.object(cas_pick_item, "get_failed_today", return_value=set()), \
             patch.object(cas_pick_item, "get_total_failures", return_value=0), \
             patch.object(cas_pick_item, "load_config", return_value={"skip_complex_items": True}):
            eligible = cas_pick_item.get_eligible(data)
        ids = [i["id"] for i in eligible]
        assert "operator-task" not in ids
        assert "regular-task" in ids

    def test_executor_eligible_true_or_absent_is_eligible(self):
        # Both explicit true AND absence of field treat as eligible
        data = _make_roadmap([
            _make_item("explicit-true", executor_eligible=True),
            _make_item("flag-absent"),  # no executor_eligible key
        ])
        with patch.object(cas_pick_item, "get_failed_today", return_value=set()), \
             patch.object(cas_pick_item, "get_total_failures", return_value=0), \
             patch.object(cas_pick_item, "load_config", return_value={"skip_complex_items": True}):
            eligible = cas_pick_item.get_eligible(data)
        ids = [i["id"] for i in eligible]
        assert "explicit-true" in ids
        assert "flag-absent" in ids

    def test_executor_eligible_falsy_string_does_not_count_as_false(self):
        # Defensive: only the exact boolean `False` triggers the skip.
        # Strings like "false" or "no" do NOT (human typo shouldn't
        # silently disable autonomous execution).
        data = _make_roadmap([
            _make_item("string-false", executor_eligible="false"),
            _make_item("string-no", executor_eligible="no"),
        ])
        with patch.object(cas_pick_item, "get_failed_today", return_value=set()), \
             patch.object(cas_pick_item, "get_total_failures", return_value=0), \
             patch.object(cas_pick_item, "load_config", return_value={"skip_complex_items": True}):
            eligible = cas_pick_item.get_eligible(data)
        ids = [i["id"] for i in eligible]
        # Both pass through as eligible: the strict `is False` check rejects strings
        assert "string-false" in ids
        assert "string-no" in ids


# ===========================================================================
# 5. Failure gating (existing logic, regression guard)
# ===========================================================================

class TestFailureGating:
    """Regression guards for daily failure and total failure thresholds."""

    def test_daily_failed_items_skipped(self):
        data = _make_roadmap([
            _make_item("failed-item"),
            _make_item("ok-item"),
        ])
        with patch.object(cas_pick_item, "get_failed_today", return_value={"failed-item"}), \
             patch.object(cas_pick_item, "get_total_failures", return_value=0), \
             patch.object(cas_pick_item, "load_config", return_value={"skip_complex_items": False}):
            eligible = cas_pick_item.get_eligible(data)
        ids = [i["id"] for i in eligible]
        assert "failed-item" not in ids
        assert "ok-item" in ids

    def test_total_failure_threshold_skips_items(self):
        data = _make_roadmap([_make_item("chronic-fail")])
        with patch.object(cas_pick_item, "get_failed_today", return_value=set()), \
             patch.object(cas_pick_item, "get_total_failures", return_value=6), \
             patch.object(cas_pick_item, "load_config", return_value={
                 "skip_complex_items": False,
                 "max_total_failures_before_defer": 6,
             }):
            eligible = cas_pick_item.get_eligible(data)
        assert len(eligible) == 0

    def test_below_threshold_still_eligible(self):
        data = _make_roadmap([_make_item("recovering")])
        with patch.object(cas_pick_item, "get_failed_today", return_value=set()), \
             patch.object(cas_pick_item, "get_total_failures", return_value=5), \
             patch.object(cas_pick_item, "load_config", return_value={
                 "skip_complex_items": False,
                 "max_total_failures_before_defer": 6,
             }):
            eligible = cas_pick_item.get_eligible(data)
        assert len(eligible) == 1


# ===========================================================================
# 5. Properties of the loop's source that hold at every call site
# ===========================================================================

class TestLoopSourceProperties:
    """What the loop does when it crashes, when it is stopped and when a
    server hangs is tested by running it (test_executor_loop_recovery.py,
    test_executor_loop_behavior.py). These read the source for what running
    one cycle cannot show: that no call site has been missed (every curl and
    every network command carries a time limit) and the order of two calls in
    the main program."""

    @staticmethod
    def _script_text():
        path = SCRIPTS_DIR / "cas-roadmap-loop.sh"
        return path.read_text()

    def test_no_command_in_the_loop_discards_work_it_does_not_own(self):
        """`git reset --hard`, a forced checkout, `git clean -f`, `git stash
        drop` and a forced push throw away changes the loop did not make (a
        person's edit, a person's stash, the remote's commits). The loop takes
        a merge back with `git reset --keep` (take_back_merge), which stops
        instead of overwriting. That a failed status mark, a failed journal
        update and an interrupted merge keep a person's edit is tested by
        running the loop (test_executor_loop_recovery.py); this reads every
        command, including the ones no run of the loop reaches."""
        import re
        code = [ln.strip() for ln in self._script_text().split("\n")
                if not ln.strip().startswith("#") and "echo " not in ln]
        pattern = (r"\bgit\b.*\b(reset\b.*--hard|checkout\b.*(\s-f\b|--force)|clean\b.*\s-\w*f|stash\s+drop"
                   r"|push\b.*(\s-f\b|--force))")
        assert [ln for ln in code if re.search(pattern, ln)] == []
        assert any("reset -q --keep" in ln for ln in code), "the take-back is the one reset, and it keeps edits"

    def test_every_curl_call_is_bounded(self):
        """A server that accepts a connection and never answers must not hold
        the loop: every curl invocation carries --max-time, either directly or
        by being ops_curl. (That a hung server does not hang the loop is tested
        by running it, in test_executor_loop_behavior.py.)"""
        import re
        text = self._script_text()
        start = text.index("bounded_curl() {")
        wrapper = text[start:text.index("\n}\n", start)]
        assert "--max-time" in wrapper and "--connect-timeout" in wrapper
        # and in size: a longer body is cut, which fails the call
        assert 'head -c "$OPS_MAX_BYTES"' in wrapper
        ops = text[text.index("ops_curl() {"):]
        assert "bounded_curl -H @-" in ops[:ops.index("\n}\n")], "ops_curl is bounded_curl plus the key"
        code = [ln for ln in text.split("\n") if not ln.strip().startswith("#")]
        bare = [ln.strip() for ln in code
                if re.search(r"(^|[\s(|;&])curl\s+-", ln) and "--max-time" not in ln]
        assert bare == [], f"curl without a time limit: {bare}"

    def test_a_time_limit_is_a_positive_number_or_the_default(self, tmp_path):
        """The limits come from the environment. To curl a limit of 0 means no
        limit at all, and a value that is not a number would make every call
        fail, so anything but a positive number falls back to the default.
        Runs the loop's own seconds_or in bash."""
        import subprocess
        text = self._script_text()
        start = text.index("seconds_or() {")
        function = text[start:text.index("\n}\n", start) + 3]
        probe = tmp_path / "probe.sh"
        probe.write_text(function + 'for v in "$@"; do printf "%s=%s\\n" "$v" "$(seconds_or "$v" DEFAULT)"; done\n')
        values = ["2.5", "10", "0.5", "", ".", "0", "0.0", "00", "1.2.3", "abc", "1e3", "5 x", "-3"]
        out = subprocess.run(["bash", str(probe), *values], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        accepted = dict(line.split("=", 1) for line in out.stdout.splitlines())
        assert accepted == {**{v: "DEFAULT" for v in values}, "2.5": "2.5", "10": "10", "0.5": "0.5"}
        for name, env, default in (("OPS_TIMEOUT_SEC", "CAS_OPS_TIMEOUT_SEC", 10),
                                   ("NET_TIMEOUT_SEC", "CAS_EXECUTOR_NET_TIMEOUT_SEC", 300),
                                   ("PUSH_TIMEOUT_SEC", "CAS_EXECUTOR_PUSH_TIMEOUT_SEC", 1800),
                                   ("GATE_TIMEOUT_SEC", "CAS_EXECUTOR_GATE_TIMEOUT_SEC", 1800)):
            assert f'{name}=$(seconds_or "${{{env}:-}}" {default})' in text

    def test_git_network_commands_run_under_a_time_limit(self):
        """fetch, pull and push talk to the remote, gh to the forge and the
        gate's npm install to the registry: each runs under with_timeout."""
        import re
        text = self._script_text()
        code = [ln for ln in text.split("\n") if not ln.strip().startswith("#")]
        for pattern in (r"\bgit fetch\b", r"\bgit pull\b", r"\bgit push\b", r"\bpr create\b"):
            # the commands themselves, not the messages that mention them
            lines = [ln.strip() for ln in code
                     if re.search(pattern, ln) and "echo " not in ln and "post_result " not in ln]
            assert lines, pattern
            assert all("with_timeout" in ln for ln in lines), lines
        assert 'with_timeout "$NET_TIMEOUT_SEC" gate_npm_install "$gate_path"' in text
        # the gate's two test commands have a limit of their own
        assert 'with_timeout "$GATE_TIMEOUT_SEC" gate_npm_test "$gate_path"' in text
        assert 'with_timeout "$GATE_TIMEOUT_SEC" gate_pytest "$gate_path" "$gate_ini"' in text

    # -------------------------------------------------------------------
    # Main-loop wiring
    # -------------------------------------------------------------------

    @staticmethod
    def _strip_comments(bash_text):
        """Strip `# ...` comments so ordering assertions don't trip on
        comment-mentions of function names."""
        lines = []
        for raw in bash_text.split("\n"):
            # Preserve lines; only strip the comment portion. Simplistic but
            # sufficient: bash comments start with #, we never embed a real #
            # inside single-quoted strings in main-loop ordering code.
            stripped = raw.split("#", 1)[0] if raw.lstrip().startswith("#") else raw
            lines.append(stripped)
        return "\n".join(lines)

    def test_main_loop_calls_check_crash_loop_before_acquire_lock(self):
        """check_crash_loop must run BEFORE acquire_lock so a wedged
        lockfile can't prevent the rate-limit back-off from firing."""
        text = self._script_text()
        main_marker = text.index("# ========== MAIN LOOP ==========")
        tail = self._strip_comments(text[main_marker:])
        check_idx = tail.index("check_crash_loop")
        acquire_idx = tail.index("acquire_lock")
        assert check_idx < acquire_idx, \
            f"check_crash_loop ({check_idx}) must be called BEFORE acquire_lock ({acquire_idx})"

    def test_main_loop_calls_sweep_after_cleanup_worktrees(self):
        """sweep_zombie_runs must run after acquire_lock + cleanup_worktrees
        so stale local state is cleared first and the DB sweep happens once."""
        text = self._script_text()
        main_marker = text.index("# ========== MAIN LOOP ==========")
        tail = self._strip_comments(text[main_marker:])
        cleanup_idx = tail.index("cleanup_worktrees\n")
        sweep_idx = tail.index("sweep_zombie_runs\n")
        assert cleanup_idx < sweep_idx
