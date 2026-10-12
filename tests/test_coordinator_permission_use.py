"""The permission table against the code that uses it, in both directions.

coordinator/actuators.py: AGENT_PERMISSIONS says what each agent (and the
failure triage step) may do. A grant nothing asks for is a permission on
paper, and a call an agent has no grant for is dead code behind an
`except PermissionError`. Both existed: three agents held a commit
permission and one held railway_deploy without ever running a playbook, the
backend engineer held a build_approve permission for a handler no trigger
reached, and the architect composed four alert emails it was not permitted to
send.

  * For every (principal, permission) pair in the table there is a scenario
    below that runs the principal's real handler, through execute() and for a
    category a work-detection trigger routes to it (or, for failure triage,
    the trigger itself). Actuators._check_permission is watched while it
    runs, and the pair has to be asked for and granted. A grant with no
    scenario fails; there is no list of exceptions.
  * No scenario may ask for a permission the caller does not hold, and a scan
    of the agent modules finds every actuator call whose permission the
    calling agent lacks.

Nothing leaves the process: there is no DATABASE_URL, the mail call behind
send_alert and the failure KB file are stand-ins, and the root conftest blocks
the network. Reads (Actuators.query_readonly, which checks no permission) are
answered by canned rows.
"""

from __future__ import annotations

import ast
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _extra in (str(ROOT), str(ROOT / "queue")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

from coordinator import tick, work_detection
from coordinator.actuators import (
    AGENT_PERMISSIONS,
    NON_AGENT_PRINCIPALS,
    PLAYBOOK_PERMISSION,
    Actuators,
)
from coordinator.agents.architect import Architect
from coordinator.agents.backend_engineer import BackendEngineer
from coordinator.agents.cost_analyst import CostAnalyst
from coordinator.agents.customer_service import CustomerService
from coordinator.agents.documentation import DocumentationEngineer
from coordinator.agents.financial_manager import FinancialManager
from coordinator.agents.infra_engineer import InfraEngineer
from coordinator.agents.qa_engineer import QAEngineer
from coordinator.agents.reconciliation import ReconciliationAgent
from coordinator.agents.security_engineer import SecurityEngineer
from coordinator.budget import ModelCallMeter, TickBudget
from coordinator.state import WorkItem, WorldState
from tests.test_coordinator_routing import _emitted_pairs

AGENTS = {cls.name: cls for cls in (
    Architect, BackendEngineer, CostAnalyst, CustomerService, DocumentationEngineer,
    FinancialManager, InfraEngineer, QAEngineer, ReconciliationAgent, SecurityEngineer,
)}
EMITTED = _emitted_pairs()
ALL_GRANTS = sorted((principal, permission)
                    for principal, permissions in AGENT_PERMISSIONS.items()
                    for permission in permissions)


# ---------------------------------------------------------------------------
# Watching the gate
# ---------------------------------------------------------------------------

@contextmanager
def _watching_permission_checks():
    """Every Actuators._check_permission call made while the block runs, as
    (agent, permission) in `granted` or `denied`. The real check still decides."""
    seen = SimpleNamespace(granted=set(), denied=set())
    real = Actuators._check_permission

    def watched(self, action_type):
        try:
            real(self, action_type)
        except PermissionError:
            seen.denied.add((self.agent, action_type))
            raise
        seen.granted.add((self.agent, action_type))

    with patch.object(Actuators, "_check_permission", watched):
        yield seen


@pytest.fixture(autouse=True)
def _nothing_leaves_the_process(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("COORDINATOR_ANALYSIS_ONLY", raising=False)
    # The mail call behind Actuators.send_alert.
    monkeypatch.setattr("ops.alerter.send_admin_alert", lambda *_a, **_k: True)
    # The failure KB the kb_record methods write: a scratch file, with two
    # entries for the infra engineer's failure-KB sync to consider.
    import cli.failure_kb as failure_kb

    kb_file = tmp_path / "failure_kb.json"
    kb_file.write_text(json.dumps({
        signature: {"signature": signature, "success_count": 4, "failure_count": 0,
                    "fix_category": "routing", "fix_description": "Fixed something."}
        for signature in ("sig-a", "sig-b")
    }))
    monkeypatch.setattr(failure_kb, "KB_PATH", kb_file)
    monkeypatch.setattr(failure_kb, "_get_kb_path", lambda: kb_file)


def _allowance(calls: int = 10) -> ModelCallMeter:
    return ModelCallMeter(TickBudget(max_api_calls=calls, max_cost_usd=calls * 0.05,
                                     allowed_models=["claude-haiku-4-5"]))


def _run(agent_name: str, category: str, context: dict | None = None, *, reads=None,
         live: bool = False, model_calls=None, state: WorldState | None = None):
    """Run one work item the way the tick does: the agent's real execute()
    with a real Actuators, then the tick's own two steps that act under the
    agent's name: the approval request for a result that asks for escalation,
    and the findings of a successful result. `reads` answers query_readonly:
    a callable taking the SQL, or the rows every query returns."""
    assert (category, agent_name) in EMITTED, f"no trigger routes {category!r} to {agent_name}"
    agent = AGENTS[agent_name]()
    actuators = Actuators(agent_name, "permission-use", dry_run=not live, model_calls=model_calls)
    if reads is not None:
        answer = reads if callable(reads) else (lambda _sql: reads)
        actuators.query_readonly = MagicMock(side_effect=lambda sql, params=(): answer(sql))
    task = WorkItem(id="permission-use", category=category, target_agent=agent_name,
                    description="permission use", context=context or {})
    result = agent.execute(task, state or WorldState(), "claude-haiku-4-5", actuators)
    tick._check_escalation(task, result, agent_name, dry_run=not live)
    if result.success:
        tick._surface_initiative_findings(task, result, dry_run=not live)
    return result


# ---------------------------------------------------------------------------
# One scenario per grant
# ---------------------------------------------------------------------------

SCENARIOS: dict = {}


def exercises(principal: str, *permissions: str):
    def register(scenario):
        for permission in permissions:
            assert (principal, permission) not in SCENARIOS, (principal, permission)
            SCENARIOS[(principal, permission)] = scenario
        return scenario
    return register


def _playbook(category: str, *, live: bool):
    """The infra engineer's build-failure handler with the error classified as
    `category` and a stand-in for the playbook's fixer."""
    fixed = SimpleNamespace(success=True, description="Applied the fix", requires_redeploy=False)
    with patch("ops.scanner.classify_error", return_value=category), \
            patch("ops.playbook.match_playbook", return_value=MagicMock()), \
            patch("ops.playbook.load_fixer", return_value=lambda _context: fixed), \
            patch("coordinator.actuators._playbook_breaker_allows", return_value=True), \
            patch("coordinator.actuators._audit_live_playbook"):
        return _run("infra_engineer", "build_failure",
                    {"build_id": "build-1", "error_message": "the build failed"}, live=live)


@exercises("infra_engineer", "railway_env")
def infra_runs_the_playbook_that_sets_a_variable(tmp_path, monkeypatch):
    assert PLAYBOOK_PERMISSION["env_config"] == "railway_env"
    _playbook("env_config", live=False)


@exercises("infra_engineer", "railway_deploy")
def infra_runs_the_playbook_that_redeploys(tmp_path, monkeypatch):
    assert PLAYBOOK_PERMISSION["railway_crash"] == "railway_deploy"
    _playbook("railway_crash", live=False)


@exercises("infra_engineer", "kb_record")
def infra_records_a_fix_that_was_applied(tmp_path, monkeypatch):
    result = _playbook("railway_crash", live=True)
    assert result.success


@exercises("infra_engineer", "email_send")
def infra_reports_an_app_that_is_down(tmp_path, monkeypatch):
    _run("infra_engineer", "app_down", {"app_name": "sample-app", "consecutive_failures": 3})


@exercises("infra_engineer", "finding_write", "approval_request")
def infra_files_a_failure_it_has_no_playbook_for(tmp_path, monkeypatch):
    with patch("ops.scanner.classify_error", return_value="something_new"), \
            patch("ops.playbook.match_playbook", return_value=None):
        result = _run("infra_engineer", "build_failure", {"build_id": "build-1", "error_message": "new failure"})
    assert result.escalate, "the handler asks for an approval request; the tick files it under this agent's name"


@exercises("infra_engineer", "model_call")
def infra_asks_before_each_failure_kb_draft(tmp_path, monkeypatch):
    result = _run("infra_engineer", "agent_initiative", {"agent": "infra_engineer"})
    assert result.output["sync"]["considered"] == 2


@exercises("security", "email_send", "finding_write", "approval_request")
def security_audits_a_deployed_build(tmp_path, monkeypatch):
    build = {
        "live_url": "http://insecure.example",
        "spec": {"pages": [], "data_models": [
            {"name": "A", "fields": [{"name": "password", "type": "str"}]},
            {"name": "B", "fields": [{"name": "secret_key", "type": "str"}]},
        ]},
        "qa_results": [{"passed": False, "name": "xss check"}],
        "tier": "build",
    }
    result = _run("security", "post_deploy_audit", {"build_id": "build-1", "app_name": "sample-app"}, reads=[build])
    assert result.escalate, "more than two high findings ask for an approval request"


@exercises("backend_engineer", "build_queue")
def backend_asks_to_rebuild_a_failed_build(tmp_path, monkeypatch):
    _run("backend_engineer", "build_failure", {"build_id": "build-1", "error_message": "the build failed"})


@exercises("backend_engineer", "email_send", "finding_write")
def backend_reviews_recent_builds(tmp_path, monkeypatch):
    def reads(sql):
        if "GROUP BY error_message" in sql:
            return [{"error_message": "npm install failed", "cnt": 4}]
        if "qa_status = 'warned'" in sql:
            return [{"cnt": 2}]
        return [{"avg_retries": 0.9}]

    result = _run("backend_engineer", "agent_initiative", {"title": "Weekly review"}, reads=reads)
    assert result.output["findings"]


@exercises("financial", "email_send")
def financial_sends_the_budget_alert(tmp_path, monkeypatch):
    _run("financial", "budget_alert", {"remaining_usd": 3.0, "cost_today_usd": 17.0})


@exercises("financial", "approval_request")
def financial_escalates_a_cost_summary_it_could_not_produce(tmp_path, monkeypatch):
    def reads(_sql):
        raise RuntimeError("statement timeout")

    result = _run("financial", "agent_initiative", reads=reads)
    assert result.escalate and not result.success


@exercises("customer_service", "model_call", "kb_record")
def customer_service_drafts_and_stores_an_answer(tmp_path, monkeypatch):
    question = "How do I export my data?"

    def reads(sql):
        if "FROM beta_feedback" in sql:
            return [{"message": question, "category": "question", "page_url": "/dashboard",
                     "email": "customer@example.com", "name": "Customer"}]
        return []   # no support KB match: a gap

    # The model call itself is replaced; whether it may be made is not.
    with patch.object(CustomerService, "_draft_kb_answer",
                      return_value="You can export your data from the dashboard as JSON."):
        _run("customer_service", "customer_feedback",
             {"feedback_id": "fb-1", "feedback_category": "question", "message_preview": question},
             reads=reads, live=True, model_calls=_allowance())


@exercises("customer_service", "approval_request")
def customer_service_escalates_a_high_severity_bug(tmp_path, monkeypatch):
    def reads(sql):
        if "FROM beta_feedback" in sql:
            return [{"message": "The page crashes and my work is lost", "category": "bug",
                     "page_url": "/edit", "email": "customer@example.com", "name": "Customer"}]
        return []

    result = _run("customer_service", "customer_feedback",
                  {"feedback_id": "fb-2", "feedback_category": "bug", "message_preview": "crashes"}, reads=reads)
    assert result.escalate


@exercises("customer_service", "email_send", "finding_write")
def customer_service_reviews_a_week_of_feedback(tmp_path, monkeypatch):
    def reads(sql):
        if "FROM beta_feedback" in sql:
            return [{"category": "bug", "message": "The page crashes when I save", "page_url": "/edit"}]
        return []

    result = _run("customer_service", "agent_initiative", {"agent": "customer_service"}, reads=reads)
    assert result.output["findings"]


@exercises("documentation", "email_send", "finding_write")
def documentation_reports_drift(tmp_path, monkeypatch):
    # The real check, on a checkout whose CLAUDE.md lists tables and misses one.
    (tmp_path / "web" / "src" / "db").mkdir(parents=True)
    (tmp_path / "web" / "src" / "db" / "schema.sql").write_text(
        "CREATE TABLE IF NOT EXISTS builds (id int);\nCREATE TABLE IF NOT EXISTS widgets (id int);\n")
    (tmp_path / "CLAUDE.md").write_text("# Notes\n\nTables: builds.\n")
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    result = _run("documentation", "agent_initiative")
    assert result.output["issues"] == ["Table 'widgets' in schema.sql but not in CLAUDE.md"]


@exercises("cost_analyst", "email_send")
def cost_analyst_sends_the_daily_report(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    result = _run("cost_analyst", "cost_report", {"date": "2026-01-15"},
                  reads=[{"usd": 6.0, "n": 5}],
                  state=WorldState(api_cost_today_usd=8.0, daily_budget_remaining_usd=12.0))
    assert result.success and result.output["total_tracked_usd"] == 12.0


@exercises("architect", "finding_write")
def architect_review_findings_are_filed_by_the_tick(tmp_path, monkeypatch):
    builds = [{"tier": "build", "qa_status": "warned", "retry_count": 3, "total_api_cost_usd": 5.0}] * 4
    result = _run("architect", "agent_initiative", reads=builds)
    assert len(result.output["findings"]) == 3


@exercises("reconciliation", "email_send", "approval_request")
def reconciliation_reports_a_failed_build_that_is_running(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    candidate = dict(zip(ReconciliationAgent._CANDIDATE_COLUMNS, (
        "build-1", "sample-app", datetime(2026, 1, 15, 12, tzinfo=timezone.utc),
        datetime(2026, 1, 15, 13, tzinfo=timezone.utc), "supervisor timeout", None)))

    def reads(sql):
        return [] if "railway_project_id FROM builds" in sql else [candidate]

    with patch("coordinator.agents.reconciliation.probe_for_zombie_deploy",
               return_value={"live_url": "https://sample-app.example", "project_id": "project-1"}):
        result = _run("reconciliation", "reconciliation_sweep", reads=reads, live=True)
    assert len(result.output["recovered"]) == 1


@exercises("failure_triage", "model_call", "build_annotate", "kb_record")
def failure_triage_categorizes_a_build_the_rules_could_not(tmp_path, monkeypatch):
    """Not an agent: the step inside the failed-builds trigger."""
    import anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-" + "key")
    reply = SimpleNamespace(content=[SimpleNamespace(type="text", text="infra")],
                            usage=SimpleNamespace(input_tokens=100, output_tokens=2))
    client = MagicMock()
    client.messages.create.return_value = reply
    monkeypatch.setattr(anthropic, "Anthropic", lambda **_kwargs: client)

    triage = Actuators("failure_triage", "work-detection", dry_run=False, model_calls=_allowance())
    state = WorldState(failed_builds_24h=[{
        "id": "build-1", "app_name": "sample-app", "error_category": None,
        "error_message": "something nobody has seen before went wrong",
    }])
    with patch("ops.scanner.classify_error", return_value="unknown"):
        items = work_detection._check_failed_builds(MagicMock(), state, None, triage)
    assert [item.context["error_category"] for item in items] == ["infra"]


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------

def test_the_agents_here_are_the_agents_in_the_table():
    assert set(AGENTS) == set(AGENT_PERMISSIONS) - NON_AGENT_PRINCIPALS


def test_every_grant_has_a_scenario_and_every_scenario_a_grant():
    """No exception list: a grant is either asked for by a real handler below
    or it is not in the table."""
    assert sorted(SCENARIOS) == ALL_GRANTS


@pytest.mark.parametrize("principal, permission", ALL_GRANTS)
def test_the_grant_is_asked_for_by_a_real_handler(principal, permission, tmp_path, monkeypatch):
    scenario = SCENARIOS.get((principal, permission))
    assert scenario is not None, (
        f"{principal} holds {permission} and no handler scenario asks for it: "
        "remove the grant, or add the scenario that reaches the call")
    with _watching_permission_checks() as seen:
        scenario(tmp_path, monkeypatch)
    assert (principal, permission) in seen.granted, (
        f"{scenario.__name__} never asked the gate for {permission}; it asked for {sorted(seen.granted)}")
    assert seen.denied == set(), f"asked for without holding it: {sorted(seen.denied)}"


def test_an_agent_with_no_grants_runs_its_work_without_asking_for_one():
    """qa_test holds nothing: its handler only reads."""
    assert AGENT_PERMISSIONS["qa_test"] == set()
    with _watching_permission_checks() as seen:
        result = _run("qa_test", "agent_initiative", reads=[])
    assert result.success
    assert seen.granted == set() and seen.denied == set()


def test_the_watch_reports_a_call_the_agent_has_no_grant_for():
    """What the architect's alert emails looked like to the gate."""
    with _watching_permission_checks() as seen:
        with pytest.raises(PermissionError):
            Actuators("architect", "permission-use", dry_run=True).send_alert("subject", "body")
    assert seen.denied == {("architect", "email_send")}


# --- The same question asked of the source ---------------------------------

def _permissions_by_method() -> dict:
    """Actuators method name -> the permissions it checks."""
    source = (ROOT / "coordinator" / "actuators.py").read_text(encoding="utf-8")
    actuators_class = next(node for node in ast.parse(source).body
                           if isinstance(node, ast.ClassDef) and node.name == "Actuators")
    by_method = {}
    for method in actuators_class.body:
        if not isinstance(method, ast.FunctionDef) or method.name.startswith("_"):
            continue
        checked = {call.args[0].value for call in ast.walk(method)
                   if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                   and call.func.attr == "_check_permission" and call.args
                   and isinstance(call.args[0], ast.Constant)}
        if method.name == "run_playbook":
            checked = set(PLAYBOOK_PERMISSION.values())
        by_method[method.name] = checked
    return by_method


def test_no_agent_module_calls_an_actuator_method_it_holds_no_permission_for():
    """A call like that can only raise, and every one of them sat inside an
    `except PermissionError: pass`."""
    by_method = _permissions_by_method()
    assert by_method["send_alert"] == {"email_send"} and by_method["query_readonly"] == set()
    not_granted = []
    for agent_name, agent_cls in sorted(AGENTS.items()):
        module_source = Path(sys.modules[agent_cls.__module__].__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(module_source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in by_method \
                    and isinstance(node.func.value, ast.Name) and "actuator" in node.func.value.id:
                missing = by_method[node.func.attr] - AGENT_PERMISSIONS[agent_name]
                if missing:
                    not_granted.append((agent_name, node.func.attr, node.lineno, sorted(missing)))
    assert not_granted == []
