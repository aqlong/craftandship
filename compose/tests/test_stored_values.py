"""A seed value or a column default that its column refuses stops the whole
seed script (or the whole schema), and the site never boots.

`validate_spec` accepted such a spec: the value's JSON type was never
compared with the column's type. The engine now checks, while the spec is
built, what Postgres is certain to refuse (`_stored_value_problem`,
`_default_problem` in compose/build.py), and leaves out what cannot be
stored, with an engine warning that says what: a seed row with such a value
is not written (`seed_value_unfit`), and a default of that kind is not put on
the column (`column_default_ignored`).

The check is only worth anything if it agrees with Postgres, so the first
test asks a real database about every pair of a column type and a value in a
corpus, with the literals the engine writes (`_seed_literal`,
`_format_sql_default`): the engine must never take what Postgres refuses, and
the values it refuses although Postgres would take them are listed here, one
by one, so that the check cannot grow stricter without a test saying so.
The rest need no database.
"""

from __future__ import annotations

import copy
import json
import math
import time
from pathlib import Path

import pytest

from compose import build as B
from compose.build import BuildSpec, compose_files, resolve_spec, validate_spec
from compose.tests import _site_check as SC

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

COLUMN_TYPES = [
    "TEXT", "VARCHAR(10)", "CHAR(4)", "INTEGER", "SMALLINT", "BIGINT", "NUMERIC(10,2)", "NUMERIC", "REAL",
    "BOOLEAN", "DATE", "TIME", "TIMESTAMPTZ", "TIMESTAMP", "JSON", "JSONB", "UUID", "TEXT[]", "INTEGER[]",
]
# A timestamp longer than the engine reads (`_STORED_TIMESTAMP_MAX_CHARS`).
LONG_TIMESTAMP = "2030-01-15 17:30:00." + "1" * 60
VALUES = [
    None, True, False, 0, 7, -7, 7.5, 2147483647, 2147483648, 32768, 9223372036854775807, 9223372036854775808,
    1e20, 12345678901.5,
    "7", " 7 ", "7.5", "1e3", "abc", "", "true", "yes", "on", "1", "t", "f", "no", "maybe",
    "2030-01-15", "2030-02-30", "2030-01-15T17:30:00Z", "2030-01-15 17:30", "2030-01-15T17:30", "17:30", "17:30:00",
    "2030-01-15 x", "2030-01-15T", "2030-01-15 17:30:00+05", "2030-01-15 17:30:00.25 America/Chicago",
    "25:00", "soon",
    "00000000-0000-4000-8000-000000000001", "{00000000-0000-4000-8000-000000000001}", "not-a-uuid",
    "{a,b}", "{}", "{1,2}", '{"a b",c}', "{{1,2},{3,4}}", '["a"]', '{"a":1}', "plain",
    ["a", 1, None], {"x": 1}, "x" * 100, LONG_TIMESTAMP,
]
# What the engine refuses although Postgres takes it: forms nobody writes into
# a spec, left out rather than allowed on a guess.
STRICTER = {
    ("TIME", "2030-01-15 17:30"),  # a date in front of the time
    ("TEXT[]", "{{1,2},{3,4}}"), ("INTEGER[]", "{{1,2},{3,4}}"),  # an array of more than one dimension
    # A timestamp longer than `_STORED_TIMESTAMP_MAX_CHARS`: Postgres takes the fraction digits.
    ("TIMESTAMP", LONG_TIMESTAMP), ("TIMESTAMPTZ", LONG_TIMESTAMP), ("TIME", LONG_TIMESTAMP),
    # A time-zone name or a numeric offset after a time: the DATE, TIME and timestamp
    # checks refuse the zone (no spec writes one). Postgres takes it and drops the zone.
    ("DATE", "2030-01-15 17:30:00.25 America/Chicago"),
    ("TIME", "2030-01-15 17:30:00+05"), ("TIME", "2030-01-15 17:30:00.25 America/Chicago"),
    ("TIMESTAMP", "2030-01-15 17:30:00.25 America/Chicago"),
    ("TIMESTAMPTZ", "2030-01-15 17:30:00.25 America/Chicago"),
}


@pytest.fixture(scope="module")
def database_url():
    pytest.importorskip("psycopg2")
    from compose.tests._scratch_pg import scratch_database_url
    with scratch_database_url() as url:
        yield url


def _accepted_by_postgres(url: str, statements) -> list:
    """For each statement `(create, insert)` run in a fresh table: whether
    Postgres accepts it. One connection, one schema, rolled back per case."""
    import psycopg2
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS stored_values CASCADE; CREATE SCHEMA stored_values; "
                        "SET search_path TO stored_values")
            results = []
            for create, insert in statements:
                try:
                    cur.execute("DROP TABLE IF EXISTS x; " + create)
                    if insert:
                        cur.execute(insert)
                    results.append(True)
                except psycopg2.Error:
                    results.append(False)
            cur.execute("DROP SCHEMA stored_values CASCADE")
            return results
    finally:
        conn.close()


def test_the_engine_takes_no_seed_value_that_postgres_refuses(database_url):
    pairs = [(t, v) for t in COLUMN_TYPES for v in VALUES]
    statements = [(f"CREATE TABLE x (v {t})", f"INSERT INTO x (v) VALUES ({B._seed_literal(v)})") for t, v in pairs]
    accepted = _accepted_by_postgres(database_url, statements)
    too_lenient, too_strict = [], []
    for (column_type, value), postgres_takes in zip(pairs, accepted):
        problem = B._stored_value_problem(value, column_type)
        if problem is None and not postgres_takes:
            too_lenient.append((column_type, value))
        if problem is not None and postgres_takes and (column_type, value) not in STRICTER:
            too_strict.append((column_type, value, problem))
    assert too_lenient == [], "the engine would write a seed value Postgres refuses"
    assert too_strict == [], "the engine would drop a seed value Postgres takes"
    # The corpus has teeth: Postgres refuses plenty of it, and takes plenty of it.
    assert 150 < sum(1 for ok in accepted if not ok) and 100 < sum(1 for ok in accepted if ok)


DEFAULTS = [
    None, "", True, False, 0, 1, 7, 7.5, "0", "7", "7.5", "FALSE", "true", "NOW()", "now()", "CURRENT_TIMESTAMP",
    "CURRENT_DATE", "CURRENT_TIME", "GEN_RANDOM_UUID()", "NULL", "pending", "abc", "2020-01-01", "12:00",
    "2020-01-01T10:00:00Z", "'[]'::jsonb", "'{}'::jsonb", "'x'::text", "'1'::int", "[1, 2]", {"a": 1},
]
DEFAULT_TYPES = ["TEXT", "VARCHAR(5)", "INTEGER", "BIGINT", "NUMERIC(10,2)", "BOOLEAN", "DATE", "TIME", "TIMESTAMPTZ",
                  "JSONB", "UUID"]
DEFAULT_STRICTER = {
    # `'x'::type` is taken when the type is the column's own kind, and not otherwise,
    # although Postgres converts some of them on its own.
    ("TEXT", "'[]'::jsonb"), ("TEXT", "'{}'::jsonb"), ("TEXT", "'1'::int"),
    ("VARCHAR(5)", "'[]'::jsonb"), ("VARCHAR(5)", "'{}'::jsonb"), ("VARCHAR(5)", "'1'::int"),
    ("NUMERIC(10,2)", "'1'::int"),
}


def test_the_engine_puts_no_default_on_a_column_that_postgres_refuses_it_for(database_url):
    pairs = [(t, d) for t in DEFAULT_TYPES for d in DEFAULTS]
    statements = [(f"CREATE TABLE x (v {t} DEFAULT {B._format_sql_default(d)})", None) for t, d in pairs
                  if d is not None and d != ""]
    runnable = [(t, d) for t, d in pairs if d is not None and d != ""]
    accepted = _accepted_by_postgres(database_url, statements)
    too_lenient, too_strict = [], []
    for (column_type, default), postgres_takes in zip(runnable, accepted):
        problem = B._default_problem(default, {"name": "v", "sql_type": column_type})
        if problem is None and not postgres_takes:
            too_lenient.append((column_type, default))
        # Postgres checks the width of a default when a row uses it, not when
        # the table is created; a default that no row could take is left off.
        too_wide = problem is not None and "characters long" in problem
        if problem is not None and postgres_takes and not too_wide and (column_type, str(default)) not in DEFAULT_STRICTER:
            too_strict.append((column_type, default, problem))
    assert too_lenient == [], "the engine would put on a column a default Postgres refuses"
    assert too_strict == [], "the engine would drop a default Postgres takes"


# ---------------------------------------------------------------------------
# What the build does with them
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "skip")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")


def _spec() -> dict:
    spec = json.loads((FIXTURES / "sample-coach-spec.json").read_text(encoding="utf-8"))
    spec["data_models"] = [{"name": "Gadget", "plural": "gadgets", "fields": [
        {"name": "title", "sql_type": "VARCHAR(20)", "required": True},
        {"name": "quantity", "sql_type": "INTEGER", "default": 1},
        {"name": "shipped", "sql_type": "BOOLEAN", "default": "FALSE"},
        {"name": "arrives_on", "sql_type": "DATE"},
        {"name": "specs", "sql_type": "JSONB", "default": "'{}'::jsonb"},
    ]}]
    return spec


def _warnings(spec: dict, reason: str) -> list:
    rs = resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))
    compose_files(rs)
    return [w["detail"] for w in rs.spec.engine_warnings if w["reason"] == reason]


def test_a_seed_row_with_a_value_its_column_cannot_store_is_left_out_and_reported():
    spec = _spec()
    spec["seed_content"] = {"gadgets": [
        {"title": "Fine", "quantity": 3, "shipped": True, "arrives_on": "2030-01-15", "specs": {"a": 1}},
        {"title": "Too many", "quantity": "lots"},
        {"title": "Not a flag", "shipped": 1},
        {"title": "A title that is far too long for its column"},
        {"title": "Not a date", "arrives_on": "soon"},
        {"title": "Fine too", "quantity": "4", "shipped": "yes"},
    ]}
    assert validate_spec(BuildSpec.from_dict(copy.deepcopy(spec))) == []
    seed = dict(compose_files(resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))))["db/seed.sql"]
    rows = [line for line in seed.splitlines() if line.startswith("INSERT INTO gadgets ")]
    assert [line.split("VALUES ", 1)[1].split(",", 1)[0] for line in rows] == ["('Fine'", "('Fine too'"]
    (warning,) = _warnings(spec, "seed_value_unfit")
    assert warning.startswith("Gadget: dropped 4 of 6 seed rows with a value its column cannot store (")
    assert "quantity is not a whole number" in warning


def test_a_model_whose_every_seed_row_is_unfit_is_not_seeded_at_all():
    spec = _spec()
    spec["seed_content"] = {"gadgets": [{"title": "One", "quantity": "x"}, {"title": "Two", "shipped": 7}]}
    seed = dict(compose_files(resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))))["db/seed.sql"]
    assert "INSERT INTO gadgets" not in seed
    assert "-- Gadget seed SKIPPED: no row has values its columns can store; admin can populate via /admin/gadgets" in seed


def test_a_default_its_column_cannot_take_is_not_put_on_it_and_the_report_says_so():
    spec = _spec()
    spec["data_models"][0]["fields"] += [
        {"name": "rank", "sql_type": "INTEGER", "default": "high"},
        {"name": "ready", "sql_type": "BOOLEAN", "default": 3},
        {"name": "label", "sql_type": "VARCHAR(5)", "default": "far too long"},
    ]
    assert validate_spec(BuildSpec.from_dict(copy.deepcopy(spec))) == []
    schema = dict(compose_files(resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))))["db/schema.sql"]
    table = schema.split("CREATE TABLE IF NOT EXISTS gadgets (", 1)[1].split(");", 1)[0]
    assert "  rank INTEGER," in table and "  ready BOOLEAN," in table and "  label VARCHAR(5)," in table
    # The defaults that fit stay.
    assert "quantity INTEGER DEFAULT 1" in table and "shipped BOOLEAN DEFAULT FALSE" in table
    assert "specs JSONB DEFAULT '{}'::jsonb" in table
    details = _warnings(spec, "column_default_ignored")
    assert len(details) == 3
    assert any(d.startswith("Gadget.rank: the default 'high' cannot be stored in a INTEGER column (") for d in details)


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("sample-*-spec.json")))
def test_the_sample_specs_lose_no_seed_row_and_no_default_to_the_check(name):
    """The check must not take anything the shipped samples rely on."""
    spec = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    rs = resolve_spec(BuildSpec.from_dict(spec))
    compose_files(rs)
    leaked = [w for w in rs.spec.engine_warnings if w["reason"] in ("seed_value_unfit", "column_default_ignored")]
    assert leaked == []


def test_the_seed_literal_of_each_kind_of_value():
    assert B._seed_literal(None) == "NULL"
    assert B._seed_literal(True) == "TRUE" and B._seed_literal(False) == "FALSE"
    assert B._seed_literal(7) == "7" and B._seed_literal(7.5) == "7.5"
    assert B._seed_literal(math.nan) == "NULL" and B._seed_literal(math.inf) == "NULL"
    assert B._seed_literal("it's") == "'it''s'"
    assert B._seed_literal(["a", 1, None]) == "'[\"a\", 1, null]'"


# ---------------------------------------------------------------------------
# Time bounds. A spec string may hold 100,000 characters. On the earlier code a
# 99,000-character value that was a date followed by spaces and a stray
# character took about 30 seconds in the timestamp check, holding the GIL the
# whole time. Each path that reads a stored value is timed here with that value.
# ---------------------------------------------------------------------------

TIME_BOUND_SECONDS = 1.0
# A valid date, 99,000 spaces, and a character the timestamp pattern does not take.
HOSTILE_TIMESTAMP = "2030-01-15" + " " * 99_000 + "!"


def _timed(call):
    started = time.perf_counter()
    result = call()
    return result, time.perf_counter() - started


def _hostile_timestamp_spec(**field) -> dict:
    spec = _spec()
    spec["data_models"][0]["fields"].append({"name": "due_at", "sql_type": "TIMESTAMPTZ", **field})
    return spec


def test_the_hostile_timestamp_fixture_is_a_string_the_spec_validator_accepts_as_is():
    assert len(HOSTILE_TIMESTAMP) < B._MAX_SPEC_STRING
    assert validate_spec(BuildSpec.from_dict(_hostile_timestamp_spec())) == []


def test_a_hostile_timestamp_seed_value_is_refused_within_the_time_bound_on_every_path():
    spec = _hostile_timestamp_spec()
    spec["seed_content"] = {"gadgets": [{"title": "Hostile", "due_at": HOSTILE_TIMESTAMP}]}
    errors, validate_seconds = _timed(lambda: validate_spec(BuildSpec.from_dict(copy.deepcopy(spec))))
    assert errors == [] and validate_seconds < TIME_BOUND_SECONDS, f"validate_spec took {validate_seconds:.2f}s"
    rs, resolve_seconds = _timed(lambda: resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec))))
    assert resolve_seconds < TIME_BOUND_SECONDS, f"resolve_spec took {resolve_seconds:.2f}s"
    (name, seed), emit_seconds = _timed(lambda: B.emit_seed_sql(rs))
    assert emit_seconds < TIME_BOUND_SECONDS, f"emit_seed_sql took {emit_seconds:.2f}s"
    assert name == "db/seed.sql" and "INSERT INTO gadgets " not in seed
    unfit = [w["detail"] for w in rs.spec.engine_warnings if w["reason"] == "seed_value_unfit"]
    assert len(unfit) == 1 and "due_at is not a date and time" in unfit[0]


def test_a_hostile_timestamp_default_is_refused_within_the_time_bound():
    spec = _hostile_timestamp_spec(default=HOSTILE_TIMESTAMP)
    rs, resolve_seconds = _timed(lambda: resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec))))
    assert resolve_seconds < TIME_BOUND_SECONDS, f"resolve_spec took {resolve_seconds:.2f}s"
    (name, schema), emit_seconds = _timed(lambda: B.emit_schema_sql(rs))
    assert emit_seconds < TIME_BOUND_SECONDS, f"emit_schema_sql took {emit_seconds:.2f}s"
    table = schema.split("CREATE TABLE IF NOT EXISTS gadgets (", 1)[1].split(");", 1)[0]
    assert "  due_at TIMESTAMPTZ," in table, "the column keeps its type and loses the default"
    details = [w["detail"] for w in rs.spec.engine_warnings if w["reason"] == "column_default_ignored"]
    assert len(details) == 1 and details[0].startswith("Gadget.due_at: the default ")


def test_a_timestamp_longer_than_64_characters_is_refused_where_the_pattern_would_match():
    # The bound is 64 characters (`_STORED_TIMESTAMP_MAX_CHARS` in compose/build.py).
    # Postgres takes the fraction digits of LONG_TIMESTAMP (it rounds them); the engine does not.
    bound = 64
    assert len(LONG_TIMESTAMP) > bound
    assert B._stored_value_problem(LONG_TIMESTAMP, "TIMESTAMPTZ") == "is not a date and time (YYYY-MM-DD HH:MM)"
    at_bound = "2030-01-15 17:30:00" + " " * (bound - len("2030-01-15 17:30:00"))
    assert len(at_bound) == bound
    assert B._stored_value_problem(at_bound, "TIMESTAMP") is None
    assert B._stored_value_problem(at_bound + " ", "TIMESTAMP") is not None
