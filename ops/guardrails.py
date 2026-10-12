# ops/guardrails.py - Safety controls for the ops agent
#
# Circuit breaker: stops auto-fixes after N fixes in a time window.
# Cost guard: caps playbook (fixer) runs per day. The playbooks are
#   deterministic and make no model calls; the "claude_call" audit rows and
#   the OPS_MAX_CLAUDE_CALLS_PER_DAY name are historical (an earlier version
#   asked a model to write the fix) and now simply count fixer invocations.
# Severity filter: only allows infrastructure fixes, never app logic.

import logging

from coordinator.config import env_int
from ops.audit import count_actions, count_claude_calls

logger = logging.getLogger("ops.guardrails")


class CircuitBreaker:
    """Stops auto-fixes after too many actions in a time window.

    Defaults: 3 fixes per 60 minutes. Override with env vars
    OPS_CIRCUIT_BREAKER_LIMIT and OPS_CIRCUIT_BREAKER_WINDOW_MINUTES.
    """

    def __init__(self):
        # A value that is not a whole number of at least 1 means the default.
        self.limit = env_int("OPS_CIRCUIT_BREAKER_LIMIT", 3, min_val=1)
        self.window_minutes = env_int("OPS_CIRCUIT_BREAKER_WINDOW_MINUTES", 60, min_val=1)

    def can_proceed(self) -> bool:
        """Return True if under the circuit breaker limit."""
        recent = count_actions(
            hours=self.window_minutes / 60,
            action_type="fix_applied",
        )
        if recent >= self.limit:
            logger.warning(
                "Circuit breaker tripped: %d fixes in last %d minutes (limit %d)",
                recent, self.window_minutes, self.limit,
            )
            return False
        return True

    def remaining(self) -> int:
        """Return how many fixes are still allowed in the current window."""
        recent = count_actions(
            hours=self.window_minutes / 60,
            action_type="fix_applied",
        )
        return max(0, self.limit - recent)


class CostGuard:
    """Caps fixer runs per day (counted as "claude_call" audit rows).

    Default: 20 runs per 24 hours. Override with OPS_MAX_CLAUDE_CALLS_PER_DAY.
    Only live (dry_run = FALSE) rows count, so a dry-run agent is uncapped; it
    has no side effects.
    """

    def __init__(self):
        self.daily_limit = env_int("OPS_MAX_CLAUDE_CALLS_PER_DAY", 20, min_val=0)

    def can_call_claude(self) -> bool:
        """Return True if under the daily fixer-run cap."""
        used = count_claude_calls(hours=24)
        if used >= self.daily_limit:
            logger.warning(
                "Cost guard: %d fixer runs in 24h (limit %d)",
                used, self.daily_limit,
            )
            return False
        return True

    def remaining(self) -> int:
        """Return how many fixer runs are left today."""
        used = count_claude_calls(hours=24)
        return max(0, self.daily_limit - used)


class SeverityFilter:
    """Only allows the two categories that have a playbook (a variable on the
    worker service, a redeploy of the worker service). Never app logic, and
    nothing that would need a change to a file."""

    ALLOWED_CATEGORIES = frozenset({
        "env_config",
        "railway_crash",
    })

    def is_auto_fixable(self, category: str) -> bool:
        """Return True if the error category can be auto-fixed."""
        return category in self.ALLOWED_CATEGORIES
