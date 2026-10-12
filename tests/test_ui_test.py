"""Tests for cli/ui_test.py -- browser-based visual quality verification (Stage 3.75).

Tests cover:
- Luminance calculation (WCAG 2.0)
- CSS color parsing (rgb/rgba)
- Programmatic CSS checks via mocked Playwright page
- Vision score parsing from Claude response
- Default port constant
"""

import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

from ui_test import (
    _check_nav_dark_bg,
    _check_body_not_flat_white,
    _check_cards_border_radius,
    _check_buttons_visible_bg,
    _check_google_fonts,
    _check_viewport_meta,
    _check_text_contrast,
    _contrast_ratio,
    _luminance_from_rgb,
    _parse_rgb,
    _parse_vision_score,
    WCAG_AA_RATIO,
    UI_TEST_PORT,
)


# ---------------------------------------------------------------------------
# _luminance_from_rgb()
# ---------------------------------------------------------------------------

class TestLuminanceFromRgb:
    """Tests for WCAG 2.0 relative luminance calculation."""

    def test_black_luminance(self):
        """Black (0,0,0) has luminance 0.0."""
        assert _luminance_from_rgb(0, 0, 0) == 0.0

    def test_white_luminance(self):
        """White (255,255,255) has luminance 1.0."""
        result = _luminance_from_rgb(255, 255, 255)
        assert abs(result - 1.0) < 0.001

    def test_dark_color_low_luminance(self):
        """Dark slate (30, 41, 59) has very low luminance (< 0.05)."""
        result = _luminance_from_rgb(30, 41, 59)
        assert result < 0.05


# ---------------------------------------------------------------------------
# _parse_rgb()
# ---------------------------------------------------------------------------

class TestParseRgb:
    """Tests for CSS rgb/rgba string parsing."""

    def test_parses_rgb(self):
        """Parses standard rgb() format."""
        assert _parse_rgb("rgb(30, 41, 59)") == (30, 41, 59)

    def test_parses_rgba(self):
        """Parses rgba() format, ignoring alpha channel."""
        assert _parse_rgb("rgba(255, 255, 255, 1)") == (255, 255, 255)

    def test_returns_none_for_invalid(self):
        """Returns None for unparseable color strings."""
        assert _parse_rgb("not-a-color") is None

    def test_parses_rgb_no_spaces(self):
        """Parses rgb() without spaces between values."""
        assert _parse_rgb("rgb(30,41,59)") == (30, 41, 59)


# ---------------------------------------------------------------------------
# _check_nav_dark_bg()
# ---------------------------------------------------------------------------

class TestCheckNavDarkBg:
    """Tests for nav dark background check via mocked Playwright page."""

    def test_passes_with_dark_nav(self):
        """Nav with dark background (luminance < 0.3) passes."""
        page = MagicMock()
        page.evaluate.return_value = "rgb(30, 41, 59)"
        result = _check_nav_dark_bg(page)
        assert result["name"] == "nav_dark_bg"
        assert result["passed"] is True

    def test_fails_with_white_nav(self):
        """Nav with white background fails."""
        page = MagicMock()
        page.evaluate.return_value = "rgb(255, 255, 255)"
        result = _check_nav_dark_bg(page)
        assert result["name"] == "nav_dark_bg"
        assert result["passed"] is False

    def test_handles_no_nav_element(self):
        """When no nav element found, check fails with appropriate detail."""
        page = MagicMock()
        page.evaluate.return_value = None
        result = _check_nav_dark_bg(page)
        assert result["name"] == "nav_dark_bg"
        assert result["passed"] is False
        assert "no nav" in result["detail"].lower() or "not found" in result["detail"].lower()


# ---------------------------------------------------------------------------
# _check_body_not_flat_white()
# ---------------------------------------------------------------------------

class TestCheckBodyNotFlatWhite:
    """Tests for body background color check."""

    def test_passes_with_tinted_bg(self):
        """Body with a tinted background (slate-50 = rgb(248,250,252)) passes."""
        page = MagicMock()
        # First evaluate call returns body bg, second checks for gradient classes
        page.evaluate.side_effect = ["rgb(248, 250, 252)", False]
        result = _check_body_not_flat_white(page)
        assert result["name"] == "body_not_flat_white"
        assert result["passed"] is True

    def test_fails_with_pure_white(self):
        """Body with pure white background fails."""
        page = MagicMock()
        page.evaluate.side_effect = ["rgb(255, 255, 255)", False]
        result = _check_body_not_flat_white(page)
        assert result["name"] == "body_not_flat_white"
        assert result["passed"] is False

    def test_fails_with_near_white(self):
        """Body with near-white (249,250,251) fails."""
        page = MagicMock()
        page.evaluate.side_effect = ["rgb(249, 250, 251)", False]
        result = _check_body_not_flat_white(page)
        assert result["name"] == "body_not_flat_white"
        assert result["passed"] is False

    def test_passes_with_gradient_classes(self):
        """Body with gradient utility classes passes even if bg is white."""
        page = MagicMock()
        page.evaluate.side_effect = ["rgb(255, 255, 255)", True]
        result = _check_body_not_flat_white(page)
        assert result["name"] == "body_not_flat_white"
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# _check_cards_border_radius()
# ---------------------------------------------------------------------------

class TestCheckCardsBorderRadius:
    """Tests for card border-radius check."""

    def test_passes_with_rounded_cards(self):
        """Cards with border-radius 0.5rem pass."""
        page = MagicMock()
        page.evaluate.return_value = [{"borderRadius": "0.5rem", "tag": "div"}]
        result = _check_cards_border_radius(page)
        assert result["name"] == "cards_border_radius"
        assert result["passed"] is True

    def test_fails_with_sharp_cards(self):
        """Cards with border-radius 0px fail."""
        page = MagicMock()
        page.evaluate.return_value = [{"borderRadius": "0px", "tag": "div"}]
        result = _check_cards_border_radius(page)
        assert result["name"] == "cards_border_radius"
        assert result["passed"] is False

    def test_handles_no_cards(self):
        """When no shadow elements found, passes (nothing to check)."""
        page = MagicMock()
        page.evaluate.return_value = []
        result = _check_cards_border_radius(page)
        assert result["name"] == "cards_border_radius"
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# _check_buttons_visible_bg()
# ---------------------------------------------------------------------------

class TestCheckButtonsVisibleBg:
    """Tests for button background visibility check."""

    def test_passes_with_colored_bg(self):
        """Buttons with a solid color background pass."""
        page = MagicMock()
        page.evaluate.return_value = [
            {"bg": "rgb(37, 99, 235)", "text": "Submit", "tag": "button"}
        ]
        result = _check_buttons_visible_bg(page)
        assert result["name"] == "buttons_visible_bg"
        assert result["passed"] is True

    def test_fails_with_transparent_bg(self):
        """Buttons with transparent background fail."""
        page = MagicMock()
        page.evaluate.return_value = [
            {"bg": "rgba(0, 0, 0, 0)", "text": "Submit", "tag": "button"}
        ]
        result = _check_buttons_visible_bg(page)
        assert result["name"] == "buttons_visible_bg"
        assert result["passed"] is False

    def test_handles_no_buttons(self):
        """When no buttons found, passes (nothing to check)."""
        page = MagicMock()
        page.evaluate.return_value = []
        result = _check_buttons_visible_bg(page)
        assert result["name"] == "buttons_visible_bg"
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# _check_google_fonts()
# ---------------------------------------------------------------------------

class TestCheckGoogleFonts:
    """Tests for Google Fonts link presence."""

    def test_passes_with_fonts_link(self):
        """Page with Google Fonts link tag passes."""
        page = MagicMock()
        page.evaluate.return_value = True
        result = _check_google_fonts(page)
        assert result["name"] == "google_fonts"
        assert result["passed"] is True

    def test_fails_without_fonts_link(self):
        """Page without Google Fonts link fails."""
        page = MagicMock()
        page.evaluate.return_value = False
        result = _check_google_fonts(page)
        assert result["name"] == "google_fonts"
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# _check_viewport_meta()
# ---------------------------------------------------------------------------

class TestCheckViewportMeta:
    """Tests for viewport meta tag presence."""

    def test_passes_with_viewport(self):
        """Page with viewport meta tag passes."""
        page = MagicMock()
        page.evaluate.return_value = True
        result = _check_viewport_meta(page)
        assert result["name"] == "viewport_meta"
        assert result["passed"] is True

    def test_fails_without_viewport(self):
        """Page without viewport meta tag fails."""
        page = MagicMock()
        page.evaluate.return_value = False
        result = _check_viewport_meta(page)
        assert result["name"] == "viewport_meta"
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# _parse_vision_score()
# ---------------------------------------------------------------------------

class TestParseVisionScore:
    """Tests for parsing Claude Vision scoring response."""

    def test_parses_valid_json(self):
        """Parses a clean JSON response with score, strengths, weaknesses."""
        text = '{"score": 8.5, "strengths": ["good layout"], "weaknesses": ["minor spacing"]}'
        result = _parse_vision_score(text)
        assert result is not None
        assert result["score"] == 8.5
        assert "good layout" in result["strengths"]

    def test_parses_json_in_markdown(self):
        """Parses JSON wrapped in markdown code fences."""
        text = '```json\n{"score": 7, "strengths": ["clean"], "weaknesses": ["bland"]}\n```'
        result = _parse_vision_score(text)
        assert result is not None
        assert result["score"] == 7

    def test_returns_none_for_invalid(self):
        """Returns None for non-JSON response."""
        result = _parse_vision_score("not json at all")
        assert result is None

    def test_parses_json_with_extra_text(self):
        """Parses JSON even when surrounded by explanation text."""
        text = 'Here is my rating:\n{"score": 6, "strengths": ["ok"], "weaknesses": ["flat"]}\nHope that helps!'
        result = _parse_vision_score(text)
        assert result is not None
        assert result["score"] == 6


# ---------------------------------------------------------------------------
# UI_TEST_PORT constant
# ---------------------------------------------------------------------------

class TestDefaultPort:
    """Tests for the default UI test port constant."""

    def test_port_is_4569(self):
        """UI test module uses port 4569."""
        assert UI_TEST_PORT == 4569


# ---------------------------------------------------------------------------
# Section-06 of WATCHDOG1 Layer 2: cli/ui_test.py vision call migration
# ---------------------------------------------------------------------------

# Minimal real PNG bytes (1x1 transparent pixel) for vision payload tests.
# Tiny on-disk PNG so the base64 round-trip in _score_vision is exercised
# end-to-end without depending on Pillow or pulling stock photos.
_TINY_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDAT"
    b"x\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x01\x5c\xcd\xff\x69"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class TestUiTestApiClientMigration:
    """Verify cli/ui_test.py _score_vision routes through api_client.call_api
    while preserving the vision payload (base64 image + text block).
    """

    def test_ui_test_py_no_direct_messages_create(self):
        """cli/ui_test.py must contain no AST-level <obj>.messages.create(...) call.

        Docstrings that quote the historical pattern as documentation are
        fine; only actual call expressions are forbidden. The AST walk in
        tests/test_api_client.py::TestNoBypassStatic is the authoritative
        cross-file check; this test scopes the same rule to ui_test.py so a
        bisect points at the right file.
        """
        import ast
        ui_test_path = Path(__file__).parent.parent / "cli" / "ui_test.py"
        tree = ast.parse(ui_test_path.read_text())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "create":
                continue
            inner = func.value
            if isinstance(inner, ast.Attribute) and inner.attr == "messages":
                offenders.append(node.lineno)
        assert offenders == [], (
            f"cli/ui_test.py still contains direct .messages.create(...) calls "
            f"at lines {offenders}. Section-06 requires all chat calls go "
            f"through cli.api_client.call_api()."
        )

    def _make_screenshot(self, tmp_path):
        """Write a tiny real PNG to tmp_path and return the screenshots dict."""
        png_path = tmp_path / "dashboard.png"
        png_path.write_bytes(_TINY_PNG_BYTES)
        return {"dashboard": str(png_path)}

    def _fake_response_text(self, text):
        from types import SimpleNamespace
        return SimpleNamespace(content=[SimpleNamespace(text=text, type="text")])

    def test_score_vision_calls_call_api_once(self, monkeypatch, tmp_path):
        """_score_vision invokes api_client.call_api exactly once."""
        from types import SimpleNamespace
        import ui_test as ui_test_mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        monkeypatch.setattr(ui_test_mod, "make_client", lambda **kw: MagicMock())

        fake = self._fake_response_text(
            '{"score": 7, "strengths": ["clean"], "weaknesses": ["flat"]}'
        )
        mock_call_api = MagicMock(return_value=(fake, SimpleNamespace()))
        monkeypatch.setattr(ui_test_mod, "call_api", mock_call_api)

        screenshots = self._make_screenshot(tmp_path)
        spec = {"app_name": "test-app"}
        ui_test_mod._score_vision(screenshots, spec, "b-vision-1")

        assert mock_call_api.call_count == 1

    def test_vision_messages_list_forwarded_unchanged(self, monkeypatch, tmp_path):
        """The base64 image block + text block must reach call_api unchanged."""
        import base64
        from types import SimpleNamespace
        import ui_test as ui_test_mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        monkeypatch.setattr(ui_test_mod, "make_client", lambda **kw: MagicMock())

        fake = self._fake_response_text('{"score": 8}')
        mock_call_api = MagicMock(return_value=(fake, SimpleNamespace()))
        monkeypatch.setattr(ui_test_mod, "call_api", mock_call_api)

        screenshots = self._make_screenshot(tmp_path)
        spec = {"app_name": "yoga-studio", "design": {"style": "warm earthy"}}
        ui_test_mod._score_vision(screenshots, spec)

        args, kwargs = mock_call_api.call_args
        # Positional: client, messages, model
        client_arg, messages_arg, model_arg = args[0], args[1], args[2]
        assert model_arg == ui_test_mod.VISION_MODEL
        assert isinstance(messages_arg, list) and len(messages_arg) == 1
        msg = messages_arg[0]
        assert msg["role"] == "user"
        content = msg["content"]
        assert isinstance(content, list) and len(content) == 2

        # Image block: base64 of the tmp PNG, media_type image/png
        img_block = content[0]
        assert img_block["type"] == "image"
        source = img_block["source"]
        assert source["type"] == "base64"
        assert source["media_type"] == "image/png"
        expected_b64 = base64.b64encode(_TINY_PNG_BYTES).decode("utf-8")
        assert source["data"] == expected_b64

        # Text block: includes app name and style hint, asks for JSON rating
        text_block = content[1]
        assert text_block["type"] == "text"
        assert "yoga-studio" in text_block["text"]
        assert "warm earthy" in text_block["text"]
        assert "score" in text_block["text"]

        # No system prompt for vision call
        assert kwargs.get("system") is None
        assert kwargs.get("max_tokens") == 1024

    def test_score_vision_parses_combined_response(self, monkeypatch, tmp_path):
        """_score_vision parses CombinedResponse-shaped objects (.content[0].text)
        with the same semantics as before migration.
        """
        from types import SimpleNamespace
        import ui_test as ui_test_mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        monkeypatch.setattr(ui_test_mod, "make_client", lambda **kw: MagicMock())

        fake = self._fake_response_text(
            '{"score": 8.5, "strengths": ["good color"], "weaknesses": ["weak hierarchy"]}'
        )
        monkeypatch.setattr(
            ui_test_mod, "call_api",
            MagicMock(return_value=(fake, SimpleNamespace())),
        )

        screenshots = self._make_screenshot(tmp_path)
        result = ui_test_mod._score_vision(screenshots, {"app_name": "x"})

        assert result["vision_score"] == 8.5
        assert result["vision_details"]["strengths"] == ["good color"]
        assert result["vision_details"]["weaknesses"] == ["weak hierarchy"]

    def test_the_cancel_key_reaches_the_model_call(self, monkeypatch, tmp_path):
        """The attempt's cancel key is what call_api gets as its build_id (it
        looks the cancel event up by it); without a key it gets ''. A
        build_id inside the spec is not a key."""
        from types import SimpleNamespace
        import ui_test as ui_test_mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        monkeypatch.setattr(ui_test_mod, "make_client", lambda **kw: MagicMock())

        fake = self._fake_response_text('{"score": 7}')
        mock_call_api = MagicMock(return_value=(fake, SimpleNamespace()))
        monkeypatch.setattr(ui_test_mod, "call_api", mock_call_api)

        screenshots = self._make_screenshot(tmp_path)

        ui_test_mod._score_vision(screenshots, {"app_name": "x"}, "b-vision")
        _, kwargs = mock_call_api.call_args
        assert kwargs.get("build_id") == "b-vision"

        # No key: "", even when the spec happens to carry a build_id.
        ui_test_mod._score_vision(screenshots, {"app_name": "x", "build_id": "in-the-spec"})
        _, kwargs = mock_call_api.call_args
        assert kwargs.get("build_id") == ""


# ---------------------------------------------------------------------------
# _contrast_ratio()
# ---------------------------------------------------------------------------

class TestContrastRatio:
    """Tests for WCAG 2.0 contrast ratio calculation."""

    def test_black_on_white_max_contrast(self):
        """Black text on white background has ~21:1 contrast."""
        l_black = _luminance_from_rgb(0, 0, 0)
        l_white = _luminance_from_rgb(255, 255, 255)
        ratio = _contrast_ratio(l_black, l_white)
        assert ratio > 20.0

    def test_same_color_ratio_is_one(self):
        """Identical foreground and background has 1:1 contrast."""
        lum = _luminance_from_rgb(128, 128, 128)
        ratio = _contrast_ratio(lum, lum)
        assert abs(ratio - 1.0) < 0.01

    def test_cream_on_cream_fails_aa(self):
        """Cream text (#f5f0e8) on cream background (#faf5ed) fails WCAG AA.

        This is the exact medicine-waves v2 failure: hero copy was invisible.
        """
        fg_lum = _luminance_from_rgb(0xF5, 0xF0, 0xE8)  # cream text
        bg_lum = _luminance_from_rgb(0xFA, 0xF5, 0xED)  # cream background
        ratio = _contrast_ratio(fg_lum, bg_lum)
        assert ratio < WCAG_AA_RATIO

    def test_dark_on_light_passes_aa(self):
        """Dark gray text on white background passes WCAG AA."""
        fg_lum = _luminance_from_rgb(51, 51, 51)   # #333333
        bg_lum = _luminance_from_rgb(255, 255, 255)
        ratio = _contrast_ratio(fg_lum, bg_lum)
        assert ratio >= WCAG_AA_RATIO

    def test_order_independent(self):
        """Contrast ratio is the same regardless of argument order."""
        l1 = _luminance_from_rgb(0, 0, 0)
        l2 = _luminance_from_rgb(255, 255, 255)
        assert _contrast_ratio(l1, l2) == _contrast_ratio(l2, l1)


# ---------------------------------------------------------------------------
# _check_text_contrast()
# ---------------------------------------------------------------------------

class TestCheckTextContrast:
    """Tests for rendered text contrast check via mocked Playwright page."""

    def test_cream_on_cream_hero_fails(self):
        """Cream-on-cream hero text (medicine-waves v2 scenario) fails."""
        page = MagicMock()
        page.evaluate.return_value = [
            {
                "selector": "h1",
                "fg": "rgb(245, 240, 232)",   # cream text
                "bg": "rgb(250, 245, 237)",   # cream background
                "tag": "h1",
            }
        ]
        result = _check_text_contrast(page)
        assert result["name"] == "text_contrast"
        assert result["passed"] is False
        assert "Low contrast" in result["detail"]

    def test_dark_text_on_light_bg_passes(self):
        """Dark text on light background passes WCAG AA."""
        page = MagicMock()
        page.evaluate.return_value = [
            {
                "selector": "h1",
                "fg": "rgb(30, 41, 59)",       # dark slate
                "bg": "rgb(255, 255, 255)",    # white
                "tag": "h1",
            },
            {
                "selector": "h2",
                "fg": "rgb(51, 51, 51)",       # dark gray
                "bg": "rgb(248, 250, 252)",    # light slate
                "tag": "h2",
            },
        ]
        result = _check_text_contrast(page)
        assert result["name"] == "text_contrast"
        assert result["passed"] is True

    def test_white_text_on_dark_bg_passes(self):
        """White text on dark background passes."""
        page = MagicMock()
        page.evaluate.return_value = [
            {
                "selector": "h1",
                "fg": "rgb(255, 255, 255)",
                "bg": "rgb(15, 23, 42)",       # slate-900
                "tag": "h1",
            },
        ]
        result = _check_text_contrast(page)
        assert result["passed"] is True

    def test_no_text_elements_passes(self):
        """When no text elements found, check passes (nothing to fail)."""
        page = MagicMock()
        page.evaluate.return_value = []
        result = _check_text_contrast(page)
        assert result["name"] == "text_contrast"
        assert result["passed"] is True

    def test_mixed_pass_fail_reports_failure(self):
        """If one element fails contrast, the whole check fails."""
        page = MagicMock()
        page.evaluate.return_value = [
            {
                "selector": "h1",
                "fg": "rgb(30, 41, 59)",       # dark, passes
                "bg": "rgb(255, 255, 255)",
                "tag": "h1",
            },
            {
                "selector": "h2",
                "fg": "rgb(200, 200, 200)",    # light gray on white, fails
                "bg": "rgb(255, 255, 255)",
                "tag": "h2",
            },
        ]
        result = _check_text_contrast(page)
        assert result["passed"] is False
        assert "h2" in result["detail"]

    def test_handles_evaluate_exception(self):
        """Playwright exceptions are caught and reported."""
        page = MagicMock()
        page.evaluate.side_effect = Exception("Page crashed")
        result = _check_text_contrast(page)
        assert result["name"] == "text_contrast"
        assert result["passed"] is False
        assert "Error" in result["detail"]

    def test_unparseable_colors_skipped(self):
        """Elements with unparseable colors are skipped, not failed."""
        page = MagicMock()
        page.evaluate.return_value = [
            {
                "selector": "h1",
                "fg": "not-a-color",
                "bg": "also-not-a-color",
                "tag": "h1",
            },
        ]
        result = _check_text_contrast(page)
        assert result["passed"] is True

    def test_text_contrast_in_check_weights(self):
        """text_contrast must be registered in CHECK_WEIGHTS."""
        from ui_test import CHECK_WEIGHTS
        assert "text_contrast" in CHECK_WEIGHTS
        assert CHECK_WEIGHTS["text_contrast"] == 2.0
