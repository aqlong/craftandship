# tests/test_classify_push.py: Tests for scripts/classify_push.py
#
# The classifier is the brain of the fast-path vs staging-path decision. A bug
# here could silently ship rendered-UI changes through the fast path (bypassing
# staging verification) or force staging on safe docs-only pushes (wasting time
# and polluting the staging branch). The rules are small and pure, so unit
# tests are the right level.

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).parent.parent
_SCRIPT = _ROOT / "scripts" / "classify_push.py"

# Load the script as a module (it's executable, not in a package).
# Register in sys.modules so string-based patch targets like
# "classify_push.subprocess" resolve correctly.
_spec = importlib.util.spec_from_file_location("classify_push", _SCRIPT)
classify_push = importlib.util.module_from_spec(_spec)
sys.modules["classify_push"] = classify_push
_spec.loader.exec_module(classify_push)


# ---------------------------------------------------------------------------
# _is_staging_path()
# ---------------------------------------------------------------------------

class TestIsStagingPath:
    @pytest.mark.parametrize("path", [
        "web/src/server.ts",
        "web/src/routes/auth.ts",
        "web/views/layout.ejs",
        "web/public/style.css",
        "web/package.json",
        "web/package-lock.json",
        "web/tsconfig.json",
        "web/vitest.config.ts",
        "web/playwright.config.ts",
    ])
    def test_web_source_paths_are_staging(self, path):
        assert classify_push._is_staging_path(path) is True

    @pytest.mark.parametrize("path", [
        "cli/build.py",
        "cli/deploy.py",
        "cli/deploy_fix.py",
        "cli/fix.py",
        "cli/review.py",
        "cli/design_review.py",
        "cli/design_fix.py",
        "cli/qa.py",
        "cli/qa_fix.py",
        "cli/ui_test.py",
        "cli/test.py",
        "cli/intake.py",
        "cli/validate.py",
        "cli/api_client.py",
        "cli/failure_kb.py",
        "cli/design_data.py",
        "cli/build_trace.py",
    ])
    def test_cli_pipeline_modules_are_staging(self, path):
        assert classify_push._is_staging_path(path) is True

    @pytest.mark.parametrize("path", [
        "cli/templates/routes_admin.js",
        "cli/templates/views_admin_settings.ejs",
        "cli/stock_photos/wellness/01.jpg",
    ])
    def test_cli_templates_and_stock_photos_are_staging(self, path):
        assert classify_push._is_staging_path(path) is True

    @pytest.mark.parametrize("path", [
        "queue/worker_pg.py",
        "monitor/ping.py",
        "monitor/diagnose.py",
        "ops/agent.py",
        "ops/scanner.py",
    ])
    def test_worker_modules_are_staging(self, path):
        assert classify_push._is_staging_path(path) is True

    @pytest.mark.parametrize("path", [
        "Dockerfile",
        "Dockerfile.worker",
        "railway.toml",
        "requirements.worker.txt",
        "requirements.txt",
        "package.json",
        "package-lock.json",
    ])
    def test_infra_files_are_staging(self, path):
        assert classify_push._is_staging_path(path) is True

    def test_coordinator_roadmap_json_is_NOT_staging(self):
        # Churn file: auto-updated by executor between commits.
        assert classify_push._is_staging_path("coordinator/roadmap.json") is False

    @pytest.mark.parametrize("path", [
        "coordinator/tick.py",
        "coordinator/scorer.py",
        "coordinator/agents/backend_engineer.py",
        "coordinator/state.py",
        "coordinator/work_detection.py",
    ])
    def test_coordinator_modules_are_staging(self, path):
        # Everything in coordinator/ except roadmap.json
        assert classify_push._is_staging_path(path) is True

    @pytest.mark.parametrize("path", [
        "docs/plans/foo.md",
        "scripts/classify_push.py",
        "tests/test_foo.py",
        "web/tests/unit/routes.auth.test.ts",
        ".github/workflows/test.yml",
        ".githooks/pre-push",
        "CLAUDE.md",
        "README.md",
    ])
    def test_fast_safe_files_are_not_staging(self, path):
        assert classify_push._is_staging_path(path) is False

    def test_unknown_file_at_repo_root_is_not_staging(self):
        # _is_staging_path returns False for unknown paths. The fail-safe
        # fallback happens in classify_files() via the "unknown" bucket.
        assert classify_push._is_staging_path("some_random_file.xyz") is False


# ---------------------------------------------------------------------------
# _is_fast_safe_path()
# ---------------------------------------------------------------------------

class TestIsFastSafePath:
    @pytest.mark.parametrize("path", [
        "docs/plans/foo.md",
        "docs/LAUNCH.md",
        "scripts/classify_push.py",
        "scripts/cas-exec",
        "tests/test_foo.py",
        "web/tests/unit/routes.auth.test.ts",
        ".github/workflows/test.yml",
        ".githooks/pre-push",
        "CLAUDE.md",
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        "version.json",
        ".gitignore",
        ".editorconfig",
    ])
    def test_fast_safe_paths_hit(self, path):
        assert classify_push._is_fast_safe_path(path) is True

    @pytest.mark.parametrize("path", [
        "coordinator/roadmap.json",
        "cli/failure_kb.json",
        "scripts/cas-executor-config.json",
    ])
    def test_churn_files_are_fast_safe(self, path):
        # These are auto-updated by the worker/executor between commits.
        assert classify_push._is_fast_safe_path(path) is True

    @pytest.mark.parametrize("path", [
        "web/src/server.ts",
        "cli/build.py",
        "queue/worker_pg.py",
        "Dockerfile",
        "some_random_file.xyz",
    ])
    def test_staging_and_unknown_paths_are_not_fast_safe(self, path):
        assert classify_push._is_fast_safe_path(path) is False


# ---------------------------------------------------------------------------
# classify_files(): the core verdict function
# ---------------------------------------------------------------------------

class TestClassifyFiles:
    def test_empty_file_list_is_empty_verdict(self):
        verdict, fast, staging, unknown = classify_push.classify_files([])
        assert verdict == "empty"
        assert fast == []
        assert staging == []
        assert unknown == []

    def test_all_fast_safe_files_yield_fast_verdict(self):
        files = [
            "docs/plans/foo.md",
            "scripts/new_tool.py",
            "tests/test_foo.py",
            ".github/workflows/test.yml",
        ]
        verdict, fast, staging, unknown = classify_push.classify_files(files)
        assert verdict == "fast"
        assert sorted(fast) == sorted(files)
        assert staging == []
        assert unknown == []

    def test_single_staging_file_triggers_staging_verdict(self):
        files = [
            "docs/plans/foo.md",           # fast-safe
            "web/src/routes/auth.ts",      # staging
        ]
        verdict, fast, staging, unknown = classify_push.classify_files(files)
        assert verdict == "staging"
        assert fast == ["docs/plans/foo.md"]
        assert staging == ["web/src/routes/auth.ts"]
        assert unknown == []

    def test_unknown_path_defaults_to_staging(self):
        # Fail-safe behavior: unclassified paths go to staging, never fast.
        files = ["some_random_root_file.xyz"]
        verdict, fast, staging, unknown = classify_push.classify_files(files)
        assert verdict == "staging"
        assert fast == []
        assert staging == []
        assert unknown == ["some_random_root_file.xyz"]

    def test_mixed_fast_and_unknown_yields_staging(self):
        files = [
            "docs/plans/foo.md",
            "totally_new_dir/file.py",
        ]
        verdict, fast, staging, unknown = classify_push.classify_files(files)
        assert verdict == "staging"
        assert fast == ["docs/plans/foo.md"]
        assert unknown == ["totally_new_dir/file.py"]

    def test_coordinator_roadmap_is_fast_safe_but_other_coordinator_is_staging(self):
        files = [
            "coordinator/roadmap.json",
            "coordinator/tick.py",
        ]
        verdict, fast, staging, unknown = classify_push.classify_files(files)
        assert verdict == "staging"
        assert fast == ["coordinator/roadmap.json"]
        assert staging == ["coordinator/tick.py"]

    def test_churn_files_alone_are_fast(self):
        files = [
            "coordinator/roadmap.json",
            "cli/failure_kb.json",
            "scripts/cas-executor-config.json",
        ]
        verdict, fast, staging, unknown = classify_push.classify_files(files)
        assert verdict == "fast"
        assert sorted(fast) == sorted(files)

    def test_web_tests_are_fast_but_web_src_is_staging(self):
        # Critical distinction: tests don't ship to prod, src does.
        files = [
            "web/tests/unit/routes.auth.test.ts",  # fast
            "web/src/routes/auth.ts",              # staging
        ]
        verdict, fast, staging, unknown = classify_push.classify_files(files)
        assert verdict == "staging"
        assert fast == ["web/tests/unit/routes.auth.test.ts"]
        assert staging == ["web/src/routes/auth.ts"]

    def test_empty_strings_in_file_list_are_skipped(self):
        files = ["", "docs/plans/foo.md", "", ""]
        verdict, fast, staging, unknown = classify_push.classify_files(files)
        assert verdict == "fast"
        assert fast == ["docs/plans/foo.md"]

    def test_docker_files_are_staging(self):
        files = ["Dockerfile.worker"]
        verdict, fast, staging, unknown = classify_push.classify_files(files)
        assert verdict == "staging"
        assert staging == ["Dockerfile.worker"]

    def test_root_package_json_is_staging_but_version_json_is_fast(self):
        # package.json ships to generated apps → staging.
        # version.json is a repo metadata file → fast.
        files_staging = ["package.json"]
        verdict, _, staging, _ = classify_push.classify_files(files_staging)
        assert verdict == "staging"
        assert staging == ["package.json"]

        files_fast = ["version.json"]
        verdict, fast, _, _ = classify_push.classify_files(files_fast)
        assert verdict == "fast"
        assert fast == ["version.json"]


# ---------------------------------------------------------------------------
# CLI integration tests (via subprocess)
# ---------------------------------------------------------------------------

class TestCliStdinMode:
    """Exercise the full script through its --stdin interface."""

    def _run(self, input_lines: list[str], *extra_args: str) -> dict:
        result = subprocess.run(
            ["python3", str(_SCRIPT), "--stdin", *extra_args],
            input="\n".join(input_lines),
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
        )
        assert result.returncode == 0, f"classify_push failed: {result.stderr}"
        return json.loads(result.stdout) if "--terse" not in extra_args else result.stdout.strip()

    def test_stdin_fast_only(self):
        out = self._run(["docs/plans/foo.md", "scripts/tool.py"])
        assert out["verdict"] == "fast"
        assert out["needs_staging"] == []

    def test_stdin_mixed_goes_staging(self):
        out = self._run(["docs/plans/foo.md", "web/src/server.ts"])
        assert out["verdict"] == "staging"
        assert "web/src/server.ts" in out["needs_staging"]
        assert "docs/plans/foo.md" in out["fast_safe"]

    def test_stdin_unknown_goes_staging_fail_safe(self):
        out = self._run(["totally_new_thing.xyz"])
        assert out["verdict"] == "staging"
        assert "totally_new_thing.xyz" in out["needs_staging"]

    def test_stdin_empty_yields_empty_verdict(self):
        out = self._run([""])
        assert out["verdict"] == "empty"

    def test_stdin_terse_mode_prints_verdict_only(self):
        out = self._run(["docs/plans/foo.md"], "--terse")
        assert out == "fast"

    def test_stdin_coordinator_roadmap_alone_is_fast(self):
        out = self._run(["coordinator/roadmap.json"])
        assert out["verdict"] == "fast"

    def test_stdin_coordinator_non_roadmap_is_staging(self):
        out = self._run(["coordinator/tick.py"])
        assert out["verdict"] == "staging"


# ---------------------------------------------------------------------------
# get_uncommitted_files(): git status --porcelain parsing
# ---------------------------------------------------------------------------

class TestGetUncommittedFiles:
    """Ensure porcelain parsing correctly distinguishes modified vs untracked."""

    def _mock_porcelain(self, raw: str):
        """Return a CompletedProcess-like patch for subprocess.check_output."""
        return patch(
            "classify_push.subprocess.check_output",
            return_value=raw,
        )

    def test_modified_only(self):
        with self._mock_porcelain(" M web/src/foo.ts\nM  cli/bar.py\n"):
            mod, untracked = classify_push.get_uncommitted_files()
        assert sorted(mod) == ["cli/bar.py", "web/src/foo.ts"]
        assert untracked == []

    def test_untracked_only(self):
        with self._mock_porcelain("?? new_file.py\n?? docs/plans/new.md\n"):
            mod, untracked = classify_push.get_uncommitted_files()
        assert mod == []
        assert sorted(untracked) == ["docs/plans/new.md", "new_file.py"]

    def test_mixed_modified_and_untracked(self):
        raw = " M web/src/foo.ts\n?? new_file.py\nA  cli/bar.py\n"
        with self._mock_porcelain(raw):
            mod, untracked = classify_push.get_uncommitted_files()
        assert sorted(mod) == ["cli/bar.py", "web/src/foo.ts"]
        assert untracked == ["new_file.py"]

    def test_renamed_file_uses_new_path(self):
        with self._mock_porcelain("R  old/path.py -> new/path.py\n"):
            mod, untracked = classify_push.get_uncommitted_files()
        assert mod == ["new/path.py"]
        assert untracked == []

    def test_empty_output(self):
        with self._mock_porcelain(""):
            mod, untracked = classify_push.get_uncommitted_files()
        assert mod == []
        assert untracked == []

    def test_subprocess_failure_returns_empty(self):
        with patch(
            "classify_push.subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "git"),
        ):
            mod, untracked = classify_push.get_uncommitted_files()
        assert mod == []
        assert untracked == []
