"""compose.markdown_safe: bounded markdown -> HTML renderer.

Purpose
=======
Faithful-copy mode (v2-faithful-site-copy-mode) imports verbatim text
from a customer's existing live website. The crawler in
`web/src/services/site_copy_crawl.ts` produces ordered `markdown_blocks`
list; this module converts those blocks into the safe HTML the
generated app's view templates render.

This is intentionally NOT a general-purpose markdown library. The input
shape is constrained:
  - Each block is a SELF-CONTAINED string (paragraph, heading, list
    item, blockquote, code line). The crawler never emits multi-block
    markdown in a single string.
  - Block-level prefix tokens (#, ##, -, >) are recognized and rendered
    to bounded HTML tags (h1..h6, ul/li, blockquote, p).
  - Inline markdown is supported for **bold**, *italic*, [text](href).
  - Hyperlinks are restricted to the safe scheme allowlist.
  - Everything else is HTML-escaped.

`sanitize_html_fragment` is the companion for the one field that arrives as
HTML rather than markdown (the faithful-copy hero `body_html`): it reduces
an untrusted HTML string to a few attribute-free tags.

Brand-voice em-dash carve-out
=============================
Customer-imported text frequently contains em dashes (U+2014). The
no-em-dash brand-voice rule applies to CAS internal source only; the
customer's own copy is exempt. This module never strips, replaces, or
flags em dashes. Test fixtures in compose/tests/test_markdown_safe.py mark
their em-dash usage with the brand-voice-allow-em-dash comment for the
CAS lint that runs on git-tracked CAS files.

Determinism
===========
Same input -> byte-identical output. No randomness, no clock reads,
no environment-dependent decisions.
"""

from __future__ import annotations

import html
import re
from typing import List

# Allowed link schemes. Anything not matching is rendered as plain text
# (the link label is preserved; the href is dropped).
_SAFE_HREF_RE = re.compile(
    # A leading "/" must be a path, not "//host" or "/\\host": browsers read
    # both of those as a protocol-relative URL to another origin.
    r"^(https?:|sms:|tel:|mailto:|/(?![/\\])|\.{1,2}/|#)",
    re.IGNORECASE,
)
# Bare-relative paths like "about.html" without a leading scheme or slash
# (the first character is restricted for the same "//host" reason).
_BARE_RELATIVE_RE = re.compile(r"^[a-z0-9_][a-z0-9_./?=&%-]*$", re.IGNORECASE)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_ITEM_RE = re.compile(r"^-\s+(.*)$")
_BLOCKQUOTE_RE = re.compile(r"^>\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\w)")
# Href group allows one level of balanced parens so "alert(1)" or a
# Wikipedia "(disambiguation)" URL is consumed whole, rather than stopping
# at the first ")" and leaving a stray ")" in the output.
#
# The label excludes "[" as well as "]". That keeps matching linear in the
# input: a label that could contain "[" lets every "[" of a long run start
# a match that scans to the end of the block before failing, which is
# quadratic (the build worker renders blocks from an imported site, and the
# engine does not cap a block's length). The one visible effect is that in
# "[a [b](url)" the link is "b", as markdown readers expect, not "a [b".
_LINK_RE = re.compile(r"\[([^\[\]]+)\]\(([^()\s]*(?:\([^()]*\)[^()\s]*)*)\)")


def render_markdown_blocks(blocks: List[str]) -> str:
    """Render an ordered list of markdown blocks into a single HTML
    fragment. Adjacent list items are grouped under a single <ul>; all
    other blocks become individual elements separated by newlines.

    The returned string is suitable for direct emission into an EJS
    template via `<%- copy_html %>`. It contains only the bounded set
    of tags this module produces.
    """
    if not isinstance(blocks, list):
        return ""
    parts: List[str] = []
    pending_list: List[str] = []

    def flush_list() -> None:
        if not pending_list:
            return
        items = "".join(f"<li>{item}</li>" for item in pending_list)
        parts.append(f"<ul class=\"copy-list\">{items}</ul>")
        pending_list.clear()

    for raw in blocks:
        if not isinstance(raw, str):
            continue
        line = raw.strip()
        if not line:
            continue
        m = _LIST_ITEM_RE.match(line)
        if m:
            pending_list.append(_render_inline(m.group(1)))
            continue
        flush_list()
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            inner = _render_inline(m.group(2))
            parts.append(f"<h{level}>{inner}</h{level}>")
            continue
        m = _BLOCKQUOTE_RE.match(line)
        if m:
            inner = _render_inline(m.group(1))
            parts.append(f"<blockquote>{inner}</blockquote>")
            continue
        # Default: paragraph. Preserve internal newlines as <br>.
        inner = _render_inline(line).replace("\n", "<br>")
        parts.append(f"<p>{inner}</p>")
    flush_list()
    return "\n".join(parts)


# Tags a raw-HTML widget parameter may carry (hero `body_html`): structure
# and emphasis only, and only WITHOUT attributes. No links, images, style,
# script, form or embed elements, and no event handlers, because no
# attribute of any kind survives.
_FRAGMENT_TAG_RE = re.compile(
    r"&lt;(/?)(p|br|em|strong|i|b|ul|ol|li|blockquote)\s*(/?)&gt;",
    re.IGNORECASE,
)


def sanitize_html_fragment(fragment: object, max_len: int = 8000) -> str:
    """Reduce an untrusted HTML string to a small, attribute-free tag set.

    Used for values a widget emits through EJS's raw output tag
    (`<%- body_html %>` in hero/centered.ejs). The faithful-copy crawler
    produces `<p>escaped text</p>` blocks for that field, but the value
    travels inside the spec, and nothing between the crawler and the
    engine re-checks it, so the engine sanitizes at the point of use.

    Method (allowlist by reconstruction, not blocklist by removal):
      1. Decode entities once, so already-escaped input is not escaped
         twice ("Tom &amp; Jerry" must not render as "Tom &amp;amp; Jerry").
      2. Escape EVERYTHING. After this step the string contains no markup.
      3. Turn back only the exact escaped forms of allowlisted tags with
         no attributes (`&lt;p&gt;` -> `<p>`).

    Anything else, including `<script>`, `<a href>`, `<img onerror>`,
    `<base>`, `<form>`, `<style>` and unknown tags, stays as inert escaped
    text. The function is idempotent, and same input gives byte-identical
    output.
    """
    if not isinstance(fragment, str):
        return ""
    text = fragment[:max_len]
    escaped = html.escape(html.unescape(text), quote=True)
    return _FRAGMENT_TAG_RE.sub(
        lambda m: f"<{m.group(1)}{m.group(2).lower()}{m.group(3)}>", escaped
    )


def _render_inline(text: str) -> str:
    """Convert inline markdown to safe HTML.

    Order matters:
      1. HTML-escape everything (inserts &amp; &lt; &gt; etc).
      2. Replace [text](href) using the original (escaped) text.
         Unsafe hrefs render as the plain link text only.
      3. Replace **bold** -> <strong>, *italic* -> <em>.
    """
    escaped = html.escape(text, quote=True)

    def link_repl(match: re.Match) -> str:
        label = match.group(1)
        href = match.group(2)
        # Both label and href came from a re.match on the ESCAPED string,
        # so we don't re-escape them. We only validate the href.
        # Unescape any &amp; the escaper wrapped in the href so the
        # output is a clean URL the browser handles, then re-escape.
        raw_href = html.unescape(href)
        if _SAFE_HREF_RE.match(raw_href) or _BARE_RELATIVE_RE.match(raw_href):
            safe = html.escape(raw_href, quote=True)
            return f'<a href="{safe}">{label}</a>'
        return label

    escaped = _LINK_RE.sub(link_repl, escaped)
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _ITALIC_RE.sub(r"<em>\1</em>", escaped)
    return escaped
