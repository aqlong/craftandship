"""Bug 4 (critical review gate): critical code-review
findings block deploy.

Without the gate, a build with critical review findings (for example a
schema/query column mismatch that makes /health fail once deployed) is
logged at INFO level and proceeds to qa_fix/deploy anyway, and can then
run until the supervisor's tier deadline force-fails it. A clean
failure at review time saves the whole cycle.

The gate ONLY fires for findings matching known deploy-blocking
patterns. Generic "nitpick" criticals (style, comments, naming) still
proceed through the fix loops.
"""

from __future__ import annotations

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "queue"))

import worker_pg  # noqa: E402


# ---------------------------------------------------------------------------
# _matching_blocking_critical_findings helper
# ---------------------------------------------------------------------------


def test_column_mismatch_finding_flagged_as_blocking():
    """The column-mismatch case: review text says server.js queries a
    column that the schema doesn't define."""
    issue = {
        "severity": "critical",
        "file": "server.js",
        "issue": (
            "queries 'SELECT key, value FROM site_settings' but the "
            "schema defines the columns as 'setting_key' and 'setting_value'"
        ),
    }
    got = worker_pg._matching_blocking_critical_findings([issue])
    assert got == [issue]


def test_missing_route_finding_flagged_as_blocking():
    issue = {
        "severity": "critical",
        "file": "routes/admin.js",
        "issue": "admin dashboard references /admin/events but the route is missing",
    }
    got = worker_pg._matching_blocking_critical_findings([issue])
    assert got == [issue]


def test_skipped_seed_finding_flagged_as_blocking():
    issue = {
        "severity": "critical",
        "file": "db/seed.sql",
        "issue": (
            "The multi-line blog post INSERT is broken into multiple "
            "-- SKIPPED comments resulting in seed failure"
        ),
    }
    got = worker_pg._matching_blocking_critical_findings([issue])
    assert got == [issue]


def test_column_does_not_exist_in_message_text():
    """Real Postgres error phrasing that sometimes bleeds into review
    text as a critical finding."""
    issue = {
        "severity": "critical",
        "message": "column \"key\" does not exist",
    }
    got = worker_pg._matching_blocking_critical_findings([issue])
    assert got == [issue]


def test_generic_style_critical_NOT_blocking():
    """We do NOT want to fail the build on every critical. Style,
    comment, and naming issues that fix loops CAN recover from pass
    through."""
    issue = {
        "severity": "critical",
        "issue": "variable names use camelCase instead of snake_case",
    }
    got = worker_pg._matching_blocking_critical_findings([issue])
    assert got == []


def test_mixed_list_only_returns_blockers():
    blocker = {
        "severity": "critical",
        "issue": "route not found for /api/inquiries",
    }
    nitpick = {
        "severity": "critical",
        "issue": "long function should be broken up",
    }
    got = worker_pg._matching_blocking_critical_findings([blocker, nitpick])
    assert got == [blocker]


# ---------------------------------------------------------------------------
# Gate wiring in _process_job
# ---------------------------------------------------------------------------


def test_review_gate_defaults_to_blocking():
    """Behavioral: BLOCK_ON_CODE_REVIEW_CRITICAL resolves strict when
    unset. Uses the shared `_block_gate_on` helper rather than a
    literal-source grep so worker_pg refactors don't break the test."""
    import os
    os.environ.pop("BLOCK_ON_CODE_REVIEW_CRITICAL", None)
    assert worker_pg._block_gate_on("BLOCK_ON_CODE_REVIEW_CRITICAL") is True


def test_reviewcriticalblockerror_class_exists_and_propagates():
    """The exception class is defined at module level AND the review
    block re-raises it so _process_job's exception handler runs the
    clear-and-rebuild retry path (the only thing that actually fixes
    a schema/query drift)."""
    assert hasattr(worker_pg, "ReviewCriticalBlockError")
    assert issubclass(worker_pg.ReviewCriticalBlockError, Exception)
    src = (Path(__file__).parent.parent / "queue" / "worker_pg.py").read_text()
    # The Stage 2.5 review block's exception ladder must RE-RAISE
    # ReviewCriticalBlockError (NOT swallow it as non-fatal).
    idx = src.find("except ReviewCriticalBlockError")
    assert idx > 0, (
        "Stage 2.5 review block must have an `except "
        "ReviewCriticalBlockError` branch that re-raises"
    )
    # Body follows the except; next 500 chars should contain `raise`
    # on a line by itself before the next top-level `except` keyword.
    import re
    body = src[idx: idx + 500]
    # Strip the leading `except ReviewCriticalBlockError:` line itself
    # so the `except` search below doesn't match the comment word "exception"
    # inside our own except body.
    m = re.search(r"\n\s+except\s+\w", body[5:])
    next_except = (m.start() + 5) if m else len(body)
    # Look for `raise` on its own (not the word "raised" inside a comment).
    assert re.search(r"\braise\b", body[:next_except]), (
        "ReviewCriticalBlockError must be re-raised, not logged"
    )
