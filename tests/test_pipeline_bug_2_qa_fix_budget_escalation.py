"""The switch behind "a qa_fix budget overrun fails the build".

Background: a v1 build once burned its qa_fix loop budget on a hung model
call, the worker logged a 'qa_fix error' warning, and the build went on to
deploy with the unfixed server.js and seed.sql. The worker now re-raises in
that case (the handlers for FixLoopBudgetExceeded and for an API timeout in
the qa_fix section of queue/worker_pg.py:_run_pipeline), unless
BLOCK_ON_QA_FIX_BUDGET is set to a rollback token.

What this file tests is the switch: worker_pg._block_gate_on, called directly,
for the unset value, for values that are not rollback tokens, and for each
rollback token.

What it does not test is the re-raise itself. Those handlers sit inline in
_run_pipeline, and no test in this repository drives the v1 pipeline into the
qa_fix loop with its budget exhausted. This file used to cover them by
searching the worker's source for the words "raise" and
"BLOCK_ON_QA_FIX_BUDGET" near the handler, which passes whatever the handler
does; those checks were removed rather than kept as if they were coverage.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# v1-only: the gate belongs to the v1 engine's QA fix loop. Skipped unless
# CAS_BUILD_ENGINE=v1 (see the root conftest.py).
pytestmark = pytest.mark.v1_only

sys.path.insert(0, str(Path(__file__).parent.parent / "queue"))


def test_block_on_qa_fix_budget_defaults_to_blocking(monkeypatch):
    """Unset, the gate is on. A value that is not a rollback token (a typo,
    'True' with a capital, an empty string) leaves it on too."""
    import worker_pg

    monkeypatch.delenv("BLOCK_ON_QA_FIX_BUDGET", raising=False)
    assert worker_pg._block_gate_on("BLOCK_ON_QA_FIX_BUDGET") is True
    for value in ("True", "true", "1", "yes", "flase", ""):
        monkeypatch.setenv("BLOCK_ON_QA_FIX_BUDGET", value)
        assert worker_pg._block_gate_on("BLOCK_ON_QA_FIX_BUDGET") is True, (
            f"{value!r} is not a rollback token and must leave the gate on"
        )


def test_block_on_qa_fix_budget_rollback_tokens_demote_to_warn(monkeypatch):
    """The only strings that turn the gate off are the known rollback tokens,
    in any case and with surrounding spaces."""
    import worker_pg

    for token in ("false", "0", "no", "off", "disabled", "FALSE", "  no  "):
        monkeypatch.setenv("BLOCK_ON_QA_FIX_BUDGET", token)
        assert worker_pg._block_gate_on("BLOCK_ON_QA_FIX_BUDGET") is False, (
            f"rollback token {token!r} must demote the gate"
        )
