"""Tests for _normalize_password_column_to_canonical (drift audit
2026-04-20).

Motivation: cli/templates/routes_admin.js queries `SELECT password FROM
users` + `UPDATE users SET password = $1`. The gold-standard MW schema
uses `password VARCHAR(255)`. Claude occasionally emits the alternative
`password_hash` column name. Without normalization, the schema↔JS drift
validator would correctly flag the mismatch and fail the build.
Normalization keeps the canonical template working regardless of
Claude's choice.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


def test_password_hash_in_schema_gets_renamed():
    schema = (
        "CREATE TABLE users (\n"
        "  id SERIAL PRIMARY KEY,\n"
        "  email VARCHAR(255),\n"
        "  password_hash TEXT,\n"
        "  created_at TIMESTAMPTZ DEFAULT NOW()\n"
        ");\n"
    )
    out = dict(build._normalize_password_column_to_canonical(
        [("db/schema.sql", schema)]
    ))
    got = out["db/schema.sql"]
    assert "password_hash" not in got
    # Canonical `password` token now in the users CREATE TABLE.
    assert "password TEXT" in got or "password VARCHAR" in got or " password " in got


def test_canonical_password_schema_unchanged():
    """If Claude already uses `password`, no-op."""
    schema = (
        "CREATE TABLE users (\n"
        "  id SERIAL,\n"
        "  email VARCHAR(255),\n"
        "  password VARCHAR(255),\n"
        "  password_set BOOLEAN DEFAULT FALSE\n"
        ");\n"
    )
    out = build._normalize_password_column_to_canonical(
        [("db/schema.sql", schema)]
    )
    # list identity unchanged.
    assert out == [("db/schema.sql", schema)]


def test_seed_sql_insert_columns_get_rewritten():
    schema = (
        "CREATE TABLE users (\n"
        "  email VARCHAR(255),\n"
        "  password_hash TEXT\n"
        ");\n"
    )
    seed = (
        "INSERT INTO users (email, password_hash) VALUES ('a@b.c', 'hash123') "
        "ON CONFLICT DO NOTHING;\n"
    )
    out = dict(build._normalize_password_column_to_canonical(
        [("db/schema.sql", schema), ("db/seed.sql", seed)]
    ))
    got_seed = out["db/seed.sql"]
    assert "password_hash" not in got_seed
    assert "INSERT INTO users (email, password)" in got_seed


def test_route_js_password_hash_references_get_rewritten():
    schema = (
        "CREATE TABLE users (\n"
        "  id SERIAL,\n"
        "  email VARCHAR(255),\n"
        "  password_hash TEXT\n"
        ");\n"
    )
    auth_js = (
        "const user = await pool.query("
        "'SELECT password_hash FROM users WHERE id = $1', [id]);\n"
        "if (await bcrypt.compare(pw, user.rows[0].password_hash)) { ok(); }\n"
    )
    out = dict(build._normalize_password_column_to_canonical(
        [("db/schema.sql", schema), ("routes/auth.js", auth_js)]
    ))
    got = out["routes/auth.js"]
    assert "password_hash" not in got
    # Both the SQL reference AND the JS object-access reference are
    # rewritten: bcrypt.compare still resolves.
    assert "SELECT password FROM users" in got
    assert "user.rows[0].password" in got


def test_non_users_table_password_hash_preserved():
    """If the password_hash column lives on a DIFFERENT table (e.g. a
    backup / audit table), leave it alone. Scope is users-only."""
    schema = (
        "CREATE TABLE users (\n"
        "  id SERIAL,\n"
        "  email VARCHAR(255),\n"
        "  password VARCHAR(255)\n"
        ");\n"
        "CREATE TABLE audit_users (\n"
        "  id SERIAL,\n"
        "  email TEXT,\n"
        "  password_hash TEXT\n"
        ");\n"
    )
    out = build._normalize_password_column_to_canonical(
        [("db/schema.sql", schema)]
    )
    # Users table has no password_hash → early return; audit_users kept.
    got = dict(out)["db/schema.sql"]
    assert "password_hash" in got  # audit_users preserved


def test_idempotent_on_second_pass():
    schema = (
        "CREATE TABLE users (\n"
        "  id SERIAL,\n"
        "  password_hash TEXT\n"
        ");\n"
    )
    once = dict(build._normalize_password_column_to_canonical(
        [("db/schema.sql", schema)]
    ))
    twice = dict(build._normalize_password_column_to_canonical(
        list(once.items())
    ))
    assert once == twice


def test_end_to_end_validator_passes_after_normalization():
    """Full compounding check: Claude emits password_hash → normalizer
    rewrites to password → canonical routes_admin.js template queries
    password → schema validator sees matching columns → no raise."""
    schema = (
        "CREATE TABLE users (\n"
        "  id SERIAL PRIMARY KEY,\n"
        "  email VARCHAR(255),\n"
        "  password_hash TEXT\n"
        ");\n"
    )
    admin_route = (
        "const user = await pool.query("
        "'SELECT password FROM users WHERE id = $1', [req.session.userId]);\n"
        "await pool.query('UPDATE users SET password = $1 WHERE id = $2', "
        "[hashed, req.session.userId]);\n"
    )
    files = [
        ("db/schema.sql", schema),
        ("routes/admin.js", admin_route),
    ]
    # Step 1: normalize
    normalized = dict(build._normalize_password_column_to_canonical(files))
    # Step 2: validate, should not raise
    out = build._verify_schema_column_references(
        list(normalized.items()), {"app_name": "x"}
    )
    assert out == list(normalized.items())


def test_registered_in_pipeline():
    import pipeline
    assert "_normalize_password_column_to_canonical" in pipeline.REGISTRY
    assert "_normalize_password_column_to_canonical" in pipeline.EXPECTED_PROCESSORS


def test_runs_before_schema_validator():
    """Ordering: normalization must run BEFORE the validator, otherwise
    the validator raises on the non-canonical state."""
    import pipeline
    order = pipeline.pipeline_order("build")
    norm_pos = order.index("_normalize_password_column_to_canonical")
    val_pos = order.index("_verify_schema_column_references")
    assert norm_pos < val_pos
