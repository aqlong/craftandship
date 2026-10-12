"""Where one SQL statement ends: a lexer for PostgreSQL text, not a keyword list.

`Actuators.query_readonly` runs the text it is given in a read-only
transaction. That transaction holds only while the text is ONE statement: a
`COMMIT;` in front of a write ends the transaction, and what follows runs in
a new one. This module counts the statements in a piece of text the way the
server's own lexer separates them, so that the caller can refuse anything
that is not exactly one.

What it understands (the places a semicolon does NOT end a statement):

  * `-- ...` to the end of the line, and `/* ... */` comments, which nest
    (PostgreSQL's do, unlike the C ones);
  * '...' strings, with '' for a quote, and E'...' strings, in which a
    backslash escapes the next character;
  * "..." identifiers, with "" for a quote;
  * dollar quoting, $$ ... $$ and $tag$ ... $tag$, but not a parameter such as
    $1 and not a dollar sign inside an identifier (a$b$c).

It assumes standard_conforming_strings = on, which is the server's default
and which query_readonly sets for its session before it sends the text. With
the setting off a backslash would also escape a quote in a plain '...'
string, and this lexer would read such a string differently from the server.

Text it cannot place (an unterminated string, comment or dollar quote) is an
error, not a guess: the caller treats it as a refusal. A semicolon inside a
BEGIN ATOMIC function body also counts as a separator, so such a statement
reads as several; that errs toward refusing.

Bytes at or above 0x80 count as identifier characters, as they do for the
server, so the text can be the encoded bytes psycopg2 sends.
"""

from __future__ import annotations

import re

__all__ = ["UnterminatedSql", "count_statements"]


class UnterminatedSql(ValueError):
    """A string, quoted identifier, comment or dollar quote that never closes."""


_WHITESPACE = frozenset(b" \t\n\r\f\v")
_IDENT_START = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_") | frozenset(range(0x80, 0x100))
_IDENT_CONT = _IDENT_START | frozenset(b"0123456789$")
# $tag$ or $$ at the start of a token. The tag is an identifier without a dollar sign.
_DOLLAR_QUOTE = re.compile(rb"\$(?:[A-Za-z_\x80-\xff][A-Za-z0-9_\x80-\xff]*)?\$")


def count_statements(sql) -> int:
    """How many statements `sql` holds: the pieces between top-level semicolons
    that contain more than whitespace and comments.

    `SELECT 1`, `SELECT 1;` and `-- note` then `SELECT 1; -- done` are one
    statement each; `SELECT 1; SELECT 2` is two; an empty or comment-only text
    is none. Raises UnterminatedSql when a quote or comment does not close.
    """
    data = sql.encode("utf-8") if isinstance(sql, str) else bytes(sql)
    size = len(data)
    count = 0
    has_token = False  # the statement being read holds something besides whitespace and comments
    i = 0
    while i < size:
        c = data[i]
        if c in _WHITESPACE:
            i += 1
        elif data.startswith(b"--", i):
            i = _end_of_line_comment(data, i)
        elif data.startswith(b"/*", i):
            i = _end_of_block_comment(data, i)
        elif c == 0x3B:  # ;
            if has_token:
                count += 1
                has_token = False
            i += 1
        else:
            has_token = True
            if c == 0x27:  # '
                i = _end_of_string(data, i, backslash_escapes=_starts_escape_string(data, i))
            elif c == 0x22:  # "
                i = _end_of_quoted_identifier(data, i)
            elif c == 0x24 and (i == 0 or data[i - 1] not in _IDENT_CONT):  # $
                i = _end_of_dollar_quote(data, i)
            elif c in _IDENT_START:
                # An identifier or keyword runs to its last character, so that a
                # '$' in the middle of it never opens a dollar quote.
                i += 1
                while i < size and data[i] in _IDENT_CONT:
                    i += 1
            else:
                i += 1
    if has_token:
        count += 1
    return count


def _is_whole_prefix(data: bytes, index: int) -> bool:
    """data[index] is a one-letter token, not the end of a longer identifier."""
    return index == 0 or data[index - 1] not in _IDENT_CONT


def _starts_escape_string(data: bytes, quote: int) -> bool:
    """The quote at `quote` opens an E'...' string."""
    return quote >= 1 and data[quote - 1] in b"Ee" and _is_whole_prefix(data, quote - 1)


def _end_of_line_comment(data: bytes, start: int) -> int:
    i = start + 2
    while i < len(data) and data[i] not in b"\r\n":
        i += 1
    return i


def _end_of_block_comment(data: bytes, start: int) -> int:
    depth = 1
    i = start + 2
    while i < len(data):
        if data.startswith(b"/*", i):
            depth += 1
            i += 2
        elif data.startswith(b"*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    raise UnterminatedSql("unterminated /* comment")


def _end_of_string(data: bytes, start: int, *, backslash_escapes: bool) -> int:
    i = start + 1
    while i < len(data):
        c = data[i]
        if backslash_escapes and c == 0x5C:  # backslash: the next byte is escaped
            i += 2
        elif c == 0x27:
            if data.startswith(b"''", i):
                i += 2
            else:
                return i + 1
        else:
            i += 1
    raise UnterminatedSql("unterminated quoted string")


def _end_of_quoted_identifier(data: bytes, start: int) -> int:
    i = start + 1
    while i < len(data):
        if data[i] == 0x22:
            if data.startswith(b'""', i):
                i += 2
            else:
                return i + 1
        else:
            i += 1
    raise UnterminatedSql("unterminated quoted identifier")


def _end_of_dollar_quote(data: bytes, start: int) -> int:
    opener = _DOLLAR_QUOTE.match(data, start)
    if opener is None:
        return start + 1  # a parameter ($1) or a lone dollar sign: not a quote
    tag = opener.group(0)
    close = data.find(tag, opener.end())
    if close < 0:
        raise UnterminatedSql(f"unterminated dollar-quoted string {tag.decode('latin-1')}")
    return close + len(tag)
