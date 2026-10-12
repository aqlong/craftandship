"""Tests for _scrub_em_dashes_in_ejs (em-dash-scrubber-postprocessor).

Regression: U+2014 em-dashes leaked onto a
homepage because the existing contract only fires on <nav> text. Scope
widening ships as a canonical @processor (Standard-Widget Doctrine) instead of
broadening the contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


# ---------------------------------------------------------------------------
# Pure text scrubbing helper (exhaustive cases; fast, no pipeline setup)
# ---------------------------------------------------------------------------


def test_scrub_space_surrounded_em_dash_becomes_comma():
    """' \u2014 ' (space-emdash-space) maps to ', ' \u2014 prose comma."""
    assert build._scrub_em_dashes_in_text("Hello \u2014 world") == "Hello, world"


def test_scrub_bare_em_dash_becomes_hyphen():
    """'Sun\u2014Fri' or '3\u20145pm' maps to ' - ' \u2014 preserves range meaning."""
    assert build._scrub_em_dashes_in_text("Sun\u2014Fri") == "Sun - Fri"
    assert build._scrub_em_dashes_in_text("3\u20145pm") == "3 - 5pm"


def test_scrub_leaves_text_without_em_dashes_unchanged():
    sample = "Regular text, no em dash at all."
    assert build._scrub_em_dashes_in_text(sample) == sample


def test_scrub_handles_multiple_em_dashes_in_one_line():
    out = build._scrub_em_dashes_in_text("A \u2014 B \u2014 C")
    assert out == "A, B, C"


# ---------------------------------------------------------------------------
# Processor applied to EJS files: covers nav, heading, paragraph, and the
# code-block skip rules.
# ---------------------------------------------------------------------------


def _run_scrubber(file_dict: dict) -> dict:
    """Helper: call the @processor with a list-of-tuples artifact shape and
    return the post-processed dict."""
    files = list(file_dict.items())
    out = build._scrub_em_dashes_in_ejs(files)
    return dict(out)


def test_replaces_em_dashes_in_nav_heading_paragraph():
    src = (
        "<nav><a>Home</a> \u2014 <a>About</a></nav>\n"
        "<h1>Welcome \u2014 the journey begins</h1>\n"
        "<p>Classes meet Sun\u2014Fri for anyone.</p>\n"
    )
    out = _run_scrubber({"views/home.ejs": src})
    text = out["views/home.ejs"]
    # Prose em-dashes in nav + heading became commas.
    assert "<a>Home</a>, <a>About</a></nav>" in text
    assert "<h1>Welcome, the journey begins</h1>" in text
    # Range em-dash became hyphen with spaces.
    assert "Sun - Fri" in text
    assert "\u2014" not in text


def test_preserves_em_dashes_in_script_tag():
    src = (
        "<h1>Hello</h1>\n"
        "<script>\n"
        "  // comment with \u2014 em-dash inside JS code\n"
        "  var x = 'one \u2014 two';\n"
        "</script>\n"
        "<p>Prose with \u2014 em-dash outside</p>\n"
    )
    out = _run_scrubber({"views/demo.ejs": src})
    text = out["views/demo.ejs"]
    # Script-internal em-dashes survive: these are JS code, not user text.
    assert "comment with \u2014 em-dash inside JS code" in text
    assert "var x = 'one \u2014 two';" in text
    # Prose outside the script was scrubbed.
    assert "<p>Prose with, em-dash outside</p>" in text


def test_preserves_em_dashes_in_style_tag():
    src = (
        "<style>\n"
        "  /* design note: \u2014 kept for contrast */\n"
        "  .x { color: #000; }\n"
        "</style>\n"
        "<p>Pricing \u2014 details below.</p>\n"
    )
    out = _run_scrubber({"views/layout.ejs": src})
    text = out["views/layout.ejs"]
    assert "design note: \u2014 kept for contrast" in text
    assert "<p>Pricing, details below.</p>" in text


def test_preserves_em_dashes_in_html_comments():
    src = (
        "<!-- Nav: A \u2014 B spacing comment -->\n"
        "<nav>A \u2014 B</nav>\n"
    )
    out = _run_scrubber({"views/home.ejs": src})
    text = out["views/home.ejs"]
    assert "<!-- Nav: A \u2014 B spacing comment -->" in text
    assert "<nav>A, B</nav>" in text


def test_preserves_em_dashes_in_ejs_code_blocks():
    """EJS code blocks (<% %>, <%= %>, <%- %>) must not have their inner
    em-dashes rewritten \u2014 they are JS/string literals, not user text."""
    src = (
        "<% var label = 'Hours: 9am \u2014 5pm'; %>\n"
        "<%= label %>\n"
        "<%- rawString %>\n"
        "<p>See hours \u2014 above.</p>\n"
    )
    out = _run_scrubber({"views/contact.ejs": src})
    text = out["views/contact.ejs"]
    # Inside <% %> block: em-dash preserved.
    assert "var label = 'Hours: 9am \u2014 5pm';" in text
    # Prose outside EJS: em-dash replaced with comma.
    assert "<p>See hours, above.</p>" in text


def test_preserves_em_dashes_between_ejs_expressions():
    """Text between two <%= %> expressions IS prose. Em-dashes there
    should still be scrubbed."""
    src = "<h1><%= title %> \u2014 <%= subtitle %></h1>\n"
    out = _run_scrubber({"views/page.ejs": src})
    text = out["views/page.ejs"]
    assert "<h1><%= title %>, <%= subtitle %></h1>" in text


def test_skips_non_ejs_files():
    """Only .ejs files are scrubbed. Raw JS / MD / JSON are untouched
    so code comments + external data keep their punctuation."""
    js = "// hello \u2014 world\nconst x = 'a \u2014 b';\n"
    out = _run_scrubber({
        "routes/foo.js": js,
        "README.md": "Text \u2014 prose",
    })
    assert out["routes/foo.js"] == js
    assert out["README.md"] == "Text \u2014 prose"


def test_idempotent_no_em_dashes_left():
    """Running the scrubber twice is a byte-identical no-op on the 2nd pass."""
    src = "<p>Hello \u2014 world</p>"
    once = _run_scrubber({"views/x.ejs": src})["views/x.ejs"]
    twice = _run_scrubber({"views/x.ejs": once})["views/x.ejs"]
    assert once == twice
    assert "\u2014" not in twice


def test_unclosed_ejs_block_fails_safely():
    """Malformed EJS with an unclosed `<%` (Claude sometimes emits this)
    MUST NOT cause the scrubber to swallow the rest of the file. The
    non-greedy `<%.*?%>` regex cannot match without a closing `%>`, so
    the opening `<%` stays in the 'text' segment. Em-dashes OUTSIDE the
    unclosed block (before the `<%`) should still get scrubbed; em-
    dashes INSIDE the unclosed tail are best-effort: the existing
    `_validate_ejs_templates` processor flags malformed EJS upstream,
    so the scrubber's job here is to NOT crash or corrupt output."""
    src = (
        "<p>Before \u2014 prose</p>\n"
        "<% if (broken /* no close tag */\n"
        "<p>After unclosed \u2014 more prose</p>\n"
    )
    out = _run_scrubber({"views/broken.ejs": src})
    text = out["views/broken.ejs"]
    # Scrubber did not crash (basic smoke).
    assert isinstance(text, str)
    # Em-dash before the unclosed `<%` was scrubbed to a comma.
    assert "<p>Before, prose</p>" in text
    # The rest of the file is preserved; we do NOT assert on the em-dash
    # inside the unclosed block because that's upstream-validated.
    assert "<% if (broken" in text


# ---------------------------------------------------------------------------
# Pipeline registration + ordering
# ---------------------------------------------------------------------------


def test_scrubber_registered_for_build_and_reapply():
    import pipeline
    assert "_scrub_em_dashes_in_ejs" in pipeline.REGISTRY
    proc = pipeline.REGISTRY["_scrub_em_dashes_in_ejs"]
    # Fix loops can leak em-dashes via qa_fix rewrites; scrubber MUST run
    # in both phases so reapply catches them.
    assert "build" in proc.phase
    assert "reapply" in proc.phase


def test_scrubber_is_in_expected_processors():
    """EXPECTED_PROCESSORS guardrail requires an explicit entry for the
    scrubber so registration drift fails fast at test-collection."""
    import pipeline
    assert "_scrub_em_dashes_in_ejs" in pipeline.EXPECTED_PROCESSORS


def test_scrubber_runs_after_strip_require_from_ejs():
    """Ordering: em-dash scrub
    runs after the require() stripper so any em-dashes inside require
    lines that got removed don't need to be re-processed."""
    import pipeline
    order = pipeline.pipeline_order("build")
    strip_pos = order.index("_strip_require_from_ejs")
    scrub_pos = order.index("_scrub_em_dashes_in_ejs")
    assert strip_pos < scrub_pos


def test_scrubber_runs_before_resilient_migrate():
    """_ensure_resilient_migrate is registered with after=('_scrub_em_dashes_in_ejs',),
    so the scrubber is wedged between strip_require and resilient_migrate."""
    import pipeline
    order = pipeline.pipeline_order("build")
    scrub_pos = order.index("_scrub_em_dashes_in_ejs")
    migrate_pos = order.index("_ensure_resilient_migrate")
    assert scrub_pos < migrate_pos
