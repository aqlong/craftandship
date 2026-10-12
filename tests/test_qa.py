# tests/test_qa.py - Tests for cli/qa.py spec-aware QA agent

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

from qa import (
    _extract_function_code,
    _normalize_results,
    _skipped_result,
    QA_TEST_PORT,
)
# The exec of generated code lives in the child-process runner, not in qa.py.
from qa_runner import execute_test_code as _execute_test_code


def _run_in_process(test_code, base_url, build_path, database_url=None, timeout=None):
    """Stand-in for qa._run_tests_in_child in the run_qa tests below.

    The real function starts a child Python process that launches a browser.
    These tests are about run_qa's bookkeeping, so the generated code is run
    here against a MagicMock page instead. The child process itself is covered
    by TestRunTestsInChild.
    """
    from qa import _bounded_results
    return _bounded_results(_execute_test_code(test_code, MagicMock(), base_url))

SAMPLE_SPEC = {
    "app_name": "garden-tracker",
    "user_roles": [
        {"role": "admin"},
        {"role": "member"},
    ],
    "pages": [
        {"path": "/dashboard", "auth_required": True, "allowed_roles": ["admin", "member"]},
        {"path": "/plots", "auth_required": True, "allowed_roles": ["admin", "member"]},
        {"path": "/admin", "auth_required": True, "allowed_roles": ["admin"]},
        {"path": "/login", "auth_required": False, "allowed_roles": []},
    ],
    "data_models": [{"name": "plot", "fields": [{"name": "location"}, {"name": "size"}]}],
    "design": {"primary_color": "#4CAF50", "style": "modern"},
}


class TestExtractFunctionCode:
    """Tests for extracting function code from Claude response."""

    def test_extracts_from_python_fence(self):
        response = "Here is the code:\n```python\ndef run_qa_tests(page, base_url):\n    return {}\n```"
        code = _extract_function_code(response)
        assert "def run_qa_tests" in code
        assert "```" not in code

    def test_extracts_bare_def(self):
        response = "def run_qa_tests(page, base_url):\n    results = {}\n    return results"
        code = _extract_function_code(response)
        assert code.startswith("def run_qa_tests")

    def test_extracts_from_generic_fence(self):
        response = "```\ndef run_qa_tests(page, base_url):\n    return {}\n```"
        code = _extract_function_code(response)
        assert "def run_qa_tests" in code

    def test_returns_raw_text_as_fallback(self):
        response = "some raw text without def"
        code = _extract_function_code(response)
        assert code == response.strip()

    def test_strips_whitespace(self):
        response = "  ```python\ndef run_qa_tests(page, base_url):\n    pass\n```  "
        code = _extract_function_code(response)
        assert not code.startswith(" ")


class TestExecuteTestCode:
    """Tests for executing generated test code."""

    def test_runs_valid_function(self):
        code = (
            "def run_qa_tests(page, base_url):\n"
            "    return {'passed': [{'name': 'test one'}], 'failed': []}\n"
        )
        mock_page = MagicMock()
        result = _execute_test_code(code, mock_page, "http://localhost:4568")
        assert result["passed"] == [{"name": "test one"}]
        assert result["failed"] == []

    def test_syntax_error_returns_failure(self):
        code = "def run_qa_tests(page, base_url):\n    return {{{invalid"
        mock_page = MagicMock()
        result = _execute_test_code(code, mock_page, "http://localhost:4568")
        assert len(result["failed"]) == 1
        assert "Syntax error" in result["failed"][0]["error"]

    def test_missing_function_returns_failure(self):
        code = "x = 1"
        mock_page = MagicMock()
        result = _execute_test_code(code, mock_page, "http://localhost:4568")
        assert len(result["failed"]) == 1
        assert "run_qa_tests not found" in result["failed"][0]["error"]

    def test_runtime_exception_returns_failure(self):
        code = (
            "def run_qa_tests(page, base_url):\n"
            "    raise ValueError('oops')\n"
        )
        mock_page = MagicMock()
        result = _execute_test_code(code, mock_page, "http://localhost:4568")
        assert len(result["failed"]) == 1
        assert "oops" in result["failed"][0]["error"]

    def test_non_dict_return_returns_failure(self):
        code = "def run_qa_tests(page, base_url):\n    return 'not a dict'\n"
        mock_page = MagicMock()
        result = _execute_test_code(code, mock_page, "http://localhost:4568")
        assert len(result["failed"]) == 1
        assert "expected dict" in result["failed"][0]["error"]

    def test_page_methods_are_called(self):
        code = (
            "def run_qa_tests(page, base_url):\n"
            "    page.goto(base_url + '/login')\n"
            "    return {'passed': [{'name': 'goto called'}], 'failed': []}\n"
        )
        mock_page = MagicMock()
        result = _execute_test_code(code, mock_page, "http://localhost:4568")
        mock_page.goto.assert_called_once_with("http://localhost:4568/login")
        assert len(result["passed"]) == 1

    def test_generated_code_can_import_anything(self):
        # execute_test_code is plain exec(): the generated code has full
        # builtins and can import os, subprocess and the rest. Nothing in the
        # function restricts it, which is why it only ever runs in the child
        # process (see TestRunTestsInChild), never in the worker.
        code = (
            "def run_qa_tests(page, base_url):\n"
            "    import os\n"
            "    return {'passed': [], 'failed': [{'name': 'import test', 'error': 'os imported'}]}\n"
        )
        mock_page = MagicMock()
        result = _execute_test_code(code, mock_page, "http://localhost:4568")
        assert len(result["failed"]) == 1


class TestNormalizeResults:
    """Tests for normalising raw {passed/failed} dicts into flat result list."""

    def test_normalises_passed_dict(self):
        raw = {"passed": [{"name": "page loads"}], "failed": []}
        results = _normalize_results(raw)
        assert len(results) == 1
        assert results[0] == {"name": "page loads", "passed": True, "error": None}

    def test_normalises_failed_dict(self):
        raw = {"passed": [], "failed": [{"name": "form submit", "error": "404"}]}
        results = _normalize_results(raw)
        assert len(results) == 1
        assert results[0]["passed"] is False
        assert results[0]["error"] == "404"

    def test_normalises_string_items(self):
        raw = {"passed": ["login works"], "failed": []}
        results = _normalize_results(raw)
        assert results[0]["name"] == "login works"
        assert results[0]["passed"] is True

    def test_mixed_results(self):
        raw = {
            "passed": [{"name": "p1"}, {"name": "p2"}],
            "failed": [{"name": "f1", "error": "err"}],
        }
        results = _normalize_results(raw)
        assert len(results) == 3
        passed = [r for r in results if r["passed"]]
        failed = [r for r in results if not r["passed"]]
        assert len(passed) == 2
        assert len(failed) == 1

    def test_empty_raw(self):
        raw = {"passed": [], "failed": []}
        results = _normalize_results(raw)
        assert results == []

    def test_missing_keys(self):
        raw = {}
        results = _normalize_results(raw)
        assert results == []


class TestSkippedResult:
    """Tests for _skipped_result helper."""

    def test_returns_success_true(self):
        result = _skipped_result("npm install failed")
        assert result["success"] is True

    def test_sets_skipped_true(self):
        result = _skipped_result("some reason")
        assert result["skipped"] is True

    def test_zero_counts(self):
        result = _skipped_result("reason")
        assert result["passed"] == 0
        assert result["failed"] == 0
        assert result["results"] == []

    def test_reason_in_report(self):
        result = _skipped_result("database setup failed")
        assert "database setup failed" in result["qa_report"]


class TestQaTestPort:
    """Test that the QA port constant is distinct from smoke test port."""

    def test_qa_port_not_same_as_test_port(self):
        from test import TEST_PORT
        assert QA_TEST_PORT != TEST_PORT

    def test_qa_port_is_valid(self):
        assert 1024 < QA_TEST_PORT < 65535


class TestRunQa:
    """Integration-style tests for run_qa with mocked Claude and Playwright."""

    def test_skips_on_app_start_failure(self, tmp_path):
        """If the managed app fails to start, run_qa returns skipped (not a hard failure)."""
        # Create a minimal spec and dummy app
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(SAMPLE_SPEC))
        (tmp_path / "package.json").write_text('{"name": "test"}')
        (tmp_path / "server.js").write_text("process.exit(1);")

        from qa import run_qa
        with patch("qa.install_deps", return_value=(True, "", None)), \
             patch("qa.setup_database", return_value=True), \
             patch("qa.start_app") as mock_start, \
             patch("qa.wait_for_app", return_value=False), \
             patch("qa.stop_app"), \
             patch("qa.teardown_database"):
            mock_start.return_value = MagicMock()
            result = run_qa(str(tmp_path), SAMPLE_SPEC)

        assert result["success"] is True  # non-blocking
        assert result["skipped"] is True

    def test_skips_on_generation_failure(self, tmp_path):
        """If Claude fails, run_qa returns skipped rather than aborting build."""
        (tmp_path / "spec.json").write_text(json.dumps(SAMPLE_SPEC))

        from qa import run_qa
        with patch("qa.install_deps", return_value=(True, "", None)), \
             patch("qa.setup_database", return_value=True), \
             patch("qa.start_app") as mock_start, \
             patch("qa.wait_for_app", return_value=True), \
             patch("qa.stop_app"), \
             patch("qa.teardown_database"), \
             patch("qa.generate_qa_tests", side_effect=RuntimeError("API error")):
            mock_start.return_value = MagicMock()
            result = run_qa(str(tmp_path), SAMPLE_SPEC)

        assert result["success"] is True
        assert result["skipped"] is True
        assert "API error" in result["qa_report"]

    def test_returns_results_on_success(self, tmp_path):
        """Full happy-path: generated tests run and return structured results."""
        (tmp_path / "spec.json").write_text(json.dumps(SAMPLE_SPEC))

        good_code = (
            "def run_qa_tests(page, base_url):\n"
            "    return {'passed': [{'name': 'dashboard loads'}], 'failed': []}\n"
        )

        from qa import run_qa
        with patch("qa.install_deps", return_value=(True, "", None)), \
             patch("qa.setup_database", return_value=True), \
             patch("qa.start_app") as mock_start, \
             patch("qa.wait_for_app", return_value=True), \
             patch("qa.stop_app"), \
             patch("qa.teardown_database"), \
             patch("qa.generate_qa_tests", return_value=good_code), \
             patch("qa._run_tests_in_child", side_effect=_run_in_process):
            mock_start.return_value = MagicMock()
            result = run_qa(str(tmp_path), SAMPLE_SPEC)

        assert result["success"] is True
        assert result["passed"] == 1
        assert result["failed"] == 0
        assert result["skipped"] is False

    def test_returns_failure_on_test_failures(self, tmp_path):
        """QA failures set success=False (non-blocking warning)."""
        (tmp_path / "spec.json").write_text(json.dumps(SAMPLE_SPEC))

        failing_code = (
            "def run_qa_tests(page, base_url):\n"
            "    return {'passed': [], 'failed': [{'name': 'plots page', 'error': '404'}]}\n"
        )

        from qa import run_qa
        with patch("qa.install_deps", return_value=(True, "", None)), \
             patch("qa.setup_database", return_value=True), \
             patch("qa.start_app") as mock_start, \
             patch("qa.wait_for_app", return_value=True), \
             patch("qa.stop_app"), \
             patch("qa.teardown_database"), \
             patch("qa.generate_qa_tests", return_value=failing_code), \
             patch("qa._run_tests_in_child", side_effect=_run_in_process):
            mock_start.return_value = MagicMock()
            result = run_qa(str(tmp_path), SAMPLE_SPEC)

        assert result["success"] is False
        assert result["failed"] == 1
        assert result["skipped"] is False

    def test_uses_provided_port_without_starting_app(self, tmp_path):
        """When port is given, run_qa must not start its own app instance."""
        (tmp_path / "spec.json").write_text(json.dumps(SAMPLE_SPEC))

        good_code = (
            "def run_qa_tests(page, base_url):\n"
            "    return {'passed': [{'name': 'ok'}], 'failed': []}\n"
        )

        from qa import run_qa
        with patch("qa.install_deps") as mock_install, \
             patch("qa.generate_qa_tests", return_value=good_code), \
             patch("qa._run_tests_in_child", side_effect=_run_in_process):
            result = run_qa(str(tmp_path), SAMPLE_SPEC, port=9999)

        # install_deps must NOT be called when port is provided
        mock_install.assert_not_called()
        assert result["success"] is True

    def test_writes_qa_tests_file(self, tmp_path):
        """Generated test code must be written to qa_tests.py for debugging."""
        (tmp_path / "spec.json").write_text(json.dumps(SAMPLE_SPEC))

        code = (
            "def run_qa_tests(page, base_url):\n"
            "    return {'passed': [{'name': 't1'}], 'failed': []}\n"
        )

        from qa import run_qa
        with patch("qa.install_deps", return_value=(True, "", None)), \
             patch("qa.setup_database", return_value=True), \
             patch("qa.start_app") as mock_start, \
             patch("qa.wait_for_app", return_value=True), \
             patch("qa.stop_app"), \
             patch("qa.teardown_database"), \
             patch("qa.generate_qa_tests", return_value=code), \
             patch("qa._run_tests_in_child", side_effect=_run_in_process):
            mock_start.return_value = MagicMock()
            run_qa(str(tmp_path), SAMPLE_SPEC)

        assert (tmp_path / "qa_tests.py").exists()


# A stand-in for the playwright package, put on the child's PYTHONPATH so the
# real child process (cli/qa_runner.py) can run without launching a browser.
_FAKE_PLAYWRIGHT_SYNC_API = """
class _Page:
    def set_default_timeout(self, value):
        pass

    def set_default_navigation_timeout(self, value):
        pass

    def goto(self, url, **kwargs):
        return None


class _Browser:
    def new_page(self, **kwargs):
        return _Page()

    def close(self):
        pass


class _Chromium:
    def launch(self, **kwargs):
        return _Browser()


class _Playwright:
    chromium = _Chromium()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def sync_playwright():
    return _Playwright()
"""

# Names only; the values are deliberately not secret-shaped.
_WORKER_SECRET_NAMES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_ADMIN_KEY", "RAILWAY_API_TOKEN", "RESEND_API_KEY",
    "GITHUB_TOKEN", "SENTRY_DSN", "OPS_API_KEY", "STRIPE_SECRET_KEY", "SESSION_SECRET",
)


@pytest.fixture
def fake_playwright(tmp_path, monkeypatch):
    """Put a stub `playwright` package first on the child's import path."""
    stubs = tmp_path / "stubs"
    (stubs / "playwright").mkdir(parents=True)
    (stubs / "playwright" / "__init__.py").write_text("")
    (stubs / "playwright" / "sync_api.py").write_text(_FAKE_PLAYWRIGHT_SYNC_API)
    monkeypatch.setenv("PYTHONPATH", str(stubs))
    return stubs


class TestRunTestsInChild:
    """The generated QA code runs in a real child process with a scrubbed environment."""

    def test_child_runs_the_code_in_the_build_dir(self, tmp_path, fake_playwright):
        from qa import _run_tests_in_child
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        code = (
            "def run_qa_tests(page, base_url):\n"
            "    import os\n"
            "    page.goto(base_url + '/login')\n"
            "    return {'passed': [{'name': os.getcwd()}, {'name': base_url}, {'name': str(os.getpid())}],\n"
            "            'failed': []}\n"
        )
        result = _run_tests_in_child(code, "http://localhost:4568", build_dir, timeout=60)
        names = [item["name"] for item in result["passed"]]
        assert Path(names[0]).resolve() == build_dir.resolve()
        assert names[1] == "http://localhost:4568"
        import os
        assert names[2] != str(os.getpid()), "generated code must not run in the worker process"
        assert result["failed"] == []

    def test_child_cannot_see_the_workers_secrets(self, tmp_path, fake_playwright, monkeypatch):
        import os
        from qa import _run_tests_in_child
        for name in _WORKER_SECRET_NAMES:
            monkeypatch.setenv(name, "dummy-value")
        monkeypatch.setenv("DATABASE_URL", "postgres://worker@localhost:5432/worker_main")
        code = (
            "def run_qa_tests(page, base_url):\n"
            "    import os\n"
            f"    names = {list(_WORKER_SECRET_NAMES)!r}\n"
            "    leaked = [n for n in names if n in os.environ]\n"
            "    return {'passed': [{'name': 'leaked=' + ','.join(leaked)},\n"
            "                       {'name': 'db=' + os.environ.get('DATABASE_URL', '<unset>')}],\n"
            "            'failed': []}\n"
        )
        test_db = "postgres://worker@localhost:5432/test_qa_app"
        result = _run_tests_in_child(code, "http://localhost:4568", tmp_path, database_url=test_db, timeout=60)
        names = [item["name"] for item in result["passed"]]
        assert names[0] == "leaked=", f"worker secrets reached generated code: {names[0]}"
        assert names[1] == "db=" + test_db
        # The worker's own environment is untouched by the run.
        assert os.environ["DATABASE_URL"].endswith("/worker_main")

    def test_child_gets_no_database_url_unless_one_is_passed(self, tmp_path, fake_playwright, monkeypatch):
        from qa import _run_tests_in_child
        monkeypatch.setenv("DATABASE_URL", "postgres://worker@localhost:5432/worker_main")
        code = (
            "def run_qa_tests(page, base_url):\n"
            "    import os\n"
            "    return {'passed': [{'name': os.environ.get('DATABASE_URL', '<unset>')}], 'failed': []}\n"
        )
        result = _run_tests_in_child(code, "http://localhost:4568", tmp_path, timeout=60)
        assert result["passed"][0]["name"] == "<unset>"

    def test_runaway_code_is_stopped_at_the_timeout(self, tmp_path, fake_playwright):
        import time
        from qa import QaChildError, _run_tests_in_child
        code = (
            "def run_qa_tests(page, base_url):\n"
            "    import time\n"
            "    time.sleep(120)\n"
            "    return {'passed': [], 'failed': []}\n"
        )
        started = time.monotonic()
        with pytest.raises(QaChildError, match="limit"):
            _run_tests_in_child(code, "http://localhost:4568", tmp_path, timeout=2)
        assert time.monotonic() - started < 30

    def test_crashing_child_raises_instead_of_returning_results(self, tmp_path, fake_playwright):
        from qa import QaChildError, _run_tests_in_child
        code = "raise SystemExit(3)\n"
        with pytest.raises(QaChildError, match="status 3"):
            _run_tests_in_child(code, "http://localhost:4568", tmp_path, timeout=60)

    def test_results_read_back_from_the_child_are_bounded(self):
        from qa import _bounded_results
        raw = {
            "passed": [{"name": "p" * 5000}] * 1000,
            "failed": [{"name": {"not": "a string"}, "error": "e" * 50000}, "bare string"],
        }
        bounded = _bounded_results(raw)
        assert len(bounded["passed"]) == 200
        assert len(bounded["passed"][0]["name"]) < 400
        assert isinstance(bounded["failed"][0]["name"], str)
        assert len(bounded["failed"][0]["error"]) < 2100
        assert bounded["failed"][1] == {"name": "bare string", "error": "unknown error"}
        assert _bounded_results("nonsense")["failed"][0]["name"] == "QA test runner"

    def test_run_qa_treats_a_failed_child_as_skipped(self, tmp_path):
        from qa import QaChildError, run_qa
        (tmp_path / "spec.json").write_text(json.dumps(SAMPLE_SPEC))
        code = "def run_qa_tests(page, base_url):\n    return {'passed': [], 'failed': []}\n"
        with patch("qa.generate_qa_tests", return_value=code), \
             patch("qa._run_tests_in_child", side_effect=QaChildError("generated tests exceeded the limit")):
            result = run_qa(str(tmp_path), SAMPLE_SPEC, port=9999)
        assert result["skipped"] is True and result["success"] is True
        assert "exceeded the limit" in result["qa_report"]

    def test_run_qa_gives_the_child_the_test_database_only(self, tmp_path, monkeypatch):
        import os
        from qa import run_qa
        (tmp_path / "spec.json").write_text(json.dumps(SAMPLE_SPEC))
        monkeypatch.setenv("DATABASE_URL", "postgres://worker@dbhost:6543/worker_main")
        code = "def run_qa_tests(page, base_url):\n    return {'passed': [{'name': 'ok'}], 'failed': []}\n"
        seen = {}

        def fake_child(test_code, base_url, build_path, database_url=None, timeout=None):
            seen["database_url"] = database_url
            seen["worker_env_during_run"] = os.environ.get("DATABASE_URL")
            return {"passed": [{"name": "ok"}], "failed": []}

        with patch("qa.install_deps", return_value=(True, "", None)), \
             patch("qa.setup_database", return_value=True), \
             patch("qa.start_app", return_value=MagicMock()), \
             patch("qa.wait_for_app", return_value=True), \
             patch("qa.stop_app"), \
             patch("qa.teardown_database"), \
             patch("qa.generate_qa_tests", return_value=code), \
             patch("qa._run_tests_in_child", side_effect=fake_child):
            result = run_qa(str(tmp_path), SAMPLE_SPEC)

        assert result["passed"] == 1
        assert seen["database_url"] == "postgres://worker@dbhost:6543/test_qa_garden_tracker"
        assert seen["worker_env_during_run"] == "postgres://worker@dbhost:6543/worker_main"

    def test_worker_module_has_no_in_process_exec(self):
        import inspect
        import qa
        assert not hasattr(qa, "_execute_test_code")
        assert "exec(" not in inspect.getsource(qa)


class TestGenerateQaTests:
    """Tests for generate_qa_tests(), compile-and-retry behaviour."""

    def _make_mock_response(self, text: str):
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text=text)]
        return mock_resp

    def test_returns_valid_code_on_first_attempt(self):
        """Happy path: valid code returned immediately, no retry needed."""
        valid_code = "def run_qa_tests(page, base_url):\n    return {'passed': [], 'failed': []}\n"
        from qa import generate_qa_tests
        with patch("qa._create_client"), \
             patch("qa._call_api", return_value=self._make_mock_response(valid_code)) as mock_call:
            result = generate_qa_tests({"app_name": "test"})

        assert "def run_qa_tests" in result
        assert mock_call.call_count == 1  # no retry

    def test_retries_once_on_syntax_error(self):
        """Syntax error on first attempt triggers one retry with fix hint in prompt."""
        bad_code = "def run_qa_tests(page, base_url):\n    return {\n"  # unmatched brace
        good_code = "def run_qa_tests(page, base_url):\n    return {'passed': [], 'failed': []}\n"

        call_count = [0]
        def _fake_call(client, messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return self._make_mock_response(bad_code)
            return self._make_mock_response(good_code)

        from qa import generate_qa_tests
        with patch("qa._create_client"), \
             patch("qa._call_api", side_effect=_fake_call):
            result = generate_qa_tests({"app_name": "test"})

        assert call_count[0] == 2  # retried once
        assert "def run_qa_tests" in result

    def test_retry_includes_error_hint(self):
        """The retry prompt must include the original syntax error message."""
        bad_code = "def run_qa_tests(page, base_url):\n    return {\n"
        good_code = "def run_qa_tests(page, base_url):\n    return {'passed': [], 'failed': []}\n"

        prompts_seen = []
        call_count = [0]
        def _fake_call(client, messages, **kwargs):
            call_count[0] += 1
            prompts_seen.append(messages[0]["content"])
            if call_count[0] == 1:
                return self._make_mock_response(bad_code)
            return self._make_mock_response(good_code)

        from qa import generate_qa_tests
        with patch("qa._create_client"), \
             patch("qa._call_api", side_effect=_fake_call):
            generate_qa_tests({"app_name": "test"})

        assert len(prompts_seen) == 2
        assert "syntax error" in prompts_seen[1].lower()

    def test_retry_uses_realtime_not_batch(self):
        """Retry always uses realtime (use_batch=False) regardless of original batch flag."""
        bad_code = "def run_qa_tests(page, base_url):\n    return {\n"
        good_code = "def run_qa_tests(page, base_url):\n    return {'passed': [], 'failed': []}\n"

        batch_flags = []
        call_count = [0]
        def _fake_call(client, messages, use_batch=False, **kwargs):
            call_count[0] += 1
            batch_flags.append(use_batch)
            if call_count[0] == 1:
                return self._make_mock_response(bad_code)
            return self._make_mock_response(good_code)

        from qa import generate_qa_tests
        with patch("qa._create_client"), \
             patch("qa._call_api", side_effect=_fake_call):
            generate_qa_tests({"app_name": "test"}, use_batch=True)

        assert call_count[0] == 2
        assert batch_flags[1] is False  # retry must be realtime


class TestGenerateQaTestsEdgeCases:
    """Edge-case tests for generate_qa_tests() retry path."""

    def _make_mock_response(self, text: str):
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text=text)]
        return mock_resp

    def _make_empty_response(self):
        mock_resp = MagicMock()
        mock_resp.content = []
        return mock_resp

    def test_retry_also_broken_propagates_gracefully(self):
        """When both attempts have syntax errors the broken code is returned (not raised).
        _execute_test_code will catch the SyntaxError and produce a single FAIL entry."""
        bad_code = "def run_qa_tests(page, base_url):\n    return {\n"

        call_count = [0]
        def _fake_call(client, messages, **kwargs):
            call_count[0] += 1
            return self._make_mock_response(bad_code)

        from qa import generate_qa_tests
        with patch("qa._create_client"), \
             patch("qa._call_api", side_effect=_fake_call):
            result = generate_qa_tests({"app_name": "test"})

        # Should not raise; exactly two API calls made (original + one retry)
        assert call_count[0] == 2
        # Result is whatever the retry returned; still broken, but caller handles it
        assert isinstance(result, str)

    def test_empty_response_on_retry_returns_empty_string(self):
        """Empty content list on retry returns '' (not IndexError)."""
        bad_code = "def run_qa_tests(page, base_url):\n    return {\n"

        call_count = [0]
        def _fake_call(client, messages, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return self._make_mock_response(bad_code)
            return self._make_empty_response()

        from qa import generate_qa_tests
        with patch("qa._create_client"), \
             patch("qa._call_api", side_effect=_fake_call):
            result = generate_qa_tests({"app_name": "test"})

        assert call_count[0] == 2
        assert isinstance(result, str)   # may be empty string or partial, no crash

    def test_no_retry_on_valid_code_batch_flag_preserved(self):
        """When first attempt is valid, use_batch flag passes through unchanged."""
        valid_code = "def run_qa_tests(page, base_url):\n    return {'passed': [], 'failed': []}\n"

        batch_flags = []
        def _fake_call(client, messages, use_batch=False, **kwargs):
            batch_flags.append(use_batch)
            mock_resp = MagicMock()
            mock_resp.content = [MagicMock(text=valid_code)]
            return mock_resp

        from qa import generate_qa_tests
        with patch("qa._create_client"), \
             patch("qa._call_api", side_effect=_fake_call):
            generate_qa_tests({"app_name": "test"}, use_batch=True)

        assert len(batch_flags) == 1
        assert batch_flags[0] is True   # original batch flag respected when no retry needed


class TestQaSystemPrompt:
    """Tests that QA_SYSTEM_PROMPT contains required guidance for known failure modes."""

    def test_networkidle_only_in_negative_instructions(self):
        """Every occurrence of 'networkidle' in the prompt must be inside a negative instruction
        ('do not', 'not use', 'never', 'avoid'). If it appears in example code or a positive
        instruction, Claude will copy that pattern and timeouts will follow (create-form-timeout fix)."""
        import re
        from qa import QA_SYSTEM_PROMPT
        refs = re.findall(r'[^\n]{0,60}networkidle[^\n]{0,60}', QA_SYSTEM_PROMPT, re.IGNORECASE)
        for ref in refs:
            ref_lower = ref.lower()
            assert any(neg in ref_lower for neg in ['not use', 'do not', 'never', 'avoid']), (
                f"networkidle appears outside a negative instruction: {ref!r}"
            )

    def test_login_example_block_uses_load_not_networkidle(self):
        """The login example block (the copy-paste template Claude uses) must not contain networkidle.
        This was the root cause: Claude copies the pattern from examples, not from rules.

        Updated 2026-04-18: the prompt no longer contains a password-login
        recipe block, magic-link auth is handled by the pipeline-injected
        _cas_login_as_admin helper (see tests/test_qa_auth_prelude.py).
        This test now asserts the prompt mentions the helper (so Claude's
        copy-paste target is the helper call, not an inline password
        form) and does not recommend networkidle anywhere.
        """
        from qa import QA_SYSTEM_PROMPT
        # The prompt must reference the auth helper by name
        assert "_cas_login_as_admin" in QA_SYSTEM_PROMPT
        # networkidle may appear in warnings ("Do NOT use networkidle") but
        # must not appear on an indented code-example line that Claude
        # would copy verbatim. Walk lines looking for indented code that
        # calls wait_for_load_state("networkidle") or similar.
        for line in QA_SYSTEM_PROMPT.splitlines():
            if line.startswith("    ") and "networkidle" in line.lower():
                raise AssertionError(
                    f"Indented code-example line contains networkidle; "
                    f"Claude will copy this pattern: {line!r}"
                )
        # Rule about using 'load' wait-state still expected
        assert '"load"' in QA_SYSTEM_PROMPT or "'load'" in QA_SYSTEM_PROMPT

    def test_uses_load_state_for_form_wait(self):
        """Prompt must instruct to use 'load' state after form submissions."""
        from qa import QA_SYSTEM_PROMPT
        assert '"load"' in QA_SYSTEM_PROMPT or "'load'" in QA_SYSTEM_PROMPT

    def test_warns_against_string_methods_on_playwright_objects(self):
        """Prompt must warn against calling .replace() on Playwright objects (dict-replace fix)."""
        from qa import QA_SYSTEM_PROMPT
        assert ".replace()" in QA_SYSTEM_PROMPT or "string methods" in QA_SYSTEM_PROMPT.lower()

    def test_includes_page_url_guidance(self):
        """Prompt must tell Claude to use page.url for the current URL string."""
        from qa import QA_SYSTEM_PROMPT
        assert "page.url" in QA_SYSTEM_PROMPT

    def test_role_switch_uses_clear_cookies_not_get_logout(self):
        """Role switch must use page.context.clear_cookies(), CAS apps have POST-only logout.
        page.goto('/logout') would 404 and leave the old session cookie intact.
        The word 'goto(/logout)' may appear in an explanatory 'this fails' sentence, but must
        NOT appear as an indented code-example line that Claude would copy verbatim."""
        import re
        from qa import QA_SYSTEM_PROMPT
        assert "clear_cookies" in QA_SYSTEM_PROMPT
        # Indented code lines (4+ leading spaces) must not use goto("/logout")
        for line in QA_SYSTEM_PROMPT.splitlines():
            if line.startswith("    ") and re.search(r'goto\([^)]*["\']/?logout["\']', line):
                raise AssertionError(
                    f"Code-example line instructs page.goto('/logout') which 404s on CAS apps; "
                    f"use page.context.clear_cookies() instead: {line!r}"
                )

    def test_includes_role_switch_login_example(self):
        """Prompt must give a concrete pattern for testing admin-only
        page access while NOT logged in as admin.

        Updated 2026-04-18: the old pattern used `member@example.com` +
        password re-login. Magic-link-only apps don't have that flow.
        The new pattern relies on clear_cookies + hitting a protected
        URL, expecting a 302 redirect to /login. Test asserts the
        prompt mentions both primitives."""
        from qa import QA_SYSTEM_PROMPT
        assert "clear_cookies" in QA_SYSTEM_PROMPT
        # Expect explicit mention of either the login-redirect pattern or
        # the admin path probe so Claude has a concrete target to copy.
        assert "/login" in QA_SYSTEM_PROMPT
        assert "/admin" in QA_SYSTEM_PROMPT

    def test_role_switch_warns_not_to_test_as_admin(self):
        """Prompt must explicitly warn that role tests should not use the initial admin login."""
        from qa import QA_SYSTEM_PROMPT
        assert "NEVER test role" in QA_SYSTEM_PROMPT or "never test role" in QA_SYSTEM_PROMPT.lower()

    def test_qa_prompt_warns_against_assumed_admin_sub_routes(self):
        """Prompt must warn Claude not to hardcode /admin/{model} paths that are not in spec.pages.
        QA failures in MW v10 were caused by tests asserting /admin/events, /admin/videos,
        /admin/gallery which do not exist, the app mounts CRUD at /events, /photos, /videos."""
        from qa import QA_SYSTEM_PROMPT
        assert "/admin/{model}" in QA_SYSTEM_PROMPT or "admin sub-pages" in QA_SYSTEM_PROMPT.lower()
        assert "spec.pages" in QA_SYSTEM_PROMPT

    def test_qa_prompt_strict_mode_has_four_rules(self):
        """QA_SYSTEM_PROMPT must contain exactly 4 STRICT MODE RULE blocks (3 existing + admin sub-route)."""
        from qa import QA_SYSTEM_PROMPT
        count = QA_SYSTEM_PROMPT.count("STRICT MODE RULE")
        assert count >= 4, (
            f"Expected at least 4 'STRICT MODE RULE' entries in QA_SYSTEM_PROMPT, found {count}"
        )
