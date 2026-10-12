# tests/test_intake.py
#
# Covers gaps in cli/intake.py not already tested elsewhere.
#
# run_conversation() (interactive loop, CLI-only) is exercised here only for
# its design enrichment of a finished spec (TestUupmEnrichment).
#
# Already tested elsewhere (skipped here):
#   - create_client() (requires ANTHROPIC_API_KEY, tested via api_client tests)
#   - call_api_with_retry() (delegates to api_client.call_api; covered by test_api_client.py)

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

# cli/ is not a proper package so add it to sys.path
_CLI_DIR = str(Path(__file__).parent.parent / "cli")
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

import intake  # noqa: E402 (after sys.path manipulation)
from intake import extract_spec_json, save_spec  # noqa: E402
from tests._intake_driver import spec_saved_by_a_conversation  # noqa: E402


# ---------------------------------------------------------------------------
# TestExtractSpecJson
# ---------------------------------------------------------------------------

class TestExtractSpecJson:
    """extract_spec_json() handles all three extraction strategies."""

    def test_extracts_from_fenced_json_block(self):
        text = 'Some preamble\n```json\n{"app_name": "my-app", "spec_version": "0.1"}\n```'
        result = extract_spec_json(text)
        assert result == {"app_name": "my-app", "spec_version": "0.1"}

    def test_extracts_from_plain_fenced_block(self):
        text = 'Here is your spec:\n```\n{"app_name": "test-app"}\n```'
        result = extract_spec_json(text)
        assert result == {"app_name": "test-app"}

    def test_extracts_from_bare_braces(self):
        text = 'Here is the spec: {"app_name": "bare-app", "x": 1}'
        result = extract_spec_json(text)
        assert result == {"app_name": "bare-app", "x": 1}

    def test_returns_none_when_no_json(self):
        result = extract_spec_json("No JSON here at all.")
        assert result is None

    def test_returns_none_for_empty_string(self):
        result = extract_spec_json("")
        assert result is None

    def test_json_block_takes_priority_over_bare_braces(self):
        text = '{"wrong": true}\n```json\n{"right": true}\n```'
        result = extract_spec_json(text)
        assert result == {"right": True}

    def test_multiline_json_extracted_correctly(self):
        spec = {
            "spec_version": "0.1",
            "app_name": "yoga-studio",
            "description": "A booking system",
            "user_roles": [{"role": "admin", "description": "Manages", "can": ["all"]}],
        }
        text = f"```json\n{json.dumps(spec, indent=2)}\n```"
        result = extract_spec_json(text)
        assert result["app_name"] == "yoga-studio"
        assert result["user_roles"][0]["role"] == "admin"

    def test_invalid_json_in_fenced_block_raises(self):
        text = "```json\n{invalid json\n```"
        with pytest.raises(json.JSONDecodeError):
            extract_spec_json(text)

    def test_plain_fence_with_non_object_content_not_extracted(self):
        """Plain ``` fence whose content doesn't start with { → falls through to brace scan."""
        text = "```\nsome plain text\n```\n{\"fallback\": true}"
        result = extract_spec_json(text)
        # Falls back to brace scan for the trailing {"fallback": true}
        assert result == {"fallback": True}

    def test_rfind_picks_last_closing_brace(self):
        """Bare-brace path uses rfind('}') so nested objects are extracted correctly."""
        spec = {"a": {"b": {"c": 1}}, "d": 2}
        text = f"prefix {json.dumps(spec)} suffix"
        result = extract_spec_json(text)
        assert result == spec


# ---------------------------------------------------------------------------
# TestSaveSpec
# ---------------------------------------------------------------------------

class TestSaveSpec:
    """save_spec() creates the file at the right path and returns it."""

    def test_saves_to_specs_dir(self, tmp_path):
        spec = {"app_name": "my-cool-app", "spec_version": "0.1"}
        with patch("intake.Path") as mock_path_cls:
            # Make Path("specs") return a real tmp_path subdir
            specs_dir = tmp_path / "specs"
            mock_path_cls.return_value = specs_dir
            # Restore Path behavior for open()
            with patch("intake.time") as mock_time:
                mock_time.time.return_value = 1234567890
                with patch("builtins.open", mock_open()) as mo:
                    result = save_spec(spec)

        # File name should include app_name and timestamp
        assert "my-cool-app" in str(result)
        assert "1234567890" in str(result)

    def test_fallback_app_name_used_when_missing(self, tmp_path, monkeypatch):
        """spec without app_name uses 'untitled-app' as the filename stem."""
        monkeypatch.chdir(tmp_path)
        spec = {"spec_version": "0.1"}  # no app_name
        result = save_spec(spec)
        assert "untitled-app" in result.name


# ---------------------------------------------------------------------------
# TestUupmEnrichment
# ---------------------------------------------------------------------------

class TestUupmEnrichment:
    """run_conversation() fills the unset fields of spec.design from the design
    context and leaves fields the customer set alone."""

    def test_enrichment_fills_missing_primary_color(self):
        """When spec.design is empty, the palette, fonts and style come from the design context."""
        spec = {"app_name": "pet-clinic", "design": {}}
        context = {
            "palette": {"primary": "#F97316", "secondary": "#EA580C", "accent": "#FED7AA"},
            "fonts": {"display": "Playfair Display", "body": "Source Sans 3"},
            "style": "warm earthy",
        }
        design = spec_saved_by_a_conversation(spec, context)["design"]
        assert design == {
            "primary_color": "#F97316", "secondary_color": "#EA580C", "accent_color": "#FED7AA",
            "font_display": "Playfair Display", "font_body": "Source Sans 3",
            "style": "warm earthy",
        }

    def test_enrichment_does_not_overwrite_explicit_color(self):
        """A primary_color the customer chose is kept; the unset style is still filled."""
        spec = {"app_name": "finance-app", "design": {"primary_color": "#1E3A5F"}}
        context = {"palette": {"primary": "#0891B2"}, "fonts": {}, "style": "corporate professional"}
        design = spec_saved_by_a_conversation(spec, context)["design"]
        assert design["primary_color"] == "#1E3A5F"
        assert design["style"] == "corporate professional"

    def test_enrichment_does_not_overwrite_explicit_style(self):
        spec = {"app_name": "dark-app", "design": {"style": "modern dark"}}
        context = {"palette": {}, "fonts": {}, "style": "warm earthy"}
        assert spec_saved_by_a_conversation(spec, context)["design"]["style"] == "modern dark"

    def test_enrichment_exception_is_swallowed(self):
        """A failing design context must not stop the spec from being saved, unchanged."""
        spec = {"app_name": "safe-app", "design": {}}
        saved = spec_saved_by_a_conversation(spec, context_error=RuntimeError("UUPM down"))
        assert saved["design"] == {}
        assert saved["app_name"] == "safe-app"


# ---------------------------------------------------------------------------
# TestValidationFeedbackLoop
# ---------------------------------------------------------------------------

class TestValidationFeedbackLoop:
    """validate_spec() integration in the extraction path."""

    def test_valid_spec_passes_validation(self):
        """A complete spec JSON should pass validate_spec()."""
        from validate import validate_spec

        spec = {
            "spec_version": "0.1",
            "app_name": "todo-app",
            "description": "A simple todo list app",
            "user_roles": [{"role": "user", "description": "Creates todos", "can": ["create"]}],
            "pages": [{"path": "/todos", "title": "Todos", "purpose": "List todos",
                       "auth_required": True, "allowed_roles": ["user"]}],
            "data_models": [{"name": "Todo", "fields": [
                {"name": "title", "type": "text", "required": True}
            ], "belongs_to": None, "has_many": None}],
            "actions": [],
            "design": {"primary_color": "#2563eb", "secondary_color": "#1e40af",
                       "accent_color": "#f59e0b", "style": "modern light"},
            "settings": {"auth": True, "seed_data": True},
        }
        errors = validate_spec(spec)
        assert errors == []

    def test_spec_with_missing_required_fields_fails_validation(self):
        from validate import validate_spec

        spec = {"spec_version": "0.1"}  # missing app_name, description, etc.
        errors = validate_spec(spec)
        assert len(errors) > 0
