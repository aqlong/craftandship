"""scripts/check_destructive_ddl.py against a corpus, in both directions.

  - EVASIONS: statements that destroy data, each written so that a check
    which reads the file line by line with regular expressions lets it
    through (a keyword pair split over two lines, a one-line DO block before
    it, a WHERE that restricts nothing, the statement inside a DO body or a
    string handed to EXECUTE, a comment that looks like an exemption). Every
    one must be refused: alone, appended to the real schema, and spliced into
    the middle of it.
  - LEGITIMATE: every statement of the real web/src/db/schema.sql, taken
    from the file as it is when the test runs, and a list of statement shapes
    that only look destructive. Every one must pass.
  - ROW CHANGES: schema.sql runs at every boot of the deployed web app, so an
    UPDATE, INSERT, DELETE, MERGE, COPY ... FROM or dynamic EXECUTE anywhere in
    it needs a "-- SAFE:" reason of its own, and a statement that names the
    status live is refused whatever comment is above it. The inputs for that
    are in web/tests/unit/fixtures/sql-scan-corpus.json, which the web unit
    tests (web/tests/unit/schema.idempotency.test.ts, with its own reader of
    SQL) read as well: the two readers are held to the same expectations, case
    by case, so that they cannot disagree about what a statement is.

The real schema is cut into statements here by a small line-based splitter
of this file's own, and the count is compared with what the checker's
tokenizer makes of the same text: a tokenizer that swallowed half the file
into one string would pass every "legitimate" case and fail that comparison.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_destructive_ddl import (
    DESTRUCTIVE_KINDS,
    EXECUTE_KIND,
    LIVE_KIND,
    MAX_BYTES,
    ROW_CHANGE_KINDS,
    UNREADABLE,
    check_sql,
    split_statements,
    tokenize,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_destructive_ddl.py"
SCHEMA = REPO_ROOT / "web" / "src" / "db" / "schema.sql"
CORPUS = REPO_ROOT / "web" / "tests" / "unit" / "fixtures" / "sql-scan-corpus.json"

DROP_TABLE, DROP_COLUMN, ALTER_TYPE = "DROP TABLE", "DROP COLUMN", "ALTER COLUMN TYPE"
TRUNCATE, DELETE = "TRUNCATE", "DELETE FROM (without WHERE)"

# (what makes it an evasion, the SQL, the kind it must be reported as)
EVASIONS = [
    # --- DROP TABLE ---
    ("a one-line DO block before it", "DO $$ BEGIN NULL; END $$;\nDROP TABLE users;", DROP_TABLE),
    ("the two keywords on separate lines", "DROP\nTABLE users;", DROP_TABLE),
    ("a trailing comment that names DROP CONSTRAINT", "DROP TABLE users; -- DROP CONSTRAINT", DROP_TABLE),
    ("a block comment between the keywords", "DROP /* soon */ TABLE users;", DROP_TABLE),
    ("tabs and lower case", "drop\ttable\tusers;", DROP_TABLE),
    ("IF EXISTS and CASCADE", "DROP TABLE IF EXISTS users CASCADE;", DROP_TABLE),
    ("a quoted name", 'DROP TABLE "users";', DROP_TABLE),
    ("several tables", "DROP TABLE public.users, public.builds;", DROP_TABLE),
    ("a foreign table", "DROP FOREIGN TABLE users;", DROP_TABLE),
    ("inside a one-line DO body", "DO $$ BEGIN DROP TABLE users; END $$;", DROP_TABLE),
    ("inside a tagged DO body, under an IF",
     "DO $body$\nBEGIN\n  IF true THEN\n    DROP TABLE users;\n  END IF;\nEND\n$body$;", DROP_TABLE),
    ("a string handed to EXECUTE", "DO $$ BEGIN EXECUTE 'DROP TABLE users'; END $$;", DROP_TABLE),
    ("a format string handed to EXECUTE", "DO $$ BEGIN EXECUTE format('DROP TABLE %I', 'users'); END $$;", DROP_TABLE),
    ("a dollar-quoted string handed to EXECUTE", "DO $$ BEGIN EXECUTE $q$DROP TABLE users$q$; END $$;", DROP_TABLE),
    ("a DO block inside a string handed to EXECUTE",
     "DO $a$ BEGIN EXECUTE 'DO $b$ BEGIN DROP TABLE users; END $b$'; END $a$;", DROP_TABLE),
    ("a function body", "CREATE OR REPLACE FUNCTION wipe() RETURNS void AS $$ BEGIN DROP TABLE users; END $$ LANGUAGE plpgsql;", DROP_TABLE),
    ("a function body given as a plain string", "CREATE FUNCTION wipe() RETURNS void AS 'DROP TABLE users' LANGUAGE sql;", DROP_TABLE),
    ("after a string with a doubled quote", "SELECT 'it''s fine'; DROP TABLE users;", DROP_TABLE),
    ("after a string that ends in a backslash", "SELECT 'a\\'; DROP TABLE users; --';", DROP_TABLE),
    ("after a dollar string that holds a semicolon", "SELECT $$ ; $$; DROP TABLE users;", DROP_TABLE),
    ("after a block comment that holds a semicolon", "/* done; */ DROP TABLE users;", DROP_TABLE),
    ("after a nested block comment", "/* a /* b */ c */ DROP TABLE users;", DROP_TABLE),
    ("second statement on a line", "CREATE TABLE IF NOT EXISTS t (id int); DROP TABLE users;", DROP_TABLE),
    ("second statement on a line whose first one is acknowledged",
     "-- SAFE: adds a table\nCREATE TABLE IF NOT EXISTS t (id int); DROP TABLE users;", DROP_TABLE),
    ("an acknowledgement inside a block comment", "DROP TABLE users; /* -- SAFE: no */", DROP_TABLE),
    ("an acknowledgement inside a string", "SELECT '-- SAFE: no'; DROP TABLE users;", DROP_TABLE),
    ("an acknowledgement two lines up, a blank line between", "-- SAFE: about something else\n\nDROP TABLE users;", DROP_TABLE),
    ("a whole schema", "DROP SCHEMA public CASCADE;", "DROP SCHEMA"),
    ("everything a role owns", "DROP OWNED BY cas;", "DROP OWNED"),
    ("the database", "DROP DATABASE craftandship;", "DROP DATABASE"),
    # --- ALTER TABLE ... DROP COLUMN ---
    ("no COLUMN keyword", "ALTER TABLE users DROP email;", DROP_COLUMN),
    ("IF EXISTS", "ALTER TABLE users DROP COLUMN IF EXISTS email;", DROP_COLUMN),
    ("each keyword on its own line", "ALTER TABLE users\n  DROP\n  COLUMN email;", DROP_COLUMN),
    ("the second action of two", "ALTER TABLE users ADD COLUMN x int, DROP COLUMN email;", DROP_COLUMN),
    ("after a DROP CONSTRAINT", "ALTER TABLE users DROP CONSTRAINT users_email_key, DROP COLUMN email;", DROP_COLUMN),
    ("ONLY, a schema and a quoted column", 'ALTER TABLE ONLY public.users DROP "email";', DROP_COLUMN),
    ("inside a DO body", "DO $$ BEGIN ALTER TABLE users DROP COLUMN email; END $$;", DROP_COLUMN),
    # --- ALTER COLUMN ... TYPE ---
    ("SET DATA TYPE", "ALTER TABLE users ALTER COLUMN email SET DATA TYPE int USING 0;", ALTER_TYPE),
    ("no COLUMN keyword", "ALTER TABLE users ALTER email TYPE int USING 0;", ALTER_TYPE),
    ("a quoted column with a space", 'ALTER TABLE users ALTER COLUMN "e mail" TYPE int;', ALTER_TYPE),
    ("IF EXISTS on the table", "ALTER TABLE IF EXISTS users ALTER COLUMN email TYPE varchar(1);", ALTER_TYPE),
    ("a column named type", "ALTER TABLE users ALTER type TYPE int USING 0;", ALTER_TYPE),
    ("inside a DO body, under an IF",
     "DO $$ BEGIN IF EXISTS (SELECT 1) THEN ALTER TABLE users ALTER COLUMN email TYPE int USING 0; END IF; END $$;", ALTER_TYPE),
    # --- TRUNCATE ---
    ("plain", "TRUNCATE users;", TRUNCATE),
    ("lower case, with options", "truncate table users restart identity cascade;", TRUNCATE),
    ("the table on the next line", "TRUNCATE\nusers;", TRUNCATE),
    ("inside a DO body", "DO $$ BEGIN TRUNCATE users; END $$;", TRUNCATE),
    # --- DELETE that restricts nothing ---
    ("no WHERE", "DELETE FROM users;", DELETE),
    ("WHERE true", "DELETE FROM users WHERE true;", DELETE),
    ("WHERE 1 = 1", "DELETE FROM users WHERE 1 = 1;", DELETE),
    ("WHERE 1=1 without spaces", "DELETE FROM users WHERE 1=1;", DELETE),
    ("two equal strings", "DELETE FROM users WHERE 'a' = 'a';", DELETE),
    ("WHERE (true)", "DELETE FROM users WHERE (true);", DELETE),
    ("WHERE NOT false", "DELETE FROM users WHERE NOT false;", DELETE),
    ("a real condition OR true", "DELETE FROM users WHERE id = 1 OR true;", DELETE),
    ("true OR a real condition", "DELETE FROM users WHERE true OR id = 1;", DELETE),
    ("the constant branch in parentheses", "DELETE FROM users WHERE (id = 1) OR (1 = 1);", DELETE),
    ("the whole OR in parentheses", "DELETE FROM users WHERE ((id = 1) OR (true));", DELETE),
    ("a constant IS NOT NULL", "DELETE FROM users WHERE 1 IS NOT NULL;", DELETE),
    ("a cast string", "DELETE FROM users WHERE 't'::boolean;", DELETE),
    ("a function of nothing", "DELETE FROM users WHERE now() IS NOT NULL;", DELETE),
    ("a schema-qualified function of nothing", "DELETE FROM users WHERE pg_catalog.now() IS NOT NULL;", DELETE),
    ("a special value compared with itself", "DELETE FROM users WHERE current_user = current_user;", DELETE),
    ("EXISTS of a constant", "DELETE FROM users WHERE EXISTS (SELECT 1);", DELETE),
    ("a count of another table", "DELETE FROM users WHERE (SELECT count(*) FROM builds) >= 0;", DELETE),
    ("a constant CASE", "DELETE FROM users WHERE CASE WHEN true THEN true END;", DELETE),
    ("WHERE and TRUE on their own lines", "DELETE FROM users\nWHERE\n  TRUE;", DELETE),
    ("ONLY", "DELETE FROM ONLY users;", DELETE),
    ("RETURNING and no WHERE", "DELETE FROM users RETURNING id;", DELETE),
    ("in a CTE, the WHERE belonging to the outer query",
     "WITH gone AS (DELETE FROM users RETURNING id) SELECT count(*) FROM gone WHERE id IS NOT NULL;", DELETE),
    ("the only WHERE inside a USING subquery",
     "DELETE FROM users USING (SELECT id FROM builds WHERE id IS NOT NULL) b;", DELETE),
    ("the WHERE in a line comment", "DELETE FROM users -- WHERE id = 1\n;", DELETE),
    ("the WHERE in a block comment", "DELETE FROM users /* WHERE id = 1 */;", DELETE),
    ("the WHERE after the semicolon, in a comment", "DELETE FROM users; -- WHERE id = 1", DELETE),
    ("inside a DO body", "DO $$ BEGIN DELETE FROM users; END $$;", DELETE),
    ("a string handed to EXECUTE", "DO $$ BEGIN EXECUTE 'DELETE FROM users WHERE true'; END $$;", DELETE),
    ("the action of a rule", "CREATE RULE r AS ON INSERT TO audit DO ALSO DELETE FROM users;", DELETE),
]

# SQL that cannot be cut into tokens is refused as it stands: the destructive
# statement may be anywhere behind the quote that never closes.
UNREADABLE_SQL = [
    ("a string that never closes", "SELECT 'unterminated; DROP TABLE users;"),
    ("a dollar quote that never closes", "DO $$ BEGIN DROP TABLE users; END;"),
    ("a tagged dollar quote closed with another tag", "DO $a$ BEGIN DROP TABLE users; END $b$;"),
    ("a block comment that never closes", "SELECT 1; /* DROP TABLE users;"),
    ("a quoted identifier that never closes", 'DROP TABLE "users;'),
]

# Statement shapes that mention a destructive keyword and destroy nothing, or
# that are acknowledged the way the header of the script describes.
LOOKS_DESTRUCTIVE_AND_IS_NOT = [
    "ALTER TABLE builds DROP CONSTRAINT IF EXISTS builds_status_check;",
    "DROP INDEX IF EXISTS idx_agent_tasks_category_hash;",
    "DROP TRIGGER IF EXISTS update_builds_updated_at ON builds;",
    "ALTER TABLE t ALTER COLUMN c DROP DEFAULT;",
    "ALTER TABLE t ALTER COLUMN c DROP NOT NULL;",
    "ALTER TABLE t ALTER COLUMN c SET DEFAULT 0;",
    "ALTER TABLE t ALTER COLUMN type SET NOT NULL;",
    "ALTER TABLE t ADD COLUMN IF NOT EXISTS type TEXT;",
    "ALTER TABLE t ADD COLUMN IF NOT EXISTS truncated_at TIMESTAMP, ADD COLUMN IF NOT EXISTS dropped BOOLEAN;",
    "SELECT 'DROP TABLE users';",
    "SELECT $$DROP TABLE users; TRUNCATE x; DELETE FROM y$$;",
    "SELECT E'a\\'; DROP TABLE users; --';",
    "-- DROP TABLE users;",
    "/* DROP TABLE users; TRUNCATE x; */ SELECT 1;",
    "COMMENT ON TABLE builds IS 'never DROP TABLE this, never TRUNCATE it';",
    'CREATE TABLE IF NOT EXISTS t (id int, "drop table" text, truncated_at timestamptz);',
    "CREATE TABLE IF NOT EXISTS t (id int REFERENCES builds(id) ON DELETE CASCADE);",
    "CREATE TRIGGER t BEFORE UPDATE ON builds FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();",
    "CREATE RULE r AS ON DELETE TO t DO INSTEAD NOTHING;",
    "DO $$ BEGIN RAISE NOTICE 'DROP TABLE skipped, nothing to TRUNCATE'; END $$;",
    "DO $$ BEGIN EXECUTE 'CREATE INDEX IF NOT EXISTS i ON t (id)'; END $$;",
    # acknowledged
    "-- SAFE: the cache holds nothing that is not rebuilt\nDROP TABLE old_cache;",
    "DROP TABLE old_cache; -- SAFE: the cache holds nothing that is not rebuilt",
    "-- SAFE: widening\n-- (a second comment line)\nALTER TABLE builds\n  ALTER COLUMN app_name TYPE VARCHAR(200);",
    "-- SAFE: widening\nDO $$\nBEGIN\n  ALTER TABLE agent_tasks ALTER COLUMN context_hash TYPE VARCHAR(32);\nEND $$;",
    "DO $$\nBEGIN\n  -- SAFE: widening\n  ALTER TABLE agent_tasks ALTER COLUMN context_hash TYPE VARCHAR(32);\nEND $$;",
    "DO $$ BEGIN\n  TRUNCATE scratch; -- SAFE: a scratch table\nEND $$;",
]


# Statements that change rows without being the unrestricted DELETE the check
# was first written for: the WHERE names a column (or the statement is an
# INSERT), so they are not DELETE FROM (without WHERE), but they are row
# changes and schema.sql runs at every boot. Each is refused as the kind given
# until it carries a reason of its own.
ROW_CHANGES_WITH_A_RESTRICTION = [
    ("DELETE FROM user_sessions WHERE expire < NOW();", "DELETE"),
    ("DELETE FROM builds WHERE id = $1;", "DELETE"),
    ("DELETE FROM builds b USING customers c WHERE b.customer_id = c.id AND c.email = 'x';", "DELETE"),
    ("DELETE FROM t WHERE id IN (SELECT id FROM t ORDER BY created_at LIMIT 10);", "DELETE"),
    ("DELETE FROM t WHERE true AND id = 4;", "DELETE"),
    ("DELETE FROM t WHERE (status = 'failed' OR status = 'queued') AND created_at < NOW() - INTERVAL '30 days';",
     "DELETE"),
    ('DELETE FROM t WHERE "true" = 1;', "DELETE"),
    ("DELETE FROM t WHERE id = 1 RETURNING *;", "DELETE"),
    ("DELETE FROM t WHERE EXISTS (SELECT 1 FROM builds b WHERE b.customer_id = t.id);", "DELETE"),
    ("WITH old AS (DELETE FROM t WHERE created_at < NOW() RETURNING id) SELECT count(*) FROM old;", "DELETE"),
    ("DO $$ BEGIN DELETE FROM t WHERE id = 1; END $$;", "DELETE"),
    ("INSERT INTO notes (body) VALUES ('please DELETE FROM your inbox') ON CONFLICT DO NOTHING;", "INSERT"),
]


# An evasion whose string is built by format() is also dynamic SQL, which is
# refused as such until it is acknowledged.
BUILT_AT_RUN_TIME = {"a format string handed to EXECUTE"}


def _expected(why, kind):
    return sorted([kind, EXECUTE_KIND] if why in BUILT_AT_RUN_TIME else [kind])


def _kinds(sql):
    return [kind for _, _, kind in check_sql(sql)]


def _real_statements():
    """The real schema cut into statements, each with the comment and blank
    lines above it: a line that ends in a semicolon outside a $$ body ends a
    statement. Not the checker's tokenizer; see the module docstring."""
    statements, current, in_body = [], [], False
    for line in SCHEMA.read_text().splitlines(keepends=True):
        current.append(line)
        code = line.split("--", 1)[0]
        if code.count("$$") % 2:
            in_body = not in_body
        if not in_body and code.rstrip().endswith(";"):
            statements.append("".join(current))
            current = []
    return statements, "".join(current)


def test_the_corpus_is_as_large_as_it_says():
    assert len(EVASIONS) >= 40
    assert len({sql for _, sql, _ in EVASIONS}) == len(EVASIONS), "an evasion is listed twice"
    assert {kind for _, _, kind in EVASIONS} == set(DESTRUCTIVE_KINDS), \
        "every kind the script refuses has at least one evasion here, and no other kind is expected"
    statements, _ = _real_statements()
    assert len(statements) >= 40


@pytest.mark.parametrize("why,sql,kind", EVASIONS, ids=[f"{kind}: {why}" for why, _, kind in EVASIONS])
def test_an_evasion_is_refused_on_its_own(why, sql, kind):
    assert _kinds(sql) == _expected(why, kind), check_sql(sql)


@pytest.mark.parametrize("why,sql", UNREADABLE_SQL, ids=[why for why, _ in UNREADABLE_SQL])
def test_sql_that_cannot_be_read_is_refused(why, sql):
    kinds = _kinds(sql)
    assert kinds and all(kind.startswith(UNREADABLE) for kind in kinds), kinds


def test_the_real_schema_passes_and_is_cut_where_its_statements_end():
    text = SCHEMA.read_text()
    assert check_sql(text) == []
    statements, remainder = _real_statements()
    assert "".join(statements) + remainder == text, "the test's splitter lost text"
    assert remainder.strip() == "" or remainder.lstrip().startswith("--"), remainder[:80]
    tokens, _ = tokenize(text)
    assert len(split_statements(tokens)) == len(statements), \
        "the checker's tokenizer and a plain line splitter disagree on how many statements the schema has"


def test_every_statement_of_the_real_schema_passes_on_its_own():
    statements, _ = _real_statements()
    refused = {statement: check_sql(statement) for statement in statements if check_sql(statement)}
    assert refused == {}


@pytest.mark.parametrize("sql", LOOKS_DESTRUCTIVE_AND_IS_NOT)
def test_a_statement_that_only_looks_destructive_passes(sql):
    assert check_sql(sql) == []


@pytest.mark.parametrize("sql,kind", ROW_CHANGES_WITH_A_RESTRICTION,
                         ids=[sql[:60] for sql, _ in ROW_CHANGES_WITH_A_RESTRICTION])
def test_a_row_change_with_a_restriction_is_not_the_unrestricted_delete_and_is_refused_all_the_same(sql, kind):
    assert _kinds(sql) == [kind]
    # a reason of its own on its line settles it, and nothing else does
    assert check_sql(sql + " -- SAFE: a reason") == []
    assert _kinds("-- SAFE: a reason\nSELECT 1;\n" + sql) == [kind], "a comment above another statement is not a reason for this one"


def test_every_evasion_is_refused_at_the_end_of_the_real_schema():
    schema = SCHEMA.read_text()
    schema_lines = schema.count("\n") + (not schema.endswith("\n"))
    for why, sql, kind in EVASIONS:
        found = check_sql(schema + ("" if schema.endswith("\n") else "\n") + sql + "\n")
        assert [k for _, _, k in found] == _expected(why, kind), (why, found)
        assert all(line > schema_lines for line, _, _ in found), "the finding points into the appended statement"


def test_every_evasion_is_refused_in_the_middle_of_the_real_schema():
    # After the first real DO block and after a statement half way down: what
    # comes before and after the splice must not change the verdict, and no
    # real statement may be blamed for it.
    statements, remainder = _real_statements()
    first_do = next(i for i, s in enumerate(statements) if s.lstrip("\n").split("\n")[-2].startswith("END $$")
                    or "\nDO $$" in "\n" + s)
    for position in (first_do + 1, len(statements) // 2):
        head, tail = "".join(statements[:position]), "".join(statements[position:]) + remainder
        head_lines = head.count("\n")
        for why, sql, kind in EVASIONS:
            spliced_lines = sql.count("\n") + 1
            found = check_sql(head + sql + "\n" + tail)
            assert [k for _, _, k in found] == _expected(why, kind), (position, why, found)
            assert all(head_lines < line <= head_lines + spliced_lines for line, _, _ in found), (why, found)


def test_a_finding_names_the_line_of_the_destructive_keyword():
    sql = "CREATE TABLE IF NOT EXISTS t (id int);\n\nDO $$\nBEGIN\n  IF true THEN\n    DROP TABLE users;\n  END IF;\nEND $$;\n"
    assert check_sql(sql) == [(6, "DROP TABLE users;", DROP_TABLE)]


def test_bodies_nested_too_deep_are_refused_not_skipped():
    sql = "DROP TABLE users"
    for depth in range(12):
        sql = f"DO $t{depth}$ BEGIN EXECUTE $s{depth}${sql}$s{depth}$; END $t{depth}$"
    kinds = _kinds(sql + ";")
    assert kinds and any(kind.startswith(UNREADABLE) for kind in kinds), kinds


def _cli(*args, stdin=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)


def test_the_command_reads_standard_input_and_names_it(tmp_path):
    # how the pre-commit hook hands it the staged schema
    refused = _cli("-", "web/src/db/schema.sql (as staged)", stdin="DROP\nTABLE users;\n")
    assert refused.returncode == 1
    assert "DESTRUCTIVE DDL detected in web/src/db/schema.sql (as staged):" in refused.stdout
    assert "Line 1 [DROP TABLE]" in refused.stdout
    accepted = _cli("-", stdin="CREATE TABLE IF NOT EXISTS t (id int);\n")
    assert accepted.returncode == 0 and "standard input" in accepted.stdout


def test_the_command_passes_the_real_schema_and_refuses_it_with_an_evasion_appended(tmp_path):
    assert _cli().returncode == 0
    copy = tmp_path / "schema.sql"
    copy.write_text(SCHEMA.read_text() + "\nDELETE FROM customers WHERE true;\n")
    result = _cli(str(copy))
    assert result.returncode == 1 and "[DELETE FROM (without WHERE)]" in result.stdout


def test_an_input_larger_than_the_limit_is_an_error_not_a_pass(tmp_path):
    big = tmp_path / "big.sql"
    big.write_text("-- padding\n" * (MAX_BYTES // 11 + 2) + "DROP TABLE users;\n")
    result = _cli(str(big))
    assert result.returncode == 2 and "larger than" in result.stderr
    assert "No unacknowledged" not in result.stdout


# ---------------------------------------------------------------------------
# Row changes, and the status live: the corpus the web unit tests also read
# ---------------------------------------------------------------------------

_NORMAL = {"DELETE FROM (without WHERE)": "DELETE", EXECUTE_KIND: "EXECUTE", LIVE_KIND: "LIVE"}
_COMPARED = {"UPDATE", "INSERT", "DELETE", "MERGE", "COPY FROM", "TRUNCATE", "EXECUTE", "LIVE"}


def _cases():
    return json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]


def _findings(sql):
    """What the web reader reports, from this reader's violations: the row
    changes and the status rule, by their shared names."""
    return sorted({(line, _NORMAL.get(kind, kind)) for line, _, kind in check_sql(sql)
                   if _NORMAL.get(kind, kind) in _COMPARED})


def _unreadable(sql):
    return any(kind.startswith(UNREADABLE) for _, _, kind in check_sql(sql))


@pytest.mark.parametrize("case", _cases(), ids=[f"{c['group']}: {c['name']}" for c in _cases()])
def test_this_reader_agrees_with_the_corpus_the_web_reader_is_held_to(case):
    sql = case["sql"]
    if case.get("unreadable"):
        assert _unreadable(sql), "an unclosed quote or comment is refused, not skipped"
        return
    assert not _unreadable(sql)
    tokens, _ = tokenize(sql)
    assert len(split_statements(tokens)) == case["statements"]
    assert _findings(sql) == sorted(tuple(f) for f in case["findings"])


def test_the_corpus_covers_what_the_two_readers_must_agree_on():
    cases = _cases()
    assert len(cases) >= 150
    assert len({c["sql"] for c in cases}) == len(cases), "a case is listed twice"
    assert {c["group"] for c in cases} == {"split", "rows", "bodies", "acknowledged", "live"}
    assert sum(1 for c in cases if c.get("splice")) >= 40
    seen = {kind for c in cases for _, kind in c.get("findings", [])}
    assert seen == _COMPARED, "every kind both readers report has a case, and no other kind is expected"
    assert {_NORMAL.get(kind, kind) for kind in ROW_CHANGE_KINDS} <= seen, \
        "every kind of row change the check knows has a case"


def _spliced_cases():
    return [c for c in _cases() if c.get("splice")]


def test_every_row_change_in_the_corpus_is_refused_at_the_end_of_the_real_schema():
    schema = SCHEMA.read_text()
    schema_lines = schema.count("\n") + (not schema.endswith("\n"))
    for case in _spliced_cases():
        found = check_sql(schema + ("" if schema.endswith("\n") else "\n") + case["sql"] + "\n")
        assert found, case["name"]
        assert not _unreadable(case["sql"]), case["name"]
        assert all(line > schema_lines for line, _, _ in found), (case["name"], found)


def test_every_row_change_in_the_corpus_is_refused_in_the_middle_of_the_real_schema():
    statements, remainder = _real_statements()
    for position in (1, len(statements) // 2):
        head, tail = "".join(statements[:position]), "".join(statements[position:]) + remainder
        head_lines = head.count("\n")
        for case in _spliced_cases():
            sql = case["sql"]
            found = check_sql(head + sql + "\n" + tail)
            assert found, (position, case["name"])
            assert all(head_lines < line <= head_lines + sql.count("\n") + 1 for line, _, _ in found), \
                (position, case["name"], found)


def test_the_reviewers_mutation_is_refused_by_this_reader_and_by_the_command():
    # A tagged DO block that sets a build live: the block the web test's earlier
    # reader (which toggled on $$ alone) read as one opaque statement.
    mutation = "\nDO $m$\nBEGIN\n  UPDATE builds SET status = 'live' WHERE id IS NOT NULL;\nEND\n$m$;\n"
    found = check_sql(SCHEMA.read_text() + mutation)
    assert sorted(kind for _, _, kind in found) == [LIVE_KIND, "UPDATE"]
    result = subprocess.run([sys.executable, str(SCRIPT), "-", "schema.sql (as staged)"],
                            input=SCHEMA.read_text() + mutation, capture_output=True, text=True, cwd=REPO_ROOT,
                            timeout=60)
    assert result.returncode == 1
    assert f"[{LIVE_KIND}]" in result.stdout and "[UPDATE]" in result.stdout
    # the reason the checker would accept for the UPDATE does not settle the status
    acknowledged = mutation.replace("  UPDATE", "  -- SAFE: a reason\n  UPDATE")
    found = check_sql(SCHEMA.read_text() + acknowledged)
    assert [kind for _, _, kind in found] == [LIVE_KIND]


def test_every_row_change_in_the_real_schema_needs_the_reason_written_for_it():
    # The nine UPDATEs of the real file each carry a "-- SAFE:" line directly
    # above them. Take any one of those lines away and exactly that statement
    # is refused: no other comment in the file stands in for it.
    lines = SCHEMA.read_text().splitlines(keepends=True)
    reasons = [i for i, line in enumerate(lines)
               if line.strip().startswith("-- SAFE:") and lines[i + 1].lstrip().startswith("UPDATE")]
    assert len(reasons) == 9, "the real schema's row changes are the nine its test lists"
    for index in reasons:
        without = "".join(lines[:index] + lines[index + 1:])
        found = check_sql(without)
        assert [kind for _, _, kind in found] == ["UPDATE"], (index, found)
        assert found[0][0] == index + 1, "and it is the statement that lost its reason"
        # a comment above the block it sits in does not take its place
        block = next(i for i in range(index, -1, -1) if lines[i].startswith("DO "))
        covered = "".join(lines[:block] + ["-- SAFE: a reason for the whole block\n"] + lines[block:index]
                          + lines[index + 1:])
        assert [kind for _, _, kind in check_sql(covered)] == ["UPDATE"], index


@pytest.mark.skipif(not (REPO_ROOT / "web" / "node_modules" / ".bin" / "vitest").exists(),
                    reason="needs `npm ci` in web/")
def test_the_web_reader_passes_the_same_corpus():
    # The other reader, in the web unit tests. Running it here means a change
    # to the corpus or to either reader that makes them disagree fails this
    # file too, not only the web suite.
    if shutil.which("node") is None:
        pytest.skip("needs node")
    result = subprocess.run(["npx", "vitest", "run", "tests/unit/schema.idempotency.test.ts"],
                            cwd=REPO_ROOT / "web", capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
    assert "passed" in result.stdout
