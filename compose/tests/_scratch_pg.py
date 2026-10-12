"""Shared helper: a scratch PostgreSQL database for a test that runs a built
site's SQL.

Not a test module. `scratch_database_url()` yields the URL of a database the
caller may reset (the `public` schema of the database is dropped and created
again by `_site_check.apply_site_sql`), or skips the test when no local
PostgreSQL can be reached.

The server is chosen the way the booted-site tests choose it
(`_postgres_admin` in test_compose_end_to_end.py): `DATABASE_URL` when it
names a server on this machine, otherwise the local one the current OS user
can reach. By default a database of its own is created and dropped. When
`CAS_COMPOSE_TEST_DATABASE` names a database, that one is used and nothing
is created or dropped; its `public` schema is replaced by every test that
uses it, so name one that holds nothing else.
"""

from __future__ import annotations

import contextlib
import os
import uuid

import pytest

from compose.tests.test_compose_end_to_end import (
    FIXED_DATABASE_ENV,
    _node_database_url,
    _postgres_admin,
)


@contextlib.contextmanager
def scratch_database_url(optional: bool = False):
    """The URL (as node-postgres and psycopg2 both read it) of a scratch
    database. See the module docstring. With `optional` the URL is None
    when there is no PostgreSQL to use, where otherwise the test is skipped
    (for a test that has a second way to run)."""
    try:
        admin, params = _postgres_admin()
        import psycopg2
    except pytest.skip.Exception:
        if optional:
            yield None
            return
        raise

    fixed = os.environ.get(FIXED_DATABASE_ENV, "").strip()
    if fixed:
        admin.close()
        yield _node_database_url(params, fixed)
        return
    name = "cas_test_cols_" + uuid.uuid4().hex[:10]
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
    except psycopg2.Error as exc:
        admin.close()
        if optional:
            yield None
            return
        pytest.skip(f"cannot create a scratch database on the local server: {type(exc).__name__}")
    try:
        yield _node_database_url(params, name)
    finally:
        try:
            with admin.cursor() as cur:
                try:
                    cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                except psycopg2.Error:
                    cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            admin.close()
