"""Which coordinator tasks can make a model call, and the proof that the others cannot.

The tick's budget is an allowance of model calls (coordinator/budget.py). When
spend cannot be read, or the day is spent, the allowance is zero, and every
task used to be skipped on the estimate of the model call it might make: the
rule-based tasks that make none (an app-down alert among them) went silent
with the rest. Tasks that cannot make a model call now draw on a private,
unmetered allowance (coordinator/tick.py: _task_may_call_model), and the ones
that can stay behind the budget, whose meter grants no call at zero.

"Cannot make a model call" is a claim about source code, so it is checked
there. The checks:

  1. the only coordinator modules that hold a model client are three, and each
     gates every call on Actuators.authorize_model_call; the modules that
     import one of them are listed;
  2. for each agent, the categories that its handlers can take to a model call
     (by following `self.method(...)` and same-module calls from the handler of
     each category, and imports of a module that reaches a model client) are
     exactly the categories the class declares in `model_categories`;
  3. every agent declares it, and a class that declares nothing is treated as
     one that may call a model in every category.

The analysis follows names inside one class and one module; a call made
through a name it cannot resolve to either (a callback passed in, a getattr)
is not seen. The second test of section 2 therefore fails on a handler that is
declared model-free and calls getattr, eval, exec or __import__.
"""

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COORDINATOR = ROOT / "coordinator"
AGENTS = COORDINATOR / "agents"

# The calls that reach a model.
GATE = "authorize_model_call"
MODEL_CLIENT_MODULES = {
    "coordinator.failure_kb_sync",             # the weekly failure-KB sync (an infra initiative)
    "coordinator.build_failure_categorizer",   # failure triage in work detection
    "coordinator.agents.customer_service",     # the support-answer draft
}
# The modules that import one of those. actuators takes a constant from the
# categorizer; the other two use the model through the gate.
IMPORTERS_OF_A_MODEL_MODULE = {
    "coordinator.actuators", "coordinator.agents.infra_engineer", "coordinator.work_detection"}


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _module_name(path):
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _imports(tree):
    """Every module an AST imports from (anywhere in it, function bodies included)."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


# ---------------------------------------------------------------------------
# 1. Who holds a model client
# ---------------------------------------------------------------------------

def _modules_that_build_a_model_client():
    holders = set()
    for path in sorted(COORDINATOR.rglob("*.py")):
        tree = _parse(path)
        if "anthropic" in _imports(tree) or any(
                isinstance(n, ast.Attribute) and n.attr == "create"
                and isinstance(n.value, ast.Attribute) and n.value.attr == "messages"
                for n in ast.walk(tree)):
            holders.add(_module_name(path))
    return holders


def test_only_three_modules_of_the_coordinator_hold_a_model_client():
    assert _modules_that_build_a_model_client() == MODEL_CLIENT_MODULES


def test_the_modules_that_import_one_of_them_are_the_ones_listed():
    """A new importer has to be added to the list above, which is a reason to
    look at what it does with the import."""
    importers = {_module_name(path) for path in COORDINATOR.rglob("*.py")
                 if _imports(_parse(path)) & MODEL_CLIENT_MODULES}
    assert importers == IMPORTERS_OF_A_MODEL_MODULE


@pytest.mark.parametrize("module", sorted(MODEL_CLIENT_MODULES))
def test_each_of_them_gates_its_calls_on_the_actuator_layer(module):
    source = (ROOT / (module.replace(".", "/") + ".py")).read_text(encoding="utf-8")
    assert f".{GATE}(" in source


# ---------------------------------------------------------------------------
# 2. What each agent's handlers can reach
# ---------------------------------------------------------------------------

def _agent_classes():
    """(module name, ClassDef, module AST) for every Agent subclass in coordinator/agents/."""
    for path in sorted(AGENTS.glob("*.py")):
        if path.name in ("__init__.py", "base.py"):
            continue
        tree = _parse(path)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and any(
                    isinstance(base, ast.Name) and base.id == "Agent" for base in node.bases):
                yield _module_name(path), node, tree


def _category_handlers(cls):
    """{category: method name} read from the `if category == "x": return self._handle_x(...)` chain of execute()."""
    execute = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "execute")
    handlers = {}
    for node in ast.walk(execute):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and len(node.test.ops) == 1 \
                and isinstance(node.test.ops[0], ast.Eq) and isinstance(node.test.left, ast.Name) \
                and node.test.left.id == "category" and isinstance(node.test.comparators[0], ast.Constant):
            category = node.test.comparators[0].value
            for call in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                        and isinstance(call.func.value, ast.Name) and call.func.value.id == "self":
                    handlers[category] = call.func.attr
                    break
    return handlers


def _reaches_a_model(entry, cls, tree, reach_modules):
    """Whether code reachable from method `entry` calls the gate or uses a module that reaches a model."""
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    names_from_model_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in reach_modules:
            names_from_model_modules.update(alias.asname or alias.name for alias in node.names)
    seen, queue, reaches, dynamic = set(), [entry], False, []
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        node = methods.get(name) or functions.get(name)
        if node is None:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.ImportFrom) and child.module in reach_modules:
                reaches = True
            if isinstance(child, ast.Attribute) and child.attr == GATE:
                reaches = True
            if isinstance(child, ast.Name) and child.id in names_from_model_modules:
                reaches = True
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "self":
                    queue.append(func.attr)
                elif isinstance(func, ast.Name):
                    queue.append(func.id)
                    if func.id in ("getattr", "eval", "exec", "__import__"):
                        dynamic.append(func.id)
    return reaches, dynamic


def test_every_agent_declares_which_categories_may_call_a_model():
    declared = {}
    for module, cls, _tree in _agent_classes():
        assignment = [n for n in cls.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "model_categories" for t in n.targets)]
        assert assignment, f"{cls.name} ({module}) does not declare model_categories"
        declared[cls.name] = assignment[0].value
    assert len(declared) >= 10, sorted(declared)


def test_the_declared_categories_are_exactly_the_ones_that_reach_a_model():
    from coordinator.agents.base import Agent
    import importlib

    reach = MODEL_CLIENT_MODULES
    checked = 0
    for module, cls, tree in _agent_classes():
        agent_class = getattr(importlib.import_module(module), cls.name)
        declared = agent_class.model_categories
        assert isinstance(declared, frozenset), f"{cls.name} declares {declared!r}"
        handlers = _category_handlers(cls)
        if not handlers:
            # An agent that does the same thing whatever the category (the
            # reconciliation sweep): execute() is the one handler, and it must
            # reach no model call.
            handlers = {"*": "execute"}
        reaching = set()
        for category, entry in handlers.items():
            reaches, dynamic = _reaches_a_model(entry, cls, tree, reach)
            # A handler that is already known to reach a model needs no further
            # analysis; one that does not must not hide a call in a dynamic one.
            assert reaches or not dynamic, f"{cls.name}.{entry} calls {dynamic}: the analysis cannot follow it"
            if reaches:
                reaching.add(category)
            checked += 1
        assert "*" not in reaching, f"{cls.name} does not dispatch by category and reaches a model call"
        assert declared == reaching, (
            f"{cls.name} declares {sorted(declared)} but its handlers reach a model call in {sorted(reaching)}")
    assert checked >= 20
    assert Agent.model_categories is None


def test_the_analysis_finds_a_model_call_it_is_given():
    """The analysis is not trivially empty: it sees the gate in a handler it is shown."""
    source = (
        "class A(Agent):\n"
        "    def execute(self, task, state, model, actuators):\n"
        "        category = task.category\n"
        "        if category == 'x':\n"
        "            return self._x(actuators)\n"
        "        elif category == 'y':\n"
        "            return self._y(actuators)\n"
        "    def _x(self, actuators):\n"
        "        return self._helper(actuators)\n"
        "    def _helper(self, actuators):\n"
        "        return actuators.authorize_model_call('p')\n"
        "    def _y(self, actuators):\n"
        "        return 1\n"
    )
    tree = ast.parse(source)
    cls = tree.body[0]
    assert _category_handlers(cls) == {"x": "_x", "y": "_y"}
    assert _reaches_a_model("_x", cls, tree, set())[0] is True
    assert _reaches_a_model("_y", cls, tree, set())[0] is False


# ---------------------------------------------------------------------------
# 3. The tick reads the declaration
# ---------------------------------------------------------------------------

@pytest.fixture()
def registry():
    from coordinator import tick

    saved = dict(tick._agent_registry)
    tick._agent_registry.clear()
    yield tick._agent_registry
    tick._agent_registry.clear()
    tick._agent_registry.update(saved)


def test_a_class_that_declares_nothing_may_call_a_model_in_every_category(registry):
    from coordinator import tick

    class Silent:
        pass

    registry["silent"] = Silent
    assert tick._task_may_call_model("silent", "anything") is True
    assert tick._task_may_call_model("not-registered", "anything") is True


def test_something_that_is_not_a_declaration_is_not_one(registry):
    from unittest.mock import MagicMock
    from coordinator import tick

    registry["stand_in"] = MagicMock()          # its model_categories is a MagicMock, which "contains" nothing
    assert tick._task_may_call_model("stand_in", "anything") is True


def test_the_declared_categories_decide(registry):
    from coordinator import tick
    from coordinator.agents.customer_service import CustomerService
    from coordinator.agents.infra_engineer import InfraEngineer

    registry["infra_engineer"] = InfraEngineer
    registry["customer_service"] = CustomerService
    assert tick._task_may_call_model("infra_engineer", "app_down") is False
    assert tick._task_may_call_model("infra_engineer", "backup_overdue") is False
    assert tick._task_may_call_model("infra_engineer", "agent_initiative") is True
    assert tick._task_may_call_model("customer_service", "customer_feedback") is True
