# coordinator/spend_tracker.py: Unified spend tracking
#
# Two sources of "what was spent today":
#   - the worker's own ledger: builds.total_api_cost_usd plus
#     agent_tasks.actual_cost_usd. It does not include anything the web tier
#     spends (intake, support and companion chat), so on its own it under-reports
#     the account's spend;
#   - the Anthropic Admin API cost report (real billing data for the whole
#     account), read only when ANTHROPIC_ADMIN_KEY is set.
# Used by budget.py for pacing and by cost_analyst for reports.
#
# When the key is set but the report cannot be read, get_comprehensive_spend
# still returns the ledger figure, for reports, and says so with
# provider_failed=True. budget.py treats that case as unknown spend and gives
# the tick no budget; it never budgets against the smaller ledger figure.
#
# "Cannot be read" includes an answer that arrives with status 200 and does
# not establish today's figure: no `data` list, no bucket for today, a page
# that says there is more without saying where, an amount that is not a
# finite non-negative number, a currency other than USD. Each of those is an
# unknown spend, never a spend of zero.

from __future__ import annotations

import json
import logging
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

from monitor.bounded_fetch import bounded_urlopen

logger = logging.getLogger("coordinator.spend_tracker")

# Cache Anthropic API spend for 10 minutes (avoid hammering the endpoint)
_anthropic_cache = {"value": None, "expires": None}
_CACHE_TTL_S = 600


def get_daily_spend(conn) -> dict:
    """Get today's tracked spend from internal DB sources (the worker's ledger).

    This is the one definition of the ledger: the budget reads it through
    get_comprehensive_spend, and build_world_state reads it for the figure
    shown in the tick record and used for scoring and alerts.

    "Today" for a build means the row was written today. builds.total_api_cost_usd
    is a running total with no date of its own, and builds.updated_at is set by
    a trigger on every update, so a build whose cost changed today is counted
    with its whole total (over-counting a build that spans midnight, the safe
    direction for a budget). builds.started_at is not used: every requeue sets
    it back to NULL, which would drop a retrying build's cost from the day.
    "Today" for an agent task means it finished today.

    Raises if a query fails; the caller decides what unknown spend means.

    Returns:
        {
            "build_pipeline_usd": float,  # from builds.total_api_cost_usd
            "agent_tasks_usd": float,     # from agent_tasks.actual_cost_usd
            "total_tracked_usd": float,   # sum of above
            "builds_today": int,          # builds written today
            "agent_tasks_today": int,     # agent tasks finished today
        }
    """
    cur = conn.cursor()
    cur.execute(LEDGER_BUILDS_SQL)
    build_row = cur.fetchone()
    cur.execute(LEDGER_AGENT_TASKS_SQL)
    agent_row = cur.fetchone()
    cur.close()
    return ledger_figures(build_row, agent_row)


# The two statements that ARE the ledger. Each returns one row: (usd, n).
# get_daily_spend runs them on the tick's connection; the cost analyst's daily
# report runs the same two through Actuators.query_readonly, so the report and
# the budget cannot disagree about what "spent today" means.
LEDGER_BUILDS_SQL = (
    "SELECT COALESCE(SUM(total_api_cost_usd), 0) AS usd, COUNT(*) AS n "
    "FROM builds WHERE updated_at >= CURRENT_DATE"
)
LEDGER_AGENT_TASKS_SQL = (
    "SELECT COALESCE(SUM(actual_cost_usd), 0) AS usd, COUNT(*) AS n "
    "FROM agent_tasks WHERE completed_at >= CURRENT_DATE "
    "AND status IN ('completed', 'failed')"
)


def ledger_figures(build_row, agent_row) -> dict:
    """The ledger dict (see get_daily_spend) from the two statements' rows.

    A row is a (dollars, count) pair. A statement that returned no row at all
    did not answer, and that is an error here, not a spend of zero.
    """
    if not build_row or not agent_row:
        raise RuntimeError("the spend ledger query returned no row")
    build_cost, agent_cost = float(build_row[0]), float(agent_row[0])
    return {
        "build_pipeline_usd": round(build_cost, 4),
        "agent_tasks_usd": round(agent_cost, 4),
        "total_tracked_usd": round(build_cost + agent_cost, 4),
        "builds_today": build_row[1],
        "agent_tasks_today": agent_row[1],
    }


_COST_REPORT_URL = "https://api.anthropic.com/v1/organizations/cost_report"
# The cost report allows at most 31 daily buckets per page; one month fits in one
# page, and the page cap is a backstop against a runaway cursor.
_COST_REPORT_PAGE_LIMIT = 31
_COST_REPORT_MAX_PAGES = 5
# Deadline of one page request, whole exchange.
_COST_REPORT_DEADLINE_S = 10


def _fetch_cost_buckets(admin_key: str, starting_at: str, ending_at: str) -> list:
    """Fetch every daily cost bucket in [starting_at, ending_at), following pages.

    Response shape (same one web/src/services/anthropic-costs.ts reads):
        {"data": [{"starting_at": "...", "ending_at": "...",
                   "results": [{"currency": "USD", "amount": "123.45", ...}]}],
         "has_more": bool, "next_page": str | null}
    `amount` is a decimal STRING in the currency's lowest unit (cents for USD).

    Raises on any HTTP/parse error, on an answer that is not this shape (no
    `data` list, a bucket that is not an object) and if pagination does not
    finish, which includes a page that says `has_more` and names no
    `next_page`: a partial month would silently under-report spend, which is
    the unsafe direction.

    Each page is one bounded exchange (monitor.bounded_fetch.bounded_urlopen):
    _COST_REPORT_DEADLINE_S for the whole request and at most a few megabytes
    of answer, so at most _COST_REPORT_MAX_PAGES times that in all.
    """
    buckets: list = []
    page = None
    for _ in range(_COST_REPORT_MAX_PAGES):
        params = {
            "starting_at": starting_at,
            "ending_at": ending_at,
            "bucket_width": "1d",
            "limit": str(_COST_REPORT_PAGE_LIMIT),
        }
        if page:
            params["page"] = page
        url = f"{_COST_REPORT_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, method="GET", headers={
            "anthropic-version": "2023-06-01",
            "x-api-key": admin_key,
        })
        with bounded_urlopen(req, timeout=_COST_REPORT_DEADLINE_S) as resp:
            data = json.loads(resp.read())
        page_buckets = data.get("data") if isinstance(data, dict) else None
        if not isinstance(page_buckets, list) or not all(isinstance(b, dict) for b in page_buckets):
            raise RuntimeError("cost_report answer has no list of buckets under 'data'")
        buckets.extend(page_buckets)
        if not data.get("has_more"):
            return buckets
        page = data.get("next_page")
        if not page:
            raise RuntimeError("cost_report said has_more without a next_page; refusing a partial total")
    raise RuntimeError("cost_report pagination did not finish; refusing a partial total")


def _bucket_usd(bucket: dict) -> float:
    """Sum one bucket's results, converting cents to dollars exactly once.

    Raises ValueError for a result that cannot be added to a dollar total: a
    currency other than USD (skipping it would report less than was spent)
    or an amount that is not a finite, non-negative number.
    """
    cents = 0.0
    for result in bucket.get("results") or []:
        if not isinstance(result, dict):
            raise ValueError("cost_report result is not an object")
        currency = result.get("currency", "USD")
        if currency != "USD":
            raise ValueError(f"cost_report result in {str(currency)[:8]!r}, not USD")
        amount = float(result.get("amount") or 0)
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("cost_report amount is not a finite, non-negative number")
        cents += amount
    return cents / 100.0


def get_anthropic_spend() -> dict | None:
    """Query Anthropic Admin API for actual billing data.

    Requires ANTHROPIC_ADMIN_KEY env var (an Anthropic Admin API key).
    Returns None if the key is not set or the lookup fails for any reason
    (HTTP error, bad payload, incomplete pagination, or an answer with no
    bucket for today: the request asks for every day from the first of the
    month through today, so an answer without today's bucket has not said
    what was spent today). None is never read as "zero spend":
    get_comprehensive_spend reports the ledger figure instead and, when the
    key was set, flags the failure (provider_failed).

    Returns:
        {
            "today_usd": float,
            "month_to_date_usd": float,
            "source": "anthropic_admin_api",
        }
    """
    admin_key = os.environ.get("ANTHROPIC_ADMIN_KEY", "")
    if not admin_key:
        return None

    now = datetime.now(timezone.utc)

    # Check cache
    if _anthropic_cache["value"] and _anthropic_cache["expires"] and now < _anthropic_cache["expires"]:
        return _anthropic_cache["value"]

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today_start + timedelta(days=1)

    try:
        buckets = _fetch_cost_buckets(
            admin_key,
            month_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            tomorrow.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        today_str = today_start.strftime("%Y-%m-%d")
        todays = [b for b in buckets if str(b.get("starting_at", "")).startswith(today_str)]
        if not todays:
            raise RuntimeError("cost_report answer has no bucket for today; today's spend is unknown")
        mtd_usd = sum(_bucket_usd(b) for b in buckets)
        today_usd = sum(_bucket_usd(b) for b in todays)
        result = {
            "today_usd": round(today_usd, 2),
            "month_to_date_usd": round(mtd_usd, 2),
            "source": "anthropic_admin_api",
        }

        # Cache
        _anthropic_cache["value"] = result
        _anthropic_cache["expires"] = now + timedelta(seconds=_CACHE_TTL_S)

        logger.info("Anthropic spend: $%.2f today, $%.2f MTD",
                     result["today_usd"], result["month_to_date_usd"])
        return result

    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode(errors="replace")[:200]
        except (OSError, AttributeError) as read_error:
            body = f"(error body unreadable: {type(read_error).__name__})"
        if exc.code == 401:
            logger.warning("ANTHROPIC_ADMIN_KEY is invalid or lacks permissions - %s", body)
        else:
            logger.warning("Anthropic cost API error: %s %s - %s", exc.code, exc.reason, body)
        return None
    except Exception as exc:
        logger.warning("Anthropic cost API failed: %s", exc)
        return None


def provider_spend_configured() -> bool:
    """True when ANTHROPIC_ADMIN_KEY is set: the operator has made the Anthropic
    cost report the spend figure the budget runs on."""
    return bool(os.environ.get("ANTHROPIC_ADMIN_KEY", ""))


def get_comprehensive_spend(conn) -> dict:
    """Get unified spend report combining all sources.

    Raises if the ledger query fails.

    Returns:
        {
            "tracked": { build_pipeline_usd, agent_tasks_usd, total_tracked_usd, ... },
            "anthropic": { today_usd, month_to_date_usd } or None,
            "total_today_usd": float,  # the cost report's figure, else the ledger's
            "source": "anthropic_api" | "internal_tracking",
            "provider_failed": bool,   # key set, report unreadable: total_today_usd
                                       # is then the ledger only and under-reports
        }
    """
    tracked = get_daily_spend(conn)
    anthropic = get_anthropic_spend()

    if anthropic:
        total = anthropic["today_usd"]
        source = "anthropic_api"
    else:
        total = tracked["total_tracked_usd"]
        source = "internal_tracking"

    return {
        "tracked": tracked,
        "anthropic": anthropic,
        "total_today_usd": total,
        "source": source,
        "provider_failed": anthropic is None and provider_spend_configured(),
    }
