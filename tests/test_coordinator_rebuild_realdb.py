"""Automatic rebuilds against a real PostgreSQL holding the real schema.

Actuators.queue_rebuild is the one place the coordinator starts a build for a
customer. What bounds it lives in three components, and these tests run all
three on the same rows:

  * the web app's build-start limits (web/src/services/build-start-limits.ts).
    The rebuild is refused at the same numbers, takes the same per-customer
    advisory lock, and is written to the same ledger (build_start_events).
    The web app's own budget statement, read out of that file, is executed
    here to show that it counts what the coordinator wrote;
  * the worker's rule for failures that repeat (ops.scanner
    .is_deterministic_failure, which queue/worker_pg.py uses for its retry
    decision): such a build is not rebuilt, and work detection files no task
    for it;
  * the worker's janitor (queue/worker_pg.py _cleanup_old_builds), which
    deletes old failed rows of an app. The lineage bound is counted from the
    ledger, so the real janitor is run in the middle of a test to show that
    deleting rows does not hand the lineage its budget back.

The tests skip without a local PostgreSQL (see scratch_pg_url in
tests/conftest.py). Nothing leaves the machine.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _extra in (str(ROOT), str(ROOT / "queue"), str(ROOT / "cli")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

import worker_pg

from coordinator import actuators as actuators_module
from coordinator import work_detection
from coordinator.actuators import Actuators
from coordinator.state import WorldState

SCHEMA_SQL = ROOT / "web" / "src" / "db" / "schema.sql"
LIMITS_TS = ROOT / "web" / "src" / "services" / "build-start-limits.ts"
SPEC = {"app_name": "sample-app", "pages": [{"path": "/", "title": "Home"}]}


@pytest.fixture(scope="module")
def rebuild_db_url(scratch_pg_url):
    """A throwaway database holding the production schema."""
    import psycopg2
    from psycopg2 import extensions

    params = extensions.parse_dsn(scratch_pg_url)
    name = "cas_test_rebuild_" + uuid.uuid4().hex[:10]
    admin = psycopg2.connect(scratch_pg_url)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    url = extensions.make_dsn(**{**params, "dbname": name})
    try:
        loader = psycopg2.connect(url)
        loader.autocommit = True
        with loader.cursor() as cur:
            cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        loader.close()
        yield url
    finally:
        with admin.cursor() as cur:
            try:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except psycopg2.Error:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


class _Db:
    def __init__(self, url):
        self.url = url

    def run(self, sql, params=None, fetch=True):
        import psycopg2
        conn = psycopg2.connect(self.url)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() if fetch and cur.description else []
            conn.commit()
            return rows
        finally:
            conn.close()

    def customer(self) -> str:
        return str(self.run(
            "INSERT INTO customers (email, password, name) VALUES (%s, 'x', 'Sample Person') RETURNING id",
            (f"sample-{uuid.uuid4().hex[:10]}@example.test",))[0][0])

    def build(self, customer, status="failed", error="npm install timed out", parent=None,
              app_name="sample-app", age_minutes=0) -> str:
        return str(self.run(
            "INSERT INTO builds (customer_id, tier, status, app_name, spec, error_message, parent_build_id, "
            "created_at, updated_at) VALUES (%s, 'build', %s, %s, %s, %s, %s, "
            "NOW() - make_interval(mins => %s), NOW() - make_interval(mins => %s)) RETURNING id",
            (customer, status, app_name, json.dumps(SPEC), error, parent, age_minutes, age_minutes))[0][0])

    def fail(self, build_id, error="npm install timed out"):
        self.run("UPDATE builds SET status = 'failed', error_message = %s WHERE id = %s",
                 (error, build_id), fetch=False)

    def row(self, build_id):
        rows = self.run("SELECT status, parent_build_id, customer_id, spec FROM builds WHERE id = %s", (build_id,))
        return rows[0] if rows else None

    def ledger(self, customer):
        return self.run("SELECT build_id::text, kind FROM build_start_events WHERE customer_id = %s "
                        "ORDER BY id", (customer,))

    def children(self, parent):
        return [str(r[0]) for r in self.run(
            "SELECT id FROM builds WHERE parent_build_id = %s ORDER BY created_at", (parent,))]


@pytest.fixture()
def db(rebuild_db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", rebuild_db_url)
    monkeypatch.delenv("COORDINATOR_ANALYSIS_ONLY", raising=False)
    database = _Db(rebuild_db_url)
    database.run("TRUNCATE build_start_events, builds, customers CASCADE", fetch=False)
    return database


def _rebuild(parent, request="Automated retry after build failure"):
    act = Actuators("backend_engineer", "task-rebuild", dry_run=False)
    new_id = act.queue_rebuild(parent, request)
    return new_id, act.actions[-1].details if act.actions else {}


# ---------------------------------------------------------------------------
# The web app's limits, lock and ledger
# ---------------------------------------------------------------------------

def _web_constant(name: str) -> int:
    match = re.search(rf"export const {name} = (\d+);", LIMITS_TS.read_text(encoding="utf-8"))
    assert match, f"{name} not found in {LIMITS_TS.relative_to(ROOT)}"
    return int(match.group(1))


def _web_budget_statement() -> str:
    """The SELECT inside readBuildStartBudget, with $1/$2 turned into named parameters."""
    source = LIMITS_TS.read_text(encoding="utf-8")
    body = source[source.index("async function readBuildStartBudget"):]
    sql = body[body.index("`") + 1:]
    sql = sql[:sql.index("`")]
    assert "build_start_events" in sql and "$1" in sql and "$2" in sql
    return sql.replace("$1", "%(customer)s").replace("$2", "%(build)s")


def test_the_python_limits_are_the_web_apps():
    assert actuators_module.CUSTOMER_MAX_ACTIVE_BUILDS == _web_constant("MAX_ACTIVE_BUILDS_PER_CUSTOMER")
    assert actuators_module.CUSTOMER_MAX_BUILD_STARTS_PER_DAY == _web_constant("MAX_BUILD_STARTS_PER_DAY")
    source = LIMITS_TS.read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock(hashtext('cas-build-start'), hashtext($1))" in source
    assert "pg_advisory_xact_lock(hashtext('cas-build-start'), hashtext(%s))" in \
        actuators_module._CUSTOMER_BUILD_LOCK_SQL


def test_an_accepted_rebuild_is_a_build_start_the_web_apps_budget_statement_counts(db):
    customer = db.customer()
    parent = db.build(customer)
    new_id, details = _rebuild(parent)
    assert new_id and details["new_build_id"] == new_id
    status, parent_id, owner, spec = db.row(new_id)
    assert (status, str(parent_id), str(owner)) == ("queued", parent, customer)
    assert spec["rebuild_request"] == "Automated retry after build failure"
    assert db.ledger(customer) == [(parent, "rebuild")], "one ledger row, filed under the lineage root"
    active, starts_today, retries = db.run(_web_budget_statement(), {"customer": customer, "build": parent})[0]
    assert (active, starts_today, retries) == (1, 1, 0)


def test_a_rebuild_is_refused_at_the_customers_daily_start_limit(db):
    customer = db.customer()
    parent = db.build(customer)
    limit = actuators_module.CUSTOMER_MAX_BUILD_STARTS_PER_DAY
    for _ in range(limit):
        db.run("INSERT INTO build_start_events (customer_id, build_id, kind) VALUES (%s, %s, 'revision')",
               (customer, str(uuid.uuid4())), fetch=False)
    new_id, details = _rebuild(parent)
    assert new_id is None and details["refused"] == "customer_build_start_limit"
    assert db.children(parent) == [] and len(db.ledger(customer)) == limit
    # One start fewer and it goes through: the limit is the web app's number, not a lower one.
    db.run("DELETE FROM build_start_events WHERE id = (SELECT MIN(id) FROM build_start_events)", fetch=False)
    new_id, _details = _rebuild(parent)
    assert new_id is not None


def test_a_rebuild_is_refused_while_the_customer_has_two_builds_in_progress(db):
    customer = db.customer()
    parent = db.build(customer)
    db.build(customer, status="building", app_name="other-app-one")
    db.build(customer, status="queued", app_name="other-app-two")
    new_id, details = _rebuild(parent)
    assert new_id is None and details["refused"] == "customer_build_start_limit"
    assert db.ledger(customer) == []


def test_a_rebuild_waits_for_the_web_apps_per_customer_lock(db):
    """The web app holds pg_advisory_xact_lock(hashtext('cas-build-start'),
    hashtext(customer id)) while it checks the budget and inserts a build. The
    coordinator takes the same lock, so the two cannot both read "one slot
    left"."""
    import psycopg2
    customer = db.customer()
    parent = db.build(customer)
    holder = psycopg2.connect(db.url)
    with holder.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('cas-build-start'), hashtext(%s))", (customer,))
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.update(new_id=_rebuild(parent)[0]), daemon=True)
    thread.start()
    try:
        thread.join(1.0)
        assert thread.is_alive(), "the rebuild did not wait for the customer's build-start lock"
        assert db.children(parent) == []
    finally:
        holder.rollback()
        holder.close()
    thread.join(15)
    assert not thread.is_alive() and outcome["new_id"] is not None


def test_a_build_without_a_customer_is_not_rebuilt(db):
    parent = db.build(None)
    new_id, details = _rebuild(parent)
    assert new_id is None and details["refused"] == "no_customer"


# ---------------------------------------------------------------------------
# Deterministic failures: the worker's rule
# ---------------------------------------------------------------------------

DETERMINISTIC_ERRORS = [
    "[v2-deterministic] v2 app deployed but crashes on startup. deploy_fix skipped",
    "Spec validation failed with 1 error(s): page '/clients' has no title",
]


@pytest.mark.parametrize("error", DETERMINISTIC_ERRORS)
def test_a_deterministic_failure_is_never_rebuilt(db, error):
    assert worker_pg._is_deterministic_failure(error), "the worker's retry decision uses the same rule"
    customer = db.customer()
    parent = db.build(customer, error=error)
    new_id, details = _rebuild(parent)
    assert new_id is None and details["refused"] == "deterministic_failure"
    assert db.children(parent) == [] and db.ledger(customer) == []


def test_a_build_that_is_not_failed_is_judged_by_its_status_not_by_old_error_text(db):
    """A preview keeps the error text of an earlier attempt in error_message
    only if something left it there; a rebuild of a build that is not failed
    (the recurring-QA handler rebuilds previews) is not refused for it."""
    customer = db.customer()
    parent = db.build(customer, status="preview", error=DETERMINISTIC_ERRORS[0])
    new_id, _details = _rebuild(parent)
    assert new_id is not None


def test_work_detection_files_no_task_for_a_deterministic_failure():
    state = WorldState(failed_builds_24h=[
        {"id": "b-1", "app_name": "one", "error_message": DETERMINISTIC_ERRORS[0], "error_category": "infra"},
        {"id": "b-2", "app_name": "two", "error_message": DETERMINISTIC_ERRORS[1], "error_category": "code_error"},
        {"id": "b-3", "app_name": "three", "error_message": "npm install timed out", "error_category": "infra"},
    ])
    items = work_detection._check_failed_builds(MagicMock(), state, None, None)
    assert [item.context["build_ids"] for item in items] == [["b-3"]]


# ---------------------------------------------------------------------------
# The lineage bound
# ---------------------------------------------------------------------------

def test_a_failed_rebuild_does_not_start_the_count_again(db, monkeypatch):
    """The per-parent cap counts the children of one build, and a rebuild that
    fails is a new parent with no children. Without the lineage bound the
    chain below never ends."""
    monkeypatch.setattr(actuators_module, "MAX_REBUILDS_PER_LINEAGE", 3)
    customer = db.customer()
    current = root = db.build(customer, age_minutes=60)
    chain = []
    for _generation in range(3):
        new_id, details = _rebuild(current)
        assert new_id, details
        db.fail(new_id)
        chain.append(new_id)
        current = new_id
    new_id, details = _rebuild(current)
    assert new_id is None and details["refused"] == "lineage_cap"
    assert db.children(current) == []
    assert db.ledger(customer) == [(root, "rebuild")] * 3
    # Any build of the lineage is refused, not only the newest one.
    assert _rebuild(chain[0])[1]["refused"] == "lineage_cap"


def test_another_lineage_of_the_same_customer_has_its_own_count(db, monkeypatch):
    monkeypatch.setattr(actuators_module, "MAX_REBUILDS_PER_LINEAGE", 1)
    customer = db.customer()
    first = db.build(customer, app_name="first-app")
    second = db.build(customer, app_name="second-app")
    first_child, _ = _rebuild(first)
    db.fail(first_child)
    assert _rebuild(first_child)[1]["refused"] == "lineage_cap"
    second_child, details = _rebuild(second)
    assert second_child, details


def test_the_count_is_over_a_rolling_week(db, monkeypatch):
    monkeypatch.setattr(actuators_module, "MAX_REBUILDS_PER_LINEAGE", 1)
    customer = db.customer()
    root = db.build(customer)
    child, _ = _rebuild(root)
    db.fail(child)
    assert _rebuild(child)[1]["refused"] == "lineage_cap"
    db.run("UPDATE build_start_events SET created_at = NOW() - INTERVAL '8 days'", fetch=False)
    again, details = _rebuild(child)
    assert again, details


def test_the_janitor_cannot_hand_a_lineage_its_budget_back(db, monkeypatch):
    """The worker's janitor deletes old failed builds of an app. A bound that
    counted build rows would shrink with every delete. The real janitor runs
    here, deletes a failed rebuild of the lineage, and the lineage is still at
    its cap."""
    import psycopg2
    monkeypatch.setattr(actuators_module, "MAX_REBUILDS_PER_LINEAGE", 3)
    monkeypatch.setattr(worker_pg, "_BUILDS_ROOT", Path("/nonexistent-builds-root-for-tests"))
    customer = db.customer()
    root = db.build(customer, age_minutes=240)
    first, _ = _rebuild(root)
    db.fail(first)
    second, _ = _rebuild(root)
    db.fail(second)
    third, details = _rebuild(second)
    assert third, details
    db.fail(third)
    # Spread the four rows out in time, oldest first, as the janitor ranks by created_at.
    for age, build_id in ((240, root), (180, first), (120, second), (60, third)):
        db.run("UPDATE builds SET created_at = NOW() - make_interval(mins => %s) WHERE id = %s",
               (age, build_id), fetch=False)
    assert _rebuild(third)[1]["refused"] == "lineage_cap"

    conn = psycopg2.connect(db.url)
    try:
        deleted = worker_pg._cleanup_old_builds(conn, third, "sample-app")
    finally:
        conn.close()
    assert deleted == 1 and db.row(first) is None, "the janitor was expected to delete the old failed leaf"
    surviving_rebuilds = db.run(
        "SELECT COUNT(*) FROM builds WHERE parent_build_id IS NOT NULL AND customer_id = %s", (customer,))[0][0]
    assert surviving_rebuilds == 2, "two rebuild rows are left, fewer than the cap of three"

    new_id, details = _rebuild(third)
    assert new_id is None and details["refused"] == "lineage_cap"
    assert db.ledger(customer) == [(root, "rebuild")] * 3, "the ledger kept all three starts"


def test_dry_run_writes_nothing(db):
    customer = db.customer()
    parent = db.build(customer)
    act = Actuators("backend_engineer", "task-dry", dry_run=True)
    assert act.queue_rebuild(parent, "x") is None
    assert db.children(parent) == [] and db.ledger(customer) == []
