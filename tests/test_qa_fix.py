# tests/test_qa_fix.py - Tests for cli/qa_fix.py QA self-correction module

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

from qa_fix import (
    attempt_qa_fix,
    _collect_source_files,
    _parse_fix_files,
    _format_qa_results,
)

SAMPLE_QA_RESULTS = [
    {"name": "plots page renders", "passed": False, "error": "Expected 200, got 404"},
    {"name": "dashboard loads", "passed": True, "error": None},
    {"name": "create plot form submits", "passed": False, "error": "Form not found"},
]

SAMPLE_SPEC = {
    "app_name": "garden-tracker",
    "user_roles": [{"role": "admin"}, {"role": "member"}],
    "pages": [
        {"path": "/dashboard", "auth_required": True, "allowed_roles": ["admin", "member"]},
        {"path": "/plots", "auth_required": True, "allowed_roles": ["admin", "member"]},
    ],
}


class TestParseFixFiles:
    """Test extraction of ===FILE: path=== blocks (shared with fix.py pattern)."""

    def test_single_file(self):
        text = "===FILE: routes/plots.js===\nmodule.exports = {};\n===END FILE==="
        files = _parse_fix_files(text)
        assert len(files) == 1
        assert files[0][0] == "routes/plots.js"
        assert "module.exports" in files[0][1]

    def test_multiple_files(self):
        text = (
            "===FILE: routes/plots.js===\nconst router = require('express').Router();\n===END FILE===\n"
            "===FILE: views/plots.ejs===\n<h1>Plots</h1>\n===END FILE==="
        )
        files = _parse_fix_files(text)
        assert len(files) == 2

    def test_path_traversal_blocked(self):
        text = "===FILE: ../../etc/passwd===\nroot\n===END FILE==="
        files = _parse_fix_files(text)
        assert len(files) == 0

    def test_absolute_path_blocked(self):
        text = "===FILE: /etc/shadow===\nroot\n===END FILE==="
        files = _parse_fix_files(text)
        assert len(files) == 0

    def test_empty_response(self):
        assert _parse_fix_files("No changes needed.") == []


class TestCollectSourceFiles:
    """Test source collection from build directory."""

    def test_collects_server_js(self, tmp_path):
        (tmp_path / "server.js").write_text("const app = require('express')();")
        files = _collect_source_files(tmp_path)
        paths = [f[0] for f in files]
        assert "server.js" in paths

    def test_collects_routes_directory(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000);")
        (tmp_path / "routes").mkdir()
        (tmp_path / "routes" / "plots.js").write_text("router.get('/plots', ...);")
        files = _collect_source_files(tmp_path)
        paths = [f[0] for f in files]
        assert "routes/plots.js" in paths

    def test_collects_views(self, tmp_path):
        (tmp_path / "server.js").write_text("x")
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "plots.ejs").write_text("<h1>Plots</h1>")
        files = _collect_source_files(tmp_path)
        paths = [f[0] for f in files]
        assert "views/plots.ejs" in paths

    def test_respects_max_context_chars(self, tmp_path):
        from qa_fix import _MAX_CONTEXT_CHARS
        (tmp_path / "server.js").write_text("x" * (_MAX_CONTEXT_CHARS + 1000))
        (tmp_path / "routes").mkdir()
        (tmp_path / "routes" / "extra.js").write_text("y" * 1000)
        files = _collect_source_files(tmp_path)
        total = sum(len(c) for _, c in files)
        assert total <= _MAX_CONTEXT_CHARS + 10000  # server.js alone may exceed

    def test_empty_directory(self, tmp_path):
        files = _collect_source_files(tmp_path)
        assert files == []


class TestFormatQaResults:
    """Test formatting of QA results for the Claude prompt."""

    def test_formats_passed(self):
        results = [{"name": "dashboard loads", "passed": True, "error": None}]
        text = _format_qa_results(results)
        assert "[PASS] dashboard loads" in text

    def test_formats_failed_with_error(self):
        results = [{"name": "plots page", "passed": False, "error": "404"}]
        text = _format_qa_results(results)
        assert "[FAIL] plots page" in text
        assert "404" in text

    def test_formats_mixed(self):
        text = _format_qa_results(SAMPLE_QA_RESULTS)
        assert "[PASS]" in text
        assert "[FAIL]" in text

    def test_handles_tuple_format(self):
        """Backward compat: also accepts (name, passed, error) tuples."""
        results = [("dashboard", True, None), ("plots", False, "404")]
        text = _format_qa_results(results)
        assert "[PASS] dashboard" in text
        assert "[FAIL] plots" in text

    def test_empty_results(self):
        text = _format_qa_results([])
        assert text == ""


class TestAttemptQaFix:
    """Tests for the main attempt_qa_fix function."""

    def test_returns_not_fixed_when_no_source_files(self, tmp_path):
        result = attempt_qa_fix(
            build_dir=str(tmp_path),
            qa_results=SAMPLE_QA_RESULTS,
            spec=SAMPLE_SPEC,
        )
        assert result["fixed"] is False
        assert "No source files" in result["description"]

    def test_returns_not_fixed_when_no_failed_checks(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000);")
        all_passed = [{"name": "t", "passed": True, "error": None}]
        result = attempt_qa_fix(
            build_dir=str(tmp_path),
            qa_results=all_passed,
            spec=SAMPLE_SPEC,
        )
        assert result["fixed"] is False
        assert "No failed" in result["description"]

    def test_calls_claude_with_failed_checks(self, tmp_path):
        (tmp_path / "server.js").write_text("const app = require('express')();")
        (tmp_path / "routes").mkdir()
        (tmp_path / "routes" / "plots.js").write_text("router.get('/plots', (req, res) => res.send('ok'));")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="===FILE: routes/plots.js===\nfixed\n===END FILE===")]

        with patch("qa_fix._create_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.messages.create.return_value = mock_response

            result = attempt_qa_fix(
                build_dir=str(tmp_path),
                qa_results=SAMPLE_QA_RESULTS,
                spec=SAMPLE_SPEC,
            )

        assert result["fixed"] is True
        assert "routes/plots.js" in result["files_changed"]

    def test_writes_fixed_files_to_disk(self, tmp_path):
        (tmp_path / "server.js").write_text("const app = require('express')();")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(
            text="===FILE: routes/plots.js===\nconst router = require('express').Router();\n===END FILE==="
        )]

        with patch("qa_fix._create_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.messages.create.return_value = mock_response

            attempt_qa_fix(
                build_dir=str(tmp_path),
                qa_results=SAMPLE_QA_RESULTS,
            )

        assert (tmp_path / "routes" / "plots.js").exists()
        content = (tmp_path / "routes" / "plots.js").read_text()
        assert "express" in content

    def test_returns_not_fixed_when_claude_returns_no_files(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000);")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No changes needed.")]

        with patch("qa_fix._create_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.messages.create.return_value = mock_response

            result = attempt_qa_fix(
                build_dir=str(tmp_path),
                qa_results=SAMPLE_QA_RESULTS,
            )

        assert result["fixed"] is False
        assert "no file fixes" in result["description"]

    def test_path_traversal_blocked_in_fix(self, tmp_path):
        build_dir = tmp_path / "app"
        build_dir.mkdir()
        (build_dir / "server.js").write_text("app.listen(3000);")
        escape_target = tmp_path / "escaped.txt"

        mock_response = MagicMock()
        mock_response.content = [MagicMock(
            text="===FILE: ../escaped.txt===\nmalicious\n===END FILE==="
        )]

        with patch("qa_fix._create_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.messages.create.return_value = mock_response

            result = attempt_qa_fix(
                build_dir=str(build_dir),
                qa_results=SAMPLE_QA_RESULTS,
            )

        assert result["fixed"] is False
        assert not escape_target.exists(), "a ../ path in the model output wrote outside the build dir"
        assert (build_dir / "server.js").read_text() == "app.listen(3000);"
        assert sorted(p.name for p in build_dir.iterdir()) == ["server.js"]

    def test_includes_qa_tests_file_in_context(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000);")
        (tmp_path / "qa_tests.py").write_text("def run_qa_tests(page, base_url):\n    pass\n")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="===FILE: server.js===\nfixed\n===END FILE===")]

        captured_prompt = []
        def capture_call(**kwargs):
            captured_prompt.append(kwargs.get("messages", [{}])[0].get("content", ""))
            return mock_response

        with patch("qa_fix._create_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.messages.create.side_effect = capture_call

            attempt_qa_fix(
                build_dir=str(tmp_path),
                qa_results=SAMPLE_QA_RESULTS,
                spec=SAMPLE_SPEC,
            )

        assert len(captured_prompt) == 1
        assert "run_qa_tests" in captured_prompt[0]

    def test_spec_included_in_prompt(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000);")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No fixes.")]

        captured_prompt = []
        def capture_call(**kwargs):
            captured_prompt.append(kwargs.get("messages", [{}])[0].get("content", ""))
            return mock_response

        with patch("qa_fix._create_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.messages.create.side_effect = capture_call

            attempt_qa_fix(
                build_dir=str(tmp_path),
                qa_results=SAMPLE_QA_RESULTS,
                spec=SAMPLE_SPEC,
            )

        assert "garden-tracker" in captured_prompt[0]


class TestAdvisorIntegration:
    """Tests for advisor tool integration in qa_fix.py."""

    def _make_mock_response(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No fixes needed.")]
        mock_metrics = MagicMock()
        return (mock_response, mock_metrics)

    def test_advisor_enabled_switches_model_to_sonnet(self, tmp_path):
        from qa_fix import attempt_qa_fix
        (tmp_path / "server.js").write_text("const express = require('express');")

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("qa_fix._shared_call_api", side_effect=capture_call), \
             patch("qa_fix._create_client"), \
             patch.dict(os.environ, {"USE_ADVISOR_QA_FIX": "true"}):
            attempt_qa_fix(
                str(tmp_path),
                qa_results=[{"name": "test1", "passed": False, "error": "err"}],
            )

        assert "sonnet" in captured.get("model", "")

    def test_advisor_enabled_passes_advisor_params(self, tmp_path):
        from qa_fix import attempt_qa_fix
        (tmp_path / "server.js").write_text("const express = require('express');")

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("qa_fix._shared_call_api", side_effect=capture_call), \
             patch("qa_fix._create_client"), \
             patch.dict(os.environ, {"USE_ADVISOR_QA_FIX": "true"}):
            attempt_qa_fix(
                str(tmp_path),
                qa_results=[{"name": "test1", "passed": False, "error": "err"}],
            )

        assert captured.get("advisor_model") == "claude-opus-4-6"
        assert captured.get("advisor_max_uses") == 2

    def test_advisor_disabled_uses_opus_no_advisor(self, tmp_path):
        from qa_fix import attempt_qa_fix
        (tmp_path / "server.js").write_text("const express = require('express');")

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("qa_fix._shared_call_api", side_effect=capture_call), \
             patch("qa_fix._create_client"), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("USE_ADVISOR_QA_FIX", None)
            attempt_qa_fix(
                str(tmp_path),
                qa_results=[{"name": "test1", "passed": False, "error": "err"}],
            )

        assert "opus" in captured.get("model", "")
        assert "advisor_model" not in captured

    def test_advisor_guidance_in_system_prompt_when_enabled(self, tmp_path):
        from qa_fix import attempt_qa_fix
        (tmp_path / "server.js").write_text("const express = require('express');")

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("qa_fix._shared_call_api", side_effect=capture_call), \
             patch("qa_fix._create_client"), \
             patch.dict(os.environ, {"USE_ADVISOR_QA_FIX": "true"}):
            attempt_qa_fix(
                str(tmp_path),
                qa_results=[{"name": "test1", "passed": False, "error": "err"}],
            )

        system_text = captured["system"][0]["text"]
        assert "advisor" in system_text
        assert "respond in under 100 words" in system_text

    def test_qa_fix_has_own_call_api(self):
        """Verify qa_fix._call_api is independent from fix._call_api."""
        from qa_fix import _call_api as qa_call_api
        from fix import _call_api as fix_call_api
        assert qa_call_api is not fix_call_api


class TestQaFixCancelChecks:
    """Verify qa_fix.py raises BuildCancelled at each of the three check points.

    Cooperative cancellation poll points (the supervisor that fires the
    cancel event is described in docs/internals/worker.md) in
    attempt_qa_fix. A hung QA fix loop must observe the cancel event and
    stop before the next API call, parse, or file write.
    """

    def setup_method(self):
        from cli import cancellation
        self.build_id = "test-qa-fix-cancel"
        cancellation.register(self.build_id)

    def teardown_method(self):
        from cli import cancellation
        cancellation.clear(self.build_id)

    def _spec(self):
        return {"app_name": "garden-tracker"}

    def _make_mock_response(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No fixes needed.")]
        mock_metrics = MagicMock()
        return (mock_response, mock_metrics)

    def test_cancels_at_iteration_top(self, tmp_path):
        """Fire cancel before attempt_qa_fix runs; _call_api must NOT run."""
        from cli import cancellation
        from api_client import BuildCancelled

        (tmp_path / "server.js").write_text("const express = require('express');")

        cancellation.fire(self.build_id)

        api_mock = MagicMock(side_effect=AssertionError(
            "_shared_call_api must not be called after cancel fired at iteration top"
        ))
        with patch("qa_fix._shared_call_api", api_mock), \
             patch("qa_fix._create_client"):
            with pytest.raises(BuildCancelled) as exc_info:
                attempt_qa_fix(
                    str(tmp_path),
                    qa_results=[{"name": "page", "passed": False, "error": "404"}],
                    spec=self._spec(),
                    cancel_key=self.build_id,
                )

        assert exc_info.value.build_id == self.build_id
        assert api_mock.called is False

    def test_cancels_after_api_return(self, tmp_path):
        """Cancel fires mid API call; post_api guard raises before parse."""
        from cli import cancellation
        from api_client import BuildCancelled

        (tmp_path / "server.js").write_text("const express = require('express');")

        def fire_and_return(**kwargs):
            cancellation.fire(self.build_id)
            return self._make_mock_response()

        parse_mock = MagicMock(side_effect=AssertionError(
            "_parse_fix_files must not be called after post_api cancel check"
        ))
        with patch("qa_fix._shared_call_api", side_effect=fire_and_return), \
             patch("qa_fix._create_client"), \
             patch("qa_fix._parse_fix_files", parse_mock):
            with pytest.raises(BuildCancelled) as exc_info:
                attempt_qa_fix(
                    str(tmp_path),
                    qa_results=[{"name": "page", "passed": False, "error": "404"}],
                    spec=self._spec(),
                    cancel_key=self.build_id,
                )

        assert exc_info.value.build_id == self.build_id
        assert parse_mock.called is False

    def test_cancels_before_subprocess(self, tmp_path):
        """API returns clean; cancel fires between parse and file-write.
        pre_subprocess guard must raise before any Path.write_text call."""
        from cli import cancellation
        from api_client import BuildCancelled

        (tmp_path / "server.js").write_text("const express = require('express');")

        def parse_and_fire(_response_text):
            cancellation.fire(self.build_id)
            return [("views/plots.ejs", "<h1>Plots</h1>")]

        with patch("qa_fix._shared_call_api", side_effect=lambda **k: self._make_mock_response()), \
             patch("qa_fix._create_client"), \
             patch("qa_fix._parse_fix_files", side_effect=parse_and_fire), \
             patch("pathlib.Path.write_text") as write_mock:
            with pytest.raises(BuildCancelled) as exc_info:
                attempt_qa_fix(
                    str(tmp_path),
                    qa_results=[{"name": "page", "passed": False, "error": "404"}],
                    spec=self._spec(),
                    cancel_key=self.build_id,
                )

        assert exc_info.value.build_id == self.build_id
        assert write_mock.called is False


class TestSubprocessTreeKill:
    """Fix #2, QA_FIX_HANG1 mitigation: process-tree SIGKILL on budget exceeded.

    Verifies that:
    1. _run_node_syntax_check() starts its subprocess in a new session so the
       entire process tree shares a killable process group.
    2. kill_proc_group() sends SIGKILL via os.killpg on budget exceeded.
    3. ProcessLookupError (process already gone) is silently swallowed.
    4. PermissionError is logged but never propagated.
    5. A subprocess that exits normally does NOT trigger killpg.
    6. Static guard: every subprocess.Popen call in cli/qa_fix.py uses
       start_new_session=True (AST-based drift prevention).
    """

    def test_subprocess_starts_in_new_session(self, tmp_path):
        """_run_node_syntax_check launches Popen with start_new_session=True."""
        import qa_fix

        (tmp_path / "server.js").write_text("const x = 1;")

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0

        with patch("qa_fix.subprocess.Popen", return_value=mock_proc) as mock_popen:
            proc = qa_fix._run_node_syntax_check(tmp_path)

        assert proc is mock_proc
        assert mock_popen.called
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("start_new_session") is True

    def test_budget_exceeded_killpg_on_process_group(self):
        """kill_proc_group() calls os.killpg(pgid, SIGKILL) on a running proc."""
        import signal
        import qa_fix

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None  # still running

        with patch("qa_fix.os.getpgid", return_value=99) as mock_getpgid, \
             patch("qa_fix.os.killpg") as mock_killpg:
            qa_fix.kill_proc_group(mock_proc)

        mock_getpgid.assert_called_once_with(12345)
        mock_killpg.assert_called_once_with(99, signal.SIGKILL)

    def test_killpg_process_already_gone_is_noop(self):
        """ProcessLookupError from os.killpg is silently swallowed."""
        import qa_fix

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None  # still running (per our view)

        with patch("qa_fix.os.getpgid", return_value=99), \
             patch("qa_fix.os.killpg", side_effect=ProcessLookupError("no such process")):
            # Must not raise
            qa_fix.kill_proc_group(mock_proc)

    def test_killpg_permission_error_logs_warning(self, caplog):
        """PermissionError from os.killpg is logged as a warning, not propagated."""
        import logging
        import qa_fix

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None

        with patch("qa_fix.os.getpgid", return_value=99), \
             patch("qa_fix.os.killpg", side_effect=PermissionError("access denied")):
            with caplog.at_level(logging.WARNING, logger="qa_fix"):
                qa_fix.kill_proc_group(mock_proc)

        assert any("kill_proc_group" in rec.message for rec in caplog.records), \
            "Expected a warning log from kill_proc_group on PermissionError"

    def test_normal_exit_does_not_trigger_killpg(self):
        """If subprocess has already exited (poll() != None), killpg is NOT called."""
        import qa_fix

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = 0  # already finished normally

        with patch("qa_fix.os.killpg") as mock_killpg:
            qa_fix.kill_proc_group(mock_proc)

        mock_killpg.assert_not_called()

    def test_static_guard_qa_fix_uses_new_session(self):
        """AST guard: every subprocess.Popen call in cli/qa_fix.py has start_new_session=True.

        Pattern mirrors test_subprocess_timeouts.py: parse the AST, walk all
        Call nodes, find subprocess.Popen invocations, assert each has the
        start_new_session keyword argument. Prevents silent regression when
        a future edit adds a Popen without the flag.
        """
        import ast
        from pathlib import Path

        qa_fix_path = Path(__file__).parent.parent / "cli" / "qa_fix.py"
        tree = ast.parse(qa_fix_path.read_text(), filename=str(qa_fix_path))

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Match subprocess.Popen(...)
            is_popen = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "Popen"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            )
            if not is_popen:
                continue
            has_new_session = any(
                kw.arg == "start_new_session" for kw in node.keywords
            )
            if not has_new_session:
                offenders.append(f"qa_fix.py:{node.lineno}; subprocess.Popen missing start_new_session=True")

        assert not offenders, (
            "Every subprocess.Popen in cli/qa_fix.py must pass start_new_session=True "
            "so process trees can be SIGKILL'd on FixLoopBudgetExceeded.\n\n"
            "Offenders:\n  " + "\n  ".join(offenders)
        )


class TestKillProcGroupEdgeCases:
    """Coverage gaps for kill_proc_group and _run_node_syntax_check."""

    def test_kill_proc_group_none_is_noop(self):
        """kill_proc_group(None) must be a no-op, does not raise."""
        import qa_fix
        with patch("qa_fix.os.killpg") as mock_killpg:
            qa_fix.kill_proc_group(None)
        mock_killpg.assert_not_called()

    def test_run_node_syntax_check_missing_server_js_returns_none(self, tmp_path):
        """When server.js does not exist, returns None and _last_qa_proc is unchanged."""
        import qa_fix
        prior = qa_fix._last_qa_proc
        result = qa_fix._run_node_syntax_check(tmp_path)  # no server.js in tmp_path
        assert result is None
        # _last_qa_proc must not have been updated to a new proc
        assert qa_fix._last_qa_proc is prior

    def test_run_node_syntax_check_popen_failure_returns_none(self, tmp_path):
        """If subprocess.Popen raises, _run_node_syntax_check returns None (no crash)."""
        import qa_fix
        (tmp_path / "server.js").write_text("const x = 1;")
        with patch("qa_fix.subprocess.Popen", side_effect=OSError("node not found")):
            result = qa_fix._run_node_syntax_check(tmp_path)
        assert result is None

    def test_getpgid_oserror_does_not_propagate(self):
        """Unexpected OSError from os.getpgid is silently swallowed."""
        import qa_fix
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        with patch("qa_fix.os.getpgid", side_effect=OSError("unexpected")):
            qa_fix.kill_proc_group(mock_proc)  # must not raise

    def test_getpgid_process_lookup_error_before_killpg(self):
        """ProcessLookupError from os.getpgid (race: process exited after poll()) is handled."""
        import qa_fix
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        with patch("qa_fix.os.getpgid", side_effect=ProcessLookupError("gone")), \
             patch("qa_fix.os.killpg") as mock_killpg:
            qa_fix.kill_proc_group(mock_proc)
        mock_killpg.assert_not_called()  # killpg must not be reached
