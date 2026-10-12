"""Bug 1 (schema column drift): schema ↔ JS column
drift must fail the build BEFORE deploy, not leak through to runtime.

A production build emitted db/schema.sql with
`site_settings (setting_key, setting_value)` but server.js ran
`SELECT key, value FROM site_settings`. Code review flagged this as
critical; worker logged it at INFO and proceeded; deploy shipped
broken code; /health 500'd. Post-fix, the _verify_schema_column_references
validator fails the build HERE with a structured list of mismatches so
the normal code-error retry path can regenerate cleanly.
"""

from __future__ import annotations

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


# ---------------------------------------------------------------------------
# _parse_schema_columns: CREATE TABLE parser
# ---------------------------------------------------------------------------


def test_parse_simple_table():
    schema = (
        "CREATE TABLE IF NOT EXISTS users (\n"
        "  id SERIAL PRIMARY KEY,\n"
        "  email VARCHAR(255),\n"
        "  password_hash TEXT\n"
        ");\n"
    )
    out = build._parse_schema_columns(schema)
    assert out == {"users": {"id", "email", "password_hash"}}


def test_parse_multiple_tables():
    schema = (
        "CREATE TABLE users (id SERIAL, email TEXT);\n"
        "CREATE TABLE posts (id SERIAL, body TEXT, user_id INT);\n"
    )
    out = build._parse_schema_columns(schema)
    assert set(out.keys()) == {"users", "posts"}
    assert out["posts"] == {"id", "body", "user_id"}


def test_parse_skips_constraint_lines():
    schema = (
        "CREATE TABLE uploads (\n"
        "  id UUID PRIMARY KEY,\n"
        "  filename VARCHAR(255),\n"
        "  CONSTRAINT uploads_name_unique UNIQUE (filename),\n"
        "  FOREIGN KEY (user_id) REFERENCES users(id)\n"
        ");\n"
    )
    out = build._parse_schema_columns(schema)
    assert out["uploads"] == {"id", "filename"}


def test_parse_handles_setting_key_mismatch_schema():
    """A schema that names the columns setting_key / setting_value."""
    schema = (
        "CREATE TABLE IF NOT EXISTS site_settings (\n"
        "  id SERIAL PRIMARY KEY,\n"
        "  setting_key VARCHAR(100) UNIQUE NOT NULL,\n"
        "  setting_value TEXT,\n"
        "  updated_at TIMESTAMPTZ\n"
        ");\n"
    )
    out = build._parse_schema_columns(schema)
    assert out["site_settings"] == {
        "id", "setting_key", "setting_value", "updated_at",
    }


# ---------------------------------------------------------------------------
# _extract_column_refs_from_sql_string: JS query scanner
# ---------------------------------------------------------------------------


def test_select_from_single_column():
    refs = build._extract_column_refs_from_sql_string(
        "SELECT email FROM users"
    )
    assert ("users", "email") in refs


def test_select_from_multiple_columns():
    refs = build._extract_column_refs_from_sql_string(
        "SELECT id, email, created_at FROM users WHERE id = $1"
    )
    assert ("users", "id") in refs
    assert ("users", "email") in refs
    assert ("users", "created_at") in refs


def test_select_star_skipped():
    refs = build._extract_column_refs_from_sql_string(
        "SELECT * FROM users"
    )
    assert refs == []


def test_select_aggregate_skipped():
    refs = build._extract_column_refs_from_sql_string(
        "SELECT COUNT(*) FROM users"
    )
    assert refs == []


def test_insert_into_columns():
    refs = build._extract_column_refs_from_sql_string(
        "INSERT INTO users (email, password_hash) VALUES ($1, $2)"
    )
    assert ("users", "email") in refs
    assert ("users", "password_hash") in refs


def test_update_set_columns():
    refs = build._extract_column_refs_from_sql_string(
        "UPDATE users SET email = $1, updated_at = NOW() WHERE id = $2"
    )
    assert ("users", "email") in refs
    assert ("users", "updated_at") in refs


# ---------------------------------------------------------------------------
# _verify_schema_column_references: end-to-end processor
# ---------------------------------------------------------------------------


def _mock_spec():
    return {"app_name": "x"}


def test_no_drift_does_nothing():
    schema = "CREATE TABLE users (id SERIAL, email TEXT);"
    server = "pool.query('SELECT email FROM users');"
    files = [("db/schema.sql", schema), ("server.js", server)]
    out = build._verify_schema_column_references(files, _mock_spec())
    # No mismatches: returns files unchanged.
    assert out == files


def test_setting_key_drift_raises():
    """The setting_key mismatch: schema has setting_key but
    server.js queries key. Validator must raise
    SchemaColumnDriftError with both file + table + column named."""
    schema = (
        "CREATE TABLE site_settings (\n"
        "  setting_key VARCHAR(100) UNIQUE NOT NULL,\n"
        "  setting_value TEXT\n"
        ");\n"
    )
    server = (
        "const result = await pool.query('SELECT key, value FROM site_settings');\n"
    )
    files = [("db/schema.sql", schema), ("server.js", server)]
    with pytest.raises(build.SchemaColumnDriftError) as excinfo:
        build._verify_schema_column_references(files, _mock_spec())
    err = excinfo.value
    # Two mismatches (key + value) on site_settings.
    assert len(err.mismatches) == 2
    mismatch_cols = {m["column"] for m in err.mismatches}
    assert mismatch_cols == {"key", "value"}
    assert all(m["table"] == "site_settings" for m in err.mismatches)
    assert all(m["file"] == "server.js" for m in err.mismatches)


def test_unknown_table_is_silently_skipped():
    """References to tables not in schema are usually CTEs / subqueries
    / aliases, too complex for this pass. Skip without error."""
    schema = "CREATE TABLE users (id SERIAL);"
    server = "pool.query('SELECT email FROM temp_projection');"
    files = [("db/schema.sql", schema), ("server.js", server)]
    out = build._verify_schema_column_references(files, _mock_spec())
    assert out == files


def test_rollback_env_var_demotes_to_warning(monkeypatch, capsys):
    """BLOCK_ON_SCHEMA_DRIFT=false should warn, not raise."""
    monkeypatch.setenv("BLOCK_ON_SCHEMA_DRIFT", "false")
    schema = "CREATE TABLE users (id SERIAL);"
    server = "pool.query('SELECT email FROM users');"
    files = [("db/schema.sql", schema), ("server.js", server)]
    out = build._verify_schema_column_references(files, _mock_spec())
    captured = capsys.readouterr()
    assert "WARNING schema-column drift" in captured.out
    assert "BLOCK_ON_SCHEMA_DRIFT=false" in captured.out
    # Still returns files unchanged (no mutation).
    assert out == files


def test_default_env_unset_is_blocking(monkeypatch):
    """When the env var is unset, block (match the user's explicit
    spec: default True)."""
    monkeypatch.delenv("BLOCK_ON_SCHEMA_DRIFT", raising=False)
    schema = "CREATE TABLE users (id SERIAL);"
    server = "pool.query('SELECT email FROM users');"
    files = [("db/schema.sql", schema), ("server.js", server)]
    with pytest.raises(build.SchemaColumnDriftError):
        build._verify_schema_column_references(files, _mock_spec())


def test_ejs_file_with_drift_flagged():
    """Column references inside EJS files (e.g. embedded in server-
    side blocks) must also be checked."""
    schema = "CREATE TABLE posts (id SERIAL, title TEXT);"
    ejs = (
        "<% pool.query('SELECT id, headline FROM posts').then(...) %>\n"
    )
    files = [("db/schema.sql", schema), ("views/list.ejs", ejs)]
    with pytest.raises(build.SchemaColumnDriftError) as excinfo:
        build._verify_schema_column_references(files, _mock_spec())
    cols = {m["column"] for m in excinfo.value.mismatches}
    assert "headline" in cols


def test_error_message_names_file_table_column():
    """SchemaColumnDriftError __str__ must name enough detail for a
    human to locate the drift without digging."""
    err = build.SchemaColumnDriftError([
        {"file": "server.js", "line": 42, "table": "site_settings",
         "column": "key", "schema_cols": {"setting_key", "setting_value"}},
    ])
    msg = str(err)
    assert "server.js" in msg
    assert "site_settings" in msg
    assert "key" in msg
    assert "schema-column drift" in msg


# ---------------------------------------------------------------------------
# Registration + ordering
# ---------------------------------------------------------------------------


def test_validator_registered_for_build_phase_only():
    import pipeline
    assert "_verify_schema_column_references" in pipeline.REGISTRY
    proc = pipeline.REGISTRY["_verify_schema_column_references"]
    # Must NOT run in reapply: that would fail a previously-shipped
    # build and block the self-healing loop from fixing it.
    assert "build" in proc.phase
    assert "reapply" not in proc.phase


def test_validator_runs_before_contracts():
    """Schema validation happens BEFORE contracts so a drift raises
    with its structured mismatch list (more actionable than a
    contract violation)."""
    import pipeline
    order = pipeline.pipeline_order("build")
    assert order.index("_verify_schema_column_references") < order.index(
        "_run_contracts"
    )


def test_validator_in_expected_processors():
    import pipeline
    assert "_verify_schema_column_references" in pipeline.EXPECTED_PROCESSORS


# ---------------------------------------------------------------------------
# Post-review B1: JOIN / multi-table SELECTs must NOT false-positive
# ---------------------------------------------------------------------------


def test_join_query_is_skipped_not_flagged():
    """Review finding B1: `SELECT p.id, u.email FROM posts p JOIN users u`
    was attributing `email` to `posts` and raising. Validator must
    detect JOIN and skip the statement entirely."""
    refs = build._extract_column_refs_from_sql_string(
        "SELECT p.id, p.title, u.email FROM posts p JOIN users u ON u.id = p.user_id"
    )
    # Skipped entirely: no (table, col) pairs emitted, no raise.
    assert refs == []


def test_left_join_also_skipped():
    refs = build._extract_column_refs_from_sql_string(
        "SELECT a.x, b.y FROM tbl_a a LEFT JOIN tbl_b b ON a.id = b.a_id"
    )
    assert refs == []


def test_natural_join_skipped():
    refs = build._extract_column_refs_from_sql_string(
        "SELECT col1 FROM t1 NATURAL JOIN t2"
    )
    assert refs == []


def test_comma_from_skipped():
    """`SELECT x FROM a, b WHERE a.id = b.a_id` is a multi-table shape
    too, skip."""
    refs = build._extract_column_refs_from_sql_string(
        "SELECT x FROM a, b WHERE a.id = b.a_id"
    )
    assert refs == []


def test_table_prefixed_column_is_ignored():
    """`p.title` cannot be attributed to a specific real table without
    a JOIN-aware parser. Returning no ref for that token prevents false
    positives on perfectly legitimate queries."""
    refs = build._extract_column_refs_from_sql_string(
        "SELECT p.title FROM posts p"
    )
    # `p.title` skipped (alias prefix); no pairs for this single-table
    # query either since no plain columns appear.
    assert refs == []


def test_legitimate_single_table_query_still_flagged():
    """Counter-test: a single-table query with a real drift still
    raises. Our B1 fix should not make the validator too permissive."""
    schema = "CREATE TABLE users (id SERIAL, email TEXT);"
    server = "pool.query('SELECT email, bad_col FROM users');"
    files = [("db/schema.sql", schema), ("server.js", server)]
    with pytest.raises(build.SchemaColumnDriftError) as excinfo:
        build._verify_schema_column_references(files, _mock_spec())
    cols = {m["column"] for m in excinfo.value.mismatches}
    assert cols == {"bad_col"}


def test_join_query_end_to_end_does_not_raise():
    """End-to-end integration: a generated server.js with a JOIN
    passes the validator without raising, matches real-world admin
    list view shape."""
    schema = (
        "CREATE TABLE posts (id SERIAL, title TEXT, user_id INT);\n"
        "CREATE TABLE users (id SERIAL, email TEXT);\n"
    )
    server = (
        "pool.query('SELECT p.title, u.email FROM posts p "
        "JOIN users u ON u.id = p.user_id');"
    )
    files = [("db/schema.sql", schema), ("server.js", server)]
    out = build._verify_schema_column_references(files, _mock_spec())
    assert out == files  # no raise, no mutation


# ---------------------------------------------------------------------------
# Post-review B2: schema `-- line comments` must not confuse column parser
# ---------------------------------------------------------------------------


def test_inline_comment_between_columns_not_dropped():
    """Review finding B2: `-- note` inside a CREATE TABLE body was
    causing the next column to be lost, producing false drift errors."""
    schema = (
        "CREATE TABLE users (\n"
        "  id SERIAL PRIMARY KEY,\n"
        "  -- created so we can track audit\n"
        "  email TEXT,\n"
        "  -- password hash stored via bcrypt\n"
        "  password_hash TEXT\n"
        ");\n"
    )
    out = build._parse_schema_columns(schema)
    assert out == {"users": {"id", "email", "password_hash"}}


def test_trailing_inline_comment_after_column_def():
    """Inline `-- comment` trailing a column on the same line: also
    stripped before comma-splitting."""
    schema = (
        "CREATE TABLE items (\n"
        "  id SERIAL,      -- auto-increment\n"
        "  name TEXT,      -- short label\n"
        "  body TEXT\n"
        ");\n"
    )
    out = build._parse_schema_columns(schema)
    assert out == {"items": {"id", "name", "body"}}


def test_schema_with_comment_drift_scenario_end_to_end():
    """End-to-end: schema with comments + server.js that SELECTs
    all the real columns should NOT raise (this used to raise
    because email was lost to the comment-eating parser)."""
    schema = (
        "CREATE TABLE users (\n"
        "  id SERIAL PRIMARY KEY,\n"
        "  -- inline explanatory comment\n"
        "  email TEXT,\n"
        "  password_hash TEXT\n"
        ");\n"
    )
    server = "pool.query('SELECT id, email, password_hash FROM users');"
    files = [("db/schema.sql", schema), ("server.js", server)]
    out = build._verify_schema_column_references(files, _mock_spec())
    assert out == files  # no raise


# ---------------------------------------------------------------------------
# N2: Env-gate allowlist pattern (false/0/no/off/disabled = rollback)
# ---------------------------------------------------------------------------


def test_gate_default_unset_is_strict(monkeypatch):
    monkeypatch.delenv("BLOCK_ON_SCHEMA_DRIFT", raising=False)
    schema = "CREATE TABLE users (id SERIAL);"
    server = "pool.query('SELECT email FROM users');"
    files = [("db/schema.sql", schema), ("server.js", server)]
    with pytest.raises(build.SchemaColumnDriftError):
        build._verify_schema_column_references(files, _mock_spec())


def test_gate_typo_true_is_strict(monkeypatch):
    """`True` (capital T) is NOT in the rollback allowlist, stays
    strict. Previously this would have resolved to `"true".lower() ==
    "true"` → True → fire → fine; now with allowlist, `"true" not in
    rollback_tokens` → stays strict. Behavior: same in this case."""
    monkeypatch.setenv("BLOCK_ON_SCHEMA_DRIFT", "True")
    schema = "CREATE TABLE users (id SERIAL);"
    server = "pool.query('SELECT email FROM users');"
    files = [("db/schema.sql", schema), ("server.js", server)]
    with pytest.raises(build.SchemaColumnDriftError):
        build._verify_schema_column_references(files, _mock_spec())


def test_gate_all_rollback_tokens_demote(monkeypatch, capsys):
    """Every token in the allowlist demotes the gate, case-insensitive
    + whitespace-tolerant."""
    for token in ("false", "0", "no", "off", "disabled", "FALSE", "  no  "):
        monkeypatch.setenv("BLOCK_ON_SCHEMA_DRIFT", token)
        schema = "CREATE TABLE users (id SERIAL);"
        server = "pool.query('SELECT email FROM users');"
        files = [("db/schema.sql", schema), ("server.js", server)]
        out = build._verify_schema_column_references(files, _mock_spec())
        assert out == files, f"rollback token {token!r} should demote"


def test_gate_empty_string_is_strict(monkeypatch):
    """Empty env var → unset-equivalent → strict."""
    monkeypatch.setenv("BLOCK_ON_SCHEMA_DRIFT", "")
    schema = "CREATE TABLE users (id SERIAL);"
    server = "pool.query('SELECT email FROM users');"
    files = [("db/schema.sql", schema), ("server.js", server)]
    with pytest.raises(build.SchemaColumnDriftError):
        build._verify_schema_column_references(files, _mock_spec())


# ---------------------------------------------------------------------------
# Parser stress cases
# ---------------------------------------------------------------------------


def test_parse_canonical_site_settings_with_empty_string_default():
    """Parser regression: `value TEXT DEFAULT ''` made the
    old backward-lookup escape-pair detection think we're still in a
    string literal and swallowed the comma, dropping `updated_at`."""
    schema = (
        "CREATE TABLE IF NOT EXISTS site_settings (\n"
        "  key VARCHAR(100) PRIMARY KEY,\n"
        "  value TEXT DEFAULT '',\n"
        "  updated_at TIMESTAMP DEFAULT NOW()\n"
        ");\n"
    )
    out = build._parse_schema_columns(schema)
    # All three columns present.
    assert out == {"site_settings": {"key", "value", "updated_at"}}


def test_parse_check_constraint_with_quoted_values_doesnt_confuse_parser():
    """CHECK (role IN ('admin', 'user')) has commas INSIDE both a
    string and a paren group, neither should split a column def."""
    schema = (
        "CREATE TABLE users (\n"
        "  id SERIAL,\n"
        "  role VARCHAR(20) NOT NULL CHECK (role IN ('admin', 'user')),\n"
        "  email TEXT\n"
        ");\n"
    )
    out = build._parse_schema_columns(schema)
    assert out["users"] == {"id", "role", "email"}


def test_parse_default_with_cast_syntax():
    """Postgres-specific `DEFAULT '{}'::jsonb`, the `::jsonb` cast
    syntax follows the closing quote; parser must see the quotes as
    balanced and pick up the next column after the comma."""
    schema = (
        "CREATE TABLE audit (\n"
        "  id SERIAL,\n"
        "  data JSONB DEFAULT '{}'::jsonb,\n"
        "  metadata JSONB DEFAULT '{}'::jsonb\n"
        ");\n"
    )
    out = build._parse_schema_columns(schema)
    assert out["audit"] == {"id", "data", "metadata"}


def test_parse_mixed_defaults():
    """A common shape: primary key, nullable text, int with 0 default,
    text array with empty default, trailing text. Previously the empty
    string default was the break point."""
    schema = (
        "CREATE TABLE posts (\n"
        "  id SERIAL,\n"
        "  title TEXT DEFAULT NULL,\n"
        "  view_count INT DEFAULT 0,\n"
        "  tags TEXT[] DEFAULT '{}',\n"
        "  body TEXT\n"
        ");\n"
    )
    out = build._parse_schema_columns(schema)
    assert out["posts"] == {"id", "title", "view_count", "tags", "body"}


def test_parse_timestamptz_with_now_default():
    """TIMESTAMPTZ DEFAULT NOW(), parens + column name follow a
    typical timestamp default."""
    schema = (
        "CREATE TABLE events (\n"
        "  id SERIAL,\n"
        "  event_date TIMESTAMPTZ DEFAULT NOW(),\n"
        "  created_at TIMESTAMPTZ DEFAULT NOW(),\n"
        "  is_featured BOOLEAN DEFAULT FALSE\n"
        ");\n"
    )
    out = build._parse_schema_columns(schema)
    assert out["events"] == {"id", "event_date", "created_at", "is_featured"}


def test_end_to_end_canonical_site_settings_after_normalization():
    """Integration: the post-normalize shape + canonical routes_settings
    template query pass the validator cleanly. This is the path every
    non-MW customer build takes."""
    # Canonical (post _ensure_site_settings normalization).
    schema = (
        "CREATE TABLE IF NOT EXISTS site_settings (\n"
        "  key VARCHAR(100) PRIMARY KEY,\n"
        "  value TEXT DEFAULT '',\n"
        "  updated_at TIMESTAMP DEFAULT NOW()\n"
        ");\n"
    )
    # The shipped routes_settings.js template queries all three columns.
    settings_route = (
        "const result = await pool.query('SELECT key, value FROM site_settings');\n"
        "await pool.query("
        "'INSERT INTO site_settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()', [k, v]);\n"
    )
    files = [
        ("db/schema.sql", schema),
        ("routes/settings.js", settings_route),
    ]
    # No raise: canonical shape validates cleanly.
    out = build._verify_schema_column_references(files, _mock_spec())
    assert out == files
