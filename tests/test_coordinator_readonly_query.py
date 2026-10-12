"""Actuators.query_readonly: one statement, in a read-only transaction.

The method's guarantee used to be "the read-only session", and a string that
began `COMMIT;` and went on with a `DELETE` deleted a row: the COMMIT ended the
read-only transaction and the rest ran in a new, writable one. These tests
drive the real method against a real PostgreSQL (the `scratch_pg_url` fixture)
and look at the table afterwards, with that input and its relatives, and with
statements that must NOT be refused because a semicolon sits in a comment, a
string or a dollar-quoted body.

Two sides meet here and both are exercised through product code: the lexer
that counts statements (coordinator/sql_statements.py) and the transaction the
database enforces. A differential test compares the lexer with the server
itself: texts made of INSERT statements into a temporary table are executed,
and the number of rows the server inserted must equal the number of statements
the lexer counted.
"""

import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coordinator.actuators import Actuators
from coordinator.sql_statements import UnterminatedSql, count_statements


# ---------------------------------------------------------------------------
# The lexer
# ---------------------------------------------------------------------------

# (text, statements). Every text here is also run through the server below.
COUNT_CASES = [
    ("SELECT 1", 1),
    ("SELECT 1;", 1),
    ("SELECT 1 ;  ", 1),
    ("SELECT 1; SELECT 2", 2),
    ("SELECT 1;; SELECT 2", 2),
    ("; SELECT 1", 1),
    ("", 0),
    ("   \n\t", 0),
    (";", 0),
    ("-- only a comment", 0),
    ("/* only a comment */", 0),
    ("-- comment; with a semicolon\nSELECT 1", 1),
    ("SELECT 1 -- trailing; comment", 1),
    ("SELECT 1; -- done", 1),
    ("SELECT 1; -- done\nSELECT 2", 2),
    ("/* ; */ SELECT 1", 1),
    ("/* a /* nested ; */ still comment ; */ SELECT 1", 1),
    ("SELECT 1 /* x */ ; /* y */", 1),
    ("SELECT 'a;b'", 1),
    ("SELECT 'it''s; fine'", 1),
    ("SELECT 'a'; SELECT 'b'", 2),
    ("SELECT E'a\\'; SELECT 2; --'", 1),
    ("SELECT e'a\\\\'; SELECT 2", 2),
    ("SELECT 'a\\'; SELECT 2", 2),            # standard strings: the backslash is a character
    ("SELECT \"a;b\" FROM (SELECT 1 AS \"a;b\") q", 1),
    ("SELECT \"a\"\"; SELECT 2\" FROM (SELECT 1 AS \"a\"\"; SELECT 2\") q", 1),
    ("SELECT $$a; SELECT 2$$", 1),
    ("SELECT $tag$ ; $$ ; $tag$", 1),
    ("SELECT $a1$ ; $a1$; SELECT 2", 2),
    ("SELECT $1; SELECT $2", 2),              # parameters, not dollar quotes
    ("SELECT 1 AS a$b; SELECT $$x$$", 2),     # a dollar sign inside an identifier
    ("SELECT 1 AS x$$y; SELECT 2", 2),
    ("COMMIT; DELETE FROM t RETURNING id", 2),
    ("SET default_transaction_read_only = off; DELETE FROM t", 2),
    ("BEGIN READ WRITE; DELETE FROM t", 2),
]


@pytest.mark.parametrize("text, expected", COUNT_CASES)
def test_the_lexer_counts_statements(text, expected):
    assert count_statements(text) == expected
    assert count_statements(text.encode("utf-8")) == expected


@pytest.mark.parametrize("text", [
    "SELECT 'abc",
    "SELECT E'abc\\'",
    "SELECT \"abc",
    "SELECT /* abc",
    "SELECT /* a /* b */ c",
    "SELECT $$ abc",
    "SELECT $tag$ abc $other$",
])
def test_text_it_cannot_place_is_an_error_not_a_guess(text):
    with pytest.raises(UnterminatedSql):
        count_statements(text)


@pytest.fixture()
def scratch_cursor(scratch_pg_url):
    import psycopg2

    connection = psycopg2.connect(scratch_pg_url)
    connection.autocommit = True
    cursor = connection.cursor()
    yield cursor
    connection.close()


def test_the_lexer_agrees_with_the_server_on_where_statements_end(scratch_cursor):
    """For each shape, a text made of INSERT statements (one per statement the
    lexer should count, with the tricky parts inside comments, strings and
    dollar quotes that hold something that would insert if it were code)."""
    insert = "INSERT INTO pg_temp.counted DEFAULT VALUES"
    bait = insert
    texts = [
        (f"{insert}", 1),
        (f"{insert};", 1),
        (f"{insert}; {insert}", 2),
        (f"-- {bait};\n{insert}", 1),
        (f"/* {bait}; */ {insert}", 1),
        (f"/* a /* nested {bait}; */ still comment {bait}; */ {insert}", 1),
        (f"{insert} -- {bait}", 1),
        (f"{insert}; -- {bait};\n{insert}", 2),
        (f"INSERT INTO pg_temp.counted (note) VALUES ('{bait}; {bait}')", 1),
        (f"INSERT INTO pg_temp.counted (note) VALUES ('it''s; {bait}; fine')", 1),
        (f"INSERT INTO pg_temp.counted (note) VALUES (E'a\\'; {bait}; --')", 1),
        (f"INSERT INTO pg_temp.counted (note) VALUES ('a\\'); {insert}; --')", 2),
        (f"INSERT INTO pg_temp.counted (note) VALUES ($${bait}; {bait}$$)", 1),
        (f"INSERT INTO pg_temp.counted (note) VALUES ($tag$ $$ {bait}; $$ $tag$)", 1),
        (f"INSERT INTO pg_temp.counted (note) VALUES ($a1$ {bait}; $a1$); {insert}", 2),
        (f"INSERT INTO pg_temp.counted (note) VALUES (1::text || 'x$$') ; {insert}", 2),
        (f"INSERT INTO pg_temp.counted (\"no;te\") VALUES ('x'); {insert}", 2),
        (f"INSERT INTO pg_temp.counted (\"no\"\";te\") VALUES ('x')", 1),
    ]
    scratch_cursor.execute("CREATE TEMP TABLE counted (note text, \"no;te\" text, \"no\"\";te\" text)")
    for text, expected in texts:
        scratch_cursor.execute("TRUNCATE pg_temp.counted")
        scratch_cursor.execute(text)
        scratch_cursor.execute("SELECT count(*) FROM pg_temp.counted")
        inserted = scratch_cursor.fetchone()[0]
        assert inserted == expected, f"the server ran {inserted} statements, expected {expected}: {text}"
        assert count_statements(text) == expected, f"the lexer disagrees with the server on: {text}"


def _random_statement_text(rng):
    """(text, statements): INSERT statements into a temporary table, joined by
    semicolons, with comments in between and literals that hold semicolons and
    statement-like text. The number of statements is known by construction."""
    insert = "INSERT INTO pg_temp.counted(note) VALUES (1)"

    def literal():
        kind = rng.choice(["plain", "doubled", "estring", "estring_backslash", "dollar", "dollar_tag",
                           "dollar_nested", "identifier_with_dollar", "concatenation", "number"])
        if kind == "plain":
            return "'" + rng.choice(["a;b", insert + ";", "x", "-- not a comment; " + insert, "/* ; */"]) + "'"
        if kind == "doubled":
            return "'it''s; " + insert + "; fine'"
        if kind == "estring":
            return "E'a;" + insert + ";b'"
        if kind == "estring_backslash":
            return "E'quote\\'; " + insert + "; still inside'"
        if kind == "dollar":
            return "$$ ; " + insert + " ; $$"
        if kind == "dollar_tag":
            return "$t$ $$ ; " + insert + " $$ ; $t$"
        if kind == "dollar_nested":
            return "$a$ $b$ ; " + insert + " ; $b$ $a$"
        if kind == "identifier_with_dollar":
            return "(SELECT 1 AS a$b$)::text || 'x;'"
        if kind == "concatenation":
            return "'a' || 'b;' || $$c;$$"
        return "1::text"

    def filler():
        return rng.choice(["", "", " ", "\n", "-- c; " + insert + "\n", "/* c; " + insert + " */",
                           "/* a /* nested; " + insert + " */ b; */", "-- x\r\n"])

    count = rng.randint(1, 4)
    parts = [filler() + "INSERT INTO pg_temp.counted(note) VALUES (" + literal() + ")" + filler() for _ in range(count)]
    text = rng.choice([";", "; ", ";\n", ";;"]).join(parts)
    if rng.random() < 0.5:
        text += rng.choice([";", "; -- end", ";/* end */", ""])
    if rng.random() < 0.2:
        text = filler() + text
    return text, count


def test_the_lexer_and_the_server_agree_on_a_few_hundred_random_texts(scratch_cursor):
    """The same comparison as above on generated text (fixed seed): for every
    text, the rows the server inserted, the number of statements built in, and
    the lexer's count are the same number."""
    import random

    rng = random.Random(20261011)
    scratch_cursor.execute("CREATE TEMP TABLE counted (note text)")
    for _ in range(400):
        text, expected = _random_statement_text(rng)
        scratch_cursor.execute("TRUNCATE pg_temp.counted")
        scratch_cursor.execute(text)
        scratch_cursor.execute("SELECT count(*) FROM pg_temp.counted")
        assert scratch_cursor.fetchone()[0] == expected, f"the server saw another number of statements in: {text!r}"
        assert count_statements(text) == expected, f"the lexer disagrees with the server on: {text!r}"


# ---------------------------------------------------------------------------
# query_readonly against a real database
# ---------------------------------------------------------------------------

@pytest.fixture()
def demo(scratch_pg_url, monkeypatch):
    """A table with three rows and a sequence, in the scratch database, and the
    environment pointing the method at it."""
    import psycopg2

    suffix = uuid.uuid4().hex[:8]
    table, sequence = f"qro_{suffix}", f"qro_seq_{suffix}"
    connection = psycopg2.connect(scratch_pg_url)
    with connection.cursor() as cur:
        cur.execute(f"CREATE TABLE {table} (id int, v text)")
        cur.execute(f"INSERT INTO {table} VALUES (1, 'one'), (2, 'two'), (3, 'three')")
        cur.execute(f"CREATE SEQUENCE {sequence}")
    connection.commit()
    monkeypatch.setenv("DATABASE_URL", scratch_pg_url)

    class Demo:
        pass

    demo = Demo()
    demo.table, demo.sequence = table, sequence

    def state():
        with connection.cursor() as cur:
            cur.execute(f"SELECT id, v FROM {table} ORDER BY id")
            rows = cur.fetchall()
            cur.execute(f"SELECT last_value, is_called FROM {sequence}")
            seq = cur.fetchone()
        connection.rollback()
        return rows, seq

    demo.state = state
    demo.untouched = state()
    yield demo
    connection.rollback()
    with connection.cursor() as cur:
        cur.execute(f"DROP TABLE {table}")
        cur.execute(f"DROP SEQUENCE {sequence}")
    connection.commit()
    connection.close()


def _actuators():
    return Actuators("infra_engineer", "task-ro", dry_run=False)


MORE_THAN_ONE_STATEMENT = [
    # the reviewer's input: the COMMIT ends the read-only transaction
    "COMMIT; DELETE FROM {t} WHERE id = 1 RETURNING id",
    "SET default_transaction_read_only = off; DELETE FROM {t}",
    "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE; DELETE FROM {t}",
    "BEGIN READ WRITE; DELETE FROM {t}",
    "SET TRANSACTION READ WRITE; WITH gone AS (DELETE FROM {t} RETURNING id) SELECT * FROM gone",
    "END; INSERT INTO {t} VALUES (4, 'four')",
    "ROLLBACK; UPDATE {t} SET v = 'changed'",
    "SELECT 1; DELETE FROM {t}",
    "-- harmless\nSELECT 1; SELECT setval('{s}', 100)",
    "SELECT 1 /* ; */ ; SELECT 2",
    "SELECT 'a'; SELECT 'b'",
]


@pytest.mark.parametrize("text", MORE_THAN_ONE_STATEMENT)
def test_more_than_one_statement_is_refused_before_a_connection_is_opened(demo, text):
    with patch("psycopg2.connect") as connect:
        with pytest.raises(PermissionError, match="exactly one statement"):
            _actuators().query_readonly(text.format(t=demo.table, s=demo.sequence))
    connect.assert_not_called()
    assert demo.state() == demo.untouched


@pytest.mark.parametrize("text", [
    "WITH gone AS (DELETE FROM {t} RETURNING id) SELECT count(*) FROM gone",
    "WITH ins AS (INSERT INTO {t} VALUES (4, 'four') RETURNING id) SELECT id FROM ins",
    "/* looks harmless */ UPDATE {t} SET v = 'changed'",
    "-- note\nDELETE FROM {t}",
    "SELECT setval('{s}', 100)",
    "SELECT nextval('{s}')",
    "SELECT * FROM {t} FOR UPDATE",
    "SELECT * INTO {t}_copy FROM {t}",
])
def test_the_database_refuses_the_writes_a_single_statement_can_make(demo, text):
    import psycopg2

    with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
        _actuators().query_readonly(text.format(t=demo.table, s=demo.sequence))
    assert demo.state() == demo.untouched


def test_the_session_default_is_read_only_when_the_lexer_is_wrong(demo):
    """The backstop. Blind the lexer (it counts every text as one statement)
    and send the reviewer's input: the COMMIT ends the transaction, and the
    DELETE starts one in a session whose default is read-only."""
    import psycopg2

    with patch("coordinator.actuators.count_statements", return_value=1):
        with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
            _actuators().query_readonly(f"COMMIT; DELETE FROM {demo.table} WHERE id = 1 RETURNING id")
    assert demo.state() == demo.untouched


@pytest.mark.parametrize("text, expected", [
    ("-- a comment; with a semicolon\nSELECT count(*) AS n FROM {t}", [{"n": 3}]),
    ("/* ; */ SELECT count(*) AS n FROM {t}", [{"n": 3}]),
    ("/* a /* nested ; */ still comment ; */ SELECT 1 AS n", [{"n": 1}]),
    ("SELECT count(*) AS n FROM {t} -- ; DELETE FROM {t}", [{"n": 3}]),
    ("SELECT 'a; DELETE FROM {t}; b' AS s", [{"s": "a; DELETE FROM {t}; b"}]),
    ("SELECT 'it''s; fine' AS s", [{"s": "it's; fine"}]),
    ("SELECT E'a\\'; DELETE FROM {t}; --' AS s", [{"s": "a'; DELETE FROM {t}; --"}]),
    ("SELECT $$a; DELETE FROM {t}; b$$ AS s", [{"s": "a; DELETE FROM {t}; b"}]),
    ("SELECT $tag$ ; $$ ; $tag$ AS s", [{"s": " ; $$ ; "}]),
    ("SELECT 1 AS \"a;b\"", [{"a;b": 1}]),
    ("SELECT count(*) AS n FROM {t};", [{"n": 3}]),
    ("SELECT count(*) AS n FROM {t}; -- done", [{"n": 3}]),
    ("SELECT count(*) AS n FROM {t} ;  /* x */", [{"n": 3}]),
])
def test_a_semicolon_in_a_comment_a_string_or_a_dollar_quote_is_not_refused(demo, text, expected):
    sql = text.format(t=demo.table)
    expected = [{k: (v.replace("{t}", demo.table) if isinstance(v, str) else v) for k, v in row.items()}
                for row in expected]
    assert _actuators().query_readonly(sql) == expected
    assert demo.state() == demo.untouched


def test_parameters_are_bound_and_a_percent_sign_is_doubled_as_before(demo):
    hostile = "x'; DELETE FROM " + demo.table + "; --"
    assert _actuators().query_readonly("SELECT %s AS s", (hostile,)) == [{"s": hostile}]
    assert _actuators().query_readonly("SELECT 'a%%b' AS s", ()) == [{"s": "a%b"}]
    assert _actuators().query_readonly(
        f"SELECT id, v FROM {demo.table} WHERE id = %s", (2,)) == [{"id": 2, "v": "two"}]
    assert demo.state() == demo.untouched


def test_a_parameter_that_lands_inside_a_quoted_placeholder_cannot_add_a_statement(demo):
    """The text is checked as it is sent, not only as it is written. In the
    template `'%s'` is one string literal, so it holds one statement; psycopg2
    puts its own quotes around the value, which closes that literal early, and
    what is sent holds three."""
    template = "SELECT '%s' AS s"
    assert count_statements(template) == 1
    with patch("psycopg2.connect", wraps=__import__("psycopg2").connect) as connect:
        with pytest.raises(PermissionError, match="exactly one statement"):
            _actuators().query_readonly(template, (f"x; DELETE FROM {demo.table}; --",))
    assert demo.state() == demo.untouched
    assert connect.called, "the refusal is of the text as sent, which needs the connection that quotes it"


def test_the_session_is_read_only_and_bounded(demo):
    row = _actuators().query_readonly(
        "SELECT current_setting('transaction_read_only') AS tx, "
        "current_setting('default_transaction_read_only') AS dflt, "
        "current_setting('statement_timeout') AS timeout, "
        "current_setting('standard_conforming_strings') AS strings")[0]
    assert row == {"tx": "on", "dflt": "on", "timeout": "15s", "strings": "on"}


def test_the_connection_is_opened_with_a_deadline(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost:1/none")
    with patch("psycopg2.connect", side_effect=RuntimeError("stop")) as connect:
        with pytest.raises(RuntimeError):
            _actuators().query_readonly("SELECT 1")
    assert connect.call_args.kwargs["connect_timeout"] == 15


def test_a_session_level_advisory_lock_does_not_outlive_the_call(demo, scratch_pg_url):
    """A kind of side effect a read-only transaction does allow (so does LOCK
    TABLE): it ends with the connection."""
    import psycopg2

    key = 424242
    assert _actuators().query_readonly(f"SELECT pg_advisory_lock({key}) AS locked") == [{"locked": ""}]
    # Closing a connection does not release its locks in the same instant (the
    # server process ends a moment later), so wait, with a bound, for the lock
    # to leave pg_locks before asserting that it is gone.
    connection = psycopg2.connect(scratch_pg_url)
    connection.autocommit = True
    try:
        deadline = time.monotonic() + 10
        while True:
            with connection.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s", (key,))
                held = cur.fetchone()[0]
            if not held or time.monotonic() > deadline:
                break
            threading.Event().wait(0.01)
        assert held == 0, "the advisory lock was still held 10s after the call returned"
    finally:
        connection.close()


@pytest.mark.parametrize("text", ["SELECT 'abc", "SELECT /* abc", "SELECT $$ abc", "SELECT \"abc", "", "-- nothing", ";"])
def test_text_with_no_clear_end_or_no_statement_is_refused(text, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(PermissionError):
        _actuators().query_readonly(text)
