"""Tests for _ensure_layout_html_skeleton.

Claude emitted `views/layout-public.ejs` as a bare fragment starting
with `<nav class="bg-white">`, no DOCTYPE, no <html>, no <head>, no
<body>. Every public page that rendered with `layout: 'layout-public'`
produced HTML with no stylesheet links, so Tailwind utility classes
did nothing and the whole site rendered as unstyled black-on-dark
text. This post-processor wraps any `views/layout*.ejs` file that
lacks a DOCTYPE with the canonical HTML5 skeleton, preserving
Claude's content between the wrapper tags.
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


def test_bare_fragment_gets_wrapped_with_doctype_and_head():
    bare = (
        '<nav class="bg-white">\n'
        '  <a href="/">Home</a>\n'
        '</nav>\n'
        '<main><%- body %></main>\n'
        '<footer>© 2026</footer>\n'
    )
    out = dict(build._ensure_layout_html_skeleton(
        [("views/layout-public.ejs", bare)], None,
    ))
    got = out["views/layout-public.ejs"]
    # DOCTYPE + html + head + body wrapping the original content.
    assert got.startswith("<!DOCTYPE html>")
    assert '<html lang="en">' in got
    assert '<head>' in got and '</head>' in got
    # Stylesheet links injected.
    assert 'href="/tailwind.css"' in got
    assert 'href="/style.css"' in got
    assert 'href="/premium-base.css"' in got
    # Body tag wraps content.
    assert '<body' in got
    assert '</body>' in got
    assert '</html>' in got
    # Original content preserved between the tags.
    assert '<nav class="bg-white">' in got
    assert '<%- body %>' in got
    assert '© 2026' in got


def test_existing_doctype_layout_untouched():
    """Canonical layout.ejs starts with DOCTYPE, the wrapper must
    NOT double-wrap a valid layout."""
    good = (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head><title>X</title></head>\n'
        '<body><%- body %></body>\n'
        '</html>\n'
    )
    out = build._ensure_layout_html_skeleton(
        [("views/layout.ejs", good)], None,
    )
    # Unchanged: no-op.
    assert dict(out)["views/layout.ejs"] == good


def test_existing_html_but_no_doctype_also_untouched():
    """Slightly off-spec but valid (missing DOCTYPE, has <html>);
    still skip the wrapper since wrapping would double-nest."""
    partial = (
        '<html>\n'
        '<head></head>\n'
        '<body><%- body %></body>\n'
        '</html>\n'
    )
    out = build._ensure_layout_html_skeleton(
        [("views/layout.ejs", partial)], None,
    )
    assert dict(out)["views/layout.ejs"] == partial


def test_wraps_all_layout_star_ejs_files():
    """Every file matching `views/layout*.ejs` gets checked, so
    layout.ejs AND layout-public.ejs AND layout-admin.ejs are all
    covered by the same pass."""
    files = [
        ("views/layout.ejs", "<!DOCTYPE html><html><body><%- body %></body></html>"),
        ("views/layout-public.ejs", "<nav>bare</nav><%- body %>"),
        ("views/layout-admin.ejs", "<nav>also bare</nav><%- body %>"),
        ("views/home.ejs", "<h1>not a layout</h1>"),
    ]
    out = dict(build._ensure_layout_html_skeleton(files, None))
    # Canonical layout untouched.
    assert out["views/layout.ejs"] == files[0][1]
    # Both broken public/admin layouts wrapped.
    assert out["views/layout-public.ejs"].startswith("<!DOCTYPE html>")
    assert out["views/layout-admin.ejs"].startswith("<!DOCTYPE html>")
    # Non-layout file untouched.
    assert out["views/home.ejs"] == "<h1>not a layout</h1>"


def test_idempotent_on_second_pass():
    """After the first wrap, the second pass sees the DOCTYPE and
    leaves the file byte-identical."""
    bare = '<nav>minimal</nav><%- body %>'
    once = dict(build._ensure_layout_html_skeleton(
        [("views/layout-x.ejs", bare)], None,
    ))
    twice = dict(build._ensure_layout_html_skeleton(
        list(once.items()), None,
    ))
    assert once["views/layout-x.ejs"] == twice["views/layout-x.ejs"]


def test_registered_in_expected_processors():
    import pipeline
    assert "_ensure_layout_html_skeleton" in pipeline.REGISTRY
    assert "_ensure_layout_html_skeleton" in pipeline.EXPECTED_PROCESSORS
    proc = pipeline.REGISTRY["_ensure_layout_html_skeleton"]
    assert "build" in proc.phase and "reapply" in proc.phase


# ---------------------------------------------------------------------------
# Regression: wrapped body must carry the same
# classes as the canonical layout.ejs body so Tailwind surface + text
# tokens resolve correctly. An unstyled "black-on-white page of text"
# was the original symptom: missing bg-secondary + text-foreground on
# the wrapped body means the customer sees raw HTML.
# ---------------------------------------------------------------------------


def test_wrapped_body_has_canonical_surface_classes():
    """Wrapped `<body>` must include `bg-secondary` and `text-foreground`
    so Tailwind surface + text tokens match the canonical layout.ejs.
    Without this the public pages render with no background tint and
    default browser text color."""
    bare = '<nav>content</nav><%- body %>'
    out = dict(build._ensure_layout_html_skeleton(
        [("views/layout-public.ejs", bare)], None,
    ))
    got = out["views/layout-public.ejs"]
    # Body tag opens: regex-free substring check since template is
    # fixed.
    body_open_start = got.find("<body")
    body_open_end = got.find(">", body_open_start)
    body_tag = got[body_open_start:body_open_end + 1]
    # Surface + text tokens in the body class attr.
    assert "bg-secondary" in body_tag, (
        f"wrapped body missing bg-secondary: {body_tag!r}"
    )
    assert "text-foreground" in body_tag, (
        f"wrapped body missing text-foreground: {body_tag!r}"
    )
    # Also the base layout essentials.
    assert "font-body" in body_tag
    assert "min-h-screen" in body_tag
    assert "flex flex-col" in body_tag


def test_wrapped_body_has_inline_background_style_fallback():
    """Inline `style="background-color: var(--color-background);"` on
    the body is a belt-and-suspenders fallback for when Tailwind hasn't
    compiled yet or the CSS is mid-load. Must be present."""
    bare = '<header>x</header><%- body %>'
    out = dict(build._ensure_layout_html_skeleton(
        [("views/layout-public.ejs", bare)], None,
    ))
    got = out["views/layout-public.ejs"]
    assert "background-color: var(--color-background)" in got


def test_wrapped_body_contains_head_stylesheets_before_body():
    """Belt-and-suspenders: the stylesheet <link> tags must come
    BEFORE the body content, inside `<head>`, Tailwind & the premium
    base layer need to load first."""
    bare = '<nav>n</nav><%- body %>'
    out = dict(build._ensure_layout_html_skeleton(
        [("views/layout-public.ejs", bare)], None,
    ))
    got = out["views/layout-public.ejs"]
    head_end = got.find("</head>")
    body_start = got.find("<body")
    tailwind_pos = got.find('href="/tailwind.css"')
    assert 0 < tailwind_pos < head_end < body_start, (
        "tailwind.css must be inside <head> and before <body>"
    )


# ---------------------------------------------------------------------------
# Review cycle 2 (2026-04-20): defensive wrapping, partial-skeleton
# files must NOT be double-wrapped into nested <head>/<body>.
# ---------------------------------------------------------------------------


def test_partial_skeleton_with_head_tag_not_wrapped():
    """Claude emits a file that starts with `<head>...</head>` but
    no `<!DOCTYPE>`, wrapping would produce nested `<head>` tags.
    Skip the file entirely (no-op), let contracts surface the issue."""
    partial = (
        '<head><title>X</title></head>\n'
        '<body><%- body %></body>\n'
    )
    out = dict(build._ensure_layout_html_skeleton(
        [("views/layout-weird.ejs", partial)], None,
    ))
    got = out["views/layout-weird.ejs"]
    # Byte-identical: no wrapping.
    assert got == partial
    # Exactly one <head> (no nesting).
    assert got.lower().count("<head>") == 1
    assert got.lower().count("<body>") == 1


def test_partial_skeleton_with_only_body_tag_not_wrapped():
    """Only `<body>...</body>` present → still skeleton-partial → skip.
    Without this guard we'd end up with `<body><body>...</body></body>`
    which is invalid HTML and breaks rendering."""
    partial = '<body><%- body %></body>'
    out = dict(build._ensure_layout_html_skeleton(
        [("views/layout-x.ejs", partial)], None,
    ))
    got = out["views/layout-x.ejs"]
    assert got == partial
    assert got.lower().count("<body>") == 1


def test_header_tag_is_NOT_confused_with_head_tag():
    """A bare fragment starting with `<header>` (semantic HTML5 nav
    container) must STILL be wrapped, `<header>` is not `<head>`.
    Without the word-boundary check, the older `<head` substring
    guard would false-positive and leave the fragment unwrapped."""
    bare = (
        '<header class="bg-white"><nav><a href="/">Home</a></nav></header>\n'
        '<main><%- body %></main>\n'
    )
    out = dict(build._ensure_layout_html_skeleton(
        [("views/layout-public.ejs", bare)], None,
    ))
    got = out["views/layout-public.ejs"]
    # Wrapping fired.
    assert got.startswith("<!DOCTYPE html>")
    # Original <header> preserved (only one, not nested).
    assert got.count("<header") == 1
    # Canonical body/head present.
    assert "<body" in got
    assert "<head>" in got


def test_mixed_case_doctype_still_treated_as_skeleton():
    """HTML tokens are case-insensitive. `<!DocType HTML>` (mixed case)
    must still be recognized as an existing skeleton → no wrap."""
    good = '<!DocType HTML>\n<html><body><%- body %></body></html>'
    out = dict(build._ensure_layout_html_skeleton(
        [("views/layout.ejs", good)], None,
    ))
    got = out["views/layout.ejs"]
    assert got == good


def test_fragment_with_closing_body_no_doctype_also_skipped():
    """Defensive: a fragment that contains `</body>` but no opening
    `<body>` is malformed, skip (no double-wrap). Claude's bad
    emissions can include close-only tags."""
    malformed = '<section>some content</section></body>'
    out = dict(build._ensure_layout_html_skeleton(
        [("views/layout-broken.ejs", malformed)], None,
    ))
    # No-op; the file is handed off unchanged for contracts to flag.
    assert dict(out)["views/layout-broken.ejs"] == malformed
