"""Pytest conftest for CAS test suite.

Section-05 (B+C plan) adds an autouse fixture that sets
CAS_CONTRACTS_COLLECT=1 so contract-framework tests see every broken
invariant in one go rather than fixing-and-rerunning once per violation.

The env var is scoped via monkeypatch: automatically unset after each test,
so pre-existing tests that don't touch run_all() / _run_contracts are
entirely unaffected.
"""

from __future__ import annotations

import importlib
import os
import shutil
import uuid
from pathlib import Path

import pytest

_CLI_DIR = Path(__file__).resolve().parent.parent / "cli"
_KB_FILES = ("failure_kb.json", "review_kb.json")


@pytest.fixture(scope="session")
def _kb_scratch_dir(tmp_path_factory):
    """Session-scoped scratch copies of the committed knowledge bases."""
    scratch = tmp_path_factory.mktemp("committed_kb")
    for name in _KB_FILES:
        src = _CLI_DIR / name
        if src.exists():
            shutil.copyfile(src, scratch / name)
    return scratch


@pytest.fixture(autouse=True)
def _isolate_committed_kbs(monkeypatch, _kb_scratch_dir):
    """Keep test runs from writing into cli/failure_kb.json and cli/review_kb.json.

    Several code paths under test (deploy_fix, qa_fix, design_fix) record
    outcomes through cli/failure_kb.py, whose local default path is the
    committed seed file. Without this fixture every pytest run appended
    throwaway signatures to that tracked file. The module-level default
    paths are pointed at session-scoped scratch copies instead. Tests that
    patch KB_PATH / REVIEW_KB_PATH themselves, or set FAILURE_KB_PATH /
    REVIEW_KB_PATH, still win: their override is applied after this one.
    """
    for mod_name in ("cli.failure_kb", "failure_kb"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001 - flat import only works when cli/ is on sys.path
            continue
        monkeypatch.setattr(mod, "KB_PATH", _kb_scratch_dir / "failure_kb.json", raising=False)
        monkeypatch.setattr(mod, "REVIEW_KB_PATH", _kb_scratch_dir / "review_kb.json", raising=False)


@pytest.fixture(autouse=True)
def _cas_contracts_collect_env(monkeypatch):
    """Auto-enable aggregated contract-violation reporting during pytest.

    Equivalent to running `export CAS_CONTRACTS_COLLECT=1` for the
    lifetime of each test. Pre-existing tests that never call run_all()
    or _run_contracts see zero behavioral change because the env var only
    matters to those code paths.
    """
    monkeypatch.setenv("CAS_CONTRACTS_COLLECT", "1")


# ---------------------------------------------------------------------------
# Throwaway PostgreSQL database for the few tests that need a real one
# ---------------------------------------------------------------------------

_LOCAL_PG_HOSTS = ("localhost", "127.0.0.1", "::1")


@pytest.fixture(scope="session")
def scratch_pg_url():
    """Connection string for an empty database created for this test session.

    The fixture picks the server like this: ``DATABASE_URL`` when it is set,
    otherwise the PostgreSQL on localhost that the current OS user can reach
    (``postgres`` is only used as the maintenance database to issue CREATE
    DATABASE). It then creates ``cas_test_scratch_<random>`` on that server and
    drops it when the session ends, so no test ever touches an existing
    database or table, and a ``DELETE FROM`` in a test cannot reach real data.

    The test skips (it does not fail) when psycopg2 is missing, when
    ``DATABASE_URL`` points anywhere but this machine, or when no local server
    answers. In CI, ``DATABASE_URL`` names the Postgres service container.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2 import extensions

    url = os.environ.get("DATABASE_URL", "").strip()
    try:
        params = extensions.parse_dsn(url) if url else {"dbname": "postgres", "host": "localhost"}
    except psycopg2.ProgrammingError:
        pytest.skip("DATABASE_URL is not a valid PostgreSQL connection string")
    host = params.get("host", "localhost")
    if host not in _LOCAL_PG_HOSTS and not host.startswith("/"):
        pytest.skip("DATABASE_URL does not point at this machine; refusing to create a scratch database elsewhere")
    params = {**params, "connect_timeout": "3"}

    try:
        admin = psycopg2.connect(extensions.make_dsn(**params))
    except psycopg2.OperationalError as exc:
        pytest.skip(
            "no local PostgreSQL reachable (start one, or set DATABASE_URL to a local server): "
            + type(exc).__name__
        )
    admin.autocommit = True
    name = "cas_test_scratch_" + uuid.uuid4().hex[:12]
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
    except psycopg2.Error as exc:
        admin.close()
        pytest.skip(f"cannot create a scratch database on the local server: {type(exc).__name__}")
    try:
        yield extensions.make_dsn(**{**params, "dbname": name})
    finally:
        try:
            with admin.cursor() as cur:
                try:
                    cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                except psycopg2.Error:
                    cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            admin.close()
