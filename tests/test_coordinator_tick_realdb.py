"""One real coordinator tick against a real PostgreSQL holding the real schema.

The other coordinator tests hand the tick mocked cursors, so they check the
statements it sends and not what the database does with them, and they check
the budget, work detection and dispatch one function at a time. These tests
run coordinator.tick.run_tick in live mode (dry_run=False) on a throwaway
database loaded from web/src/db/schema.sql, with two real agents registered,
and look at what a whole tick did:

  * the budget is a bound on model calls, not only on tasks: failure triage in
    work detection, the support-answer draft and the failure-KB sync together
    make no more calls than the tick was allowed, and what they used is
    written to the tick record;
  * a tick that cannot read today's spend spends nothing, whichever spend
    source is configured: the worker's ledger (no ANTHROPIC_ADMIN_KEY) or the
    Anthropic cost report (key set). "Cannot read" is a statement that really
    fails on the real connection, or a cost report that cannot be fetched;
  * a zero budget means zero calls;
  * what a dispatched agent returns (its notes, on the task's row) is still
    in the database after the tick, and the performance figures scoring uses
    come from real rows;
  * the reconciliation sweep, with real build rows, the real read-only query
    and the worker's real probe (only Railway and the health request are
    stubbed), never offers for recovery a Railway project that a build row
    records, the failed build's own row included.
  * the worker's own coordinator loop, run for several ticks: a row written
    while a tick is running is picked up by the next tick, once, and a
    restarted process carries on from the last tick the database recorded;
  * an agent that raises leaves its task failed, not running, so the event
    behind it can be filed again;
  * an agent that runs past the round's deadline has its task closed at the
    deadline, and what its thread does after that is refused or dropped;
  * the failure circuit across the change from dry run to live: tasks that
    "failed" only because dry run refused them do not open it, and a circuit
    that real failures opened lets one probe through after its interval and
    closes on a success;
  * a tick that cannot read the pipeline counts treats that as "a customer
    build may be running", not as "none is";
  * initiative work is filed at most once per agent per interval;
  * the cost analyst's daily report against real ledger rows.

Nothing leaves the machine: the Anthropic SDK is replaced by a stub that
counts calls, the cost report fetch by a function that returns or raises, and
the root conftest blocks every non-loopback connection. The tests skip without
a local PostgreSQL (see the scratch_pg_url fixture in tests/conftest.py).
"""

from __future__ import annotations

import json
import sys
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coordinator import scorer, spend_tracker, tick
from coordinator.failure_kb_sync import DEFAULT_TOP_N
from coordinator.work_detection import MAX_TRIAGE_CALLS_PER_TICK

SCHEMA_SQL = ROOT / "web" / "src" / "db" / "schema.sql"
HAIKU = "claude-haiku-4-5"


@pytest.fixture(scope="module")
def coordinator_db_url(scratch_pg_url):
    """A second throwaway database holding the production schema."""
    import psycopg2
    from psycopg2 import extensions

    params = extensions.parse_dsn(scratch_pg_url)
    name = "cas_test_coordinator_" + uuid.uuid4().hex[:10]
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


class _LedgerFailureCursor:
    """A cursor that sends `SELECT 1/0` in place of chosen ledger statements,
    so the tick gets a real psycopg2 error and a really aborted transaction."""

    def __init__(self, real, owner):
        self._real, self._owner = real, owner

    def execute(self, sql, params=None):
        if "total_api_cost_usd" in str(sql):
            self._owner.ledger_reads += 1
            if self._owner.ledger_reads in self._owner.fail_reads:
                return self._real.execute("SELECT 1/0")
        if any(needle in str(sql) for needle in self._owner.fail_statements):
            self._owner.statements_failed += 1
            return self._real.execute("SELECT 1/0")
        return self._real.execute(sql, params)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._real.close()
        return False

    def __getattr__(self, name):
        return getattr(self._real, name)


class _LedgerFailureConn:
    """The tick's connection, with the Nth read of the ledger made to fail.
    Read 1 is build_world_state's, read 2 is the budget's."""

    def __init__(self, real, fail_reads=(), fail_statements=()):
        self._real, self.fail_reads, self.ledger_reads = real, set(fail_reads), 0
        # Statements containing one of these texts fail the same way.
        self.fail_statements, self.statements_failed = tuple(fail_statements), 0

    def cursor(self, *args, **kwargs):
        return _LedgerFailureCursor(self._real.cursor(*args, **kwargs), self)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _World:
    """What a test gets: the database, the model-call log and a tick runner."""

    def __init__(self, url, model_calls):
        self.url, self.model_calls = url, model_calls

    def run_tick(self, fail_ledger_reads=(), dry_run=False, fail_statements=()):
        import psycopg2
        real = psycopg2.connect(self.url)
        try:
            conn = _LedgerFailureConn(real, fail_ledger_reads, fail_statements)
            summary = tick.run_tick(conn, tick_number=1, dry_run=dry_run)
            self.ledger_reads = conn.ledger_reads
            self.statements_failed = conn.statements_failed
            return summary
        finally:
            real.close()

    def execute(self, sql, params=None):
        """A write, committed."""
        import psycopg2
        conn = psycopg2.connect(self.url)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() if cur.description else []
            conn.commit()
            return rows
        finally:
            conn.close()

    def query(self, sql, params=None):
        import psycopg2
        conn = psycopg2.connect(self.url)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            conn.close()

    def tick_budget(self):
        return self.query("SELECT tick_budget FROM coordinator_ticks ORDER BY created_at DESC LIMIT 1")[0][0]

    def finished_tasks(self):
        return {(agent, category): (status, float(cost) if cost is not None else None)
                for agent, category, status, cost in self.query(
                    "SELECT agent, task_category, status, actual_cost_usd FROM agent_tasks "
                    "WHERE status IN ('completed', 'failed')")}


@pytest.fixture()
def world(coordinator_db_url, monkeypatch, tmp_path):
    """A seeded database and a live coordinator that cannot reach anything real.

    Seed: seven failed builds whose error the regex classifier does not know
    (failure triage will want a model call for each, capped at five a tick),
    one support question with no knowledge-base answer (the customer service
    agent drafts one: one call) and one infra initiative task (the failure-KB
    sync drafts one answer per KB entry, twelve entries, capped at ten).
    Left to themselves that is sixteen model calls.
    """
    import psycopg2

    for name in ("ANTHROPIC_ADMIN_KEY", "GITHUB_TOKEN", "RAILWAY_API_TOKEN", "RESEND_API_KEY",
                 "RAILWAY_ENVIRONMENT", "COORDINATOR_ANALYSIS_ONLY", "ENABLE_TEST_BUILDS",
                 "AGENT_RECONCILIATION_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", coordinator_db_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-" + "a-key")
    # $2 a day, and the budget's clock two seconds before midnight UTC: one
    # tick is left, so the tick gets the 25% cap, $0.50, which is ten Haiku
    # calls at the $0.05 estimate.
    monkeypatch.setenv("COORDINATOR_DAILY_BUDGET", "2")
    monkeypatch.setenv("COORDINATOR_INTERVAL_S", "1800")
    clock = MagicMock()
    clock.now.return_value = datetime(2026, 4, 12, 23, 59, 58, tzinfo=timezone.utc)
    monkeypatch.setattr("coordinator.budget.datetime", clock)

    kb = tmp_path / "failure_kb.json"
    kb.write_text(json.dumps({
        f"sample failure signature {n}": {"signature": f"sample failure signature {n}",
                                          "fix_description": "retry", "success_count": 3, "failure_count": 1}
        for n in range(DEFAULT_TOP_N + 2)
    }))
    monkeypatch.setenv("FAILURE_KB_PATH", str(kb))
    roadmap = tmp_path / "roadmap.json"
    roadmap.write_text('{"version": 1, "items": []}\n')
    monkeypatch.setenv("COORDINATOR_ROADMAP_PATH", str(roadmap))

    # The Anthropic SDK, replaced by a stub that records the model of each call.
    model_calls = []

    class _Messages:
        def create(self, **kwargs):
            model_calls.append(kwargs.get("model"))
            system = kwargs.get("system") or ""
            text = "code_error" if "classify CAS build failures" in system else "A short drafted answer."
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)],
                                         usage=types.SimpleNamespace(input_tokens=300, output_tokens=5))

    class _Client:
        def __init__(self, *args, **kwargs):
            self.messages = _Messages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=_Client))
    monkeypatch.setattr(tick.time, "sleep", lambda *_a, **_k: None)  # pacing between dispatches
    monkeypatch.setitem(spend_tracker._anthropic_cache, "value", None)
    monkeypatch.setitem(spend_tracker._anthropic_cache, "expires", None)

    seed = psycopg2.connect(coordinator_db_url)
    with seed.cursor() as cur:
        cur.execute("TRUNCATE coordinator_ticks, agent_tasks, agent_learnings, builds, customers, "
                    "beta_feedback, support_kb, coordinator_findings, approval_requests CASCADE")
        cur.execute("INSERT INTO customers (email, password, name) VALUES (%s, 'x', 'Sample Person') RETURNING id",
                    (f"sample-{uuid.uuid4().hex[:8]}@example.test",))
        customer = cur.fetchone()[0]
        for n in range(MAX_TRIAGE_CALLS_PER_TICK + 2):
            cur.execute("INSERT INTO builds (customer_id, tier, status, app_name, error_message) "
                        "VALUES (%s, 'build', 'failed', %s, %s)",
                        (customer, f"sample-app-{n}", f"something odd happened in step {n}"))
        cur.execute("INSERT INTO beta_feedback (customer_id, category, message) "
                    "VALUES (%s, 'question', %s) RETURNING id",
                    (customer, "How do I change the photo on my home page?"))
        feedback = cur.fetchone()[0]
        cur.execute("INSERT INTO agent_tasks (agent, task_category, task_description, context, context_hash, status) "
                    "VALUES ('customer_service', 'customer_feedback', 'New feedback: question', %s, %s, 'pending')",
                    (json.dumps({"feedback_id": str(feedback), "feedback_category": "question"}), uuid.uuid4().hex))
        cur.execute("INSERT INTO agent_tasks (agent, task_category, task_description, context, context_hash, status) "
                    "VALUES ('infra_engineer', 'agent_initiative', 'Initiative: infra_engineer autonomous work', "
                    "%s, %s, 'pending')", (json.dumps({"agent": "infra_engineer"}), uuid.uuid4().hex))
    seed.commit()
    seed.close()

    saved = dict(tick._agent_registry)
    tick._agent_registry.clear()
    from coordinator.agents.customer_service import register as register_customer
    from coordinator.agents.infra_engineer import register as register_infra
    register_customer()
    register_infra()
    try:
        yield _World(coordinator_db_url, model_calls)
    finally:
        tick._agent_registry.clear()
        tick._agent_registry.update(saved)


def _cost_report(monkeypatch, usd_today=None, error=None):
    """Make the Anthropic cost report the spend source and decide what it says."""
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "not-" + "an-admin-key")

    def fetch(admin_key, starting_at, ending_at):
        if error is not None:
            raise error
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        return [{"starting_at": today, "results": [{"currency": "USD", "amount": str(usd_today * 100)}]}]

    monkeypatch.setattr(spend_tracker, "_fetch_cost_buckets", fetch)


def test_a_tick_makes_no_more_model_calls_than_its_budget(world):
    summary = world.run_tick()
    assert summary.get("error") is None, summary
    budget = world.tick_budget()
    assert (budget["max_api_calls"], budget["max_cost_usd"]) == (10, pytest.approx(0.50))

    # Sixteen calls were wanted; ten were allowed and ten were made, counting
    # triage (before dispatch) and both agents together.
    assert len(world.model_calls) == 10
    assert budget["model_calls_granted"] == 10
    assert budget["model_calls_estimated_usd"] == pytest.approx(0.50)
    # Every one on the cheap tier, including the sync run by an agent that was
    # itself dispatched on Sonnet.
    assert set(world.model_calls) == {HAIKU}

    # Triage took its five first; the dispatch loop saw what was left.
    triage = [d for d in world.query("SELECT decisions FROM coordinator_ticks")[0][0]
              if d.get("decision") == "failure_triage"]
    assert triage and triage[0]["model_calls"] == MAX_TRIAGE_CALLS_PER_TICK
    assert triage[0]["budget_after"]["remaining_calls"] == 10 - MAX_TRIAGE_CALLS_PER_TICK

    # Both agents ran, and the ledger books a task at no less than the calls it made.
    finished = world.finished_tasks()
    assert finished[("customer_service", "customer_feedback")][0] == "completed"
    assert finished[("infra_engineer", "agent_initiative")][0] == "completed"
    sync = world.query("SELECT result FROM agent_tasks WHERE agent = 'infra_engineer'")[0][0]["sync"]
    assert sync["drafted"] + sync["skipped_not_authorized"] == DEFAULT_TOP_N
    assert finished[("infra_engineer", "agent_initiative")][1] >= max(0.12, 0.05 * sync["drafted"]) - 1e-9


@pytest.mark.parametrize("scenario", [
    "ledger: the budget's read fails",
    "ledger: no read succeeds",
    "cost report: unreachable",
    "cost report: over budget",
    "daily budget is zero",
])
def test_a_tick_with_no_budget_makes_no_model_call(world, monkeypatch, scenario):
    fail_reads, reads_expected = (), 0
    if scenario == "ledger: the budget's read fails":
        fail_reads, reads_expected = (2,), 2   # the world state's read (1) succeeded a moment earlier
    elif scenario == "ledger: no read succeeds":
        fail_reads, reads_expected = (1, 2, 3, 4), 1   # after the first failure nothing reads again
    elif scenario == "cost report: unreachable":
        _cost_report(monkeypatch, error=OSError("cost report unreachable"))
    elif scenario == "cost report: over budget":
        _cost_report(monkeypatch, usd_today=25)   # the ledger says nothing was spent
    elif scenario == "daily budget is zero":
        monkeypatch.setenv("COORDINATOR_DAILY_BUDGET", "0")

    summary = world.run_tick(fail_ledger_reads=fail_reads)
    assert summary.get("error") is None, summary
    if fail_reads:
        assert world.ledger_reads == reads_expected, "the ledger read that was to fail never happened"

    budget = world.tick_budget()
    assert (budget["max_api_calls"], budget["max_cost_usd"], budget["max_opus_calls"]) == (0, 0.0, 0)
    assert world.model_calls == [], f"{len(world.model_calls)} model call(s) in a tick with no budget"
    assert budget["model_calls_granted"] == 0
    assert world.finished_tasks() == {}
    assert summary["tasks_dispatched"] == 0


@pytest.mark.parametrize("scenario", [
    "ledger: the budget's read fails",
    "cost report: unreachable",
    "cost report: over budget",
    "daily budget is zero",
])
def test_a_tick_with_no_budget_still_runs_a_task_that_makes_no_model_call(world, monkeypatch, scenario):
    """The budget is an allowance of model calls. An app-down alert makes none,
    so an unreadable cost report (or a spent day) does not silence it, while
    the two tasks of the world that can reach a model stay where they are,
    pending, and no call is made."""
    sent = []
    monkeypatch.setattr("ops.alerter.send_admin_alert", lambda subject, body: sent.append(subject) or True)
    world.execute(
        "INSERT INTO agent_tasks (agent, task_category, task_description, context, context_hash, status) "
        "VALUES ('infra_engineer', 'app_down', 'App down', %s, %s, 'pending')",
        (json.dumps({"app_name": "sample-app", "consecutive_failures": 4}), uuid.uuid4().hex))
    fail_reads = ()
    if scenario == "ledger: the budget's read fails":
        fail_reads = (2,)
    elif scenario == "cost report: unreachable":
        _cost_report(monkeypatch, error=OSError("cost report unreachable"))
    elif scenario == "cost report: over budget":
        _cost_report(monkeypatch, usd_today=25)
    elif scenario == "daily budget is zero":
        monkeypatch.setenv("COORDINATOR_DAILY_BUDGET", "0")

    summary = world.run_tick(fail_ledger_reads=fail_reads)

    assert summary.get("error") is None, summary
    budget = world.tick_budget()
    assert (budget["max_api_calls"], budget["max_cost_usd"]) == (0, 0.0)
    assert world.model_calls == [] and budget["model_calls_granted"] == 0
    assert set(world.finished_tasks()) == {("infra_engineer", "app_down")}
    assert world.finished_tasks()[("infra_engineer", "app_down")][0] == "completed"
    assert summary["tasks_dispatched"] == 1
    assert sent == ["[CAS] App down: sample-app (4 failures)"]
    result = world.query("SELECT result FROM agent_tasks WHERE task_category = 'app_down'")[0][0]
    assert result["action"] == "alert_sent" and result["effects"][0]["outcome"] == "sent"
    still_pending = {(agent, category) for agent, category in world.query(
        "SELECT agent, task_category FROM agent_tasks WHERE status = 'pending'")}
    # (work detection also files tasks for agents this world does not register; they wait too)
    assert {("customer_service", "customer_feedback"), ("infra_engineer", "agent_initiative")} <= still_pending


def test_the_cost_report_is_the_budget_when_the_key_is_set(world, monkeypatch):
    # $1.80 of the $2 day is spent on the account; the worker's ledger shows none of it.
    _cost_report(monkeypatch, usd_today=1.80)
    world.run_tick()
    budget = world.tick_budget()
    assert budget["max_cost_usd"] == pytest.approx(0.20)
    assert budget["max_api_calls"] == 4
    assert len(world.model_calls) == 4 == budget["model_calls_granted"]


def test_a_requeued_builds_cost_stays_in_todays_ledger(world):
    """Every requeue (five statements in the web app, four in the worker) sets
    builds.started_at back to NULL and leaves total_api_cost_usd on the row.
    The ledger must still count that money as spent today."""
    import psycopg2

    conn = psycopg2.connect(world.url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM customers LIMIT 1")
            customer = cur.fetchone()[0]
            cur.execute("INSERT INTO builds (customer_id, tier, status, app_name, total_api_cost_usd, started_at) "
                        "VALUES (%s, 'build', 'building', 'sample-costly', 3.25, NOW()) RETURNING id", (customer,))
            build = cur.fetchone()[0]
            conn.commit()
            assert spend_tracker.get_daily_spend(conn)["build_pipeline_usd"] == pytest.approx(3.25)
            # the worker's requeue, reduced to the columns that matter here
            cur.execute("UPDATE builds SET status = 'queued', started_at = NULL WHERE id = %s", (build,))
            conn.commit()
            assert spend_tracker.get_daily_spend(conn)["build_pipeline_usd"] == pytest.approx(3.25)
    finally:
        conn.close()

    # and a tick budgets against it: $2 a day, $3.25 spent, nothing left
    world.run_tick()
    budget = world.tick_budget()
    assert (budget["max_api_calls"], budget["max_cost_usd"]) == (0, 0.0)
    assert world.model_calls == []


def test_what_an_agent_returns_is_still_there_after_the_tick(world):
    """An agent's notes are kept on its task's row. (They used to be copied
    into agent_learnings as well, where the coordinator read them back into
    an argument no agent used; the tick no longer writes that table.)"""
    world.run_tick()
    notes = {agent: learnings for agent, learnings in world.query(
        "SELECT agent, learnings FROM agent_tasks WHERE status = 'completed'")}
    assert any("Support KB gap" in note for note in notes["customer_service"]), notes
    assert any("Failure KB" in note for note in notes["infra_engineer"]), notes
    assert world.query("SELECT COUNT(*) FROM agent_learnings")[0][0] == 0


def test_scoring_reads_agent_performance_from_the_task_rows(world):
    import psycopg2

    world.run_tick()
    conn = psycopg2.connect(world.url)
    try:
        cache = scorer.build_performance_cache(conn)
        assert conn.info.transaction_status == psycopg2.extensions.TRANSACTION_STATUS_INTRANS, \
            "the query failed and was rolled back; an empty cache would hide that"
    finally:
        conn.close()
    assert cache[("customer_service", "customer_feedback")] == {"total": 1, "successful": 1}
    assert cache[("infra_engineer", "agent_initiative")] == {"total": 1, "successful": 1}


# ---------------------------------------------------------------------------
# The reconciliation sweep against real build rows and the real probe
# ---------------------------------------------------------------------------

def _railway(monkeypatch, projects):
    """Stand in for Railway and for the deployed app, nothing else: the
    project list, the service domain lookup and the /health request. The
    probe's own filtering (name, time window, excluded ids) runs for real.
    Returns the list of project ids whose domain was looked up, which is how
    far the probe got with each candidate."""
    import importlib

    import monitor.ping as ping

    try:                                   # the module the probe itself resolves
        deploy = importlib.import_module("deploy")
    except Exception:
        from cli import deploy
    looked_up = []

    def get_service_domain(project_id, token, deadline=None):
        looked_up.append(project_id)
        return {"domain": f"{project_id}.up.railway.app", "service_id": f"svc-{project_id}"}

    monkeypatch.setattr(deploy, "get_token", lambda: "not-" + "a-token")
    monkeypatch.setattr(deploy, "list_projects", lambda token, deadline=None: projects)
    monkeypatch.setattr(deploy, "get_service_domain", get_service_domain)
    monkeypatch.setattr(ping, "probe_url_problem", lambda url, **_kw: None)
    healthy = types.SimpleNamespace(status_code=200, text='{"status":"ok"}', body_truncated=False)
    monkeypatch.setattr(ping, "guarded_session", lambda **_caps: types.SimpleNamespace(
        get=lambda url, timeout=None, allow_redirects=True: healthy, close=lambda: None))
    return looked_up


@pytest.fixture()
def failed_build(coordinator_db_url, monkeypatch):
    """One build that failed on a supervisor timeout an hour after it started.
    Its row records `proj-announced`: the deployment it reached preview with
    before it was requeued in place and failed. A second row of the same app
    name (another customer's build) records `proj-of-another-build`."""
    import psycopg2

    for name in ("RESEND_API_KEY", "COORDINATOR_ANALYSIS_ONLY", "RAILWAY_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", coordinator_db_url)
    conn = psycopg2.connect(coordinator_db_url)
    with conn.cursor() as cur:
        # builds.started_at and completed_at are TIMESTAMP without time zone,
        # written with NOW(), and the probe reads them as UTC (which is what
        # the deployed database's clock is). Seed them in UTC whatever this
        # machine's Postgres is set to.
        cur.execute("SET TIME ZONE 'UTC'")
        cur.execute("TRUNCATE builds, customers, approval_requests CASCADE")
        cur.execute("INSERT INTO customers (email, password, name) VALUES (%s, 'x', 'Sample Person') RETURNING id",
                    (f"sample-{uuid.uuid4().hex[:8]}@example.test",))
        customer = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO builds (customer_id, tier, status, app_name, error_message, started_at, "
            "completed_at, railway_project_id) VALUES (%s, 'build', 'failed', 'sample-studio', "
            "'supervisor timeout: force-failed', NOW() - INTERVAL '1 hour', NOW(), 'proj-announced') "
            "RETURNING id", (customer,))
        build_id = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO builds (customer_id, tier, status, app_name, railway_project_id) "
            "VALUES (%s, 'build', 'live', 'sample-studio', 'proj-of-another-build')", (customer,))
    conn.commit()
    conn.close()
    return build_id


def _sweep(coordinator_db_url):
    from coordinator.actuators import Actuators
    from coordinator.agents.reconciliation import ReconciliationAgent
    from coordinator.state import WorkItem, WorldState

    task = WorkItem(id="sweep", category="reconciliation_sweep", target_agent="reconciliation")
    with patch("ops.alerter.send_admin_alert", return_value=True):
        return ReconciliationAgent().execute(
            task, WorldState(), HAIKU, Actuators("reconciliation", "sweep", dry_run=False))


def _approval_requests(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT context->>'build_id', context->>'project_id' FROM approval_requests")
            return cur.fetchall()
    finally:
        conn.close()


def _project(project_id):
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": project_id, "name": "sample-studio", "createdAt": created}


def test_a_project_recorded_on_a_build_row_is_never_offered_as_a_zombie(
        coordinator_db_url, failed_build, monkeypatch):
    """Railway has two live projects of this app name and both are recorded
    on build rows: one on another build, one on the failed build's own row.
    Both are announced deployments. The sweep must find nothing to recover."""
    looked_up = _railway(monkeypatch, [_project("proj-announced"), _project("proj-of-another-build")])

    result = _sweep(coordinator_db_url)

    assert result.success is True, result.output
    assert result.output["recovered"] == []
    assert _approval_requests(coordinator_db_url) == []
    assert looked_up == [], "an excluded project was still probed"


def test_a_project_no_row_records_is_still_found(coordinator_db_url, failed_build, monkeypatch):
    """The control: next to the two recorded projects there is one that no
    row records, which is what a failed attempt leaves behind. It is found,
    and a person is asked."""
    looked_up = _railway(monkeypatch, [_project("proj-announced"), _project("proj-left-behind"),
                                       _project("proj-of-another-build")])

    result = _sweep(coordinator_db_url)

    assert result.success is True, result.output
    assert [r["project_id"] for r in result.output["recovered"]] == ["proj-left-behind"]
    assert _approval_requests(coordinator_db_url) == [(failed_build, "proj-left-behind")]
    assert looked_up == ["proj-left-behind"]


def test_candidates_are_recent_supervisor_timeouts_only(coordinator_db_url, failed_build, monkeypatch):
    """The candidate query on real rows: a failure that is not a timeout, a
    timeout older than the window and a build that is not failed are left out."""
    import psycopg2

    conn = psycopg2.connect(coordinator_db_url)
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
        cur.execute("SELECT id FROM customers LIMIT 1")
        customer = cur.fetchone()[0]
        for app, status, error, age in (
            ("sample-broken", "failed", "npm install exploded", "1 hour"),
            ("sample-old", "failed", "Supervisor TIMEOUT after 3600s", "80 hours"),
            ("sample-preview", "preview", "watchdog note left on a healthy build", "1 hour"),
            ("sample-watchdog", "failed", "killed by the Watchdog", "2 hours"),
        ):
            cur.execute(
                "INSERT INTO builds (customer_id, tier, status, app_name, error_message, started_at, completed_at) "
                "VALUES (%s, 'build', %s, %s, %s, NOW() - %s::interval - INTERVAL '1 hour', NOW() - %s::interval)",
                (customer, status, app, error, age, age))
    conn.commit()
    conn.close()
    looked_up = _railway(monkeypatch, [])   # Railway knows no project: nothing can be recovered

    from coordinator.actuators import Actuators
    from coordinator.agents.reconciliation import ReconciliationAgent
    sql, params = ReconciliationAgent._candidate_query()
    rows = Actuators("reconciliation", "sweep", dry_run=False).query_readonly(sql, params)

    assert sorted(row["app_name"] for row in rows) == ["sample-studio", "sample-watchdog"]
    result = _sweep(coordinator_db_url)
    assert result.success is True and result.output["skipped_count"] == 2, result.output
    assert looked_up == []


# ---------------------------------------------------------------------------
# The watermark: what one tick leaves for the next
# ---------------------------------------------------------------------------

def _feedback(world, message):
    """A support question from the seeded customer, committed now."""
    import psycopg2
    customer = world.query("SELECT id FROM customers LIMIT 1")[0][0]
    conn = psycopg2.connect(world.url)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO beta_feedback (customer_id, category, message) "
                        "VALUES (%s, 'question', %s) RETURNING id", (customer, message))
            feedback_id = str(cur.fetchone()[0])
        conn.commit()
        return feedback_id
    finally:
        conn.close()


def _tasks_for(world, feedback_id):
    return world.query("SELECT status FROM agent_tasks WHERE task_category = 'customer_feedback' "
                       "AND context->>'feedback_id' = %s", (feedback_id,))


def _run_the_workers_coordinator_loop(world, monkeypatch, ticks, during_tick=None):
    """queue/worker_pg.py's own coordinator loop, for `ticks` ticks.

    The loop is the worker's (it decides what each tick is told about the one
    before). Replaced: the thread start, the registration of all ten agents
    (the fixture's two stay), and the connection factory, which points at the
    test database. `during_tick(n)` runs in the middle of tick n, after that
    tick has looked for work and before it dispatches."""
    import psycopg2

    queue_dir = str(ROOT / "queue")
    if queue_dir not in sys.path:
        sys.path.insert(0, queue_dir)
    import worker_pg

    captured = {}

    class _Thread:
        def __init__(self, target=None, **_kwargs):
            captured["target"] = target

        def start(self):
            pass

    real_run_tick = tick.run_tick
    real_performance_cache = scorer.build_performance_cache
    summaries = []
    current = {"tick": 0}

    def counted_tick(**kwargs):
        current["tick"] = kwargs["tick_number"]
        summaries.append(real_run_tick(**kwargs))
        if len(summaries) >= ticks:
            worker_pg._shutdown_event.set()
        return summaries[-1]

    def performance_cache(conn):
        if during_tick is not None:
            during_tick(current["tick"])
        return real_performance_cache(conn)

    monkeypatch.setenv("COORDINATOR_DRY_RUN", "false")
    monkeypatch.setenv("COORDINATOR_INTERVAL_S", "1")
    with patch.object(worker_pg.threading, "Thread", _Thread):
        worker_pg._start_coordinator_thread()
    monkeypatch.setattr(tick, "register_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(tick, "run_tick", counted_tick)
    monkeypatch.setattr(scorer, "build_performance_cache", performance_cache)
    monkeypatch.setattr(worker_pg, "_get_conn", lambda *_a, **_k: psycopg2.connect(world.url))
    was_set = worker_pg._shutdown_event.is_set()
    worker_pg._shutdown_event.clear()
    try:
        captured["target"]()
    finally:
        if not was_set:
            worker_pg._shutdown_event.clear()
    return summaries


def test_a_row_written_while_a_tick_runs_is_seen_by_the_next_tick_once(world, monkeypatch):
    """The loop used to hand each tick the time the previous one ENDED. A
    question that arrived while tick 2 was dispatching was then older than
    tick 3's starting point, and newer than what tick 2 had looked at: no tick
    ever saw it."""
    arrived = {}

    def during_tick(number):
        if number == 2 and "id" not in arrived:
            arrived["id"] = _feedback(world, "Can I add a second page later?")

    summaries = _run_the_workers_coordinator_loop(world, monkeypatch, ticks=4, during_tick=during_tick)
    assert [s.get("error") for s in summaries] == [None] * 4, summaries
    assert not any(s.get("skipped") for s in summaries), summaries

    tasks = _tasks_for(world, arrived["id"])
    assert len(tasks) == 1, f"the question that arrived during tick 2 was filed {len(tasks)} times"
    # Each tick's watermark is the database time at its start, and they only move forward.
    marks = [s["watermark"] for s in summaries]
    assert marks == sorted(marks) and len(set(marks)) == 4
    recorded = [row[0] for row in world.query("SELECT started_at FROM coordinator_ticks ORDER BY started_at")]
    assert recorded == marks, "coordinator_ticks.started_at is the watermark a restart resumes from"


def test_a_restarted_process_resumes_from_the_last_tick_in_the_database(world, monkeypatch):
    """A new worker process has no watermark of its own. It used to start
    from nothing, and whatever arrived between the old process's last tick
    and the restart was never looked at."""
    monkeypatch.setenv("COORDINATOR_INTERVAL_S", "1")
    first = world.run_tick()
    assert first.get("error") is None, first
    between = _feedback(world, "Where do I change my opening hours?")
    world.query("SELECT pg_sleep(0.6)")   # past the overlap guard (half of the 1 s interval)

    second = world.run_tick()             # last_tick_time=None, as after a restart
    assert second.get("error") is None and not second.get("skipped"), second
    assert len(_tasks_for(world, between)) == 1
    assert second["watermark"] > first["watermark"]


def test_a_first_ever_tick_does_not_reach_back_for_old_rows(world):
    """With no completed tick in the database there is nothing to resume
    from: rows that already exist are not treated as new."""
    seeded = world.query("SELECT id::text FROM beta_feedback")[0][0]
    world.run_tick()
    # The fixture seeds one task for that question by hand; the tick adds none.
    assert len(_tasks_for(world, seeded)) == 1


def test_an_agent_that_raises_leaves_its_task_failed_and_the_event_can_be_filed_again(world):
    """The task used to stay 'running' until the stuck-task sweep failed it
    hours later, and for that long the same work item could not be filed."""
    import psycopg2

    class _Raising:
        name = "customer_service"

        def execute(self, **_kwargs):
            raise RuntimeError("the agent fell over")

    tick.register_agent("customer_service", _Raising)
    summary = world.run_tick()
    assert summary.get("error") is None, summary

    rows = world.query("SELECT status, result, completed_at, context_hash, context FROM agent_tasks "
                       "WHERE agent = 'customer_service' AND task_category = 'customer_feedback'")
    assert len(rows) == 1
    status, result, completed_at, context_hash, context = rows[0]
    assert status == "failed", f"the task was left {status!r}"
    assert "the agent fell over" in result["error"] and completed_at is not None

    from coordinator.state import WorkItem
    conn = psycopg2.connect(world.url)
    try:
        again = WorkItem(category="customer_feedback", target_agent="customer_service",
                         description="New feedback: question", context=context, context_hash=context_hash)
        assert tick._persist_work_item(conn, again, summary["tick_id"]) is True
        conn.commit()
    finally:
        conn.close()


def test_an_agent_that_runs_past_the_deadline_has_its_task_closed_and_its_late_work_dropped(
        world, monkeypatch):
    """The tick stops waiting at the round's deadline but cannot stop the
    thread. The task used to stay 'running' (holding its work item's dedup
    slot) until the stuck-task sweep, and whatever the thread did when it
    finally returned was written as if nothing had happened: the task was
    completed, its learnings recorded, its escalation filed, and its actuators
    still sent."""
    import threading

    import psycopg2

    from coordinator.agents.base import AgentResult
    from coordinator.state import WorkItem

    release, thread_done = threading.Event(), threading.Event()
    late, outcomes = {}, []

    class _Slow:
        name = "customer_service"

        def execute(self, task, state, model, actuators):
            release.wait(60)            # far past the one-second deadline below
            try:
                late["alert"] = actuators.send_alert("late alert", "sent after the deadline")
            except PermissionError as exc:
                late["alert"] = str(exc)
            try:
                late["approval"] = actuators.create_approval_request(
                    "escalation:customer_service", "a request filed after the deadline")
            except PermissionError as exc:
                late["approval"] = str(exc)
            return AgentResult(success=True, output={"findings": ["a late finding"]},
                               learnings=["a late learning"], escalate=True, escalation_reason="late")

    real_dispatch = tick._dispatch_one_parallel

    def watched_dispatch(*args, **kwargs):
        try:
            outcomes.append(real_dispatch(*args, **kwargs))
            return outcomes[-1]
        finally:
            thread_done.set()

    tick._agent_registry.pop("infra_engineer")        # one task in the round: the slow one
    tick.register_agent("customer_service", _Slow)
    monkeypatch.setattr(tick, "_dispatch_one_parallel", watched_dispatch)
    monkeypatch.setattr(tick, "_get_agent_timeout", lambda *_args: 1)

    def task_row():
        rows = world.query(
            "SELECT status, result, completed_at, learnings, escalated, context_hash, context "
            "FROM agent_tasks WHERE agent = 'customer_service' AND task_category = 'customer_feedback'")
        assert len(rows) == 1
        return rows[0]

    try:
        summary = world.run_tick()

        # At the deadline: the tick records the timeout and the row is closed.
        tick_errors = world.query("SELECT errors FROM coordinator_ticks ORDER BY created_at DESC LIMIT 1")[0][0]
        assert [error["error"] for error in tick_errors] == ["Agent timed out after 1s"], tick_errors
        status, result, completed_at, _learnings, _escalated, context_hash, context = task_row()
        assert status == "failed", f"the task was left {status!r} while its thread ran on"
        assert result == {"error": "Agent timed out after 1s", "timed_out": True}
        assert completed_at is not None
        assert not thread_done.is_set(), "the agent's thread is still running: that is the case under test"

        # The event behind it can be filed again straight away.
        conn = psycopg2.connect(world.url)
        try:
            again = WorkItem(category="customer_feedback", target_agent="customer_service",
                             description="New feedback: question", context=context, context_hash=context_hash)
            assert tick._persist_work_item(conn, again, summary["tick_id"]) is True
            conn.rollback()
        finally:
            conn.close()
    finally:
        release.set()
        assert thread_done.wait(30), "the agent's thread never finished"

    # What the thread did after the deadline came to nothing.
    assert "closed at the deadline" in late["alert"]
    assert "closed at the deadline" in late["approval"]
    assert [outcome["decision"] for outcome in outcomes] == ["abandoned"]
    status, result, _completed_at, learnings, escalated, _hash, _context = task_row()
    assert status == "failed" and result == {"error": "Agent timed out after 1s", "timed_out": True}
    assert not learnings and not escalated
    assert world.query("SELECT COUNT(*) FROM approval_requests")[0][0] == 0
    assert world.query("SELECT COUNT(*) FROM coordinator_findings")[0][0] == 0



# ---------------------------------------------------------------------------
# The failure circuit, from dry run to live
# ---------------------------------------------------------------------------

def _only_the_backend_engineer(world, monkeypatch):
    """One failed build (so one build_failure task per tick, routed to the
    backend engineer by the rule classifier) and no other work: nothing else
    is pending, and no agent is in the initiative rotation."""
    from coordinator import work_detection
    from coordinator.agents.backend_engineer import register as register_backend
    monkeypatch.setattr(work_detection, "_AGENT_ROTATION", [])
    tick._agent_registry.clear()
    register_backend()
    world.execute("DELETE FROM agent_tasks")
    world.execute("DELETE FROM builds WHERE app_name <> 'sample-app-0'")
    # An error the regex classifier knows, so no triage call is wanted.
    world.execute("UPDATE builds SET error_message = 'TypeError: x is not a function'")
    return world.query("SELECT id::text FROM builds")[0][0]


def _next_tick(world, monkeypatch, **kwargs):
    """Run one more tick, a little later than the last one (the overlap guard
    is half of the one-second interval set here)."""
    monkeypatch.setenv("COORDINATOR_INTERVAL_S", "1")
    world.query("SELECT pg_sleep(0.6)")
    summary = world.run_tick(**kwargs)
    assert summary.get("error") is None and not summary.get("skipped"), summary
    return summary


def _backend_tasks(world):
    return world.query(
        "SELECT status, result FROM agent_tasks WHERE agent = 'backend_engineer' "
        "AND task_category = 'build_failure' ORDER BY created_at")


def _performance_cache(world):
    """What the scorer reads from the task rows, on a connection of its own."""
    import psycopg2

    conn = psycopg2.connect(world.url)
    try:
        cache = scorer.build_performance_cache(conn)
        assert conn.info.transaction_status == psycopg2.extensions.TRANSACTION_STATUS_INTRANS, \
            "the query failed and was rolled back; an empty cache would hide that"
        return cache
    finally:
        conn.close()


def _last_decisions(world):
    return world.query("SELECT decisions FROM coordinator_ticks ORDER BY started_at DESC LIMIT 1")[0][0]


def test_dry_run_does_not_trip_the_failure_circuit_and_live_work_starts_when_it_is_turned_off(
        world, monkeypatch):
    """The default configuration, then the switch. Under dry run the rebuild
    is refused by design, the handler reports no success, and the task row
    says 'failed'. Three of those used to open the circuit for good: the
    fourth tick skipped the pair, a skipped task writes no outcome, and so
    turning dry run off changed nothing."""
    build = _only_the_backend_engineer(world, monkeypatch)

    for _tick in range(4):
        _next_tick(world, monkeypatch, dry_run=True)
    rows = _backend_tasks(world)
    assert [status for status, _result in rows] == ["failed"] * 4, (
        "under dry run every tick still dispatches the task", rows)
    assert all(result["dry_run"] is True and result["action"] == "dry_run" for _status, result in rows)
    assert world.query("SELECT COUNT(*) FROM builds WHERE parent_build_id IS NOT NULL")[0][0] == 0
    # Nor do the four refusals count against the agent's score.
    assert ("backend_engineer", "build_failure") not in _performance_cache(world)

    summary = _next_tick(world, monkeypatch, dry_run=False)

    assert summary["tasks_dispatched"] == 1, _last_decisions(world)
    status, result = _backend_tasks(world)[-1]
    assert status == "completed" and result["action"] == "rebuild_queued" and "dry_run" not in result
    children = world.query("SELECT parent_build_id::text, status FROM builds WHERE parent_build_id IS NOT NULL")
    assert children == [(build, "queued")], "the first live tick queued the rebuild the dry runs only described"
    assert _performance_cache(world)[("backend_engineer", "build_failure")] == {"total": 1, "successful": 1}


def test_an_open_circuit_lets_one_probe_through_after_its_interval_and_a_success_closes_it(
        world, monkeypatch):
    import psycopg2

    from coordinator import config
    build = _only_the_backend_engineer(world, monkeypatch)
    # Five real (live) failures of the pair, just now.
    for n in range(5):
        world.execute(
            "INSERT INTO agent_tasks (agent, task_category, context_hash, status, result, completed_at) "
            "VALUES ('backend_engineer', 'build_failure', %s, 'failed', %s, NOW())",
            (uuid.uuid4().hex, json.dumps({"action": "rebuild_failed"})))

    def circuit():
        conn = psycopg2.connect(world.url)
        try:
            return tick._failure_circuit(conn, "backend_engineer", "build_failure")
        finally:
            conn.close()

    assert circuit() == ("open", 1.0)
    summary = _next_tick(world, monkeypatch)
    assert summary["tasks_dispatched"] == 0
    skipped = [d for d in _last_decisions(world) if d.get("agent") == "backend_engineer"]
    assert [(d["decision"], d["failure_circuit"]) for d in skipped] == [("skip", "open")]
    assert world.query("SELECT COUNT(*) FROM builds WHERE parent_build_id IS NOT NULL")[0][0] == 0

    # Nothing happens for as long as the interval has not passed, however many ticks run...
    _next_tick(world, monkeypatch)
    assert world.query("SELECT COUNT(*) FROM builds WHERE parent_build_id IS NOT NULL")[0][0] == 0

    # ...and once it has (the failures are now older than the retry interval), one probe goes out.
    world.execute(
        "UPDATE agent_tasks SET completed_at = completed_at - make_interval(secs => %s) "
        "WHERE status = 'failed'", (config.FAILURE_RETRY_HOURS * 3600 + 60,))
    assert circuit() == ("half_open", 1.0)
    summary = _next_tick(world, monkeypatch)
    assert summary["tasks_dispatched"] == 1
    probe = [d for d in _last_decisions(world) if d.get("decision") == "probe"]
    assert len(probe) == 1 and probe[0]["failure_circuit"] == "half_open"
    assert world.query("SELECT parent_build_id::text FROM builds WHERE parent_build_id IS NOT NULL") == [(build,)]

    # The probe succeeded: the circuit is closed, with five old failures still in the window.
    state, rate = circuit()
    assert state == "closed" and rate == pytest.approx(5 / 6)


def test_a_probe_that_fails_opens_the_circuit_again(world, monkeypatch):
    import psycopg2

    from coordinator import config
    _only_the_backend_engineer(world, monkeypatch)
    for n in range(3):
        world.execute(
            "INSERT INTO agent_tasks (agent, task_category, context_hash, status, result, completed_at) "
            "VALUES ('backend_engineer', 'build_failure', %s, 'failed', '{}', "
            "NOW() - make_interval(secs => %s))",
            (uuid.uuid4().hex, config.FAILURE_RETRY_HOURS * 3600 + 60))
    # The probe will fail for a real reason: the build has no customer to rebuild for.
    world.execute("UPDATE builds SET customer_id = NULL")

    summary = _next_tick(world, monkeypatch)
    assert summary["tasks_dispatched"] == 1
    assert _backend_tasks(world)[-1][0] == "failed"
    conn = psycopg2.connect(world.url)
    try:
        assert tick._failure_circuit(conn, "backend_engineer", "build_failure") == ("open", 1.0)
    finally:
        conn.close()
    summary = _next_tick(world, monkeypatch)
    assert summary["tasks_dispatched"] == 0, "one probe per interval, not one per tick"


def test_two_tasks_of_a_half_open_pair_get_one_probe_between_them(world, monkeypatch):
    from coordinator import config
    _only_the_backend_engineer(world, monkeypatch)
    for n in range(3):
        world.execute(
            "INSERT INTO agent_tasks (agent, task_category, context_hash, status, result, completed_at) "
            "VALUES ('backend_engineer', 'build_failure', %s, 'failed', '{}', "
            "NOW() - make_interval(secs => %s))",
            (uuid.uuid4().hex, config.FAILURE_RETRY_HOURS * 3600 + 60))
    # A second pending task of the same pair, besides the one the tick will file.
    world.execute(
        "INSERT INTO agent_tasks (agent, task_category, task_description, context, context_hash, status) "
        "VALUES ('backend_engineer', 'build_failure', 'Build failed: other', %s, %s, 'pending')",
        (json.dumps({"build_id": str(uuid.uuid4()), "error_message": "x"}), uuid.uuid4().hex))

    _next_tick(world, monkeypatch)

    decisions = [d for d in _last_decisions(world) if d.get("agent") == "backend_engineer"]
    assert sorted(d["decision"] for d in decisions) == ["dispatch", "probe", "skip"], decisions


def test_a_failure_history_that_cannot_be_read_holds_the_task(world, monkeypatch):
    """It used to read as "no failures"."""
    _only_the_backend_engineer(world, monkeypatch)
    summary = _next_tick(world, monkeypatch, fail_statements=("COALESCE(completed_at, created_at) <=",))
    assert world.statements_failed >= 1, "the history query that was to fail never ran"
    assert summary["tasks_dispatched"] == 0
    held = [d for d in _last_decisions(world) if d.get("agent") == "backend_engineer"]
    assert [(d["decision"], d["failure_circuit"]) for d in held] == [("skip", "unknown")]


# ---------------------------------------------------------------------------
# Unknown is not zero
# ---------------------------------------------------------------------------

def test_a_tick_that_cannot_read_the_pipeline_yields_as_if_a_customer_build_were_running(world):
    """The pipeline query fails for real (the transaction is aborted and
    rolled back). active_builds used to stay at its default of 0, so the
    "customer builds always win" rule did not apply and the tick got its
    whole budget: ten calls, every model."""
    summary = world.run_tick(fail_statements=("GROUP BY status",))
    assert summary.get("error") is None, summary
    assert world.statements_failed == 1

    assert summary["active_builds"] is None and summary["budget_mode"] == "haiku_only"
    budget = world.tick_budget()
    assert budget["allowed_models"] == [HAIKU]
    assert budget["max_api_calls"] == 1 and budget["max_opus_calls"] == 0
    assert budget["max_cost_usd"] == pytest.approx(0.10)
    assert len(world.model_calls) <= 1
    recorded = world.query("SELECT world_state_summary FROM coordinator_ticks")[0][0]
    assert recorded["active_builds"] is None and recorded["queued_builds"] is None
    assert recorded["failed_builds_24h"] is None, "unknown is stored as null, not as 0"
    assert recorded["spend_known"] is True, "the ledger was readable; only the pipeline was not"


def test_the_control_a_readable_pipeline_with_no_build_running_gets_the_normal_budget(world):
    world.run_tick()
    budget = world.tick_budget()
    assert budget["max_api_calls"] == 10 and len(budget["allowed_models"]) == 3


def test_a_running_customer_build_and_an_unknown_pipeline_get_the_same_budget(world):
    world.execute("UPDATE builds SET status = 'building' WHERE app_name = 'sample-app-0'")
    world.run_tick()
    with_a_build = world.tick_budget()
    assert (with_a_build["allowed_models"], with_a_build["max_api_calls"]) == ([HAIKU], 1)


# ---------------------------------------------------------------------------
# Initiative work is weekly
# ---------------------------------------------------------------------------

def test_an_idle_coordinator_files_one_initiative_per_agent_per_interval(world):
    import psycopg2

    from coordinator import config, work_detection

    world.execute("DELETE FROM agent_tasks")
    rotation = list(work_detection._AGENT_ROTATION)

    def detect():
        conn = psycopg2.connect(world.url)
        try:
            return work_detection._check_agent_initiative(conn)
        finally:
            conn.close()

    # Idle tick after idle tick: each agent gets its turn once...
    filed = []
    for _tick in range(len(rotation) + 3):
        items = detect()
        if not items:
            break
        assert len(items) == 1
        filed.append(items[0].target_agent)
        # the tick files the task, dispatches it and it finishes
        world.execute(
            "INSERT INTO agent_tasks (agent, task_category, context_hash, status, completed_at) "
            "VALUES (%s, 'agent_initiative', %s, 'completed', NOW())", (filed[-1], uuid.uuid4().hex))
    assert filed == rotation, "every agent once, in rotation order"
    # ...and then nothing, tick after tick. (An idle tick used to file one every time.)
    assert detect() == [] and detect() == []

    # A week later the agent that has waited longest is due again, and only that one.
    world.execute(
        "UPDATE agent_tasks SET created_at = created_at - make_interval(days => %s) WHERE agent = %s",
        (config.INITIATIVE_INTERVAL_DAYS, "security"))
    world.execute(
        "UPDATE agent_tasks SET created_at = created_at - make_interval(days => %s) WHERE agent = %s",
        (config.INITIATIVE_INTERVAL_DAYS + 2, "architect"))
    assert [item.target_agent for item in detect()] == ["architect"]


# ---------------------------------------------------------------------------
# The cost analyst's daily report, against real ledger rows
# ---------------------------------------------------------------------------

def test_the_daily_cost_report_carries_the_ledger_the_budget_runs_on(world, monkeypatch):
    """The report used to look for a connection in its task context that
    nothing put there, and so always fell back to a query of its own: builds
    by started_at (a requeued build has none), and no agent costs at all."""
    import psycopg2

    from coordinator.actuators import Actuators
    from coordinator.agents.cost_analyst import CostAnalyst
    from coordinator.state import WorkItem, WorldState

    monkeypatch.setenv("COORDINATOR_DAILY_BUDGET", "20")
    world.execute("UPDATE builds SET total_api_cost_usd = 1.25 WHERE app_name IN ('sample-app-0', 'sample-app-1')")
    world.execute("UPDATE builds SET status = 'queued', started_at = NULL WHERE app_name = 'sample-app-1'")
    world.execute(
        "INSERT INTO agent_tasks (agent, task_category, context_hash, status, actual_cost_usd, completed_at) "
        "VALUES ('infra_engineer', 'app_down', %s, 'completed', 0.36, NOW())", (uuid.uuid4().hex,))

    conn = psycopg2.connect(world.url)
    try:
        ledger = spend_tracker.get_daily_spend(conn)
    finally:
        conn.close()
    assert ledger["total_tracked_usd"] == pytest.approx(2.86)

    actuators = Actuators("cost_analyst", "task-report", dry_run=True)
    task = WorkItem(id="task-report", category="cost_report", target_agent="cost_analyst",
                    context={"date": "2026-04-12"})
    result = CostAnalyst().execute(task, WorldState(), HAIKU, actuators)

    assert result.success, result.output
    assert result.output["build_pipeline_usd"] == pytest.approx(ledger["build_pipeline_usd"])
    assert result.output["agent_tasks_usd"] == pytest.approx(0.36)
    assert result.output["total_tracked_usd"] == pytest.approx(ledger["total_tracked_usd"])
    assert result.output["builds_today"] == ledger["builds_today"]
    assert result.output["spend_source"] == "internal_tracking"
    assert result.output["budget_remaining_usd"] == pytest.approx(20 - 2.86)
    sent = [a.details for a in actuators.actions if a.action_type == "email_send"]
    assert sent and sent[0]["subject"] == "[CAS] Daily Cost Report - $2.86 spent"
