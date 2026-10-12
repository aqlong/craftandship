"""Tests for cli/design_data.py, UUPM design data module."""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cli'))

from design_data import (
    BANNED_FONTS, VALID_STYLES, COLOR_PALETTES, FONT_PAIRINGS,
    PRODUCT_RECOMMENDATIONS, PRODUCT_KEYWORDS,
    VISUAL_DESIGN_CHECKLIST, STYLE_VISUAL_HOOKS,
    STYLE_TYPOGRAPHY_SCALE, STYLE_DONT_PATTERNS, STYLE_COMPONENT_TOKENS,
    STYLE_LAYOUT_PRINCIPLES, STYLE_RESPONSIVE_NOTES,
    generate_design_md,
    _hex_to_luminance, _contrast_ratio, _ensure_wcag,
    _hex_luminance, _are_generic_dark_colors,
    match_product_type, get_design_context,
)


# --- WCAG AA compliance ---

class TestWCAGCompliance:
    """Every palette must pass WCAG AA 4.5:1 for primary/on_primary."""

    @pytest.mark.parametrize("name,palette", list(COLOR_PALETTES.items()))
    def test_palette_wcag_aa(self, name, palette):
        ratio = _contrast_ratio(palette["primary"], palette["on_primary"])
        assert ratio >= 4.5, (
            f"Palette '{name}': primary {palette['primary']} vs on_primary "
            f"{palette['on_primary']} has contrast {ratio:.2f} < 4.5"
        )

    @pytest.mark.parametrize("name,palette", list(COLOR_PALETTES.items()))
    def test_palette_has_required_keys(self, name, palette):
        required = {"primary", "on_primary", "secondary", "accent", "background", "foreground", "card", "muted", "border"}
        missing = required - set(palette.keys())
        assert not missing, f"Palette '{name}' missing keys: {missing}"


# --- Banned fonts ---

class TestBannedFonts:
    """No banned fonts in any FONT_PAIRINGS entry."""

    @pytest.mark.parametrize("name,pairing", list(FONT_PAIRINGS.items()))
    def test_display_not_banned(self, name, pairing):
        assert pairing["display"] not in BANNED_FONTS, (
            f"Pairing '{name}': display font '{pairing['display']}' is banned"
        )

    @pytest.mark.parametrize("name,pairing", list(FONT_PAIRINGS.items()))
    def test_body_not_banned(self, name, pairing):
        assert pairing["body"] not in BANNED_FONTS, (
            f"Pairing '{name}': body font '{pairing['body']}' is banned"
        )

    @pytest.mark.parametrize("name,pairing", list(FONT_PAIRINGS.items()))
    def test_pairing_has_required_keys(self, name, pairing):
        required = {"display", "body", "google_fonts_url", "tailwind_config"}
        missing = required - set(pairing.keys())
        assert not missing, f"Pairing '{name}' missing keys: {missing}"


# --- Product recommendations ---

class TestProductRecommendations:
    """Style values must be in the 6-item validated enum."""

    @pytest.mark.parametrize("product,rec", list(PRODUCT_RECOMMENDATIONS.items()))
    def test_style_in_valid_enum(self, product, rec):
        assert rec["style"] in VALID_STYLES, (
            f"Product '{product}': style '{rec['style']}' not in valid enum"
        )

    @pytest.mark.parametrize("product,rec", list(PRODUCT_RECOMMENDATIONS.items()))
    def test_fonts_key_exists(self, product, rec):
        assert rec["fonts"] in FONT_PAIRINGS, (
            f"Product '{product}': fonts '{rec['fonts']}' not in FONT_PAIRINGS"
        )

    @pytest.mark.parametrize("product,rec", list(PRODUCT_RECOMMENDATIONS.items()))
    def test_product_has_palette(self, product, rec):
        assert product in COLOR_PALETTES, (
            f"Product '{product}' in recommendations but not in COLOR_PALETTES"
        )


# --- Keyword matching ---

class TestMatchProductType:
    def test_pet_grooming(self):
        assert match_product_type("A pet grooming appointment scheduler") == "pet_services"

    def test_invoice_tracking(self):
        assert match_product_type("A freelancer invoice tracking app") == "invoice"

    def test_freelancer_platform(self):
        assert match_product_type("A freelance project management platform") == "freelancer"

    def test_restaurant(self):
        assert match_product_type("Online restaurant reservation system") == "restaurant"

    def test_gym(self):
        assert match_product_type("Gym membership management app") == "fitness"

    def test_healthcare(self):
        assert match_product_type("Patient health records portal") == "healthcare"

    def test_ecommerce(self):
        assert match_product_type("Online shop for handmade crafts") == "ecommerce"

    def test_real_estate(self):
        assert match_product_type("Real estate property listing platform") == "real_estate"

    def test_education(self):
        assert match_product_type("Online school management system") == "education"

    def test_saas(self):
        assert match_product_type("SaaS analytics dashboard") == "saas"

    def test_no_match(self):
        assert match_product_type("xyzzy qwert zxcvb") is None

    def test_empty_string(self):
        assert match_product_type("") is None

    def test_none_input(self):
        assert match_product_type(None) is None


# --- WCAG utility functions ---

class TestWCAGUtilities:
    def test_contrast_black_white(self):
        ratio = _contrast_ratio("#000000", "#FFFFFF")
        assert ratio == pytest.approx(21.0, abs=0.1)

    def test_contrast_same_color(self):
        ratio = _contrast_ratio("#2563EB", "#2563EB")
        assert ratio == pytest.approx(1.0, abs=0.01)

    def test_ensure_wcag_passes(self):
        result = _ensure_wcag("#2563EB", "#FFFFFF")
        assert result == "#FFFFFF"

    def test_ensure_wcag_corrects_low_contrast(self):
        result = _ensure_wcag("#F97316", "#FFFFFF")
        assert result in ("#0F172A", "#FFFFFF")
        assert _contrast_ratio("#F97316", result) >= 4.5

    def test_ensure_wcag_fallback_to_dark(self):
        result = _ensure_wcag("#93C5FD", "#FFFFFF")
        assert result == "#0F172A"

    def test_luminance_black(self):
        assert _hex_to_luminance("#000000") == pytest.approx(0.0, abs=0.001)

    def test_luminance_white(self):
        assert _hex_to_luminance("#FFFFFF") == pytest.approx(1.0, abs=0.001)


# --- get_design_context ---

class TestGetDesignContext:
    def test_returns_uupm_defaults_from_description(self):
        spec = {"description": "A pet grooming appointment scheduler"}
        ctx = get_design_context(spec)
        assert ctx["product_type"] == "pet_services"
        assert ctx["palette"]["primary"] == "#F97316"
        assert ctx["fonts"]["display"] == "Varela Round"

    def test_customer_colors_override(self):
        spec = {
            "description": "A pet grooming app",
            "design": {"primary_color": "#FF0000"},
        }
        ctx = get_design_context(spec)
        assert ctx["palette"]["primary"] == "#FF0000"

    def test_customer_font_override(self):
        spec = {
            "description": "A SaaS dashboard",
            "design": {"font_display": "Montserrat"},
        }
        ctx = get_design_context(spec)
        assert ctx["fonts"]["display"] == "Montserrat"

    def test_banned_font_ignored(self):
        spec = {
            "description": "A SaaS dashboard",
            "design": {"font_display": "Inter"},
        }
        ctx = get_design_context(spec)
        assert ctx["fonts"]["display"] != "Inter"

    def test_no_match_returns_empty_palette(self):
        spec = {"description": "xyzzy qwert zxcvb"}
        ctx = get_design_context(spec)
        assert ctx["product_type"] is None
        assert ctx["palette"] == {}

    def test_no_description_returns_empty(self):
        spec = {}
        ctx = get_design_context(spec)
        assert ctx["product_type"] is None

    def test_none_design_field(self):
        spec = {"description": "A pet app", "design": None}
        ctx = get_design_context(spec)
        assert ctx["product_type"] == "pet_services"

    def test_wcag_enforced_after_override(self):
        spec = {
            "description": "A SaaS dashboard",
            "design": {"primary_color": "#93C5FD"},
        }
        ctx = get_design_context(spec)
        if ctx["palette"].get("on_primary"):
            ratio = _contrast_ratio(ctx["palette"]["primary"], ctx["palette"]["on_primary"])
            assert ratio >= 4.5

    def test_style_from_recommendation(self):
        spec = {"description": "A restaurant reservation app"}
        ctx = get_design_context(spec)
        assert ctx["style"] == "warm earthy"

    def test_style_fallback_default(self):
        spec = {"description": "xyzzy unknown app"}
        ctx = get_design_context(spec)
        assert ctx["style"] == "modern light"

    def test_healing_arts_keyword_match(self):
        """'healing arts community' matches healing_arts, not event_management."""
        spec = {"description": "A healing arts community website featuring events"}
        ctx = get_design_context(spec)
        assert ctx["product_type"] == "healing_arts"
        assert ctx["palette"]["primary"] == "#0f2b3c"  # ocean-teal
        assert ctx["palette"]["accent"] == "#c8963e"  # warm gold

    def test_ceremony_keyword_match(self):
        spec = {"description": "Sacred ceremony and meditation gatherings"}
        ctx = get_design_context(spec)
        assert ctx["product_type"] == "healing_arts"

    def test_generic_dark_colors_overridden_by_uupm(self):
        """When spec has near-black placeholder colors and UUPM palette available, prefer UUPM."""
        spec = {
            "description": "A healing arts community",
            "design": {
                "primary_color": "#002040",
                "secondary_color": "#202040",
                "accent_color": "#404060",
            },
        }
        ctx = get_design_context(spec)
        # UUPM palette should win over generic darks
        assert ctx["palette"]["primary"] == "#0f2b3c"
        assert ctx["palette"]["accent"] == "#c8963e"

    def test_vibrant_customer_colors_preserved(self):
        """Customer-chosen vibrant colors are NOT overridden even with UUPM match."""
        spec = {
            "description": "A healing arts community",
            "design": {
                "primary_color": "#8B5CF6",  # vibrant purple, clearly intentional
                "secondary_color": "#A78BFA",
                "accent_color": "#F59E0B",
            },
        }
        ctx = get_design_context(spec)
        # Customer colors should win (they're not generic dark)
        assert ctx["palette"]["primary"] == "#8B5CF6"
        assert ctx["palette"]["accent"] == "#F59E0B"


# --- Data integrity ---

class TestDataIntegrity:
    def test_color_palettes_count(self):
        assert len(COLOR_PALETTES) >= 50

    def test_font_pairings_count(self):
        assert len(FONT_PAIRINGS) >= 20

    def test_product_recommendations_count(self):
        assert len(PRODUCT_RECOMMENDATIONS) >= 50

    def test_all_keywords_point_to_valid_products(self):
        for keyword, product in PRODUCT_KEYWORDS.items():
            assert product in COLOR_PALETTES, (
                f"Keyword '{keyword}' -> '{product}' not in COLOR_PALETTES"
            )


# --- Visual Design Checklist ---

class TestVisualDesignChecklist:
    """VISUAL_DESIGN_CHECKLIST must be well-formed and cover key rules."""

    def test_is_nonempty_string(self):
        assert isinstance(VISUAL_DESIGN_CHECKLIST, str)
        assert len(VISUAL_DESIGN_CHECKLIST) > 100

    def test_under_token_budget(self):
        # Budget raised 2500 → 2700 on 2026-04-16 to accommodate VISUAL_DESIGN
        # rule 16 (accent color variety) added as part of the MW v12 systemic
        # color-distribution fix. Further rules must still justify any increase.
        assert len(VISUAL_DESIGN_CHECKLIST) < 2700

    def test_has_required_terms(self):
        required = ["split-panel", "gradient", "glassmorphism", "animate-in",
                     "varied card sizes", "hero", "card-premium", "stat-card",
                     "font-brand", "input-premium"]
        for term in required:
            assert term in VISUAL_DESIGN_CHECKLIST, (
                f"Missing required term '{term}' in VISUAL_DESIGN_CHECKLIST"
            )

    def test_has_numbered_items(self):
        for i in range(1, 12):
            assert f"{i}." in VISUAL_DESIGN_CHECKLIST, (
                f"Missing numbered item {i} in VISUAL_DESIGN_CHECKLIST"
            )


# --- Style Visual Hooks ---

class TestStyleVisualHooks:
    """STYLE_VISUAL_HOOKS must cover all valid styles with concrete directives."""

    def test_keys_match_valid_styles(self):
        assert set(STYLE_VISUAL_HOOKS.keys()) == VALID_STYLES

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_hook_is_nonempty_string(self, style):
        hook = STYLE_VISUAL_HOOKS[style]
        assert isinstance(hook, str)
        assert len(hook) >= 40, (
            f"Hook for '{style}' too short ({len(hook)} chars)"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_hook_under_budget(self, style):
        # Narrative descriptions are ~150-800 chars; cap at 1200 to prevent runaway content.
        assert len(STYLE_VISUAL_HOOKS[style]) < 1200, (
            f"Hook for '{style}' too long ({len(STYLE_VISUAL_HOOKS[style])} chars)"
        )


# --- STYLE_TYPOGRAPHY_SCALE ---

class TestStyleTypographyScale:
    """STYLE_TYPOGRAPHY_SCALE must have complete entries for every valid style."""

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_entry_exists(self, style):
        assert style in STYLE_TYPOGRAPHY_SCALE, (
            f"STYLE_TYPOGRAPHY_SCALE missing entry for '{style}'"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_required_scale_keys(self, style):
        entry = STYLE_TYPOGRAPHY_SCALE[style]
        for label in ("H1:", "H2:", "H3:", "body:", "caption:"):
            assert label in entry, (
                f"STYLE_TYPOGRAPHY_SCALE['{style}'] missing '{label}'"
            )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_no_banned_fonts(self, style):
        entry = STYLE_TYPOGRAPHY_SCALE[style]
        for font in BANNED_FONTS:
            assert font not in entry, (
                f"STYLE_TYPOGRAPHY_SCALE['{style}'] contains banned font '{font}'"
            )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_uses_font_utility_classes(self, style):
        entry = STYLE_TYPOGRAPHY_SCALE[style]
        assert "font-display" in entry, (
            f"STYLE_TYPOGRAPHY_SCALE['{style}'] missing 'font-display' utility class"
        )
        assert "font-body" in entry, (
            f"STYLE_TYPOGRAPHY_SCALE['{style}'] missing 'font-body' utility class"
        )


# --- STYLE_DONT_PATTERNS ---

class TestStyleDontPatterns:
    """STYLE_DONT_PATTERNS must have clear anti-pattern guidance per style."""

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_entry_exists(self, style):
        assert style in STYLE_DONT_PATTERNS, (
            f"STYLE_DONT_PATTERNS missing entry for '{style}'"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_contains_never(self, style):
        assert "NEVER" in STYLE_DONT_PATTERNS[style], (
            f"STYLE_DONT_PATTERNS['{style}'] contains no NEVER instruction"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_substantive_length(self, style):
        entry = STYLE_DONT_PATTERNS[style]
        assert len(entry) > 50, (
            f"STYLE_DONT_PATTERNS['{style}'] too short ({len(entry)} chars)"
        )

    def test_clean_minimal_bans_gradients(self):
        assert "gradient" in STYLE_DONT_PATTERNS["clean minimal"], (
            "clean minimal don't patterns must explicitly ban gradients"
        )


# --- STYLE_VISUAL_HOOKS replacement (regression guard) ---

class TestStyleVisualHooksReplacement:
    """STYLE_VISUAL_HOOKS values replaced with narrative descriptions.
    Dict must still exist with all 6 keys."""

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_entry_still_exists(self, style):
        assert style in STYLE_VISUAL_HOOKS, (
            f"STYLE_VISUAL_HOOKS missing entry for '{style}' after replacement"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_entry_is_narrative(self, style):
        entry = STYLE_VISUAL_HOOKS[style]
        assert len(entry) > 100, (
            f"STYLE_VISUAL_HOOKS['{style}'] too short ({len(entry)} chars); "
            "expected narrative description > 100 chars"
        )

    def test_warm_earthy_mentions_serif(self):
        assert "serif" in STYLE_VISUAL_HOOKS["warm earthy"], (
            "warm earthy visual hook must mention serif typography"
        )


# --- STYLE_COMPONENT_TOKENS ---

REQUIRED_TOKEN_KEYS = [
    "btn_primary", "btn_secondary", "btn_ghost", "btn_danger",
    "card", "card_stat", "input", "nav",
    "badge_success", "badge_warning", "badge_error",
    "shadow_sm", "shadow_md", "shadow_lg",
]


class TestStyleComponentTokens:
    """All 14 component token keys present and correctly formed for every style."""

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_entry_exists_for_each_style(self, style):
        assert style in STYLE_COMPONENT_TOKENS, (
            f"STYLE_COMPONENT_TOKENS missing entry for '{style}'"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_contains_all_14_required_keys(self, style):
        entry = STYLE_COMPONENT_TOKENS[style]
        missing = [k for k in REQUIRED_TOKEN_KEYS if k not in entry]
        assert not missing, (
            f"STYLE_COMPONENT_TOKENS['{style}'] missing keys: {missing}"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_all_values_are_nonempty_strings(self, style):
        entry = STYLE_COMPONENT_TOKENS[style]
        for key in REQUIRED_TOKEN_KEYS:
            val = entry[key]
            assert isinstance(val, str) and len(val) > 0, (
                f"STYLE_COMPONENT_TOKENS['{style}']['{key}'] is not a nonempty string"
            )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_btn_danger_is_always_red(self, style):
        danger = STYLE_COMPONENT_TOKENS[style]["btn_danger"]
        assert any(x in danger for x in ("red-600", "red-700", "#DC2626")), (
            f"STYLE_COMPONENT_TOKENS['{style}']['btn_danger'] must contain red-600, red-700, or #DC2626"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_btn_primary_uses_css_var(self, style):
        btn = STYLE_COMPONENT_TOKENS[style]["btn_primary"]
        assert "var(--color-primary)" in btn, (
            f"STYLE_COMPONENT_TOKENS['{style}']['btn_primary'] must use var(--color-primary)"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_no_palette_hex_in_non_danger_tokens(self, style):
        # A palette-specific hex should never appear verbatim in component tokens
        # (tokens use var(--color-primary) instead). We test with a sample hex.
        sample_palette_hex = "#1A2B3C"
        entry = STYLE_COMPONENT_TOKENS[style]
        non_danger_keys = [k for k in REQUIRED_TOKEN_KEYS if k != "btn_danger"]
        for key in non_danger_keys:
            assert sample_palette_hex not in entry[key], (
                f"STYLE_COMPONENT_TOKENS['{style}']['{key}'] contains hardcoded palette hex"
            )

    def test_nav_differs_between_clean_minimal_and_bold_vibrant(self):
        clean_nav = STYLE_COMPONENT_TOKENS["clean minimal"]["nav"]
        bold_nav = STYLE_COMPONENT_TOKENS["bold vibrant"]["nav"]
        # clean minimal: light nav with border-b (no colored primary background)
        assert "border-b" in clean_nav, "clean minimal nav must have border-b"
        # bold vibrant: colored primary nav (no border-b, uses var(--color-primary))
        assert "var(--color-primary)" in bold_nav, "bold vibrant nav must use var(--color-primary)"
        assert "border-b" not in bold_nav, "bold vibrant nav must not have border-b"


# --- STYLE_LAYOUT_PRINCIPLES + STYLE_RESPONSIVE_NOTES ---

class TestStyleLayoutResponsiveMood:
    """Tests for STYLE_LAYOUT_PRINCIPLES, STYLE_RESPONSIVE_NOTES,
    and the updated STYLE_VISUAL_HOOKS (used as mood descriptions)."""

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_layout_principles_all_styles_present(self, style):
        assert style in STYLE_LAYOUT_PRINCIPLES, (
            f"STYLE_LAYOUT_PRINCIPLES missing entry for '{style}'"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_layout_principles_substantive(self, style):
        entry = STYLE_LAYOUT_PRINCIPLES[style]
        assert isinstance(entry, str) and len(entry) > 30, (
            f"STYLE_LAYOUT_PRINCIPLES['{style}'] too short ({len(entry)} chars)"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_responsive_notes_all_styles_present(self, style):
        assert style in STYLE_RESPONSIVE_NOTES, (
            f"STYLE_RESPONSIVE_NOTES missing entry for '{style}'"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_responsive_notes_substantive(self, style):
        entry = STYLE_RESPONSIVE_NOTES[style]
        assert isinstance(entry, str) and len(entry) > 30, (
            f"STYLE_RESPONSIVE_NOTES['{style}'] too short ({len(entry)} chars)"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_visual_hooks_all_styles_still_present(self, style):
        assert style in STYLE_VISUAL_HOOKS, (
            f"STYLE_VISUAL_HOOKS missing entry for '{style}' (regression)"
        )

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_visual_hooks_richer_than_before(self, style):
        entry = STYLE_VISUAL_HOOKS[style]
        assert len(entry) > 100, (
            f"STYLE_VISUAL_HOOKS['{style}'] too short ({len(entry)} chars); "
            "expected rich narrative > 100 chars"
        )


# --- generate_design_md() ---

_TEST_CTX = {
    "palette": {
        "primary": "#3B82F6",
        "on_primary": "#FFFFFF",
        "secondary": "#8B5CF6",
        "accent": "#10B981",
        "background": "#F8FAFC",
        "foreground": "#0F172A",
        "card": "#FFFFFF",
        "muted": "#94A3B8",
        "border": "#E2E8F0",
    },
    "fonts": {
        "display": "DM Serif Display",
        "body": "Source Sans 3",
        "url": "https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=Source+Sans+3:wght@400;600&display=swap",
    },
}

_TEST_SPEC = {
    "name": "Pet Grooming App",
    "description": "A pet grooming appointment scheduler",
    "design": {"style": "clean minimal"},
}


class TestGenerateDesignMdFullMode:
    """generate_design_md() full mode (abbreviated=False) produces correct output."""

    def _result(self):
        return generate_design_md(_TEST_SPEC, _TEST_CTX)

    def test_full_mode_starts_with_design_md_header(self):
        assert self._result().startswith("# DESIGN.md:")

    def test_full_mode_contains_all_nine_section_headers(self):
        result = self._result()
        for n in range(1, 10):
            assert f"## {n}." in result, f"Missing section header '## {n}.' in full mode output"

    def test_full_mode_section2_contains_primary_hex(self):
        assert _TEST_CTX["palette"]["primary"] in self._result()

    def test_full_mode_section2_contains_on_primary(self):
        result = self._result()
        assert "--color-on-primary" in result or "on_primary" in result

    def test_full_mode_section3_contains_display_font(self):
        assert _TEST_CTX["fonts"]["display"] in self._result()

    def test_full_mode_section3_contains_google_fonts(self):
        result = self._result()
        assert "Google Fonts" in result or "googleapis.com" in result

    def test_full_mode_section4_contains_btn_primary(self):
        assert "btn_primary" in self._result()

    def test_full_mode_section4_contains_btn_danger(self):
        assert "btn_danger" in self._result()

    def test_full_mode_section7_contains_never(self):
        assert "NEVER" in self._result()

    def test_full_mode_includes_app_name_in_header(self):
        assert _TEST_SPEC["name"] in self._result()

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_full_mode_no_banned_fonts_parametrized(self, style):
        spec = {"name": "Test App", "design": {"style": style}}
        result = generate_design_md(spec, _TEST_CTX)
        for font in BANNED_FONTS:
            assert font not in result, (
                f"generate_design_md output for '{style}' contains banned font '{font}'"
            )


class TestGenerateDesignMdAbbreviatedMode:
    """generate_design_md(abbreviated=True) emits sections 2,3,4,7 only."""

    def _abbreviated(self):
        return generate_design_md(_TEST_SPEC, _TEST_CTX, abbreviated=True)

    def _full(self):
        return generate_design_md(_TEST_SPEC, _TEST_CTX)

    def test_abbreviated_contains_sections_2_3_4_7(self):
        result = self._abbreviated()
        for n in (2, 3, 4, 7):
            assert f"## {n}." in result, f"Abbreviated mode missing section '## {n}.'"

    def test_abbreviated_omits_sections_1_5_6_8_9(self):
        result = self._abbreviated()
        for n in (1, 5, 6, 8, 9):
            assert f"## {n}." not in result, (
                f"Abbreviated mode must not contain section '## {n}.'"
            )

    def test_abbreviated_shorter_than_full(self):
        assert len(self._abbreviated()) < len(self._full())

    def test_abbreviated_still_contains_primary_hex(self):
        assert _TEST_CTX["palette"]["primary"] in self._abbreviated()

    def test_abbreviated_still_contains_never(self):
        assert "NEVER" in self._abbreviated()

    def test_abbreviated_no_visual_theme_prose(self):
        assert "## 1. Visual Theme" not in self._abbreviated()


class TestGenerateDesignMdEdgeCases:
    """generate_design_md() handles missing/invalid inputs gracefully."""

    def test_empty_palette_no_keyerror(self):
        ctx = {"palette": {}, "fonts": _TEST_CTX["fonts"]}
        result = generate_design_md(_TEST_SPEC, ctx)
        assert isinstance(result, str)
        assert "## 2." in result

    def test_unknown_style_falls_back(self):
        spec = {"name": "Test", "design": {"style": "brutalist"}}
        result = generate_design_md(spec, _TEST_CTX)
        assert isinstance(result, str)
        assert "# DESIGN.md:" in result

    def test_missing_fonts_key_graceful(self):
        ctx = {"palette": _TEST_CTX["palette"]}
        result = generate_design_md(_TEST_SPEC, ctx)
        assert isinstance(result, str)
        assert "## 1." in result or "## 2." in result

    def test_spec_with_no_name_field(self):
        spec = {"design": {"style": "clean minimal"}}
        result = generate_design_md(spec, _TEST_CTX)
        assert result.startswith("# DESIGN.md:")

    def test_returns_string_type(self):
        result = generate_design_md(_TEST_SPEC, _TEST_CTX)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Gap-filling tests: coverage identified during review
# ---------------------------------------------------------------------------

class TestBtnPrimaryOnPrimaryContract:
    """btn_primary must use var(--color-on-primary) for text, WCAG contract.

    var(--color-on-primary) is the only correct text color on primary
    backgrounds; hardcoding 'text-white' breaks dark-on-light palettes.
    """

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_btn_primary_uses_css_var_for_text(self, style):
        btn = STYLE_COMPONENT_TOKENS[style]["btn_primary"]
        assert "var(--color-on-primary)" in btn, (
            f"STYLE_COMPONENT_TOKENS['{style}']['btn_primary'] must use "
            "var(--color-on-primary) for text color (WCAG contract); "
            f"got: {btn}"
        )


class TestGenerateDesignMdSection2OnPrimary:
    """Section 2 Color Palette table must include --color-on-primary as a row.

    The table row format is:  | --color-on-primary | #XXXXXX | Role |
    A substring match on 'on_primary' is insufficient, the token must
    appear as a proper table cell so Claude reads it as a CSS variable name.
    """

    _CTX_WITH_ON_PRIMARY = {
        "palette": {
            "primary": "#3B82F6",
            "on_primary": "#FFFFFF",
            "secondary": "#8B5CF6",
            "accent": "#10B981",
            "background": "#F8FAFC",
            "foreground": "#0F172A",
            "card": "#FFFFFF",
            "muted": "#94A3B8",
            "border": "#E2E8F0",
        },
        "fonts": _TEST_CTX["fonts"],
    }

    def test_section2_has_on_primary_table_row(self):
        spec = {"name": "App", "design": {"style": "modern light"}}
        result = generate_design_md(spec, self._CTX_WITH_ON_PRIMARY)
        assert "| --color-on-primary |" in result, (
            "Section 2 table must include '| --color-on-primary |' row; "
            f"got section 2: {result[result.find('## 2.'):result.find('## 3.')][:300]}"
        )

    def test_section2_on_primary_hex_present(self):
        """The hex value for on_primary must appear in the table row."""
        spec = {"name": "App", "design": {"style": "modern light"}}
        result = generate_design_md(spec, self._CTX_WITH_ON_PRIMARY)
        on_primary_hex = self._CTX_WITH_ON_PRIMARY["palette"]["on_primary"]
        # Find the on_primary row and verify hex appears there
        lines_with_on_primary = [
            l for l in result.split("\n") if "--color-on-primary" in l
        ]
        assert any(on_primary_hex in l for l in lines_with_on_primary), (
            f"on_primary hex {on_primary_hex} not found in --color-on-primary row(s): "
            f"{lines_with_on_primary}"
        )


class TestGenerateDesignMdCrossStyleDifferentiation:
    """Different styles must produce genuinely different section 4 content.

    Regression guard: if the style argument were silently ignored and the
    function always returned 'modern light' tokens, all parametrized token
    tests would pass but the output would be wrong.
    """

    def _section4(self, style):
        spec = {"name": "App", "design": {"style": style}}
        result = generate_design_md(spec, _TEST_CTX)
        s4_start = result.find("## 4.")
        s4_end = result.find("\n\n## 5.")
        if s4_end == -1:
            s4_end = result.find("\n\n## 7.")  # abbreviated mode or no section 5
        return result[s4_start:s4_end] if s4_start != -1 else ""

    def test_modern_dark_differs_from_modern_light_in_section4(self):
        dark = self._section4("modern dark")
        light = self._section4("modern light")
        assert dark != light, (
            "Section 4 for 'modern dark' must differ from 'modern light'; "
            "style argument is not being applied"
        )

    def test_modern_dark_section4_contains_dark_theme_classes(self):
        """modern dark cards use slate-800/slate-900, not bg-white."""
        dark = self._section4("modern dark")
        assert "slate" in dark, (
            "Section 4 for 'modern dark' must contain dark Tailwind classes "
            f"(slate-*); got: {dark[:300]}"
        )

    def test_bold_vibrant_differs_from_clean_minimal_in_section4(self):
        vibrant = self._section4("bold vibrant")
        minimal = self._section4("clean minimal")
        assert vibrant != minimal


class TestGenerateDesignMdSection6Content:
    """Section 6 Depth & Elevation must reference actual shadow token values."""

    def _section6(self, style="modern light"):
        spec = {"name": "App", "design": {"style": style}}
        result = generate_design_md(spec, _TEST_CTX)
        s6_start = result.find("## 6.")
        s6_end = result.find("\n\n## 7.")
        return result[s6_start:s6_end] if s6_start != -1 else ""

    def test_section6_mentions_shadow_sm(self):
        assert "shadow_sm" in self._section6()

    def test_section6_mentions_shadow_md(self):
        assert "shadow_md" in self._section6()

    def test_section6_mentions_shadow_lg(self):
        assert "shadow_lg" in self._section6()

    def test_section6_content_varies_by_style(self):
        """Minimal uses shadow-none while bold vibrant uses shadow-2xl."""
        minimal_s6 = self._section6("clean minimal")
        vibrant_s6 = self._section6("bold vibrant")
        assert minimal_s6 != vibrant_s6, (
            "Section 6 content must vary by style (shadow tokens differ); "
            "clean minimal uses shadow-none, bold vibrant uses shadow-2xl"
        )


class TestGenerateDesignMdStyleInHeader:
    """Style name must appear in the DESIGN.md header for all styles.

    UUPM pipeline tests assert 'warm earthy' in ctx and
    'corporate professional' in ctx, this parametrized test catches
    regressions to the other 4 styles before they reach pipeline tests.
    """

    @pytest.mark.parametrize("style", sorted(VALID_STYLES))
    def test_style_name_in_header(self, style):
        spec = {"name": "Test App", "design": {"style": style}}
        result = generate_design_md(spec, _TEST_CTX)
        # Header line: "# DESIGN.md, Test App\n**Style:** {style}"
        assert style in result, (
            f"Style name '{style}' must appear in DESIGN.md header; "
            f"first 200 chars: {result[:200]}"
        )


class TestModernDarkTypographyLightText:
    """modern dark typography must use explicit light text classes.

    var(--color-foreground) resolves to a DARK color in all palettes
    (it is the body text color on light backgrounds). Using it in a dark
    theme would produce invisible dark-on-dark text.
    """

    def test_modern_dark_typography_does_not_use_foreground_var(self):
        entry = STYLE_TYPOGRAPHY_SCALE["modern dark"]
        assert "var(--color-foreground)" not in entry, (
            "STYLE_TYPOGRAPHY_SCALE['modern dark'] must not use var(--color-foreground) "
            "(resolves to dark text on dark background); use text-white/text-slate-* instead"
        )

    def test_modern_dark_typography_h1_is_light(self):
        h1_line = [l for l in STYLE_TYPOGRAPHY_SCALE["modern dark"].split("\n") if l.startswith("H1:")][0]
        assert any(cls in h1_line for cls in ("text-white", "text-slate-100", "text-slate-200")), (
            f"modern dark H1 must use a light text class; got: {h1_line}"
        )
