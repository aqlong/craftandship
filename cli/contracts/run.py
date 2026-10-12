"""Deterministic run_all() entry point that invokes every per-view contract.

Order (alphabetical): about, admin_crud, admin_dashboard, admin_settings,
home, layout, login.

Two modes:

  collect_all=False (production default)
    First ContractViolation raised by any module propagates, stops run_all.

  collect_all=True (pytest / dev loop)
    Each module appends violations into a shared list. When all modules
    have run, if the list is non-empty, run_all raises one aggregated
    ContractViolation whose message lists every violation, one per line.

Environment:
  CAS_CONTRACTS_COLLECT=1 forces collect_all=True regardless of the kwarg.
  The pytest conftest autouse fixture sets this so dev-loop tests see every
  broken contract in one go instead of fixing-and-rerunning once per error.
"""

from __future__ import annotations

import os

from cli.contracts import ContractViolation
from cli.contracts import about as _about
from cli.contracts import admin_crud as _admin_crud
from cli.contracts import admin_dashboard as _admin_dashboard
from cli.contracts import admin_settings as _admin_settings
from cli.contracts import home as _home
from cli.contracts import layout as _layout
from cli.contracts import login as _login
from cli.contracts import widget_helpers as _widget_helpers


# Alphabetical module list. Import order is irrelevant; this tuple is what
# drives run order, and it is sorted for determinism across Python runs.
_MODULES = (
    ("about", _about),
    ("admin_crud", _admin_crud),
    ("admin_dashboard", _admin_dashboard),
    ("admin_settings", _admin_settings),
    ("home", _home),
    ("layout", _layout),
    ("login", _login),
    ("widget_helpers", _widget_helpers),
)


def run_all(artifact, *, collect_all: bool = False) -> None:
    """Invoke every contract module's check() in deterministic order."""
    if os.environ.get("CAS_CONTRACTS_COLLECT") == "1":
        collect_all = True

    if not collect_all:
        # Fail-fast: first violation raises.
        for _name, module in _MODULES:
            module.check(artifact, collect_all=False, violations=None)
        return

    violations: list[str] = []
    for _name, module in _MODULES:
        try:
            module.check(artifact, collect_all=True, violations=violations)
        except ContractViolation as exc:
            # A module that hand-rolls a raise (e.g. file-missing paths)
            # instead of routing through the collector still surfaces here.
            violations.append(str(exc))
        except Exception as exc:
            # Unexpected error inside one module must not mask the others.
            # Append a descriptive violation string and continue.
            violations.append(
                f"contract module {_name!r} raised {type(exc).__name__}: {exc}"
            )

    if violations:
        joined = "\n".join(violations)
        raise ContractViolation(
            f"{len(violations)} violations:\n{joined}"
        )


__all__ = ["run_all"]
