"""Tests for cli/review.py, code review agent (Stage 2.5)."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)

import review


# ---------------------------------------------------------------------------
# _collect_source_files
# ---------------------------------------------------------------------------

class TestCollectSourceFiles:
    def test_collects_key_files(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000)")
        (tmp_path / "package.json").write_text('{"name":"test"}')
        files = review._collect_source_files(tmp_path)
        paths = [f[0] for f in files]
        assert "server.js" in paths
        assert "package.json" in paths

    def test_collects_routes_and_views(self, tmp_path):
        (tmp_path / "routes").mkdir()
        (tmp_path / "routes" / "auth.js").write_text("router.get('/')")
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "home.ejs").write_text("<h1>Home</h1>")
        files = review._collect_source_files(tmp_path)
        paths = [f[0] for f in files]
        assert "routes/auth.js" in paths
        assert "views/home.ejs" in paths

    def test_skips_non_source_files(self, tmp_path):
        (tmp_path / "routes").mkdir()
        (tmp_path / "routes" / "auth.js").write_text("x")
        (tmp_path / "routes" / "notes.txt").write_text("ignore me")
        files = review._collect_source_files(tmp_path)
        paths = [f[0] for f in files]
        assert "routes/notes.txt" not in paths

    def test_respects_context_limit(self, tmp_path):
        (tmp_path / "routes").mkdir()
        big_content = "x" * (review._MAX_CONTEXT_CHARS + 1000)
        (tmp_path / "routes" / "big.js").write_text(big_content)
        # Should not raise; just skip files that exceed the limit
        files = review._collect_source_files(tmp_path)
        assert isinstance(files, list)


# ---------------------------------------------------------------------------
# _build_review_prompt
# ---------------------------------------------------------------------------

class TestBuildReviewPrompt:
    def _spec(self):
        return {
            "app_name": "test-app",
            "description": "A test app",
            "roles": ["admin", "user"],
            "pages": [
                {"name": "Dashboard", "path": "/dashboard", "auth_required": True, "roles": ["admin"]},
                {"name": "Home", "path": "/", "auth_required": False},
            ],
            "data_models": [
                {"name": "Widget", "fields": []},
                {"name": "Order", "fields": []},
            ],
        }

    def test_includes_spec_pages(self):
        prompt = review._build_review_prompt(self._spec(), [])
        assert "/dashboard" in prompt
        assert "Dashboard" in prompt

    def test_includes_data_models(self):
        prompt = review._build_review_prompt(self._spec(), [])
        assert "Widget" in prompt
        assert "Order" in prompt

    def test_includes_crud_operations(self):
        prompt = review._build_review_prompt(self._spec(), [])
        assert "list" in prompt
        assert "create" in prompt
        assert "delete" in prompt

    def test_includes_auth_note_for_protected_pages(self):
        prompt = review._build_review_prompt(self._spec(), [])
        assert "requireAuth" in prompt

    def test_includes_source_files(self):
        files = [("server.js", "app.listen(3000)"), ("routes/auth.js", "router.get('/')")]
        prompt = review._build_review_prompt(self._spec(), files)
        assert "server.js" in prompt
        assert "app.listen(3000)" in prompt


# ---------------------------------------------------------------------------
# review_build
# ---------------------------------------------------------------------------

class TestReviewBuild:
    def _spec(self):
        return {"app_name": "test-app", "description": "test", "pages": [], "data_models": [], "roles": []}

    def test_returns_skipped_when_dir_missing(self):
        result = review.review_build("/nonexistent/path/xyz", self._spec())
        assert result["passed"] is True
        assert result.get("skipped") is True

    def test_returns_skipped_when_no_source_files(self, tmp_path):
        result = review.review_build(str(tmp_path), self._spec())
        assert result.get("skipped") is True

    def _mock_client(self, response_text):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=response_text)]
        mock_client.messages.create.return_value = mock_response
        return mock_client

    def test_parses_valid_json_response(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000)")
        payload = json.dumps({
            "passed": True, "critical_count": 0, "warning_count": 1,
            "summary": "Looks good with minor warnings",
            "issues": [{"severity": "warning", "file": "views/home.ejs", "issue": "Missing aria-label"}],
        })
        with patch("review._create_client", return_value=self._mock_client(payload)):
            result = review.review_build(str(tmp_path), self._spec())

        assert result["passed"] is True
        assert result["critical_count"] == 0
        assert result["warning_count"] == 1
        assert len(result["issues"]) == 1

    def test_returns_skipped_on_json_parse_error(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000)")
        with patch("review._create_client", return_value=self._mock_client("not valid json")):
            result = review.review_build(str(tmp_path), self._spec())

        assert result["passed"] is True
        assert result.get("skipped") is True

    def test_strips_markdown_fences_from_response(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000)")
        payload = json.dumps({"passed": False, "critical_count": 1, "warning_count": 0,
                               "summary": "Missing routes", "issues": []})
        with patch("review._create_client", return_value=self._mock_client(f"```json\n{payload}\n```")):
            result = review.review_build(str(tmp_path), self._spec())

        assert result["passed"] is False
        assert result["critical_count"] == 1

    def test_returns_skipped_on_api_error(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000)")
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("network error")
        with patch("review._create_client", return_value=mock_client):
            result = review.review_build(str(tmp_path), self._spec())

        assert result["passed"] is True
        assert result.get("skipped") is True


# ---------------------------------------------------------------------------
# _skipped helper
# ---------------------------------------------------------------------------

class TestSkipped:
    def test_skipped_always_passes(self):
        result = review._skipped("test reason")
        assert result["passed"] is True
        assert result["skipped"] is True
        assert result["critical_count"] == 0
        assert "test reason" in result["summary"]
