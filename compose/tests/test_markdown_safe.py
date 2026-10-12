"""Tests for compose.markdown_safe; faithful-copy markdown renderer.

Brand-voice em-dash carve-out: customer-imported text is exempt from
the no-em-dash rule. Test fixtures here that contain em dashes
represent customer copy and should pass through unchanged.
"""

import re

from compose.markdown_safe import render_markdown_blocks


def test_renders_paragraph_block():
    out = render_markdown_blocks(["A sample paragraph of copy."])
    assert out == "<p>A sample paragraph of copy.</p>"


def test_renders_heading_levels():
    out = render_markdown_blocks(["# Title", "## Subtitle", "### Detail"])
    assert "<h1>Title</h1>" in out
    assert "<h2>Subtitle</h2>" in out
    assert "<h3>Detail</h3>" in out


def test_groups_adjacent_list_items_into_ul():
    out = render_markdown_blocks(["- One", "- Two", "- Three"])
    # All items live inside one <ul>, in order.
    assert out.count("<ul") == 1
    assert out.count("</ul>") == 1
    assert out.index("One") < out.index("Two") < out.index("Three")


def test_separates_lists_when_paragraph_intervenes():
    out = render_markdown_blocks(["- One", "- Two", "Mid paragraph.", "- Three"])
    # Two distinct <ul>s with the paragraph between.
    assert out.count("<ul") == 2
    assert "Mid paragraph" in out


def test_preserves_em_dashes_verbatim():
    em = chr(0x2014)
    fixture = f"Oak floors{em}wool blankets."
    out = render_markdown_blocks([fixture])
    assert em in out
    assert fixture in out


def test_escapes_html_special_chars():
    out = render_markdown_blocks(["Say <script>alert(1)</script> safely."])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_renders_bold_italic_inline():
    out = render_markdown_blocks(["This is **bold** and *italic*."])
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out


def test_renders_safe_links():
    out = render_markdown_blocks(["See [our about](/about) for more."])
    assert '<a href="/about">our about</a>' in out


def test_drops_unsafe_link_hrefs_keeps_text():
    out = render_markdown_blocks([
        "Click [here](javascript:alert(1)) please.",
        "And [data](data:text/html,<script>) too.",
    ])
    assert "javascript:" not in out
    assert "data:" not in out
    assert "here" in out
    assert "data" in out


def test_rejected_link_with_parens_leaves_no_stray_paren():
    # The href group balances one level of parens, so a rejected link
    # whose URL contains parens ("alert(1)") is consumed whole rather than
    # stopping at the first ")" and leaving a malformed "here)" in the output.
    out = render_markdown_blocks(["Click [here](javascript:alert(1)) now."])
    assert "javascript:" not in out
    assert "alert" not in out
    assert "here)" not in out
    assert out == "<p>Click here now.</p>"


def test_keeps_balanced_parens_in_safe_url():
    # A legitimate URL with parens (Wikipedia disambiguation style) is matched
    # whole and preserved, not truncated at the inner ")".
    out = render_markdown_blocks([
        "See [Mercury](https://example.org/wiki/Mercury_(planet)) here.",
    ])
    assert '<a href="https://example.org/wiki/Mercury_(planet)">Mercury</a>' in out


def test_renders_blockquote_with_inline():
    out = render_markdown_blocks(["> A gentle space."])
    assert "<blockquote>A gentle space.</blockquote>" in out


def test_handles_non_list_input_gracefully():
    assert render_markdown_blocks(None) == ""  # type: ignore[arg-type]
    assert render_markdown_blocks("not a list") == ""  # type: ignore[arg-type]
    assert render_markdown_blocks([]) == ""


def test_skips_non_string_entries():
    out = render_markdown_blocks(["valid", 42, None, "also valid"])  # type: ignore[list-item]
    assert "<p>valid</p>" in out
    assert "<p>also valid</p>" in out
    assert "42" not in out


def test_deterministic_output():
    blocks = ["# A", "Para 1.", "- Item 1", "- Item 2", "## B"]
    a = render_markdown_blocks(blocks)
    b = render_markdown_blocks(blocks)
    assert a == b


def test_output_uses_only_allowlisted_tags():
    out = render_markdown_blocks([
        "# Heading",
        "## Sub",
        "Para with **bold** and *italic* and a [link](/x).",
        "- Item",
        "- Item",
        "> Quote",
    ])
    # Strip text content; verify no surprise tags landed.
    tags = set(re.findall(r"<(/?[a-z0-9]+)\b", out))
    allowed = {
        "h1", "/h1", "h2", "/h2", "h3", "/h3", "h4", "/h4", "h5", "/h5", "h6", "/h6",
        "p", "/p", "ul", "/ul", "li", "/li", "blockquote", "/blockquote",
        "strong", "/strong", "em", "/em", "a", "/a",
    }
    surprise = tags - allowed
    assert not surprise, f"unexpected tags emitted: {surprise}"
