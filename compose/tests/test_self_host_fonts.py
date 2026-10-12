"""Tests for the self-hosted Google Fonts pattern (v2-self-hosted-fonts-pattern).

Covers compose/utils/self_host_fonts.py pure helpers + the
emit_layout_files / emit_middleware_settings_js / compose_files
integration so an opt-in build with a populated cache bakes WOFF2s
and an opt-in build without cache files falls back cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compose.build import (
    BuildSpec, resolve_spec, compose_files,
    emit_layout_files, emit_middleware_settings_js,
    _self_host_fonts_plan,
)
from compose.utils.self_host_fonts import (
    DEFAULT_FONT_CACHE_DIR,
    SelfHostFontsPlan,
    emit_font_files,
    family_slug,
    font_filename,
    is_self_host_enabled,
    plan_self_hosted_fonts,
    render_font_face_css,
    render_preload_links,
)


FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
SAMPLE_SPEC = FIXTURE_DIR / "sample-healer-spec.json"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_family_slug_lowercases_and_dashes():
    assert family_slug("Cormorant Garamond") == "cormorant-garamond"
    assert family_slug("DM Serif Display") == "dm-serif-display"
    assert family_slug("IBM Plex Serif") == "ibm-plex-serif"
    assert family_slug("Tangerine") == "tangerine"
    assert family_slug("Inter") == "inter"


def test_family_slug_handles_empty_and_punctuation():
    assert family_slug("") == ""
    assert family_slug("   ") == ""
    # Multiple non-alnum runs collapse; leading/trailing dashes strip.
    assert family_slug("-- Foo  Bar --") == "foo-bar"


def test_font_filename_pairs_slug_with_weight():
    assert font_filename("Cormorant Garamond", 400) == "cormorant-garamond-400.woff2"
    assert font_filename("Montserrat", 700) == "montserrat-700.woff2"
    assert font_filename("Inter", 500) == "inter-500.woff2"


def test_is_self_host_enabled_only_accepts_literal_true():
    assert is_self_host_enabled({"self_host_fonts": True}) is True
    assert is_self_host_enabled({"self_host_fonts": False}) is False
    assert is_self_host_enabled({"self_host_fonts": "true"}) is False  # string != bool
    assert is_self_host_enabled({"self_host_fonts": 1}) is False  # int != bool
    assert is_self_host_enabled({}) is False
    assert is_self_host_enabled(None) is False
    assert is_self_host_enabled("not a dict") is False


# ---------------------------------------------------------------------------
# plan_self_hosted_fonts: cache resolution + fallback
# ---------------------------------------------------------------------------

def _make_font_pair(display_family="Tangerine", display_weights=(400, 700),
                    body_family="Montserrat", body_weights=(400, 500, 600, 700)):
    return {
        "id": f"{family_slug(display_family)}-{family_slug(body_family)}",
        "display": {"family": display_family, "weights": list(display_weights), "is_serif": True},
        "body": {"family": body_family, "weights": list(body_weights)},
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Tangerine&family=Montserrat",
    }


def _populate_cache(cache_dir: Path, font_pair: dict, payload: bytes = b"WOFF2DATA"):
    """Write fake WOFF2 bytes for every (family, weight) the pair needs."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    for fam_slot in (font_pair["display"], font_pair["body"]):
        fam = fam_slot["family"]
        for w in fam_slot["weights"]:
            (cache_dir / font_filename(fam, w)).write_bytes(payload)


def test_plan_returns_files_when_cache_complete(tmp_path):
    fp = _make_font_pair()
    _populate_cache(tmp_path, fp)
    plan = plan_self_hosted_fonts(fp, cache_dir=tmp_path)
    assert plan.fallback_reason is None
    assert plan.missing == []
    # 2 display + 4 body = 6 weights, no overlap between Tangerine + Montserrat
    assert len(plan.files) == 6
    fams = {f.family for f in plan.files}
    assert fams == {"Tangerine", "Montserrat"}
    # Preload set covers both families, deduped, display first.
    assert plan.preload_families == ["Tangerine", "Montserrat"]


def test_plan_fallback_when_cache_missing(tmp_path):
    fp = _make_font_pair()
    # Don't populate cache.
    plan = plan_self_hosted_fonts(fp, cache_dir=tmp_path)
    assert plan.fallback_reason is not None
    assert "missing" in plan.fallback_reason.lower()
    # files cleared so partial bake never happens
    assert plan.files == []
    assert len(plan.missing) == 6


def test_plan_partial_cache_still_falls_back(tmp_path):
    fp = _make_font_pair()
    _populate_cache(tmp_path, fp)
    # Remove ONE file to simulate partial cache.
    (tmp_path / font_filename("Montserrat", 600)).unlink()
    plan = plan_self_hosted_fonts(fp, cache_dir=tmp_path)
    assert plan.fallback_reason is not None
    assert plan.files == []  # all-or-nothing
    assert ("Montserrat", 600) in plan.missing


def test_plan_dedupes_when_display_and_body_share_family(tmp_path):
    fp = _make_font_pair(
        display_family="Inter", display_weights=(500, 600, 700),
        body_family="Inter", body_weights=(300, 400, 500),
    )
    _populate_cache(tmp_path, fp)
    plan = plan_self_hosted_fonts(fp, cache_dir=tmp_path)
    assert plan.fallback_reason is None
    # Dedup: {300, 400, 500, 600, 700} = 5 distinct weights
    assert len(plan.files) == 5
    assert plan.preload_families == ["Inter"]


def test_plan_rejects_non_dict_font_pair(tmp_path):
    plan = plan_self_hosted_fonts(None, cache_dir=tmp_path)
    assert plan.fallback_reason
    plan = plan_self_hosted_fonts("not-a-dict", cache_dir=tmp_path)
    assert plan.fallback_reason


def test_plan_rejects_missing_family(tmp_path):
    fp = {"display": {"family": "", "weights": [400]}, "body": {"family": "Inter", "weights": [400]}}
    plan = plan_self_hosted_fonts(fp, cache_dir=tmp_path)
    assert plan.fallback_reason and "family" in plan.fallback_reason.lower()


def test_plan_rejects_no_weights(tmp_path):
    fp = {"display": {"family": "Foo", "weights": []}, "body": {"family": "Bar", "weights": []}}
    plan = plan_self_hosted_fonts(fp, cache_dir=tmp_path)
    assert plan.fallback_reason and "weight" in plan.fallback_reason.lower()


# ---------------------------------------------------------------------------
# Rendering: preload links + @font-face CSS
# ---------------------------------------------------------------------------

def test_render_preload_links_one_per_family_at_preload_weight(tmp_path):
    fp = _make_font_pair()
    _populate_cache(tmp_path, fp)
    plan = plan_self_hosted_fonts(fp, cache_dir=tmp_path)
    html = render_preload_links(plan)
    # One link per family
    assert html.count('<link rel="preload"') == 2
    assert 'as="font"' in html
    assert 'type="font/woff2"' in html
    assert 'crossorigin' in html
    # Preload weight defaults to 400 when available
    assert "/fonts/tangerine-400.woff2" in html
    assert "/fonts/montserrat-400.woff2" in html


def test_render_preload_links_falls_back_to_first_weight_when_400_missing(tmp_path):
    fp = _make_font_pair(
        display_family="Anton", display_weights=(700,),
        body_family="Inter", body_weights=(400, 500),
    )
    _populate_cache(tmp_path, fp)
    plan = plan_self_hosted_fonts(fp, cache_dir=tmp_path)
    html = render_preload_links(plan)
    # Anton has no 400; first declared weight (700) is preloaded
    assert "/fonts/anton-700.woff2" in html
    # Inter has 400; preferred
    assert "/fonts/inter-400.woff2" in html


def test_render_preload_links_empty_when_no_files():
    assert render_preload_links(SelfHostFontsPlan()) == ""


def test_render_font_face_css_one_rule_per_weight(tmp_path):
    fp = _make_font_pair()
    _populate_cache(tmp_path, fp)
    plan = plan_self_hosted_fonts(fp, cache_dir=tmp_path)
    css = render_font_face_css(plan)
    assert css.count("@font-face") == 6
    assert "font-family: 'Tangerine'" in css
    assert "font-family: 'Montserrat'" in css
    # font-display: swap is required so we don't regress to FOIT
    assert "font-display: swap" in css
    # Self-hosted URLs only, no fonts.gstatic.com / fonts.googleapis.com
    assert "fonts.gstatic.com" not in css
    assert "fonts.googleapis.com" not in css
    # WOFF2 format only
    assert css.count("format('woff2')") == 6


def test_render_font_face_css_empty_when_no_files():
    assert render_font_face_css(SelfHostFontsPlan()) == ""


# ---------------------------------------------------------------------------
# emit_font_files: bytes pipeline
# ---------------------------------------------------------------------------

def test_emit_font_files_returns_public_path_and_bytes(tmp_path):
    fp = _make_font_pair()
    payload = b"FAKE_WOFF2_BYTES_for_testing"
    _populate_cache(tmp_path, fp, payload=payload)
    plan = plan_self_hosted_fonts(fp, cache_dir=tmp_path)
    files = emit_font_files(plan)
    assert len(files) == 6
    for path, data in files:
        assert path.startswith("public/fonts/")
        assert path.endswith(".woff2")
        assert data == payload


def test_emit_font_files_skips_empty_cache_files(tmp_path):
    fp = _make_font_pair()
    _populate_cache(tmp_path, fp, payload=b"OK")
    # Overwrite one with empty payload
    (tmp_path / font_filename("Montserrat", 400)).write_bytes(b"")
    plan = plan_self_hosted_fonts(fp, cache_dir=tmp_path)
    files = emit_font_files(plan)
    # Empty file skipped, others emitted
    assert len(files) == 5
    paths = [p for p, _ in files]
    assert "public/fonts/montserrat-400.woff2" not in paths


# ---------------------------------------------------------------------------
# Integration: build pipeline behavior with + without opt-in / cache
# ---------------------------------------------------------------------------

def _load_sample_spec_with(harbor_meta=None):
    with open(SAMPLE_SPEC) as f:
        d = json.load(f)
    if harbor_meta is not None:
        d["_harbor_meta"] = harbor_meta
    return BuildSpec.from_dict(d)


def test_default_build_does_not_self_host(monkeypatch, tmp_path):
    """No `_harbor_meta.self_host_fonts` → existing Google Fonts behavior."""
    spec = _load_sample_spec_with(harbor_meta=None)
    rs = resolve_spec(spec)
    # Layout still has empty placeholders (no inline @font-face / preloads)
    layout_files = dict(emit_layout_files(rs))
    layout = layout_files["views/layout-public.ejs"]
    assert "@font-face" not in layout
    assert 'rel="preload"' not in layout
    # The placeholder markers must be removed from the emitted layout.
    assert "__SELF_HOST_FONT_PRELOADS__" not in layout
    assert "__SELF_HOST_FONT_FACE__" not in layout
    # Middleware still ships the Google Fonts URL
    _, settings_js = emit_middleware_settings_js(rs)
    assert "fonts.googleapis.com" in settings_js
    # No translation_warnings appended on the no-opt-in path
    assert all(
        w.get("reason") != "self_host_fonts_fallback"
        for w in rs.spec.engine_warnings
    )


def test_opt_in_with_missing_cache_falls_back_with_warning(monkeypatch, tmp_path):
    """`self_host_fonts: true` but cache empty → fallback + warning."""
    # Point at an empty cache directory.
    monkeypatch.setattr(
        "compose.utils.self_host_fonts.DEFAULT_FONT_CACHE_DIR", tmp_path
    )
    spec = _load_sample_spec_with(harbor_meta={"self_host_fonts": True})
    rs = resolve_spec(spec)
    layout_files = dict(emit_layout_files(rs))
    layout = layout_files["views/layout-public.ejs"]
    # No inline self-hosted CSS / preload; falls back to Google Fonts.
    assert "@font-face" not in layout
    assert 'rel="preload"' not in layout or "fonts.googleapis" not in layout
    # Placeholders must be substituted to empty strings (not left raw).
    assert "__SELF_HOST_FONT_PRELOADS__" not in layout
    assert "__SELF_HOST_FONT_FACE__" not in layout
    # Middleware still falls back to Google Fonts.
    _, settings_js = emit_middleware_settings_js(rs)
    assert "fonts.googleapis.com" in settings_js
    # Translation warning surfaced.
    fallback_warnings = [
        w for w in rs.spec.engine_warnings
        if w.get("reason") == "self_host_fonts_fallback"
    ]
    assert len(fallback_warnings) == 1
    assert "missing" in fallback_warnings[0]["detail"].lower()


def test_opt_in_with_populated_cache_self_hosts(monkeypatch, tmp_path):
    """`self_host_fonts: true` + cache complete → bake + suppress Google Fonts."""
    # Pre-populate cache for the sample spec's font_pair (cormorant-lato).
    # Need to know which families + weights; pull from font-pairs.json.
    spec = _load_sample_spec_with(harbor_meta={"self_host_fonts": True})
    rs = resolve_spec(spec)
    _populate_cache(tmp_path, rs.font_pair, payload=b"FAKE_WOFF2_BYTES")
    monkeypatch.setattr(
        "compose.utils.self_host_fonts.DEFAULT_FONT_CACHE_DIR", tmp_path
    )

    # Re-resolve to clear any cached plan state on `rs`.
    rs = resolve_spec(spec)

    # Layout carries inline @font-face + preload links.
    layout_files = dict(emit_layout_files(rs))
    layout = layout_files["views/layout-public.ejs"]
    assert "@font-face" in layout
    assert 'rel="preload"' in layout
    assert 'as="font"' in layout
    assert "font-display: swap" in layout
    # Slug-based filename (Cormorant Garamond -> cormorant-garamond)
    assert "cormorant-garamond" in layout
    assert "lato" in layout
    # Placeholders fully resolved (not left as raw markers)
    assert "__SELF_HOST_FONT_PRELOADS__" not in layout
    assert "__SELF_HOST_FONT_FACE__" not in layout

    # Middleware suppresses the Google Fonts URL to break the round-trip.
    _, settings_js = emit_middleware_settings_js(rs)
    assert "fonts.googleapis.com" not in settings_js
    assert 'FONTS_URL = ""' in settings_js


def test_full_compose_emits_woff2_files_under_public_fonts(monkeypatch, tmp_path):
    """compose_files extends emit pipeline with the WOFF2 binaries."""
    spec = _load_sample_spec_with(harbor_meta={"self_host_fonts": True})
    # Pre-populate cache for the actual font_pair via resolve_spec.
    rs_probe = resolve_spec(spec)
    _populate_cache(tmp_path, rs_probe.font_pair, payload=b"X" * 64)
    monkeypatch.setattr(
        "compose.utils.self_host_fonts.DEFAULT_FONT_CACHE_DIR", tmp_path
    )

    # Re-resolve so the cached plan attaches to a fresh ResolvedSpec.
    rs = resolve_spec(spec)
    files = compose_files(rs)
    by_path = dict(files)
    font_paths = [p for p in by_path if p.startswith("public/fonts/")]
    # At least one WOFF2 per declared weight in the font_pair.
    assert len(font_paths) >= 4
    for fp in font_paths:
        assert fp.endswith(".woff2")
        assert by_path[fp] == b"X" * 64


def test_self_host_plan_is_memoized(monkeypatch, tmp_path):
    """_self_host_fonts_plan must not duplicate fallback warnings on repeat calls.

    emit_layout_files + emit_middleware_settings_js + compose_files all call
    it; without memoization the warning would multiply.
    """
    monkeypatch.setattr(
        "compose.utils.self_host_fonts.DEFAULT_FONT_CACHE_DIR", tmp_path
    )
    spec = _load_sample_spec_with(harbor_meta={"self_host_fonts": True})
    rs = resolve_spec(spec)
    _self_host_fonts_plan(rs)
    _self_host_fonts_plan(rs)
    _self_host_fonts_plan(rs)
    fallback_warnings = [
        w for w in rs.spec.engine_warnings
        if w.get("reason") == "self_host_fonts_fallback"
    ]
    assert len(fallback_warnings) == 1


def test_compose_no_woff2_files_when_self_host_disabled():
    """Default builds (no opt-in) must NOT emit anything under public/fonts/."""
    spec = _load_sample_spec_with(harbor_meta=None)
    rs = resolve_spec(spec)
    files = compose_files(rs)
    font_paths = [p for p, _ in files if p.startswith("public/fonts/")]
    assert font_paths == []


def test_layout_preserves_existing_placeholders_after_self_host_substitution(
    monkeypatch, tmp_path
):
    """Self-host substitution must not collide with the existing template-family
    placeholders. Regression guard against accidental ordering bugs."""
    spec = _load_sample_spec_with(harbor_meta={"self_host_fonts": True})
    rs = resolve_spec(spec)
    _populate_cache(tmp_path, rs.font_pair, payload=b"WOFF2")
    monkeypatch.setattr(
        "compose.utils.self_host_fonts.DEFAULT_FONT_CACHE_DIR", tmp_path
    )
    rs = resolve_spec(spec)
    layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
    # All four compose-time placeholders must be substituted (not left raw).
    for marker in (
        "__SHELL_NAV_FILE__",
        "__SHELL_FOOTER_FILE__",
        "__TYPOGRAPHY_SCALE_CSS__",
        "__TYPOGRAPHY_SCALE_CLASSES__",
        "__SELF_HOST_FONT_PRELOADS__",
        "__SELF_HOST_FONT_FACE__",
    ):
        assert marker not in layout, f"placeholder {marker} left raw in emitted layout"
