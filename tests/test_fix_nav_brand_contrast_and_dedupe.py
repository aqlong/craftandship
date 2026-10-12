"""Tests for _fix_nav_brand_contrast_and_dedupe.

TWO interacting nav bugs:
  (a) Nav painted in primary color + Claude's anchor has inline
      `style="color: var(--color-primary)"` → primary-on-primary,
      illegible.
  (b) _ensure_nav_brand_element injects `<a class="brand site-name ...">`
      when it can't detect Claude's existing brand, Claude often emits
      an anchor without `brand`/`logo`/etc. classes, so we get a
      duplicate wordmark.
"""

from __future__ import annotations

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


SAMPLE_SPEC = {
    "design": {
        "primary_color": "#3b2f5c",
        "secondary_color": "#f4f1fa",
        "accent_color": "#b9a7d9",
    },
}


def test_contrast_fix_rewrites_primary_color_on_primary_nav():
    """Nav with `bg-[var(--color-primary)]` + descendant anchor with
    `color: var(--color-primary)` → rewrite color to on-primary."""
    layout = (
        "<html><body>\n"
        '<nav class="bg-[var(--color-primary)]/90 sticky top-0">\n'
        '  <a href="/" class="font-display" style="color: var(--color-primary);">Sample Practitioner</a>\n'
        "</nav>\n"
        "</body></html>"
    )
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    assert "color: var(--color-on-primary, #fff)" in got
    # original maroon-on-maroon is gone.
    assert "color: var(--color-primary);" not in got


def test_contrast_fix_skips_non_primary_nav():
    """Nav with a white bg and a brand using primary color → no rewrite
    (the color is legible against white)."""
    layout = (
        '<nav class="bg-white sticky">\n'
        '  <a href="/" style="color: var(--color-primary);">Sample Practitioner</a>\n'
        "</nav>"
    )
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    # Untouched: we don't second-guess white-bg primary-color brands.
    assert "color: var(--color-primary);" in out["views/layout.ejs"]


def test_dedupe_injected_orphan_when_claude_also_has_wordmark():
    """_ensure_nav_brand_element injected an orphan; Claude's unmarked
    anchor is the real brand. Strip the orphan, transplant
    `brand site-name` classes onto Claude's anchor."""
    layout = (
        "<html><body>\n"
        '<nav class="bg-white">\n'
        '    \n'
        '    <a href="/" class="brand site-name font-display font-semibold text-lg whitespace-nowrap" aria-label="Sample Practitioner home"><span class="site-name">Sample Practitioner</span></a>\n'
        '    \n'
        '    <div class="container">\n'
        '      <a href="/" class="font-display text-xl">Sample Practitioner</a>\n'
        '      <a href="/about">About</a>\n'
        '    </div>\n'
        "</nav>\n"
        "</body></html>"
    )
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # Injected orphan is gone.
    assert 'aria-label="Sample Practitioner home"' not in got
    # Claude's anchor now carries brand + site-name classes.
    assert "brand site-name font-display text-xl" in got
    # Only ONE "Sample Practitioner" wordmark remains.
    assert got.count("Sample Practitioner") == 1
    # /about link still present.
    assert 'href="/about"' in got


def test_dedupe_preserves_legitimate_nav_links_that_happen_to_match_wordmark():
    """Dedupe must NOT strip a legitimate nav link whose text happens
    to include the wordmark, only anchors whose FULL visible text is
    exactly the wordmark count as duplicates."""
    layout = (
        '<nav class="bg-white">\n'
        '    <a href="/" class="brand site-name font-display">Sample Practitioner</a>\n'
        '    <a href="/about">About Sample Practitioner Studio</a>\n'
        "</nav>"
    )
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # No duplicate found (the other anchor has different text), so the
    # injected brand stays.
    assert 'class="brand site-name font-display">Sample Practitioner</a>' in got
    assert "About Sample Practitioner Studio" in got


def test_contrast_and_dedupe_both_fire_on_combined_snippet():
    """End-to-end on a layout that has both problems:
    primary-bg nav + injected orphan brand + Claude's brand
    with `color: var(--color-primary)`."""
    layout = (
        '<nav class="bg-[var(--color-primary)]/90 sticky top-0" '
        'style="background-color:rgba(107,21,25,0.92);">\n'
        '    <a href="/" class="brand site-name font-display font-semibold text-lg whitespace-nowrap" '
        'aria-label="Sample Practitioner home"><span class="site-name">Sample Practitioner</span></a>\n'
        '    \n'
        '    <div class="max-w-6xl mx-auto">\n'
        '      <a href="/" class="font-display text-xl font-bold" '
        'style="color: var(--color-primary);">Sample Practitioner</a>\n'
        '    </div>\n'
        "</nav>"
    )
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # Both fires landed:
    # 1. Injected orphan gone.
    assert 'aria-label="Sample Practitioner home"' not in got
    # 2. Claude's anchor kept + has brand classes + contrast fixed.
    assert "brand site-name font-display text-xl font-bold" in got
    assert "color: var(--color-on-primary, #fff)" in got
    assert "color: var(--color-primary);" not in got


def test_idempotent_when_no_duplicate_and_no_primary_bg():
    """Plain nav with no duplicate brand and white bg → no changes."""
    layout = (
        '<nav class="bg-white">\n'
        '    <a href="/" class="brand site-name">Home</a>\n'
        "</nav>"
    )
    out = build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    )
    # Unchanged.
    assert dict(out)["views/layout.ejs"] == layout


def test_registered_in_expected_processors():
    import pipeline
    assert "_fix_nav_brand_contrast_and_dedupe" in pipeline.REGISTRY
    assert "_fix_nav_brand_contrast_and_dedupe" in pipeline.EXPECTED_PROCESSORS
    proc = pipeline.REGISTRY["_fix_nav_brand_contrast_and_dedupe"]
    assert "build" in proc.phase and "reapply" in proc.phase
    # Ordering: runs AFTER _ensure_nav_brand_element so the injection is
    # in place when we look for a duplicate.
    order = pipeline.pipeline_order("build")
    assert order.index("_ensure_nav_brand_element") < order.index(
        "_fix_nav_brand_contrast_and_dedupe"
    )


# ---------------------------------------------------------------------------
# Regression: `color: var(--color-foreground)` on nav links
# ---------------------------------------------------------------------------


def test_contrast_fix_rewrites_foreground_token_on_primary_nav():
    """Every nav link is styled
    `style="color: var(--color-foreground);"`. On a primary-colored
    nav bg, foreground resolves to a dark gray, illegible. Contrast
    fix must rewrite this as well, not just --color-primary."""
    layout = (
        '<nav class="bg-[var(--color-primary)]/90 sticky top-0">\n'
        '  <a href="/" class="nav-link" style="color: var(--color-foreground);">Home</a>\n'
        '  <a href="/about" class="nav-link" style="color: var(--color-foreground);">About</a>\n'
        "</nav>"
    )
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # Foreground tokens rewritten.
    assert "color: var(--color-foreground)" not in got
    assert got.count("color: var(--color-on-primary, #fff)") == 2


def test_contrast_fix_rewrites_text_token_on_primary_nav():
    """Older Claude emissions used --color-text (pre-emerald cleanup).
    Same fix, illegible on primary bg."""
    layout = (
        '<nav class="bg-[var(--color-primary)]">\n'
        '  <a href="/" style="color: var(--color-text);">Home</a>\n'
        "</nav>"
    )
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    assert "color: var(--color-text)" not in got
    assert "color: var(--color-on-primary, #fff)" in got


def test_contrast_fix_preserves_on_primary_and_accent_tokens():
    """Tokens already designed for visibility on primary bg must NOT
    be rewritten, they're correct as-is."""
    layout = (
        '<nav class="bg-[var(--color-primary)]">\n'
        '  <a href="/" style="color: var(--color-on-primary, #fff);">Home</a>\n'
        '  <a href="/x" style="color: var(--color-accent);">Featured</a>\n'
        '  <a href="/y" style="color: var(--color-secondary);">Muted</a>\n'
        "</nav>"
    )
    out = build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    )
    # Unchanged: all three tokens are already on-surface-safe.
    assert dict(out)["views/layout.ejs"] == layout


def test_foreground_token_on_white_nav_untouched():
    """Foreground IS the correct color on a white/cream nav bg; only
    rewrite when the nav bg is actually primary-colored."""
    layout = (
        '<nav class="bg-white border-b">\n'
        '  <a href="/" style="color: var(--color-foreground);">Home</a>\n'
        "</nav>"
    )
    out = build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    )
    assert dict(out)["views/layout.ejs"] == layout


# ---------------------------------------------------------------------------
# Regression: dedupe must match when Claude's anchor text extends
# past the wordmark (e.g. "Sample Practitioner Admin" vs orphan "Sample Practitioner").
# ---------------------------------------------------------------------------


def test_dedupe_matches_when_claude_anchor_text_extends_wordmark():
    """The injected orphan reads
    "Sample Practitioner" but Claude's anchor reads "Sample Practitioner Admin". Old
    strict `\\s*wordmark\\s*</a>` regex missed this, so both wordmarks
    rendered. Widened startswith match fixes it."""
    layout = (
        '<nav class="bg-white">\n'
        '    \n'
        '    <a href="/" class="brand site-name font-display" aria-label="Sample Practitioner home"><span class="site-name">Sample Practitioner</span></a>\n'
        '    \n'
        '    <a href="/admin" class="font-display text-xl">Sample Practitioner Admin</a>\n'
        '    <a href="/logout">Log out</a>\n'
        "</nav>"
    )
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # Injected orphan stripped.
    assert 'aria-label="Sample Practitioner home"' not in got
    # Claude's anchor kept + carries brand classes.
    assert "brand site-name font-display text-xl" in got
    assert "Sample Practitioner Admin" in got
    # Unrelated nav links untouched.
    assert 'href="/logout"' in got


def test_dedupe_matches_when_claude_anchor_has_inline_span_suffix():
    """Claude emits `<a>Sample Practitioner <span>Admin</span></a>`, visible
    text after tag-strip is "Sample Practitioner Admin", which starts with the
    wordmark. Dedupe must fire."""
    layout = (
        '<nav class="bg-white">\n'
        '    <a href="/" class="brand site-name font-display" aria-label="Sample Practitioner home"><span class="site-name">Sample Practitioner</span></a>\n'
        '    <a href="/" class="font-display"><span>Sample Practitioner</span> <span class="text-sm">Admin</span></a>\n'
        "</nav>"
    )
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # Orphan gone.
    assert 'aria-label="Sample Practitioner home"' not in got
    # Kept anchor has brand classes.
    assert "brand site-name font-display" in got
    # Admin suffix text preserved.
    assert "Admin" in got


def test_dedupe_is_case_insensitive():
    """Claude sometimes emits the brand in a different case (lowercase
    for minimal design, etc.). Dedupe should still match."""
    layout = (
        '<nav class="bg-white">\n'
        '    <a href="/" class="brand site-name font-display" aria-label="Sample Practitioner home"><span class="site-name">Sample Practitioner</span></a>\n'
        '    <a href="/" class="font-display lowercase">sample practitioner</a>\n'
        "</nav>"
    )
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    assert 'aria-label="Sample Practitioner home"' not in got
    assert "sample practitioner" in got
    assert "brand site-name font-display lowercase" in got


def test_dedupe_skips_nav_link_where_wordmark_is_mid_text():
    """Still a guardrail: a legit nav link like "About Sample Practitioner Studio"
    has the wordmark MID-text, not at the start, must NOT be treated
    as a duplicate brand. Orphan stays, link stays, no consolidation."""
    layout = (
        '<nav class="bg-white">\n'
        '    <a href="/" class="brand site-name font-display" aria-label="Sample Practitioner home"><span class="site-name">Sample Practitioner</span></a>\n'
        '    <a href="/about">About Sample Practitioner Studio</a>\n'
        '    <a href="/events">Events</a>\n'
        "</nav>"
    )
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # Orphan STAYS: no duplicate brand detected.
    assert 'aria-label="Sample Practitioner home"' in got
    # About link intact.
    assert "About Sample Practitioner Studio" in got


def test_dedupe_first_matching_anchor_is_the_one_consolidated():
    """If multiple anchors start with the wordmark (unusual but possible
    when Claude emits a heading + sub-heading pattern), the FIRST one
    wins, deterministic behavior so pipelines stay reproducible."""
    layout = (
        '<nav class="bg-white">\n'
        '    <a href="/" class="brand site-name font-display" aria-label="Sample Practitioner home"><span class="site-name">Sample Practitioner</span></a>\n'
        '    <a href="/" class="first-match font-display">Sample Practitioner Shop</a>\n'
        '    <a href="/admin" class="second-match">Sample Practitioner Admin</a>\n'
        "</nav>"
    )
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # First match consumed the orphan.
    assert "brand site-name first-match font-display" in got
    # Second match still present as a plain anchor.
    assert 'class="second-match"' in got
    assert "Sample Practitioner Admin" in got


def test_dedupe_idempotent_on_second_pass():
    """After consolidation, running the post-processor again must
    leave the file byte-identical (no infinite loops, no double-
    prepending of class names)."""
    layout = (
        '<nav class="bg-white">\n'
        '    <a href="/" class="brand site-name font-display" aria-label="Sample Practitioner home"><span class="site-name">Sample Practitioner</span></a>\n'
        '    <a href="/" class="font-display text-xl">Sample Practitioner Admin</a>\n'
        "</nav>"
    )
    once = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    twice = dict(build._fix_nav_brand_contrast_and_dedupe(
        list(once.items()), SAMPLE_SPEC,
    ))
    assert once["views/layout.ejs"] == twice["views/layout.ejs"]
