"""Tests for coordinator/build_failure_categorizer.py and the gate around it.

Covers the pure helpers (_normalize_category, _estimate_cost_usd), the model
call and the log read (categorize_log, fetch_log_excerpt), the two actuator
methods that persist a categorization (Actuators.set_build_error_category and
record_failure_category), and enrich_uncategorized / work detection, which may
reach the model and those writes only through an Actuators instance: nothing
happens under dry run, without the permission, or past the per-tick cap.

Anthropic clients and connections are mocked, so no real API call is made; one
test runs the UPDATE on a scratch PostgreSQL database and skips without one.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Repo root on sys.path so coordinator/ + cli/ resolve correctly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from coordinator.actuators import Actuators
from coordinator.budget import ModelCallMeter, TickBudget
from coordinator.build_failure_categorizer import (
    VALID_CATEGORIES,
    _normalize_category,
    _estimate_cost_usd,
    categorize_log,
    fetch_log_excerpt,
    enrich_uncategorized,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _mock_anthropic_response(text: str, *, in_tokens: int = 120, out_tokens: int = 3):
    """Return a fake Anthropic messages.create response."""
    block = SimpleNamespace(text=text)
    usage = SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens)
    return SimpleNamespace(content=[block], usage=usage)


def _mock_client_returning(text: str, *, in_tokens: int = 120, out_tokens: int = 3):
    """Build a mock anthropic.Anthropic client whose messages.create returns text."""
    client = MagicMock()
    client.messages.create.return_value = _mock_anthropic_response(
        text, in_tokens=in_tokens, out_tokens=out_tokens
    )
    return client


def _mock_client_raising(exc: Exception):
    client = MagicMock()
    client.messages.create.side_effect = exc
    return client


def _mock_conn_with_logs(rows):
    """Mock psycopg2 conn whose cursor.fetchall returns `rows` (list of tuples)."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn.cursor.return_value = cur
    return conn


# ---------------------------------------------------------------------------
# _normalize_category
# ---------------------------------------------------------------------------

class TestNormalizeCategory:
    def test_exact_match(self):
        assert _normalize_category("infra") == "infra"
        assert _normalize_category("code_error") == "code_error"
        assert _normalize_category("spec_error") == "spec_error"
        assert _normalize_category("timeout") == "timeout"
        assert _normalize_category("app_logic") == "app_logic"

    def test_strips_punctuation(self):
        assert _normalize_category("infra.") == "infra"
        assert _normalize_category("'code_error'") == "code_error"
        assert _normalize_category("timeout!\n") == "timeout"

    def test_uppercase_normalized(self):
        assert _normalize_category("INFRA") == "infra"
        assert _normalize_category("Code_Error") == "code_error"

    def test_substring_fallback(self):
        # Haiku occasionally adds a brief prefix/suffix around the word.
        assert _normalize_category("category: infra") == "infra"
        assert _normalize_category("the timeout one") == "timeout"

    def test_empty_returns_unknown(self):
        assert _normalize_category("") == "unknown"
        assert _normalize_category("   ") == "unknown"

    def test_garbage_returns_unknown(self):
        assert _normalize_category("xyz") == "unknown"
        assert _normalize_category("12345") == "unknown"


# ---------------------------------------------------------------------------
# _estimate_cost_usd
# ---------------------------------------------------------------------------

class TestEstimateCostUsd:
    def test_zero_tokens(self):
        assert _estimate_cost_usd(0, 0) == 0.0

    def test_typical_call_cost_under_a_tenth_of_a_cent(self):
        # 500 input + 5 output tokens at Haiku 4.5 pricing: well under $0.001
        cost = _estimate_cost_usd(500, 5)
        assert cost < 0.001

    def test_million_input_tokens_costs_one_dollar(self):
        assert _estimate_cost_usd(1_000_000, 0) == pytest.approx(1.00)

    def test_million_output_tokens_costs_five_dollars(self):
        assert _estimate_cost_usd(0, 1_000_000) == pytest.approx(5.00)


# ---------------------------------------------------------------------------
# categorize_log
# ---------------------------------------------------------------------------

class TestCategorizeLog:
    def test_returns_unknown_for_empty_excerpt(self):
        category, cost = categorize_log("")
        assert category == "unknown"
        assert cost == 0.0

    def test_returns_unknown_for_whitespace_excerpt(self):
        category, cost = categorize_log("   \n  ")
        assert category == "unknown"
        assert cost == 0.0

    def test_returns_unknown_when_no_api_key_and_no_client(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        category, cost = categorize_log("EADDRINUSE :::4000")
        assert category == "unknown"
        assert cost == 0.0

    def test_happy_path_returns_normalized_category(self):
        client = _mock_client_returning("infra")
        category, cost = categorize_log("connection refused", client=client)
        assert category == "infra"
        assert cost > 0.0
        client.messages.create.assert_called_once()

    def test_passes_default_model_to_client(self):
        client = _mock_client_returning("timeout")
        categorize_log("deadline exceeded", client=client)
        call = client.messages.create.call_args
        assert call.kwargs["model"] == "claude-haiku-4-5"

    def test_passes_excerpt_in_user_message(self):
        client = _mock_client_returning("code_error")
        categorize_log("TypeError: undefined", client=client)
        call = client.messages.create.call_args
        messages = call.kwargs["messages"]
        assert messages[0]["role"] == "user"
        assert "TypeError: undefined" in messages[0]["content"]

    def test_handles_verbose_response(self):
        client = _mock_client_returning("Category: spec_error")
        category, _ = categorize_log("validation failed", client=client)
        assert category == "spec_error"

    def test_handles_unrecognized_response(self):
        client = _mock_client_returning("definitely-not-a-category")
        category, _ = categorize_log("some log", client=client)
        assert category == "unknown"

    def test_api_exception_returns_unknown_zero_cost(self):
        client = _mock_client_raising(RuntimeError("anthropic 500"))
        category, cost = categorize_log("some log", client=client)
        assert category == "unknown"
        assert cost == 0.0

    def test_cost_uses_response_usage(self):
        client = _mock_client_returning(
            "infra", in_tokens=200, out_tokens=10
        )
        _, cost = categorize_log("connection refused", client=client)
        # 200/1M * 1.00 + 10/1M * 5.00 = 0.0002 + 0.00005 = 0.00025
        assert cost == pytest.approx(0.00025, abs=1e-6)

    def test_response_with_no_usage_yields_zero_cost(self):
        # Some responses (mocked or older SDK shapes) may omit usage.
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="infra")], usage=None
        )
        category, cost = categorize_log("connection refused", client=client)
        assert category == "infra"
        assert cost == 0.0


# ---------------------------------------------------------------------------
# fetch_log_excerpt
# ---------------------------------------------------------------------------

class TestFetchLogExcerpt:
    def test_returns_empty_for_blank_build_id(self):
        conn = MagicMock()
        assert fetch_log_excerpt(conn, "") == ""
        conn.cursor.assert_not_called()

    def test_returns_empty_when_no_log_rows(self):
        conn = _mock_conn_with_logs([])
        assert fetch_log_excerpt(conn, "build-1") == ""

    def test_joins_rows_in_chronological_order(self):
        # Rows come back DESC by id; we expect the function to reverse them.
        rows = [("third",), ("second",), ("first",)]
        conn = _mock_conn_with_logs(rows)
        result = fetch_log_excerpt(conn, "build-1")
        assert result == "first\nsecond\nthird"

    def test_truncates_to_last_max_chars(self):
        long_msg = "x" * 1000
        rows = [(long_msg,)]
        conn = _mock_conn_with_logs(rows)
        result = fetch_log_excerpt(conn, "build-1", max_chars=100)
        assert len(result) == 100
        assert result == "x" * 100

    def test_default_max_chars_is_500(self):
        long_msg = "y" * 2000
        rows = [(long_msg,)]
        conn = _mock_conn_with_logs(rows)
        result = fetch_log_excerpt(conn, "build-1")
        assert len(result) == 500

    def test_db_error_returns_empty_string(self):
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("db down")
        result = fetch_log_excerpt(conn, "build-1")
        assert result == ""

    def test_filters_empty_rows(self):
        rows = [("real message",), (None,), ("", )]
        conn = _mock_conn_with_logs(rows)
        result = fetch_log_excerpt(conn, "build-1")
        assert result == "real message"


# ---------------------------------------------------------------------------
# The two writes, through the actuator layer
# ---------------------------------------------------------------------------

def _allowance(calls=50, models=("claude-haiku-4-5",)):
    """A tick's model-call allowance good for `calls` cheap-tier calls."""
    return ModelCallMeter(TickBudget(max_api_calls=calls, max_cost_usd=calls * 0.05,
                                     allowed_models=list(models)))


def _triage(dry_run=False, model_calls="default"):
    """The principal coordinator.work_detection uses for failure triage, with
    a model-call allowance unless the test passes its own (or None)."""
    if model_calls == "default":
        model_calls = _allowance()
    return Actuators("failure_triage", "work-detection", dry_run=dry_run, model_calls=model_calls)


def _sandbox_kb(tmp_path, monkeypatch):
    """Point the failure KB at a file in tmp_path and return that path."""
    kb_file = tmp_path / "failure_kb.json"
    import cli.failure_kb as kb_mod
    monkeypatch.setattr(kb_mod, "KB_PATH", kb_file)
    monkeypatch.setenv("FAILURE_KB_PATH", str(kb_file))
    return kb_file


@pytest.fixture
def fake_db(monkeypatch):
    """A DATABASE_URL plus a psycopg2.connect that hands back a mock connection."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/does_not_matter")
    conn = MagicMock()
    conn.cursor.return_value.rowcount = 1
    with patch("psycopg2.connect", return_value=conn) as connect:
        yield SimpleNamespace(connect=connect, conn=conn, cur=conn.cursor.return_value)


class TestSetBuildErrorCategory:
    def test_writes_update_with_category_and_id(self, fake_db):
        assert _triage().set_build_error_category("build-42", "infra") is True
        fake_db.cur.execute.assert_called_once()
        sql, params = fake_db.cur.execute.call_args.args
        assert "UPDATE builds" in sql and "error_category" in sql
        assert "status = 'failed'" in sql, "only a build that is still failed is annotated"
        assert params == ("infra", "build-42")
        fake_db.conn.commit.assert_called_once()

    def test_dry_run_opens_no_connection(self, fake_db):
        actuators = _triage(dry_run=True)
        assert actuators.set_build_error_category("build-42", "infra") is False
        fake_db.connect.assert_not_called()
        assert actuators.actions[-1].details["dry_run"] is True

    @pytest.mark.parametrize("build_id, category", [
        ("build-1", "unknown"), ("", "infra"), ("build-1", "rogue_category"),
    ])
    def test_skips_invalid_input(self, fake_db, build_id, category):
        assert _triage().set_build_error_category(build_id, category) is False
        fake_db.connect.assert_not_called()

    def test_reports_false_when_no_failed_row_matched(self, fake_db):
        fake_db.cur.rowcount = 0
        assert _triage().set_build_error_category("build-9", "infra") is False

    def test_db_error_is_contained(self, fake_db):
        fake_db.cur.execute.side_effect = RuntimeError("write failed")
        assert _triage().set_build_error_category("build-7", "code_error") is False
        fake_db.conn.commit.assert_not_called()
        fake_db.conn.close.assert_called_once()

    @pytest.mark.parametrize("agent", ["customer_service", "infra_engineer", "no_such_agent"])
    def test_only_the_triage_principal_may_annotate_a_build(self, fake_db, agent):
        with pytest.raises(PermissionError, match="not permitted"):
            Actuators(agent, "t", dry_run=False).set_build_error_category("build-1", "infra")
        fake_db.connect.assert_not_called()

    def test_analysis_only_mode_refuses_the_build_write(self, fake_db, monkeypatch):
        monkeypatch.setenv("COORDINATOR_ANALYSIS_ONLY", "true")
        with pytest.raises(PermissionError, match="COORDINATOR_ANALYSIS_ONLY"):
            _triage().set_build_error_category("build-1", "infra")
        fake_db.connect.assert_not_called()

    def test_real_database_only_a_failed_build_is_annotated(self, scratch_pg_url, monkeypatch):
        """The statement itself, on PostgreSQL: the status predicate holds, and
        a dry run leaves the row alone."""
        import psycopg2
        table_owner = psycopg2.connect(scratch_pg_url)
        with table_owner.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS builds")
            cur.execute("CREATE TABLE builds (id text PRIMARY KEY, status text, error_category text)")
            cur.execute("INSERT INTO builds VALUES ('b-failed', 'failed', NULL), "
                        "('b-requeued', 'queued', NULL), ('b-dry', 'failed', NULL)")
        table_owner.commit()
        monkeypatch.setenv("DATABASE_URL", scratch_pg_url)
        try:
            assert _triage().set_build_error_category("b-failed", "infra") is True
            assert _triage().set_build_error_category("b-requeued", "infra") is False
            assert _triage(dry_run=True).set_build_error_category("b-dry", "infra") is False
            with table_owner.cursor() as cur:
                cur.execute("SELECT id, error_category FROM builds ORDER BY id")
                assert cur.fetchall() == [("b-dry", None), ("b-failed", "infra"), ("b-requeued", None)]
        finally:
            with table_owner.cursor() as cur:
                cur.execute("DROP TABLE builds")
            table_owner.commit()
            table_owner.close()


class TestRecordFailureCategory:
    def test_writes_an_outstanding_failure_with_the_category(self, tmp_path, monkeypatch):
        kb_file = _sandbox_kb(tmp_path, monkeypatch)
        ok = _triage().record_failure_category(
            "TypeError: cannot read property 'name' of undefined", "code_error")
        assert ok is True
        import json
        data = json.loads(kb_file.read_text())
        assert len(data) == 1
        entry = next(iter(data.values()))
        assert entry["fix_category"] == "code_error"
        assert entry["failure_count"] == 1
        assert entry["recent_outcomes"] == [False]
        assert "[auto-categorized]" in entry["fix_description"]

    def test_dry_run_does_not_touch_the_kb(self, tmp_path, monkeypatch):
        kb_file = _sandbox_kb(tmp_path, monkeypatch)
        assert _triage(dry_run=True).record_failure_category("some error", "infra") is False
        assert not kb_file.exists()

    @pytest.mark.parametrize("message, category", [
        ("some error", "unknown"), ("some error", "rogue_category"), ("", "infra"),
    ])
    def test_skips_invalid_input(self, tmp_path, monkeypatch, message, category):
        kb_file = _sandbox_kb(tmp_path, monkeypatch)
        assert _triage().record_failure_category(message, category) is False
        assert not kb_file.exists()

    def test_an_agent_without_kb_record_is_refused(self, tmp_path, monkeypatch):
        kb_file = _sandbox_kb(tmp_path, monkeypatch)
        with pytest.raises(PermissionError, match="not permitted"):
            Actuators("architect", "t", dry_run=False).record_failure_category("some error", "infra")
        assert not kb_file.exists()


# ---------------------------------------------------------------------------
# enrich_uncategorized (top-level entry)
# ---------------------------------------------------------------------------

class TestEnrichUncategorized:
    def test_returns_existing_category_when_already_set(self, monkeypatch):
        # Even with an API key + client, a real category short-circuits the call.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        client = _mock_client_returning("infra")
        conn = MagicMock()
        result = enrich_uncategorized(
            conn, {"id": "b1", "error_message": "x"}, "code_error",
            actuators=_triage(), client=client,
        )
        assert result == "code_error"
        client.messages.create.assert_not_called()

    def test_actuators_are_required(self):
        with pytest.raises(TypeError):
            enrich_uncategorized(MagicMock(), {"id": "b1", "error_message": "x"}, "unknown")

    def test_skips_when_no_api_key_and_no_client(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        conn = MagicMock()
        result = enrich_uncategorized(
            conn, {"id": "b1", "error_message": "x"}, "unknown", actuators=_triage(),
        )
        assert result == "unknown"
        conn.cursor.assert_not_called()

    def test_skips_when_build_id_blank(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        client = _mock_client_returning("infra")
        conn = MagicMock()
        result = enrich_uncategorized(
            conn, {"id": "", "error_message": "x"}, "unknown",
            actuators=_triage(), client=client,
        )
        assert result == "unknown"
        client.messages.create.assert_not_called()

    def test_falls_back_to_error_message_when_logs_empty(self, monkeypatch, tmp_path, fake_db):
        _sandbox_kb(tmp_path, monkeypatch)
        conn = _mock_conn_with_logs([])  # the build_logs query returns nothing
        client = _mock_client_returning("spec_error")
        result = enrich_uncategorized(
            conn,
            {"id": "build-99", "error_message": "validation failed: missing key"},
            "unknown",
            actuators=_triage(), client=client,
        )
        assert result == "spec_error"
        # Excerpt sent to Haiku should contain the error_message text.
        sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "validation failed" in sent

    def test_persists_to_db_and_kb_on_success(self, monkeypatch, tmp_path, fake_db):
        kb_file = _sandbox_kb(tmp_path, monkeypatch)
        conn = _mock_conn_with_logs([("npm ERR! TypeError: undefined",)])
        client = _mock_client_returning("code_error")
        actuators = _triage()
        result = enrich_uncategorized(
            conn,
            {"id": "build-1", "error_message": "TypeError: undefined"},
            None,
            actuators=actuators, client=client,
        )
        assert result == "code_error"
        # The tick's connection is only read from; it is never committed here.
        assert all("SELECT message" in c.args[0] for c in conn.cursor.return_value.execute.call_args_list)
        conn.commit.assert_not_called()
        # The UPDATE goes through the actuator's own connection.
        sql, params = fake_db.cur.execute.call_args.args
        assert "UPDATE builds" in sql and params == ("code_error", "build-1")
        import json
        data = json.loads(kb_file.read_text())
        assert any(e.get("fix_category") == "code_error" for e in data.values())
        assert [a.action_type for a in actuators.actions] == ["model_call", "build_annotate", "kb_record"]

    def test_dry_run_makes_no_model_call_and_no_write(self, monkeypatch, tmp_path, fake_db):
        """Dry run is the coordinator's default: triage must cost nothing and
        change nothing, and the failure is routed as 'unknown'."""
        kb_file = _sandbox_kb(tmp_path, monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        conn = _mock_conn_with_logs([("npm ERR! TypeError: undefined",)])
        client = _mock_client_returning("code_error")
        result = enrich_uncategorized(
            conn, {"id": "build-1", "error_message": "TypeError: undefined"}, None,
            actuators=_triage(dry_run=True), client=client,
        )
        assert result == "unknown"
        client.messages.create.assert_not_called()
        conn.cursor.assert_not_called()
        fake_db.connect.assert_not_called()
        assert not kb_file.exists()

    def test_a_caller_without_model_call_gets_no_model_call(self, monkeypatch, tmp_path, fake_db):
        kb_file = _sandbox_kb(tmp_path, monkeypatch)
        client = _mock_client_returning("code_error")
        result = enrich_uncategorized(
            _mock_conn_with_logs([("x",)]), {"id": "build-1", "error_message": "x"}, None,
            actuators=Actuators("architect", "t", dry_run=False), client=client,
        )
        assert result == "unknown"
        client.messages.create.assert_not_called()
        fake_db.connect.assert_not_called()
        assert not kb_file.exists()

    def test_analysis_only_mode_categorizes_but_does_not_write_the_build_row(
            self, monkeypatch, tmp_path, fake_db):
        kb_file = _sandbox_kb(tmp_path, monkeypatch)
        monkeypatch.setenv("COORDINATOR_ANALYSIS_ONLY", "true")
        client = _mock_client_returning("infra")
        result = enrich_uncategorized(
            _mock_conn_with_logs([("connection refused",)]),
            {"id": "build-1", "error_message": "connection refused"}, None,
            actuators=_triage(), client=client,
        )
        assert result == "infra"
        fake_db.connect.assert_not_called()          # build_annotate is not an analysis permission
        assert kb_file.exists()                      # kb_record is

    def test_returns_unknown_when_haiku_returns_unknown(self, monkeypatch, tmp_path, fake_db):
        kb_file = _sandbox_kb(tmp_path, monkeypatch)
        conn = _mock_conn_with_logs([("noise",)])
        client = _mock_client_returning("totally-not-a-category")
        result = enrich_uncategorized(
            conn,
            {"id": "build-2", "error_message": "noise"},
            "unknown",
            actuators=_triage(), client=client,
        )
        assert result == "unknown"
        fake_db.connect.assert_not_called()
        assert not kb_file.exists()


# ---------------------------------------------------------------------------
# Integration with work detection
# ---------------------------------------------------------------------------

def _failed(build_id, message="weird thing"):
    return {"id": build_id, "app_name": "myapp", "error_message": message,
            "tier": "build", "retry_count": 0}  # no error_category: the regex says 'unknown'


class TestCheckFailedBuildsIntegration:
    """work_detection reaches the categorizer only through gated actuators."""

    @pytest.fixture
    def categorizer_calls(self, monkeypatch, tmp_path):
        _sandbox_kb(tmp_path, monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        from coordinator import build_failure_categorizer as bfc
        calls = []

        def fake_categorize_log(excerpt, *, client=None, **kw):
            calls.append(excerpt)
            return ("infra", 0.0009)

        monkeypatch.setattr(bfc, "categorize_log", fake_categorize_log)
        return calls

    def test_live_triage_categorizes_and_routes(self, categorizer_calls, fake_db):
        from coordinator.work_detection import _check_failed_builds
        from coordinator.state import WorldState

        conn = _mock_conn_with_logs([("connection refused",)])
        state = WorldState(failed_builds_24h=[_failed("build-77", "connection refused")])
        items = _check_failed_builds(conn, state, None, _triage())
        assert len(items) == 1
        assert items[0].context["error_category"] == "infra"
        assert items[0].target_agent == "infra_engineer"
        assert len(categorizer_calls) == 1
        assert fake_db.cur.execute.call_args.args[1] == ("infra", "build-77")

    def test_detect_work_defaults_to_dry_run_so_triage_does_nothing(self, categorizer_calls, fake_db):
        """The real entry point, called the way older callers do (no dry_run
        argument): no model call, no connection, no commit on the tick's own
        connection."""
        from coordinator.work_detection import detect_work
        from coordinator.state import WorldState

        conn = MagicMock()
        state = WorldState(failed_builds_24h=[_failed("build-77")])
        items = detect_work(conn, state, None)
        failure_items = [i for i in items if i.category == "build_failure"]
        assert len(failure_items) == 1
        assert failure_items[0].context["error_category"] == "unknown"
        assert categorizer_calls == []
        fake_db.connect.assert_not_called()
        conn.commit.assert_not_called()

    def test_detect_work_with_dry_run_off_runs_triage(self, categorizer_calls, fake_db):
        from coordinator.work_detection import detect_work
        from coordinator.state import WorldState

        state = WorldState(failed_builds_24h=[_failed("build-77")])
        allowance = _allowance()
        items = detect_work(MagicMock(), state, None, dry_run=False, model_calls=allowance)
        failure_items = [i for i in items if i.category == "build_failure"]
        assert failure_items[0].context["error_category"] == "infra"
        assert len(categorizer_calls) == 1
        assert allowance.granted() == (1, pytest.approx(0.05))

    def test_detect_work_without_a_model_call_allowance_makes_no_call(self, categorizer_calls, fake_db):
        """Live, permitted, key present: still nothing without the tick's meter."""
        from coordinator.work_detection import detect_work
        from coordinator.state import WorldState

        state = WorldState(failed_builds_24h=[_failed("build-77")], daily_budget_remaining_usd=20.0)
        items = detect_work(MagicMock(), state, None, dry_run=False)
        failure_items = [i for i in items if i.category == "build_failure"]
        assert failure_items[0].context["error_category"] == "unknown"
        assert categorizer_calls == []

    def test_without_actuators_there_is_no_triage(self, categorizer_calls, fake_db):
        from coordinator.work_detection import _check_failed_builds
        from coordinator.state import WorldState

        conn = MagicMock()
        items = _check_failed_builds(conn, WorldState(failed_builds_24h=[_failed("build-78")]), None)
        assert items[0].context["error_category"] == "unknown"
        assert categorizer_calls == []
        conn.cursor.assert_not_called()

    def test_triage_is_capped_per_tick(self, categorizer_calls, fake_db):
        from coordinator.work_detection import MAX_TRIAGE_CALLS_PER_TICK, _check_failed_builds
        from coordinator.state import WorldState

        builds = [_failed(f"build-{n}", f"odd failure number {n}")
                  for n in range(MAX_TRIAGE_CALLS_PER_TICK + 3)]
        _check_failed_builds(_mock_conn_with_logs([("log",)]),
                             WorldState(failed_builds_24h=builds), None, _triage())
        assert len(categorizer_calls) == MAX_TRIAGE_CALLS_PER_TICK

    @pytest.mark.parametrize("model_calls", [
        None,                                   # no meter handed over
        ModelCallMeter(TickBudget()),           # a tick with no budget
        ModelCallMeter(TickBudget(max_api_calls=3, max_cost_usd=0.04,
                                  allowed_models=["claude-haiku-4-5"])),  # less than one call's estimate
    ])
    def test_no_triage_when_the_tick_has_no_allowance_for_it(
            self, categorizer_calls, fake_db, model_calls):
        """The world state says $20 is left; what counts is the tick's meter."""
        from coordinator.work_detection import _check_failed_builds
        from coordinator.state import WorldState

        state = WorldState(failed_builds_24h=[_failed("build-79")],
                           daily_budget_remaining_usd=20.0)
        items = _check_failed_builds(_mock_conn_with_logs([("log",)]), state, None,
                                     _triage(model_calls=model_calls))
        assert items[0].context["error_category"] == "unknown"
        assert categorizer_calls == []

    def test_triage_stops_when_the_allowance_is_used_up(self, categorizer_calls, fake_db):
        from coordinator.work_detection import _check_failed_builds
        from coordinator.state import WorldState

        builds = [_failed(f"build-{n}", f"odd failure number {n}") for n in range(4)]
        allowance = _allowance(calls=2)
        _check_failed_builds(_mock_conn_with_logs([("log",)]),
                             WorldState(failed_builds_24h=builds), None,
                             _triage(model_calls=allowance))
        assert len(categorizer_calls) == 2
        assert allowance.granted() == (2, pytest.approx(0.10))

    def test_uncategorized_failure_skips_haiku_when_no_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        conn = MagicMock()
        from coordinator.work_detection import _check_failed_builds
        from coordinator.state import WorldState

        state = WorldState(failed_builds_24h=[_failed("build-78")])
        items = _check_failed_builds(conn, state, None, _triage())
        assert len(items) == 1
        assert items[0].context["error_category"] == "unknown"
        # No DB calls because the categorizer short-circuits.
        conn.cursor.assert_not_called()
