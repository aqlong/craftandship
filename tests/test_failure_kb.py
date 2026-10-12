# tests/test_failure_kb.py - Tests for cli/failure_kb.py
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))

from cli.failure_kb import (
    normalize_error,
    lookup,
    record_fix,
    get_prompt_hints,
    get_build_prompt_rules,
    get_review_rules,
    get_stats,
    record_review_issues,
    fuzzy_lookup,
    find_similar,
    extract_deploy_crash_signature,
    is_generic_crash_signature,
    _tokenize,
    _tf_vector,
    _cosine_similarity,
    _load_kb,
    _load_review_kb,
    _save_kb,
    KB_PATH,
)


@pytest.fixture(autouse=True)
def temp_kb(tmp_path, monkeypatch, request):
    """Use a temporary KB file for each test.

    Tests that exercise `_get_review_kb_path()` directly (TestGetReviewKbPath)
    must opt out of the review-kb monkeypatch by marking the class
    `@pytest.mark.no_review_kb_patch`, otherwise they'd be testing the
    lambda stub instead of the real function.
    """
    kb_file = tmp_path / "failure_kb.json"
    review_kb_file = tmp_path / "review_kb.json"
    import cli.failure_kb as kb_mod
    monkeypatch.setattr(kb_mod, "KB_PATH", kb_file)
    if not request.node.get_closest_marker("no_review_kb_patch"):
        monkeypatch.setattr(kb_mod, "_get_review_kb_path", lambda: review_kb_file)
    yield kb_file


class TestNormalizeError:
    def test_strips_uuids(self):
        sig = normalize_error("Build 87654321-4321-4321-8321-cba987654321 failed")
        assert "<UUID>" in sig
        assert "87654321" not in sig

    def test_strips_ports(self):
        sig = normalize_error("Expected redirect, got http://localhost:4567/login")
        assert "<PORT>" in sig
        assert "4567" not in sig

    def test_strips_file_paths(self):
        sig = normalize_error("Error in /app/routes/auth.js at line 42")
        assert "<FILE>" in sig

    def test_strips_line_col(self):
        sig = normalize_error("SyntaxError at file.js:42:13")
        assert "<LINE>:<COL>" in sig

    def test_collapses_whitespace(self):
        sig = normalize_error("error   with    spaces")
        assert "  " not in sig

    def test_truncates_long_errors(self):
        sig = normalize_error("x" * 500)
        assert len(sig) <= 300

    def test_empty_input(self):
        assert normalize_error("") == ""
        assert normalize_error(None) == ""


class TestRecordAndLookup:
    def test_record_and_lookup(self):
        record_fix("login redirect failed", "Fixed auth.js redirect",
                    ["routes/auth.js"], fix_category="auth")
        entry = lookup("login redirect failed")
        assert entry is not None
        assert entry["success_count"] == 1
        assert entry["fix_category"] == "auth"

    def test_repeated_records_increment(self):
        for _ in range(3):
            record_fix("CSRF missing", "Added CSRF token",
                        ["views/form.ejs"], fix_category="csrf")
        entry = lookup("CSRF missing")
        assert entry["success_count"] == 3

    def test_failure_increments_failure_count(self):
        record_fix("some error", "tried fix", ["file.js"], succeeded=False)
        entry = lookup("some error")
        assert entry["failure_count"] == 1
        assert entry["success_count"] == 0

    def test_lookup_returns_none_for_unknown(self):
        assert lookup("never seen this error") is None

    def test_normalized_matching(self):
        record_fix("Error on localhost:4567/login", "Fixed redirect",
                    ["routes/auth.js"])
        # Different port should match same signature
        entry = lookup("Error on localhost:9999/login")
        assert entry is not None


class TestPromptHints:
    def test_no_hints_for_unknown_error(self):
        assert get_prompt_hints("brand new error") == ""

    def test_hints_after_enough_successes(self):
        for _ in range(3):
            record_fix("session race", "Added req.session.save()",
                        ["routes/auth.js"], fix_category="session")
        hints = get_prompt_hints("session race")
        assert "KNOWN FIX PATTERN" in hints
        assert "session" in hints

    def test_no_hints_for_low_success_rate(self):
        record_fix("flaky error", "tried A", ["a.js"], succeeded=True)
        record_fix("flaky error", "tried B", ["b.js"], succeeded=False)
        record_fix("flaky error", "tried C", ["c.js"], succeeded=False)
        hints = get_prompt_hints("flaky error")
        assert hints == ""


class TestBuildPromptRules:
    def test_no_rules_when_empty(self):
        assert get_build_prompt_rules() == ""

    def test_rules_after_threshold(self):
        for _ in range(4):
            record_fix("bcrypt mismatch", "Fixed seed hash",
                        ["db/seed.sql"], fix_category="seed_data")
        rules = get_build_prompt_rules()
        assert "Learned rules" in rules
        assert "seed_data" in rules

    def test_low_success_rate_excluded(self):
        for _ in range(2):
            record_fix("unstable fix", "tried X", ["x.js"], succeeded=True)
        for _ in range(5):
            record_fix("unstable fix", "tried Y", ["y.js"], succeeded=False)
        rules = get_build_prompt_rules()
        assert rules == ""


class TestStats:
    def test_empty_stats(self):
        stats = get_stats()
        assert stats["total_entries"] == 0

    def test_stats_after_records(self):
        for _ in range(4):
            record_fix("auth middleware error", "fix auth", ["auth.js"], fix_category="auth")
        record_fix("css stylesheet not loading", "fix css", ["style.css"], fix_category="styling")
        stats = get_stats()
        assert stats["total_entries"] == 2
        assert stats["categories"]["auth"] == 1
        assert stats["auto_applicable"] == 1  # auth has 4 successes >= threshold


class TestKBPruning:
    def test_prune_over_max(self, monkeypatch):
        import cli.failure_kb as kb_mod
        monkeypatch.setattr(kb_mod, "MAX_ENTRIES", 5)
        for i in range(10):
            record_fix(f"error {i}", f"fix {i}", [f"file{i}.js"])
        kb = _load_kb()
        assert len(kb) <= 5


class TestTestBuildAppNameExclusion:
    """Regression for the failure_kb noise audit (2026-05-04).

    `QA failed: test1` (376x) and `QA failed: plots page renders; create plot
    form submits` (918x) topped the failure-count list because manually-queued
    test/dogfood builds with placeholder app_names looped through qa_fix.py
    forever, each iteration calling record_fix(succeeded=False).
    """

    def test_test_prefix_skipped_in_record_fix(self):
        record_fix("QA failed: test1", "Claude returned no QA fixes", [],
                   fix_category="qa", succeeded=False, app_name="test1")
        assert lookup("QA failed: test1") is None

    def test_test_dash_prefix_skipped(self):
        record_fix("some error", "fix", [], app_name="test-build-foo")
        assert lookup("some error") is None

    def test_coach_simple_bug_verify_skipped(self):
        record_fix("plots failed", "fix", [],
                   app_name="coach-simple-bug-verify")
        assert lookup("plots failed") is None

    def test_phase_followup_skipped(self):
        record_fix("plots failed", "fix", [],
                   app_name="phase2-followup-simple")
        assert lookup("plots failed") is None

    def test_dogfood_skipped(self):
        record_fix("err", "fix", [], app_name="dogfood-haven-river")
        assert lookup("err") is None

    def test_e2e_skipped(self):
        record_fix("err", "fix", [], app_name="e2e-pipeline-test")
        assert lookup("err") is None

    def test_plots_app_name_skipped(self):
        record_fix("err", "fix", [], app_name="plots-foo")
        assert lookup("err") is None

    def test_real_app_name_recorded(self):
        # Coordinator's automated test specs use real archetype-shaped names
        # and MUST still record their failures.
        record_fix("login redirect failed", "Fixed auth.js redirect",
                   ["routes/auth.js"], fix_category="auth",
                   app_name="recipe-meal-planner")
        entry = lookup("login redirect failed")
        assert entry is not None
        assert entry["success_count"] == 1

    def test_no_app_name_records(self):
        # Legacy callers without app_name plumbing record everything.
        record_fix("login redirect failed", "fix", ["a.js"])
        assert lookup("login redirect failed") is not None

    def test_empty_app_name_records(self):
        record_fix("login redirect failed", "fix", ["a.js"], app_name="")
        assert lookup("login redirect failed") is not None

    def test_none_app_name_records(self):
        record_fix("login redirect failed", "fix", ["a.js"], app_name=None)
        assert lookup("login redirect failed") is not None

    def test_test_substring_inside_real_name_records(self):
        # The pattern is a prefix anchor; "contest", "latest", etc. don't match.
        record_fix("login redirect failed", "fix", ["a.js"],
                   app_name="contest-dashboard")
        assert lookup("login redirect failed") is not None

    def test_review_issues_test_app_skipped(self):
        record_review_issues(
            [{"severity": "critical", "issue": "Bad route", "file": "routes/foo.js"}],
            app_name="test-build-stress",
        )
        from cli.failure_kb import _load_review_kb
        assert _load_review_kb() == {}

    def test_review_issues_real_app_recorded(self):
        record_review_issues(
            [{"severity": "critical", "issue": "Bad route", "file": "routes/foo.js"}],
            app_name="neighborhood-cms",
        )
        from cli.failure_kb import _load_review_kb
        assert len(_load_review_kb()) == 1

    def test_is_test_build_helper_direct(self):
        from cli.failure_kb import _is_test_build_app_name
        assert _is_test_build_app_name("test1") is True
        assert _is_test_build_app_name("test_foo") is True
        assert _is_test_build_app_name("test-foo") is True
        assert _is_test_build_app_name("test") is True
        assert _is_test_build_app_name("TEST1") is True  # case-insensitive
        assert _is_test_build_app_name("coach-simple-bug-verify-1") is True
        assert _is_test_build_app_name("phase2-followup-x") is True
        assert _is_test_build_app_name("dogfood") is True
        assert _is_test_build_app_name("e2e-foo") is True
        assert _is_test_build_app_name("plots") is True
        assert _is_test_build_app_name("plot-tracker") is True
        assert _is_test_build_app_name("recipe-meal-planner") is False
        assert _is_test_build_app_name("contestant-tracker") is False
        assert _is_test_build_app_name("testimonial-app") is False

    def test_testimonial_prefix_excluded_by_design(self):
        # Documenting the known false-positive: app_name starting with "testi"
        # (e.g. testimonial-something) would match `^test([-_0-9]|$)` only if
        # followed by `-` `_` or digit. "testi" is followed by `i`, so it does NOT match.
        from cli.failure_kb import _is_test_build_app_name
        assert _is_test_build_app_name("testimonial-app") is False
        assert _is_test_build_app_name("tested-app") is False


# ---------------------------------------------------------------------------
# record_review_issues()
# ---------------------------------------------------------------------------

class TestRecordReviewIssues:
    def test_records_critical_issues(self):
        issues = [
            {"severity": "critical", "file": "routes/tasks.js", "issue": "Missing CSRF token in POST form"},
        ]
        record_review_issues(issues)
        kb = _load_review_kb()
        assert len(kb) == 1
        entry = list(kb.values())[0]
        assert entry["count"] == 1
        assert "CSRF" in entry["description"]

    def test_increments_count_on_repeat(self):
        issues = [
            {"severity": "critical", "file": "routes/tasks.js", "issue": "Missing CSRF token in POST form"},
        ]
        record_review_issues(issues)
        record_review_issues(issues)
        record_review_issues(issues)
        kb = _load_review_kb()
        assert len(kb) == 1
        entry = list(kb.values())[0]
        assert entry["count"] == 3

    def test_ignores_non_critical_issues(self):
        issues = [
            {"severity": "warning", "file": "routes/tasks.js", "issue": "Long function body"},
            {"severity": "info", "file": "views/tasks/list.ejs", "issue": "Consider adding aria labels"},
        ]
        record_review_issues(issues)
        kb = _load_review_kb()
        assert len(kb) == 0

    def test_normalizes_route_file_paths(self):
        issues = [
            {"severity": "critical", "file": "routes/tasks.js",
             "issue": "routes/tasks.js is missing auth middleware"},
            {"severity": "critical", "file": "routes/projects.js",
             "issue": "routes/projects.js is missing auth middleware"},
        ]
        record_review_issues(issues)
        kb = _load_review_kb()
        # Both should normalize to the same signature (routes/<SLUG>.js)
        assert len(kb) == 1
        entry = list(kb.values())[0]
        assert entry["count"] == 2

    def test_normalizes_view_file_paths(self):
        issues = [
            {"severity": "critical", "file": "views/tasks/list.ejs",
             "issue": "views/tasks/list missing XSS escaping"},
            {"severity": "critical", "file": "views/projects/list.ejs",
             "issue": "views/projects/list missing XSS escaping"},
        ]
        record_review_issues(issues)
        kb = _load_review_kb()
        # Both should match the same pattern after normalization
        assert len(kb) == 1

    def test_empty_issues_is_noop(self):
        record_review_issues([])
        kb = _load_review_kb()
        assert len(kb) == 0

    def test_skips_empty_description(self):
        issues = [{"severity": "critical", "file": "x.js", "issue": ""}]
        record_review_issues(issues)
        kb = _load_review_kb()
        assert len(kb) == 0


# ---------------------------------------------------------------------------
# get_review_rules()
# ---------------------------------------------------------------------------

class TestGetReviewRules:
    def test_empty_when_no_issues(self):
        assert get_review_rules() == ""

    def test_returns_rules_for_recurring_issues(self):
        issues = [
            {"severity": "critical", "file": "routes/tasks.js",
             "issue": "POST route missing CSRF validation"},
        ]
        # Record enough times to meet min_count (default 2)
        record_review_issues(issues)
        record_review_issues(issues)
        rules = get_review_rules(min_count=2)
        assert "Recurring code review issues" in rules
        assert "REVIEW FINDING" in rules
        assert "CSRF" in rules

    def test_below_min_count_returns_empty(self):
        issues = [
            {"severity": "critical", "file": "routes/tasks.js",
             "issue": "POST route missing CSRF validation"},
        ]
        record_review_issues(issues)  # only once
        rules = get_review_rules(min_count=2)
        assert rules == ""

    def test_caps_at_15_rules(self):
        # Create 20 distinct critical issues, each recorded 3 times
        for i in range(20):
            issues = [
                {"severity": "critical", "file": f"file{i}.js",
                 "issue": f"Unique critical issue number {i} in the codebase"},
            ]
            for _ in range(3):
                record_review_issues(issues)
        rules = get_review_rules(min_count=2)
        assert rules != ""
        # Count the number of REVIEW FINDING lines
        finding_count = rules.count("REVIEW FINDING")
        assert finding_count <= 15


# ---------------------------------------------------------------------------
# Vector similarity: tokenization
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_produces_word_and_ngram_tokens(self):
        tokens = _tokenize("CSRF missing in form")
        word_tokens = [t for t in tokens if t.startswith("w:")]
        ngram_tokens = [t for t in tokens if t.startswith("n:")]
        assert len(word_tokens) > 0
        assert len(ngram_tokens) > 0
        assert "w:csrf" in word_tokens
        assert "w:missing" in word_tokens

    def test_empty_input(self):
        assert _tokenize("") == []
        assert _tokenize(None) == []

    def test_filters_short_words(self):
        tokens = _tokenize("a is in the x")
        word_tokens = [t for t in tokens if t.startswith("w:")]
        # Single-char words filtered out; "is", "in" kept (2+ chars); "the" kept
        assert "w:a" not in word_tokens
        assert "w:x" not in word_tokens
        assert "w:is" in word_tokens

    def test_lowercases(self):
        tokens = _tokenize("EADDRINUSE TypeError")
        word_tokens = [t for t in tokens if t.startswith("w:")]
        assert "w:eaddrinuse" in word_tokens
        assert "w:typeerror" in word_tokens


class TestTfVector:
    def test_produces_nonzero_values(self):
        vec = _tf_vector(["w:csrf", "w:csrf", "w:form"])
        assert vec["w:csrf"] > vec["w:form"]  # repeated token has higher TF
        assert all(v > 0 for v in vec.values())

    def test_empty_input(self):
        vec = _tf_vector([])
        assert vec == {}


class TestCosineSimilarity:
    def test_identical_vectors(self):
        vec = {"a": 1.0, "b": 2.0}
        assert abs(_cosine_similarity(vec, vec) - 1.0) < 0.001

    def test_orthogonal_vectors(self):
        vec_a = {"a": 1.0}
        vec_b = {"b": 1.0}
        assert _cosine_similarity(vec_a, vec_b) == 0.0

    def test_partial_overlap(self):
        vec_a = {"a": 1.0, "b": 1.0}
        vec_b = {"b": 1.0, "c": 1.0}
        score = _cosine_similarity(vec_a, vec_b)
        assert 0.0 < score < 1.0

    def test_empty_vectors(self):
        assert _cosine_similarity({}, {"a": 1.0}) == 0.0
        assert _cosine_similarity({"a": 1.0}, {}) == 0.0


# ---------------------------------------------------------------------------
# Vector similarity: fuzzy lookup
# ---------------------------------------------------------------------------

class TestFuzzyLookup:
    def test_finds_similar_error(self):
        # Record a fix for EADDRINUSE on port 8080
        record_fix(
            "failed; Error: listen EADDRINUSE: address already in use :::8080",
            "Use process.env.PORT",
            ["server.js"],
            fix_category="routing",
        )
        # Look up a slightly different EADDRINUSE error (port 3000)
        result = fuzzy_lookup("failed; Error: listen EADDRINUSE: address already in use :::3000")
        assert result is not None
        entry, score = result
        assert score > 0.5
        assert "PORT" in entry["fix_description"]

    def test_returns_none_for_unrelated_error(self):
        record_fix("CSRF token missing in POST form", "Added CSRF field",
                    ["views/form.ejs"], fix_category="csrf")
        result = fuzzy_lookup("database connection timeout after 30 seconds")
        # Should not match; completely unrelated
        assert result is None

    def test_empty_kb_returns_none(self):
        assert fuzzy_lookup("any error") is None

    def test_empty_error_returns_none(self):
        assert fuzzy_lookup("") is None

    def test_matches_similar_deploy_crashes(self):
        record_fix(
            "deploy-crash; TypeError: Cannot read property 'id' of undefined",
            "Added null check before accessing .id",
            ["routes/items.js"],
            fix_category="routing",
        )
        # Similar TypeError with different property name
        result = fuzzy_lookup(
            "deploy-crash; TypeError: Cannot read property 'name' of undefined"
        )
        assert result is not None
        entry, score = result
        assert score > 0.5

    def test_respects_threshold(self):
        record_fix("session race condition on login redirect",
                    "Added session.save()", ["routes/auth.js"])
        # Very strict threshold should reject partial matches
        result = fuzzy_lookup("session timeout error", threshold=0.99)
        assert result is None


class TestFindSimilar:
    def test_returns_ranked_results(self):
        record_fix("login redirect failed", "Fix redirect",
                    ["routes/auth.js"], fix_category="auth")
        record_fix("CSRF missing in login form", "Added CSRF",
                    ["views/login.ejs"], fix_category="csrf")
        record_fix("database pool exhausted", "Increased pool size",
                    ["db/index.js"], fix_category="unknown")
        results = find_similar("login page redirect broken")
        assert len(results) > 0
        # Login-related entry should rank higher than database entry
        assert results[0]["similarity"] > results[-1]["similarity"]

    def test_empty_error_returns_empty(self):
        assert find_similar("") == []

    def test_top_k_limits_results(self):
        for i in range(10):
            record_fix(f"unique error pattern number {i} in module",
                        f"fix {i}", [f"file{i}.js"])
        results = find_similar("unique error pattern number five in module", top_k=3)
        assert len(results) <= 3


# ---------------------------------------------------------------------------
# Integration: lookup with fuzzy fallback
# ---------------------------------------------------------------------------

class TestLookupWithFuzzy:
    def test_exact_match_preferred(self):
        record_fix("login redirect failed", "Exact fix",
                    ["routes/auth.js"], fix_category="auth")
        # Exact match should work without fuzzy
        entry = lookup("login redirect failed", use_fuzzy=False)
        assert entry is not None
        assert entry["fix_description"] == "Exact fix"

    def test_fuzzy_fallback_when_no_exact(self):
        record_fix(
            "failed; Error: listen EADDRINUSE: address already in use :::8080",
            "Use process.env.PORT",
            ["server.js"],
        )
        # Slightly different error (port 5000 instead of 8080)
        entry = lookup(
            "failed; Error: listen EADDRINUSE: address already in use :::5000",
            use_fuzzy=True,
        )
        assert entry is not None
        assert "PORT" in entry["fix_description"]

    def test_fuzzy_disabled_returns_none(self):
        record_fix("session race on login", "session.save()",
                    ["routes/auth.js"])
        # Without fuzzy, a different wording won't match
        entry = lookup("session racing condition during login", use_fuzzy=False)
        assert entry is None

    def test_hints_include_similarity_for_fuzzy(self):
        for _ in range(3):
            record_fix(
                "deploy-crash; TypeError: Cannot read property 'id' of undefined",
                "Added null check",
                ["routes/items.js"],
                fix_category="routing",
            )
        # Different property name triggers fuzzy match
        hints = get_prompt_hints(
            "deploy-crash; TypeError: Cannot read property 'email' of undefined"
        )
        assert "similarity match" in hints
        assert "KNOWN FIX PATTERN" in hints

    def test_hints_no_similarity_label_for_exact(self):
        for _ in range(3):
            record_fix("CSRF token missing", "Added CSRF field",
                        ["views/form.ejs"], fix_category="csrf")
        hints = get_prompt_hints("CSRF token missing")
        assert "KNOWN FIX PATTERN" in hints
        assert "similarity match" not in hints


# ---------------------------------------------------------------------------
# TestGetKbPath: path resolution: env override, Railway volume, bootstrap
# ---------------------------------------------------------------------------
# Regression for 2026-04-19: cli/failure_kb.json was baked into the Railway
# worker container image. Every deploy reset the file to the committed copy,
# silently overwriting hours of runtime failure-count accumulation. The fix
# moves persistent reads/writes to the existing /app/builds volume mount
# when RAILWAY_ENVIRONMENT is set, with a seed-on-first-access bootstrap
# so a fresh deploy still benefits from the committed learnings.

class TestAtomicWrite:
    """_atomic_write_json uses tmp-file + os.replace so a SIGTERM mid-write
    can never leave a partial/truncated KB file. Regression against the
    previous `path.write_text()` behavior which was non-atomic and would
    leave the KB as invalid JSON if the worker was killed mid-save.

    Critical because `_load_kb` treats invalid JSON as empty dict, a torn
    write would effectively wipe the KB for the next read.
    """

    def test_atomic_write_creates_file_from_nothing(self, tmp_path):
        from cli.failure_kb import _atomic_write_json
        path = tmp_path / "kb.json"
        _atomic_write_json(path, {"a": 1})
        assert path.exists()
        assert json.loads(path.read_text()) == {"a": 1}

    def test_atomic_write_leaves_no_tmp_file_on_success(self, tmp_path):
        from cli.failure_kb import _atomic_write_json
        path = tmp_path / "kb.json"
        _atomic_write_json(path, {"x": 2})
        tmp = path.with_suffix(".json.tmp")
        assert not tmp.exists(), "tmp file must be renamed away on success"

    def test_atomic_write_preserves_existing_on_replace_failure(self, tmp_path, monkeypatch):
        """If os.replace raises (e.g. cross-fs rename), the ORIGINAL file must
        stay intact, only way a partial-write could cause data loss is if
        we overwrote the real file directly instead of via rename."""
        from cli.failure_kb import _atomic_write_json
        import cli.failure_kb as kb_mod
        path = tmp_path / "kb.json"
        path.write_text(json.dumps({"original": True}))
        original_content = path.read_text()

        def boom(*_, **__):
            raise OSError("simulated fault during rename")
        monkeypatch.setattr(kb_mod.os, "replace", boom)
        with pytest.raises(OSError):
            _atomic_write_json(path, {"new": True})
        # Original untouched
        assert path.read_text() == original_content

    def test_save_kb_uses_atomic_write(self, tmp_path, monkeypatch):
        """_save_kb must delegate to _atomic_write_json, not call
        path.write_text() directly. Regression guard: we fixed this on
        2026-04-19 to prevent torn writes during Railway deploy drain."""
        import cli.failure_kb as kb_mod
        from cli.failure_kb import _save_kb
        calls = []
        monkeypatch.setattr(kb_mod, "_atomic_write_json",
                            lambda path, data: calls.append((path, data)))
        _save_kb({"sig-1": {"failure_count": 5}})
        assert len(calls) == 1, "_save_kb must go through _atomic_write_json"
        assert calls[0][1] == {"sig-1": {"failure_count": 5}}

    def test_save_review_kb_uses_atomic_write(self, tmp_path, monkeypatch):
        import cli.failure_kb as kb_mod
        from cli.failure_kb import _save_review_kb
        calls = []
        monkeypatch.setattr(kb_mod, "_atomic_write_json",
                            lambda path, data: calls.append((path, data)))
        _save_review_kb({"rule-1": {"count": 3}})
        assert len(calls) == 1, "_save_review_kb must go through _atomic_write_json"
        assert calls[0][1] == {"rule-1": {"count": 3}}

    def test_simulated_crash_mid_write_leaves_old_file_valid(self, tmp_path, monkeypatch):
        """End-to-end: simulate the torn-write scenario we're protecting
        against. Old file has accumulated data; we try to save new data
        but the 'replace' step fails partway. Old file must still be
        parseable JSON (not truncated). This is the scenario `_load_kb`
        can't recover from without atomic writes, partial JSON → empty KB."""
        from cli.failure_kb import _atomic_write_json
        path = tmp_path / "kb.json"
        accumulated = {"sig-A": {"failure_count": 100, "success_count": 80}}
        path.write_text(json.dumps(accumulated))

        def boom(*_, **__):
            raise OSError("simulated SIGTERM during rename")
        monkeypatch.setattr("cli.failure_kb.os.replace", boom)
        with pytest.raises(OSError):
            _atomic_write_json(path, {"sig-B": {"failure_count": 1}})

        # Old file still parses: _load_kb would return the accumulated dict, NOT {}
        assert json.loads(path.read_text()) == accumulated


class TestGetKbPath:
    """_get_kb_path() precedence + bootstrap behavior."""

    def _clear_env(self, monkeypatch):
        """Scrub env vars that could pick the wrong path branch."""
        for var in ("FAILURE_KB_PATH", "RAILWAY_ENVIRONMENT"):
            monkeypatch.delenv(var, raising=False)

    def test_default_is_local_kb_path(self, monkeypatch, tmp_path):
        """No env vars set → returns the module KB_PATH (which tests patch)."""
        from cli.failure_kb import _get_kb_path
        import cli.failure_kb as kb_mod
        self._clear_env(monkeypatch)
        monkeypatch.setattr(kb_mod, "KB_PATH", tmp_path / "local-kb.json")
        assert _get_kb_path() == tmp_path / "local-kb.json"

    def test_explicit_env_override_wins(self, monkeypatch, tmp_path):
        """FAILURE_KB_PATH env var beats everything."""
        from cli.failure_kb import _get_kb_path
        self._clear_env(monkeypatch)
        override = tmp_path / "override.json"
        monkeypatch.setenv("FAILURE_KB_PATH", str(override))
        # Even with RAILWAY_ENVIRONMENT set, the explicit override still wins
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        assert _get_kb_path() == override

    def test_railway_env_selects_volume_path(self, monkeypatch, tmp_path):
        """RAILWAY_ENVIRONMENT set → <volume-dir>/failure_kb.json.

        Patches `_RAILWAY_VOLUME_DIR` so the test doesn't need write access
        to the real `/builds` on the developer's machine.
        """
        from cli.failure_kb import _get_kb_path
        import cli.failure_kb as kb_mod
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        fake_volume = tmp_path / "volume"
        monkeypatch.setattr(kb_mod, "_RAILWAY_VOLUME_DIR", fake_volume)
        # Stub seed so bootstrap is a no-op
        monkeypatch.setattr(kb_mod, "_KB_SEED_PATH", tmp_path / "no-seed.json")
        path = _get_kb_path()
        assert path == fake_volume / "failure_kb.json"
        assert fake_volume.is_dir(), "volume dir should be created (mkdir parents=True, exist_ok=True)"

    def test_railway_bootstrap_seeds_from_committed_file(self, monkeypatch, tmp_path):
        """First Railway access with empty volume → copy committed seed."""
        from cli.failure_kb import _get_kb_path
        import cli.failure_kb as kb_mod
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")

        seed = tmp_path / "cli-seed" / "failure_kb.json"
        seed.parent.mkdir()
        seed_content = json.dumps({"committed sig": {"failure_count": 42}})
        seed.write_text(seed_content)

        fake_volume = tmp_path / "volume"
        monkeypatch.setattr(kb_mod, "_KB_SEED_PATH", seed)
        monkeypatch.setattr(kb_mod, "_RAILWAY_VOLUME_DIR", fake_volume)

        path = _get_kb_path()
        assert path.exists(), "bootstrap must create the volume file"
        assert json.loads(path.read_text()) == {"committed sig": {"failure_count": 42}}

    def test_railway_bootstrap_no_overwrite_when_volume_file_exists(
            self, monkeypatch, tmp_path):
        """Bootstrap is a one-shot seed; must NEVER clobber existing accumulation."""
        from cli.failure_kb import _get_kb_path
        import cli.failure_kb as kb_mod
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")

        seed = tmp_path / "cli-seed" / "failure_kb.json"
        seed.parent.mkdir()
        seed.write_text(json.dumps({"stale seed": {"failure_count": 1}}))

        fake_volume = tmp_path / "volume"
        fake_volume.mkdir()
        accumulated = {"real prod sig": {"failure_count": 9999}}
        (fake_volume / "failure_kb.json").write_text(json.dumps(accumulated))

        monkeypatch.setattr(kb_mod, "_KB_SEED_PATH", seed)
        monkeypatch.setattr(kb_mod, "_RAILWAY_VOLUME_DIR", fake_volume)

        path = _get_kb_path()
        assert path == fake_volume / "failure_kb.json"
        # Critical invariant: accumulated data UNCHANGED
        assert json.loads(path.read_text()) == accumulated

    def test_railway_bootstrap_noop_when_seed_missing(self, monkeypatch, tmp_path):
        """If the committed seed doesn't exist (edge case), don't crash;
        return the volume path so the first save creates an empty KB."""
        from cli.failure_kb import _get_kb_path
        import cli.failure_kb as kb_mod
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")

        fake_volume = tmp_path / "volume"
        monkeypatch.setattr(kb_mod, "_KB_SEED_PATH", tmp_path / "nonexistent.json")
        monkeypatch.setattr(kb_mod, "_RAILWAY_VOLUME_DIR", fake_volume)

        path = _get_kb_path()
        assert path == fake_volume / "failure_kb.json"
        assert not path.exists()  # no bootstrap happened, but no crash either

    def test_railway_volume_dir_is_absolute_builds(self):
        """Module default for _RAILWAY_VOLUME_DIR must be /builds (absolute).
        Regression: pre-fix code used Path(__file__).parent.parent / 'builds'
        which resolved to /app/builds, ephemeral container storage."""
        from cli.failure_kb import _RAILWAY_VOLUME_DIR
        assert str(_RAILWAY_VOLUME_DIR) == "/builds", \
            f"expected /builds (matches Railway volumeInstances.mountPath), got {_RAILWAY_VOLUME_DIR}"


class TestLoadSaveUsesResolvedPath:
    """_load_kb and _save_kb must go through _get_kb_path() every call
   , never cache the path, so env changes mid-process take effect."""

    def test_save_writes_to_override_path(self, monkeypatch, tmp_path):
        from cli.failure_kb import _save_kb
        monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
        target = tmp_path / "target.json"
        monkeypatch.setenv("FAILURE_KB_PATH", str(target))
        _save_kb({"sig-A": {"failure_count": 3}})
        assert target.exists()
        assert json.loads(target.read_text()) == {"sig-A": {"failure_count": 3}}

    def test_load_reads_from_override_path(self, monkeypatch, tmp_path):
        from cli.failure_kb import _load_kb
        monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
        target = tmp_path / "target.json"
        target.write_text(json.dumps({"sig-B": {"failure_count": 7}}))
        monkeypatch.setenv("FAILURE_KB_PATH", str(target))
        assert _load_kb() == {"sig-B": {"failure_count": 7}}

    def test_save_into_a_missing_directory_is_dropped_without_raising(self, monkeypatch, tmp_path):
        """_save_kb does not create parent directories: on the Railway volume
        _get_kb_path makes the directory, and elsewhere a write that cannot
        happen is dropped (the KB is a cache, not critical state). It must
        neither raise nor leave a partial file behind."""
        from cli.failure_kb import _save_kb
        monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
        nested = tmp_path / "nonexistent" / "failure_kb.json"
        monkeypatch.setenv("FAILURE_KB_PATH", str(nested))
        _save_kb({"x": {"failure_count": 1}})
        assert not nested.exists()
        assert not nested.parent.exists()

    def test_end_to_end_railway_bootstrap_then_record(self, monkeypatch, tmp_path):
        """Simulate a fresh Railway deploy: empty volume → bootstrap on first
        access → subsequent record_fix lands on volume, not on the committed seed."""
        from cli.failure_kb import record_fix
        import cli.failure_kb as kb_mod

        monkeypatch.delenv("FAILURE_KB_PATH", raising=False)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")

        seed = tmp_path / "cli-seed" / "failure_kb.json"
        seed.parent.mkdir()
        seed.write_text(json.dumps({"SeededSig": {"failure_count": 100, "success_count": 50}}))

        fake_volume = tmp_path / "volume"
        monkeypatch.setattr(kb_mod, "_KB_SEED_PATH", seed)
        monkeypatch.setattr(kb_mod, "_RAILWAY_VOLUME_DIR", fake_volume)

        volume_file = fake_volume / "failure_kb.json"
        assert not volume_file.exists()

        # Calling record_fix triggers _get_kb_path → bootstrap → _save_kb(volume)
        record_fix("new error x", "fix y", ["file.py"], fix_category="test", succeeded=True)

        assert volume_file.exists()
        final = json.loads(volume_file.read_text())
        assert "SeededSig" in final, "seed must have been preserved through bootstrap"
        assert len(final) >= 2, "new record must have been added"

        # Committed seed file was NOT modified (prod writes don't contaminate dev)
        seed_after = json.loads(seed.read_text())
        assert seed_after == {"SeededSig": {"failure_count": 100, "success_count": 50}}


@pytest.mark.no_review_kb_patch
class TestGetReviewKbPath:
    """_get_review_kb_path() mirrors _get_kb_path() behavior (env override,
    Railway volume, bootstrap). Regression for the pre-existing bug where
    review_kb.json was written to /app/builds/ (ephemeral container storage)
    instead of /builds/ (Railway volume mount), silently wiped on every deploy."""

    def _clear_env(self, monkeypatch):
        for var in ("REVIEW_KB_PATH", "RAILWAY_ENVIRONMENT"):
            monkeypatch.delenv(var, raising=False)

    def test_default_is_local_cli_dir(self, monkeypatch):
        from cli.failure_kb import _get_review_kb_path, REVIEW_KB_PATH
        self._clear_env(monkeypatch)
        assert _get_review_kb_path() == REVIEW_KB_PATH

    def test_explicit_env_override_wins(self, monkeypatch, tmp_path):
        from cli.failure_kb import _get_review_kb_path
        self._clear_env(monkeypatch)
        target = tmp_path / "override-review.json"
        monkeypatch.setenv("REVIEW_KB_PATH", str(target))
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        assert _get_review_kb_path() == target

    def test_railway_uses_absolute_builds_volume(self, monkeypatch, tmp_path):
        """On Railway, returns <volume>/review_kb.json, using the same
        _RAILWAY_VOLUME_DIR constant as failure_kb for a single source of truth."""
        from cli.failure_kb import _get_review_kb_path
        import cli.failure_kb as kb_mod
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        fake_volume = tmp_path / "volume"
        monkeypatch.setattr(kb_mod, "_RAILWAY_VOLUME_DIR", fake_volume)
        monkeypatch.setattr(kb_mod, "_REVIEW_KB_SEED_PATH", tmp_path / "no-seed.json")
        path = _get_review_kb_path()
        assert path == fake_volume / "review_kb.json"
        assert fake_volume.is_dir()

    def test_railway_bootstrap_seeds_from_committed_review_kb(self, monkeypatch, tmp_path):
        from cli.failure_kb import _get_review_kb_path
        import cli.failure_kb as kb_mod
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        seed = tmp_path / "cli-seed" / "review_kb.json"
        seed.parent.mkdir()
        seed.write_text(json.dumps({"rule-1": {"count": 5}}))
        fake_volume = tmp_path / "volume"
        monkeypatch.setattr(kb_mod, "_REVIEW_KB_SEED_PATH", seed)
        monkeypatch.setattr(kb_mod, "_RAILWAY_VOLUME_DIR", fake_volume)
        path = _get_review_kb_path()
        assert path.exists()
        assert json.loads(path.read_text()) == {"rule-1": {"count": 5}}

    def test_railway_bootstrap_no_overwrite_of_accumulated_review_data(
            self, monkeypatch, tmp_path):
        """Same invariant as failure_kb: bootstrap must never clobber
        accumulated volume data."""
        from cli.failure_kb import _get_review_kb_path
        import cli.failure_kb as kb_mod
        self._clear_env(monkeypatch)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        seed = tmp_path / "cli-seed" / "review_kb.json"
        seed.parent.mkdir()
        seed.write_text(json.dumps({"stale": {"count": 1}}))
        fake_volume = tmp_path / "volume"
        fake_volume.mkdir()
        accumulated = {"real rule": {"count": 9999}}
        (fake_volume / "review_kb.json").write_text(json.dumps(accumulated))
        monkeypatch.setattr(kb_mod, "_REVIEW_KB_SEED_PATH", seed)
        monkeypatch.setattr(kb_mod, "_RAILWAY_VOLUME_DIR", fake_volume)
        path = _get_review_kb_path()
        assert json.loads(path.read_text()) == accumulated


class TestExtractDeployCrashSignature:
    """RCA 2026-05-04: better signature extraction for deploy-crash logs.

    Pre-fix: worker built `"deploy-crash; " + " ".join(_pre_dep_logs[-5:])`
    which produced too-generic signatures like `deploy-crash; CRASHED` when
    Railway's tail was just status words. Distinct underlying crashes
    (TypeError, missing module, etc.) bucketed into one poisoned KB entry.

    Post-fix: walk logs preferring lines that look like real errors before
    falling back to last-N joining.
    """

    def test_empty_logs_returns_health_check_failed(self):
        assert extract_deploy_crash_signature([]) == "deploy-crash; health-check-failed"
        assert extract_deploy_crash_signature(None) == "deploy-crash; health-check-failed"
        assert extract_deploy_crash_signature(["", "  ", None]) == "deploy-crash; health-check-failed"

    def test_traceback_line_wins(self):
        logs = [
            "Server started", "Connecting to DB",
            "Traceback (most recent call last):",
            "CRASHED",  # status-word noise after the real error
        ]
        sig = extract_deploy_crash_signature(logs)
        assert "Traceback" in sig
        assert sig.startswith("deploy-crash; ")

    def test_typeerror_line_preferred_over_status_words(self):
        logs = ["log line 5", "TypeError: Cannot read property 'id' of undefined", "CRASHED"]
        sig = extract_deploy_crash_signature(logs)
        assert "TypeError" in sig
        assert "Cannot read property" in sig

    def test_referenceerror_match(self):
        logs = ["ok", "ReferenceError: foo is not defined", "exited 1"]
        sig = extract_deploy_crash_signature(logs)
        assert "ReferenceError" in sig

    def test_cannot_find_module_match(self):
        logs = ["init", "Error: Cannot find module 'express'", "exit 1"]
        sig = extract_deploy_crash_signature(logs)
        assert "Cannot find module" in sig

    def test_econnrefused_match(self):
        logs = ["boot", "Error: connect ECONNREFUSED 127.0.0.1:5432", "CRASHED"]
        sig = extract_deploy_crash_signature(logs)
        assert "ECONNREFUSED" in sig

    def test_python_attributeerror_match(self):
        logs = ["starting", "AttributeError: 'NoneType' object has no attribute 'split'"]
        sig = extract_deploy_crash_signature(logs)
        assert "AttributeError" in sig

    def test_falls_back_to_recent_lines_when_no_indicator(self):
        # All lines lack error markers; return last few non-status lines.
        logs = [
            "Server boot phase 1",
            "Loading config",
            "Connecting to upstream service",
            "CRASHED",  # generic, should be filtered out of the join
        ]
        sig = extract_deploy_crash_signature(logs)
        assert sig.startswith("deploy-crash; ")
        assert "CRASHED" not in sig
        assert "upstream service" in sig

    def test_status_only_logs_use_legacy_shape(self):
        # Everything is a generic status word; preserve the legacy
        # last-line shape so it's at least debuggable.
        logs = ["CRASHED", "exit 1", "killed"]
        sig = extract_deploy_crash_signature(logs)
        assert sig.startswith("deploy-crash; ")
        # Should not be empty.
        assert len(sig) > len("deploy-crash; ")

    def test_signature_truncated_to_200_chars(self):
        long_error = "TypeError: " + "x" * 500
        logs = [long_error]
        sig = extract_deploy_crash_signature(logs)
        # signature body is 200 chars max + "deploy-crash; " prefix
        body = sig[len("deploy-crash; "):]
        assert len(body) <= 200

    def test_most_recent_error_preferred_when_multiple(self):
        # When multiple error lines, the most recent one (later in logs) wins.
        logs = [
            "TypeError: old issue at line 12",
            "things continued",
            "ReferenceError: actual final cause",
            "CRASHED",
        ]
        sig = extract_deploy_crash_signature(logs)
        assert "ReferenceError" in sig
        assert "actual final cause" in sig
        assert "TypeError" not in sig

    def test_accepts_non_string_log_entries(self):
        # Defensive: real Railway responses sometimes include dicts/None.
        logs = [None, "", {"ts": 123}, "TypeError: real error", "CRASHED"]
        sig = extract_deploy_crash_signature(logs)
        assert "TypeError" in sig


class TestIsGenericCrashSignature:
    """RCA 2026-05-04: blocklist generic deploy-crash signatures from the
    worker's failure_kb early-exit gate.

    Generic signatures bucket distinct underlying crashes; the bucket's
    historical 0% success rate cannot be trusted as a per-build predictor.
    Worker MUST NOT skip the deploy_fix loop on a generic signature even
    when the bucket has high failure_count + 0% rate.
    """

    @pytest.mark.parametrize("sig", [
        "deploy-crash; CRASHED",
        "deploy-crash; crashed",
        "deploy-crash; FAILED",
        "deploy-crash; crash",
        "deploy-crash; crash on startup",
        "deploy-crash; crash on Startup",  # case variations
        "deploy-crash; health-check-failed",
        "deploy-crash; exit 1",
        "deploy-crash; exit 137",
        "deploy-crash; OOMKilled",
        "deploy-crash; SIGTERM",
        "deploy-crash; SIGKILL",
        "deploy-crash; killed",
        "deploy-crash; terminated",
        "deploy-crash; log line 55 log line 56 log line 57",
    ])
    def test_known_generic_patterns(self, sig):
        assert is_generic_crash_signature(sig) is True, f"expected generic: {sig}"

    @pytest.mark.parametrize("sig", [
        "deploy-crash; TypeError: Cannot read property 'id' of undefined",
        "deploy-crash; ReferenceError: foo is not defined",
        "deploy-crash; Error: Cannot find module 'express'",
        "deploy-crash; ECONNREFUSED 127.0.0.1:5432",
        "deploy-crash; Traceback (most recent call last):",
        "deploy-crash; Cannot find module 'pg'",
        "deploy-crash; ModuleNotFoundError: No module named 'foo'",
        "deploy-crash; AttributeError: 'NoneType' object has no attribute",
        "qa-failed; admin login redirect failed",
        "deploy-fix-failed; deploy_fix.py exited with code 1",
    ])
    def test_specific_signatures_not_generic(self, sig):
        assert is_generic_crash_signature(sig) is False, f"expected specific: {sig}"

    def test_empty_or_none_inputs(self):
        assert is_generic_crash_signature("") is False
        assert is_generic_crash_signature(None) is False
        assert is_generic_crash_signature("   ") is False
        assert is_generic_crash_signature(123) is False

    def test_extractor_then_blocklist_integration(self):
        # End-to-end: extractor falls through to status-word last-line for
        # a CRASHED-only logs payload; blocklist must catch the resulting
        # signature so the worker doesn't apply the early-exit gate.
        logs = ["CRASHED"]
        sig = extract_deploy_crash_signature(logs)
        # The signature is generic; blocklist should catch it.
        assert is_generic_crash_signature(sig) is True

    def test_extractor_real_error_signature_not_blocklisted(self):
        # End-to-end: extractor finds a real TypeError; blocklist should
        # NOT match (worker should apply the early-exit gate normally).
        logs = ["init", "TypeError: Cannot read property 'id' of undefined", "CRASHED"]
        sig = extract_deploy_crash_signature(logs)
        assert is_generic_crash_signature(sig) is False


# Every signature in the shipped failure KB must be found by lookup() from its own
# text, through the exact path or the similarity fallback. Entries that name a
# sample event are among them, so the rule each one carries fires on that text.
def test_every_shipped_signature_matches_its_own_text(monkeypatch):
    import cli.failure_kb as kb_mod

    shipped = Path(__file__).resolve().parent.parent / "cli" / "failure_kb.json"
    real_kb = json.loads(shipped.read_text(encoding="utf-8"))
    monkeypatch.setattr(kb_mod, "_load_kb", lambda: real_kb)
    misses = [key[:80] for key, entry in real_kb.items() if kb_mod.lookup(key) is not entry]
    assert not misses, f"signatures that no longer match their own text: {misses}"
