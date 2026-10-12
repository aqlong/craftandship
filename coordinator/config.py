# coordinator/config.py: Centralized threshold configuration
#
# All production-impacted thresholds are read from environment variables
# at import time with sensible defaults matching previous hardcoded values.
# This ensures backward compatibility when no env vars are set.

import logging
import math
import os

logger = logging.getLogger("coordinator.config")


def env_int(key: str, default: int, min_val: int = None) -> int:
    """Read an integer env var with optional minimum bound.

    A value that is not a whole number, or is below `min_val`, is logged and
    replaced by `default`: a mistyped setting must not stop the process that
    reads it at import. The worker, the monitor and the ops agent read their
    numeric settings through this function and env_float as well.
    """
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        val = int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r, using default %d", key, raw, default)
        return default
    if min_val is not None and val < min_val:
        logger.warning("%s=%d below minimum %d, using default %d", key, val, min_val, default)
        return default
    return val


def env_float(key: str, default: float, min_val: float = None,
              max_val: float = None) -> float:
    """Read a float env var with optional min/max clamping.

    A value that is not a finite number is logged and replaced by `default`.
    """
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r, using default %.2f", key, raw, default)
        return default
    if not math.isfinite(val):
        logger.warning("Non-finite float for %s=%r, using default %.2f", key, raw, default)
        return default
    if min_val is not None and val < min_val:
        logger.warning("%s=%.2f below minimum %.2f, clamping", key, val, min_val)
        val = min_val
    if max_val is not None and val > max_val:
        logger.warning("%s=%.2f above maximum %.2f, clamping", key, val, max_val)
        val = max_val
    return val


# The names this module's own settings use.
_int = env_int
_float = env_float


# Failure detection
FAILURE_RATE_WINDOW = _int("COORDINATOR_FAILURE_WINDOW", 10, min_val=1)
FAILURE_RATE_THRESHOLD = _float("COORDINATOR_FAILURE_THRESHOLD", 0.8,
                                min_val=0.0, max_val=1.0)

# Staleness detection
STALLED_MINUTES = _int("COORDINATOR_STALLED_MINUTES", 30, min_val=1)

# Work expiry
WORK_EXPIRY_HOURS = _int("COORDINATOR_WORK_EXPIRY_HOURS", 2, min_val=1)
STUCK_TASK_HOURS = _int("COORDINATOR_STUCK_TASK_HOURS", 4, min_val=1)

# Approval requests: remind, then deny (never approve) when unanswered.
# The timeout must be at least the reminder delay; otherwise the reminder
# would never be sent, so a smaller value is raised to match.
APPROVAL_REMINDER_HOURS = _int("COORDINATOR_APPROVAL_REMINDER_HOURS", 3, min_val=1)
APPROVAL_TIMEOUT_HOURS = max(
    APPROVAL_REMINDER_HOURS,
    _int("COORDINATOR_APPROVAL_TIMEOUT_HOURS", 4, min_val=1),
)

# Automatic rebuilds: how many the coordinator may queue for one lineage (one
# root ancestor) in a rolling week. 0 switches automatic rebuilds off. See
# Actuators.queue_rebuild.
MAX_REBUILDS_PER_LINEAGE = _int("COORDINATOR_MAX_REBUILDS_PER_LINEAGE", 3, min_val=0)
# ... and how many for one parent build in 24 hours.
MAX_REBUILDS_PER_BUILD_24H = _int("COORDINATOR_MAX_REBUILDS_PER_BUILD", 2, min_val=0)

# The failure circuit (coordinator/tick.py:_failure_circuit). After this many
# hours without a live attempt, one task of an (agent, category) pair whose
# circuit is open is let through as a probe.
FAILURE_RETRY_HOURS = _float("COORDINATOR_FAILURE_RETRY_HOURS", 6.0, min_val=0.25)

# Initiative work: one agent at a time on an idle tick, and each agent at most
# once in this many days (coordinator/work_detection.py:_check_agent_initiative).
INITIATIVE_INTERVAL_DAYS = _int("COORDINATOR_INITIATIVE_INTERVAL_DAYS", 7, min_val=1)

# How often the reconciliation sweep may be filed, in hours.
RECONCILIATION_INTERVAL_H = _int("COORDINATOR_RECONCILIATION_INTERVAL_H", 2, min_val=1)


# Bounds on one model call made by a coordinator module (the support-answer
# draft, the failure-KB sync, failure triage). The Anthropic SDK's defaults are
# ten minutes per request and two retries; a tick dispatches agents under
# deadlines of 15 to 30 minutes and should not spend one on a single call.
# The timeout is the SDK's per-phase one (connect, write, read, pool), not a
# deadline for the call; the answer's size is bounded by each call's max_tokens.
MODEL_CALL_TIMEOUT_S = 60.0
MODEL_CALL_MAX_RETRIES = 1


def daily_budget_usd() -> float:
    """Daily coordinator spend budget in USD (COORDINATOR_DAILY_BUDGET, default 20).

    Read on every call (not at import) so a changed environment takes effect on
    the next tick. A malformed or non-finite value falls back to the default; a
    negative one is clamped to 0, which stops all discretionary spend.
    """
    return _float("COORDINATOR_DAILY_BUDGET", 20.0, min_val=0.0)


def tick_interval_s() -> int:
    """Seconds between coordinator ticks (COORDINATOR_INTERVAL_S, default 1800, minimum 1)."""
    return _int("COORDINATOR_INTERVAL_S", 1800, min_val=1)
