"""Boolean environment switches: one parser, and which way each switch falls.

coordinator/envflags.py has two readers. A safety switch (a dry run, analysis
only, a blocking gate) is turned off only by an explicit OFF word; an arming
switch (enable the coordinator, allow the orphan sweep, send progress email)
is turned on only by an explicit ON word. Everything else that is set, a typo
included, leaves the switch in its safe state.

Every switch read by queue/, cli/deploy.py, ops/ and coordinator/ is tested
here through the place that reads it, for the values 1, yes, on, "TRUE " (upper
case with a trailing space), 0, false, the empty string and a word that is not
a boolean, plus the unset case. The scan at the end reads those directories
and all of cli/ for an environment read that is compared with a string,
lower-cased or cast to bool without going through the parser; the hand-parsed
switches of the frozen v1 engine are listed there one by one with the rule each
applies. The web tier restates the two word lists for the admin settings page
(web/src/utils/env-switch.ts), and one test here compares them.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _extra in (str(ROOT), str(ROOT / "queue"), str(ROOT / "cli")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

import deploy  # noqa: E402  (cli/deploy.py, as the worker imports it)
import mailer  # noqa: E402  (queue/mailer.py)
import worker_pg  # noqa: E402

from coordinator import envflags, tick, work_detection  # noqa: E402
from coordinator.actuators import Actuators  # noqa: E402
from coordinator.state import WorldState  # noqa: E402
from ops import alerter  # noqa: E402

VALUES = ("1", "yes", "on", "TRUE ", "0", "false", "", "garbage")

# value -> expected state, per kind of switch
SAFETY_ON_BY_DEFAULT = dict(zip(VALUES, (True, True, True, True, False, False, True, True)))
SAFETY_OFF_BY_DEFAULT = dict(zip(VALUES, (True, True, True, True, False, False, False, True)))
ARMING = dict(zip(VALUES, (True, True, True, True, False, False, False, False)))


# ---------------------------------------------------------------------------
# The read sites
# ---------------------------------------------------------------------------

class _RecordedThread:
    """Stands in for threading.Thread: keeps the target, starts nothing."""

    target = None

    def __init__(self, target=None, **_kwargs):
        type(self).target = target

    def start(self):
        pass


def _coordinator_thread_dry_run() -> bool:
    """The dry_run flag the worker's coordinator loop hands to run_tick."""
    seen = {}

    def one_tick(**kwargs):
        seen["dry_run"] = kwargs["dry_run"]
        worker_pg._shutdown_event.set()
        return {}

    registry_before = dict(tick._agent_registry)
    was_set = worker_pg._shutdown_event.is_set()
    try:
        with patch.object(worker_pg.threading, "Thread", _RecordedThread):
            worker_pg._start_coordinator_thread()
        worker_pg._shutdown_event.clear()
        with patch("coordinator.tick.run_tick", one_tick), \
                patch.object(worker_pg, "_get_conn", return_value=MagicMock()):
            _RecordedThread.target()
    finally:
        tick._agent_registry.clear()
        tick._agent_registry.update(registry_before)
        if was_set:
            worker_pg._shutdown_event.set()
        else:
            worker_pg._shutdown_event.clear()
    return seen["dry_run"]


def _ops_thread_dry_run() -> bool:
    """The dry_run flag the worker's ops-agent thread hands to run_agent."""
    seen = {}
    with patch.object(worker_pg.threading, "Thread", _RecordedThread):
        worker_pg._start_ops_agent_thread()
    with patch("ops.agent.run_agent", lambda **kwargs: seen.update(kwargs)):
        _RecordedThread.target()
    return seen["dry_run"]


def _worker_started_the_coordinator() -> bool:
    """run_worker with every side effect stubbed: which daemon it starts, and
    what it writes to worker_heartbeat.coordinator_enabled (the two must agree)."""
    started = []
    heartbeat = MagicMock()
    was_set = worker_pg._shutdown_event.is_set()
    worker_pg._shutdown_event.set()  # the poll loop exits before its first pass
    try:
        with patch.object(worker_pg.signal, "signal"), \
                patch.object(worker_pg, "_get_conn", return_value=heartbeat), \
                patch.object(worker_pg, "_recover_orphans_once", return_value=0), \
                patch.object(worker_pg, "_sweep_orphan_railway_projects", return_value=0), \
                patch.object(worker_pg, "_startup_disk_cleanup"), \
                patch.object(worker_pg, "_start_monitor_thread"), \
                patch.object(worker_pg, "_start_backup_thread"), \
                patch.object(worker_pg, "_start_coordinator_thread", lambda: started.append("coordinator")), \
                patch.object(worker_pg, "_start_ops_agent_thread", lambda: started.append("ops")):
            worker_pg.run_worker()
    finally:
        if not was_set:
            worker_pg._shutdown_event.clear()
    assert started in (["coordinator"], ["ops"]), started
    heartbeat_params = heartbeat.cursor.return_value.execute.call_args[0][1]
    recorded = heartbeat_params[1]
    assert recorded is (started == ["coordinator"]), "the heartbeat and the daemon disagree"
    return recorded


def _orphan_sweep_ran() -> bool:
    """True when _sweep_orphan_railway_projects got as far as listing projects."""
    conn = MagicMock()
    with patch("deploy.gql", return_value={"projects": {"edges": []}}) as listing, \
            patch("deploy.destroy_project") as destroy:
        assert worker_pg._sweep_orphan_railway_projects(conn) == 0
    destroy.assert_not_called()
    return listing.called


def _progress_email_sent() -> bool:
    with patch.object(mailer, "_send") as send:
        mailer.send_build_progress("keeper@example.test", "Sam", "sample-app", "building")
    return send.called


def _test_build_trigger_armed() -> bool:
    """ENABLE_TEST_BUILDS: with the switch off the trigger returns before it
    looks at anything; with it on it reads the build counts."""
    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = (0,)
    state = WorldState(active_builds=0, queued_builds=0, daily_budget_remaining_usd=50.0)
    return bool(work_detection._check_test_build_needed(conn, state))


def _reconciliation_trigger_armed() -> bool:
    from datetime import datetime, timezone
    with patch.object(work_detection, "_last_reconciliation_sweep_time", return_value=None):
        return bool(work_detection._check_reconciliation_sweep(
            MagicMock(), datetime.now(timezone.utc), None))


SAFETY_ON_READERS = {
    "COORDINATOR_DRY_RUN": _coordinator_thread_dry_run,
    "OPS_DRY_RUN": _ops_thread_dry_run,
    "OPS_DRY_RUN (alerter)": alerter._is_dry_run,
    "BLOCK_ON_CODE_REVIEW_CRITICAL": lambda: worker_pg._block_gate_on("BLOCK_ON_CODE_REVIEW_CRITICAL"),
    "BLOCK_ON_QA_FIX_BUDGET": lambda: worker_pg._block_gate_on("BLOCK_ON_QA_FIX_BUDGET"),
    "USE_MAGIC_LINK_AUTH": deploy.magic_link_auth_enabled,
}

SAFETY_OFF_READERS = {
    "COORDINATOR_ANALYSIS_ONLY": lambda: Actuators("architect", "task-1").analysis_only,
    "BLOCKING_DESIGN_REVIEW": worker_pg._blocking_design_review,
    "BLOCKING_QA": worker_pg._blocking_qa,
}

ARMING_READERS = {
    "COORDINATOR_ENABLED": _worker_started_the_coordinator,
    "ENABLE_ORPHAN_SWEEP": _orphan_sweep_ran,
    "CAS_HARBOR_AUTO_REVIEW": lambda: worker_pg._initial_review_status() == "ai_review_queued",
    "SEND_BUILD_PROGRESS_EMAILS": _progress_email_sent,
    "ENABLE_TEST_BUILDS": _test_build_trigger_armed,
    "AGENT_RECONCILIATION_ENABLED": _reconciliation_trigger_armed,
    "AGENT_RECONCILIATION_ENABLED (dispatch)": lambda: tick._is_agent_enabled("reconciliation", False),
}

# Per-agent toggles: an arming switch that is ON when unset.
AGENT_TOGGLES = {
    "infra_engineer": "AGENT_INFRA_ENABLED",
    "financial": "AGENT_FINANCIAL_ENABLED",
    "customer_service": "AGENT_CUSTOMER_ENABLED",
    "documentation": "AGENT_DOCS_ENABLED",
    "security": "AGENT_SECURITY_ENABLED",
    "qa_test": "AGENT_QA_ENABLED",
    "backend_engineer": "AGENT_BACKEND_ENABLED",
    "cost_analyst": "AGENT_COST_ENABLED",
    "architect": "AGENT_ARCHITECT_ENABLED",
}


def _env_name(label: str) -> str:
    return label.split(" ")[0]


def _cases(readers: dict, expected: dict) -> list:
    return [pytest.param(label, value, expected[value], id=f"{label}={value!r}")
            for label in sorted(readers) for value in VALUES]


@pytest.fixture(autouse=True)
def _clean_switches(monkeypatch):
    for label in (*SAFETY_ON_READERS, *SAFETY_OFF_READERS, *ARMING_READERS):
        monkeypatch.delenv(_env_name(label), raising=False)
    for name in AGENT_TOGGLES.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.mark.parametrize("label, value, expected", _cases(SAFETY_ON_READERS, SAFETY_ON_BY_DEFAULT))
def test_a_safety_switch_goes_off_only_for_an_explicit_off_word(label, value, expected, monkeypatch):
    monkeypatch.setenv(_env_name(label), value)
    assert SAFETY_ON_READERS[label]() is expected


@pytest.mark.parametrize("label", sorted(SAFETY_ON_READERS))
def test_a_safety_switch_is_on_when_unset(label):
    assert SAFETY_ON_READERS[label]() is True


@pytest.mark.parametrize("label, value, expected", _cases(SAFETY_OFF_READERS, SAFETY_OFF_BY_DEFAULT))
def test_an_opt_in_safety_switch_treats_any_other_set_value_as_on(label, value, expected, monkeypatch):
    monkeypatch.setenv(_env_name(label), value)
    assert SAFETY_OFF_READERS[label]() is expected


@pytest.mark.parametrize("label", sorted(SAFETY_OFF_READERS))
def test_an_opt_in_safety_switch_is_off_when_unset(label):
    assert SAFETY_OFF_READERS[label]() is False


@pytest.mark.parametrize("label, value, expected", _cases(ARMING_READERS, ARMING))
def test_an_arming_switch_goes_on_only_for_an_explicit_on_word(label, value, expected, monkeypatch):
    monkeypatch.setenv(_env_name(label), value)
    assert ARMING_READERS[label]() is expected


@pytest.mark.parametrize("label", sorted(ARMING_READERS))
def test_an_arming_switch_is_off_when_unset(label):
    assert ARMING_READERS[label]() is False


@pytest.mark.parametrize("agent, value, expected", [
    pytest.param(agent, value, ARMING[value], id=f"{name}={value!r}")
    for agent, name in sorted(AGENT_TOGGLES.items()) for value in VALUES
])
def test_a_per_agent_toggle_that_is_set_needs_an_explicit_on_word(agent, value, expected, monkeypatch):
    monkeypatch.setenv(AGENT_TOGGLES[agent], value)
    assert tick._is_agent_enabled(agent, False) is expected


@pytest.mark.parametrize("agent", sorted(AGENT_TOGGLES))
def test_a_per_agent_toggle_that_is_unset_leaves_the_agent_on(agent):
    assert tick._is_agent_enabled(agent, False) is True


def test_the_reconciliation_trigger_and_its_dispatch_gate_read_one_rule(monkeypatch):
    """The trigger used to accept only "true" while the dispatch gate accepted
    1 and yes as well, so AGENT_RECONCILIATION_ENABLED=1 enabled an agent that
    was never given work."""
    for value in VALUES:
        monkeypatch.setenv("AGENT_RECONCILIATION_ENABLED", value)
        assert _reconciliation_trigger_armed() is tick._is_agent_enabled("reconciliation", False), value


# ---------------------------------------------------------------------------
# USE_MAGIC_LINK_AUTH: four readers (the worker, cli/build.py, cli/test.py,
# cli/deploy.py), one rule
# ---------------------------------------------------------------------------

_MAGIC_LINK_PROBE = (
    "import sys\n"
    "sys.path[:0] = [sys.argv[1] + '/cli', sys.argv[1] + '/queue', sys.argv[1]]\n"
    "import build, deploy, worker_pg\n"
    "import os\n"
    "print(build.USE_MAGIC_LINK_AUTH, deploy.magic_link_auth_enabled(),\n"
    "      worker_pg.USE_MAGIC_LINK_AUTH, repr(os.environ.get('USE_MAGIC_LINK_AUTH')))\n"
)


def _magic_link_readers_in_a_fresh_process(value):
    """What cli/build.py, cli/deploy.py and the worker each make of the
    variable when a new process starts with it set to `value` (None: unset),
    and the variable as that process then holds it."""
    import subprocess

    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if value is not None:
        env["USE_MAGIC_LINK_AUTH"] = value
    run = subprocess.run(
        [sys.executable, "-c", _MAGIC_LINK_PROBE, str(ROOT)],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(ROOT / "tests"),
    )
    assert run.returncode == 0, run.stderr[-2000:]
    return run.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("value, enabled", [(None, True), ("1", True), ("garbage", True), ("0", False)])
def test_the_generator_the_deploy_code_and_the_worker_read_the_magic_link_switch_alike(value, enabled):
    """cli/build.py used to accept only the literal "true": started on its own
    with USE_MAGIC_LINK_AUTH=1 it built a password login while cli/deploy.py
    probed a magic-link one. All of them now read it through safety_switch,
    and nothing rewrites the variable to make them agree."""
    stored = repr(value)
    assert _magic_link_readers_in_a_fresh_process(value) == f"{enabled} {enabled} {enabled} {stored}"


def _calls(source: str, function: str) -> int:
    return sum(
        1 for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == function
    )


def test_cli_build_and_cli_test_take_the_magic_link_switch_from_the_one_reader():
    build_source = (ROOT / "cli" / "build.py").read_text(encoding="utf-8")
    assigned = [
        node for node in ast.parse(build_source).body
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "USE_MAGIC_LINK_AUTH" for target in node.targets)
    ]
    assert len(assigned) == 1
    assert isinstance(assigned[0].value, ast.Call) and assigned[0].value.func.id == "magic_link_auth_enabled"

    test_source = (ROOT / "cli" / "test.py").read_text(encoding="utf-8")
    assert _calls(test_source, "magic_link_auth_enabled") == 1
    for source in (build_source, test_source):
        assert '"USE_MAGIC_LINK_AUTH"' not in source, "read through cli/deploy.py's magic_link_auth_enabled()"


# ---------------------------------------------------------------------------
# The parser itself
# ---------------------------------------------------------------------------

def test_an_unrecognised_value_is_logged_with_the_variable_name(monkeypatch, caplog):
    monkeypatch.setenv("COORDINATOR_DRY_RUN", "flase")
    monkeypatch.setenv("COORDINATOR_ENABLED", "ture")
    with caplog.at_level("WARNING", logger="envflags"):
        assert envflags.safety_switch("COORDINATOR_DRY_RUN") is True
        assert envflags.arming_switch("COORDINATOR_ENABLED") is False
    text = caplog.text
    assert "COORDINATOR_DRY_RUN" in text and "stays ON" in text
    assert "COORDINATOR_ENABLED" in text and "stays OFF" in text


def test_recognised_values_are_not_logged(monkeypatch, caplog):
    with caplog.at_level("WARNING", logger="envflags"):
        for value in ("true", "1", "yes", "on", "enabled", "false", "0", "no", "off", "disabled", ""):
            monkeypatch.setenv("X_SWITCH", value)
            envflags.safety_switch("X_SWITCH")
            envflags.arming_switch("X_SWITCH")
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# No boolean read goes around the parser
# ---------------------------------------------------------------------------

SCANNED = ("queue", "monitor", "ops", "coordinator", "cli")

# Reads of an environment variable that are compared with text, lower-cased or
# cast to bool without the parser, as (file, variable): reason. Each is a value
# or the presence of a value, not an on/off switch.
NOT_A_SWITCH = {
    ("queue/worker_pg.py", "CAS_BUILD_ENGINE"): "an engine name (v1 or v2), read at the claim and at the build stage",
    ("coordinator/spend_tracker.py", "ANTHROPIC_ADMIN_KEY"): "whether a credential is present",
    ("cli/test.py", "RAILWAY_ENVIRONMENT"): "whether the process runs on Railway (the variable is present)",
}

# On/off switches the frozen v1 engine (cli/) parses by hand, left as they
# are, as (file, variable): the rule that code applies. None of them is read
# outside the file or files listed for it, so no two readers of one variable
# use different rules. USE_MAGIC_LINK_AUTH is not here: the worker and
# cli/deploy.py read it too, so cli/build.py and cli/test.py take it from
# cli/deploy.py's magic_link_auth_enabled().
FROZEN_V1_READS = {
    ("cli/api_client.py", "CAS_ALLOW_TEST_HOOKS"): 'a test hook: on only for the literal "1"',
    ("cli/api_client.py", "DISABLE_BATCH_API"): 'batching goes off only for "true" in any case; a typo leaves it on',
    ("cli/build.py", "BLOCK_ON_SCHEMA_DRIFT"): "the safety rule restated: only false, 0, no, off or disabled demotes the gate",
    ("cli/build.py", "CAS_CONTRACTS_COLLECT"): 'a diagnostic mode: on only for the literal "1"',
    ("cli/contracts/run.py", "CAS_CONTRACTS_COLLECT"): 'the same diagnostic mode, the same rule: on only for the literal "1"',
    ("cli/build_trace.py", "BUILD_TRACE_DISABLED"): "tracing goes off only for 1, true or yes; a typo leaves it on",
    ("cli/deploy_fix.py", "USE_ADVISOR_DEPLOY_FIX"): "a model choice in a v1 fix loop: on for any text but false, 0 or no",
    ("cli/design_fix.py", "USE_ADVISOR_DESIGN_FIX"): "a model choice in a v1 fix loop: on for any text but false, 0 or no",
    ("cli/fix.py", "USE_ADVISOR_FIX"): "a model choice in a v1 fix loop: on for any text but false, 0 or no",
    ("cli/qa_fix.py", "USE_ADVISOR_QA_FIX"): "a model choice in a v1 fix loop: on for any text but false, 0 or no",
    ("cli/ui_test.py", "BLOCKING_UI_TEST_CONTRAST"): 'a v1 gate that is off by default: on only for "true" in any case',
}

# Module-level NAME = "text" assignments of the file being scanned, so a read
# through a named constant is reported under the variable it names.
_STRING_CONSTANTS: dict = {}


def _variable_name(node) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in _STRING_CONSTANTS:
        return _STRING_CONSTANTS[node.id]
    return "?"


def _env_read_name(node):
    """The variable name when `node` is os.environ.get(...) / os.getenv(...) /
    os.environ[...] (under any module alias), else None."""
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) \
            and node.value.attr == "environ":
        return _variable_name(node.slice)
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    is_get = isinstance(func, ast.Attribute) and func.attr == "get" \
        and isinstance(func.value, ast.Attribute) and func.value.attr == "environ"
    is_getenv = (isinstance(func, ast.Attribute) and func.attr == "getenv") \
        or (isinstance(func, ast.Name) and func.id == "getenv")
    if not (is_get or is_getenv):
        return None
    return _variable_name(node.args[0])


def _unwrap(node):
    """(env variable name or None, whether .lower()/.upper() was applied)."""
    lowered = False
    while True:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("lower", "upper", "strip", "casefold"):
            lowered = lowered or node.func.attr in ("lower", "upper", "casefold")
            node = node.func.value
            continue
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                name, inner = _unwrap(value)
                if name:
                    return name, lowered or inner
            return None, lowered
        return _env_read_name(node), lowered


def _is_text(node) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(_is_text(element) for element in node.elts)
    return False


def _hand_parsed_reads(source: str) -> set:
    found = set()
    tree = ast.parse(source)
    _STRING_CONSTANTS.clear()
    _STRING_CONSTANTS.update({
        node.targets[0].id: node.value.value for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    })
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            names = [_unwrap(operand)[0] for operand in operands]
            if any(names) and any(_is_text(operand) for operand in operands):
                found.update(name for name in names if name)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "bool" \
                and node.args and _unwrap(node.args[0])[0]:
            found.add(_unwrap(node.args[0])[0])
        elif isinstance(node, ast.Call):
            name, lowered = _unwrap(node)
            if name and lowered:
                found.add(name)
    return found


def _scanned_files():
    for entry in SCANNED:
        path = ROOT / entry
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for file in files:
            yield file.relative_to(ROOT).as_posix(), file.read_text(encoding="utf-8")


@pytest.mark.parametrize("snippet, variable", [
    ('import os\nx = os.environ.get("A_FLAG", "true").lower() == "true"\n', "A_FLAG"),
    ('import os\nif os.environ.get("A_FLAG", "").lower() != "true":\n    pass\n', "A_FLAG"),
    ('import os\nx = os.environ.get("A_FLAG") == "true"\n', "A_FLAG"),
    ('import os\nx = os.environ.get("A_FLAG", "").lower() in ("true", "1", "yes")\n', "A_FLAG"),
    ('import os as _os\nraw = _os.environ.get("A_FLAG", "").strip().lower()\n', "A_FLAG"),
    ('import os\nx = os.getenv("A_FLAG", "").lower() == "true"\n', "A_FLAG"),
    ('import os\nx = bool(os.environ.get("A_FLAG", ""))\n', "A_FLAG"),
    ('import os\nx = (os.environ.get("A_FLAG") or "v2").strip().lower()\n', "A_FLAG"),
    ('import os\nx = os.environ["A_FLAG"] == "1"\n', "A_FLAG"),
    ('import os\n_NAME = "A_FLAG"\nx = os.environ.get(_NAME, "").lower() in ("1", "true")\n', "A_FLAG"),
])
def test_the_scan_sees_each_way_of_parsing_a_switch_by_hand(snippet, variable):
    assert _hand_parsed_reads(snippet) == {variable}


def test_no_boolean_environment_read_goes_around_the_parser():
    found = {(rel, name) for rel, source in _scanned_files() for name in _hand_parsed_reads(source)}
    listed = set(NOT_A_SWITCH) | set(FROZEN_V1_READS)
    unlisted = sorted(found - listed)
    assert unlisted == [], (
        "an environment variable is compared with text, lower-cased or cast to bool without "
        "coordinator.envflags (use safety_switch or arming_switch, or list it in NOT_A_SWITCH "
        f"with the reason it is not an on/off switch): {unlisted}"
    )
    stale = sorted(listed - found)
    assert stale == [], f"listed but no longer in the code (remove the entry): {stale}"


def test_the_frozen_list_holds_only_v1_engine_files_and_never_a_shared_switch():
    """The list is for switches only the v1 engine reads. A variable that the
    worker, the deploy code, ops or the coordinator also reads has to go
    through the parser everywhere, or two readers can disagree again."""
    assert not set(NOT_A_SWITCH) & set(FROZEN_V1_READS)
    for rel, name in FROZEN_V1_READS:
        assert rel.startswith("cli/") and rel != "cli/deploy.py", rel
    frozen_names = {name for _rel, name in FROZEN_V1_READS}
    assert "USE_MAGIC_LINK_AUTH" not in frozen_names
    outside_v1 = "\n".join(
        source for rel, source in _scanned_files() if not rel.startswith("cli/") or rel == "cli/deploy.py"
    )
    shared = sorted(name for name in frozen_names if f'"{name}"' in outside_v1)
    assert shared == [], f"read outside the v1 engine as well, so it cannot stay hand-parsed: {shared}"


# ---------------------------------------------------------------------------
# The web tier's copy of the word lists (the admin settings page)
# ---------------------------------------------------------------------------

def _typescript_word_list(source: str, name: str) -> frozenset:
    import re

    match = re.search(rf"export const {name}: readonly string\[\] = \[([^\]]*)\];", source)
    assert match, f"{name} is not declared as a string array literal in web/src/utils/env-switch.ts"
    return frozenset(re.findall(r"'([^']*)'", match.group(1)))


def test_the_web_tiers_word_lists_are_the_parsers():
    """The settings page shows each switch as the worker will read it, from
    its own copy of the lists. web/tests/unit/env-switch.test.ts makes the
    same comparison from the other side and runs both parsers over one table."""
    source = (ROOT / "web" / "src" / "utils" / "env-switch.ts").read_text(encoding="utf-8")
    assert _typescript_word_list(source, "ON_WORDS") == envflags.ON_WORDS
    assert _typescript_word_list(source, "OFF_WORDS") == envflags.OFF_WORDS
