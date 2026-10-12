#!/usr/bin/env python3
"""scripts/check_destructive_ddl.py: Reject destructive DDL and unacknowledged
row changes in schema.sql.

Reads the SQL as PostgreSQL reads it: comments, quoted strings, quoted
identifiers and dollar-quoted bodies (with or without a tag) are recognised for
what they are, and the text is cut into statements at the semicolons that end
them. A statement is refused when it contains, anywhere in it:

    DROP TABLE / DROP SCHEMA / DROP DATABASE / DROP OWNED
    ALTER TABLE ... DROP [COLUMN] <column>      (DROP CONSTRAINT is not refused)
    ALTER TABLE ... ALTER [COLUMN] <column> [SET DATA] TYPE ...
    TRUNCATE
    DELETE FROM with no WHERE, or with a WHERE that names no column
        (WHERE true, WHERE 1 = 1, WHERE id = 1 OR true: a condition that is
        constant, or has a constant branch of an OR, restricts nothing)

and, because schema.sql is applied at every boot of the deployed web app, any
statement that changes rows:

    UPDATE, INSERT INTO, DELETE FROM, MERGE INTO, COPY ... FROM,
    EXECUTE of SQL that is built at run time (format(), ||, a variable)

wherever it stands: at the top level, inside a DO block or a function body,
in a string handed to EXECUTE, in the parentheses of a WITH query. UPDATE and
the other words are not taken for the command where they only name an event
or a privilege (BEFORE UPDATE, ON DELETE CASCADE, FOR UPDATE, GRANT UPDATE).
The body of a DO block and of a CREATE FUNCTION or CREATE PROCEDURE is read the
same way, statement by statement, and so is the text of a string handed to
EXECUTE.

Acknowledging a statement: a comment

    -- SAFE: <reason>

on the line of the destructive keyword, or in the run of comment lines
directly above the statement it is in (for a DROP or an ALTER inside a DO
block: also directly above the DO). A statement that changes or removes rows
(UPDATE, INSERT, DELETE, MERGE, COPY, TRUNCATE, dynamic EXECUTE) is
acknowledged more narrowly: on its own line, or directly above its own
statement, never above the DO block around it, so a statement added to a block
later is not covered by the reason written for the ones that were there.

One rule has no acknowledgement. The status `live` of a build is written by
three statements of the web application (two in web/src/routes/builds.ts and one
in web/src/routes/webhooks.ts) and by nothing else, and schema.sql is not one
of them: a statement that changes rows or runs dynamic SQL and names the string
live anywhere in it, and any DEFAULT of live, is refused whatever comment is
above it.

What this does not do. It does not evaluate expressions: `WHERE id = id`
passes, and `EXECUTE 'DR' || 'OP TABLE t'` is refused only because it is
dynamic SQL, not because of what it would run. It follows no value through a
variable (`s := 'live'; UPDATE builds SET status = s` is an unacknowledged
UPDATE, and an acknowledged one passes) and no call (`SELECT f()` runs whatever
f does). It does not decode a string (`E'\\x6c\\x69ve'`, `U&'...'` and
`'li' || 've'` do not name live as far as it can tell), so the rule about live
catches the literal spelling; the reason a person writes above a row change,
and the merge guard that keeps an executor branch out of the file, are what
stand behind it. It reads PL/pgSQL only as far as cutting a body at its
semicolons.

Bounds. The input is read whole and is refused above MAX_BYTES (exit 2). The
time is linear in its size except within one statement, which is refused above
MAX_STATEMENT_TOKENS tokens; bodies are followed to MAX_DEPTH levels and a
deeper one is refused. Nothing is skipped silently at a bound.

The comment satisfies this check and nothing else. web/tests/unit/
schema.idempotency.test.ts (part of the web unit tests the pre-push hook runs)
reads the file with its own tokenizer, refuses a top-level DROP TABLE, DROP
COLUMN, DROP CONSTRAINT, TRUNCATE, DELETE, UPDATE or INSERT whatever comment
precedes it, and holds the inventory of the row changes that are allowed
inside DO blocks. Both read the same corpus of awkward inputs
(web/tests/unit/fixtures/sql-scan-corpus.json) so that they cannot disagree
about what a statement is.

Usage:
    python3 scripts/check_destructive_ddl.py                    # check schema.sql
    python3 scripts/check_destructive_ddl.py path/to/file.sql   # check arbitrary file
    git show :web/src/db/schema.sql | python3 scripts/check_destructive_ddl.py - [NAME]
                                                                # check stdin (NAME is used in messages)

Exit codes:
    0: no unacknowledged destructive DDL found
    1: destructive DDL found without safety comment, or SQL that cannot be read
       (an unterminated string, comment or dollar quote, a statement of more
       than MAX_STATEMENT_TOKENS tokens, bodies nested more than MAX_DEPTH deep)
    2: the file cannot be read, or is larger than MAX_BYTES
"""

import re
import sys
from pathlib import Path

# The largest input read. schema.sql is tens of kilobytes.
MAX_BYTES = 8 * 1024 * 1024
# The longest statement read, in tokens (the longest in schema.sql has 166).
# Several checks look along the rest of the statement from a keyword, so the
# time a statement takes grows with the square of its length; a longer one is
# refused as unreadable, which keeps the time on any input within the size
# limit to minutes and on a file like schema.sql to a fraction of a second.
MAX_STATEMENT_TOKENS = 2000
# How deep bodies inside bodies are followed (a DO block whose EXECUTE string
# holds a DO block, and so on). Deeper than this is refused, not skipped.
MAX_DEPTH = 6

# The kinds of statement this check refuses.
DESTRUCTIVE_KINDS = (
    "DROP TABLE", "DROP SCHEMA", "DROP DATABASE", "DROP OWNED", "DROP COLUMN",
    "ALTER COLUMN TYPE", "TRUNCATE", "DELETE FROM (without WHERE)",
)
# Statements that change rows. TRUNCATE and DELETE FROM with no restricting
# WHERE are destructive kinds above; the rest are refused unless acknowledged.
EXECUTE_KIND = "EXECUTE (dynamic SQL)"
ROW_CHANGE_KINDS = ("UPDATE", "INSERT", "DELETE", "MERGE", "COPY FROM", EXECUTE_KIND)
# Everything that changes or removes rows: acknowledged only by a comment of its
# own (see _check), where DROP and ALTER can also be acknowledged above a DO.
CHANGES_ROWS = ROW_CHANGE_KINDS + ("TRUNCATE", "DELETE FROM (without WHERE)")
# A row change, or a default, that names the status live: never acknowledged.
LIVE_KIND = "NAMES THE STATUS live"
# The row changes that can write a value (not DELETE, TRUNCATE).
_CAN_WRITE_LIVE = ("UPDATE", "INSERT", "MERGE", "COPY FROM", EXECUTE_KIND)
UNREADABLE = "UNREADABLE SQL"

# Safety comment that acknowledges the destructive operation
SAFETY_MARKER = re.compile(r'--\s*SAFE:', re.IGNORECASE)
_LIVE_WORD = re.compile(r"\blive\b", re.IGNORECASE)

# The example the refusal message prints: a statement this check flags, that
# the comment settles, and that the web suite's schema test accepts at top
# level (tests/test_db_safety.py checks all three).
ACKNOWLEDGED_EXAMPLE = (
    "-- SAFE: widening the column loses no data",
    "ALTER TABLE builds ALTER COLUMN app_name TYPE VARCHAR(200);",
)

# Words that can stand in a condition without being a column: constants,
# operators spelled as words, the special values that take no parentheses,
# and the type names a cast is usually written with.
NOT_A_COLUMN = frozenset("""
    true false null unknown not and or is in between like ilike similar to escape any all some
    exists case when then else end cast as distinct from select isnull notnull symmetric array row
    collate at time zone interval date timestamp timestamptz current_date current_time
    current_timestamp current_user current_role current_catalog current_schema session_user user
    localtime localtimestamp boolean bool int integer int2 int4 int8 smallint bigint text varchar
    char character numeric decimal real float double precision uuid json jsonb bytea
    returning
""".split())


class SqlError(Exception):
    """The text cannot be cut into tokens: something in it is not closed."""

    def __init__(self, message, line):
        super().__init__(message)
        self.line = line


class Token:
    """kind is one of: word, ident (a quoted identifier), string, dollar (a
    dollar-quoted string), number, op. For string and dollar, `body` is the
    text between the quotes and `body_line` the line it starts on."""

    __slots__ = ("kind", "text", "line", "body", "body_line")

    def __init__(self, kind, text, line, body=None, body_line=None):
        self.kind, self.text, self.line, self.body, self.body_line = kind, text, line, body, body_line

    def is_word(self, *words):
        return self.kind == "word" and self.text.lower() in words


_WORD_START = re.compile(r"[A-Za-z_\u0080-￿]")
_WORD = re.compile(r"[A-Za-z_\u0080-￿][A-Za-z0-9_$\u0080-￿]*")
_NUMBER = re.compile(r"\d+(\.\d*)?([eE][+-]?\d+)?|\.\d+([eE][+-]?\d+)?")
_DOLLAR_OPEN = re.compile(r"\$([A-Za-z_\u0080-￿][A-Za-z0-9_\u0080-￿]*)?\$")


def tokenize(text, first_line=1):
    """Cut SQL text into tokens and comments.

    Returns (tokens, comments): comments maps a line number to the text of the
    `--` comment on it. Line numbers start at first_line. Raises SqlError for
    a string, quoted identifier, block comment or dollar quote that is never
    closed.
    """
    tokens, comments = [], {}
    i, line, n = 0, first_line, len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
        elif ch.isspace():
            i += 1
        elif text.startswith("--", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            comments[line] = text[i:end]
            i = end
        elif text.startswith("/*", i):
            depth, j, start_line = 1, i + 2, line
            while depth and j < n:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    line += text[j] == "\n"
                    j += 1
            if depth:
                raise SqlError("a /* comment is never closed", start_line)
            i = j
        elif ch == "'":
            # A one-letter prefix touching the quote (E'..', B'..', X'..',
            # N'..') belongs to the string and is not a word of its own. An
            # E string takes backslash escapes; any other string does not.
            escapes = False
            if tokens and tokens[-1].kind == "word" and len(tokens[-1].text) == 1 \
                    and tokens[-1].text in "EeBbXxNn" and text[i - 1] == tokens[-1].text:
                escapes = tokens.pop().text in "Ee"
            j, start_line, body = i + 1, line, []
            while True:
                if j >= n:
                    raise SqlError("a string is never closed", start_line)
                c = text[j]
                if escapes and c == "\\" and j + 1 < n:
                    body.append(text[j:j + 2])
                    line += text[j + 1] == "\n"
                    j += 2
                elif c == "'":
                    if text.startswith("''", j):
                        body.append("'")
                        j += 2
                    else:
                        break
                else:
                    line += c == "\n"
                    body.append(c)
                    j += 1
            tokens.append(Token("string", text[i:j + 1], start_line, "".join(body), start_line))
            i = j + 1
        elif ch == '"':
            j, start_line = i + 1, line
            while True:
                if j >= n:
                    raise SqlError("a quoted identifier is never closed", start_line)
                if text[j] == '"':
                    if text.startswith('""', j):
                        j += 2
                        continue
                    break
                line += text[j] == "\n"
                j += 1
            tokens.append(Token("ident", text[i:j + 1], start_line))
            i = j + 1
        elif ch == "$" and _DOLLAR_OPEN.match(text, i):
            tag = _DOLLAR_OPEN.match(text, i).group(0)
            body_start = i + len(tag)
            close = text.find(tag, body_start)
            if close == -1:
                raise SqlError(f"a {tag} quote is never closed", line)
            body = text[body_start:close]
            tokens.append(Token("dollar", text[i:close + len(tag)], line, body, line))
            line += body.count("\n")
            i = close + len(tag)
        elif _WORD_START.match(ch):
            word = _WORD.match(text, i).group(0)
            tokens.append(Token("word", word, line))
            i += len(word)
        elif ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            number = _NUMBER.match(text, i).group(0)
            tokens.append(Token("number", number, line))
            i += len(number)
        elif text.startswith("::", i):
            tokens.append(Token("op", "::", line))
            i += 2
        else:
            tokens.append(Token("op", ch, line))
            i += 1
    return tokens, comments


def split_statements(tokens):
    """Cut a token list at the semicolons. Empty statements are dropped."""
    statements, current = [], []
    for token in tokens:
        if token.kind == "op" and token.text == ";":
            if current:
                statements.append(current)
            current = []
        else:
            current.append(token)
    if current:
        statements.append(current)
    return statements


def _strip_parens(tokens):
    """Remove pairs of parentheses that wrap the whole expression."""
    while len(tokens) >= 2 and tokens[0].text == "(" and tokens[0].kind == "op":
        depth = 0
        for index, token in enumerate(tokens):
            if token.kind == "op" and token.text == "(":
                depth += 1
            elif token.kind == "op" and token.text == ")":
                depth -= 1
                if depth == 0:
                    break
        if index != len(tokens) - 1:
            return tokens
        tokens = tokens[1:-1]
    return tokens


def _names_a_column(tokens):
    """Does the expression mention anything that can be a column? A quoted
    identifier always can. A bare word can unless it is a keyword or constant
    (NOT_A_COLUMN), a function being called (also as schema.function), a type
    after `::`, or a table named after FROM or JOIN in a subquery."""
    for index, token in enumerate(tokens):
        if token.kind == "ident":
            return True
        if token.kind != "word" or token.text.lower() in NOT_A_COLUMN:
            continue
        # the end of a dotted name that starts here: a.b.c
        end = index
        while end + 2 < len(tokens) and tokens[end + 1].text == "." and tokens[end + 2].kind == "word":
            end += 2
        following = tokens[end + 1] if end + 1 < len(tokens) else None
        if following is not None and following.kind == "op" and following.text == "(":
            continue
        previous = tokens[index - 1] if index else None
        if previous is not None and previous.kind == "op" and previous.text in ("::", "."):
            continue
        if previous is not None and previous.is_word("from", "join"):
            continue
        return True
    return False


def _restricts(tokens):
    """Is this WHERE condition one that can leave rows out? Not when it names
    no column, and not when one branch of an OR at its top level names none."""
    tokens = _strip_parens(tokens)
    if not tokens:
        return False
    branches, current, depth = [], [], 0
    for token in tokens:
        if token.kind == "op" and token.text == "(":
            depth += 1
        elif token.kind == "op" and token.text == ")":
            depth -= 1
        if depth == 0 and token.is_word("or"):
            branches.append(current)
            current = []
        else:
            current.append(token)
    branches.append(current)
    if len(branches) > 1:
        return all(_restricts(branch) for branch in branches)
    return _names_a_column(tokens)


def _delete_without_where(tokens, start):
    """tokens[start] is DELETE and tokens[start + 1] is FROM. True when no
    restricting WHERE follows in the same scope (up to the parenthesis that
    closes the scope the DELETE is in, or the end of the statement)."""
    depth, where = 0, None
    index = start + 2
    while index < len(tokens):
        token = tokens[index]
        if token.kind == "op" and token.text == "(":
            depth += 1
        elif token.kind == "op" and token.text == ")":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and where is None and token.is_word("where"):
            where = index
        elif depth == 0 and where is not None and token.is_word("returning"):
            break
        index += 1
    if where is None:
        return True
    return not _restricts(tokens[where + 1:index])


def _alter_table_findings(tokens, start):
    """tokens[start] is the TABLE of an ALTER [FOREIGN] TABLE. Yields
    (token, kind) for each action that drops a column or changes a column's
    type."""
    index = start + 1
    # [IF EXISTS] [ONLY] name [. name] [*]
    while index < len(tokens) and tokens[index].is_word("if", "exists", "only"):
        index += 1
    if index < len(tokens) and tokens[index].kind in ("word", "ident"):
        index += 1
        while index + 1 < len(tokens) and tokens[index].text == "." and tokens[index + 1].kind in ("word", "ident"):
            index += 2
    if index < len(tokens) and tokens[index].text == "*":
        index += 1
    actions, current, depth = [], [], 0
    for token in tokens[index:]:
        if token.kind == "op" and token.text == "(":
            depth += 1
        elif token.kind == "op" and token.text == ")":
            depth -= 1
        if depth == 0 and token.kind == "op" and token.text == ",":
            actions.append(current)
            current = []
        else:
            current.append(token)
    actions.append(current)
    for action in actions:
        if not action:
            continue
        if action[0].is_word("drop"):
            if not (len(action) > 1 and action[1].is_word("constraint")):
                yield action[0], "DROP COLUMN"
        elif action[0].is_word("alter"):
            rest = action[1:]
            if rest and rest[0].is_word("column"):
                rest = rest[1:]
            rest = rest[1:]  # the column's name
            if rest and (rest[0].is_word("type")
                         or (len(rest) > 2 and rest[0].is_word("set") and rest[1].is_word("data")
                             and rest[2].is_word("type"))):
                yield action[0], "ALTER COLUMN TYPE"


_DROP_OBJECTS = {"table": "DROP TABLE", "schema": "DROP SCHEMA", "database": "DROP DATABASE",
                 "owned": "DROP OWNED"}


# Words after which UPDATE is not the command: a trigger event (BEFORE / AFTER /
# OR / OF UPDATE), a foreign key action (ON UPDATE), a row lock (FOR UPDATE,
# FOR NO KEY UPDATE) and a privilege (GRANT / REVOKE UPDATE, or after a comma
# in a list of privileges).
_NOT_THE_COMMAND_BEFORE = frozenset(("for", "key", "on", "or", "before", "after", "of", "grant", "revoke"))


def _after_a_privilege_or_event_word(tokens, index):
    previous = tokens[index - 1] if index else None
    if previous is None:
        return False
    if previous.kind == "op" and previous.text == ",":
        return True
    return previous.kind == "word" and previous.text.lower() in _NOT_THE_COMMAND_BEFORE


def _execute_is_dynamic(tokens, index):
    """EXECUTE followed by exactly one string (and then, at most, INTO or
    USING) names the SQL it runs, and that SQL is read like any other body.
    Anything else builds the SQL at run time (format(), ||, a variable, the
    name of a prepared statement) and cannot be read."""
    first = tokens[index + 1] if index + 1 < len(tokens) else None
    if first is None or first.kind not in ("string", "dollar"):
        return True
    after = tokens[index + 2] if index + 2 < len(tokens) else None
    return not (after is None or after.is_word("into", "using"))


def _copy_reads_a_file(tokens, index):
    """COPY ... FROM (loads rows); COPY ... TO and COPY (query) TO only read."""
    depth = 0
    for later in tokens[index + 1:]:
        if later.kind == "op" and later.text == "(":
            depth += 1
        elif later.kind == "op" and later.text == ")":
            depth -= 1
        elif depth == 0 and later.is_word("from"):
            return True
        elif depth == 0 and later.is_word("to"):
            return False
    return False


def _row_change_kind(tokens, index):
    """The kind of row change the word at tokens[index] begins, or None. The
    commands are recognised wherever they stand in the statement (after THEN
    or ELSE in a PL/pgSQL branch, inside the parentheses of a WITH query, after
    DO ALSO in a rule), because a statement here is whatever lies between two
    semicolons and a DO body is not one statement."""
    token = tokens[index]
    word = token.text.lower()
    following = tokens[index + 1] if index + 1 < len(tokens) else None
    if word == "update":
        return None if _after_a_privilege_or_event_word(tokens, index) else "UPDATE"
    if word == "insert":
        return "INSERT" if following is not None and following.is_word("into") else None
    if word == "merge":
        return "MERGE" if following is not None and following.is_word("into") else None
    if word == "copy":
        return "COPY FROM" if _copy_reads_a_file(tokens, index) else None
    if word == "execute":
        if following is not None and following.is_word("function", "procedure"):
            return None  # a trigger names its function this way
        if _after_a_privilege_or_event_word(tokens, index):
            return None  # GRANT EXECUTE ON FUNCTION
        return EXECUTE_KIND if _execute_is_dynamic(tokens, index) else None
    return None


def _names_live(token):
    return token.kind in ("string", "dollar") and _LIVE_WORD.search(token.body or "") is not None


def _sets_a_default_of_live(tokens):
    for index, token in enumerate(tokens):
        if token.is_word("default"):
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if following is not None and following.kind == "op" and following.text == "(":
                following = tokens[index + 2] if index + 2 < len(tokens) else None
            if following is not None and _names_live(following):
                return following
    return None


def statement_findings(tokens):
    """The destructive constructs in one statement, as (token, anchor, kind):
    token is the keyword to report, anchor the keyword the command it belongs
    to begins with (the ALTER of ALTER TABLE; otherwise the token itself)."""
    found, seen = [], set()

    def add(token, kind, anchor=None):
        if id(token) not in seen:
            seen.add(id(token))
            found.append((token, anchor or token, kind))

    for index, token in enumerate(tokens):
        if token.kind != "word":
            continue
        word = token.text.lower()
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if word == "drop" and following is not None and following.kind == "word":
            target = following.text.lower()
            if target == "foreign" and index + 2 < len(tokens) and tokens[index + 2].is_word("table"):
                target = "table"
            if target in _DROP_OBJECTS:
                add(token, _DROP_OBJECTS[target])
            elif target == "column":
                add(token, "DROP COLUMN")
        elif word == "alter" and following is not None and (
                following.is_word("table")
                or (following.is_word("foreign") and index + 2 < len(tokens) and tokens[index + 2].is_word("table"))):
            table = index + (1 if following.is_word("table") else 2)
            for action_token, kind in _alter_table_findings(tokens, table):
                add(action_token, kind, anchor=token)
        elif word == "truncate":
            add(token, "TRUNCATE")
        elif word == "delete" and following is not None and following.is_word("from"):
            add(token, "DELETE FROM (without WHERE)" if _delete_without_where(tokens, index) else "DELETE")
        else:
            kind = _row_change_kind(tokens, index)
            if kind is not None:
                add(token, kind)
    # The status `live` is written by three statements of the web application
    # and by nothing else. A statement that changes rows and names it, or that
    # makes it a column's default, is refused whatever comment is above it.
    if any(kind in _CAN_WRITE_LIVE for _, _, kind in found):
        for token in tokens:
            if _names_live(token):
                add(token, LIVE_KIND)
                break
    default = _sets_a_default_of_live(tokens)
    if default is not None:
        add(default, LIVE_KIND)
    return found


def _bodies(tokens):
    """The tokens in one statement whose text is itself SQL that will run:
    the body of a DO block and of CREATE FUNCTION / PROCEDURE (a dollar quote
    or a plain string), and every string after an EXECUTE in a PL/pgSQL
    statement (dynamic SQL; EXECUTE FUNCTION and EXECUTE PROCEDURE, which name
    a trigger's function, are not that)."""
    words = [t.text.lower() for t in tokens if t.kind == "word"]
    first = words[0] if words else ""
    if first == "do" or (first == "create" and ("function" in words[:6] or "procedure" in words[:6])):
        for token in tokens:
            if token.kind in ("dollar", "string"):
                yield token
        return
    for index, token in enumerate(tokens):
        if token.is_word("execute"):
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if following is not None and following.is_word("function", "procedure"):
                continue
            for later in tokens[index + 1:]:
                if later.kind in ("dollar", "string"):
                    yield later
            return


class _Source:
    """The file being checked: its lines, the comments found so far, and which
    token comes first on each line (a comment above a line speaks for the
    command that begins the line, not for one further along it)."""

    def __init__(self, text):
        # Split where the tokenizer counts lines (a "\n"), not where
        # str.splitlines() does (a form feed, a vertical tab, U+2028 ...): the
        # line numbers of tokens index this list.
        self.lines = text.split("\n")
        self.comments = {}
        self.first_on_line = {}

    def line_text(self, line):
        return self.lines[line - 1].strip() if 1 <= line <= len(self.lines) else ""

    def add(self, tokens, comments):
        self.comments.update(comments)
        for token in tokens:
            self.first_on_line.setdefault(token.line, token)

    def safe_on(self, line):
        return SAFETY_MARKER.search(self.comments.get(line, "")) is not None

    def safe_above(self, token):
        """Is there a SAFE comment in the run of comment lines directly above
        the line `token` begins?"""
        if self.first_on_line.get(token.line) is not token:
            return False
        above = token.line - 1
        while 1 <= above <= len(self.lines) and self.lines[above - 1].lstrip().startswith("--"):
            if self.safe_on(above):
                return True
            above -= 1
        return False


def _check(text, first_line, source, enclosing, depth, violations):
    """Check the statements of `text`, which starts on line `first_line` of
    the file. `enclosing` holds the first token of each statement this text
    is nested in: a SAFE comment directly above one of them acknowledges what
    is found here."""
    try:
        tokens, comments = tokenize(text, first_line)
    except SqlError as exc:
        violations.append((exc.line, source.line_text(exc.line), f"{UNREADABLE}: {exc}"))
        return
    source.add(tokens, comments)
    for statement in split_statements(tokens):
        if len(statement) > MAX_STATEMENT_TOKENS:
            line = statement[0].line
            violations.append((line, source.line_text(line),
                               f"{UNREADABLE}: a statement of more than {MAX_STATEMENT_TOKENS} tokens"))
            continue
        starts = enclosing + [statement[0]]
        for token, anchor, kind in statement_findings(statement):
            if kind != LIVE_KIND:
                if source.safe_on(token.line) or source.safe_on(anchor.line):
                    continue
                # A row change is acknowledged by a comment on its own line or
                # directly above its own statement. A comment above the DO
                # block it sits in does not speak for it: a statement added to
                # a block later would be covered by the reason written for
                # the statements that were there.
                scope = [anchor, statement[0]] if kind in CHANGES_ROWS else [anchor, *starts]
                if any(source.safe_above(start) for start in scope):
                    continue
            violations.append((token.line, source.line_text(token.line), kind))
        for body in _bodies(statement):
            if depth >= MAX_DEPTH:
                violations.append((body.line, source.line_text(body.line),
                                   f"{UNREADABLE}: bodies nested more than {MAX_DEPTH} deep"))
                continue
            _check(body.body, body.body_line, source, starts, depth + 1, violations)


def check_sql(text):
    """Scan SQL text for unacknowledged destructive DDL.

    Returns a list of (line_number, line_text, kind) tuples, in file order.
    """
    violations = []
    _check(text, 1, _Source(text), [], 0, violations)
    return sorted(set(violations), key=lambda v: (v[0], v[2]))


def check_file(filepath: str) -> list:
    """check_sql for a file. Raises OSError when it cannot be read and
    ValueError when it is larger than MAX_BYTES: a file that was not read is
    not a file without destructive DDL."""
    path = Path(filepath)
    with open(path, "rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    return check_sql(_decode(data, str(path)))


def _decode(data: bytes, name: str) -> str:
    if len(data) > MAX_BYTES:
        raise ValueError(f"{name} is larger than {MAX_BYTES} bytes")
    return data.decode("utf-8", errors="replace")


def main():
    args = sys.argv[1:]
    try:
        if args and args[0] == "-":
            label = args[1] if len(args) > 1 else "standard input"
            violations = check_sql(_decode(sys.stdin.buffer.read(MAX_BYTES + 1), label))
        else:
            if args:
                label = args[0]
            else:
                # Default: check the project's schema.sql
                project_root = Path(__file__).resolve().parent.parent
                label = str(project_root / "web" / "src" / "db" / "schema.sql")
            violations = check_file(label)
    except (OSError, ValueError) as exc:
        print(f"check_destructive_ddl: cannot check: {exc}", file=sys.stderr)
        sys.exit(2)

    if violations:
        print(f"DESTRUCTIVE DDL detected in {label}:")
        print()
        for line_num, text, pattern in violations:
            print(f"  Line {line_num} [{pattern}]: {text}")
        print()
        print("To acknowledge, add '-- SAFE: <reason>' on the line before the statement.")
        print("Example:")
        for example_line in ACKNOWLEDGED_EXAMPLE:
            print(f"  {example_line}")
        print()
        print("An UPDATE, INSERT, DELETE, MERGE, COPY ... FROM or dynamic EXECUTE needs its own")
        print("'-- SAFE: <reason>' directly above it; a comment above the DO block around it does not")
        print("count. A statement that names the status live is never accepted: the three")
        print("statements of the web application are the only writers of it.")
        print()
        print("The comment satisfies this check only. A top-level DROP TABLE, DROP COLUMN,")
        print("DROP CONSTRAINT, TRUNCATE, DELETE, UPDATE or INSERT is also refused, whatever the")
        print("comment, by web/tests/unit/schema.idempotency.test.ts (run by the pre-push hook),")
        print("because schema.sql is applied at every boot: put it in a guarded DO $$ block instead.")
        sys.exit(1)
    else:
        print(f"No unacknowledged destructive DDL or row change in {label}")
        sys.exit(0)


if __name__ == "__main__":
    main()
