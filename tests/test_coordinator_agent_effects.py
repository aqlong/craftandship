"""What a coordinator agent reports about the side effects it asked for.

Nineteen handlers in coordinator/agents/ used to catch PermissionError and do
nothing, and the result then said what the agent had meant to do: "alert_sent"
for an email the permission table refused, the analysis-only mode switched off
or the mailer dropped, and for a dry run that sent nothing. These tests drive
the real agents with a real Actuators (the permission table, the dry-run flag
and the mailer's answer are the real ones; only the mail call and the failure
KB file are stand-ins) and then the real tick on a real PostgreSQL, so both
sides of the seam are exercised: what an agent says it did, and what the tick
writes into agent_tasks.

The outcomes are the five in coordinator/agents/base.py: sent, recorded (a
finding or KB entry), dry_run, refused (by the permission table or the closed
task) and failed.
"""

import ast
import json
import sys
import threading
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coordinator import tick
from coordinator.actuators import Actuators
from coordinator.agents.backend_engineer import BackendEngineer
from coordinator.agents.base import Effects
from coordinator.agents.financial_manager import FinancialManager
from coordinator.agents.infra_engineer import InfraEngineer
from coordinator.state import WorkItem, WorldState

SCHEMA_SQL = ROOT / "web" / "src" / "db" / "schema.sql"
AGENTS_DIR = ROOT / "coordinator" / "agents"


@pytest.fixture(scope="module")
def effects_db_url(scratch_pg_url):
    """A throwaway database holding the production schema."""
    import psycopg2
    from psycopg2 import extensions

    params = extensions.parse_dsn(scratch_pg_url)
    name = "cas_test_effects_" + uuid.uuid4().hex[:10]
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


@pytest.fixture()
def no_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("COORDINATOR_ANALYSIS_ONLY", raising=False)


@pytest.fixture()
def database(effects_db_url, monkeypatch):
    import psycopg2

    monkeypatch.setenv("DATABASE_URL", effects_db_url)
    monkeypatch.delenv("COORDINATOR_ANALYSIS_ONLY", raising=False)
    connection = psycopg2.connect(effects_db_url)
    with connection.cursor() as cur:
        cur.execute("TRUNCATE coordinator_ticks, agent_tasks, coordinator_findings, approval_requests CASCADE")
    connection.commit()
    yield connection
    connection.close()


def _task(category, context):
    return WorkItem(id=str(uuid.uuid4()), category=category, target_agent="x", context=context)


def _mailer(result):
    """The mail call behind Actuators.send_alert, answering `result`."""
    return patch("ops.alerter.send_admin_alert", return_value=result)


# ---------------------------------------------------------------------------
# One alert, every way it can end
# ---------------------------------------------------------------------------

ALERT_CASES = [
    # (principal, dry_run, what the mailer answers, outcome)
    ("infra_engineer", False, True, "sent"),
    ("infra_engineer", False, False, "failed"),
    ("infra_engineer", True, True, "dry_run"),      # the actuator answers True and sends nothing
    ("qa_test", False, True, "refused"),            # holds no email_send
    ("qa_test", True, True, "refused"),
]


@pytest.mark.parametrize("principal, dry_run, mailer, outcome", ALERT_CASES)
def test_an_alert_reports_how_it_ended(no_database, principal, dry_run, mailer, outcome):
    effects = Effects()
    with _mailer(mailer) as mail:
        got = effects.alert(Actuators(principal, "t", dry_run=dry_run), "subject", "body")
    assert got == outcome
    assert effects.entries[0]["outcome"] == outcome and effects.entries[0]["effect"] == "alert"
    assert mail.called is (outcome in ("sent", "failed")), "the mailer was called when it must not be, or not when it must"


def test_an_alert_of_a_closed_task_is_refused(no_database):
    closed = threading.Event()
    closed.set()
    effects = Effects()
    assert effects.alert(Actuators("infra_engineer", "t", dry_run=False, abandoned=closed), "s", "b") == "refused"
    assert "closed" in effects.entries[0]["detail"]


def test_an_alert_whose_call_raises_is_a_failure_and_does_not_end_the_task(no_database):
    effects = Effects()
    actuators = Actuators("infra_engineer", "t", dry_run=False)
    with patch.object(Actuators, "send_alert", side_effect=RuntimeError("mailer exploded")):
        assert effects.alert(actuators, "s", "b") == "failed"
    assert "RuntimeError" in effects.entries[0]["detail"]


# ---------------------------------------------------------------------------
# The agents' own results
# ---------------------------------------------------------------------------

APP_DOWN = ("app_down", {"app_name": "demo", "consecutive_failures": 3})
BACKUP_OVERDUE = ("backup_overdue", {"last_backup_age_hours": 60})


@pytest.mark.parametrize("category, context", [APP_DOWN, BACKUP_OVERDUE])
@pytest.mark.parametrize("principal, dry_run, mailer, outcome, action_word, succeeded", [
    ("infra_engineer", False, True, "sent", "alert_sent", True),
    ("infra_engineer", False, False, "failed", "alert_failed", False),
    ("infra_engineer", True, True, "dry_run", "alert_dry_run", True),
    ("qa_test", False, True, "refused", "alert_refused", False),
])
def test_the_infra_engineer_says_what_became_of_its_alert(no_database, category, context, principal, dry_run,
                                                           mailer, outcome, action_word, succeeded):
    actuators = Actuators(principal, "t", dry_run=dry_run)
    with _mailer(mailer):
        result = InfraEngineer().execute(_task(category, context), WorldState(), "m", actuators)
    assert result.output["action"] == action_word
    assert result.success is succeeded
    assert [e["outcome"] for e in result.effects] == [outcome]
    if outcome in ("refused", "failed"):
        assert any(outcome in line for line in result.learnings), "the learnings do not say the alert did not go out"


@pytest.mark.parametrize("principal, dry_run, mailer, outcome, succeeded", [
    ("financial", False, True, "sent", True),
    ("financial", False, False, "failed", False),
    ("financial", True, True, "dry_run", True),
    ("qa_test", False, True, "refused", False),
])
def test_the_financial_manager_says_what_became_of_the_budget_alert(no_database, principal, dry_run, mailer,
                                                                    outcome, succeeded):
    actuators = Actuators(principal, "t", dry_run=dry_run)
    task = _task("budget_alert", {"remaining_usd": 3.0, "cost_today_usd": 17.0})
    with _mailer(mailer):
        result = FinancialManager().execute(task, WorldState(), "m", actuators)
    assert result.output["action"] == f"budget_alert_{outcome}"
    assert result.success is succeeded
    assert [e["outcome"] for e in result.effects] == [outcome]


def test_a_refused_rebuild_is_not_reported_as_a_failed_one_and_the_finding_is_recorded(database, monkeypatch):
    """COORDINATOR_ANALYSIS_ONLY refuses the rebuild; the agent files a finding
    instead, and says so. Both effects are in the result, and the finding is
    really in coordinator_findings."""
    monkeypatch.setenv("COORDINATOR_ANALYSIS_ONLY", "true")
    actuators = Actuators("backend_engineer", "t", dry_run=False)
    task = _task("build_failure", {"build_id": "abcd1234-0000-0000-0000-000000000000", "error_message": "boom"})
    result = BackendEngineer().execute(task, WorldState(), "m", actuators)
    assert result.output["action"] == "finding_flagged"
    assert [(e["effect"], e["outcome"]) for e in result.effects] == [("rebuild", "refused"), ("finding", "recorded")]
    with database.cursor() as cur:
        cur.execute("SELECT finding_key FROM coordinator_findings")
        assert cur.fetchall() == [("build-fail-abcd1234",)]


def test_a_rebuild_refused_with_no_finding_possible_says_refused(no_database, monkeypatch):
    monkeypatch.setenv("COORDINATOR_ANALYSIS_ONLY", "true")
    actuators = Actuators("backend_engineer", "t", dry_run=False)   # no DATABASE_URL: the finding cannot be filed
    task = _task("build_failure", {"build_id": "abcd1234-0000-0000-0000-000000000000", "error_message": "boom"})
    result = BackendEngineer().execute(task, WorldState(), "m", actuators)
    assert result.output["action"] == "rebuild_refused"
    assert result.success is False
    assert [(e["effect"], e["outcome"]) for e in result.effects] == [("rebuild", "refused"), ("finding", "failed")]


# ---------------------------------------------------------------------------
# The tick writes it down
# ---------------------------------------------------------------------------

def _dispatch(connection, agent_type, category, context, dry_run, monkeypatch=None):
    """tick._dispatch_agent on a real agent_tasks row; returns (result, stored row)."""
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (agent, task_category, task_description, context, context_hash, status) "
            "VALUES (%s, %s, 'test', %s, %s, 'pending') RETURNING id",
            (agent_type, category, json.dumps(context), uuid.uuid4().hex[:32]),
        )
        task_id = str(cur.fetchone()[0])
    connection.commit()
    saved = dict(tick._agent_registry)
    tick._agent_registry.clear()
    tick._agent_registry[agent_type] = {"infra_engineer": InfraEngineer, "financial": FinancialManager}[agent_type]
    try:
        task = WorkItem(id=task_id, category=category, target_agent=agent_type, context=context)
        result = tick._dispatch_agent(connection, task, agent_type, "claude-haiku-4-5", WorldState(), dry_run, None)
    finally:
        tick._agent_registry.clear()
        tick._agent_registry.update(saved)
    with connection.cursor() as cur:
        cur.execute("SELECT status, result FROM agent_tasks WHERE id = %s", (task_id,))
        status, stored = cur.fetchone()
    connection.commit()
    return result, status, stored


def test_the_tick_stores_each_effect_and_marks_a_task_whose_alert_failed_as_failed(database, monkeypatch):
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)   # live, with nobody to send to: the mailer answers False
    result, status, stored = _dispatch(database, "infra_engineer", *APP_DOWN, dry_run=False)
    assert status == "failed"
    assert stored["action"] == "alert_failed"
    assert [(e["effect"], e["outcome"]) for e in stored["effects"]] == [("alert", "failed")]


def test_the_tick_stores_a_dry_run_alert_as_a_dry_run(database):
    result, status, stored = _dispatch(database, "infra_engineer", *APP_DOWN, dry_run=True)
    assert status == "completed"
    assert stored["action"] == "alert_dry_run" and stored["effects"][0]["outcome"] == "dry_run"
    assert stored["dry_run"] is True


def test_the_tick_stores_a_sent_alert(database, monkeypatch):
    with _mailer(True):
        result, status, stored = _dispatch(database, "financial", "budget_alert",
                                           {"remaining_usd": 3.0, "cost_today_usd": 17.0}, dry_run=False)
    assert status == "completed"
    assert stored["action"] == "budget_alert_sent" and stored["effects"][0]["outcome"] == "sent"


# ---------------------------------------------------------------------------
# Nothing swallows a refusal any more
# ---------------------------------------------------------------------------

def _permission_error_handlers():
    """(file, line, handler) for every `except PermissionError` in coordinator/agents/."""
    for path in sorted(AGENTS_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ExceptHandler) and node.type is not None:
                caught = {n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)}
                if "PermissionError" in caught:
                    yield path.name, node.lineno, node


def test_no_handler_in_the_agents_swallows_a_permission_error_silently():
    """A tripwire on source text (the behavior is in the tests above): a handler
    for PermissionError whose whole body is `pass`, or a bare constant, is how
    "refused" became "done". A handler that logs and returns False (the two
    in customer_service.py, which feed a count that is reported) is allowed."""
    silent = [
        f"{name}:{line}" for name, line, handler in _permission_error_handlers()
        if all(isinstance(stmt, ast.Pass) or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
               for stmt in handler.body)
    ]
    assert silent == [], f"silent PermissionError handlers: {silent}"
