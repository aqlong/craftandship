"""Tests for the release workflow tooling: the scripts and settings between a
commit and a promoted release. Most are shape checks (a file exists, a script
or the CI workflow contains a step), not runs of the workflow itself.

Covers: the CI workflow file, the executor's staging settings, pre-push hook
enforcement, risk classification, the production smoke-test script, version
bumping, changelog generation, QualityGateError non-retry, visual diff, and
the staging promotion script. The "Initiative N" headings below are the order
this tooling was added in.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = ROOT / "scripts"
CLI_DIR = ROOT / "cli"

# Ensure imports work
if str(ROOT / "queue") not in sys.path:
    sys.path.insert(0, str(ROOT / "queue"))
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))


# ===========================================================================
# Initiative 0: Python Tests in CI
# ===========================================================================

@pytest.fixture(scope="module")
def jobs():
    """The jobs of .github/workflows/test.yml, parsed."""
    return yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]


class TestCIWorkflow:
    """The `python` job of .github/workflows/test.yml, read as YAML: each
    assertion is about that job, not about text found somewhere in the file."""

    def test_ci_workflow_has_python_job(self, jobs):
        """test.yml has a python job alongside unit and ui, and it runs pytest
        on tests/ and on compose/tests/."""
        assert {"unit", "python", "ui"} <= set(jobs)
        commands = [step.get("run", "") for step in jobs["python"]["steps"]]
        assert any(re.search(r"\bpytest\b.*\btests/", c) and "compose/tests" not in c for c in commands)
        assert any(re.search(r"\bpytest\b.*\bcompose/tests", c) for c in commands)

    def test_ci_python_job_has_timeout(self, jobs):
        """The python job itself carries timeout-minutes, so a hung runner is
        cut off; every other job does too."""
        timeout = jobs["python"].get("timeout-minutes")
        assert isinstance(timeout, int) and 0 < timeout <= 60, timeout
        assert all(isinstance(job.get("timeout-minutes"), int) for job in jobs.values()), \
            {name: job.get("timeout-minutes") for name, job in jobs.items()}

    def test_ci_python_job_has_postgres(self, jobs):
        """The python job has a PostgreSQL service container, and the
        DATABASE_URL its tests read points at that container."""
        python = jobs["python"]
        service = python["services"]["postgres"]
        assert re.fullmatch(r"postgres:\d+", service["image"]), service["image"]
        env = service["env"]
        url = python["env"]["DATABASE_URL"]
        assert url == (f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
                       f"@localhost:5432/{env['POSTGRES_DB']}")
        assert "5432:5432" in service["ports"]


# ===========================================================================
# Initiative 1: Staging Environment
# ===========================================================================

class TestStagingConfig:
    def test_executor_config_has_target_branch(self):
        config = json.loads((SCRIPTS_DIR / "cas-executor-config.json").read_text())
        assert "target_branch" in config
        assert config["target_branch"] in ("master", "staging")

    def test_executor_config_has_tiered_governance(self):
        config = json.loads((SCRIPTS_DIR / "cas-executor-config.json").read_text())
        assert "tiered_governance" in config
        assert isinstance(config["tiered_governance"], bool)

    def test_executor_config_has_staging_workflow(self):
        config = json.loads((SCRIPTS_DIR / "cas-executor-config.json").read_text())
        assert "use_staging_workflow" in config
        assert isinstance(config["use_staging_workflow"], bool)

    def test_wait_for_deploy_has_staging_flag(self):
        """wait_for_deploy.py should support --staging flag."""
        script = (SCRIPTS_DIR / "wait_for_deploy.py").read_text()
        assert "--staging" in script
        assert "STAGING_BASE_URL" in script


# ===========================================================================
# Initiative 2: Blocking Quality Gates
# ===========================================================================


# ===========================================================================
# Initiative 3: Auto-Rollback
# ===========================================================================

class TestSmokeTests:
    def test_smoke_prod_module_loads(self):
        """smoke_prod.py should be importable."""
        spec = __import__("importlib").util.spec_from_file_location(
            "smoke_prod", str(SCRIPTS_DIR / "smoke_prod.py"))
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "smoke")

    def test_smoke_returns_correct_structure(self):
        """smoke() should return {passed, checks}."""
        spec = __import__("importlib").util.spec_from_file_location(
            "smoke_prod", str(SCRIPTS_DIR / "smoke_prod.py"))
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Test with a non-existent URL to verify structure
        with patch.dict(os.environ, {"CAS_STAGING_URL": ""}):
            result = mod.smoke("staging")
        assert "passed" in result
        assert "checks" in result
        assert isinstance(result["checks"], list)

    def test_promote_staging_exists_and_executable(self):
        """promote_staging.sh should exist and be executable."""
        script = SCRIPTS_DIR / "promote_staging.sh"
        assert script.exists()
        assert os.access(str(script), os.X_OK)

    def test_promote_staging_has_bypass(self):
        """promote_staging.sh should use CAS_PROMOTE=1 for pre-push bypass."""
        script = (SCRIPTS_DIR / "promote_staging.sh").read_text()
        assert "CAS_PROMOTE=1" in script

    def test_promote_staging_has_rollback(self):
        """promote_staging.sh should have rollback logic."""
        script = (SCRIPTS_DIR / "promote_staging.sh").read_text()
        assert "git revert" in script
        assert "PRODUCTION SMOKE FAILED" in script


# ===========================================================================
# Initiative 4: Executor Tiered Governance
# ===========================================================================

class TestRiskClassification:
    def test_classify_risk_module_loads(self):
        """classify_risk.py should be importable."""
        spec = __import__("importlib").util.spec_from_file_location(
            "classify_risk", str(SCRIPTS_DIR / "classify_risk.py"))
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "classify")

    def _load_classify(self):
        spec = __import__("importlib").util.spec_from_file_location(
            "classify_risk", str(SCRIPTS_DIR / "classify_risk.py"))
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.classify

    def test_auth_middleware_is_high_risk(self):
        classify = self._load_classify()
        assert classify(["web/src/middleware/auth.ts"]) == "high"

    def test_worker_pg_is_high_risk(self):
        classify = self._load_classify()
        assert classify(["queue/worker_pg.py"]) == "high"

    def test_build_py_is_high_risk(self):
        classify = self._load_classify()
        assert classify(["cli/build.py"]) == "high"

    def test_schema_sql_is_high_risk(self):
        classify = self._load_classify()
        assert classify(["web/src/db/schema.sql"]) == "high"

    def test_view_file_is_low_risk(self):
        classify = self._load_classify()
        assert classify(["web/views/login.ejs"]) == "low"

    def test_test_file_is_low_risk(self):
        classify = self._load_classify()
        assert classify(["tests/test_something.py"]) == "low"

    def test_docs_is_low_risk(self):
        classify = self._load_classify()
        assert classify(["docs/README.md"]) == "low"

    def test_many_files_is_high_risk(self):
        """More than 10 files changed is always high risk."""
        classify = self._load_classify()
        files = [f"web/views/page{i}.ejs" for i in range(12)]
        assert classify(files) == "high"

    def test_mixed_files_high_if_any_critical(self):
        classify = self._load_classify()
        files = ["web/views/login.ejs", "web/src/routes/auth.ts", "web/views/signup.ejs"]
        assert classify(files) == "high"

    def test_empty_files_is_low_risk(self):
        classify = self._load_classify()
        assert classify([]) == "low"

    def test_dockerfile_is_high_risk(self):
        classify = self._load_classify()
        assert classify(["Dockerfile.worker"]) == "high"

    def test_requirements_is_high_risk(self):
        classify = self._load_classify()
        assert classify(["requirements.worker.txt"]) == "high"

# ===========================================================================
# Initiative 5: Visual Regression Testing
# ===========================================================================

class TestVisualDiff:
    def test_visual_diff_module_loads(self):
        """visual_diff.py should be importable."""
        from visual_diff import compare_screenshots
        assert callable(compare_screenshots)

    def test_visual_diff_no_baseline_files(self):
        """Should fail gracefully with no baseline files."""
        from visual_diff import compare_screenshots
        with tempfile.TemporaryDirectory() as baseline, \
             tempfile.TemporaryDirectory() as candidate, \
             tempfile.TemporaryDirectory() as output:
            result = compare_screenshots(baseline, candidate, output)
            assert result["passed"] is False

    def test_visual_diff_identical_images(self):
        """Identical images should pass."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")

        from visual_diff import compare_screenshots
        with tempfile.TemporaryDirectory() as baseline, \
             tempfile.TemporaryDirectory() as candidate, \
             tempfile.TemporaryDirectory() as output:
            # Create identical test images
            img = Image.new("RGB", (100, 100), color=(255, 0, 0))
            img.save(f"{baseline}/test.png")
            img.save(f"{candidate}/test.png")

            result = compare_screenshots(baseline, candidate, output)
            assert result["passed"] is True
            assert len(result["pages"]) == 1
            assert result["pages"][0]["mismatch_pct"] == 0.0

    def test_visual_diff_different_images(self):
        """Different images should fail."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")

        from visual_diff import compare_screenshots
        with tempfile.TemporaryDirectory() as baseline, \
             tempfile.TemporaryDirectory() as candidate, \
             tempfile.TemporaryDirectory() as output:
            img_a = Image.new("RGB", (100, 100), color=(255, 0, 0))
            img_b = Image.new("RGB", (100, 100), color=(0, 0, 255))
            img_a.save(f"{baseline}/test.png")
            img_b.save(f"{candidate}/test.png")

            result = compare_screenshots(baseline, candidate, output)
            assert result["passed"] is False
            assert result["pages"][0]["mismatch_pct"] > 0.5

    def test_visual_diff_missing_candidate(self):
        """Missing candidate image should report 100% mismatch."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")

        from visual_diff import compare_screenshots
        with tempfile.TemporaryDirectory() as baseline, \
             tempfile.TemporaryDirectory() as candidate, \
             tempfile.TemporaryDirectory() as output:
            img = Image.new("RGB", (100, 100), color=(255, 0, 0))
            img.save(f"{baseline}/homepage.png")
            # No candidate file

            result = compare_screenshots(baseline, candidate, output)
            assert result["passed"] is False
            assert result["pages"][0]["mismatch_pct"] == 1.0

    def test_visual_diff_writes_diff_image(self):
        """Should write amplified diff image to output dir."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")

        from visual_diff import compare_screenshots
        with tempfile.TemporaryDirectory() as baseline, \
             tempfile.TemporaryDirectory() as candidate, \
             tempfile.TemporaryDirectory() as output:
            img_a = Image.new("RGB", (100, 100), color=(255, 0, 0))
            img_b = Image.new("RGB", (100, 100), color=(200, 0, 0))
            img_a.save(f"{baseline}/page.png")
            img_b.save(f"{candidate}/page.png")

            result = compare_screenshots(baseline, candidate, output)
            diff_path = result["pages"][0].get("diff_path")
            assert diff_path is not None
            assert Path(diff_path).exists()

    def test_pillow_in_requirements(self):
        """Pillow should be in requirements.worker.txt."""
        reqs = (ROOT / "requirements.worker.txt").read_text()
        assert "Pillow" in reqs


# ===========================================================================
# Initiative 6: Release Tags + Changelog
# ===========================================================================

class TestVersioning:
    def test_version_json_exists(self):
        data = json.loads((ROOT / "version.json").read_text())
        assert "version" in data
        assert "released_at" in data

    def test_version_is_semver(self):
        data = json.loads((ROOT / "version.json").read_text())
        parts = data["version"].split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_bump_version_dry_run(self):
        result = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "bump_version.py"), "--dry-run", "--bump", "patch"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "Would bump" in result.stdout

    @pytest.mark.requires_tools("git")
    def test_bump_version_analyze_commits(self):
        result = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "bump_version.py"), "--analyze-commits"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout.strip() in ("patch", "minor", "major")

    @pytest.mark.requires_tools("git")
    def test_generate_changelog_dry_run(self):
        result = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "generate_changelog.py"), "--dry-run"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        # Should produce some output (commit list)
        assert len(result.stdout) > 10

    def test_app_ts_has_version(self):
        """web/src/app.ts should expose appVersion in /health."""
        app_ts = (ROOT / "web/src/app.ts").read_text()
        assert "appVersion" in app_ts
        assert "version.json" in app_ts
        assert "APP_VERSION" in app_ts  # Railway fallback

    def test_health_response_includes_version(self):
        """Health endpoint should include version field."""
        app_ts = (ROOT / "web/src/app.ts").read_text()
        assert "version: appVersion" in app_ts


# ===========================================================================
# Pre-push hook enforcement
# ===========================================================================

class TestPrePushHook:
    # What the staging gate does when it runs (blocked, both bypasses, other
    # branches, config off) is exercised in tests/test_hook_golden_guard.py.
    def test_hook_exists(self):
        hook = ROOT / ".githooks/pre-push"
        assert hook.exists()

    def test_web_tests_run_once_not_per_beta_mode(self):
        """The BETA_MODE tier system was removed, so nothing under web/ reads
        BETA_MODE and running the web tests once per BETA_MODE state is dead
        weight. Guard both halves: no source reads it, and no hook, script or
        workflow re-runs the suite per state."""
        readers = []
        for sub in ("src", "views"):
            for path in (ROOT / "web" / sub).rglob("*"):
                if path.is_file() and path.suffix in (".ts", ".js", ".ejs", ".json"):
                    if "BETA_MODE" in path.read_text(errors="ignore"):
                        readers.append(str(path.relative_to(ROOT)))
        assert not readers, f"web source reads BETA_MODE again: {readers}"

        for rel in (".githooks/pre-push", "scripts/run_web_tests.sh",
                    ".github/workflows/test.yml"):
            text = (ROOT / rel).read_text()
            assert "BETA_MODE" not in text, f"{rel} still mentions BETA_MODE"

        workflow = (ROOT / ".github/workflows/test.yml").read_text()
        unit_job = workflow[workflow.index("  unit:"):workflow.index("  python:")]
        assert unit_job.count("run: npm test") == 1, (
            "the unit job must run the web tests exactly once"
        )

# ===========================================================================
# Cross-initiative integration
# ===========================================================================

class TestWorkflowIntegration:
    def test_promote_script_runs_smoke_tests(self):
        """promote_staging.sh should call smoke_prod.py for both pre and post."""
        script = (SCRIPTS_DIR / "promote_staging.sh").read_text()
        assert "smoke_prod.py --env staging" in script
        assert "smoke_prod.py --env production" in script

    def test_promote_script_runs_version_bump(self):
        """promote_staging.sh should bump version and generate changelog."""
        script = (SCRIPTS_DIR / "promote_staging.sh").read_text()
        assert "bump_version.py" in script
        assert "generate_changelog.py" in script

    def test_promote_script_tags_for_rollback(self):
        """promote_staging.sh should create a rollback tag before merge."""
        script = (SCRIPTS_DIR / "promote_staging.sh").read_text()
        assert "release/good/" in script

    def test_promote_script_has_visual_regression(self):
        """promote_staging.sh should optionally run visual regression."""
        script = (SCRIPTS_DIR / "promote_staging.sh").read_text()
        assert "capture_cas_screenshots.py" in script
        assert "visual_diff" in script
        assert "--skip-visual" in script

    def test_promote_script_has_dry_run(self):
        """promote_staging.sh should support --dry-run."""
        script = (SCRIPTS_DIR / "promote_staging.sh").read_text()
        assert "--dry-run" in script
        assert "DRY RUN" in script

    def test_all_new_scripts_exist(self):
        """All scripts from the plan should exist."""
        expected = [
            "scripts/smoke_prod.py",
            "scripts/promote_staging.sh",
            "scripts/classify_risk.py",
            "scripts/capture_cas_screenshots.py",
            "scripts/bump_version.py",
            "scripts/generate_changelog.py",
            "cli/visual_diff.py",
            "version.json",
        ]
        for path in expected:
            assert (ROOT / path).exists(), f"Missing: {path}"

    def test_all_new_workflows_exist(self):
        """GitHub Actions workflows should exist."""
        assert (ROOT / ".github/workflows/test.yml").exists()
        assert (ROOT / ".github/workflows/hook-integrity.yml").exists()
        # test.yml should have python job
        workflow = (ROOT / ".github/workflows/test.yml").read_text()
        assert "Python Tests" in workflow
