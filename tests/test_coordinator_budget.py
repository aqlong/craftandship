# tests/test_coordinator_budget.py
#
# Covers gaps in coordinator/budget.py not already in tests/test_coordinator.py.
#
# Already tested elsewhere (skipped here):
#   - active_builds > 0 yields Haiku-only budget
#   - no active builds gives full model list
#   - zero budget (zero dollars means zero calls)
#   - RPM_SPACING values
#   - per-tick 25% cap
#   - ticks_left uses COORDINATOR_INTERVAL_S

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from coordinator.budget import ModelCallMeter, TickBudget, compute_tick_budget
from coordinator.state import WorldState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(
    active_builds: int = 0,
    daily_budget_remaining: float = 20.0,
    api_cost_today: float = 0.0,
) -> WorldState:
    state = MagicMock(spec=WorldState)
    state.active_builds = active_builds
    state.daily_budget_remaining_usd = daily_budget_remaining
    state.api_cost_today_usd = api_cost_today
    state.failed_builds_24h = []
    state.monitored_apps_down = []
    state.queued_builds = 0
    return state


def _fixed_datetime(hour: int = 12, minute: int = 0, second: int = 0) -> MagicMock:
    """Return a mock for coordinator.budget.datetime that makes ticks_left predictable."""
    now = datetime(2026, 4, 12, hour, minute, second, tzinfo=timezone.utc)
    mock_dt = MagicMock()
    mock_dt.now.return_value = now
    return mock_dt


# ---------------------------------------------------------------------------
# TestBudgetSpendTracker
# ---------------------------------------------------------------------------

class TestBudgetSpendTracker:
    """Verify the spend_tracker integration path in compute_tick_budget."""

    def test_uses_spend_tracker_when_conn_provided(self):
        """With a valid conn, get_comprehensive_spend is called and remaining is used."""
        state = _make_state(active_builds=0, daily_budget_remaining=0.0)

        mock_conn = MagicMock()
        spend_return = {"total_today_usd": 5.0, "source": "internal_tracking"}

        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0",
                                     "COORDINATOR_INTERVAL_S": "1800"}):
            with patch("coordinator.spend_tracker.get_comprehensive_spend",
                       return_value=spend_return) as mock_spend:
                with patch("coordinator.budget.datetime", _fixed_datetime(hour=23, minute=59, second=58)):
                    result = compute_tick_budget(state, conn=mock_conn)

        mock_spend.assert_called_once_with(mock_conn)
        # remaining = 20.0 - 5.0 = 15.0; ticks_left=1; per_tick=15.0 capped at 20*0.25=5.0
        # max_cost_usd should be 5.0 (25% cap)
        assert result.max_cost_usd == pytest.approx(5.0, abs=0.01)

    def test_uses_spend_tracker_when_conn_provided_low_remaining(self):
        """Remaining derived from spend_tracker is reflected in the budget."""
        state = _make_state(active_builds=0, daily_budget_remaining=99.0)  # stale state value

        mock_conn = MagicMock()
        # spend_tracker says $18 spent out of $20 budget => $2 remaining
        spend_return = {"total_today_usd": 18.0, "source": "anthropic_api"}

        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0",
                                     "COORDINATOR_INTERVAL_S": "1800"}):
            with patch("coordinator.spend_tracker.get_comprehensive_spend",
                       return_value=spend_return):
                with patch("coordinator.budget.datetime", _fixed_datetime(hour=23, minute=59, second=58)):
                    result = compute_tick_budget(state, conn=mock_conn)

        # remaining=2.0, ticks_left=1, per_tick=2.0 (less than 20*0.25=5)
        assert result.max_cost_usd == pytest.approx(2.0, abs=0.01)

    def test_no_budget_when_the_spend_lookup_raises(self):
        """Without an admin key the ledger is the spend source: if its query
        raises, spend is unknown. The $8 the world state read from the same
        tables earlier in the tick is not budgeted against."""
        state = _make_state(active_builds=0, daily_budget_remaining=8.0)
        mock_conn = MagicMock()

        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0",
                                     "COORDINATOR_INTERVAL_S": "1800",
                                     "ANTHROPIC_ADMIN_KEY": ""}):
            with patch("coordinator.spend_tracker.get_comprehensive_spend",
                       side_effect=RuntimeError("DB unavailable")):
                with patch("coordinator.budget.datetime", _fixed_datetime(hour=23, minute=59, second=58)):
                    result = compute_tick_budget(state, conn=mock_conn)

        mock_conn.rollback.assert_called()  # the failed query's transaction is cleared
        assert result.max_cost_usd == 0.0
        assert result.max_api_calls == 0
        assert result.max_opus_calls == 0

    def test_conn_none_means_no_budget(self):
        """Without a connection no spend source can be read: the tick gets
        nothing, whatever the world state says is left."""
        state = _make_state(active_builds=0, daily_budget_remaining=12.0)

        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0",
                                     "COORDINATOR_INTERVAL_S": "1800"}):
            with patch("coordinator.spend_tracker.get_comprehensive_spend") as mock_spend:
                with patch("coordinator.budget.datetime", _fixed_datetime(hour=23, minute=59, second=58)):
                    result = compute_tick_budget(state, conn=None)

        mock_spend.assert_not_called()
        assert result.max_cost_usd == 0.0
        assert result.max_api_calls == 0

    def test_unknown_world_state_spend_means_no_budget(self):
        """build_world_state could not read the ledger at the start of the tick."""
        state = _make_state(active_builds=0, daily_budget_remaining=12.0)
        state.spend_known = False
        spend_return = {"total_today_usd": 0.0, "source": "internal_tracking"}

        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0",
                                     "COORDINATOR_INTERVAL_S": "1800"}):
            with patch("coordinator.spend_tracker.get_comprehensive_spend",
                       return_value=spend_return):
                with patch("coordinator.budget.datetime", _fixed_datetime(hour=23, minute=59, second=58)):
                    result = compute_tick_budget(state, conn=MagicMock())

        assert result.max_cost_usd == 0.0
        assert result.max_api_calls == 0

    def test_spend_tracker_zero_spent_gives_full_remaining(self):
        """When $0 spent, full daily budget is available."""
        state = _make_state(active_builds=0, daily_budget_remaining=0.0)
        mock_conn = MagicMock()
        spend_return = {"total_today_usd": 0.0, "source": "internal_tracking"}

        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0",
                                     "COORDINATOR_INTERVAL_S": "1800"}):
            with patch("coordinator.spend_tracker.get_comprehensive_spend",
                       return_value=spend_return):
                with patch("coordinator.budget.datetime", _fixed_datetime(hour=23, minute=59, second=58)):
                    result = compute_tick_budget(state, conn=mock_conn)

        # remaining=20.0, ticks_left=1, per_tick=20.0 -> capped at 5.0
        assert result.max_cost_usd == pytest.approx(5.0, abs=0.01)

    def test_spend_tracker_overspend_clamps_to_zero(self):
        """If spend exceeds daily budget, remaining clamps to 0 (not negative)."""
        state = _make_state(active_builds=0, daily_budget_remaining=5.0)
        mock_conn = MagicMock()
        spend_return = {"total_today_usd": 25.0, "source": "anthropic_api"}  # over budget

        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0",
                                     "COORDINATOR_INTERVAL_S": "1800"}):
            with patch("coordinator.spend_tracker.get_comprehensive_spend",
                       return_value=spend_return):
                with patch("coordinator.budget.datetime", _fixed_datetime(hour=23, minute=59, second=58)):
                    result = compute_tick_budget(state, conn=mock_conn)

        # max(0, 20-25) = 0.0 remaining; per_tick=0.0; max_cost_usd=0.0
        assert result.max_cost_usd == pytest.approx(0.0, abs=0.001)
        assert result.max_api_calls == 0


# ---------------------------------------------------------------------------
# TestSpendSourceFailsClosed
# ---------------------------------------------------------------------------

def _ledger_conn(build_cost: float, agent_cost: float) -> MagicMock:
    """A connection whose ledger queries (get_daily_spend) return these sums."""
    conn = MagicMock()
    conn.cursor.return_value.fetchone.side_effect = [(build_cost, 1), (agent_cost, 1)]
    return conn


class TestSpendSourceFailsClosed:
    """The budget runs on one configured spend source and gives the tick no
    budget when that source cannot be read. These tests run the real
    spend_tracker; only the cost-report HTTP call and the cursor are stubbed."""

    DUMMY_KEY = "dummy-" + "admin-key"

    @pytest.fixture(autouse=True)
    def _fresh_cost_report_cache(self):
        from coordinator.spend_tracker import _anthropic_cache
        _anthropic_cache["value"] = None
        _anthropic_cache["expires"] = None
        yield
        _anthropic_cache["value"] = None
        _anthropic_cache["expires"] = None

    def _budget(self, state, conn, admin_key, urlopen):
        env = {"COORDINATOR_DAILY_BUDGET": "20.0", "COORDINATOR_INTERVAL_S": "1800",
               "ANTHROPIC_ADMIN_KEY": admin_key}
        with patch.dict(os.environ, env), \
             patch("coordinator.spend_tracker.bounded_urlopen", urlopen), \
             patch("coordinator.budget.datetime", _fixed_datetime(hour=23, minute=59, second=58)):
            return compute_tick_budget(state, conn=conn)

    def test_cost_report_failure_with_admin_key_means_no_budget(self):
        """The ledger says $1 was spent, but with an admin key configured the
        cost report is the source; when it fails, the smaller ledger figure must
        not be budgeted against."""
        state = _make_state(daily_budget_remaining=19.0)
        urlopen = MagicMock(side_effect=OSError("cost report unreachable"))
        result = self._budget(state, _ledger_conn(1.0, 0.0), self.DUMMY_KEY, urlopen)
        urlopen.assert_called_once()
        assert result.max_cost_usd == 0.0
        assert result.max_api_calls == 0
        assert result.max_opus_calls == 0

    def test_cost_report_figure_is_used_when_it_can_be_read(self):
        import json
        body = {"data": [{"starting_at": "2026-04-12T00:00:00Z",
                          "results": [{"currency": "USD", "amount": "1700"}]}],
                "has_more": False}
        resp = MagicMock()
        resp.read.return_value = json.dumps(body).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        state = _make_state(daily_budget_remaining=19.0)
        with patch("coordinator.spend_tracker.datetime") as clock:
            clock.now.return_value = datetime(2026, 4, 12, 23, 59, 58, tzinfo=timezone.utc)
            result = self._budget(state, _ledger_conn(1.0, 0.0), self.DUMMY_KEY,
                                  MagicMock(return_value=resp))
        # $17.00 on the cost report (1700 cents), $1 on the ledger: $3 is left, not $19
        assert result.max_cost_usd == pytest.approx(3.0, abs=0.01)

    def test_ledger_is_the_source_when_no_admin_key_is_set(self):
        state = _make_state(daily_budget_remaining=0.0)
        urlopen = MagicMock(side_effect=AssertionError("no cost report without a key"))
        result = self._budget(state, _ledger_conn(17.0, 1.0), "", urlopen)
        urlopen.assert_not_called()
        assert result.max_cost_usd == pytest.approx(2.0, abs=0.01)

    def test_ledger_failure_with_admin_key_set_means_no_budget(self):
        """The world-state figure is the ledger's, not the configured source's."""
        state = _make_state(daily_budget_remaining=8.0)
        conn = MagicMock()
        conn.cursor.return_value.execute.side_effect = RuntimeError("relation does not exist")
        result = self._budget(state, conn, self.DUMMY_KEY, MagicMock())
        conn.rollback.assert_called()
        assert result.max_cost_usd == 0.0

    def test_ledger_failure_without_admin_key_means_no_budget(self):
        """The ledger is the source and it cannot be read: the $2 the world
        state read from it earlier is not a substitute."""
        state = _make_state(daily_budget_remaining=2.0)
        conn = MagicMock()
        conn.cursor.return_value.execute.side_effect = RuntimeError("relation does not exist")
        result = self._budget(state, conn, "", MagicMock())
        conn.rollback.assert_called()
        assert result.max_cost_usd == 0.0
        assert result.max_api_calls == 0


# ---------------------------------------------------------------------------
# TestTickBudgetReturnShape
# ---------------------------------------------------------------------------

class TestTickBudgetReturnShape:
    """Sanity-check TickBudget dataclass fields for normal operation paths."""

    def _budget(self, spent_today, hour=12, minute=0, second=0, active_builds=0):
        state = _make_state(active_builds=active_builds)
        with patch.dict(os.environ, {"COORDINATOR_DAILY_BUDGET": "20.0",
                                     "COORDINATOR_INTERVAL_S": "1800",
                                     "ANTHROPIC_ADMIN_KEY": ""}):
            with patch("coordinator.budget.datetime", _fixed_datetime(hour, minute, second)):
                return compute_tick_budget(state, conn=_ledger_conn(spent_today, 0.0))

    def test_normal_budget_returns_tick_budget_instance(self):
        assert isinstance(self._budget(0.0), TickBudget)

    def test_all_three_models_available_when_not_constrained(self):
        result = self._budget(0.0)
        assert "claude-haiku-4-5" in result.allowed_models
        assert "claude-sonnet-4-6" in result.allowed_models
        assert "claude-opus-4-6" in result.allowed_models

    def test_call_counts_are_what_the_dollars_pay_for(self):
        # one tick left, $2.00 unspent: 40 Haiku calls at $0.05, 2 Opus at $0.80
        result = self._budget(18.0, hour=23, minute=59, second=58)
        assert result.max_cost_usd == pytest.approx(2.0)
        assert result.max_api_calls == 40
        assert result.max_opus_calls == 2

    def test_max_api_calls_is_zero_when_the_tick_cannot_pay_for_a_call(self):
        # one tick left and a tenth of a cent unspent
        result = self._budget(19.999, hour=23, minute=59, second=58)
        assert 0 < result.max_cost_usd < 0.05
        assert result.max_api_calls == 0
        assert result.max_opus_calls == 0

    def test_active_builds_get_one_call_only_while_it_is_affordable(self):
        assert self._budget(0.0, active_builds=1).max_api_calls == 1
        assert self._budget(19.999, hour=23, minute=59, second=58, active_builds=1).max_api_calls == 0


# ---------------------------------------------------------------------------
# TestModelCallMeter
# ---------------------------------------------------------------------------

HAIKU, SONNET, OPUS = "claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6"


class TestModelCallMeter:
    """The per-tick counter every coordinator model call is granted by."""

    def test_zero_budget_grants_nothing(self):
        meter = ModelCallMeter(TickBudget())
        assert meter.grant(HAIKU) is False
        assert meter.granted() == (0, 0.0)

    def test_grants_stop_at_the_call_count(self):
        meter = ModelCallMeter(TickBudget(max_api_calls=2, max_cost_usd=10.0, allowed_models=[HAIKU]))
        assert [meter.grant(HAIKU) for _ in range(4)] == [True, True, False, False]
        assert meter.granted() == (2, pytest.approx(0.10))

    def test_grants_stop_when_the_dollars_do_not_cover_the_estimate(self):
        # $0.12 pays for two Haiku calls at $0.05; the third is refused although calls remain
        meter = ModelCallMeter(TickBudget(max_api_calls=10, max_cost_usd=0.12, allowed_models=[HAIKU]))
        assert [meter.grant(HAIKU) for _ in range(3)] == [True, True, False]

    def test_a_dearer_model_costs_its_own_estimate(self):
        meter = ModelCallMeter(TickBudget(max_api_calls=10, max_cost_usd=0.20,
                                          allowed_models=[HAIKU, SONNET]))
        assert meter.grant(SONNET) is True       # $0.12
        assert meter.grant(SONNET) is False      # $0.08 left
        assert meter.grant(HAIKU) is True        # $0.05
        assert meter.granted() == (2, pytest.approx(0.17))

    def test_a_model_the_tick_does_not_allow_is_refused(self):
        meter = ModelCallMeter(TickBudget(max_api_calls=5, max_cost_usd=5.0, allowed_models=[HAIKU]))
        assert meter.grant(SONNET) is False
        assert meter.grant("some-other-model") is False
        assert meter.granted() == (0, 0.0)

    def test_opus_has_its_own_sub_cap(self):
        meter = ModelCallMeter(TickBudget(max_api_calls=5, max_cost_usd=5.0, max_opus_calls=1,
                                          allowed_models=[HAIKU, OPUS]))
        assert [meter.grant(OPUS), meter.grant(OPUS)] == [True, False]
        assert meter.grant(HAIKU) is True

    def test_parallel_grants_never_exceed_the_allowance(self):
        import threading

        meter = ModelCallMeter(TickBudget(max_api_calls=50, max_cost_usd=100.0, allowed_models=[HAIKU]))
        results = []
        start = threading.Barrier(8)

        def worker():
            start.wait()
            results.extend(meter.grant(HAIKU) for _ in range(20))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 50
        assert meter.granted()[0] == 50
