"""Tests for scripts/analyze_harbor_conversion_funnel.py.

Covers:
  - compute_metrics: pure rollup of trial rows into metrics dict
  - render_learnings: human-readable strings, date-stamped so trends accrue
  - write_learnings: mirrors coordinator._record_learning hash + ON CONFLICT
  - run_analysis: orchestrator wires fetch + compute + render + write
  - main: arg parsing, DB url resolution, dry-run, error exits
  - bucketing edge cases (0, 1, 3, 4, 7, 8, 15, 16, 100 turns)
  - empty result is a no-op (still records "no trials" learning unless dry-run)
  - duplicate-text dedup via UNIQUE(source_agent, category, learning_hash)
    is exercised through the SQL contract (ON CONFLICT clause shape).
"""
from __future__ import annotations

import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
import analyze_harbor_conversion_funnel as funnel  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(*, turn_count=0, handoff=False, email=None,
              started_days_ago=1, days_to_handoff=None,
              first_turn_days_ago=None):
    now = datetime.now(timezone.utc)
    started = now - timedelta(days=started_days_ago)
    handoff_at = None
    handoff_build_id = None
    if handoff:
        handoff_build_id = uuid.uuid4()
        if days_to_handoff is not None:
            ftd = first_turn_days_ago if first_turn_days_ago is not None else started_days_ago
            first = now - timedelta(days=ftd)
            handoff_at = first + timedelta(days=days_to_handoff)
        else:
            handoff_at = now
    first_turn_at = None
    if turn_count > 0:
        ftd = first_turn_days_ago if first_turn_days_ago is not None else started_days_ago
        first_turn_at = now - timedelta(days=ftd)
    return {
        "trial_id": uuid.uuid4(),
        "started_at": started,
        "handoff_build_id": handoff_build_id,
        "handoff_at": handoff_at,
        "email_captured": email,
        "turn_count": turn_count,
        "first_turn_at": first_turn_at,
    }


def _make_mock_conn(fetch_rows):
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = fetch_rows
    conn.cursor.return_value = cur
    return conn, cur


# ---------------------------------------------------------------------------
# TestBucketForTurns
# ---------------------------------------------------------------------------

class TestBucketForTurns:
    @pytest.mark.parametrize("turns,expected", [
        (0, "0"),
        (1, "1-3"),
        (3, "1-3"),
        (4, "4-7"),
        (7, "4-7"),
        (8, "8-15"),
        (15, "8-15"),
        (16, "16+"),
        (100, "16+"),
        (-1, "0"),  # defensive
    ])
    def test_buckets(self, turns, expected):
        assert funnel._bucket_for_turns(turns) == expected


# ---------------------------------------------------------------------------
# TestPct
# ---------------------------------------------------------------------------

class TestPct:
    def test_zero_denom(self):
        assert funnel._pct(5, 0) == 0
        assert funnel._pct(0, 0) == 0

    def test_basic(self):
        assert funnel._pct(1, 4) == 25
        assert funnel._pct(3, 4) == 75
        assert funnel._pct(0, 5) == 0
        assert funnel._pct(5, 5) == 100


# ---------------------------------------------------------------------------
# TestComputeMetrics
# ---------------------------------------------------------------------------

class TestComputeMetrics:
    def test_empty_rows(self):
        m = funnel.compute_metrics([])
        assert m["total"] == 0
        assert m["converted"] == 0
        assert m["non_converted"] == 0
        assert m["conversion_rate_pct"] == 0
        assert m["converter_median_turns"] is None
        assert m["converter_median_days_to_handoff"] is None
        assert m["non_converter_median_turns"] is None
        assert m["non_converter_email_capture_rate_pct"] == 0
        assert m["non_converter_reached_8_turns"] == 0
        assert m["non_converter_reached_8_pct"] == 0
        assert m["non_converter_bucket_counts"] == {}

    def test_all_converters(self):
        rows = [
            _make_row(turn_count=10, handoff=True, days_to_handoff=2.0),
            _make_row(turn_count=12, handoff=True, days_to_handoff=4.0),
            _make_row(turn_count=14, handoff=True, days_to_handoff=6.0),
        ]
        m = funnel.compute_metrics(rows)
        assert m["total"] == 3
        assert m["converted"] == 3
        assert m["non_converted"] == 0
        assert m["conversion_rate_pct"] == 100
        assert m["converter_median_turns"] == 12
        assert m["converter_median_days_to_handoff"] == 4.0

    def test_mixed_with_buckets(self):
        rows = [
            _make_row(turn_count=10, handoff=True, days_to_handoff=3.0),
            _make_row(turn_count=2),  # 1-3 bucket
            _make_row(turn_count=2, email="user@example.com"),
            _make_row(turn_count=5),  # 4-7
            _make_row(turn_count=8),  # 8-15
            _make_row(turn_count=20),  # 16+
            _make_row(turn_count=0),  # 0 bucket
        ]
        m = funnel.compute_metrics(rows)
        assert m["total"] == 7
        assert m["converted"] == 1
        assert m["non_converted"] == 6
        assert m["conversion_rate_pct"] == 14
        assert m["converter_median_turns"] == 10
        assert m["converter_median_days_to_handoff"] == 3.0
        # Non-converter turns: [2, 2, 5, 8, 20, 0] → median = 3.5 → int(3)
        assert m["non_converter_median_turns"] == 3
        # 1 of 6 captured email
        assert m["non_converter_email_capture_rate_pct"] == 17
        # 2 of 6 reached >= 8 turns (8 and 20)
        assert m["non_converter_reached_8_turns"] == 2
        assert m["non_converter_reached_8_pct"] == 33
        assert m["non_converter_bucket_counts"] == {
            "1-3": 2, "4-7": 1, "8-15": 1, "16+": 1, "0": 1,
        }

    def test_converter_with_no_first_turn_skips_days(self):
        # Edge case: handoff but no first_turn_at (shouldn't happen in prod
        # but the code must not crash on it).
        row = _make_row(turn_count=0, handoff=True)
        # Force no first_turn
        row["first_turn_at"] = None
        m = funnel.compute_metrics([row])
        assert m["converted"] == 1
        assert m["converter_median_turns"] == 0
        # No timing data → None
        assert m["converter_median_days_to_handoff"] is None

    def test_email_captured_blank_string_does_not_count(self):
        rows = [
            _make_row(turn_count=2, email=""),
            _make_row(turn_count=2, email="   "),  # whitespace
            _make_row(turn_count=2, email="real@x.example"),
        ]
        m = funnel.compute_metrics(rows)
        assert m["non_converter_email_capture_rate_pct"] == 33


# ---------------------------------------------------------------------------
# TestRenderLearnings
# ---------------------------------------------------------------------------

class TestRenderLearnings:
    _DATE = date(2026, 5, 4)

    def test_empty_metrics(self):
        m = funnel.compute_metrics([])
        lines = funnel.render_learnings(m, window_days=3, analysis_date=self._DATE)
        assert len(lines) == 1
        assert "2026-05-04" in lines[0]
        assert "3d" in lines[0]
        assert "No trial keepers" in lines[0]

    def test_full_learning_set(self):
        rows = [
            _make_row(turn_count=10, handoff=True, days_to_handoff=2.0),
            _make_row(turn_count=12, handoff=True, days_to_handoff=4.0),
            _make_row(turn_count=2),
            _make_row(turn_count=2, email="user@example.com"),
            _make_row(turn_count=8),
        ]
        m = funnel.compute_metrics(rows)
        lines = funnel.render_learnings(m, window_days=3, analysis_date=self._DATE)
        # Conversion rate, converters, non-converter median, >=8 stat, histogram
        assert any("Conversion rate" in l and "40%" in l for l in lines)
        assert any("Converters reached handoff at median 11 turns" in l
                   for l in lines)
        assert any("Non-converters: median" in l for l in lines)
        assert any("reached >=8 turns" in l for l in lines)
        assert any("histogram" in l for l in lines)

    def test_date_in_every_line_for_dedup_separation(self):
        rows = [_make_row(turn_count=5, handoff=True, days_to_handoff=2.0)]
        m = funnel.compute_metrics(rows)
        lines = funnel.render_learnings(m, window_days=3, analysis_date=self._DATE)
        for line in lines:
            assert "2026-05-04" in line, (
                "Every line must carry the analysis date so sequential "
                "runs do not collapse into a single dedup hash."
            )


# ---------------------------------------------------------------------------
# TestWriteLearnings
# ---------------------------------------------------------------------------

class TestWriteLearnings:
    def test_inserts_each_learning_with_dedup_clause(self):
        conn, cur = _make_mock_conn([])
        learnings = ["learning A", "learning B"]
        n = funnel.write_learnings(conn, learnings)
        assert n == 2
        assert cur.execute.call_count == 2
        # Each call uses the ON CONFLICT clause matching coordinator pattern
        for call in cur.execute.call_args_list:
            sql = call.args[0]
            assert "INSERT INTO agent_learnings" in sql
            assert "ON CONFLICT" in sql
            assert "learning_hash" in sql
            assert "DO UPDATE SET last_applied_at" in sql

    def test_hash_matches_coordinator_pattern(self):
        # The hash must be SHA-256 of agent:category:text → first 16 hex chars
        # so coordinator.tick._record_learning and this script agree.
        import hashlib
        text = "test learning"
        expected = hashlib.sha256(
            f"qa_engineer:harbor_conversion_funnel:{text}".encode()
        ).hexdigest()[:16]

        conn, cur = _make_mock_conn([])
        funnel.write_learnings(conn, [text])
        params = cur.execute.call_args.args[1]
        assert params[0] == "qa_engineer"
        assert params[1] == "harbor_conversion_funnel"
        assert params[2] == text
        assert params[3] == expected

    def test_empty_list_writes_nothing(self):
        conn, cur = _make_mock_conn([])
        n = funnel.write_learnings(conn, [])
        assert n == 0
        cur.execute.assert_not_called()


# ---------------------------------------------------------------------------
# TestFetchTrialRows
# ---------------------------------------------------------------------------

class TestFetchTrialRows:
    def test_window_param_substituted(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur

        funnel.fetch_trial_rows(conn, window_days=7)
        sql, params = cur.execute.call_args.args
        assert "trial_keepers" in sql
        assert "companion_turns" in sql
        assert "handoff_build_id" in sql
        assert "started_at" in sql
        assert params == ("7",)


# ---------------------------------------------------------------------------
# TestRunAnalysis
# ---------------------------------------------------------------------------

class TestRunAnalysis:
    def test_dry_run_does_not_write_or_commit(self):
        rows = [_make_row(turn_count=5, handoff=True, days_to_handoff=1.0)]
        conn, cur = _make_mock_conn(rows)
        result = funnel.run_analysis(conn, window_days=3, dry_run=True,
                                     analysis_date=date(2026, 5, 4))
        assert result["dry_run"] is True
        assert result["written"] == 0
        # SELECT ran (fetch_trial_rows), but no INSERT and no commit
        # Verify by checking commit not called
        conn.commit.assert_not_called()
        # The execute calls should only contain the SELECT, not any INSERT
        assert all(
            "INSERT INTO agent_learnings" not in c.args[0]
            for c in cur.execute.call_args_list
        )

    def test_live_run_writes_and_commits(self):
        rows = [
            _make_row(turn_count=10, handoff=True, days_to_handoff=2.0),
            _make_row(turn_count=2),
        ]
        conn, cur = _make_mock_conn(rows)
        result = funnel.run_analysis(conn, window_days=3, dry_run=False,
                                     analysis_date=date(2026, 5, 4))
        assert result["dry_run"] is False
        assert result["written"] >= 1
        conn.commit.assert_called_once()
        # At least one INSERT INTO agent_learnings was issued
        assert any(
            "INSERT INTO agent_learnings" in c.args[0]
            for c in cur.execute.call_args_list
        )

    def test_empty_rows_still_writes_explanatory_learning(self):
        conn, cur = _make_mock_conn([])
        result = funnel.run_analysis(conn, window_days=3, dry_run=False,
                                     analysis_date=date(2026, 5, 4))
        # A "no trials" learning is still written so the operator can see
        # the analysis ran but had no data.
        assert result["written"] == 1
        assert any("No trial keepers" in l for l in result["learnings"])
        conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# TestResolveDbUrl
# ---------------------------------------------------------------------------

class TestResolveDbUrl:
    def test_explicit_override_wins(self, monkeypatch):
        monkeypatch.setenv("PROD_DATABASE_URL", "prod-url")
        monkeypatch.setenv("DATABASE_URL", "local-url")
        assert funnel._resolve_db_url("override-url") == "override-url"

    def test_prod_preferred_over_local(self, monkeypatch):
        monkeypatch.setenv("PROD_DATABASE_URL", "prod-url")
        monkeypatch.setenv("DATABASE_URL", "local-url")
        assert funnel._resolve_db_url(None) == "prod-url"

    def test_falls_back_to_local(self, monkeypatch):
        monkeypatch.delenv("PROD_DATABASE_URL", raising=False)
        monkeypatch.setenv("DATABASE_URL", "local-url")
        assert funnel._resolve_db_url(None) == "local-url"

    def test_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("PROD_DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert funnel._resolve_db_url(None) is None


# ---------------------------------------------------------------------------
# TestMain
# ---------------------------------------------------------------------------

class TestMain:
    def test_no_db_url_exits_3(self, monkeypatch):
        monkeypatch.delenv("PROD_DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        rc = funnel.main([])
        assert rc == 3

    def test_invalid_window_days_exits_3(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "x")
        rc = funnel.main(["--window-days", "0"])
        assert rc == 3
        rc = funnel.main(["--window-days", "-5"])
        assert rc == 3

    def test_db_connect_failure_exits_1(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://broken")
        import psycopg2
        with patch("analyze_harbor_conversion_funnel.psycopg2.connect",
                   side_effect=psycopg2.OperationalError("nope")):
            rc = funnel.main([])
        assert rc == 1

    def test_happy_path_returns_0_and_dry_run_skips_writes(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        conn, cur = _make_mock_conn([])
        with patch("analyze_harbor_conversion_funnel.psycopg2.connect",
                   return_value=conn):
            rc = funnel.main(["--dry-run"])
        assert rc == 0
        # dry-run: no commit, no INSERT
        conn.commit.assert_not_called()

    def test_sql_failure_rolls_back_and_exits_1(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        import psycopg2
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute.side_effect = psycopg2.ProgrammingError("bad sql")
        conn.cursor.return_value = cur
        with patch("analyze_harbor_conversion_funnel.psycopg2.connect",
                   return_value=conn):
            rc = funnel.main([])
        assert rc == 1
        conn.rollback.assert_called_once()
