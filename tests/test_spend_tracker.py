# tests/test_spend_tracker.py: Spend tracker tests

import json
from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock, patch
from coordinator import spend_tracker
from coordinator.spend_tracker import (
    get_daily_spend, get_anthropic_spend, get_comprehensive_spend, _anthropic_cache,
)

# The instant the Anthropic-spend tests run at. Pinned so the day and month
# windows the code computes cannot straddle UTC midnight or a month boundary
# while a test runs, and so no test depends on today's date.
_NOW = datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc)
_TODAY = _NOW.strftime("%Y-%m-%d")
_MONTH_FIRST = _NOW.replace(day=1).strftime("%Y-%m-%d")


def _freeze_spend_clock(monkeypatch, instant=_NOW):
    """Make coordinator.spend_tracker see `instant` as the current UTC time."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is None else instant.astimezone(tz)

    monkeypatch.setattr(spend_tracker, "datetime", _Frozen)


@pytest.fixture(autouse=True)
def _fresh_spend_cache(monkeypatch):
    """get_anthropic_spend memoizes in a module-level dict that outlives the
    test; clear it before and after so one test's cached figures can never
    be served to another, whatever order the tests run in."""
    _anthropic_cache["value"] = None
    _anthropic_cache["expires"] = None
    _freeze_spend_clock(monkeypatch)
    yield
    _anthropic_cache["value"] = None
    _anthropic_cache["expires"] = None


def _mock_conn(build_cost=5.0, build_count=3, agent_cost=0.5, agent_count=8):
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.fetchone = MagicMock(side_effect=[
        (build_cost, build_count),
        (agent_cost, agent_count),
    ])
    return conn


class TestGetDailySpend:
    def test_aggregates_build_and_agent_costs(self):
        conn = _mock_conn(build_cost=4.50, build_count=2, agent_cost=0.25, agent_count=5)
        result = get_daily_spend(conn)

        assert result["build_pipeline_usd"] == 4.50
        assert result["agent_tasks_usd"] == 0.25
        assert result["total_tracked_usd"] == 4.75
        assert result["builds_today"] == 2
        assert result["agent_tasks_today"] == 5

    def test_handles_zero_spend(self):
        conn = _mock_conn(build_cost=0, build_count=0, agent_cost=0, agent_count=0)
        result = get_daily_spend(conn)

        assert result["total_tracked_usd"] == 0.0


class TestGetAnthropicSpend:
    def test_returns_none_without_admin_key(self):
        _anthropic_cache["value"] = None
        _anthropic_cache["expires"] = None
        with patch.dict("os.environ", {}, clear=True):
            assert get_anthropic_spend() is None

    @staticmethod
    def _response(payload: dict):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(payload).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    @staticmethod
    def _bucket(day: str, amount: str, currency: str = "USD"):
        return {
            "starting_at": f"{day}T00:00:00Z",
            "ending_at": f"{day}T23:59:59Z",
            "results": [{"currency": currency, "amount": amount, "model": None}],
        }

    def test_queries_api_with_admin_key(self):
        """Real cost_report shape: results[].amount is a decimal STRING in CENTS."""
        _anthropic_cache["value"] = None
        _anthropic_cache["expires"] = None
        today, month_first = _TODAY, _MONTH_FIRST
        payload = {"data": [self._bucket(month_first, "1234.56"),
                            self._bucket(today, "250.00")],
                   "has_more": False, "next_page": None}

        with patch.dict("os.environ", {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-test"}):
            with patch("coordinator.spend_tracker.bounded_urlopen", return_value=self._response(payload)) as mock_open:
                result = get_anthropic_spend()

        assert result is not None
        assert result["source"] == "anthropic_admin_api"
        assert result["today_usd"] == 2.50          # 250 cents, divided once
        assert result["month_to_date_usd"] == 14.85  # (1234.56 + 250.00) cents
        url = mock_open.call_args[0][0].full_url
        assert "bucket_width=1d" in url and "limit=31" in url
        # The query window is the current month up to the end of today.
        assert "starting_at=2026-10-01T00%3A00%3A00Z" in url
        assert "ending_at=2026-10-16T00%3A00%3A00Z" in url

    def test_on_the_first_of_the_month_one_bucket_is_both_today_and_the_month(self, monkeypatch):
        first = datetime(2026, 11, 1, 0, 5, tzinfo=timezone.utc)
        _freeze_spend_clock(monkeypatch, first)
        payload = {"data": [self._bucket("2026-11-01", "1485.56")],
                   "has_more": False, "next_page": None}
        with patch.dict("os.environ", {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-test"}):
            with patch("coordinator.spend_tracker.bounded_urlopen", return_value=self._response(payload)):
                result = get_anthropic_spend()
        assert result["today_usd"] == 14.86
        assert result["month_to_date_usd"] == 14.86

    def test_today_is_only_todays_bucket(self):
        _anthropic_cache["value"] = None
        _anthropic_cache["expires"] = None
        payload = {"data": [self._bucket(_TODAY, "100")], "has_more": False}
        with patch.dict("os.environ", {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-test"}):
            with patch("coordinator.spend_tracker.bounded_urlopen", return_value=self._response(payload)):
                result = get_anthropic_spend()
        assert result["today_usd"] == 1.00
        assert result["month_to_date_usd"] == 1.00

    def test_follows_pagination(self):
        _anthropic_cache["value"] = None
        _anthropic_cache["expires"] = None
        today = _TODAY
        page1 = {"data": [self._bucket("2000-01-01", "500")], "has_more": True, "next_page": "tok+/="}
        page2 = {"data": [self._bucket(today, "300")], "has_more": False, "next_page": None}
        with patch.dict("os.environ", {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-test"}):
            with patch("coordinator.spend_tracker.bounded_urlopen",
                       side_effect=[self._response(page1), self._response(page2)]) as mock_open:
                result = get_anthropic_spend()
        assert mock_open.call_count == 2
        assert "page=tok%2B%2F%3D" in mock_open.call_args_list[1][0][0].full_url
        assert result["month_to_date_usd"] == 8.00

    def test_incomplete_pagination_is_a_failure_not_a_low_number(self):
        """A partial month would under-report spend; refuse it (None => fall back)."""
        _anthropic_cache["value"] = None
        _anthropic_cache["expires"] = None
        endless = {"data": [self._bucket("2000-01-01", "1")], "has_more": True, "next_page": "again"}
        with patch.dict("os.environ", {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-test"}):
            with patch("coordinator.spend_tracker.bounded_urlopen", side_effect=lambda *a, **k: self._response(endless)):
                assert get_anthropic_spend() is None

    def test_a_result_in_another_currency_makes_the_spend_unknown(self):
        """This test used to pin a non-USD result as "ignored", which read
        100 euros of spend as $0.00 spent. A figure that cannot be added to
        a dollar total leaves the total unknown."""
        _anthropic_cache["value"] = None
        _anthropic_cache["expires"] = None
        payload = {"data": [self._bucket(_TODAY, "100", currency="EUR")], "has_more": False}
        with patch.dict("os.environ", {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-test"}):
            with patch("coordinator.spend_tracker.bounded_urlopen", return_value=self._response(payload)):
                assert get_anthropic_spend() is None

    def test_malformed_payload_returns_none(self):
        _anthropic_cache["value"] = None
        _anthropic_cache["expires"] = None
        with patch.dict("os.environ", {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-test"}):
            resp = MagicMock()
            resp.read.return_value = b"not json"
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            with patch("coordinator.spend_tracker.bounded_urlopen", return_value=resp):
                assert get_anthropic_spend() is None

    def test_returns_none_on_401(self):
        _anthropic_cache["value"] = None
        _anthropic_cache["expires"] = None
        import urllib.error
        with patch.dict("os.environ", {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-bad"}):
            with patch("coordinator.spend_tracker.bounded_urlopen",
                       side_effect=urllib.error.HTTPError(None, 401, "Unauthorized", {}, None)):
                result = get_anthropic_spend()
        assert result is None


class TestGetComprehensiveSpend:
    def test_uses_anthropic_when_available(self):
        conn = _mock_conn()
        anthropic_data = {"today_usd": 15.0, "month_to_date_usd": 200.0, "source": "anthropic_admin_api"}

        with patch("coordinator.spend_tracker.get_anthropic_spend", return_value=anthropic_data):
            result = get_comprehensive_spend(conn)

        assert result["total_today_usd"] == 15.0
        assert result["source"] == "anthropic_api"

    def test_falls_back_to_tracked(self):
        conn = _mock_conn(build_cost=3.0, agent_cost=0.5)

        with patch("coordinator.spend_tracker.get_anthropic_spend", return_value=None):
            result = get_comprehensive_spend(conn)

        assert result["total_today_usd"] == 3.5
        assert result["source"] == "internal_tracking"


class TestFailClosed:
    """A failed lookup must never read as zero spend, and a failed cost report
    must be distinguishable from "no cost report configured": the ledger figure
    reported in its place leaves out the web tier's spend, so coordinator.budget
    refuses to budget against it (tests/test_coordinator_budget.py)."""

    def test_anthropic_failure_falls_back_to_tracked_spend(self):
        conn = _mock_conn(build_cost=12.0, agent_cost=3.0)
        with patch("coordinator.spend_tracker.get_anthropic_spend", return_value=None):
            result = get_comprehensive_spend(conn)
        assert result["total_today_usd"] == 15.0
        assert result["source"] == "internal_tracking"

    def test_a_failed_cost_report_is_flagged_when_the_key_is_set(self):
        conn = _mock_conn(build_cost=12.0, agent_cost=3.0)
        with patch.dict("os.environ", {"ANTHROPIC_ADMIN_KEY": "dummy-" + "admin-key"}), \
             patch("coordinator.spend_tracker.bounded_urlopen", side_effect=OSError("unreachable")) as urlopen:
            result = get_comprehensive_spend(conn)
        urlopen.assert_called_once()
        assert result["provider_failed"] is True
        assert result["anthropic"] is None
        assert result["total_today_usd"] == 15.0 and result["source"] == "internal_tracking"

    def test_no_admin_key_is_not_a_provider_failure(self):
        with patch.dict("os.environ", {"ANTHROPIC_ADMIN_KEY": ""}):
            result = get_comprehensive_spend(_mock_conn())
        assert result["provider_failed"] is False
        assert result["source"] == "internal_tracking"

    def test_a_readable_cost_report_is_not_a_provider_failure(self):
        anthropic_data = {"today_usd": 15.0, "month_to_date_usd": 200.0, "source": "anthropic_admin_api"}
        with patch.dict("os.environ", {"ANTHROPIC_ADMIN_KEY": "dummy-" + "admin-key"}), \
             patch("coordinator.spend_tracker.get_anthropic_spend", return_value=anthropic_data):
            result = get_comprehensive_spend(_mock_conn())
        assert result["provider_failed"] is False and result["source"] == "anthropic_api"
