"""Tests for cli/design_review.py, design quality review agent (Stage 2.75)."""
import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)

import design_review


# ---------------------------------------------------------------------------
# _collect_design_files
# ---------------------------------------------------------------------------

class TestCollectDesignFiles:
    def test_collects_layout_and_views(self, tmp_path):
        views = tmp_path / "views"
        views.mkdir()
        (views / "layout.ejs").write_text("<html>layout</html>")
        (views / "login.ejs").write_text("<form>login</form>")
        (views / "dashboard.ejs").write_text("<h1>Dashboard</h1>")
        files = design_review._collect_design_files(tmp_path)
        paths = [f[0] for f in files]
        assert "views/layout.ejs" in paths
        assert "views/login.ejs" in paths
        assert "views/dashboard.ejs" in paths

    def test_collects_css_files(self, tmp_path):
        public = tmp_path / "public"
        public.mkdir()
        (public / "style.css").write_text("body { color: red; }")
        files = design_review._collect_design_files(tmp_path)
        paths = [f[0] for f in files]
        assert "public/style.css" in paths

    def test_skips_db_and_route_logic(self, tmp_path):
        (tmp_path / "db").mkdir()
        (tmp_path / "db" / "schema.sql").write_text("CREATE TABLE x;")
        (tmp_path / "routes").mkdir()
        (tmp_path / "routes" / "auth.js").write_text("router.get('/')")
        (tmp_path / "server.js").write_text("app.listen(3000)")
        files = design_review._collect_design_files(tmp_path)
        paths = [f[0] for f in files]
        assert "db/schema.sql" not in paths
        assert "routes/auth.js" not in paths
        assert "server.js" in paths

    def test_respects_80kb_limit(self, tmp_path):
        views = tmp_path / "views"
        views.mkdir()
        big_content = "x" * (design_review._MAX_CONTEXT_CHARS + 5000)
        (views / "layout.ejs").write_text(big_content)
        # Should not raise; gracefully handle oversized files
        files = design_review._collect_design_files(tmp_path)
        assert isinstance(files, list)


# ---------------------------------------------------------------------------
# _build_design_review_prompt
# ---------------------------------------------------------------------------

class TestBuildDesignReviewPrompt:
    def _spec(self, **overrides):
        spec = {
            "app_name": "test-app",
            "description": "A test web app",
            "design": {
                "primary_color": "#2563EB",
                "secondary_color": "#3B82F6",
                "accent_color": "#EA580C",
                "font_display": "DM Sans",
                "font_body": "Source Sans 3",
                "style": "modern light",
            },
            "pages": [
                {"name": "Dashboard", "path": "/dashboard", "auth_required": True},
            ],
            "data_models": [
                {"name": "Widget", "fields": []},
            ],
        }
        spec.update(overrides)
        return spec

    def test_includes_visual_design_checklist(self):
        prompt = design_review._build_design_review_prompt(self._spec(), [])
        assert "split-panel" in prompt
        assert "Page backgrounds" in prompt
        assert "Nav bar" in prompt

    def test_includes_style_visual_hooks(self):
        spec = self._spec()
        spec["design"]["style"] = "warm earthy"
        prompt = design_review._build_design_review_prompt(spec, [])
        assert "card-premium" in prompt

    def test_includes_ux_rules(self):
        prompt = design_review._build_design_review_prompt(self._spec(), [])
        assert "Sticky glassmorphism nav" in prompt

    def test_includes_spec_colors(self):
        spec = self._spec()
        spec["design"]["primary_color"] = "#B45309"
        prompt = design_review._build_design_review_prompt(spec, [])
        assert "#B45309" in prompt

    def test_includes_logo_checks_when_logo_present(self):
        spec = self._spec(logo_data="data:image/png;base64,abc123")
        prompt = design_review._build_design_review_prompt(spec, [])
        assert "logo" in prompt.lower()

    def test_excludes_logo_checks_when_no_logo(self):
        spec = self._spec()
        # Ensure no logo_data key
        spec.pop("logo_data", None)
        prompt = design_review._build_design_review_prompt(spec, [])
        # The logo-specific review section should not be present
        assert "logo placement" not in prompt.lower()

    def test_prompt_calls_out_critical_contrast_check(self):
        prompt = design_review._build_design_review_prompt(self._spec(), [])
        # The "What to check" section must include the explicit critical-contrast item
        # so Claude scans for cream-on-cream / dark-on-dark hero text.
        assert "CRITICAL CONTRAST" in prompt
        # And it should reference the specific Tailwind cream/beige class names that
        # caused the MW v2 invisible-hero failure.
        assert "bg-amber-50" in prompt
        assert "text-amber-50" in prompt or "text-cream" in prompt or "text-white" in prompt


# ---------------------------------------------------------------------------
# DESIGN_REVIEW_SYSTEM_PROMPT: 5th Critical rule (light-on-light hero text)
# ---------------------------------------------------------------------------

class TestSystemPromptCriticalRules:
    def test_system_prompt_lists_light_on_light_critical(self):
        sys_prompt = design_review.DESIGN_REVIEW_SYSTEM_PROMPT
        # The MW v2 regression fix added a 5th Critical bullet for light-on-light
        # hero text. It must mention the specific failure mode (light text colors
        # on cream/beige backgrounds) so Claude flags it as critical, not warning.
        assert "light-on-light" in sys_prompt or "light on light" in sys_prompt
        assert "bg-amber-50" in sys_prompt
        # Should reference at least one of the light text classes that triggered the bug
        assert any(
            cls in sys_prompt
            for cls in ("text-white", "text-cream", "text-ivory", "text-amber-50")
        )

    def test_system_prompt_still_lists_original_four_criticals(self):
        sys_prompt = design_review.DESIGN_REVIEW_SYSTEM_PROMPT
        # Regression guard: don't lose the original 4 critical rules when
        # adding the 5th. Each rule should still be present in some form.
        assert "split-panel" in sys_prompt  # login/register
        assert "Nav bar" in sys_prompt  # nav surface
        assert "flat white" in sys_prompt or "gradient" in sys_prompt  # backgrounds
        assert "CSS custom properties" in sys_prompt or "Tailwind config" in sys_prompt


# ---------------------------------------------------------------------------
# _KEY_FILES expansion: must include views Claude was missing
# ---------------------------------------------------------------------------

class TestKeyFilesExpansion:
    def test_key_files_includes_critical_landing_views(self):
        # MW v2 design review missed the homepage hero entirely because
        # the alphabetical rglob exhausted the 80k char budget on admin/*
        # views. The fix preloads index/contact/events before that walk.
        assert "views/index.ejs" in design_review._KEY_FILES
        assert "views/contact.ejs" in design_review._KEY_FILES
        assert "views/events.ejs" in design_review._KEY_FILES

    def test_key_files_includes_tailwind_config(self):
        # tailwind.config.js drives the actual color/font tokens; the reviewer
        # needs to see it to validate the spec palette is wired up.
        assert "tailwind.config.js" in design_review._KEY_FILES

    def test_key_files_preserves_original_priority_files(self):
        # Regression guard for the original 5 priority files.
        for required in ("server.js", "views/layout.ejs", "views/login.ejs", "views/register.ejs"):
            assert required in design_review._KEY_FILES


# ---------------------------------------------------------------------------
# review_design
# ---------------------------------------------------------------------------

class TestReviewDesign:
    def _spec(self):
        return {
            "app_name": "test-app",
            "description": "test",
            "design": {
                "primary_color": "#2563EB",
                "style": "modern light",
            },
            "pages": [],
            "data_models": [],
        }

    def _mock_response(self, text):
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text=text)]
        return mock_resp

    def _mock_metrics(self, cost=0.05):
        metrics = MagicMock()
        metrics.estimated_cost_usd = cost
        return metrics

    def test_returns_structured_json_for_passing_build(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000)")
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html></html>")
        payload = json.dumps({
            "passed": True, "score": 8, "critical_count": 0, "warning_count": 1,
            "issues": [{"severity": "warning", "category": "colors", "file": "views/layout.ejs",
                        "issue": "Low contrast on muted text", "suggestion": "Use darker shade"}],
        })
        with patch("design_review._create_client"), \
             patch("design_review._shared_call_api",
                   return_value=(self._mock_response(payload), self._mock_metrics())):
            result = design_review.review_design(str(tmp_path), self._spec())

        assert result["passed"] is True
        assert result["score"] == 8
        assert result["critical_count"] == 0
        assert result["warning_count"] == 1
        assert len(result["issues"]) == 1

    def test_returns_structured_json_for_failing_build(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000)")
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html></html>")
        issues = [
            {"severity": "critical", "category": "layout", "file": "views/login.ejs",
             "issue": f"Missing split-panel layout #{i}", "suggestion": "Add split-panel"}
            for i in range(3)
        ]
        payload = json.dumps({
            "passed": False, "score": 3, "critical_count": 3, "warning_count": 0,
            "issues": issues,
        })
        with patch("design_review._create_client"), \
             patch("design_review._shared_call_api",
                   return_value=(self._mock_response(payload), self._mock_metrics())):
            result = design_review.review_design(str(tmp_path), self._spec())

        assert result["passed"] is False
        assert result["critical_count"] == 3

    def test_handles_api_failure_gracefully(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000)")
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html></html>")
        with patch("design_review._create_client"), \
             patch("design_review._shared_call_api",
                   side_effect=ConnectionError("network failure")):
            result = design_review.review_design(str(tmp_path), self._spec())

        assert result["passed"] is True
        assert result.get("skipped") is True

    def test_handles_unparseable_response(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000)")
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html></html>")
        with patch("design_review._create_client"), \
             patch("design_review._shared_call_api",
                   return_value=(self._mock_response("not valid json"), self._mock_metrics())):
            result = design_review.review_design(str(tmp_path), self._spec())

        assert result["passed"] is True
        assert result.get("skipped") is True

    def test_uses_sonnet_model(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000)")
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html></html>")
        payload = json.dumps({
            "passed": True, "score": 7, "critical_count": 0, "warning_count": 0,
            "issues": [],
        })
        captured = {}

        def capture_call_api(**kwargs):
            captured.update(kwargs)
            return (self._mock_response(payload), self._mock_metrics())

        with patch("design_review._create_client"), \
             patch("design_review._shared_call_api", side_effect=capture_call_api):
            design_review.review_design(str(tmp_path), self._spec())

        assert "opus" in captured.get("model", "").lower()

    def test_returns_cost_metadata(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000)")
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html></html>")
        payload = json.dumps({
            "passed": True, "score": 9, "critical_count": 0, "warning_count": 0,
            "issues": [],
        })
        with patch("design_review._create_client"), \
             patch("design_review._shared_call_api",
                   return_value=(self._mock_response(payload), self._mock_metrics(0.03))):
            result = design_review.review_design(str(tmp_path), self._spec())

        assert "api_cost_usd" in result
        assert result["api_cost_usd"] == 0.03


# ---------------------------------------------------------------------------
# Observability (Slice B): confidence default, model attribution, trace events
# ---------------------------------------------------------------------------

class TestObservability:
    """Regression tests for the Slice B observability additions."""

    def _spec(self):
        return {
            "app_name": "test-app",
            "description": "test",
            "design": {"primary_color": "#2563EB", "style": "modern light"},
            "pages": [],
            "data_models": [],
        }

    def _mock_response(self, text):
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text=text)]
        return mock_resp

    def _mock_metrics(self, cost=0.05):
        metrics = MagicMock()
        metrics.estimated_cost_usd = cost
        return metrics

    def _setup_dir(self, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000)")
        (tmp_path / "views").mkdir()
        (tmp_path / "views" / "layout.ejs").write_text("<html></html>")

    def test_system_prompt_declares_confidence_field(self):
        # The schema in the system prompt must document the new confidence field.
        assert '"confidence"' in design_review.DESIGN_REVIEW_SYSTEM_PROMPT
        assert "Confidence guide" in design_review.DESIGN_REVIEW_SYSTEM_PROMPT

    def test_confidence_defaults_to_3_when_model_omits_it(self, tmp_path):
        self._setup_dir(tmp_path)
        # Model response intentionally omits confidence: older responses may
        # not include the new field until rollouts settle.
        payload = json.dumps({
            "passed": True, "score": 8, "critical_count": 0, "warning_count": 0,
            "issues": [],
        })
        with patch("design_review._create_client"), \
             patch("design_review._shared_call_api",
                   return_value=(self._mock_response(payload), self._mock_metrics())):
            result = design_review.review_design(str(tmp_path), self._spec())

        assert result.get("confidence") == 3

    def test_confidence_preserved_when_model_provides_it(self, tmp_path):
        self._setup_dir(tmp_path)
        payload = json.dumps({
            "passed": True, "score": 8, "confidence": 5,
            "critical_count": 0, "warning_count": 0, "issues": [],
        })
        with patch("design_review._create_client"), \
             patch("design_review._shared_call_api",
                   return_value=(self._mock_response(payload), self._mock_metrics())):
            result = design_review.review_design(str(tmp_path), self._spec())

        assert result["confidence"] == 5

    def test_result_includes_model_attribution(self, tmp_path):
        self._setup_dir(tmp_path)
        payload = json.dumps({
            "passed": True, "score": 8, "confidence": 4,
            "critical_count": 0, "warning_count": 0, "issues": [],
        })
        with patch("design_review._create_client"), \
             patch("design_review._shared_call_api",
                   return_value=(self._mock_response(payload), self._mock_metrics())):
            result = design_review.review_design(str(tmp_path), self._spec())

        assert result.get("model") == design_review.MODEL

    def test_emits_completed_trace_event_on_success(self, tmp_path):
        self._setup_dir(tmp_path)
        payload = json.dumps({
            "passed": True, "score": 9, "confidence": 4,
            "critical_count": 0, "warning_count": 1, "issues": [],
        })
        with patch("design_review._create_client"), \
             patch("design_review._shared_call_api",
                   return_value=(self._mock_response(payload), self._mock_metrics())), \
             patch("design_review.emit_build_trace") as mock_emit:
            design_review.review_design(str(tmp_path), self._spec())

        event_names = [call.args[1] for call in mock_emit.call_args_list]
        assert "design_review.start" in event_names
        assert "design_review.completed" in event_names
        # Pull the completed payload for deeper inspection.
        completed = [
            call for call in mock_emit.call_args_list
            if call.args[1] == "design_review.completed"
        ]
        assert len(completed) == 1
        payload_arg = completed[0].args[2]
        assert payload_arg["passed"] is True
        assert payload_arg["score"] == 9
        assert payload_arg["confidence"] == 4
        assert payload_arg["model"] == design_review.MODEL

    def test_emits_skipped_trace_event_on_missing_build_dir(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        with patch("design_review.emit_build_trace") as mock_emit:
            result = design_review.review_design(str(missing), self._spec())

        assert result.get("skipped") is True
        skipped_calls = [
            call for call in mock_emit.call_args_list
            if call.args[1] == "design_review.skipped"
        ]
        assert len(skipped_calls) == 1
        assert skipped_calls[0].args[2]["reason"] == "build_dir_not_found"

    def test_emits_skipped_trace_event_on_no_design_files(self, tmp_path):
        # Empty build dir: no server.js, no views, no public.
        with patch("design_review.emit_build_trace") as mock_emit:
            result = design_review.review_design(str(tmp_path), self._spec())

        assert result.get("skipped") is True
        skipped_calls = [
            call for call in mock_emit.call_args_list
            if call.args[1] == "design_review.skipped"
        ]
        assert len(skipped_calls) == 1
        assert skipped_calls[0].args[2]["reason"] == "no_design_files"

    def test_emits_skipped_trace_event_on_json_parse_error(self, tmp_path):
        self._setup_dir(tmp_path)
        with patch("design_review._create_client"), \
             patch("design_review._shared_call_api",
                   return_value=(self._mock_response("not valid json"), self._mock_metrics())), \
             patch("design_review.emit_build_trace") as mock_emit:
            design_review.review_design(str(tmp_path), self._spec())

        skipped_calls = [
            call for call in mock_emit.call_args_list
            if call.args[1] == "design_review.skipped"
        ]
        assert len(skipped_calls) == 1
        assert skipped_calls[0].args[2]["reason"] == "json_parse_error"

    def test_emits_skipped_trace_event_on_unexpected_error(self, tmp_path):
        self._setup_dir(tmp_path)
        with patch("design_review._create_client"), \
             patch("design_review._shared_call_api",
                   side_effect=ConnectionError("network down")), \
             patch("design_review.emit_build_trace") as mock_emit:
            design_review.review_design(str(tmp_path), self._spec())

        skipped_calls = [
            call for call in mock_emit.call_args_list
            if call.args[1] == "design_review.skipped"
        ]
        assert len(skipped_calls) == 1
        assert skipped_calls[0].args[2]["reason"] == "unexpected_error"


# ---------------------------------------------------------------------------
# _skipped helper
# ---------------------------------------------------------------------------

class TestSkipped:
    def test_skipped_always_passes(self):
        result = design_review._skipped("test reason")
        assert result["passed"] is True
        assert result["skipped"] is True
        assert result["critical_count"] == 0
        assert result["score"] == 0
        assert "test reason" in result["summary"]
