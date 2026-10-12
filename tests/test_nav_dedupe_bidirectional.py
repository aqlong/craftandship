"""Tests for the bidirectional widening of
_fix_nav_brand_contrast_and_dedupe.

The widening adds:
- Bidirectional dedupe: match when EITHER anchor's visible text
  starts with the other's (the first version was forward-only).
- Orphan color inheritance: when no match found and the injected
  orphan stays, inject inline color so it inherits nav text color
  (var(--color-on-primary) on primary-bg, var(--color-foreground)
  on white-bg) instead of rendering black by default.

The forward-direction cases remain covered by
tests/test_fix_nav_brand_contrast_and_dedupe.py.
"""

from __future__ import annotations

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


def _layout_with_nav_body(body: str, bg_class: str = "bg-primary") -> str:
    return (
        "<!DOCTYPE html><html><body>\n"
        f'<nav class="{bg_class} sticky top-0">\n'
        f"{body}\n"
        "</nav>\n"
        "</body></html>"
    )


# ---- test 1: forward, claude's anchor starts with injected -------

def test_claude_anchor_extends_injected_wordmark():
    """injected 'Sample Practitioner' + Claude 'Sample Practitioner Admin' → dedupe."""
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Sample Practitioner home"><span class="site-name">Sample Practitioner</span></a>\n'
        '<a href="/dashboard" class="text-xl font-bold">Sample Practitioner Admin</a>'
    )
    layout = _layout_with_nav_body(body)
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # Only ONE brand-class anchor remains after consolidation.
    assert got.count('class="brand site-name') == 1
    # Visible "Sample Practitioner Admin" (Claude's) is kept.
    assert "Sample Practitioner Admin" in got
    # The injected-orphan class signature is not duplicated.
    assert got.count("aria-label=\"Sample Practitioner home\"") == 0


# ---- test 2: reverse, injected starts with claude's --------------

def test_injected_wordmark_extends_claude_anchor():
    """injected 'Sample Practitioner V1 Rc2' + Claude 'Sample Practitioner' → dedupe."""
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Sample Practitioner V1 Rc2 home"><span class="site-name">Sample Practitioner V1 Rc2</span></a>\n'
        '<a href="/dashboard" class="text-xl font-bold text-white">Sample Practitioner</a>'
    )
    layout = _layout_with_nav_body(body)
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # The injected orphan anchor (its aria-label ends in "home") is gone.
    assert "Sample Practitioner V1 Rc2 home" not in got
    # Only ONE brand-class anchor remains.
    assert got.count('class="brand site-name') == 1
    # Visible "Sample Practitioner" (Claude's shorter text) is kept.
    assert "Sample Practitioner" in got


# ---- test 3: exact match prefers claude's anchor ------------------

def test_exact_match_prefers_claude_anchor():
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Sample Practitioner home"><span class="site-name">Sample Practitioner</span></a>\n'
        '<a href="/dashboard" class="text-xl font-bold text-white">Sample Practitioner</a>'
    )
    layout = _layout_with_nav_body(body)
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # Only ONE brand-class anchor remains.
    assert got.count('class="brand site-name') == 1
    # Claude's anchor (text-xl font-bold) kept with brand classes
    # transplanted.
    assert "text-xl font-bold" in got
    # Injected orphan's aria-label signature is removed.
    assert "aria-label=\"Sample Practitioner home\"" not in got


# ---- test 4: no match keeps both (orphan + unrelated anchor) ------

def test_no_match_keeps_orphan_with_color_inherited():
    """When injected orphan has no matching anchor, orphan stays but
    picks up the on-primary color for legibility on primary-bg nav."""
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Sample Practitioner home"><span class="site-name">Sample Practitioner</span></a>\n'
        '<a href="/about" class="text-white">About Us</a>\n'
        '<a href="/contact" class="text-white">Contact</a>'
    )
    layout = _layout_with_nav_body(body)
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # Orphan stays.
    assert 'class="brand site-name' in got
    # Orphan picked up the on-primary color inline style.
    assert "color: var(--color-on-primary" in got
    # Unrelated anchors untouched.
    assert "About Us" in got and "Contact" in got


# ---- test 5: orphan color inheritance on primary bg --------------

def test_orphan_kept_inherits_on_primary_on_primary_bg():
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Home"><span class="site-name">Sample Practitioner</span></a>'
    )
    layout = _layout_with_nav_body(body, bg_class="bg-primary")
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    assert "color: var(--color-on-primary" in got


# ---- test 6: orphan color inheritance on white bg ----------------

def test_orphan_kept_inherits_foreground_on_white_bg():
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Home"><span class="site-name">Sample Practitioner</span></a>'
    )
    # White bg nav: no bg-primary class, no inline primary bg.
    layout = _layout_with_nav_body(body, bg_class="bg-white")
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    assert "color: var(--color-foreground)" in got
    # Should NOT have on-primary (that's for primary-bg nav).
    assert "color: var(--color-on-primary" not in got


# ---- test 7: dedupe fires no residual color on kept --------------

def test_orphan_dropped_when_dedupe_fires_no_color_mutation():
    """When dedupe drops the orphan, the kept anchor doesn't get an
    unrelated color style bolted on. Only the brand classes transplant."""
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Sample Practitioner home"><span class="site-name">Sample Practitioner</span></a>\n'
        '<a href="/dashboard" class="text-xl font-bold text-white">Sample Practitioner Admin</a>'
    )
    layout = _layout_with_nav_body(body)
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # The kept anchor (/dashboard) must not get a color style from the orphan
    # branch. The contrast fix is a separate branch and may add one elsewhere,
    # so only the kept anchor's own tag is checked.
    import re
    m = re.search(r'<a href="/dashboard"[^>]*>', got)
    assert m is not None, "Kept anchor should still be present"
    kept_tag = m.group(0)
    assert 'style="color: var(--color-on-primary' not in kept_tag


# ---- test 8: case-insensitive match -------------------------------

def test_case_insensitive_match():
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Home"><span class="site-name">Sample Practitioner</span></a>\n'
        '<a href="/dashboard" class="text-xl">sample practitioner studio</a>'
    )
    layout = _layout_with_nav_body(body)
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # Dedupe fires because lowercased "sample practitioner studio" starts
    # with lowercased "sample practitioner".
    assert got.count('class="brand site-name') == 1
    assert "sample practitioner studio" in got


# ---- test 9: idempotent reapply -----------------------------------

def test_idempotent_reapply_bidirectional():
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Sample Practitioner V1 Rc2 home"><span class="site-name">Sample Practitioner V1 Rc2</span></a>\n'
        '<a href="/dashboard" class="text-xl">Sample Practitioner</a>'
    )
    layout = _layout_with_nav_body(body)
    once = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    twice = dict(build._fix_nav_brand_contrast_and_dedupe(
        list(once.items()), SAMPLE_SPEC,
    ))
    assert once["views/layout.ejs"] == twice["views/layout.ejs"]


# ---- test 10: orphan color inheritance idempotent -----------------

def test_orphan_color_injection_idempotent():
    """Running twice on the orphan-kept branch doesn't double-inject
    the color style."""
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Home"><span class="site-name">Sample Practitioner</span></a>\n'
        '<a href="/about" class="text-white">About Us</a>'
    )
    layout = _layout_with_nav_body(body)
    once = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    twice = dict(build._fix_nav_brand_contrast_and_dedupe(
        list(once.items()), SAMPLE_SPEC,
    ))
    got_once = once["views/layout.ejs"]
    got_twice = twice["views/layout.ejs"]
    # Second pass must be idempotent.
    assert got_once == got_twice
    # Color declaration appears exactly once in the orphan anchor.
    # (The nav may have other --color-on-primary appearances from
    # the contrast-fix (a); we scope to the brand anchor.)
    import re
    orphan_match = re.search(
        r'<a[^>]*class="brand site-name[^"]*"[^>]*>',
        got_twice,
    )
    assert orphan_match is not None
    orphan_tag = orphan_match.group(0)
    assert orphan_tag.count("color: var(--color-on-primary") == 1


# ---- F2 regression guards: contrast regex must not match inside
# background-color, border-color, outline-color, etc.


def test_f2_background_color_not_rewritten_on_primary_bg_nav():
    """Login-style button inside primary-bg nav: style has
    `background-color: var(--color-primary)`. Contrast fix must NOT
    rewrite it, doing so turns bg white and makes the button
    invisible (white-on-white), which is the
    regression this test guards against."""
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Home"><span class="site-name">Sample Practitioner</span></a>\n'
        '<a href="/login" class="text-white px-4 py-2 rounded-md" '
        'style="background-color: var(--color-primary);">Login</a>'
    )
    layout = _layout_with_nav_body(body, bg_class="bg-primary")
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # The login button's background-color stayed as var(--color-primary).
    assert "background-color: var(--color-primary)" in got, (
        "background-color was rewritten when it shouldn't have been"
    )
    # Sanity: the contrast fix did not accidentally blank the bg.
    assert "background-color: var(--color-on-primary" not in got


def test_f2_border_color_not_rewritten_on_primary_bg_nav():
    """Same guard for border-color."""
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Home"><span class="site-name">Sample Practitioner</span></a>\n'
        '<a href="/pill" class="text-white" '
        'style="border-color: var(--color-primary); color: var(--color-on-primary);">Pill</a>'
    )
    layout = _layout_with_nav_body(body, bg_class="bg-primary")
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    assert "border-color: var(--color-primary)" in got


def test_f2_outline_color_not_rewritten_on_primary_bg_nav():
    """Same guard for outline-color (focus rings etc)."""
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Home"><span class="site-name">Sample Practitioner</span></a>\n'
        '<a href="/focus" style="outline-color: var(--color-foreground);">X</a>'
    )
    layout = _layout_with_nav_body(body, bg_class="bg-primary")
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    assert "outline-color: var(--color-foreground)" in got


def test_f2_color_standalone_still_rewritten_on_primary_bg_nav():
    """Regression safety net: the fix must still rewrite standalone
    `color:` declarations (the original use case). Only the
    compound-property forms should be skipped."""
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Home"><span class="site-name">Sample Practitioner</span></a>\n'
        '<a href="/link" style="color: var(--color-primary);">Link</a>'
    )
    layout = _layout_with_nav_body(body, bg_class="bg-primary")
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # Standalone color: was rewritten to on-primary (invisible-on-maroon
    # before; white-on-maroon after).
    assert "color: var(--color-on-primary" in got
    # The pre-rewrite token is gone.
    import re
    # Make sure no standalone `color: var(--color-primary)` remains, any
    # surviving match would be inside a compound property (which is fine).
    for m in re.finditer(r"color:\s*var\(--color-primary", got):
        start = m.start()
        # Check the 1 char before "color:", must be a letter/hyphen (else
        # this is a standalone color: that we failed to rewrite).
        if start > 0:
            prev = got[start - 1]
            assert prev.isalpha() or prev == '-', (
                f"standalone `color: var(--color-primary)` survived at "
                f"offset {start}; surroundings: {got[max(0, start-10):start+30]!r}"
            )


def test_f2_compound_property_preserved_with_color_standalone_rewrite():
    """Both forms on the same element: standalone color: rewritten,
    compound background-color: preserved."""
    body = (
        '<a href="/" class="brand site-name font-display" '
        'aria-label="Home"><span class="site-name">Sample Practitioner</span></a>\n'
        '<a href="/combo" style="background-color: var(--color-primary); '
        'color: var(--color-foreground);">Combo</a>'
    )
    layout = _layout_with_nav_body(body, bg_class="bg-primary")
    out = dict(build._fix_nav_brand_contrast_and_dedupe(
        [("views/layout.ejs", layout)], SAMPLE_SPEC,
    ))
    got = out["views/layout.ejs"]
    # background-color preserved.
    assert "background-color: var(--color-primary)" in got
    # standalone color: foreground → on-primary.
    assert "color: var(--color-on-primary" in got
    assert "color: var(--color-foreground)" not in got
