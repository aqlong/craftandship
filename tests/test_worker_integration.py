"""What the worker stores about a build's design review and UI test.

Every test here calls the worker's own function (queue/worker_pg.py:
_store_design_review_results and _store_ui_test_results) and looks at the
statement it runs. The same two functions run against the real schema in
tests/test_worker_pg_realdb.py (TestReviewAndUiTestResultsAreStored).

The v1 engine's design-fix and QA-fix loops are written inline in
_run_pipeline and have no test that runs them: the cases that used to sit in
this file restated those loops' conditions in the test body and called
nothing in the worker, so they could not fail when the worker changed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _extra in (str(ROOT), str(ROOT / "queue"), str(ROOT / "cli")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

import worker_pg


def _recording_conn():
    """A connection whose cursor records the statement it is given."""
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    return conn, cursor


@pytest.mark.parametrize("result, status", [
    ({"skipped": True}, "skipped"),
    ({"skipped": True, "critical_count": 2}, "skipped"),
    ({"critical_count": 2}, "warned"),
    ({"critical_count": 1, "score": 9}, "warned"),
    ({"critical_count": 0, "score": 8}, "passed"),
    ({}, "passed"),
])
def test_the_design_review_status_the_worker_stores(result, status):
    conn, cursor = _recording_conn()

    worker_pg._store_design_review_results(conn, "build-1", result)

    sql, params = cursor.execute.call_args.args
    assert sql.startswith("UPDATE builds SET design_review_status = %s, design_review_results = %s::jsonb")
    assert params[0] == status
    assert params[2] == "build-1"
    conn.commit.assert_called_once()


def test_the_design_review_issues_are_stored_and_nothing_else_of_the_result():
    conn, cursor = _recording_conn()
    issues = [{"issue": "low contrast text on the primary colour", "severity": "critical"}]

    worker_pg._store_design_review_results(conn, "build-1", {"critical_count": 1, "issues": issues, "score": 4})

    _sql, params = cursor.execute.call_args.args
    assert json.loads(params[1]) == issues

    worker_pg._store_design_review_results(conn, "build-1", {"skipped": True})
    assert json.loads(cursor.execute.call_args.args[1][1]) == []


def test_the_ui_test_score_and_the_whole_result_are_stored():
    conn, cursor = _recording_conn()
    result = {"score": 8.5, "checks": [{"name": "text_contrast", "passed": True}]}

    worker_pg._store_ui_test_results(conn, "build-1", result)

    sql, params = cursor.execute.call_args.args
    assert sql.startswith("UPDATE builds SET ui_test_score = %s, ui_test_results = %s::jsonb")
    assert params[0] == 8.5
    assert json.loads(params[1]) == result
    assert params[2] == "build-1"
    conn.commit.assert_called_once()


def test_a_skipped_ui_test_stores_no_score():
    conn, cursor = _recording_conn()

    worker_pg._store_ui_test_results(conn, "build-1", {"skipped": True, "score": None})

    assert cursor.execute.call_args.args[1][0] is None


STORES = [
    (worker_pg._store_design_review_results, {"critical_count": 2, "issues": []}),
    (worker_pg._store_ui_test_results, {"score": 7.0}),
]


@pytest.mark.parametrize("store, result", STORES)
def test_a_statement_that_fails_is_rolled_back_and_does_not_stop_the_build(store, result):
    """These two stages are reports about the build: a database error while
    storing one must not turn into a failed build."""
    conn, cursor = _recording_conn()
    cursor.execute.side_effect = RuntimeError("column does not exist")

    store(conn, "build-1", result)   # does not raise

    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


@pytest.mark.parametrize("store, result", STORES)
def test_a_connection_that_cannot_even_roll_back_does_not_stop_the_build(store, result):
    conn, cursor = _recording_conn()
    cursor.execute.side_effect = RuntimeError("server closed the connection")
    conn.rollback.side_effect = RuntimeError("connection already closed")

    store(conn, "build-1", result)   # does not raise
