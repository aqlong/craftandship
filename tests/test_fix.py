import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

# tests/test_fix.py - Tests for cli/fix.py self-correction module

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure cli/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

from fix import (
    attempt_fix,
    _collect_source_files,
    _parse_fix_files,
    _format_test_results,
    _build_route_manifest,
    _MAX_CONTEXT_CHARS,
)


class TestParseFixFiles:
    """Test extraction of --BEGINFILE-- path blocks from Claude response."""

    def test_single_file(self):
        text = '--BEGINFILE-- server.js\nconsole.log("fixed");\n--ENDFILE--'
        files = _parse_fix_files(text)
        assert len(files) == 1
        assert files[0][0] == "server.js"
        assert 'console.log("fixed")' in files[0][1]

    def test_multiple_files(self):
        text = (
            "--BEGINFILE-- server.js\nconst app = express();\n--ENDFILE--\n"
            "--BEGINFILE-- routes/auth.js\nrouter.get('/login');\n--ENDFILE--"
        )
        files = _parse_fix_files(text)
        assert len(files) == 2
        assert files[0][0] == "server.js"
        assert files[1][0] == "routes/auth.js"

    def test_path_traversal_blocked(self):
        text = "--BEGINFILE-- ../../../etc/passwd\nroot:x:0\n--ENDFILE--"
        files = _parse_fix_files(text)
        assert len(files) == 0

    def test_absolute_path_blocked(self):
        text = "--BEGINFILE-- /etc/passwd\nroot:x:0\n--ENDFILE--"
        files = _parse_fix_files(text)
        assert len(files) == 0

    def test_empty_response(self):
        files = _parse_fix_files("No changes needed.")
        assert len(files) == 0

    def test_duplicated_beginfile_marker_keeps_last_copy(self):
        """Regression: Claude sometimes emits two --BEGINFILE-- markers for the
        same file without an --ENDFILE-- between them on long continuations.
        The non-greedy outer regex then captures both copies as one block with
        the inner marker embedded in content. The parser must split on the
        embedded marker and keep only the last (most recent) copy. A
        medicine-waves build failure was the original incident.
        """
        text = (
            "--BEGINFILE-- routes/videos.js\n"
            "const stale = require('express');\n"
            "stale.old_code_goes_here();\n"
            "--BEGINFILE-- routes/videos.js\n"
            "const express = require('express');\n"
            "const router = express.Router();\n"
            "module.exports = router;\n"
            "--ENDFILE--"
        )
        files = _parse_fix_files(text)
        assert len(files) == 1
        path, content = files[0]
        assert path == "routes/videos.js"
        # Only the last copy survives; stale copy and embedded marker are gone
        assert "stale" not in content
        assert "--BEGINFILE--" not in content
        assert "const express = require('express');" in content
        assert "module.exports = router;" in content


class TestCollectSourceFiles:
    """Test source file collection from build directory."""

    def test_collects_key_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            build_dir = Path(tmpdir)
            (build_dir / "server.js").write_text("const app = require('express')();")
            (build_dir / "package.json").write_text('{"name": "test"}')
            (build_dir / "middleware").mkdir()
            (build_dir / "middleware" / "auth.js").write_text("module.exports = {};")

            files = _collect_source_files(build_dir)
            paths = [f[0] for f in files]
            assert "server.js" in paths
            assert "package.json" in paths
            assert "middleware/auth.js" in paths

    def test_collects_routes_and_views(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            build_dir = Path(tmpdir)
            (build_dir / "server.js").write_text("app.listen(3000);")
            (build_dir / "routes").mkdir()
            (build_dir / "routes" / "auth.js").write_text("router.get('/login');")
            (build_dir / "views").mkdir()
            (build_dir / "views" / "login.ejs").write_text("<h1>Login</h1>")

            files = _collect_source_files(build_dir)
            paths = [f[0] for f in files]
            assert "routes/auth.js" in paths
            assert "views/login.ejs" in paths

    def test_skips_non_source_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            build_dir = Path(tmpdir)
            (build_dir / "server.js").write_text("app.listen(3000);")
            (build_dir / "public").mkdir()
            (build_dir / "public" / "image.png").write_bytes(b"\x89PNG")

            files = _collect_source_files(build_dir)
            paths = [f[0] for f in files]
            assert "public/image.png" not in paths

    def test_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            files = _collect_source_files(Path(tmpdir))
            assert len(files) == 0


class TestFormatTestResults:
    """Test formatting of test results for Claude prompt."""

    def test_format_mixed_results(self):
        results = [
            ("health endpoint returns 200", True, None),
            ("login page renders form", False, "Missing email input"),
        ]
        text = _format_test_results(results)
        assert "[PASS] health endpoint" in text
        assert "[FAIL] login page" in text
        assert "Missing email input" in text


class TestAttemptFix:
    """Test the main attempt_fix function."""

    def test_no_source_files_returns_not_fixed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = attempt_fix(
                build_dir=tmpdir,
                test_results=[("test1", False, "error")],
            )
            assert result["fixed"] is False

    def test_no_failed_tests_returns_not_fixed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "server.js").write_text("app.listen(3000);")
            result = attempt_fix(
                build_dir=tmpdir,
                test_results=[("test1", True, None)],
            )
            assert result["fixed"] is False

    @patch("fix._create_client")
    def test_prompt_redacts_secrets_from_app_stderr(self, mock_create):
        """The app under test is started with a database URL; if it prints it,
        the password must not reach the model provider inside the fix prompt."""
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        mock_client.messages.create.return_value = MagicMock(content=[MagicMock(text="no fixes")])
        password = "pw-" + "q" * 20
        stderr = "Error: could not connect to postgres://worker:" + password + "@dbhost:5432/test_app\n    at boot (server.js:12)"

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "server.js").write_text("// broken")
            attempt_fix(
                build_dir=tmpdir,
                test_results=[("health endpoint", False, "Expected 200, got 500")],
                app_stderr=stderr,
            )

        sent = repr(mock_client.messages.create.call_args)
        assert password not in sent
        assert "server.js:12" in sent  # the useful part of the trace survives

    @patch("fix._create_client")
    def test_writes_fixed_files(self, mock_create):
        """When Claude returns fixes, they should be written to disk."""
        mock_client = MagicMock()
        mock_create.return_value = mock_client

        fix_response = '--BEGINFILE-- server.js\nconst express = require("express");\nconst app = express();\napp.get("/health", (_, res) => res.json({status:"ok"}));\napp.listen(3000);\n--ENDFILE--'

        mock_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text=fix_response)]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "server.js").write_text("// broken")

            result = attempt_fix(
                build_dir=tmpdir,
                test_results=[("health endpoint", False, "Expected 200, got 500")],
            )

            assert result["fixed"] is True
            assert "server.js" in result["files_changed"]
            # Verify file was actually written
            content = (Path(tmpdir) / "server.js").read_text()
            assert "express" in content

    @patch("fix._create_client")
    def test_no_fixes_returned(self, mock_create):
        """When Claude returns no file blocks, return not fixed."""
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        mock_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="I cannot determine the issue.")]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "server.js").write_text("// broken")

            result = attempt_fix(
                build_dir=tmpdir,
                test_results=[("test1", False, "error")],
            )

            assert result["fixed"] is False

    @patch("fix._create_client")
    def test_creates_subdirectories_for_new_files(self, mock_create):
        """If Claude outputs a file in a new subdirectory, it should be created."""
        mock_client = MagicMock()
        mock_create.return_value = mock_client

        fix_response = '--BEGINFILE-- routes/auth.js\nmodule.exports = router;\n--ENDFILE--'

        mock_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text=fix_response)]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "server.js").write_text("app.listen(3000);")

            result = attempt_fix(
                build_dir=tmpdir,
                test_results=[("auth test", False, "Missing route")],
            )

            assert result["fixed"] is True
            assert (Path(tmpdir) / "routes" / "auth.js").exists()


class TestBuildRouteManifest:
    """Tests for _build_route_manifest used in fix context."""

    def test_includes_spec_pages(self):
        spec = {
            "pages": [
                {"path": "/dashboard", "auth_required": True},
                {"path": "/time", "auth_required": True},
            ],
            "data_models": [],
        }
        manifest = _build_route_manifest(spec)
        assert "GET /dashboard" in manifest
        assert "GET /time" in manifest
        assert "auth required" in manifest

    def test_includes_crud_routes(self):
        spec = {
            "pages": [],
            "data_models": [
                {"name": "TimeEntry", "fields": []},
            ],
        }
        manifest = _build_route_manifest(spec)
        assert "TimeEntry" in manifest
        assert "time-entry" in manifest

    def test_includes_route_aliases(self):
        spec = {
            "pages": [
                {"path": "/time", "auth_required": True},
            ],
            "data_models": [
                {"name": "TimeEntry", "fields": []},
            ],
        }
        manifest = _build_route_manifest(spec)
        assert "CRITICAL" in manifest
        assert "/time" in manifest
        assert "time-entry" in manifest
        assert "server.js" in manifest

    def test_empty_spec_returns_empty(self):
        assert _build_route_manifest({}) == ""
        assert _build_route_manifest(None) == ""


class TestAdvisorIntegration:
    """Tests for advisor tool integration in fix.py."""

    def _make_mock_response(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No fixes needed.")]
        mock_metrics = MagicMock()
        return (mock_response, mock_metrics)

    def test_advisor_enabled_switches_model_to_sonnet(self, tmp_path):
        from fix import attempt_fix
        (tmp_path / "server.js").write_text("const express = require('express');")

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("fix._shared_call_api", side_effect=capture_call), \
             patch("fix._create_client"), \
             patch.dict(os.environ, {"USE_ADVISOR_FIX": "true"}):
            attempt_fix(str(tmp_path), [("test1", False, "error1")])

        assert "sonnet" in captured.get("model", "")

    def test_advisor_enabled_passes_advisor_params(self, tmp_path):
        from fix import attempt_fix
        (tmp_path / "server.js").write_text("const express = require('express');")

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("fix._shared_call_api", side_effect=capture_call), \
             patch("fix._create_client"), \
             patch.dict(os.environ, {"USE_ADVISOR_FIX": "true"}):
            attempt_fix(str(tmp_path), [("test1", False, "error1")])

        assert captured.get("advisor_model") == "claude-opus-4-6"
        assert captured.get("advisor_max_uses") == 2

    def test_advisor_disabled_uses_opus_no_advisor(self, tmp_path):
        from fix import attempt_fix
        (tmp_path / "server.js").write_text("const express = require('express');")

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("fix._shared_call_api", side_effect=capture_call), \
             patch("fix._create_client"), \
             patch.dict(os.environ, {}, clear=False):
            # Ensure USE_ADVISOR_FIX is not set
            os.environ.pop("USE_ADVISOR_FIX", None)
            attempt_fix(str(tmp_path), [("test1", False, "error1")])

        assert "opus" in captured.get("model", "")
        assert "advisor_model" not in captured

    def test_advisor_guidance_in_system_prompt_when_enabled(self, tmp_path):
        from fix import attempt_fix
        (tmp_path / "server.js").write_text("const express = require('express');")

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("fix._shared_call_api", side_effect=capture_call), \
             patch("fix._create_client"), \
             patch.dict(os.environ, {"USE_ADVISOR_FIX": "true"}):
            attempt_fix(str(tmp_path), [("test1", False, "error1")])

        system_text = captured["system"][0]["text"]
        assert "advisor" in system_text
        assert "respond in under 100 words" in system_text

    def test_advisor_guidance_absent_when_disabled(self, tmp_path):
        from fix import attempt_fix, FIX_SYSTEM_PROMPT
        (tmp_path / "server.js").write_text("const express = require('express');")

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("fix._shared_call_api", side_effect=capture_call), \
             patch("fix._create_client"), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("USE_ADVISOR_FIX", None)
            attempt_fix(str(tmp_path), [("test1", False, "error1")])

        system_text = captured["system"][0]["text"]
        assert system_text == FIX_SYSTEM_PROMPT

    def test_call_api_forwards_advisor_params(self):
        from fix import _call_api

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            mock_resp = MagicMock()
            mock_resp.content = [MagicMock(text="ok")]
            return (mock_resp, MagicMock())

        with patch("fix._shared_call_api", side_effect=capture_call):
            _call_api(MagicMock(), [{"role": "user", "content": "hi"}],
                      advisor_model="test-model", advisor_max_uses=3)

        assert captured["advisor_model"] == "test-model"
        assert captured["advisor_max_uses"] == 3


class TestFixCancelChecks:
    """Verify fix.py raises BuildCancelled at each of the three check points.

    Cooperative cancellation poll points (the supervisor that fires the
    cancel event is described in docs/internals/worker.md) in
    attempt_fix. The supervisor fires the cancel event of the attempt it gave
    up on, and the worker hands that attempt's key to attempt_fix as
    cancel_key; attempt_fix must bail before any further API call, response parse, or
    file write so we stop burning budget on a build the worker has given up
    on.
    """

    def setup_method(self):
        from cli import cancellation
        self.build_id = "test-fix-cancel"
        cancellation.register(self.build_id)

    def teardown_method(self):
        from cli import cancellation
        cancellation.clear(self.build_id)

    def _spec(self):
        return {"app_name": "test-app"}

    def _make_mock_response(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No fixes needed.")]
        mock_metrics = MagicMock()
        return (mock_response, mock_metrics)

    def test_a_build_id_inside_the_spec_is_not_a_cancel_key(self, tmp_path):
        """attempt_fix used to look for its cancel key in spec["build_id"], a
        key the worker never set, so the supervisor's signal reached nobody.
        The key is an argument now; the spec is only a spec."""
        from cli import cancellation

        (tmp_path / "server.js").write_text("const express = require('express');")
        cancellation.fire(self.build_id)
        api_mock = MagicMock(side_effect=lambda **k: self._make_mock_response())
        with patch("fix._shared_call_api", api_mock), patch("fix._create_client"):
            attempt_fix(
                str(tmp_path), [("smoke", False, "login broken")],
                spec={"build_id": self.build_id, "app_name": "test-app"},
            )
        assert api_mock.called is True
        assert api_mock.call_args.kwargs.get("build_id") is None

    def test_cancels_at_iteration_top(self, tmp_path):
        """Fire cancel before attempt_fix runs; _call_api must NOT be invoked."""
        from cli import cancellation
        from api_client import BuildCancelled

        (tmp_path / "server.js").write_text("const express = require('express');")

        # Fire BEFORE calling attempt_fix so the top-of-iteration guard trips.
        cancellation.fire(self.build_id)

        api_mock = MagicMock(side_effect=AssertionError(
            "_shared_call_api must not be called after cancel fired at iteration top"
        ))
        with patch("fix._shared_call_api", api_mock), \
             patch("fix._create_client"):
            with pytest.raises(BuildCancelled) as exc_info:
                attempt_fix(
                    str(tmp_path),
                    [("smoke", False, "login broken")],
                    spec=self._spec(),
                    cancel_key=self.build_id,
                )

        assert exc_info.value.build_id == self.build_id
        assert api_mock.called is False

    def test_cancels_after_api_return(self, tmp_path):
        """Mock _shared_call_api to fire cancel on the way back; post_api guard
        must raise BEFORE _parse_fix_files runs."""
        from cli import cancellation
        from api_client import BuildCancelled

        (tmp_path / "server.js").write_text("const express = require('express');")

        def fire_and_return(**kwargs):
            cancellation.fire(self.build_id)
            return self._make_mock_response()

        parse_mock = MagicMock(side_effect=AssertionError(
            "_parse_fix_files must not be called after post_api cancel check"
        ))
        with patch("fix._shared_call_api", side_effect=fire_and_return), \
             patch("fix._create_client"), \
             patch("fix._parse_fix_files", parse_mock):
            with pytest.raises(BuildCancelled) as exc_info:
                attempt_fix(
                    str(tmp_path),
                    [("smoke", False, "login broken")],
                    spec=self._spec(),
                    cancel_key=self.build_id,
                )

        assert exc_info.value.build_id == self.build_id
        assert parse_mock.called is False

    def test_cancels_before_subprocess(self, tmp_path):
        """API returns cleanly; cancel fires between parse and file-write.
        The pre_subprocess guard must raise before any Path.write_text call."""
        from cli import cancellation
        from api_client import BuildCancelled

        (tmp_path / "server.js").write_text("const express = require('express');")

        def parse_and_fire(_response_text):
            cancellation.fire(self.build_id)
            return [("routes/auth.js", "module.exports = {};")]

        with patch("fix._shared_call_api", side_effect=lambda **k: self._make_mock_response()), \
             patch("fix._create_client"), \
             patch("fix._parse_fix_files", side_effect=parse_and_fire), \
             patch("pathlib.Path.write_text") as write_mock:
            with pytest.raises(BuildCancelled) as exc_info:
                attempt_fix(
                    str(tmp_path),
                    [("smoke", False, "login broken")],
                    spec=self._spec(),
                    cancel_key=self.build_id,
                )

        assert exc_info.value.build_id == self.build_id
        assert write_mock.called is False
