# tests/test_ops_guardrails.py - Tests for ops agent guardrails

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = str(Path(__file__).parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ops.guardrails import CircuitBreaker, CostGuard, SeverityFilter


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    @patch("ops.guardrails.count_actions", return_value=0)
    def test_can_proceed_when_under_limit(self, mock_count):
        cb = CircuitBreaker()
        assert cb.can_proceed() is True

    @patch("ops.guardrails.count_actions", return_value=3)
    def test_blocked_when_at_limit(self, mock_count):
        cb = CircuitBreaker()
        assert cb.can_proceed() is False

    @patch("ops.guardrails.count_actions", return_value=5)
    def test_blocked_when_over_limit(self, mock_count):
        cb = CircuitBreaker()
        assert cb.can_proceed() is False

    @patch("ops.guardrails.count_actions", return_value=1)
    def test_remaining_count(self, mock_count):
        cb = CircuitBreaker()
        assert cb.remaining() == 2

    @patch("ops.guardrails.count_actions", return_value=0)
    def test_env_var_override(self, mock_count):
        with patch.dict(os.environ, {"OPS_CIRCUIT_BREAKER_LIMIT": "10"}):
            cb = CircuitBreaker()
            assert cb.limit == 10
            assert cb.can_proceed() is True


# ---------------------------------------------------------------------------
# CostGuard
# ---------------------------------------------------------------------------

class TestCostGuard:
    @patch("ops.guardrails.count_claude_calls", return_value=0)
    def test_can_call_when_under_budget(self, mock_count):
        cg = CostGuard()
        assert cg.can_call_claude() is True

    @patch("ops.guardrails.count_claude_calls", return_value=20)
    def test_blocked_when_at_budget(self, mock_count):
        cg = CostGuard()
        assert cg.can_call_claude() is False

    @patch("ops.guardrails.count_claude_calls", return_value=5)
    def test_remaining_calls(self, mock_count):
        cg = CostGuard()
        assert cg.remaining() == 15

    @patch("ops.guardrails.count_claude_calls", return_value=0)
    def test_env_var_override(self, mock_count):
        with patch.dict(os.environ, {"OPS_MAX_CLAUDE_CALLS_PER_DAY": "50"}):
            cg = CostGuard()
            assert cg.daily_limit == 50


# ---------------------------------------------------------------------------
# SeverityFilter
# ---------------------------------------------------------------------------

class TestSeverityFilter:
    @pytest.mark.parametrize("category", ["version_mismatch", "missing_dep", "missing_tool", "dockerfile"])
    def test_a_failure_that_would_need_a_file_change_is_not_auto_fixable(self, category):
        """These categories had playbooks that committed to the repository.
        They are classification labels now: the failure is reported."""
        assert SeverityFilter().is_auto_fixable(category) is False

    def test_the_fixable_categories_are_the_ones_with_a_playbook(self):
        from ops.playbook import PLAYBOOKS
        assert SeverityFilter.ALLOWED_CATEGORIES == set(PLAYBOOKS) == {"env_config", "railway_crash"}

    @pytest.mark.parametrize("name, default", [
        ("OPS_CIRCUIT_BREAKER_LIMIT", 3), ("OPS_CIRCUIT_BREAKER_WINDOW_MINUTES", 60),
    ])
    @pytest.mark.parametrize("raw", ["three", "", "0", "-1", "2.5"])
    def test_a_mistyped_breaker_setting_means_the_default(self, monkeypatch, name, default, raw):
        """int() on the raw text used to raise here, on every scan."""
        from ops.guardrails import CircuitBreaker
        monkeypatch.setenv(name, raw)
        breaker = CircuitBreaker()
        assert getattr(breaker, "limit" if name.endswith("LIMIT") else "window_minutes") == default

    def test_a_mistyped_daily_cap_means_the_default(self, monkeypatch):
        from ops.guardrails import CostGuard
        monkeypatch.setenv("OPS_MAX_CLAUDE_CALLS_PER_DAY", "twenty")
        assert CostGuard().daily_limit == 20

    def test_env_config_is_fixable(self):
        sf = SeverityFilter()
        assert sf.is_auto_fixable("env_config") is True

    def test_railway_crash_is_fixable(self):
        sf = SeverityFilter()
        assert sf.is_auto_fixable("railway_crash") is True

    def test_app_logic_not_fixable(self):
        sf = SeverityFilter()
        assert sf.is_auto_fixable("app_logic") is False

    def test_unknown_not_fixable(self):
        sf = SeverityFilter()
        assert sf.is_auto_fixable("unknown") is False
