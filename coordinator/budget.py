# coordinator/budget.py: Tick budget computation
#
# Dynamic per-tick budget that yields to active customer builds.
# Customer builds always have priority over coordinator agent work.
#
# What the budget is, in one sentence: COORDINATOR_DAILY_BUDGET minus today's
# spend as read from ONE configured source (the Anthropic cost report when
# ANTHROPIC_ADMIN_KEY is set, otherwise the worker's own ledger of build and
# agent-task costs, which does not include what the web tier spends on chat),
# spread over the ticks left in the day; when that source cannot be read the
# tick gets no budget, and a tick with no budget makes no model call.
#
# The budget is spent in two places, both in coordinator/tick.py:
#   - tasks: each dispatched task is charged its estimated cost and one call
#     (tick._task_charge), which caps how many tasks a tick starts;
#   - model calls: every call a coordinator module makes to a model is first
#     granted by the tick's ModelCallMeter (through
#     Actuators.authorize_model_call), which caps how many calls a tick makes
#     and their estimated cost, whichever task or step makes them.

import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from coordinator import config
from coordinator.scorer import ESTIMATED_COST, estimate_task_cost
from coordinator.state import WorldState, customer_builds_active

logger = logging.getLogger("coordinator.budget")

CHEAP_MODEL = "claude-haiku-4-5"
OPUS_MODEL = "claude-opus-4-6"


@dataclass
class TickBudget:
    max_api_calls: int = 0
    max_cost_usd: float = 0.0
    max_opus_calls: int = 0
    allowed_models: list = field(default_factory=list)


# Seconds to sleep between sequential agent API calls (RPM pacing)
RPM_SPACING = {
    "claude-opus-4-6":   2.0,
    "claude-sonnet-4-6": 1.0,
    "claude-haiku-4-5":  0.5,
}


class ModelCallMeter:
    """One tick's allowance for model calls, counted where each call is granted.

    run_tick builds one meter from the tick's TickBudget and hands it to every
    Actuators instance that may authorize a model call in that tick: failure
    triage in work detection and each dispatched agent. A call is granted only
    while the tick has a call left, the model is one the tick allows, and the
    dollars left cover the call's estimated cost (scorer.ESTIMATED_COST; no
    caller knows its token count before it calls). A budget of zero therefore
    grants nothing, and so does the absence of a meter: Actuators refuses a
    model call when it was given none.

    The meter counts calls, not tokens: it bounds how many calls a tick makes
    and what they cost at the estimate, it does not measure the bill. Parallel
    agents share one meter, so it is locked.
    """

    def __init__(self, budget: TickBudget):
        self._lock = threading.Lock()
        self._calls_left = max(0, int(budget.max_api_calls))
        # Dollars are kept as whole millionths so that repeated subtraction
        # stays exact: three $0.05 calls fit in $0.15.
        self._micros_left = max(0, self._micros(budget.max_cost_usd))
        self._opus_left = max(0, int(budget.max_opus_calls))
        self._allowed = frozenset(budget.allowed_models)
        self._calls_granted = 0
        self._micros_granted = 0

    @staticmethod
    def _micros(usd: float) -> int:
        return int(round(float(usd) * 1_000_000))

    def grant(self, model: str) -> bool:
        """Reserve one call to `model`; False when the tick cannot afford it."""
        cost = self._micros(estimate_task_cost("", model))
        with self._lock:
            if model not in self._allowed or self._calls_left < 1 or cost > self._micros_left:
                return False
            if model == OPUS_MODEL:
                if self._opus_left < 1:
                    return False
                self._opus_left -= 1
            self._calls_left -= 1
            self._micros_left -= cost
            self._calls_granted += 1
            self._micros_granted += cost
            return True

    def granted(self) -> tuple:
        """(calls, estimated dollars) granted so far in this tick."""
        with self._lock:
            return self._calls_granted, self._micros_granted / 1_000_000


def _whole_calls(usd: float, per_call_usd: float) -> int:
    """How many calls at `per_call_usd` fit in `usd`, counted in millionths of
    a dollar so that $0.20 (which may arrive as 0.19999999999999996) buys four
    $0.05 calls and not three."""
    per_call = int(round(per_call_usd * 1_000_000))
    return max(0, int(round(usd * 1_000_000)) // per_call) if per_call > 0 else 0


def _remaining_today(state: WorldState, conn, daily_budget: float) -> float:
    """Dollars left in today's budget; 0.0 when today's spend cannot be established.

    The spend figure comes from one configured source, read here, and a failure
    of that source is never papered over with another number:

      - ANTHROPIC_ADMIN_KEY set: the Anthropic cost report, which covers the
        whole account (worker, coordinator and web tier). If the report cannot
        be read, spend is unknown. The worker's ledger is not substituted for
        it: the ledger leaves out everything the web tier spends, so it would
        report less than was spent.
      - ANTHROPIC_ADMIN_KEY unset: the worker's ledger
        (spend_tracker.get_daily_spend). If that query fails, spend is unknown.
        The figure build_world_state read from the same tables earlier in the
        tick is not used in its place: it is older, and a ledger that cannot be
        read now is not a ledger to spend against.

    Without a connection (conn is None) nothing can be read and the answer is
    0.0 for either source. The same holds when build_world_state could not
    read the ledger at the start of this tick (state.spend_known is False).
    """
    if conn is None:
        logger.warning("No database connection for the spend lookup; "
                       "treating the daily budget as exhausted")
        return 0.0
    if not getattr(state, "spend_known", True):
        logger.warning("Today's spend is unknown; treating the daily budget as exhausted")
        return 0.0

    from coordinator import spend_tracker
    try:
        spend = spend_tracker.get_comprehensive_spend(conn)
    except Exception as exc:
        try:
            conn.rollback()  # a failed query leaves the transaction aborted
        except Exception as rollback_exc:
            logger.warning("rollback after the failed spend lookup did not succeed: %s", rollback_exc)
        logger.warning("Spend lookup failed (%s); treating the daily budget as exhausted", exc)
        return 0.0

    if spend.get("provider_failed"):
        logger.warning("ANTHROPIC_ADMIN_KEY is set but the cost report could not be read; "
                       "treating the daily budget as exhausted")
        return 0.0
    spent = spend.get("total_today_usd")
    if isinstance(spent, bool) or not isinstance(spent, (int, float)) \
            or not math.isfinite(spent) or spent < 0:
        logger.warning("The spend source returned %r for today; treating the daily budget as exhausted",
                       spent)
        return 0.0
    if spend.get("source") == "anthropic_api":
        logger.debug("Budget using Anthropic API spend: $%.2f today", spent)
    return max(0.0, daily_budget - spent)


def compute_tick_budget(state: WorldState, conn) -> TickBudget:
    """Compute the API budget for this tick.

    conn is the tick's database connection: the spend figure is read through
    it, and without one (None) the budget is zero.

    Rule 1: Customer builds always win. If a customer build is in progress,
    or the pipeline counts could not be read this tick (unknown is not
    "none": coordinator.state.customer_builds_active), the coordinator limits
    itself to 1 Haiku call.

    Rule 2: Per-tick budget is dynamic based on remaining daily budget
    and time left in the day; no single tick gets more than 25% of the day.

    Rule 3: Unknown spend means no budget (see _remaining_today).

    Rule 4: No budget means no calls. The call counts are whole calls the
    dollars can pay for at the per-call estimates, so a per-tick figure below
    the cheapest call's estimate yields max_api_calls == 0.

    The allowance returned here is spent in run_tick (see the module header):
    each dispatched task is charged its estimated cost and one call unless the
    agent reports its own figures (coordinator.tick._task_charge), and each
    model call is granted by a ModelCallMeter built from it.
    """
    daily_budget = config.daily_budget_usd()
    remaining = _remaining_today(state, conn, daily_budget)

    now = datetime.now(timezone.utc)
    interval_s = config.tick_interval_s()
    seconds_until_midnight = ((24 - now.hour) * 3600) - (now.minute * 60) - now.second
    ticks_left = max(1, seconds_until_midnight // interval_s)
    per_tick = remaining / ticks_left
    per_tick = min(per_tick, daily_budget * 0.25)  # No single tick > 25% of daily budget

    haiku_cost = ESTIMATED_COST[CHEAP_MODEL]
    opus_cost = ESTIMATED_COST[OPUS_MODEL]

    # Rule 1: yield to customer builds, and to not knowing whether there are any
    if customer_builds_active(state):
        capped = min(per_tick, 0.10)
        return TickBudget(
            max_api_calls=min(1, _whole_calls(capped, haiku_cost)),
            max_cost_usd=capped,
            max_opus_calls=0,
            allowed_models=[CHEAP_MODEL],
        )

    # Normal operation
    return TickBudget(
        max_api_calls=_whole_calls(per_tick, haiku_cost),
        max_cost_usd=per_tick,
        max_opus_calls=_whole_calls(per_tick, opus_cost),
        allowed_models=[CHEAP_MODEL, "claude-sonnet-4-6", OPUS_MODEL],
    )
