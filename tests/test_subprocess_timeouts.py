"""Static enforcement: every subprocess.run(...) in cli/ has timeout=.

This is pure drift prevention. Research in claude-plan.md §2 confirmed
every current cli/*.py subprocess.run site already passes timeout=, so
this test lands green immediately. It exists to guard the invariant
against future commits that copy-paste a subprocess call without the
keyword argument.

The WATCHDOG1 Layer 2 plan replaces the ctypes async-exception watchdog
with cooperative cancellation plus a wall-clock budget in call_api. That
strategy only holds if every sidecar subprocess is also bounded, a
subprocess.run without timeout= inherits the OS default (never) and
silently reintroduces the same hang-forever hole the watchdog was trying
to close.

Pure stdlib. No network. No disk I/O beyond reading cli/*.py. Runtime
is well under one second.
"""

import ast
from pathlib import Path

_CLI_DIR = Path(__file__).resolve().parent.parent / "cli"


def _walk_cli_files():
    """Yield (relative_path, ast.Module) for every cli/*.py file.

    Uses rglob so subpackages of cli/ are covered. Skips __init__.py
    (empty marker) because there is nothing to walk.
    """
    root = _CLI_DIR.parent
    for path in sorted(_CLI_DIR.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(root)
        yield rel, tree


def _file_imports_run_from_subprocess(tree: ast.Module) -> bool:
    """True if the file contains `from subprocess import ..., run, ...`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name == "run":
                    return True
    return False


class TestSubprocessTimeouts:
    """Every subprocess.run call in cli/ must pass an explicit timeout=.

    The value of the timeout is not checked, any concrete value (even
    one that is too long) beats inheriting the OS default. The point is
    that every subprocess invocation is bounded so the supervisor can
    make a wall-clock guarantee about total build time.
    """

    def test_every_subprocess_run_has_timeout(self):
        offenders: list[str] = []

        for rel, tree in _walk_cli_files():
            has_bare_run_import = _file_imports_run_from_subprocess(tree)

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue

                matched = False
                # subprocess.run(...)
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                ):
                    matched = True
                # bare run(...) when `from subprocess import run`
                elif (
                    has_bare_run_import
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "run"
                ):
                    matched = True

                if not matched:
                    continue

                has_timeout = any(
                    kw.arg == "timeout" for kw in node.keywords
                )
                if not has_timeout:
                    offenders.append(
                        f"{rel}:{node.lineno}; subprocess.run(...) missing timeout="
                    )

        assert not offenders, (
            "Every subprocess.run(...) in cli/ must pass an explicit "
            "`timeout=` keyword argument. Inheriting the OS default "
            "reopens the hang-forever hole the WATCHDOG1 Layer 2 plan "
            "is meant to close.\n\nOffenders:\n  " + "\n  ".join(offenders)
        )
