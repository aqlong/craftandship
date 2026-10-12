"""In the coordinator's budget chain "unknown" is a value of its own.

A figure the coordinator could not read used to keep the default it was
initialised with, and the default was the permissive one: no spend, no
active builds, an empty list. Three inputs turned that into a budget the tick
should not have had:

  * a cost report that arrives with status 200 and says nothing about today
    (no `data`, an empty `data`, only other days, a page that says there is
    more without saying where);
  * a pipeline query that fails, leaving active_builds at 0;
  * anything else a trigger or the dispatch gate reads the same way.

The cost report is served here by a real HTTP listener on loopback, so the
answer goes through the same request, the same parser and the same budget
function as in production. The real-database side (a pipeline query that
really fails inside a real tick) is in tests/test_coordinator_tick_realdb.py.
"""

import http.server
import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from coordinator import budget as budget_mod
from coordinator import scorer, spend_tracker, work_detection
from coordinator.budget import compute_tick_budget
from coordinator.state import (
    WorkItem,
    WorldState,
    budget_remaining_usd,
    build_world_state,
    customer_builds_active,
    pipeline_idle,
)

HAIKU = "claude-haiku-4-5"
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
YESTERDAY = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _bucket(day, cents="0", currency="USD"):
    return {"starting_at": f"{day}T00:00:00Z", "ending_at": f"{day}T23:59:59Z",
            "results": [{"currency": currency, "amount": cents}]}


# ---------------------------------------------------------------------------
# A cost report over HTTP
# ---------------------------------------------------------------------------

class _CostReport(http.server.BaseHTTPRequestHandler):
    pages = None      # list of (status, body bytes); the last one repeats
    seen = None

    def do_GET(self):
        self.seen.append((self.path, self.headers.get("x-api-key")))
        status, body = self.pages[min(len(self.seen), len(self.pages)) - 1]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def cost_report(monkeypatch):
    """Serve the given pages as the Anthropic cost report and make it the
    budget's spend source. Returns a function that sets the pages; the
    requests that arrive are in `.seen`."""
    seen = []
    handler = type("Report", (_CostReport,), {"pages": [(200, b"{}")], "seen": seen})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    monkeypatch.setattr(spend_tracker, "_COST_REPORT_URL",
                        f"http://127.0.0.1:{server.server_address[1]}/v1/organizations/cost_report")
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "not-" + "an-admin-key")
    monkeypatch.setenv("COORDINATOR_DAILY_BUDGET", "20")
    monkeypatch.setenv("COORDINATOR_INTERVAL_S", "86400")     # one tick a day: the tick gets the 25% cap
    monkeypatch.setitem(spend_tracker._anthropic_cache, "value", None)
    monkeypatch.setitem(spend_tracker._anthropic_cache, "expires", None)

    def serve(*pages):
        handler.pages = [(status, body if isinstance(body, bytes) else json.dumps(body).encode())
                         for status, body in pages]

    serve.seen = seen
    yield serve
    server.shutdown()
    server.server_close()


class _Ledger:
    """The tick's connection as far as the ledger's two statements go: each
    answers (0 dollars, 0 rows)."""

    def cursor(self):
        cur = MagicMock()
        cur.fetchone.return_value = (0, 0)
        return cur

    def rollback(self):
        pass


def _budget():
    return compute_tick_budget(WorldState(), conn=_Ledger())


class TestACostReportThatDoesNotSayWhatWasSpentToday:
    def test_the_control_a_report_with_todays_bucket_is_the_budget(self, cost_report):
        cost_report((200, {"data": [_bucket(YESTERDAY, "900"), _bucket(TODAY, "1200")], "has_more": False}))
        tick_budget = _budget()
        assert tick_budget.max_cost_usd == pytest.approx(5.0)          # 25% of $20; $12 of it is spent
        assert tick_budget.max_api_calls == 100
        assert cost_report.seen[0][1] == "not-" + "an-admin-key"

    def test_a_day_with_nothing_spent_is_a_known_zero(self, cost_report):
        cost_report((200, {"data": [{"starting_at": f"{TODAY}T00:00:00Z", "results": []}], "has_more": False}))
        assert spend_tracker.get_anthropic_spend()["today_usd"] == 0.0
        assert _budget().max_cost_usd == pytest.approx(5.0)

    @pytest.mark.parametrize("answer", [
        {},                                                         # no `data` at all
        {"data": []},                                               # an empty one
        {"data": None},
        {"data": "none today"},
        {"data": {"results": []}},
        {"data": [_bucket(YESTERDAY, "900")], "has_more": False},   # other days only
        {"data": ["2026-01-01"]},                                   # buckets that are not objects
        [],                                                         # not an object
        {"error": {"type": "overloaded_error"}},
    ], ids=["empty object", "empty data", "null data", "text data", "object data", "no bucket for today",
            "buckets not objects", "a list", "an error object"])
    def test_a_200_that_says_nothing_about_today_is_unknown_spend_and_no_budget(self, cost_report, answer):
        """Each of these used to read as $0.00 spent today: the full budget."""
        cost_report((200, answer))
        assert spend_tracker.get_anthropic_spend() is None
        tick_budget = _budget()
        assert (tick_budget.max_cost_usd, tick_budget.max_api_calls, tick_budget.max_opus_calls) == (0.0, 0, 0)
        assert len(cost_report.seen) >= 1, "the report was asked for; this is about what came back"

    def test_has_more_without_a_next_page_is_a_partial_total_and_is_refused(self, cost_report):
        """Today's bucket is on the first page and more is said to follow: the
        month is incomplete. The function's own last line said it refuses a
        partial total, and the line before it returned one."""
        cost_report((200, {"data": [_bucket(TODAY, "100")], "has_more": True, "next_page": None}))
        assert spend_tracker.get_anthropic_spend() is None
        assert _budget().max_api_calls == 0
        with pytest.raises(RuntimeError, match="has_more without a next_page"):
            spend_tracker._fetch_cost_buckets("k", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")

    def test_pages_are_followed_to_the_end(self, cost_report):
        cost_report((200, {"data": [_bucket(YESTERDAY, "500")], "has_more": True, "next_page": "p2"}),
                    (200, {"data": [_bucket(TODAY, "300")], "has_more": False}))
        spend = spend_tracker.get_anthropic_spend()
        assert (spend["today_usd"], spend["month_to_date_usd"]) == (3.0, 8.0)
        assert "page=p2" in cost_report.seen[1][0]

    @pytest.mark.parametrize("result", [
        {"currency": "EUR", "amount": "100"},
        {"currency": "USD", "amount": "nan"},
        {"currency": "USD", "amount": "inf"},
        {"currency": "USD", "amount": "-500"},
        {"currency": "USD", "amount": "a lot"},
        "100",
    ])
    def test_a_figure_that_cannot_be_added_to_a_dollar_total_is_unknown(self, cost_report, result):
        cost_report((200, {"data": [{"starting_at": f"{TODAY}T00:00:00Z", "results": [result]}]}))
        assert spend_tracker.get_anthropic_spend() is None
        assert _budget().max_api_calls == 0

    @pytest.mark.parametrize("status", [401, 429, 500, 302])
    def test_an_error_status_is_unknown(self, cost_report, status):
        cost_report((status, {"data": [_bucket(TODAY, "0")]}))
        assert spend_tracker.get_anthropic_spend() is None
        assert _budget().max_api_calls == 0

    def test_an_answer_that_is_not_json_is_unknown(self, cost_report):
        cost_report((200, b"<html>maintenance</html>"))
        assert _budget().max_api_calls == 0


class TestTheSpendFigureItself:
    @pytest.mark.parametrize("spent", [None, "12", float("nan"), float("inf"), -1.0, True])
    def test_a_spend_source_that_returns_no_usable_number_gives_no_budget(self, monkeypatch, spent):
        monkeypatch.setattr(spend_tracker, "get_comprehensive_spend",
                            lambda conn: {"total_today_usd": spent, "source": "internal_tracking"})
        assert budget_mod._remaining_today(WorldState(), MagicMock(), 20.0) == 0.0

    def test_a_ledger_statement_that_returns_no_row_is_an_error_not_zero(self):
        with pytest.raises(RuntimeError, match="returned no row"):
            spend_tracker.ledger_figures(None, (0, 0))
        conn = MagicMock()
        conn.cursor.return_value.fetchone.return_value = None
        with pytest.raises(RuntimeError):
            spend_tracker.get_daily_spend(conn)


# ---------------------------------------------------------------------------
# The world state: a figure that was not read is None
# ---------------------------------------------------------------------------

class _FailingConn:
    """A connection whose statements fail when their text contains a needle."""

    def __init__(self, fail_on, rows=None):
        self.fail_on, self.rows, self.rollbacks = fail_on, rows or {}, 0

    def cursor(self):
        conn = self

        class _Cursor:
            def execute(self, sql, params=None):
                if any(needle in sql for needle in conn.fail_on):
                    raise RuntimeError("relation does not exist")
                self._sql = sql

            def fetchall(self):
                return next((rows for needle, rows in conn.rows.items() if needle in self._sql), [])

            def fetchone(self):
                return (0, 0)

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Cursor()

    def rollback(self):
        self.rollbacks += 1


class TestBuildWorldState:
    def test_a_failed_pipeline_read_leaves_the_pipeline_unknown_not_idle(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_DAILY_BUDGET", "20")
        state = build_world_state(_FailingConn(fail_on=("GROUP BY status",)), tick_number=1)
        assert state.active_builds is None and state.queued_builds is None
        assert state.preview_awaiting is None and state.failed_builds_24h is None
        assert customer_builds_active(state) is True, "unknown is not 'none running'"
        assert pipeline_idle(state) is False
        # The other refreshes still ran and their figures are known.
        assert state.monitored_apps_down == [] and state.pending_feedback == 0
        assert state.spend_known is True and state.daily_budget_remaining_usd == 20.0

    def test_a_query_that_fails_halfway_does_not_leave_half_an_answer(self):
        """The counts are read first and the failed-builds list second. If the
        second statement fails, the counts it shares a refresh with are not
        left looking like an answer."""
        conn = _FailingConn(fail_on=("LEFT JOIN dead_letter_builds",),
                            rows={"GROUP BY status": [("building", 2), ("queued", 1)]})
        state = build_world_state(conn, tick_number=1)
        assert state.active_builds is None and state.queued_builds is None

    def test_the_control_a_readable_pipeline(self):
        conn = _FailingConn(fail_on=(), rows={"GROUP BY status": [("building", 2), ("queued", 1)]})
        state = build_world_state(conn, tick_number=1)
        assert (state.active_builds, state.queued_builds, state.preview_awaiting) == (2, 1, 0)
        assert state.failed_builds_24h == []
        assert customer_builds_active(state) is True
        idle = build_world_state(_FailingConn(fail_on=()), tick_number=2)
        assert (idle.active_builds, idle.queued_builds) == (0, 0)
        assert customer_builds_active(idle) is False and pipeline_idle(idle) is True

    def test_each_refresh_fails_on_its_own(self):
        state = build_world_state(_FailingConn(fail_on=("monitor_apps", "beta_feedback")), tick_number=1)
        assert state.monitored_apps_down is None and state.monitored_apps_degraded is None
        assert state.pending_feedback is None and state.abandoned_intakes_24h is None
        assert state.active_builds == 0

    def test_a_failed_ledger_read_leaves_the_spend_unknown(self):
        state = build_world_state(_FailingConn(fail_on=("total_api_cost_usd",)), tick_number=1)
        assert state.spend_known is False
        assert state.api_cost_today_usd is None and state.daily_budget_remaining_usd is None
        assert budget_remaining_usd(state) == 0.0


# ---------------------------------------------------------------------------
# Every reader answers the restrictive way
# ---------------------------------------------------------------------------

def _unknown_pipeline(**known):
    return WorldState(active_builds=None, queued_builds=None, preview_awaiting=None,
                      failed_builds_24h=None, **known)


def _unknown_spend():
    state = WorldState(api_cost_today_usd=None, daily_budget_remaining_usd=None)
    state.spend_known = False
    return state


class TestReaders:
    def test_the_budget_yields_to_customer_builds_it_cannot_rule_out(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_DAILY_BUDGET", "20")
        monkeypatch.setenv("COORDINATOR_INTERVAL_S", "86400")
        monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
        unknown = compute_tick_budget(_unknown_pipeline(), conn=_Ledger())
        running = compute_tick_budget(WorldState(active_builds=1), conn=_Ledger())
        idle = compute_tick_budget(WorldState(active_builds=0), conn=_Ledger())
        assert unknown == running
        assert (unknown.allowed_models, unknown.max_api_calls, unknown.max_opus_calls) == ([HAIKU], 1, 0)
        assert unknown.max_cost_usd == pytest.approx(0.10)
        assert idle.max_api_calls == 100 and len(idle.allowed_models) == 3

    def test_no_test_build_is_started_into_a_pipeline_that_could_not_be_read(self, monkeypatch):
        monkeypatch.setenv("ENABLE_TEST_BUILDS", "true")
        conn = MagicMock()
        conn.cursor.return_value.fetchone.return_value = (0,)
        idle = WorldState(active_builds=0, queued_builds=0, daily_budget_remaining_usd=20.0)
        assert len(work_detection._check_test_build_needed(conn, idle)) == 1, "the control"
        assert work_detection._check_test_build_needed(conn, _unknown_pipeline(daily_budget_remaining_usd=20.0)) == []
        only_queue_unknown = WorldState(active_builds=0, queued_builds=None, daily_budget_remaining_usd=20.0)
        assert work_detection._check_test_build_needed(conn, only_queue_unknown) == []
        assert work_detection._check_test_build_needed(conn, _unknown_spend()) == []

    def test_no_build_failure_work_and_no_triage_from_an_unknown_list(self):
        triage = MagicMock()
        assert work_detection._check_failed_builds(MagicMock(), _unknown_pipeline(), None, triage) == []
        triage.authorize_model_call.assert_not_called()

    def test_no_app_down_work_from_an_unknown_registry(self):
        assert work_detection._check_apps_down(WorldState(monitored_apps_down=None)) == []
        down = WorldState(monitored_apps_down=[{"app_name": "a", "live_url": "https://a.example"}])
        assert len(work_detection._check_apps_down(down)) == 1

    def test_no_budget_alert_states_a_figure_that_is_not_known(self):
        """It used to file "Daily budget low: $0.00 remaining" for a spend
        that could not be read."""
        assert work_detection._check_budget_alert(_unknown_spend()) == []
        low = work_detection._check_budget_alert(WorldState(daily_budget_remaining_usd=3.0, api_cost_today_usd=17.0))
        assert [item.context["remaining_usd"] for item in low] == [3.0]
        assert work_detection._check_budget_alert(WorldState(daily_budget_remaining_usd=12.0)) == []

    def test_scoring_throttles_fully_when_the_spend_is_unknown(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_DAILY_BUDGET", "20")
        assert scorer._cost_penalty(_unknown_spend()) == 0.1
        assert scorer._cost_penalty(WorldState(daily_budget_remaining_usd=20.0)) == 1.0
        task = WorkItem(category="app_down", target_agent="infra_engineer")
        assert scorer.score_task(task, _unknown_spend()) < scorer.score_task(task, WorldState())

    def test_a_reader_that_forgets_to_ask_fails_instead_of_reading_zero(self):
        """What None buys: the old default made `state.active_builds > 0`
        quietly False."""
        with pytest.raises(TypeError):
            _unknown_pipeline().active_builds > 0
        with pytest.raises(TypeError):
            _unknown_spend().daily_budget_remaining_usd < 5.0

    def test_the_reconciliation_sweep_is_not_filed_when_its_last_run_cannot_be_read(self, monkeypatch):
        """"When did a sweep last run" used to come back as "never" on a query
        error, which files a sweep on every tick."""
        monkeypatch.setenv("AGENT_RECONCILIATION_ENABLED", "true")
        now = datetime.now(timezone.utc)
        broken = MagicMock()
        broken.cursor.side_effect = RuntimeError("connection lost")
        assert work_detection._run_check(
            broken, work_detection._check_reconciliation_sweep, broken, now, None) == []
        never_ran = MagicMock()
        never_ran.cursor.return_value.__enter__.return_value.fetchone.return_value = (None,)
        assert len(work_detection._check_reconciliation_sweep(never_ran, now, None)) == 1, "the control"

    def test_the_financial_agents_alert_says_unknown_not_zero(self):
        from coordinator.actuators import Actuators
        from coordinator.agents.financial_manager import FinancialManager

        actuators = Actuators("financial", "task-1", dry_run=True)
        sent = {}
        actuators.send_alert = lambda subject, body: sent.update(subject=subject, body=body) or True
        task = WorkItem(category="budget_alert", target_agent="financial",
                        context={"remaining_usd": 3.0, "cost_today_usd": 17.0})
        FinancialManager().execute(task, _unknown_pipeline(), HAIKU, actuators)
        assert "Active builds: unknown" in sent["body"] and "Failed builds (24h): unknown" in sent["body"]


# ---------------------------------------------------------------------------
# The failure circuit's states, from rows
# ---------------------------------------------------------------------------

def _history(*rows):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = list(rows)
    return conn


FAILED_RECENT, FAILED_OLD, DONE = ("failed", False), ("failed", True), ("completed", False)


class TestFailureCircuit:
    @pytest.mark.parametrize("rows, expected", [
        ([], ("closed", 0.0)),
        ([FAILED_RECENT, FAILED_RECENT], ("closed", 0.0)),                       # fewer than three
        ([FAILED_RECENT] * 5, ("open", 1.0)),
        ([FAILED_OLD] * 5, ("half_open", 1.0)),
        ([FAILED_OLD, FAILED_RECENT, FAILED_RECENT], ("half_open", 1.0)),        # only the newest decides "due"
        ([DONE] + [FAILED_OLD] * 9, ("closed", 0.9)),                            # one success closes it
        ([FAILED_RECENT, DONE, DONE, DONE, FAILED_RECENT], ("closed", 0.4)),     # below the threshold
        ([FAILED_RECENT, FAILED_RECENT, FAILED_RECENT, FAILED_RECENT, DONE], ("open", 0.8)),
    ])
    def test_states(self, rows, expected):
        from coordinator import tick
        state, rate = tick._failure_circuit(_history(*rows), "infra_engineer", "build_failure")
        assert (state, pytest.approx(rate)) == (expected[0], expected[1])

    def test_only_live_outcomes_are_asked_for_and_the_interval_is_the_configured_one(self):
        from coordinator import config, state, tick
        conn = _history()
        tick._failure_circuit(conn, "infra_engineer", "build_failure")
        sql, params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args
        assert state.LIVE_OUTCOME_SQL in sql and state.LIVE_OUTCOME_SQL == "(result->>%s) IS DISTINCT FROM 'true'"
        assert params == (config.FAILURE_RETRY_HOURS * 3600.0, "infra_engineer", "build_failure",
                          state.DRY_RUN_RESULT_KEY, config.FAILURE_RATE_WINDOW)
        assert sql.count("%s") == len(params)
        assert config.FAILURE_RETRY_HOURS == 6.0

    def test_the_scorers_performance_figures_ask_for_live_outcomes_too(self):
        """The second statement that counts task outcomes. With dry-run rows
        in it, a week of refused dry-run tasks would start an agent's live
        work at the lowest performance multiplier."""
        from coordinator import state
        conn = _history()
        assert scorer.build_performance_cache(conn) == {}
        sql, params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args
        assert state.LIVE_OUTCOME_SQL in sql
        assert params == (state.DRY_RUN_RESULT_KEY,)

    def test_a_history_that_cannot_be_read_is_unknown_and_the_transaction_is_cleared(self):
        from coordinator import tick
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("DB unavailable")
        assert tick._failure_circuit(conn, "infra_engineer", "build_failure") == ("unknown", 0.0)
        conn.rollback.assert_called_once()

    def test_the_tick_marks_a_row_with_the_mode_it_ran_in(self):
        from coordinator import tick
        assert json.loads(tick._task_result_json({"action": "x"}, dry_run=True)) == {"action": "x", "dry_run": True}
        assert json.loads(tick._task_result_json({"action": "x"}, dry_run=False)) == {"action": "x"}
        # An agent cannot mark its own live failure as a dry-run row.
        assert json.loads(tick._task_result_json({"dry_run": True}, dry_run=False)) == {}
        assert json.loads(tick._task_result_json(["not", "a", "dict"], dry_run=True)) == {
            "output": ["not", "a", "dict"], "dry_run": True}
