"""Tests for cli/design_fix.py, design fix agent."""
import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)

import design_fix


@pytest.fixture(autouse=True)
def _stub_anthropic_api_key(monkeypatch):
    """Ensure make_client() has a key so design_fix.fix_design() can instantiate
    its Anthropic client. Individual tests patch _shared_call_api to avoid real
    network traffic; this fixture just satisfies make_client()'s key check,
    which is now eager (§02 timeout hardening).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-design-fix")


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

class TestFixDesignGuardrails:
    """Verify that protected files are never written, regardless of Claude output."""

    def _spec(self):
        return {
            "design": {
                "primary_color": "#B45309",
                "secondary_color": "#78350F",
                "accent_color": "#F59E0B",
                "font_display": "DM Sans",
                "font_body": "Source Sans 3",
                "style": "warm earthy",
            }
        }

    def test_skips_protected_files(self, tmp_path):
        """Create files for all protected paths. Claude returns fixes for all.
        Only layout.ejs should be written."""
        # Create build dir with files
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>old layout</html>")
        (tmp_path / "server.js").write_text("const app = require('express')();")
        (tmp_path / "middleware").mkdir()
        (tmp_path / "middleware" / "auth.js").write_text("module.exports = {};")
        (tmp_path / "db").mkdir()
        (tmp_path / "db" / "schema.sql").write_text("CREATE TABLE x;")

        issues = [
            {"severity": "critical", "category": "nav", "file": "views/layout.ejs",
             "issue": "Nav is transparent", "suggestion": "Add bg-gray-900"},
        ]

        # Claude returns fix blocks for protected AND allowed files
        response_text = (
            "===FILE: views/layout.ejs===\n<html>fixed layout</html>\n===END FILE===\n"
            "===FILE: server.js===\nconst app = 'hacked';\n===END FILE===\n"
            "===FILE: middleware/auth.js===\nmodule.exports = 'hacked';\n===END FILE===\n"
            "===FILE: routes/auth.js===\nrouter.get('/');\n===END FILE===\n"
            "===FILE: db/schema.sql===\nDROP TABLE;\n===END FILE===\n"
            "===FILE: db/seed.sql===\nINSERT evil;\n===END FILE===\n"
            "===FILE: db/migrate.js===\nprocess.exit();\n===END FILE===\n"
            "===FILE: db/index.js===\nrequire('evil');\n===END FILE===\n"
            "===FILE: package.json===\n{\"hacked\": true}\n===END FILE===\n"
        )

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=response_text)]

        with patch("design_fix._shared_call_api", return_value=(mock_response, None)):
            result = design_fix.fix_design(str(tmp_path), self._spec(), issues)

        assert result["success"] is True
        assert result["files_modified"] == ["views/layout.ejs"]
        # Protected files should not have been modified
        assert (tmp_path / "server.js").read_text() == "const app = require('express')();"
        assert (tmp_path / "middleware" / "auth.js").read_text() == "module.exports = {};"
        assert (tmp_path / "db" / "schema.sql").read_text() == "CREATE TABLE x;"

    def test_only_modifies_ejs_and_css(self, tmp_path):
        """Claude returns .js file content. Verify .js files not written."""
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>old</html>")

        issues = [
            {"severity": "critical", "category": "layout", "file": "views/layout.ejs",
             "issue": "Bad layout", "suggestion": "Fix it"},
        ]

        response_text = (
            "===FILE: views/layout.ejs===\n<html>fixed</html>\n===END FILE===\n"
            "===FILE: routes/tasks.js===\nrouter.get('/');\n===END FILE===\n"
            "===FILE: public/custom.css===\nbody { color: red; }\n===END FILE===\n"
        )

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=response_text)]

        with patch("design_fix._shared_call_api", return_value=(mock_response, None)):
            result = design_fix.fix_design(str(tmp_path), self._spec(), issues)

        assert result["success"] is True
        assert "views/layout.ejs" in result["files_modified"]
        assert "public/custom.css" in result["files_modified"]
        assert "routes/tasks.js" not in result["files_modified"]
        # .js file should not exist
        assert not (tmp_path / "routes" / "tasks.js").exists()


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

class TestFixDesignPrompt:
    """Verify prompt includes design context and issues."""

    def _spec(self):
        return {
            "design": {
                "primary_color": "#B45309",
                "secondary_color": "#78350F",
                "accent_color": "#F59E0B",
                "font_display": "DM Sans",
                "font_body": "Source Sans 3",
                "style": "warm earthy",
            }
        }

    def test_includes_design_context(self, tmp_path):
        """Verify prompt contains colors, fonts, style name."""
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>test</html>")

        issues = [
            {"severity": "critical", "category": "nav", "file": "views/layout.ejs",
             "issue": "Nav is white", "suggestion": "Add bg-gray-900"},
        ]

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No changes needed.")]
        captured_messages = []

        def capture_call_api(**kwargs):
            captured_messages.append(kwargs.get("messages", []))
            return (mock_response, None)

        with patch("design_fix._shared_call_api", side_effect=capture_call_api):
            design_fix.fix_design(str(tmp_path), self._spec(), issues)

        assert len(captured_messages) == 1
        prompt_text = captured_messages[0][0]["content"]
        assert "#B45309" in prompt_text
        assert "DM Sans" in prompt_text
        assert "warm earthy" in prompt_text

    def test_includes_issues_in_prompt(self, tmp_path):
        """Verify prompt contains the issue descriptions."""
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>test</html>")

        issues = [
            {"severity": "critical", "category": "nav", "file": "views/layout.ejs",
             "issue": "Nav is white", "suggestion": "Add bg-gray-900"},
            {"severity": "critical", "category": "cards", "file": "views/dashboard.ejs",
             "issue": "No card shadows", "suggestion": "Add shadow-lg"},
        ]

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No changes.")]
        captured_messages = []

        def capture_call_api(**kwargs):
            captured_messages.append(kwargs.get("messages", []))
            return (mock_response, None)

        with patch("design_fix._shared_call_api", side_effect=capture_call_api):
            design_fix.fix_design(str(tmp_path), self._spec(), issues)

        prompt_text = captured_messages[0][0]["content"]
        assert "Nav is white" in prompt_text
        assert "No card shadows" in prompt_text
        assert "Add bg-gray-900" in prompt_text
        assert "Add shadow-lg" in prompt_text


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestFixDesignErrorHandling:
    """Verify graceful degradation on errors."""

    def _spec(self):
        return {
            "design": {
                "primary_color": "#B45309",
                "font_display": "DM Sans",
                "style": "warm earthy",
            }
        }

    def test_api_failure_returns_graceful_result(self, tmp_path):
        """Mock API to raise ConnectionError. Verify returns success=False."""
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>test</html>")

        issues = [
            {"severity": "critical", "category": "nav", "file": "views/layout.ejs",
             "issue": "Nav is white", "suggestion": "Fix it"},
        ]

        with patch("design_fix._shared_call_api", side_effect=ConnectionError("network down")):
            result = design_fix.fix_design(str(tmp_path), self._spec(), issues)

        assert result["success"] is False
        assert result["files_modified"] == []
        assert "network down" in result["error"]


# ---------------------------------------------------------------------------
# Behavior
# ---------------------------------------------------------------------------

class TestFixDesignBehavior:
    """Verify single-attempt semantics and model selection."""

    def _spec(self):
        return {
            "design": {
                "primary_color": "#B45309",
                "font_display": "DM Sans",
                "style": "warm earthy",
            }
        }

    def test_single_attempt_only(self, tmp_path):
        """Mock API. Call fix_design. Verify API called exactly once."""
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>test</html>")

        issues = [
            {"severity": "critical", "category": "nav", "file": "views/layout.ejs",
             "issue": "Nav is white", "suggestion": "Fix it"},
        ]

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No changes.")]

        with patch("design_fix._shared_call_api", return_value=(mock_response, None)) as mock_api:
            design_fix.fix_design(str(tmp_path), self._spec(), issues)

        assert mock_api.call_count == 1

    def test_uses_opus_model(self, tmp_path):
        """Mock API, capture model arg. Verify opus is used."""
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>test</html>")

        issues = [
            {"severity": "critical", "category": "nav", "file": "views/layout.ejs",
             "issue": "Nav is white", "suggestion": "Fix it"},
        ]

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No changes.")]
        captured_kwargs = {}

        def capture_call_api(**kwargs):
            captured_kwargs.update(kwargs)
            return (mock_response, None)

        with patch("design_fix._shared_call_api", side_effect=capture_call_api):
            design_fix.fix_design(str(tmp_path), self._spec(), issues)

        assert "opus" in captured_kwargs.get("model", "")

    def test_returns_success_with_modified_files(self, tmp_path):
        """Create layout.ejs. Mock API to return valid fix block. Verify result."""
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>old</html>")

        issues = [
            {"severity": "critical", "category": "layout", "file": "views/layout.ejs",
             "issue": "Bad layout", "suggestion": "Fix it"},
        ]

        response_text = "===FILE: views/layout.ejs===\n<html>fixed layout</html>\n===END FILE==="

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=response_text)]

        with patch("design_fix._shared_call_api", return_value=(mock_response, None)):
            result = design_fix.fix_design(str(tmp_path), self._spec(), issues)

        assert result["success"] is True
        assert result["files_modified"] == ["views/layout.ejs"]
        assert result["error"] is None
        # Verify file actually written
        assert (tmp_path / "views" / "layout.ejs").read_text() == "<html>fixed layout</html>\n"

    def test_no_critical_issues_returns_early(self, tmp_path):
        """If all issues are warnings (not critical), return early with no API call."""
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>test</html>")

        issues = [
            {"severity": "warning", "category": "nav", "file": "views/layout.ejs",
             "issue": "Minor spacing issue", "suggestion": "Adjust padding"},
        ]

        with patch("design_fix._shared_call_api") as mock_api:
            result = design_fix.fix_design(str(tmp_path), self._spec(), issues)

        mock_api.assert_not_called()
        assert result["success"] is True
        assert result["files_modified"] == []


# ---------------------------------------------------------------------------
# _parse_fix_files
# ---------------------------------------------------------------------------

class TestParseFixFiles:
    """Test extraction of ===FILE: path=== blocks."""

    def test_single_file(self):
        text = "===FILE: views/layout.ejs===\n<html>fixed</html>\n===END FILE==="
        files = design_fix._parse_fix_files(text)
        assert len(files) == 1
        assert files[0][0] == "views/layout.ejs"
        assert "<html>fixed</html>" in files[0][1]

    def test_multiple_files(self):
        text = (
            "===FILE: views/layout.ejs===\n<html>fixed</html>\n===END FILE===\n"
            "===FILE: public/style.css===\nbody { color: red; }\n===END FILE==="
        )
        files = design_fix._parse_fix_files(text)
        assert len(files) == 2

    def test_path_traversal_blocked(self):
        text = "===FILE: ../../etc/passwd===\nroot\n===END FILE==="
        files = design_fix._parse_fix_files(text)
        assert len(files) == 0

    def test_absolute_path_blocked(self):
        text = "===FILE: /etc/shadow===\nroot\n===END FILE==="
        files = design_fix._parse_fix_files(text)
        assert len(files) == 0

    def test_protected_files_filtered(self):
        text = (
            "===FILE: server.js===\nhacked\n===END FILE===\n"
            "===FILE: package.json===\nhacked\n===END FILE===\n"
            "===FILE: views/layout.ejs===\n<html>ok</html>\n===END FILE==="
        )
        files = design_fix._parse_fix_files(text)
        assert len(files) == 1
        assert files[0][0] == "views/layout.ejs"

    def test_non_allowed_extension_filtered(self):
        text = (
            "===FILE: routes/tasks.js===\nmodule.exports = {};\n===END FILE===\n"
            "===FILE: views/home.ejs===\n<h1>Home</h1>\n===END FILE==="
        )
        files = design_fix._parse_fix_files(text)
        assert len(files) == 1
        assert files[0][0] == "views/home.ejs"

    def test_empty_response(self):
        assert design_fix._parse_fix_files("No changes needed.") == []


# ---------------------------------------------------------------------------
# _collect_relevant_files
# ---------------------------------------------------------------------------

class TestCollectRelevantFiles:
    """Test source file collection from build directory."""

    def test_always_includes_layout(self, tmp_path):
        """layout.ejs is always collected even if not in issues."""
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>layout</html>")

        issues = [
            {"severity": "critical", "category": "cards", "file": "views/dashboard.ejs",
             "issue": "No shadows", "suggestion": "Add shadow-lg"},
        ]
        files = design_fix._collect_relevant_files(tmp_path, issues)
        paths = [f[0] for f in files]
        assert "views/layout.ejs" in paths

    def test_collects_issue_files(self, tmp_path):
        """Files referenced in issues are collected."""
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>layout</html>")
        (tmp_path / "views" / "dashboard.ejs").write_text("<h1>Dashboard</h1>")

        issues = [
            {"severity": "critical", "category": "cards", "file": "views/dashboard.ejs",
             "issue": "No shadows", "suggestion": "Add shadow-lg"},
        ]
        files = design_fix._collect_relevant_files(tmp_path, issues)
        paths = [f[0] for f in files]
        assert "views/dashboard.ejs" in paths

    def test_only_collects_ejs_and_css(self, tmp_path):
        """Non-EJS/CSS files are skipped even if referenced in issues."""
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>layout</html>")
        (tmp_path / "routes").mkdir()
        (tmp_path / "routes" / "auth.js").write_text("router.get('/');")

        issues = [
            {"severity": "critical", "category": "auth", "file": "routes/auth.js",
             "issue": "Something", "suggestion": "Fix"},
        ]
        files = design_fix._collect_relevant_files(tmp_path, issues)
        paths = [f[0] for f in files]
        assert "routes/auth.js" not in paths

    def test_respects_context_limit(self, tmp_path):
        """Total chars collected should not exceed _MAX_CONTEXT_CHARS."""
        (tmp_path / "views").mkdir()
        big_content = "x" * (design_fix._MAX_CONTEXT_CHARS + 1000)
        (tmp_path / "views" / "layout.ejs").write_text(big_content)
        (tmp_path / "views" / "extra.ejs").write_text("y" * 1000)

        issues = [
            {"severity": "critical", "category": "layout", "file": "views/extra.ejs",
             "issue": "Bad", "suggestion": "Fix"},
        ]
        files = design_fix._collect_relevant_files(tmp_path, issues)
        # Should not raise; may skip extra files that exceed limit
        assert isinstance(files, list)


# ---------------------------------------------------------------------------
# _build_design_context_summary
# ---------------------------------------------------------------------------

class TestBuildDesignContextSummary:
    """Test design context formatting."""

    def test_includes_all_fields(self):
        spec = {
            "design": {
                "primary_color": "#B45309",
                "secondary_color": "#78350F",
                "accent_color": "#F59E0B",
                "font_display": "DM Sans",
                "font_body": "Source Sans 3",
                "style": "warm earthy",
            }
        }
        ctx = design_fix._build_design_context_summary(spec)
        assert "#B45309" in ctx
        assert "#78350F" in ctx
        assert "#F59E0B" in ctx
        assert "DM Sans" in ctx
        assert "Source Sans 3" in ctx
        assert "warm earthy" in ctx

    def test_handles_missing_design(self):
        spec = {}
        ctx = design_fix._build_design_context_summary(spec)
        assert isinstance(ctx, str)
        # Should not raise, just return something


# ---------------------------------------------------------------------------
# Advisor toggle
# ---------------------------------------------------------------------------

class TestAdvisorToggle:
    """Tests for USE_ADVISOR_DESIGN_FIX env var integration."""

    def _spec(self):
        return {
            "design": {
                "primary_color": "#B45309",
                "font_display": "DM Sans",
                "style": "warm earthy",
            }
        }

    def _make_mock_response(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No changes needed.")]
        mock_metrics = MagicMock()
        return (mock_response, mock_metrics)

    def test_advisor_enabled_switches_model_to_sonnet(self, tmp_path):
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>test</html>")

        issues = [
            {"severity": "critical", "category": "nav", "file": "views/layout.ejs",
             "issue": "Nav is white", "suggestion": "Fix it"},
        ]

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("design_fix._shared_call_api", side_effect=capture_call), \
             patch.dict(os.environ, {"USE_ADVISOR_DESIGN_FIX": "true"}):
            design_fix.fix_design(str(tmp_path), self._spec(), issues)

        assert "sonnet" in captured.get("model", "")

    def test_advisor_enabled_passes_advisor_params(self, tmp_path):
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>test</html>")

        issues = [
            {"severity": "critical", "category": "nav", "file": "views/layout.ejs",
             "issue": "Nav is white", "suggestion": "Fix it"},
        ]

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("design_fix._shared_call_api", side_effect=capture_call), \
             patch.dict(os.environ, {"USE_ADVISOR_DESIGN_FIX": "true"}):
            design_fix.fix_design(str(tmp_path), self._spec(), issues)

        assert captured.get("advisor_model") == "claude-opus-4-6"
        assert captured.get("advisor_max_uses") == 2

    def test_advisor_disabled_uses_opus_no_advisor(self, tmp_path):
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>test</html>")

        issues = [
            {"severity": "critical", "category": "nav", "file": "views/layout.ejs",
             "issue": "Nav is white", "suggestion": "Fix it"},
        ]

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("design_fix._shared_call_api", side_effect=capture_call), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("USE_ADVISOR_DESIGN_FIX", None)
            design_fix.fix_design(str(tmp_path), self._spec(), issues)

        assert "opus" in captured.get("model", "")
        assert "advisor_model" not in captured

    def test_advisor_guidance_in_system_prompt_when_enabled(self, tmp_path):
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>test</html>")

        issues = [
            {"severity": "critical", "category": "nav", "file": "views/layout.ejs",
             "issue": "Nav is white", "suggestion": "Fix it"},
        ]

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("design_fix._shared_call_api", side_effect=capture_call), \
             patch.dict(os.environ, {"USE_ADVISOR_DESIGN_FIX": "true"}):
            design_fix.fix_design(str(tmp_path), self._spec(), issues)

        system_text = captured["system"][0]["text"]
        assert "advisor" in system_text.lower()
        assert "respond in under 100 words" in system_text


class TestDesignFixCancelChecks:
    """Verify design_fix.py raises BuildCancelled at each of the three check points.

    Cooperative cancellation poll points (the supervisor that fires the
    cancel event is described in docs/internals/worker.md) in
    fix_design. design_fix wraps its body in try/except Exception, so the
    cancel exception must be re-raised by an explicit `except BuildCancelled:
    raise` clause placed BEFORE the broad `except Exception` handler,
    otherwise the hung build's supervisor fire would be swallowed and the
    fix loop would log it as a benign "Design fix failed" and continue.
    """

    def setup_method(self):
        from cli import cancellation
        self.build_id = "test-design-fix-cancel"
        cancellation.register(self.build_id)

    def teardown_method(self):
        from cli import cancellation
        cancellation.clear(self.build_id)

    def _spec(self):
        return {
            "app_name": "test-app",
            "design": {
                "primary_color": "#B45309",
                "secondary_color": "#78350F",
                "accent_color": "#F59E0B",
                "font_display": "DM Sans",
                "font_body": "Source Sans 3",
                "style": "warm earthy",
            },
        }

    def _issues(self):
        return [
            {
                "severity": "critical",
                "category": "nav",
                "file": "views/layout.ejs",
                "issue": "Nav is transparent",
                "suggestion": "Add bg-gray-900",
            }
        ]

    def _make_mock_response(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No fixes needed.")]
        mock_metrics = MagicMock()
        return (mock_response, mock_metrics)

    def _build_tree(self, tmp_path):
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html>old layout</html>")

    def test_cancels_at_iteration_top(self, tmp_path):
        """Fire cancel before fix_design runs; _shared_call_api must NOT run.
        The broad except Exception must NOT swallow BuildCancelled."""
        from cli import cancellation
        from api_client import BuildCancelled

        self._build_tree(tmp_path)

        cancellation.fire(self.build_id)

        api_mock = MagicMock(side_effect=AssertionError(
            "_shared_call_api must not be called after cancel fired at iteration top"
        ))
        with patch("design_fix._shared_call_api", api_mock):
            with pytest.raises(BuildCancelled) as exc_info:
                design_fix.fix_design(str(tmp_path), self._spec(), self._issues(),
                                      cancel_key=self.build_id)

        assert exc_info.value.build_id == self.build_id
        assert api_mock.called is False

    def test_cancels_after_api_return(self, tmp_path):
        """Cancel fires mid _shared_call_api; post_api guard raises before
        _parse_fix_files. BuildCancelled must escape the broad except."""
        from cli import cancellation
        from api_client import BuildCancelled

        self._build_tree(tmp_path)

        def fire_and_return(**kwargs):
            cancellation.fire(self.build_id)
            return self._make_mock_response()

        parse_mock = MagicMock(side_effect=AssertionError(
            "_parse_fix_files must not be called after post_api cancel check"
        ))
        with patch("design_fix._shared_call_api", side_effect=fire_and_return), \
             patch("design_fix._parse_fix_files", parse_mock):
            with pytest.raises(BuildCancelled) as exc_info:
                design_fix.fix_design(str(tmp_path), self._spec(), self._issues(),
                                      cancel_key=self.build_id)

        assert exc_info.value.build_id == self.build_id
        assert parse_mock.called is False

    def test_cancels_before_subprocess(self, tmp_path):
        """API returns clean; cancel fires between parse and file-write.
        pre_subprocess guard must raise before any Path.write_text call."""
        from cli import cancellation
        from api_client import BuildCancelled

        self._build_tree(tmp_path)

        def parse_and_fire(_response_text):
            cancellation.fire(self.build_id)
            return [("views/layout.ejs", "<html>fixed layout</html>")]

        with patch("design_fix._shared_call_api", side_effect=lambda **k: self._make_mock_response()), \
             patch("design_fix._parse_fix_files", side_effect=parse_and_fire), \
             patch("pathlib.Path.write_text") as write_mock:
            with pytest.raises(BuildCancelled) as exc_info:
                design_fix.fix_design(str(tmp_path), self._spec(), self._issues(),
                                      cancel_key=self.build_id)

        assert exc_info.value.build_id == self.build_id
        assert write_mock.called is False
