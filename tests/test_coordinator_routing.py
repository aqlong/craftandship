"""The routing matrix: what work detection emits against what the agents handle.

coordinator/work_detection.py decides, per trigger, which agent a work item
goes to. Each agent's execute() dispatches on the item's category and answers
"Unknown category" for one it has no handler for, which the tick records as a
failed task. Nothing ties the two lists together, so a trigger can be routed
to an agent that refuses it, and it will fire, fail and be re-created on every
tick. Four triggers did exactly that before they were removed.

These tests tie the lists together from the code itself:

  * every trigger function defined in work_detection is called by detect_work
    (no trigger kept "in place" behind a commented-out call);
  * every (category, agent) pair a called trigger can emit is accepted by that
    agent's real execute(): the pairs are read from the trigger's source, and
    acceptance is observed by calling execute() with the handlers stubbed out,
    so no handler's side effects run;
  * every agent a trigger targets can actually be dispatched: it is
    registered, has a model entry and a permission entry;
  * the reverse direction holds with no exceptions: every category an agent's
    execute() dispatches on is one a trigger routes to that agent, every
    _handle_* method is reached by a work item a trigger emits, every
    registered agent is some trigger's target, and the scorer's weight table
    and the tick's timeout table name only categories that are emitted.

agent_tasks rows are created only from work detection's items and from an
agent's follow_up_work, and no agent returns follow-up work, so a branch no
trigger reaches could run only for a row inserted by hand. There are none.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import coordinator.agents as agents_package
from coordinator import tick, work_detection
from coordinator.actuators import AGENT_PERMISSIONS, NON_AGENT_PRINCIPALS, Actuators
from coordinator.agents.base import AgentResult
from coordinator.scorer import AGENT_MODELS, CATEGORY_WEIGHTS
from coordinator.state import WorkItem, WorldState

_SOURCE = ast.parse(inspect.getsource(work_detection))
_FUNCTIONS = {node.name: node for node in _SOURCE.body if isinstance(node, ast.FunctionDef)}


def _defined_triggers() -> set:
    return {name for name in _FUNCTIONS if name.startswith("_check_")}


def _called_triggers() -> set:
    """The _check_* functions detect_work refers to (directly or through _run_check)."""
    return {node.id for node in ast.walk(_FUNCTIONS["detect_work"])
            if isinstance(node, ast.Name) and node.id.startswith("_check_")}


def _returned_strings(function_name: str) -> set:
    return {node.value.value for node in ast.walk(_FUNCTIONS[function_name])
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)}


# A trigger that picks its target agent at run time, with every value the
# variable can take. Anything else must be a string literal in the source.
VARIABLE_TARGETS = {
    ("_check_failed_builds", "target_agent"): _returned_strings("_route_build_failure"),
    ("_check_agent_initiative", "next_agent"): set(work_detection._AGENT_ROTATION),
}


def _emitted_pairs() -> set:
    """(category, agent) for every _make_item call in a called trigger."""
    pairs = set()
    for trigger in sorted(_called_triggers()):
        calls = [node for node in ast.walk(_FUNCTIONS[trigger])
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == "_make_item"]
        assert calls, f"{trigger} is called by detect_work but creates no work item"
        for call in calls:
            category, target = call.args[0], call.args[1]
            assert isinstance(category, ast.Constant), f"{trigger}: the category must be a literal"
            if isinstance(target, ast.Constant):
                targets = {target.value}
            else:
                key = (trigger, ast.unparse(target))
                assert key in VARIABLE_TARGETS, (
                    f"{trigger} routes to the variable {key[1]!r}; list its values in VARIABLE_TARGETS")
                targets = VARIABLE_TARGETS[key]
            pairs |= {(category.value, agent) for agent in targets}
    return pairs


@pytest.fixture(scope="module")
def registry():
    """The agent registry as the worker builds it: every module under
    coordinator/agents/ that has a register() function."""
    saved = dict(tick._agent_registry)
    tick._agent_registry.clear()
    try:
        for module_info in pkgutil.iter_modules(agents_package.__path__):
            module = importlib.import_module(f"coordinator.agents.{module_info.name}")
            if hasattr(module, "register"):
                module.register()
        yield dict(tick._agent_registry)
    finally:
        tick._agent_registry.clear()
        tick._agent_registry.update(saved)


_HANDLED = "reached a handler"


_WHOLE_AGENT = "execute"


def _handlers(agent_cls) -> list:
    return sorted(name for name in dir(agent_cls) if name.startswith("_handle"))


def _handler_for(agent_cls, category: str):
    """The _handle_* method agent_cls.execute() dispatches `category` to, or
    None when it answers "Unknown category".

    The agent's own execute() runs; every _handle_* method is replaced by a
    stub that reports its name, so the answer is the dispatch decision and
    nothing else happens. An agent with no _handle_* method and no look at the
    category (the reconciliation agent: one job, whatever the item says)
    accepts anything; for it the answer is "execute".
    """
    agent = agent_cls()
    handlers = _handlers(agent_cls)
    if not handlers:
        looks_at_category = any(
            isinstance(node, ast.Attribute) and node.attr == "category"
            for node in ast.walk(ast.parse(inspect.getsource(agent_cls))))
        assert not looks_at_category, f"{agent_cls.__name__} branches on category without _handle_* methods"
        return _WHOLE_AGENT

    def stub(name):
        return lambda *args, **kwargs: AgentResult(success=True, output={"routing": _HANDLED, "handler": name})

    for name in handlers:
        setattr(agent, name, stub(name))
    task = WorkItem(id="routing-task", category=category, target_agent=agent_cls.name,
                    description="routing matrix", context={})
    result = agent.execute(task, WorldState(), "claude-haiku-4-5",
                           Actuators(agent_cls.name, "routing-task", dry_run=True))
    if result.output.get("routing") == _HANDLED:
        return result.output["handler"]
    assert "Unknown category" in str(result.output.get("error", "")), (
        f"{agent_cls.__name__}.execute({category!r}) neither reached a handler nor refused: {result.output}")
    return None


def _accepts(agent_cls, category: str) -> bool:
    """Does agent_cls.execute() dispatch `category` to a handler?"""
    return _handler_for(agent_cls, category) is not None


def _dispatched_categories(agent_cls) -> set:
    """The category literals an agent's execute() compares against."""
    found = set()
    for node in ast.walk(ast.parse(inspect.getsource(agent_cls.execute).lstrip())):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "category":
            for comparator in node.comparators:
                for constant in ast.walk(comparator):
                    if isinstance(constant, ast.Constant) and isinstance(constant.value, str):
                        found.add(constant.value)
    return found


def test_every_trigger_that_is_defined_is_called():
    defined, called = _defined_triggers(), _called_triggers()
    assert called <= defined
    assert defined - called == set(), (
        "trigger functions that detect_work never calls (wire them or delete them): "
        f"{sorted(defined - called)}")
    assert len(defined) == 17


def test_every_emitted_work_item_is_accepted_by_the_agent_it_is_routed_to(registry):
    pairs = _emitted_pairs()
    assert len(pairs) >= 20, "the source scan found too few work items to mean anything"
    refused = sorted((category, agent) for category, agent in pairs
                     if agent not in registry or not _accepts(registry[agent], category))
    assert refused == [], (
        "work items a trigger emits that the target agent answers with 'Unknown category' "
        f"(each firing would record a failed task): {refused}")


def test_the_acceptance_probe_can_tell_a_refusal(registry):
    """The probe above is only worth something if it reports the refusals the
    removed triggers ran into."""
    assert _accepts(registry["infra_engineer"], "build_failure") is True
    assert _accepts(registry["infra_engineer"], "kb_pattern_degrading") is False
    assert _accepts(registry["infra_engineer"], "self_improvement") is False
    assert _accepts(registry["qa_test"], "learning_validation") is False
    assert _accepts(registry["documentation"], "persona_update_proposal") is False
    assert _accepts(registry["reconciliation"], "reconciliation_sweep") is True
    assert _handler_for(registry["infra_engineer"], "app_down") == "_handle_app_down"


@pytest.mark.parametrize("agent, category", [
    ("backend_engineer", "auto_approve_preview"), ("backend_engineer", "platform_improvement"),
    ("backend_engineer", "rebuild_app"), ("backend_engineer", "deploy_failure"),
    ("infra_engineer", "pipeline_idle"), ("cost_analyst", "cost_optimization"),
    ("documentation", "docs_sync"), ("qa_test", "regression_check"), ("qa_test", "post_deploy_audit"),
    ("financial", "cost_report"),
])
def test_a_category_no_trigger_routes_to_an_agent_is_refused_by_it(registry, agent, category):
    """These had a dispatch branch and no trigger. A row carrying one now
    fails as an unknown category instead of running a handler nothing tests
    through the tick."""
    assert (category, agent) not in _emitted_pairs()
    assert _accepts(registry[agent], category) is False


def test_the_coordinator_files_no_roadmap_work(registry):
    """Roadmap items belong to the local executor (scripts/cas-roadmap-loop.sh).
    With a coordinator trigger for them as well, every item was dispatched
    twice; no trigger emits the category and no agent accepts it."""
    assert "roadmap_item" not in {category for category, _agent in _emitted_pairs()}
    for agent, agent_cls in registry.items():
        if _handlers(agent_cls):
            assert _accepts(agent_cls, "roadmap_item") is False, agent


def test_every_targeted_agent_can_be_dispatched(registry):
    for agent in sorted({agent for _category, agent in _emitted_pairs()}):
        assert agent in registry, f"{agent} is a routing target but no module registers it"
        assert agent in AGENT_MODELS, f"{agent} has no model entry, so select_model() skips every task for it"
        assert agent in AGENT_PERMISSIONS and agent not in NON_AGENT_PRINCIPALS
        assert registry[agent].name == agent


def test_every_dispatch_branch_is_one_a_trigger_routes_to_that_agent(registry):
    """No exception list. A branch for a category no trigger sends to this
    agent is code that runs only for a row inserted by hand."""
    emitted = _emitted_pairs()
    unreached = sorted((agent, category)
                       for agent, agent_cls in registry.items()
                       for category in _dispatched_categories(agent_cls)
                       if (category, agent) not in emitted)
    assert unreached == [], (
        "dispatch branches no trigger reaches (delete the branch and its handler, or add the trigger): "
        f"{unreached}")


def test_every_handler_is_reached_by_a_work_item_a_trigger_emits(registry):
    """The other half: a _handle_* method that no emitted work item is
    dispatched to. Observed by running each agent's real execute() for every
    category a trigger routes to it."""
    emitted = _emitted_pairs()
    unreachable = []
    for agent, agent_cls in sorted(registry.items()):
        reached = {_handler_for(agent_cls, category) for category, target in emitted if target == agent}
        unreachable += [(agent, name) for name in _handlers(agent_cls) if name not in reached]
    assert unreachable == [], f"handlers no trigger reaches: {unreachable}"


def test_every_registered_agent_is_some_triggers_target(registry):
    targeted = {agent for _category, agent in _emitted_pairs()}
    assert sorted(set(registry) - targeted) == [], "registered agents that are never given work"
    assert set(registry) == set(AGENT_PERMISSIONS) - NON_AGENT_PRINCIPALS


def test_the_weight_and_timeout_tables_name_only_emitted_categories():
    """A weight or a timeout for a category nothing emits is a setting for
    code that cannot run."""
    emitted = {category for category, _agent in _emitted_pairs()}
    assert set(CATEGORY_WEIGHTS) == emitted
    assert set(tick.CATEGORY_TIMEOUTS) <= emitted
