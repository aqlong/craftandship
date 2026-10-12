"""The coordinator's actuator gates, checked for every agent and every action.

tests/test_coordinator_actuators.py spot-checks individual denials. This file
closes the gaps those spot checks leave:

  * a permission matrix: for each side-effecting Actuators method and each
    principal in the permission table, the call is refused exactly when
    AGENT_PERMISSIONS does not grant the permission, and in
    COORDINATOR_ANALYSIS_ONLY mode every write action is refused for everyone.
    A new method that forgets its gate, or a permission edit that widens an
    agent, fails here;
  * a dry-run matrix: under dry run (the default) no gated method opens a
    database connection, makes an HTTP request, calls ops/fixer.py, sends an
    alert or writes a knowledge base, with or without analysis-only mode. The
    one documented exemption (finding_add) is pinned as exactly that;
  * run_playbook, the one method that downgrades to a dry run instead of
    raising: the fixer runs live only with the permission, outside dry run and
    analysis-only mode, and inside the circuit breaker;
  * two scans of coordinator/ for side effects made anywhere else, the check
    for an effect that goes around the Actuators layer altogether:
      - an import scan, which is the one that can be relied on: outside
        actuators.py a module may import only a short allowlist of
        standard-library modules and other coordinator modules. A network,
        subprocess, database or model library, or first-party code outside
        coordinator/, is a finding unless listed with a reason, whatever
        name it is imported or called under. `os`, dynamic code and file
        opens need no import and are checked by use;
      - a call-pattern scan, which names the function a known side effect
        sits in (a commit, an HTTP call, a model client, a subprocess, a file
        write). It matches call names, so it is a tripwire and nothing more.
    Neither establishes that a module writes nothing through a database
    connection it was handed; see the note above SAFE_IMPORTS;
  * query_readonly, the one door to the database for agents that may not
    write: a courtesy refusal of the plainest writes, and the real defense,
    one statement in a read-only transaction (a write hidden in a CTE or
    behind a comment is refused by Postgres itself; more than one statement
    is refused before anything is sent). The statement-count and
    read-only-transaction cases are in tests/test_coordinator_readonly_query.py.
"""

import ast
import functools
import importlib.util
import inspect
import json
import re
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coordinator.actuators import (
    AGENT_PERMISSIONS,
    NON_AGENT_PRINCIPALS,
    PLAYBOOK_PERMISSION,
    Actuators,
    _ANALYSIS_ONLY_PERMISSIONS,
)

# method name -> (permission it requires, call that supplies valid arguments)
GATED = {
    "send_alert": ("email_send", lambda a: a.send_alert("subject", "body")),
    "queue_rebuild": ("build_queue", lambda a: a.queue_rebuild("parent-1", "req")),
    "queue_new_build": ("build_queue", lambda a: a.queue_new_build("cust-1", "app", {})),
    "set_build_error_category": ("build_annotate", lambda a: a.set_build_error_category("build-1", "infra")),
    "authorize_model_call": ("model_call", lambda a: a.authorize_model_call("a test")),
    "support_kb_insert": ("kb_record", lambda a: a.support_kb_insert("question?", "answer.")),
    "record_fix": ("kb_record", lambda a: a.record_fix("err", "fix", [], "code_error", True)),
    "record_failure_category": ("kb_record", lambda a: a.record_failure_category("some error", "infra")),
    "finding_add": ("finding_write", lambda a: a.finding_add("key-1", "title", "desc")),
    "create_approval_request": ("approval_request",
                                lambda a: a.create_approval_request("commit_production", "please")),
}

# The one public method that carries no permission gate: query_readonly cannot
# write. (create_approval_request used to be listed here as well: it checked
# no permission and ignored analysis-only mode.)
UNGATED = {"query_readonly"}

# The one method that answers a missing permission by running dry, not by raising.
DOWNGRADING = {"run_playbook"}

# The gated method whose only effect is an entry in a queue a person reviews; it
# writes under dry run on purpose (see the header of coordinator/actuators.py).
DRY_RUN_EXEMPT = {"finding_add"}

AGENTS = sorted(AGENT_PERMISSIONS) + ["no_such_agent"]


@pytest.fixture(autouse=True)
def _inert_environment(monkeypatch, tmp_path):
    """Nothing a permitted call does may leave the test: no database, no
    GitHub, no Railway."""
    for name in ("DATABASE_URL", "RAILWAY_API_TOKEN", "COORDINATOR_ANALYSIS_ONLY", "RESEND_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RAILWAY_WORKER_SERVICE_ID", "svc-1")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "env-1")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "proj-1")
    with patch("cli.failure_kb.record_fix"), patch("cli.failure_kb._save_kb"):
        yield


def _is_gate_error(exc: PermissionError) -> bool:
    text = str(exc)
    return "not permitted" in text or "COORDINATOR_ANALYSIS_ONLY" in text


def _refused(call) -> bool:
    try:
        call()
    except PermissionError as exc:
        return _is_gate_error(exc)
    return False


def test_every_public_actuator_method_is_in_the_matrix():
    public = {
        name for name, member in inspect.getmembers(Actuators)
        if not name.startswith("_") and inspect.isfunction(member)
    }
    assert public - UNGATED - DOWNGRADING == set(GATED), (
        "a public Actuators method is missing from (or extra in) the permission matrix"
    )


def test_the_matrix_and_the_table_use_the_same_permission_names():
    """Every permission the table grants is checked by some method (a grant
    nothing checks, like the old db_write, is a claim with no gate behind it),
    and every permission a method checks is granted to someone."""
    checked = {perm for perm, _ in GATED.values()} | set(PLAYBOOK_PERMISSION.values())
    granted = set().union(*AGENT_PERMISSIONS.values())
    assert granted <= checked, f"granted but never checked: {granted - checked}"
    assert checked <= granted, f"checked but granted to nobody: {checked - granted}"


def test_non_agent_principals_are_in_the_table_and_cannot_report_or_deploy():
    assert NON_AGENT_PRINCIPALS <= set(AGENT_PERMISSIONS)
    assert AGENT_PERMISSIONS["failure_triage"] == {"model_call", "build_annotate", "kb_record"}


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("method", sorted(GATED))
def test_an_action_is_refused_exactly_when_the_agent_lacks_the_permission(agent, method):
    permission, call = GATED[method]
    actuators = Actuators(agent, "task-matrix", dry_run=True)
    allowed = permission in AGENT_PERMISSIONS.get(agent, set())
    assert _refused(lambda: call(actuators)) is (not allowed), (
        f"{agent}.{method} (needs {permission}): expected {'allowed' if allowed else 'refused'}"
    )


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize(
    "method", sorted(m for m, (perm, _) in GATED.items() if perm not in _ANALYSIS_ONLY_PERMISSIONS)
)
def test_analysis_only_mode_refuses_every_write_action_for_every_agent(agent, method, monkeypatch):
    monkeypatch.setenv("COORDINATOR_ANALYSIS_ONLY", "true")
    _, call = GATED[method]
    assert _refused(lambda: call(Actuators(agent, "task-analysis", dry_run=True))) is True


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize(
    "method", sorted(m for m, (perm, _) in GATED.items() if perm in _ANALYSIS_ONLY_PERMISSIONS)
)
def test_analysis_only_mode_never_widens_an_agents_own_permissions(agent, method, monkeypatch):
    """Analysis-only narrows the allowed set; an agent that lacks a permission
    in AGENT_PERMISSIONS must still be refused for it."""
    monkeypatch.setenv("COORDINATOR_ANALYSIS_ONLY", "true")
    permission, call = GATED[method]
    allowed = permission in AGENT_PERMISSIONS.get(agent, set())
    refused = _refused(lambda: call(Actuators(agent, "task-analysis", dry_run=True)))
    assert refused is (not allowed), (
        f"{agent}.{method} (needs {permission}) in analysis-only mode: "
        f"expected {'allowed' if allowed else 'refused'}"
    )


@pytest.mark.parametrize("method", sorted(GATED))
def test_every_gated_action_is_refused_once_the_task_was_closed_at_its_deadline(method):
    """The tick sets the event when it stops waiting for the agent. The thread
    goes on, so the gate has to say no from then on (the real sequence, with a
    thread and a database, is in tests/test_coordinator_tick_realdb.py)."""
    import threading

    permission, call = GATED[method]
    holder = next(agent for agent, perms in sorted(AGENT_PERMISSIONS.items()) if permission in perms)
    closed = threading.Event()
    actuators = Actuators(holder, "task-deadline", dry_run=True, abandoned=closed)

    assert not _refused(lambda: call(actuators)), "allowed while the task is open"
    recorded_while_open = len(actuators.actions)
    closed.set()
    assert _refused(lambda: call(actuators))
    assert len(actuators.actions) == recorded_while_open, "an action was recorded for a closed task"


def test_no_principal_can_approve_or_promote_a_build():
    """There is no permission that names an approval and no actuator method
    that makes one: the coordinator cannot write a review verdict or the live
    status. (An approve_build method and a build_approve permission existed
    for a handler no trigger reached; all three are gone.)"""
    every_permission = set().union(*AGENT_PERMISSIONS.values())
    assert not {p for p in every_permission if "approve" in p or "promote" in p or "live" in p}
    assert not [name for name in dir(Actuators) if "approve_build" in name or "promote" in name]
    source = (ROOT / "coordinator" / "actuators.py").read_text()
    assert "review_status" not in source and "status = 'live'" not in source


# ---------------------------------------------------------------------------
# Dry run: no gated method has an effect, in either mode
# ---------------------------------------------------------------------------

def _holder_of(permission: str) -> str:
    return sorted(agent for agent, perms in AGENT_PERMISSIONS.items() if permission in perms)[0]


class _EffectTraps:
    """Every way an Actuators method reaches outside the process, patched."""

    def __init__(self, stack):
        self.traps = {
            "database connection": stack.enter_context(patch("psycopg2.connect")),
            "HTTP request": stack.enter_context(patch("coordinator.actuators.bounded_urlopen")),
            "Railway API call": stack.enter_context(patch("ops.fixer._railway_gql")),
            "Railway variable": stack.enter_context(patch("ops.fixer.upsert_env_var")),
            "alert email": stack.enter_context(patch("ops.alerter.send_admin_alert")),
            "failure KB fix": stack.enter_context(patch("cli.failure_kb.record_fix")),
            "failure KB save": stack.enter_context(patch("cli.failure_kb._save_kb")),
        }

    def touched(self) -> list:
        return [name for name, trap in self.traps.items() if trap.called]


@pytest.fixture
def effect_traps(monkeypatch, tmp_path):
    import contextlib
    # A database and the mail and API credentials ARE configured here, so the
    # only thing standing between a call and its effect is the dry-run check.
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/does_not_matter")
    monkeypatch.setenv("RESEND_API_KEY", "dummy-" + "mail-key")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("RAILWAY_API_TOKEN", "dummy-" + "railway-token")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "svc-1")
    with contextlib.ExitStack() as stack:
        yield _EffectTraps(stack)


# (method, analysis_only). In analysis-only mode a method whose permission is
# not an analysis permission is refused outright (the permission matrix above
# covers that), so only the others have a dry-run behavior to check there.
_DRY_RUN_CASES = [
    (method, analysis_only)
    for method in sorted(set(GATED) - DRY_RUN_EXEMPT)
    for analysis_only in (False, True)
    if not analysis_only or GATED[method][0] in _ANALYSIS_ONLY_PERMISSIONS
]


@pytest.mark.parametrize("method, analysis_only", _DRY_RUN_CASES)
def test_dry_run_has_no_effect_outside_the_process(method, analysis_only, effect_traps, monkeypatch):
    permission, call = GATED[method]
    if analysis_only:
        monkeypatch.setenv("COORDINATOR_ANALYSIS_ONLY", "true")
    actuators = Actuators(_holder_of(permission), "task-dry", dry_run=True)
    call(actuators)
    assert effect_traps.touched() == [], (
        f"{method} under dry run (analysis_only={analysis_only}) reached: {effect_traps.touched()}"
    )
    assert actuators.actions and actuators.actions[-1].details.get("dry_run") is True, (
        f"{method} did not record its dry run"
    )


def test_the_same_calls_do_have_effects_when_live(effect_traps):
    """The traps are real: without dry run, these methods reach them. (Guards
    the dry-run matrix above against passing because nothing is wired.)"""
    reached = {}
    for method in ("send_alert", "set_build_error_category", "support_kb_insert", "record_fix",
                   "record_failure_category"):
        permission, call = GATED[method]
        for trap in effect_traps.traps.values():
            trap.reset_mock()
        call(Actuators(_holder_of(permission), "task-live", dry_run=False))
        reached[method] = effect_traps.touched()
    assert reached == {
        "send_alert": ["alert email"],
        "set_build_error_category": ["database connection"],
        "support_kb_insert": ["database connection"],
        "record_fix": ["failure KB fix"],
        "record_failure_category": ["failure KB save"],
    }


def test_the_dry_run_exemption_writes_only_its_review_queue(effect_traps):
    finding = Actuators(_holder_of("finding_write"), "task-dry", dry_run=True)
    finding.finding_add("key-1", "title", "desc")
    assert effect_traps.touched() == ["database connection"]
    sql = effect_traps.traps["database connection"].return_value.cursor.return_value.execute.call_args.args[0]
    assert "INSERT INTO coordinator_findings" in sql


def test_no_actuator_method_commits_redeploys_or_sets_a_variable_itself():
    """A redeploy and a Railway variable are reachable only through
    run_playbook; their limits live in ops/fixer.py (tests/test_ops_fixer.py).
    The direct methods that used to duplicate those limits had no caller, and
    nothing commits at all (no github_write permission, no commit playbook)."""
    assert "github_write" not in set().union(*AGENT_PERMISSIONS.values())
    for gone in ("commit_files", "redeploy_service", "set_env_var", "roadmap_add"):
        assert not hasattr(Actuators, gone), gone
    source = (ROOT / "coordinator" / "actuators.py").read_text()
    assert "ops.fixer import" not in source and "from ops import fixer" not in source


def test_create_approval_request_files_nothing_under_dry_run(effect_traps):
    actuators = Actuators(_holder_of("approval_request"), "task-dry", dry_run=True)
    assert actuators.create_approval_request("commit_production", "please") is None
    assert effect_traps.touched() == []


def test_create_approval_request_is_refused_without_the_permission_and_in_analysis_only_mode(
        effect_traps, monkeypatch):
    """It used to check nothing: any principal could file a request and send
    the escalation email, in COORDINATOR_ANALYSIS_ONLY mode as well. Live
    mode here, so the gate is the only thing between the call and its effect."""
    holder = _holder_of("approval_request")
    lacking = sorted(agent for agent, perms in AGENT_PERMISSIONS.items() if "approval_request" not in perms)
    assert lacking, "some principal must lack the permission for this test to mean anything"
    for agent in lacking:
        with pytest.raises(PermissionError, match="not permitted: approval_request"):
            Actuators(agent, "task-live", dry_run=False).create_approval_request("escalation:x", "please")
    monkeypatch.setenv("COORDINATOR_ANALYSIS_ONLY", "true")
    with pytest.raises(PermissionError, match="COORDINATOR_ANALYSIS_ONLY"):
        Actuators(holder, "task-live", dry_run=False).create_approval_request("escalation:x", "please")
    assert effect_traps.touched() == [], "a refused approval request reached the database or the mail API"

    # The control: the holder, live, outside analysis-only mode, does file it.
    monkeypatch.delenv("COORDINATOR_ANALYSIS_ONLY")
    effect_traps.traps["database connection"].return_value.cursor.return_value.fetchone.return_value = ("id",)
    assert Actuators(holder, "task-live", dry_run=False).create_approval_request("escalation:x", "please")
    assert set(effect_traps.touched()) == {"database connection", "HTTP request"}


def test_approval_request_is_not_an_analysis_only_permission():
    assert "approval_request" not in _ANALYSIS_ONLY_PERMISSIONS
    assert _ANALYSIS_ONLY_PERMISSIONS == {"kb_record", "model_call", "finding_write", "email_send"}


# ---------------------------------------------------------------------------
# run_playbook: live only with the permission, outside dry run, inside the breaker
# ---------------------------------------------------------------------------

PLAYBOOK_CATEGORIES = sorted(PLAYBOOK_PERMISSION)


def _run_playbook(agent, category, *, dry_run, breaker=True):
    seen = {}

    def fixer(context):
        seen.update(context)
        return MagicMock(success=True, description="did it")

    actuators = Actuators(agent, "task-playbook", dry_run=dry_run)
    with patch("ops.guardrails.CircuitBreaker.can_proceed") as can_proceed, \
         patch("ops.audit.log_action") as audit:
        if isinstance(breaker, Exception):
            can_proceed.side_effect = breaker
        else:
            can_proceed.return_value = breaker
        _, ran_live = actuators.run_playbook(category, fixer, {"build_ids": ["b-1"], "category": category})
    return seen, ran_live, audit, actuators


@pytest.mark.parametrize("analysis_only", [False, True])
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("category", PLAYBOOK_CATEGORIES)
@pytest.mark.parametrize("agent", AGENTS)
def test_a_playbook_runs_live_exactly_when_every_gate_allows_it(
        agent, category, dry_run, analysis_only, monkeypatch):
    if analysis_only:
        monkeypatch.setenv("COORDINATOR_ANALYSIS_ONLY", "true")
    permission = PLAYBOOK_PERMISSION[category]
    may_run_live = (
        not dry_run
        and permission in AGENT_PERMISSIONS.get(agent, set())
        and not (analysis_only and permission not in _ANALYSIS_ONLY_PERMISSIONS)
    )
    seen, ran_live, audit, actuators = _run_playbook(agent, category, dry_run=dry_run)
    assert ran_live is may_run_live
    assert seen["dry_run"] is (not may_run_live), "the fixer was told the wrong mode"
    assert audit.called is may_run_live, "only a live run is written to the ops audit log"
    assert actuators.actions[-1].details == {
        "playbook": category, "dry_run": not may_run_live, "success": True}


def test_no_playbook_permission_is_an_analysis_permission():
    assert not set(PLAYBOOK_PERMISSION.values()) & _ANALYSIS_ONLY_PERMISSIONS


def test_every_playbook_has_a_permission_and_nothing_else_is_run():
    """One permission per playbook in the registry, and no default: a
    category the table does not name is refused before its fixer is called.
    (Categories without an entry used to fall back to the commit permission.)"""
    from ops.playbook import PLAYBOOKS
    assert set(PLAYBOOK_PERMISSION) == set(PLAYBOOKS)
    called = []
    actuators = Actuators("infra_engineer", "task-playbook", dry_run=False)
    for category in ("dockerfile", "missing_deps", "version_mismatch", "anything_new"):
        with pytest.raises(ValueError, match="no playbook permission is defined"):
            actuators.run_playbook(category, lambda context: called.append(context), {})
    assert called == [] and actuators.actions == []


@pytest.mark.parametrize("breaker", [False, RuntimeError("audit table unreadable")])
def test_a_tripped_or_unreadable_circuit_breaker_keeps_a_playbook_dry(breaker):
    seen, ran_live, audit, _ = _run_playbook("infra_engineer", "railway_crash", dry_run=False, breaker=breaker)
    assert ran_live is False and seen["dry_run"] is True
    audit.assert_not_called()


def test_a_playbook_action_is_logged_under_the_name_the_review_step_reads():
    from coordinator.tick import ACTIONS_REQUIRING_REVIEW
    _, _, _, variable = _run_playbook("infra_engineer", "env_config", dry_run=False)
    _, _, _, redeploy = _run_playbook("infra_engineer", "railway_crash", dry_run=False)
    assert variable.actions[-1].action_type == "railway_env"
    assert redeploy.actions[-1].action_type == "railway_deploy"
    assert ACTIONS_REQUIRING_REVIEW == {"railway_deploy"}


# ---------------------------------------------------------------------------
# Nothing in coordinator/ has a side effect outside the Actuators layer
# ---------------------------------------------------------------------------

_WRITE_MODES = set("wax+")
_FILE_WRITERS = {"os.replace", "os.rename", "os.remove", "os.unlink", "shutil.rmtree",
                 "shutil.copy", "shutil.copyfile", "shutil.move", "json.dump"}
_REACH_AROUND_MODULES = {"ops.fixer": "ops fixer", "ops.alerter": "ops alerter", "ops.audit": "ops audit log"}
_REACH_AROUND_NAMES = {
    ("cli.failure_kb", "record_fix"): "failure KB write",
    ("cli.failure_kb", "_save_kb"): "failure KB write",
    ("cli.failure_kb", "record_review_issues"): "failure KB write",
    ("coordinator.actuators", "_send_escalation_email"): "escalation email",
    ("ops.playbook", "load_fixer"): "playbook loader",
    ("watchdog_probe", "probe_for_zombie_deploy"): "railway probe",
}


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    parts.append(node.id if isinstance(node, ast.Name) else "()")
    return ".".join(reversed(parts))


def _call_kind(call: ast.Call):
    name = _dotted(call.func)
    last = name.rsplit(".", 1)[-1]
    if last == "commit":
        return "db commit"
    if name == "psycopg2.connect":
        return "db connect"
    if last in {"urlopen", "guarded_urlopen", "bounded_urlopen", "provider_post", "guarded_session"}:
        return "http"
    if name.startswith("requests.") and last in {"get", "post", "put", "patch", "delete", "request"}:
        return "http"
    if last == "Anthropic":
        return "model client"
    if name.startswith("subprocess."):
        return "subprocess"
    if name in _FILE_WRITERS or last in {"write_text", "write_bytes"}:
        return "file write"
    if name == "open" or last == "fdopen":
        mode = call.args[1].value if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant) else None
        for keyword in call.keywords:
            if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                mode = keyword.value.value
        if isinstance(mode, str) and _WRITE_MODES & set(mode):
            return "file write"
    return None


def _side_effects(source: str) -> set:
    """(enclosing function or Class.method, kind) for every side-effect
    primitive or reach-around import in a module's source."""
    found = set()

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            child_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                child_scope = f"{scope}.{child.name}" if scope else child.name
            if isinstance(child, ast.Call):
                kind = _call_kind(child)
                if kind:
                    found.add((scope or "<module>", kind))
            elif isinstance(child, ast.ImportFrom) and child.module:
                if child.module in _REACH_AROUND_MODULES:
                    found.add((scope or "<module>", _REACH_AROUND_MODULES[child.module]))
                for alias in child.names:
                    kind = _REACH_AROUND_NAMES.get((child.module, alias.name))
                    if kind:
                        found.add((scope or "<module>", kind))
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    if alias.name in _REACH_AROUND_MODULES:
                        found.add((scope or "<module>", _REACH_AROUND_MODULES[alias.name]))
            visit(child, child_scope)

    visit(ast.parse(source), "")
    return found


# Every side-effect primitive in coordinator/ outside actuators.py, with the
# reason it is allowed to exist there. Anything not listed fails the test below.
SIDE_EFFECTS_OUTSIDE_THE_GATE = {
    # Reads.
    ("coordinator/spend_tracker.py", "_fetch_cost_buckets", "http"):
        "GET of the Anthropic cost report",
    ("coordinator/agents/backend_engineer.py", "BackendEngineer._handle_test_build", "db connect"):
        "two SELECTs on a read-only session; the build is queued through Actuators.queue_new_build",
    ("coordinator/agents/security_engineer.py", "SecurityEngineer._handle_daily_scan", "http"):
        "HEAD of each live app through the guarded, bounded session (monitor.ping.guarded_session)",
    ("coordinator/agents/reconciliation.py", "<module>", "railway probe"):
        "read-only Railway lookup shared with the worker's supervisor",
    # Model calls: each is made only after Actuators.authorize_model_call
    # returned True (tests: test_customer_service_agent.py,
    # test_failure_kb_sync.py, test_build_failure_categorizer.py).
    ("coordinator/agents/customer_service.py", "CustomerService._draft_kb_answer", "model client"):
        "support-answer draft, gated in _draft_and_record_kb_answer",
    ("coordinator/failure_kb_sync.py", "draft_failure_answer", "model client"):
        "failure-KB answer draft, gated in sync_failure_kb_to_support_kb",
    ("coordinator/build_failure_categorizer.py", "categorize_log", "model client"):
        "failure categorization, gated in enrich_uncategorized",
    # The fixer this loads is run by Actuators.run_playbook, not by the agent.
    ("coordinator/agents/infra_engineer.py", "InfraEngineer._handle_build_failure", "playbook loader"):
        "loads the fixer and hands it to Actuators.run_playbook",
    # The tick loop's own bookkeeping (task rows, the tick record,
    # approval-request expiry). Not an agent action; not behind the gate.
    ("coordinator/tick.py", "_create_thread_conn", "db connect"): "per-thread bookkeeping connection",
    ("coordinator/tick.py", "run_tick", "db commit"): "tick record and pending work",
    ("coordinator/tick.py", "_dispatch_agent", "db commit"): "task status and result",
    ("coordinator/tick.py", "_fail_task_after_exception", "db commit"):
        "task marked failed after its agent raised",
    ("coordinator/tick.py", "_close_task_at_deadline", "db commit"):
        "task marked failed when its agent ran past the round's deadline",
    ("coordinator/tick.py", "_dispatch_one_parallel", "db commit"): "task bookkeeping on the thread connection",
    ("coordinator/tick.py", "_write_tick_placeholder", "db commit"): "tick row",
    ("coordinator/tick.py", "_expire_stale_approval_requests", "db commit"):
        "denies approval requests nobody answered",
    ("coordinator/tick.py", "_send_approval_reminder", "escalation email"):
        "one reminder to the admin about a pending approval request",
}


def _coordinator_sources():
    for path in sorted((ROOT / "coordinator").rglob("*.py")):
        yield path.relative_to(ROOT).as_posix(), path.read_text()


def test_no_coordinator_module_has_a_side_effect_outside_the_actuator_layer():
    found = {
        (rel, scope, kind)
        for rel, source in _coordinator_sources() if rel != "coordinator/actuators.py"
        for scope, kind in _side_effects(source)
    }
    unlisted = sorted(found - set(SIDE_EFFECTS_OUTSIDE_THE_GATE))
    assert unlisted == [], (
        "side effect outside coordinator/actuators.py with no entry in "
        "SIDE_EFFECTS_OUTSIDE_THE_GATE (route it through an Actuators method): " + repr(unlisted)
    )
    stale = sorted(set(SIDE_EFFECTS_OUTSIDE_THE_GATE) - found)
    assert stale == [], f"listed but no longer in the code (remove the entry): {stale}"


def test_every_effect_inside_the_actuator_layer_sits_in_a_method_the_matrix_covers():
    source = (ROOT / "coordinator" / "actuators.py").read_text()
    covered = set(GATED) | UNGATED | DOWNGRADING
    helpers = {
        "_send_escalation_email": "create_approval_request",        # its email
        "_audit_live_playbook": "run_playbook",                     # its audit row
    }
    for scope, kind in sorted(_side_effects(source)):
        method = helpers.get(scope) or scope.removeprefix("Actuators.")
        assert method in covered, f"{scope} ({kind}) is not a method in the permission matrix"


@pytest.mark.parametrize("snippet, kind", [
    ("def f(conn):\n    conn.commit()\n", "db commit"),
    ("import psycopg2\ndef f():\n    psycopg2.connect('x')\n", "db connect"),
    ("from urllib.request import urlopen\ndef f():\n    urlopen('https://example.test')\n", "http"),
    ("import requests\ndef f():\n    requests.post('https://example.test')\n", "http"),
    ("import anthropic\ndef f():\n    anthropic.Anthropic(api_key='k')\n", "model client"),
    ("import subprocess\ndef f():\n    subprocess.run(['true'])\n", "subprocess"),
    ("def f(p):\n    open(p, 'w').write('x')\n", "file write"),
    ("def f(p):\n    p.write_text('x')\n", "file write"),
    ("def f():\n    from ops.fixer import trigger_redeploy\n", "ops fixer"),
    ("def f():\n    from cli.failure_kb import _save_kb\n", "failure KB write"),
    ("def f():\n    from ops.playbook import load_fixer\n", "playbook loader"),
    ("def f(guard, req):\n    guard.guarded_urlopen(req, timeout=5)\n", "http"),
    ("def f(req):\n    bounded_urlopen(req, timeout=5)\n", "http"),
    ("def f(url):\n    provider_post(url, json={}, timeout=5)\n", "http"),
    ("def f(url):\n    guarded_session().head(url, timeout=5)\n", "http"),
    ("def f():\n    from watchdog_probe import probe_for_zombie_deploy\n", "railway probe"),
])
def test_the_scan_recognises_each_kind_of_side_effect(snippet, kind):
    assert _side_effects(snippet) == {("f", kind)}


def test_the_scan_ignores_reads():
    assert _side_effects(
        "def f(conn, p):\n    conn.rollback()\n    open(p).read()\n    open(p, 'r')\n    conn.cursor().execute('SELECT 1')\n"
    ) == set()


# ---------------------------------------------------------------------------
# What a coordinator module may import, and what it may do without importing
# ---------------------------------------------------------------------------
#
# The call-pattern scan above recognises a side effect by the name it is called
# under, so it is a tripwire: an alias, a different HTTP library or
# `os.system` walks past it. This scan closes that by turning the question
# around. Outside coordinator/actuators.py a module may import only
#   - the standard-library modules in SAFE_IMPORTS, none of which can open a
#     socket, start a process, or reach a database or a model, and
#   - other coordinator modules (but not a library one of them imported: a
#     name such as coordinator.tick.psycopg2 counts as importing psycopg2).
# Every other import (a network, subprocess, database or model library, or
# first-party code outside coordinator/, which can wrap any of those) is a
# finding and must be listed below with the reason it is there. The capability
# is refused where it enters the module, whatever it is then called.
#
# Three capabilities need no import, so they are checked by use:
#   - `os`: only the attributes in OS_SAFE_ATTRS (os.system, os.popen,
#     os.remove and the rest are findings, under any alias);
#   - dynamic code and module objects: eval, exec, compile, __import__,
#     globals, vars, locals, sys.modules, __dict__ and friends, and a module
#     object used as a value (getattr(os, "system"), handing `subprocess` to a
#     helper) instead of as the base of an attribute access;
#   - files: open() unless its mode is a literal read mode, under any receiver
#     (so pathlib's Path.open too), and the pathlib and os writers by method
#     name.
#
# What this scan does NOT establish: that a module makes no database write
# through a connection it was handed. tick.py, work_detection.py, state.py,
# scorer.py, budget.py and the spend tracker receive the tick's connection;
# `.commit()`, a `with` block on an existing object and SQL text that writes
# are reported as tripwires (and listed for tick.py, whose bookkeeping they
# are), but a write can be spelled so that none of the three matches.

SAFE_IMPORTS = frozenset({
    "__future__", "abc", "collections", "concurrent.futures", "copy", "dataclasses", "datetime",
    "decimal", "enum", "functools", "hashlib", "itertools", "json", "logging", "math", "pathlib",
    "posixpath", "random", "re", "sys", "threading", "time", "typing", "urllib.error",
    "urllib.parse", "uuid",
})
OS_SAFE_ATTRS = frozenset({"environ", "getenv", "path", "sep", "linesep", "getcwd", "getpid", "name",
                           "fspath", "cpu_count"})
_DYNAMIC_BUILTINS = frozenset({"eval", "exec", "compile", "__import__", "globals", "vars", "locals",
                               "breakpoint", "__builtins__"})
_DYNAMIC_ATTRS = frozenset({"__dict__", "__globals__", "__builtins__", "__import__", "__subclasses__",
                            "__loader__", "__spec__"})
_PATH_WRITERS = frozenset({"write_text", "write_bytes", "unlink", "rmdir", "mkdir", "touch", "rename",
                           "symlink_to", "hardlink_to", "chmod", "rmtree", "truncate"})
_WRITE_SQL_RE = re.compile(
    r"\b(insert\s+into|update\s+\w+\s+set|delete\s+from|truncate\s+\w|drop\s+(table|index)|alter\s+table)\b",
    re.IGNORECASE)


@functools.lru_cache(maxsize=None)
def _is_module(dotted: str) -> bool:
    """True when `dotted` names an importable module (used to tell
    `from logging import handlers`, a submodule, from `from logging import INFO`)."""
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _is_internal(dotted: str) -> bool:
    return dotted == "coordinator" or dotted.startswith("coordinator.")


def _unsafe(dotted: str) -> bool:
    """True when importing module `dotted` outside actuators.py is a finding.
    `os` is judged by use (OS_SAFE_ATTRS) and coordinator modules by what is
    taken from them."""
    return not (dotted == "os" or dotted.startswith("os.") or _is_internal(dotted)
                or dotted in SAFE_IMPORTS)


def _absolute_module(node: ast.ImportFrom, package: str) -> str:
    """The absolute dotted module an ImportFrom names, resolving leading dots."""
    if not node.level:
        return node.module or ""
    base = package.split(".")
    base = base[:max(0, len(base) - (node.level - 1))]
    return ".".join(base + ([node.module] if node.module else []))


def _imported_modules(node, package: str):
    """(local name, module the local name refers to, module imported) for each
    name an Import or ImportFrom statement binds. The middle field is "" when
    the name is not a module object (a function taken from a library), and
    None when the statement takes a name from `os` or from a coordinator
    module, which the caller resolves."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.asname:
                yield alias.asname, alias.name, alias.name
            else:
                top = alias.name.split(".")[0]
                yield top, top, alias.name
        return
    module = _absolute_module(node, package)
    for alias in node.names:
        local = alias.asname or alias.name
        full = f"{module}.{alias.name}" if module else alias.name
        if not module:                                  # `from .. import x`, above the package
            yield local, alias.name, alias.name
        elif _is_internal(module) or module == "os":
            yield local, None, full
        elif _unsafe(module):
            yield local, "", module
        elif _is_module(full):
            yield local, full, full                     # a submodule of a safe package
        # else: a plain name from a safe module, nothing to record


def _module_bindings(source: str, package: str, known=None) -> dict:
    """local name -> dotted module, for every name a module binds to a module
    object or takes from an unsafe library: what another coordinator module
    gets by importing that name from this one. `known` is the same map for
    the other coordinator modules, so a name passed along twice is followed."""
    known = known or {}
    bound = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for local, refers_to, imported in _imported_modules(node, package):
            if refers_to is None:
                owner, _, name = imported.rpartition(".")
                refers_to = known.get(owner, {}).get(name) or (
                    imported if imported in known or _is_module(imported) else "")
            if refers_to:
                bound[local] = refers_to
            elif _unsafe(imported):
                bound[local] = imported
    return bound


def _capability_findings(source: str, package: str = "coordinator", reexports=None) -> set:
    """(scope, finding) for every import and use described in the block
    comment above. `package` is the package the module lives in (for relative
    imports); `reexports` maps a coordinator module's dotted name to
    _module_bindings of it."""
    reexports = reexports or {}
    tree = ast.parse(source)
    found = set()
    aliases = {}            # local name -> dotted module the name refers to
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))

    def note_import(scope, node):
        for local, refers_to, imported in _imported_modules(node, package):
            if refers_to is None:
                owner, _, name = imported.rpartition(".")
                if owner == "os":
                    if name == "path":
                        aliases[local] = "os.path"
                    elif name not in OS_SAFE_ATTRS:
                        found.add((scope, f"os.{name}"))
                    continue
                borrowed = reexports.get(owner, {}).get(name)
                if borrowed:                           # what another coordinator module imported
                    aliases[local] = borrowed
                    if _unsafe(borrowed):
                        found.add((scope, f"import {borrowed}"))
                elif imported in reexports or _is_module(imported):
                    aliases[local] = imported          # a coordinator submodule
                continue
            if refers_to:
                aliases[local] = refers_to
            if _unsafe(imported):
                found.add((scope, f"import {imported}"))

    def resolve(node):
        """The dotted module an expression refers to, or None."""
        if isinstance(node, ast.Name):
            return aliases.get(node.id)
        if isinstance(node, ast.Attribute):
            base = resolve(node.value)
            if base is None:
                return None
            borrowed = reexports.get(base, {}).get(node.attr)
            if borrowed:
                return borrowed
            full = f"{base}.{node.attr}"
            if full in reexports or full == "os.path" or (not _is_internal(base) and _is_module(full)):
                return full
        return None

    def note_use(scope, node, parent):
        is_attribute_base = isinstance(parent, ast.Attribute) and parent.value is node
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            module = resolve(node)
            if module is not None:
                if _unsafe(module) and isinstance(node, ast.Attribute):
                    found.add((scope, f"import {module}"))
                if not is_attribute_base:
                    found.add((scope, f"module object used as a value: {module}"))
        if isinstance(node, ast.Attribute):
            base = resolve(node.value)
            if base == "os" and node.attr not in OS_SAFE_ATTRS:
                found.add((scope, f"os.{node.attr}"))
            if base == "sys" and node.attr == "modules":
                found.add((scope, "dynamic code: sys.modules"))
            if node.attr in _DYNAMIC_ATTRS:
                found.add((scope, f"dynamic code: {node.attr}"))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in _DYNAMIC_BUILTINS:
                found.add((scope, f"dynamic code: {node.id}"))
            if node.id == "open" and not (isinstance(parent, ast.Call) and parent.func is node):
                found.add((scope, "file write: open used as a value"))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name == "open":
                position = 1 if isinstance(func, ast.Name) else 0
                mode = node.args[position] if len(node.args) > position else None
                for keyword in node.keywords:
                    if keyword.arg == "mode":
                        mode = keyword.value
                literal = mode.value if isinstance(mode, ast.Constant) and isinstance(mode.value, str) else None
                if mode is not None and (literal is None or set(literal) & set("wax+")):
                    found.add((scope, "file write: open"))
            elif isinstance(func, ast.Attribute) and name in _PATH_WRITERS:
                found.add((scope, f"file write: .{name}()"))
            elif isinstance(func, ast.Attribute) and name == "replace" and len(node.args) == 1 \
                    and not node.keywords:
                found.add((scope, "file write: .replace(target)"))  # str.replace takes two arguments
            elif isinstance(func, ast.Attribute) and name == "commit":
                found.add((scope, "db write: .commit()"))
        elif isinstance(node, ast.With):
            for item in node.items:
                if not isinstance(item.context_expr, ast.Call):
                    found.add((scope, f"with block on an existing object: {ast.unparse(item.context_expr)}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if _WRITE_SQL_RE.search(node.value):
                found.add((scope, "db write: SQL text"))

    def walk(node, scope, visit):
        for child in ast.iter_child_nodes(node):
            child_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                child_scope = f"{scope}.{child.name}" if scope else child.name
            visit(scope or "<module>", child, node)
            walk(child, child_scope, visit)

    # imports first, so that a use above its import in the file is still seen
    walk(tree, "", lambda scope, child, _parent: note_import(scope, child)
         if isinstance(child, (ast.Import, ast.ImportFrom)) else None)
    walk(tree, "", note_use)
    return found


def _package_of(rel: str) -> str:
    dotted = rel[:-3].replace("/", ".")
    return dotted.removesuffix(".__init__") if rel.endswith("__init__.py") else dotted.rsplit(".", 1)[0]


def _coordinator_reexports() -> dict:
    """_module_bindings for every coordinator module, repeated until a name
    handed from module to module has been followed to the library it names."""
    sources = list(_coordinator_sources())
    known = {}
    for _ in range(len(sources)):
        latest = {
            rel[:-3].replace("/", ".").removesuffix(".__init__"): _module_bindings(source, _package_of(rel), known)
            for rel, source in sources
        }
        if latest == known:
            break
        known = latest
    return known


# Every finding of _capability_findings in coordinator/ outside actuators.py,
# as (file, finding), with the reason. The scope is left out on purpose: the
# statement is about what a module can reach. Anything not listed fails.
CAPABILITIES_OUTSIDE_THE_GATE = {
    # Model library. Each call is made only after Actuators.authorize_model_call
    # granted it (dry run, permission and the tick's model-call allowance).
    ("coordinator/agents/customer_service.py", "import anthropic"): "support-answer draft",
    ("coordinator/failure_kb_sync.py", "import anthropic"): "failure-KB answer draft",
    ("coordinator/build_failure_categorizer.py", "import anthropic"): "failure categorization",
    # Network libraries, used for reads.
    ("coordinator/spend_tracker.py", "import urllib.request"):
        "builds the Request for the Anthropic cost report; it is sent by bounded_urlopen",
    ("coordinator/spend_tracker.py", "import monitor.bounded_fetch"):
        "bounded_urlopen: the GET of the cost report, one deadline and one byte cap per page",
    ("coordinator/agents/security_engineer.py", "import monitor.ping"):
        "probe_url_problem and guarded_session: the HEAD of each live app, address-pinned and bounded",
    ("coordinator/agents/reconciliation.py", "import watchdog_probe"):
        "read-only Railway lookup shared with the worker's supervisor",
    # Database library.
    ("coordinator/tick.py", "import psycopg2"): "the per-thread bookkeeping connection",
    ("coordinator/agents/backend_engineer.py", "import psycopg2"):
        "two SELECTs on a read-only session; the build is queued through Actuators.queue_new_build",
    # First-party code outside coordinator/.
    ("coordinator/failure_kb_sync.py", "import cli.failure_kb"): "_load_kb: reads the failure KB",
    ("coordinator/work_detection.py", "import ops.scanner"):
        "classify_error and is_deterministic_failure, two text classifiers",
    ("coordinator/agents/infra_engineer.py", "import ops.scanner"): "classify_error, a regex classifier",
    ("coordinator/agents/infra_engineer.py", "import ops.playbook"):
        "match_playbook and load_fixer; the fixer is run by Actuators.run_playbook, not here",
    # The tick loop's own bookkeeping on the tick's connection (task rows, the
    # tick record, approval-request expiry). Not an agent action and not
    # behind the gate; the header of coordinator/actuators.py says so.
    ("coordinator/tick.py", "db write: .commit()"): "tick bookkeeping",
    ("coordinator/tick.py", "db write: SQL text"): "tick bookkeeping",
    ("coordinator/tick.py", "dynamic code: __dict__"):
        "serialises an Action dataclass into the task row (a.__dict__)",
    ("coordinator/budget.py", "with block on an existing object: self._lock"):
        "a threading.Lock around the model-call meter's counters",
}


def test_coordinator_modules_import_and_use_only_what_is_listed():
    reexports = _coordinator_reexports()
    found = set()
    for rel, source in _coordinator_sources():
        if rel == "coordinator/actuators.py":
            continue
        found |= {(rel, finding)
                  for _scope, finding in _capability_findings(source, _package_of(rel), reexports)}
    unlisted = sorted(found - set(CAPABILITIES_OUTSIDE_THE_GATE))
    assert unlisted == [], (
        "a coordinator module outside actuators.py imports or uses something that is not in "
        "SAFE_IMPORTS / OS_SAFE_ATTRS and has no entry in CAPABILITIES_OUTSIDE_THE_GATE: " + repr(unlisted)
    )
    stale = sorted(set(CAPABILITIES_OUTSIDE_THE_GATE) - found)
    assert stale == [], f"listed but no longer in the code (remove the entry): {stale}"


def test_the_safe_import_list_holds_no_capability_library():
    """The allowlist is the trusted part of the scan, so it is pinned: adding a
    network, process, database or model library to it must be a visible edit
    to this test."""
    assert SAFE_IMPORTS == {
        "__future__", "abc", "collections", "concurrent.futures", "copy", "dataclasses", "datetime",
        "decimal", "enum", "functools", "hashlib", "itertools", "json", "logging", "math", "pathlib",
        "posixpath", "random", "re", "sys", "threading", "time", "typing", "urllib.error",
        "urllib.parse", "uuid",
    }
    assert OS_SAFE_ATTRS == {"environ", "getenv", "path", "sep", "linesep", "getcwd", "getpid", "name",
                             "fspath", "cpu_count"}


# One way of getting each capability into a module that the call-pattern scan
# (_side_effects) does not see. Every one must be a finding here.
EVASIONS = [
    # aliases of a database connect, and a transaction block instead of commit()
    ("import psycopg2\nconnect = psycopg2.connect\ndef f():\n    connect('x')\n", "import psycopg2"),
    ("from psycopg2 import connect as c\ndef f():\n    c('x')\n", "import psycopg2"),
    ("import psycopg2 as db\ndef f():\n    db.connect('x')\n", "import psycopg2"),
    ("def f(conn):\n    with conn:\n        conn.cursor().execute('SELECT 1')\n",
     "with block on an existing object: conn"),
    ("def f(conn):\n    conn.cursor().execute('UPDATE builds SET status = %s', ('live',))\n", "db write: SQL text"),
    ("def f(conn):\n    conn.cursor().execute('delete from builds')\n", "db write: SQL text"),
    ("import sqlite3\n", "import sqlite3"),
    # HTTP and raw network libraries other than the two the call-pattern scan knows
    ("import httpx\ndef f():\n    httpx.post('https://example.test')\n", "import httpx"),
    ("import aiohttp\n", "import aiohttp"),
    ("import socket\ndef f():\n    socket.create_connection(('example.test', 80))\n", "import socket"),
    ("import smtplib\ndef f():\n    smtplib.SMTP('example.test').sendmail('a', 'b', 'c')\n", "import smtplib"),
    ("import http.client\n", "import http.client"),
    ("from http import client\n", "import http"),
    ("import urllib.request as r\ndef f():\n    r.urlopen('https://example.test')\n", "import urllib.request"),
    ("from urllib import request\ndef f():\n    request.urlopen('https://example.test')\n", "import urllib"),
    ("import urllib\n", "import urllib"),
    ("import logging.handlers\n", "import logging.handlers"),
    ("from logging import handlers\n", "import logging.handlers"),
    ("import logging\ndef f():\n    logging.handlers.SMTPHandler('h', 'a', 'b', 's')\n", "import logging.handlers"),
    ("import asyncio\n", "import asyncio"),
    # model clients under another name
    ("from anthropic import AsyncAnthropic\ndef f():\n    AsyncAnthropic()\n", "import anthropic"),
    ("import anthropic as a\ndef f():\n    a.Anthropic()\n", "import anthropic"),
    ("import openai\n", "import openai"),
    # processes
    ("import os\ndef f():\n    os.system('true')\n", "os.system"),
    ("import os as o\ndef f():\n    o.popen('true')\n", "os.popen"),
    ("from os import system\ndef f():\n    system('true')\n", "os.system"),
    ("from os import execv as run\n", "os.execv"),
    ("import os\ndef f():\n    getattr(os, 'sys' + 'tem')('true')\n", "module object used as a value: os"),
    ("import os\ndef f(g):\n    g(os)\n", "module object used as a value: os"),
    ("import subprocess as sp\ndef f():\n    sp.run(['true'])\n", "import subprocess"),
    ("from subprocess import run\ndef f():\n    run(['true'])\n", "import subprocess"),
    ("import multiprocessing\n", "import multiprocessing"),
    ("import ctypes\n", "import ctypes"),
    # dynamic imports and code
    ("def f():\n    __import__('subprocess').run(['true'])\n", "dynamic code: __import__"),
    ("import importlib\ndef f():\n    importlib.import_module('socket')\n", "import importlib"),
    ("def f(s):\n    eval(s)\n", "dynamic code: eval"),
    ("def f(s):\n    exec(s)\n", "dynamic code: exec"),
    ("import sys\ndef f():\n    sys.modules['subprocess'].run(['true'])\n", "dynamic code: sys.modules"),
    ("def f():\n    globals()['x'] = 1\n", "dynamic code: globals"),
    ("def f(m):\n    m.__dict__['connect']('x')\n", "dynamic code: __dict__"),
    # files
    ("from pathlib import Path\ndef f(p):\n    Path(p).open('w').write('x')\n", "file write: open"),
    ("from pathlib import Path\ndef f(p):\n    with Path(p).open(mode='a') as fh:\n        fh.write('x')\n",
     "file write: open"),
    ("def f(p, mode):\n    open(p, mode)\n", "file write: open"),
    ("def f(p):\n    o = open\n    o(p, 'w')\n", "file write: open used as a value"),
    ("def f(p):\n    p.unlink()\n", "file write: .unlink()"),
    ("def f(p, q):\n    p.replace(q)\n", "file write: .replace(target)"),
    ("def f(p):\n    p.mkdir(parents=True)\n", "file write: .mkdir()"),
    ("import os\ndef f(p):\n    os.remove(p)\n", "os.remove"),
    ("import os\ndef f(p):\n    os.makedirs(p)\n", "os.makedirs"),
    ("import shutil\n", "import shutil"),
    ("import tempfile\n", "import tempfile"),
    ("import io\n", "import io"),
    # first-party code outside coordinator/, which can wrap any of the above
    ("from ops.fixer import trigger_redeploy\n", "import ops.fixer"),
    ("import mailer\n", "import mailer"),
    ("from cli import deploy\n", "import cli"),
    ("from .. import queue\n", "import queue"),
]


@pytest.mark.parametrize("snippet, expected", EVASIONS)
def test_the_import_scan_sees_each_evasion(snippet, expected):
    findings = {finding for _scope, finding in _capability_findings(snippet, "coordinator")}
    assert expected in findings, f"missed: {snippet!r} gave {sorted(findings)}"


def test_most_of_those_evasions_do_pass_the_call_pattern_scan():
    """Why the import scan exists: the name matcher sees none of these."""
    unseen = [snippet for snippet, _ in EVASIONS if _side_effects(snippet) == set()]
    assert len(unseen) >= 40, len(unseen)


def test_a_library_cannot_be_borrowed_from_another_coordinator_module():
    """coordinator.tick imports psycopg2 and os. Taking either name from there
    is importing it, and what is done with it is judged the same way."""
    reexports = _coordinator_reexports()
    assert reexports["coordinator.tick"]["psycopg2"] == "psycopg2"
    assert reexports["coordinator.tick"]["os"] == "os"
    assert reexports["coordinator.actuators"]["bounded_urlopen"] == "monitor.bounded_fetch"
    for snippet, expected in (
        ("from coordinator.tick import psycopg2\n", "import psycopg2"),
        ("from coordinator import tick\ndef f():\n    tick.psycopg2.connect('x')\n", "import psycopg2"),
        ("from . import tick\ndef f():\n    tick.psycopg2.connect('x')\n", "import psycopg2"),
        ("import coordinator.tick as t\ndef f():\n    t.psycopg2.connect('x')\n", "import psycopg2"),
        ("from coordinator.actuators import bounded_urlopen\n", "import monitor.bounded_fetch"),
        ("from coordinator.tick import os as o\ndef f():\n    o.system('true')\n", "os.system"),
        ("from coordinator import tick\ndef f():\n    tick.os.system('true')\n", "os.system"),
        ("from coordinator import tick\ndef f(g):\n    g(tick)\n",
         "module object used as a value: coordinator.tick"),
    ):
        findings = {f for _s, f in _capability_findings(snippet, "coordinator", reexports)}
        assert expected in findings, (snippet, sorted(findings))


def test_the_import_scan_accepts_ordinary_code():
    clean = (
        '"""Docstring that mentions INSERT INTO builds and UPDATE builds SET x."""\n'
        "from __future__ import annotations\n"
        "import json\nimport logging\nimport os\nimport re\nfrom datetime import datetime, timezone\n"
        "from pathlib import Path\nfrom urllib.parse import quote\n"
        "from coordinator import config\nfrom coordinator.state import WorldState\n"
        "logger = logging.getLogger(__name__)\n"
        "def f(conn, p, name):\n"
        "    key = os.environ.get('X', '') + os.path.basename(str(p))\n"
        "    text = Path(p).read_text().replace('a', 'b')\n"
        "    with open(p) as fh, open(p, 'rb') as raw, conn.cursor() as cur:\n"
        "        cur.execute('SELECT id FROM builds WHERE updated_at > %s', (datetime.now(timezone.utc),))\n"
        "        rows = cur.fetchall()\n"
        "    conn.rollback()\n"
        "    return json.dumps([key, text, quote(name), config.FAILURE_RATE_WINDOW, rows, re.sub('a', 'b', name)])\n"
    )
    assert _capability_findings(clean, "coordinator", _coordinator_reexports()) == set()


# ---------------------------------------------------------------------------
# An agent is handed its Actuators; it does not make one
# ---------------------------------------------------------------------------
#
# The permission table, analysis-only mode and dry run are all properties of
# the INSTANCE an agent calls. An agent that could build its own would choose
# them: Actuators("infra_engineer", task.id, dry_run=False) from inside the
# QA agent is every permission of the infra engineer, live, in the default
# dry-run configuration. Two things stand in the way:
#   - at run time, Actuators.__init__ refuses a call from any coordinator
#     module that is not one of the issuers;
#   - at review time, the scan below finds a construction, a subclass or any
#     other use of the class as a value in a coordinator module that is not an
#     issuer (annotations and the import itself are what an agent needs).

from coordinator.actuators import _ISSUING_MODULES


def _run_as_module(module_name: str, source: str) -> dict:
    """Execute `source` the way Python would run a module called
    `module_name` (its globals carry that __name__)."""
    namespace = {"__name__": module_name}
    exec(compile(source, f"<{module_name}>", "exec"), namespace)  # test code built here
    return namespace


_ROGUE_AGENT = (
    "from coordinator.actuators import Actuators\n"
    "def execute(task_id, handed):\n"
    "    mine = {construction}\n"
    "    return mine\n"
)


@pytest.mark.parametrize("construction", [
    'Actuators("infra_engineer", task_id, dry_run=False)',
    'Actuators(agent_name=handed.agent, task_id=task_id, dry_run=False, model_calls=handed.model_calls)',
    'type(handed)("infra_engineer", task_id, dry_run=False)',
    'handed.__class__("infra_engineer", task_id, dry_run=False)',
    '(lambda cls: cls("infra_engineer", task_id, dry_run=False))(Actuators)',
    '__import__("functools").partial(Actuators, "infra_engineer", task_id, dry_run=False)()',
    'type("Mine", (Actuators,), dict())("infra_engineer", task_id, dry_run=False)',
])
@pytest.mark.parametrize("module_name", [
    "coordinator.agents.qa_engineer", "coordinator.agents.rogue", "coordinator.scorer", "coordinator",
])
def test_a_coordinator_module_that_is_not_an_issuer_cannot_construct_actuators(module_name, construction):
    handed = Actuators("qa_test", "task-1", dry_run=True)
    rogue = _run_as_module(module_name, _ROGUE_AGENT.format(construction=construction))
    with pytest.raises(PermissionError, match="may not construct Actuators"):
        rogue["execute"]("task-1", handed)


@pytest.mark.parametrize("module_name", sorted(_ISSUING_MODULES))
def test_the_issuers_can(module_name):
    issuer = _run_as_module(module_name, _ROGUE_AGENT.format(
        construction='Actuators("infra_engineer", task_id, dry_run=True)'))
    assert issuer["execute"]("task-1", None).agent == "infra_engineer"


def test_the_issuers_are_the_tick_and_work_detection():
    assert _ISSUING_MODULES == {"coordinator.tick", "coordinator.work_detection"}


def test_the_real_agents_run_with_the_instance_they_are_handed():
    """Every registered agent, through execute(), with a handed instance:
    none of them trips the constructor rule (they would raise PermissionError
    from Actuators.__init__, which no handler catches by that message)."""
    from coordinator.state import WorkItem, WorldState
    from tests.test_coordinator_permission_use import AGENTS as agent_classes

    constructed = []
    real_init = Actuators.__init__

    def counting_init(self, *args, **kwargs):
        constructed.append(sys._getframe(1).f_globals.get("__name__"))
        # one frame deeper than the product code expects: hand it the caller's name
        real_init(self, *args, **kwargs)

    for name, agent_cls in sorted(agent_classes.items()):
        handed = Actuators(name, "task-handed", dry_run=True)
        handed.query_readonly = MagicMock(return_value=[])
        task = WorkItem(id="task-handed", category="agent_initiative", target_agent=name, context={})
        with patch.object(Actuators, "__init__", counting_init), \
                patch("ops.alerter.send_admin_alert", return_value=True):
            agent_cls().execute(task, WorldState(), "claude-haiku-4-5", handed)
    assert constructed == [], f"an agent module constructed an Actuators: {constructed}"


def _actuators_uses(source: str, module_name: str) -> set:
    """(scope, finding) for every way a module handles the Actuators class
    other than importing it and naming it in an annotation."""
    tree = ast.parse(source)
    annotations = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            hints = [arg.annotation for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs]
            hints += [node.returns, getattr(node.args.vararg, "annotation", None),
                      getattr(node.args.kwarg, "annotation", None)]
            for hint in hints:
                if hint is not None:
                    annotations.update(id(inner) for inner in ast.walk(hint))
        elif isinstance(node, ast.AnnAssign):
            annotations.update(id(inner) for inner in ast.walk(node.annotation))

    names = {"Actuators"}       # local names bound to the class
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("actuators"):
            names.update(alias.asname or alias.name for alias in node.names if alias.name == "Actuators")

    found = set()

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            child_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                child_scope = f"{scope}.{child.name}" if scope else child.name
            if id(child) not in annotations:
                is_class_name = isinstance(child, ast.Name) and child.id in names and isinstance(child.ctx, ast.Load)
                is_class_attr = isinstance(child, ast.Attribute) and child.attr == "Actuators"
                if is_class_name or is_class_attr:
                    found.add((scope or "<module>", "Actuators used as a value"))
                if isinstance(child, ast.Attribute) and child.attr == "__class__":
                    found.add((scope or "<module>", "__class__"))
                # type(x)(...) builds another instance of x's class, and
                # three-argument type() builds a subclass. (type(exc).__name__,
                # which the modules use in log lines, is neither.)
                if isinstance(child, ast.Call):
                    func = child.func
                    type_of = isinstance(func, ast.Call) and isinstance(func.func, ast.Name) \
                        and func.func.id == "type"
                    new_class = isinstance(func, ast.Name) and func.id == "type" and len(child.args) == 3
                    if type_of or new_class:
                        found.add((scope or "<module>", "type()"))
                if isinstance(child, ast.Constant) and child.value == "Actuators":
                    found.add((scope or "<module>", "the class named in a string"))
            visit(child, child_scope)

    visit(tree, "")
    return found


# Where the class may be used as a value (constructed), and by whom.
ACTUATORS_CONSTRUCTED_IN = {
    ("coordinator/tick.py", "_dispatch_agent"): "the one instance a dispatched task's agent is handed",
    ("coordinator/tick.py", "_check_escalation"): "files the escalation under the agent's own name",
    ("coordinator/tick.py", "_surface_initiative_findings"): "files a successful task's findings",
    ("coordinator/work_detection.py", "detect_work"): "the failure-triage principal",
}


def test_no_coordinator_module_but_the_issuers_constructs_or_handles_the_class():
    found = set()
    for rel, source in _coordinator_sources():
        if rel == "coordinator/actuators.py":
            continue
        module_name = rel[:-3].replace("/", ".")
        found |= {(rel, scope) for scope, _finding in _actuators_uses(source, module_name)}
    assert found == set(ACTUATORS_CONSTRUCTED_IN), (
        "Actuators is constructed, subclassed or passed around outside the listed places: "
        + repr(sorted(found - set(ACTUATORS_CONSTRUCTED_IN)))
        + "; listed but gone: " + repr(sorted(set(ACTUATORS_CONSTRUCTED_IN) - found))
    )
    assert {rel[:-3].replace("/", ".") for rel, _scope in ACTUATORS_CONSTRUCTED_IN} == set(_ISSUING_MODULES)


@pytest.mark.parametrize("snippet, finding", [
    ("from coordinator.actuators import Actuators\nclass A:\n    def execute(self, task, actuators):\n"
     "        mine = Actuators('infra_engineer', task.id, dry_run=False)\n", "Actuators used as a value"),
    ("from coordinator.actuators import Actuators as Gate\ndef f(task):\n    return Gate('x', task.id)\n",
     "Actuators used as a value"),
    ("from coordinator import actuators\ndef f(task):\n    return actuators.Actuators('x', task.id)\n",
     "Actuators used as a value"),
    ("from coordinator.actuators import Actuators\nclass Mine(Actuators):\n    pass\n", "Actuators used as a value"),
    ("from coordinator.actuators import Actuators\nmaker = Actuators\n", "Actuators used as a value"),
    ("def f(handed, task):\n    return type(handed)('x', task.id, dry_run=False)\n", "type()"),
    ("def f(base, task):\n    return type('Mine', (base,), {})('x', task.id, dry_run=False)\n", "type()"),
    ("def f(handed, task):\n    return handed.__class__('x', task.id, dry_run=False)\n", "__class__"),
    ("import coordinator.actuators as a\ndef f(task):\n    return getattr(a, 'Actuators')('x', task.id)\n",
     "the class named in a string"),
])
def test_the_scan_sees_each_way_of_making_one(snippet, finding):
    assert finding in {found for _scope, found in _actuators_uses(snippet, "coordinator.agents.rogue")}


def test_the_scan_accepts_what_an_agent_needs():
    """Importing the class and naming it in a signature is how every agent is
    written; calling methods on the instance it was handed is the point."""
    ordinary = (
        "from coordinator.actuators import Actuators\n"
        "class A:\n"
        "    def execute(self, task, state, model: str, actuators: Actuators) -> 'AgentResult':\n"
        "        return self._handle(task.context, actuators)\n"
        "    def _handle(self, context: dict, actuators: Actuators):\n"
        "        actuators.send_alert('s', 'b')\n"
        "        try:\n"
        "            return actuators.query_readonly('SELECT 1')\n"
        "        except Exception as exc:\n"
        "            return type(exc).__name__\n"
    )
    assert _actuators_uses(ordinary, "coordinator.agents.sample") == set()


# ---------------------------------------------------------------------------
# query_readonly
# ---------------------------------------------------------------------------

MUTATING_STATEMENTS = [
    "INSERT INTO t VALUES (1)",
    "update t set v = 'x'",
    "  DELETE FROM t",
    "DROP TABLE t",
    "ALTER TABLE t ADD COLUMN c int",
    "TRUNCATE t",
    "CREATE TABLE u (id int)",
]


@pytest.mark.parametrize("statement", MUTATING_STATEMENTS)
def test_query_readonly_rejects_mutating_keywords_before_touching_the_database(statement):
    actuators = Actuators("infra_engineer", "task-ro", dry_run=False)
    with patch("psycopg2.connect") as connect:
        with pytest.raises(PermissionError, match="does not allow mutations"):
            actuators.query_readonly(statement)
    connect.assert_not_called()


def test_query_readonly_without_a_database_returns_nothing():
    assert Actuators("infra_engineer", "task-ro", dry_run=False).query_readonly("SELECT 1") == []


@pytest.fixture()
def demo_table(scratch_pg_url, monkeypatch):
    import psycopg2

    table = "qro_" + uuid.uuid4().hex[:8]
    conn = psycopg2.connect(scratch_pg_url)
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE {table} (id int, v text)")
        cur.execute(f"INSERT INTO {table} VALUES (1, 'one'), (2, 'two')")
    conn.commit()
    monkeypatch.setenv("DATABASE_URL", scratch_pg_url)
    yield table
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE {table}")
    conn.commit()
    conn.close()


def _rows(scratch_pg_url, table):
    import psycopg2

    conn = psycopg2.connect(scratch_pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id, v FROM {table} ORDER BY id")
            return cur.fetchall()
    finally:
        conn.close()


def test_query_readonly_returns_rows_as_dicts_and_binds_parameters(demo_table):
    actuators = Actuators("infra_engineer", "task-ro", dry_run=False)
    rows = actuators.query_readonly(f"SELECT id, v FROM {demo_table} WHERE id = %s", (2,))
    assert rows == [{"id": 2, "v": "two"}]


@pytest.mark.parametrize("sneaky", [
    "WITH gone AS (DELETE FROM {t} RETURNING id) SELECT count(*) FROM gone",
    "WITH ins AS (INSERT INTO {t} VALUES (3, 'three') RETURNING id) SELECT id FROM ins",
    "/* looks harmless */ UPDATE {t} SET v = 'changed'",
])
def test_query_readonly_cannot_write_even_when_the_keyword_check_is_dodged(demo_table, scratch_pg_url, sneaky):
    import psycopg2

    before = _rows(scratch_pg_url, demo_table)
    actuators = Actuators("infra_engineer", "task-ro", dry_run=False)
    with pytest.raises(psycopg2.Error):
        actuators.query_readonly(sneaky.format(t=demo_table))
    assert _rows(scratch_pg_url, demo_table) == before == [(1, "one"), (2, "two")]
