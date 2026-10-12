"""Bug 3 (seed.sql scanner): seed.sql multi-line
INSERTs with embedded apostrophes must NOT be false-positively flagged
as unbalanced quotes and dropped into -- SKIPPED comments.

Before this fix, cli/build.py's _check_quotes did
`stmt.replace('\\'\\'', '').count('\\'') % 2`, which falsely flagged
statements where single-quote-pair escaping inside a string interleaved
with statement-level quotes in a shape that produced an odd raw count.

Example that the old check DID flag:
  INSERT INTO blog_posts (title, body) VALUES
    ('Sample Post Title',
     'This sample body mentions a reader''s question, '
     'then keeps going on a second line.');

The new helper `_sql_stmt_strings_closed` uses a proper SQL-aware state
machine that handles `''` as an escape pair.
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


# ---------------------------------------------------------------------------
# _sql_stmt_strings_closed scanner: unit tests
# ---------------------------------------------------------------------------


def test_balanced_single_string():
    assert build._sql_stmt_strings_closed("'hello';")


def test_balanced_pair_of_strings():
    assert build._sql_stmt_strings_closed("INSERT INTO t VALUES ('a', 'b');")


def test_balanced_escaped_apostrophe():
    """Claude correctly doubles the apostrophe: `women's` → `women''s`.
    The scanner must recognize `''` as an escape inside a string."""
    assert build._sql_stmt_strings_closed(
        "INSERT INTO t VALUES ('women''s circle', 'other');"
    )


def test_balanced_multiple_escaped_apostrophes():
    assert build._sql_stmt_strings_closed(
        "INSERT INTO t VALUES ('don''t', 'won''t', 'can''t');"
    )


def test_balanced_multiline_string_with_embedded_apostrophes():
    """Real-world blog-post body: multi-line, multiple `''` escapes."""
    stmt = (
        "INSERT INTO blog_posts (title, body) VALUES\n"
        "  ('Full Moon Rituals',\n"
        "   'Women''s circles meet under the full moon.\n"
        "We share stories and release what''s ready to go.\n"
        "You''ll leave grounded.');"
    )
    assert build._sql_stmt_strings_closed(stmt)


def test_unbalanced_truly_unclosed_string():
    """Claude truncates a string mid-value, scanner correctly flags
    this as unbalanced (we still WANT to SKIP truly broken SQL)."""
    stmt = "INSERT INTO t VALUES ('unclosed"
    assert not build._sql_stmt_strings_closed(stmt)


def test_semicolon_inside_string_doesnt_break_scanner():
    """A `;` inside a quoted string is literal; the scanner never
    treats it as a statement terminator."""
    assert build._sql_stmt_strings_closed(
        "INSERT INTO t VALUES ('value with ; in it');"
    )


def test_adjacent_string_concatenation_sql_standard():
    """SQL string concat across whitespace: 'a' 'b' → 'ab'. The
    scanner treats each as its own pair of quotes, still balanced."""
    assert build._sql_stmt_strings_closed(
        "INSERT INTO t VALUES ('a' 'b' 'c');"
    )


# ---------------------------------------------------------------------------
# End-to-end: _split_seed_inserts preserves multi-line INSERT with
# embedded apostrophes
# ---------------------------------------------------------------------------


def test_multiline_blog_post_insert_survives_scanner():
    """Integration: a realistic Claude-emitted blog_posts INSERT with
    a body paragraph containing `reader''s` (properly escaped) passes
    through _split_seed_inserts unchanged, NOT dropped as SKIPPED."""
    seed = (
        "INSERT INTO blog_posts (title, body) VALUES\n"
        "  ('Sample Post Title',\n"
        "   'This sample body mentions a reader''s question, then keeps "
        "going on a second line.');\n"
    )
    out = dict(build._split_seed_inserts([("db/seed.sql", seed)]))
    got = out["db/seed.sql"]
    # NOT flagged.
    assert "SKIPPED (unbalanced quotes)" not in got
    # Content preserved.
    assert "Sample Post Title" in got
    assert "reader''s question" in got


def test_events_insert_with_embedded_apostrophe_survives():
    """A multi-line INSERT for an event titled
    `Sample Share & Community Gathering` with a description containing
    an apostrophe (escaped)."""
    seed = (
        "INSERT INTO events (title, description, location) VALUES\n"
        "  ('Sample Share & Community Gathering',\n"
        "   'Open to all levels. We''ll start with a short welcome\n"
        "and a round of names, and end with tea.',\n"
        "   'The Organizer''s Sample Studio');\n"
    )
    out = dict(build._split_seed_inserts([("db/seed.sql", seed)]))
    got = out["db/seed.sql"]
    assert "SKIPPED (unbalanced quotes)" not in got
    assert "Sample Share" in got
    assert "We''ll start" in got


def test_truly_broken_insert_still_flagged():
    """Counterweight: an INSERT Claude actually broke (mid-string
    truncation) is STILL flagged. The fix is not to swallow all
    quote-parse failures, it's to stop flagging valid ones."""
    seed = (
        "INSERT INTO blog_posts (title, body) VALUES\n"
        "  ('Broken post', 'this string is never closed\n"
        "and the next insert is swallowed);\n"
    )
    out = dict(build._split_seed_inserts([("db/seed.sql", seed)]))
    got = out["db/seed.sql"]
    # The scanner catches it.
    assert "SKIPPED (unbalanced quotes)" in got
