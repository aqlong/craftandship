# queue/worker_pg.py
# PostgreSQL-based build worker for Railway deployment.
#
# Reads DATABASE_URL from the environment (not from web/.env) and polls the
# builds table for queued jobs. Claims jobs atomically with SELECT FOR UPDATE
# SKIP LOCKED to prevent races when multiple workers are running.

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# The modules in queue/ and cli/ are imported as top-level modules ("import
# deploy", "from mailer import ..."), so both directories go on sys.path.
# queue/ has no __init__.py and is never imported as a package: a regular
# package named "queue" at the repository root would shadow the standard
# library's queue module, which concurrent.futures and urllib3 import. The
# repository root is added for the real packages (compose, monitor, ops,
# coordinator).
_QUEUE_DIR = str(Path(__file__).parent)
_CLI_DIR = str(Path(__file__).parent.parent / "cli")
_ROOT_DIR = str(Path(__file__).parent.parent)
if _QUEUE_DIR not in sys.path:
    sys.path.insert(0, _QUEUE_DIR)
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

import sentry_sdk

from coordinator.config import env_float, env_int
from coordinator.envflags import arming_switch, safety_switch
from monitor.url_redaction import redact_url_credentials
from ops.scanner import is_deterministic_failure


_EMAIL_IN_TEXT_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _sentry_text(value):
    """Text bound for Sentry: credentials redacted, email addresses masked."""
    if not isinstance(value, str):
        return value
    return _EMAIL_IN_TEXT_RE.sub("<email>", _redact_secrets(value))


def _sentry_scrub_event(event, hint=None):
    """Sentry before_send hook: scrub text that leaves for a third party.

    Exception values, log messages and breadcrumbs can quote Railway responses,
    the customer app's own output, database URLs, probe URLs that carry a
    login token in the query string, and customer email addresses (the worker
    logs them). All of that text gets the _redact_secrets pass used for
    build_logs plus an email mask. Stack-frame local variables are not sent at
    all (see _sentry_init_options).
    """
    try:
        for entry in ((event.get("exception") or {}).get("values") or []):
            if isinstance(entry, dict) and entry.get("value"):
                entry["value"] = _sentry_text(str(entry["value"]))
        if isinstance(event.get("message"), str):
            event["message"] = _sentry_text(event["message"])
        logentry = event.get("logentry")
        if isinstance(logentry, dict):
            for key in ("message", "formatted"):
                if isinstance(logentry.get(key), str):
                    logentry[key] = _sentry_text(logentry[key])
            if logentry.get("params"):
                logentry["params"] = [_sentry_text(str(param)) for param in logentry["params"]]
        crumbs = event.get("breadcrumbs")
        crumb_list = crumbs.get("values") if isinstance(crumbs, dict) else crumbs
        for crumb in (crumb_list or []):
            if not isinstance(crumb, dict):
                continue
            if isinstance(crumb.get("message"), str):
                crumb["message"] = _sentry_text(crumb["message"])
            data = crumb.get("data")
            if isinstance(data, dict):
                for key, value in list(data.items()):
                    data[key] = _sentry_text(value)
    except Exception:
        # A scrubber that cannot read the event must not let it through as is.
        return None
    return event


def _sentry_init_options() -> dict:
    """Options for sentry_sdk.init (a function so tests can assert on them).

    include_local_variables=False is the important one. The SDK default sends
    the local variables of every stack frame, and the frames of a failed
    deploy hold the Railway project token, the generated DATABASE_URL and
    SESSION_SECRET of the customer app, magic-link tokens and the job (spec
    and customer email). The SDK's own scrubber only hides variables whose
    exact name is on a short list ("token", "password", ...), which misses
    names like project_token, database_url and env_vars.
    """
    return {
        "dsn": os.environ.get("SENTRY_DSN"),
        "environment": os.environ.get("RAILWAY_ENVIRONMENT", "development"),
        "traces_sample_rate": 0.2,
        "send_default_pii": False,
        "include_local_variables": False,
        "before_send": _sentry_scrub_event,
    }


if os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(**_sentry_init_options())

from mailer import (  # queue/mailer.py
    send_build_failed,
    send_build_progress,
    send_preview_ready,
    send_review_pending,
)

# cli.cancellation is loaded from its file path instead of with
# `from cli import cancellation`. That statement takes the first `cli` it finds
# on sys.path, and the order of sys.path is not the same everywhere: this
# module, the test files and pytest each insert directories at the front. A
# directory that holds a module named cli.py (monitor/cli.py is one) and sits
# ahead of the repository root would be found first, and it has no
# `cancellation` attribute. Loading the package from its location on disk
# gives the same module however sys.path is ordered.
def _load_cli_cancellation():
    import importlib.util
    from pathlib import Path as _Path
    _repo_root = _Path(__file__).resolve().parent.parent
    _cli_pkg_init = _repo_root / "cli" / "__init__.py"
    _cancel_file = _repo_root / "cli" / "cancellation.py"
    if "cli" not in sys.modules:
        pkg_spec = importlib.util.spec_from_file_location(
            "cli",
            str(_cli_pkg_init),
            submodule_search_locations=[str(_cli_pkg_init.parent)],
        )
        pkg_mod = importlib.util.module_from_spec(pkg_spec)
        sys.modules["cli"] = pkg_mod
        pkg_spec.loader.exec_module(pkg_mod)
    sub_spec = importlib.util.spec_from_file_location(
        "cli.cancellation", str(_cancel_file)
    )
    sub_mod = importlib.util.module_from_spec(sub_spec)
    sys.modules["cli.cancellation"] = sub_mod
    sub_spec.loader.exec_module(sub_mod)
    return sub_mod

cli_cancellation = _load_cli_cancellation()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("worker_pg")

class QualityGateError(RuntimeError):
    """Base: blocking quality gate failed after all fix attempts.

    Subclasses carry gate-specific retry policy. Existing
    ``except QualityGateError`` sites continue to catch both subclasses.
    """
    pass


class DesignQualityGateError(QualityGateError):
    """Design review critical issues remained after design_fix iterations.

    Retryable ONCE with the critical issue list injected into
    ``builds.rebuild_request`` so the next build prompt treats them as
    HIGHEST PRIORITY. See _inject_design_issues_as_rebuild_request.
    """
    pass


class QAQualityGateError(QualityGateError):
    """QA failures remained after qa_fix iterations.

    Non-retryable: structural test failures (missing CSRF, auth bypass,
    etc.) do not self-heal via stochastic re-generation.
    """
    pass

_POLL_INTERVAL = 10  # seconds to sleep when queue is empty

# Status contract. A build is owned by a worker thread only while it is
# 'building', 'testing' or 'deploying', and every status write the worker makes
# is an UPDATE guarded twice:
#   * By status. _set_status moves a build forward through those three stages,
#     never back; _set_preview_status, _requeue_for_retry, _fail_build and the
#     supervisor act only on a build that is still in one of them (never on
#     'queued'). A late or duplicate write therefore cannot move a build that
#     has since been requeued, previewed, failed or published.
#   * By attempt. The claim sets builds.started_at and hands that value to the
#     build thread as job["attempt"]; every requeue (the worker's own, and the
#     web app's retry, reset and requeue routes) clears started_at and the
#     next claim sets a new one. Each of the writes above also requires
#     started_at to equal the writer's attempt, so a thread left over from an
#     earlier attempt of the same build (for example one the supervisor gave
#     up on) cannot advance, preview, requeue or fail a later attempt. Its next
#     stage write matches no row and stops it (BuildNoLongerOwnedError).
#     Orphan recovery guards its own writes with the started_at it read.
# The attempt guard covers the status writes, and the Railway ids, which are
# written by the preview write itself (see _set_preview_status). What a build
# thread stores besides (build_logs rows, screenshots, the stored artifact,
# cost) is keyed by build id, so a leftover thread can still write those until
# it reaches its next stage write.
# The worker never writes 'live': that transition belongs to the web app, after
# the review gate.
#
# The attempt predicate in each statement is
#     started_at IS NOT DISTINCT FROM %s::timestamp
# and the token is required: every function that makes such a write raises
# ValueError when it is called with attempt=None, so a caller that lost its
# token cannot fall back to the status guard alone. The one caller that can
# meet a row with no started_at (orphan recovery, for a row something other
# than the claim put into a pipeline status) says so explicitly with
# ROW_WITHOUT_STARTED_AT, and its write then matches only a row whose
# started_at is still NULL.


class _RowWithoutStartedAt:
    """Type of ROW_WITHOUT_STARTED_AT (a named singleton, so that None can
    never be mistaken for it)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ROW_WITHOUT_STARTED_AT"


# Attempt token for a build row whose started_at was NULL when it was read.
ROW_WITHOUT_STARTED_AT = _RowWithoutStartedAt()


def _attempt_param(attempt):
    """The value a guarded status write compares builds.started_at with.

    Raises ValueError for None: an attempt token is not optional. Returns None
    (SQL NULL, matched with IS NOT DISTINCT FROM) only for
    ROW_WITHOUT_STARTED_AT.
    """
    if attempt is None:
        raise ValueError(
            "attempt token missing: a guarded status write needs the started_at of the "
            "attempt that makes it (or ROW_WITHOUT_STARTED_AT)"
        )
    if attempt is ROW_WITHOUT_STARTED_AT:
        return None
    return attempt


class BuildNoLongerOwnedError(Exception):
    """A guarded status write inside the pipeline matched no row.

    The build left the worker's hands while its pipeline was running: an admin
    killed it, or another writer failed, requeued or previewed it. The pipeline
    stops at once (in particular before it deploys anything) and _process_job
    drops the job without a retry, a failure record or an email.
    """


def _block_gate_on(env_name: str) -> bool:
    """Return True when the BLOCK_ON_* gate named ``env_name`` should fire
    (block the build).

    Each gate is a safety switch (coordinator.envflags.safety_switch): strict
    by default, and rolled back only by an explicit OFF word (false, 0, no,
    off, disabled). Unset, blank, a typo, "True" or "1" all keep the gate
    strict, which is the right way to fall for gates whose purpose is to stop
    broken code from being deployed silently.
    """
    return safety_switch(env_name)


def _blocking_design_review() -> bool:
    """BLOCKING_DESIGN_REVIEW (v1): off unless set. Blocking is the strict
    state, so any value that is set and is not an explicit OFF word blocks."""
    return safety_switch("BLOCKING_DESIGN_REVIEW", default=False)


def _blocking_qa() -> bool:
    """BLOCKING_QA (v1): off unless set, read like BLOCKING_DESIGN_REVIEW."""
    return safety_switch("BLOCKING_QA", default=False)

# Tier-based build timeouts (minutes). Larger tiers get more time. Multi-pass
# v1 builds (7+ model calls) need the headroom; the ladder is kept linear.
_TIER_TIMEOUTS = {"build": 60, "grow": 90, "scale": 120}

# Supervisor grace period (seconds), added to the tier timeout before the outer
# supervisor force-fails a stuck build. The outer ThreadPoolExecutor +
# future.result(timeout=tier_deadline + grace) is the SOLE timeout mechanism.
# Nothing interrupts a thread blocked in a C-level syscall (subprocess.wait,
# socket recv, SSL_read): the supervisor decides the build's fate without it
# and leaves the thread to end on its own. What cooperative cancellation
# (cli.cancellation) adds is that the v1 fix loops and model calls of the
# abandoned attempt stop at their next checkpoint instead of working on. An
# async-exception watchdog was removed because it cannot interrupt a C-level
# call either and can deadlock if the target thread holds a lock.
_SUPERVISOR_GRACE_SECONDS = 30

# Zombie-thread reality check. When the supervisor fires, the build thread may
# have been blocked in a C-level syscall while the Railway deploy actually
# completed. Before writing `status=failed` the supervisor probes Railway
# (queue/watchdog_probe.py) for a live deployment matching app_name in the
# build's time window; if its /health answers healthy (the definition a fresh
# deploy has to meet, deploy.health_response_ok) the build goes to preview and
# into review instead (see _set_preview_status); it is never published from
# here. The probe deadline lives in watchdog_probe.PROBE_DEADLINE_S.
_SUPERVISOR_REALITY_PROBE_DEADLINE_S = 20

# Max concurrent builds (across all workers). Default 1 to space out API usage.
# Enforced in _claim_next_job under a database advisory lock. A value that is
# not a whole number of at least 1 means 1 (env_int logs it): a mistyped
# setting must not stop the worker at import.
_MAX_CONCURRENT = env_int("MAX_CONCURRENT_BUILDS", 1, min_val=1)

# Daily API spend cap (USD). No build is claimed once the figure returned by
# _get_daily_api_spend reaches this. Set to 0 to disable. What it bounds, and
# what it does not:
#   * It counts model spend the build pipeline recorded on build rows. Only the
#     v1 engine calls a model while building; under v2 (the default) the figure
#     is 0 and the cap never triggers.
#   * It is checked at claim time only, and spend is written when an attempt
#     ends, so attempts in flight finish and the cap can be overshot by up to
#     MAX_CONCURRENT_BUILDS attempts.
#   * It is a sum over build rows, not a ledger of calls. A build's recorded
#     cost is cumulative, so a build retried today that also spent on an
#     earlier day counts in full; an attempt that began before midnight and
#     ended after it is counted on neither day; and the cost of a failed build
#     leaves the sum when the janitor deletes its row (_cleanup_old_builds).
# A value that is not a finite number means the default of 50 (a typo must not
# switch the cap off); a negative one is read as 0, which disables it.
_DAILY_SPEND_CAP = env_float("DAILY_API_SPEND_CAP", 50.0, min_val=0.0)

# Transient error patterns that trigger auto-retry instead of permanent failure.
# These cover network issues, API rate limits, and infrastructure/environment
# problems that are not caused by the generated app code itself.
#
# Plain substrings, matched in the lower-cased message. The HTTP status codes
# are not in this list: a number is matched as a whole token (see
# _TRANSIENT_STATUS_RE), because "429" is also a piece of a build id.
_TRANSIENT_PATTERNS = [
    # Network / connectivity
    "connection refused",
    "connection reset",
    "timeout",
    "timed out",          # "Deployment timed out after 300s", a socket read, a subprocess
    "deadline exceeded",  # monitor/bounded_fetch.py: the whole exchange ran past its deadline
    "ran out of time",    # cli/deploy.py: a Railway API call used up its deadline
    "network is unreachable",
    "temporary failure",
    "dns resolution",
    # API rate limits and server errors
    "rate limit",
    "overloaded",
    # Infrastructure / environment issues
    "executable doesn't exist",
    "no such file or directory",
    "command not found",
    "npm err",
    "enospc",           # disk full
    "enomem",           # out of memory
    "killed",           # OOM killer
    "signal 9",         # SIGKILL
    # Database setup failures (transient in container context)
    "could not connect to server",
    "the database system is starting up",
    "too many connections",
]

# HTTP statuses that mean "try again" (429 Too Many Requests, 502 Bad Gateway,
# 503 Service Unavailable) and errno 2, matched as whole tokens so that a build
# id such as 3f429a1c, a version such as 2.429.1, a duration such as 1.503 or
# errno 22 does not read as one. A bare number elsewhere in a message ("line
# 429") still reads as a status code.
_TRANSIENT_STATUS_RE = re.compile(
    r"(?<![0-9a-z_\-])(?<![0-9]\.)(?:429|502|503)(?![0-9a-z_\-]|\.[0-9])"
    r"|errno 2(?![0-9])"
)

_MAX_RETRIES = 6

# v1 fix-loop wall-clock budgets (seconds). Each fix loop in _run_pipeline
# tracks elapsed time via time.monotonic() and raises FixLoopBudgetExceeded as
# soon as the next iteration would exceed its budget. That raise propagates into
# _process_job's exception handler, is classified as a code error and retried
# (or dead-lettered after _MAX_RETRIES). The budgets stop a pathological loop
# before the outer supervisor deadline turns the whole build into a force-fail.
_FIX_LOOP_BUDGETS_S: dict[str, int] = {
    "fix": 30 * 60,          # test-phase self-correction (fix.py)
    "qa_fix": 20 * 60,       # QA self-correction (qa_fix.py)
    "design_fix": 20 * 60,   # design self-correction (design_fix.py)
    # Shorter than the others: a slow model call can eat most of the tier
    # timeout, so deploy_fix.py also caps each call (DEPLOY_FIX_PER_CALL_BUDGET_S).
    "deploy_fix": 15 * 60,   # post-deploy self-correction (deploy_fix.py)
}


class ReviewCriticalBlockError(Exception):
    """Raised by the v1 code-review stage when a critical finding matches a
    known deploy-blocking pattern and BLOCK_ON_CODE_REVIEW_CRITICAL is on
    (default true).

    Without this gate a schema/query column mismatch flagged as critical was
    only logged, the fix loops could not repair it, and the broken code was
    deployed. A clean failure at review time lets the normal retry path clear
    the artifact and regenerate, which is what fixes this class of drift.

    The rollback knob is BLOCK_ON_CODE_REVIEW_CRITICAL=false (any OFF word of
    coordinator.envflags), the same pattern as the other BLOCK_ON_* gates.
    """


# Regex patterns that, if found in a critical review issue's summary text, mean
# the issue is deploy-blocking. Kept narrow on purpose: the build should fail
# only on the class of issues fix loops cannot recover from at runtime, not on
# every critical finding. Example finding that matches: "queries 'SELECT key,
# value FROM site_settings' but the schema defines the columns as
# 'setting_key' and 'setting_value'".
_BLOCKING_REVIEW_PATTERNS = (
    r"column\s+mismatch",
    r"column\s+['\"]?\w+['\"]?\s+does\s+not\s+exist",
    r"schema\s+defines\s+the\s+columns",
    r"missing\s+route",
    r"route\s+not\s+found",
    r"route\s+not\s+defined",
    r"schema[\s/-]query\s+drift",
    r"query.*schema.*mismatch",
    r"skipped\s+seed",
    r"SKIPPED.*seed",
    r"broken\s+seed",
    r"seed\s+failure",
)


def _matching_blocking_critical_findings(critical_issues):
    """Filter a list of {'severity':'critical', ...} dicts to those whose
    issue text matches any of the known deploy-blocking patterns."""
    import re as _re_mod
    blockers = []
    for issue in critical_issues:
        text = " ".join(
            str(issue.get(k, "")) for k in ("issue", "message", "summary", "file")
        )
        for pat in _BLOCKING_REVIEW_PATTERNS:
            if _re_mod.search(pat, text, _re_mod.IGNORECASE):
                blockers.append(issue)
                break
    return blockers


class FixLoopBudgetExceeded(Exception):
    """Raised inside _run_pipeline when a fix loop exceeds its wall-clock budget.

    The supervisor/watchdog covers the whole-build deadline; this exception
    covers the "one fix loop pegged at 100%" failure mode; e.g. a deploy_fix
    loop that keeps producing nearly-passing patches and never converges.
    Raised from inside the loop, caught by _process_job's generic exception
    handler and routed through the normal code-error retry path.

    Attributes:
        loop_name: one of "fix" | "qa_fix" | "design_fix" | "deploy_fix".
        elapsed_s: wall-clock seconds spent in the loop so far.
        budget_s:  wall-clock budget for this loop from _FIX_LOOP_BUDGETS_S.
    """

    def __init__(self, loop_name: str, elapsed_s: float, budget_s: float) -> None:
        self.loop_name = loop_name
        self.elapsed_s = elapsed_s
        self.budget_s = budget_s
        super().__init__(
            f"{loop_name} loop exceeded wall-clock budget "
            f"({elapsed_s:.0f}s > {budget_s:.0f}s)"
        )


# Graceful-shutdown event. Thread-safe; replaces plain boolean.
# Use .set() to request shutdown, .is_set() to check, .wait(timeout) to sleep interruptibly.
_shutdown_event = threading.Event()


def _handle_sigterm(signum, frame):
    """Request a graceful stop: no new claims; the process then waits for in-flight builds.

    Railway sends SIGKILL after `drainingSeconds` (60, see railway.toml), far
    shorter than a build, so a build still running then is cut off mid-flight
    and handed back to the queue by orphan recovery in the next process.

    Only sets an event. Anything that can block (Sentry flush) happens after the
    main loop exits, not inside the signal handler. Installed by run_worker(),
    not at import time, so importing this module (tests, tools) never changes
    the host process's signal handling.
    """
    logger.info("SIGTERM received; finishing current job then exiting.")
    _shutdown_event.set()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_conn(retries: int = 10, base_delay: float = 2.0):
    """Return a new psycopg2 connection using DATABASE_URL from the environment.

    Retries with exponential backoff when the database is not yet available.
    Each attempt gives up after 15 seconds so an unreachable host cannot hang
    the calling thread for the OS TCP timeout.
    """
    import psycopg2

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL environment variable is not set")

    for attempt in range(1, retries + 1):
        try:
            return psycopg2.connect(db_url, connect_timeout=15)
        except psycopg2.OperationalError as exc:
            if attempt == retries:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), 30.0)
            logger.warning(
                "Database not ready (attempt %d/%d), retrying in %.1fs: %s",
                attempt, retries, delay, exc,
            )
            time.sleep(delay)
    raise RuntimeError("Unreachable")


def _ensure_conn(conn):
    """Check connection health and return a fresh one if stale.

    Long-running pipelines (15-60 min) can lose their DB connection due to
    PostgreSQL idle timeouts, Railway restarts, or network hiccups. This
    function verifies the connection with a lightweight query and reconnects
    if needed, preventing mid-pipeline failures.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return conn
    except Exception:
        logger.warning("Database connection stale, reconnecting")
        try:
            conn.close()
        except Exception as _guard_exc:  # closing a connection that is already gone
            logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)
        return _get_conn(retries=5, base_delay=1.0)


def _active_build_count(conn) -> int:
    """Return the number of builds currently in an active processing state."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM builds WHERE status IN ('building', 'testing', 'deploying')"
        )
        return cur.fetchone()[0]


def _get_daily_api_spend(conn) -> float:
    """Return the recorded API spend (USD) of builds worked on today.

    A build counts when its current attempt started today, or when it has no
    attempt in progress and its row was last written today. The second case is
    what keeps a retrying build in the sum: every requeue clears started_at,
    and a build waiting in the queue for its next attempt would otherwise drop
    out of the figure together with everything it already spent, which let the
    queue reopen the moment the build that reached the cap was requeued.
    (builds.updated_at is set by a trigger on every UPDATE, so the requeue
    itself and the cost write at the end of the attempt both stamp it.)

    "Today" is the database session's calendar day (CURRENT_DATE; UTC on the
    hosted Postgres). Cost is written when an attempt ends, so attempts in
    flight are not counted yet. See _DAILY_SPEND_CAP for what the cap bounds.

    Fails CLOSED: if the query cannot run, the spend is reported as infinite so
    the caller pauses new builds instead of silently disabling the cap.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(total_api_cost_usd), 0) FROM builds "
                "WHERE COALESCE(started_at, updated_at) >= CURRENT_DATE"
            )
            return float(cur.fetchone()[0])
    except Exception as exc:
        logger.warning("Failed to query daily API spend (pausing new builds): %s", exc)
        try:
            conn.rollback()
        except Exception as _guard_exc:  # rollback guard: the connection is already unusable
            logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)
        return float("inf")


# Transaction-scoped advisory lock key that serializes the "count active builds,
# then claim one" sequence across workers.
_CLAIM_LOCK_KEY = 7301001


def _claim_next_job(conn) -> Optional[dict]:
    """Atomically claim the next queued build and mark it as 'building'.

    Two guarantees:
      * No double claim: the row is picked with FOR UPDATE SKIP LOCKED and the
        status flip to 'building' commits in the same transaction, so two
        workers never get the same build.
      * No over-claim: the active-build count and the claim run under a
        transaction-scoped advisory lock (pg_try_advisory_xact_lock), so two
        workers cannot both see "0 active" and each start a build past
        MAX_CONCURRENT_BUILDS. A worker that loses the lock race simply tries
        again on its next poll.

    The oldest queued build is claimed first. builds.tier does not affect the
    order (it only selects the build timeout, see _TIER_TIMEOUTS).

    Returns None if the queue is empty, at the concurrency limit, over the daily
    spend cap, or another worker is mid-claim.
    """
    with conn.cursor() as lock_cur:
        lock_cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (_CLAIM_LOCK_KEY,))
        got_claim_lock = lock_cur.fetchone()[0]   # one row, one boolean, always
    if not got_claim_lock:
        logger.debug("Another worker is mid-claim; skipping this poll")
        conn.rollback()
        return None

    # Concurrency guard: skip if already at capacity.
    if _active_build_count(conn) >= _MAX_CONCURRENT:
        logger.debug("At concurrency limit (%d); skipping claim", _MAX_CONCURRENT)
        conn.rollback()
        return None

    # Daily spend guard: pause new builds when approaching spend cap.
    if _DAILY_SPEND_CAP > 0:
        daily_spend = _get_daily_api_spend(conn)
        if daily_spend >= _DAILY_SPEND_CAP:
            logger.info("Daily API spend $%.2f >= cap $%.2f; pausing new builds",
                        daily_spend, _DAILY_SPEND_CAP)
            conn.rollback()
            return None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.id, b.tier, b.app_name, b.spec, b.customer_id,
                   b.retry_count, b.rebuild_request, b.error_message,
                   c.email AS customer_email, c.name AS customer_name
            FROM builds b
            JOIN customers c ON c.id = b.customer_id
            WHERE b.status = 'queued'
            ORDER BY b.created_at
            LIMIT 1
            FOR UPDATE OF b SKIP LOCKED
            """,
        )
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return None

        (build_id, tier, app_name, spec, customer_id, retry_count, rebuild_request,
         error_message, customer_email, customer_name) = row

        # Authoritative engine attribution: the worker reads CAS_BUILD_ENGINE at
        # dispatch time and writes it onto the row, so the admin UI shows the
        # true engine instead of inferring it from spec shape (the v2 intake
        # translator runs in memory and does not persist the translated spec).
        # Default is "v2"; CAS_BUILD_ENGINE=v1 opts into the legacy pipeline for
        # repro and forensics. Unknown values fall back to v2 here, while the
        # build stage itself rejects them.
        _engine_for_dispatch = (os.environ.get("CAS_BUILD_ENGINE") or "v2").strip().lower()
        if _engine_for_dispatch not in ("v1", "v2"):
            _engine_for_dispatch = "v2"

        # started_at doubles as the attempt token (see the status contract at
        # the top of this file). Every claim commits its own transaction, so
        # NOW() differs from one claim of a build to the next.
        cur.execute(
            "UPDATE builds SET status = 'building', started_at = NOW(), build_engine = %s "
            "WHERE id = %s AND status = 'queued' "
            "RETURNING started_at",
            (_engine_for_dispatch, build_id),
        )
        claimed = cur.fetchone()
        if claimed is None or claimed[0] is None:
            # No attempt token, no job: every status write of the attempt would
            # be refused (see _attempt_param). The row was locked as 'queued'
            # by the SELECT above, so this is not expected to happen.
            conn.rollback()
            logger.error("Build %s: claim wrote no row; leaving it for the next poll", build_id)
            return None
        conn.commit()

    return {
        # The attempt token: every status write this attempt makes is guarded
        # by it, so a thread left over from an earlier attempt of the same
        # build cannot write to this one.
        "attempt": claimed[0],
        "id": str(build_id),
        "tier": tier,
        "app_name": app_name,
        "spec": spec,
        "customer_id": str(customer_id),
        "customer_email": customer_email,
        "customer_name": customer_name,
        "retry_count": retry_count or 0,
        "rebuild_request": rebuild_request,
        # Whoever queued the build may have left a stage-resume marker here
        # (see _parse_resume_marker and _resume_stage).
        "error_message": error_message,
    }


def _store_screenshots(conn, build_id: str, screenshot_paths: list) -> None:
    """Read screenshot PNGs from disk and merge into builds.screenshots JSONB.

    Each entry is {page: "/path", data: "base64...", phase: "test|deploy"} so
    the web service can serve images without filesystem access to the worker
    volume.

    MERGE semantics: new entries are appended to whatever is already in builds.screenshots,
    and any existing entry with the same (phase, page) pair is replaced by
    the newer one. This lets test-phase screenshots, deploy-phase login
    screenshots, and verify_render screenshots coexist on the same build
    instead of the last writer stomping the others.
    """
    if not screenshot_paths:
        return
    import base64
    new_entries = []
    for p in screenshot_paths:
        try:
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            # Derive page label from path
            parts = p.replace("\\", "/").split("/screenshots/")
            rel = parts[-1] if len(parts) > 1 else p
            page_label = "/" + rel.replace(".png", "").replace("-", "/")
            is_deploy = "/deploy/" in p
            new_entries.append({
                "page": page_label,
                "data": b64,
                "phase": "deploy" if is_deploy else "test",
            })
        except Exception as read_exc:
            logger.warning("Build %s: failed to read screenshot %s: %s", build_id, p, read_exc)
    if not new_entries:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT screenshots FROM builds WHERE id = %s",
                (build_id,),
            )
            row = cur.fetchone()
            existing = []
            if row and row[0]:
                raw = row[0]
                if isinstance(raw, (bytes, bytearray)):
                    try:
                        raw = raw.decode("utf-8")
                    except Exception:
                        raw = "[]"
                if isinstance(raw, str):
                    try:
                        existing = json.loads(raw) or []
                    except Exception:
                        existing = []
                elif isinstance(raw, list):
                    existing = raw
            new_keys = {(e.get("phase"), e.get("page")) for e in new_entries}
            preserved = [
                e for e in existing
                if isinstance(e, dict) and (e.get("phase"), e.get("page")) not in new_keys
            ]
            merged = preserved + new_entries
            cur.execute(
                "UPDATE builds SET screenshots = %s::jsonb WHERE id = %s",
                (json.dumps(merged), build_id),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("Build %s: failed to store screenshots: %s", build_id, exc)
        try:
            conn.rollback()
        except Exception as _guard_exc:  # rollback guard: the connection is already unusable
            logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)


def _initial_review_status() -> str:
    """review_status a build gets each time it arrives at preview.

    'pending' means human review at /admin/reviews. With
    CAS_HARBOR_AUTO_REVIEW switched on (an arming switch, see
    coordinator.envflags) the build is queued as 'ai_review_queued' instead,
    for an automated review loop that ran outside this repository and is not
    included in it: nothing in this repository consumes that state.
    Either way the build waits in preview; the worker never promotes it.
    """
    return 'ai_review_queued' if _auto_review_enabled() else 'pending'


def _auto_review_enabled() -> bool:
    """CAS_HARBOR_AUTO_REVIEW: off unless set to an explicit ON word."""
    return arming_switch("CAS_HARBOR_AUTO_REVIEW")


def _railway_record(project_id, service_id=None, environment_id=None, database_url=None,
                    live_url=None) -> dict:
    """The Railway facts about one deployment, in the shape the worker passes around.

    Kept in an attempt's own state from the moment deploy() returns (see
    _attempt_state) and written to the build row only by _set_preview_status.
    """
    return {
        "project_id": project_id,
        "service_id": service_id,
        "environment_id": environment_id,
        "database_url": database_url,
        "live_url": live_url,
    }


def _set_preview_status(conn, build_id: str, *, preview_url: str = None, attempt,
                        railway: dict = None) -> bool:
    """Move an active build to preview, put it into review and record its deployment.

    One guarded UPDATE. It matches only a build that is still building, testing
    or deploying, and only the attempt whose token is given (the token is
    required, see _attempt_param). In the same statement it sets
    status = 'preview', the initial review_status (see _initial_review_status)
    and preview_url, clears what an earlier review recorded (reviewed_at,
    reviewed_by, review_notes and the automated-review columns) and, when
    `railway` is given, records the Railway ids of the deployment that is being
    announced.

    This is the only statement in the worker that writes
    builds.railway_project_id, so that column always names a deployment that
    reached preview. (One other statement, _record_environment_id, fills in a
    missing environment id on a row that already names the project.) A
    project that an attempt created but did not get to preview lives
    in that attempt's own state (_attempt_state) and never appears on the row:
    nothing that reads the row (the admin delete in the web app, the janitor,
    the zombie probe's exclusion list) can take a failed attempt's project for
    the announced one, or the announced one for a failed attempt's. `railway`
    is a _railway_record; pass None for a local build that deployed nothing,
    and the row's Railway columns are left as they are.

    railway_database_url is stored with its user and password replaced by
    "***" (redact_url_credentials): the host, port and database name of the
    customer database stay, the password provision_postgres (cli/deploy.py)
    generated does not. No code reads the column. The password is kept by Railway
    itself, as the DATABASE_URL variable of the app service and
    POSTGRES_PASSWORD of the database service (both set by cli/deploy.py);
    monitor/backup.py reads DATABASE_URL from Railway's variables query.
    Whether that query answers with it at every point after a deploy cannot
    be established from this repository.

    A review verdict belongs to the deployment that was reviewed. Every arrival
    here follows a pipeline run that deployed again (a first build, a rebuild,
    a retry, a resume), so a verdict left on the row by an earlier cycle
    ('approved', 'changes_requested') is not carried over: the build goes back
    into review.

    When the row already recorded a different project (the build was in
    preview before, was requeued in place and has now deployed again), that
    earlier project is no longer referenced by the row. The worker does not
    destroy it, because it was announced; it is named in a warning and left
    for the operator.

    Returns True when this call moved the build to preview; the caller then
    announces it (see _announce_preview). Returns False when no row matched:
    the build is already in preview (a duplicate call for the same deployment)
    or it was failed, killed, requeued or published in the meantime, or it is
    on a later attempt. Nothing is written in that case, so a duplicate call
    can neither notify twice nor reset a verdict given since the first call,
    and a stale thread cannot pull a build back to preview. The worker never
    writes 'live': only the web app promotes a build, after review.
    """
    attempt_value = _attempt_param(attempt)
    has_railway = bool(railway and railway.get("project_id"))
    record = railway if has_railway else _railway_record(None)
    with conn.cursor() as cur:
        cur.execute(
            """WITH prior AS (SELECT railway_project_id FROM builds WHERE id = %s)
               UPDATE builds
                  SET status = 'preview',
                      review_status = %s,
                      reviewed_at = NULL,
                      reviewed_by = NULL,
                      review_notes = NULL,
                      review_report = NULL,
                      ai_review_iterations = 0,
                      ai_reviewed_at = NULL,
                      railway_project_id = CASE WHEN %s THEN %s ELSE railway_project_id END,
                      railway_service_id = CASE WHEN %s THEN %s ELSE railway_service_id END,
                      railway_environment_id = CASE WHEN %s THEN %s ELSE railway_environment_id END,
                      railway_database_url = CASE WHEN %s THEN %s ELSE railway_database_url END,
                      preview_url = %s,
                      completed_at = NOW(),
                      updated_at = NOW()
                WHERE id = %s
                  AND status IN ('building', 'testing', 'deploying')
                  AND started_at IS NOT DISTINCT FROM %s::timestamp
            RETURNING id, (SELECT railway_project_id FROM prior)
            """,
            (
                build_id,
                _initial_review_status(),
                has_railway, record["project_id"],
                has_railway, record["service_id"],
                has_railway, record["environment_id"],
                has_railway, redact_url_credentials(record["database_url"]),
                preview_url, build_id, attempt_value,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    landed = row is not None
    if not landed:
        logger.warning(
            "Build %s: preview transition matched no active build (already in preview, "
            "or failed, killed, requeued or published in the meantime, or on a later "
            "attempt); row left untouched",
            build_id,
        )
        return False
    prior_project_id = row[1]
    if has_railway and isinstance(prior_project_id, str) and prior_project_id \
            and prior_project_id != record["project_id"]:
        logger.warning(
            "Build %s: now announced on Railway project %s. Its earlier deployment, project "
            "%s, is no longer referenced by the build row. It was announced, so the worker "
            "does not delete it: it keeps running (and stays in the monitor registry) until "
            "an operator removes it.",
            build_id, record["project_id"], prior_project_id,
        )
    return True


# The janitor's candidates: failed or abandoned builds of one customer's app,
# beyond the two newest rows of that app. Placeholders: app_name, build id.
_JANITOR_CANDIDATES_SQL = """
    SELECT id FROM (
        SELECT id, status,
               ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rn
          FROM builds
         WHERE app_name = %s
           AND customer_id IS NOT DISTINCT FROM
               (SELECT customer_id FROM builds WHERE id = %s)
    ) ranked
    WHERE rn > 2 AND status IN ('failed', 'abandoned')
"""

# Locks the candidates that may be deleted and re-reads, at that moment, every
# condition that makes a build deletable. A row locked by anyone else is
# skipped (SKIP LOCKED): the web app's retry, a rebuild inserting a child of it
# and a trial being stamped with it all hold a lock on the row while they run.
# Placeholder: list of candidate ids.
_JANITOR_LOCK_SQL = """
    SELECT b.id
      FROM builds b
     WHERE b.id = ANY(%s::uuid[])
       AND b.status IN ('failed', 'abandoned')
       AND b.railway_project_id IS NULL
       AND b.live_url IS NULL
       AND NOT EXISTS (SELECT 1 FROM trial_keepers t WHERE t.handoff_build_id = b.id)
       AND NOT EXISTS (SELECT 1 FROM keepers k WHERE k.handoff_build_id = b.id)
       FOR UPDATE OF b SKIP LOCKED
"""

# The delete repeats the status predicate and the two lineage conditions that
# another transaction could have changed since the lock was taken by committing
# an INSERT (a new child row) or by stamping a trial with the build.
# Placeholders: list of ids to delete, twice.
_JANITOR_DELETE_SQL = """
    DELETE FROM builds b
     WHERE b.id = ANY(%s::uuid[])
       AND b.status IN ('failed', 'abandoned')
       AND b.railway_project_id IS NULL
       AND b.live_url IS NULL
       AND NOT EXISTS (SELECT 1 FROM trial_keepers t WHERE t.handoff_build_id = b.id)
       AND NOT EXISTS (SELECT 1 FROM keepers k WHERE k.handoff_build_id = b.id)
       AND NOT EXISTS (
           SELECT 1 FROM builds child
            WHERE child.parent_build_id = b.id
              AND NOT (child.id = ANY(%s::uuid[]))
       )
    RETURNING b.id
"""


def _without_rows_that_have_surviving_children(cur, candidate_ids: list) -> list:
    """Drop from `candidate_ids` every build that a surviving build descends from.

    A build may be deleted only together with all of its children. A child
    that stays (it is in preview, live, queued, in flight, or simply among the
    two newest rows) keeps its parent, and that parent keeps its own parent, so
    the set is reduced until it is closed: nothing outside it points into it.
    """
    keep_deleting = [str(candidate) for candidate in candidate_ids]
    while keep_deleting:
        cur.execute(
            "SELECT DISTINCT parent_build_id FROM builds "
            "WHERE parent_build_id = ANY(%s::uuid[]) AND NOT (id = ANY(%s::uuid[]))",
            (keep_deleting, keep_deleting),
        )
        parents_of_survivors = {str(row[0]) for row in cur.fetchall()}
        if not parents_of_survivors:
            break
        keep_deleting = [bid for bid in keep_deleting if bid not in parents_of_survivors]
    return keep_deleting


def _cleanup_old_builds(conn, build_id: str, app_name: str) -> int:
    """The janitor: delete old FAILED duplicate builds of one app. Returns how many.

    It deletes rows and nothing else. In particular it destroys no Railway
    project: a row that records one is not deleted at all (see below).

    Candidates are builds of the SAME customer with the same app_name (app_name
    is not unique across customers, so a name match alone is not ownership),
    in a failed or abandoned status, beyond the two newest rows of that app.
    A candidate is deleted only if, at the moment of the delete, all of this
    still holds:
      * it is still failed or abandoned. The status is part of the DELETE
        itself, and the row is locked first, so a build that was retried
        between the ranking query and the delete is not deleted;
      * no surviving build descends from it. A rebuild or revision points at
        its source through parent_build_id, and the web app has read ancestry
        through that chain to decide whether a build came out of a Harbor
        conversation (and therefore needs a subscription to be published).
        The janitor used to set parent_build_id to NULL on the children and
        delete the parent, which erased that ancestry. It now deletes a build
        only together with all of its descendants, and never rewrites
        parent_build_id on a row that stays;
      * no trial and no keeper is stamped with it (trial_keepers.handoff_build_id,
        keepers.handoff_build_id). Those columns are ON DELETE SET NULL, so
        deleting the build would silently clear the stamp that says "this
        trial has had its build", and with it the limit of one build per trial;
      * it records no Railway project and was never published
        (railway_project_id and live_url are NULL). Under the recording rule in
        _set_preview_status a project on a row is a deployment that was
        announced to the customer. Such a row is kept so that its project stays
        findable; an operator removes both together with the admin delete.

    What goes with a deleted build: its intake_messages, build_logs,
    feature_requests (and their messages) and dead_letter_builds rows, and its
    directory under builds/. What is deliberately not tied to it: the
    build_start_events ledger (no foreign key; the web app's start budget must
    not get refunded by a delete) and monitor_apps (a build without a Railway
    project has no registry row). The daily spend figure loses the cost of a
    deleted build (see _DAILY_SPEND_CAP).
    """
    if not app_name:
        return 0
    deleted_ids: list = []
    try:
        with conn.cursor() as cur:
            # Advisory lock on app_name prevents two concurrent builds from
            # racing to delete the same old builds (safe but wasteful).
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (app_name,))
            got_lock = cur.fetchone()[0]
            if not got_lock:
                logger.info("Build %s: skipping cleanup, another thread holds the lock for '%s'", build_id, app_name)
                return 0
            try:
                cur.execute(_JANITOR_CANDIDATES_SQL, (app_name, build_id))
                candidate_ids = [str(row[0]) for row in cur.fetchall()]
                if candidate_ids:
                    cur.execute(_JANITOR_LOCK_SQL, (candidate_ids,))
                    locked_ids = [str(row[0]) for row in cur.fetchall()]
                    stale_ids = _without_rows_that_have_surviving_children(cur, locked_ids)
                else:
                    stale_ids = []
                if stale_ids:
                    # Ids arrive as strings; every comparison against a UUID column
                    # needs the explicit ::uuid[] cast or Postgres rejects it with
                    # "operator does not exist: uuid = text".
                    cur.execute("DELETE FROM intake_messages WHERE build_id = ANY(%s::uuid[])", (stale_ids,))
                    cur.execute(
                        "DELETE FROM feature_messages WHERE feature_request_id IN "
                        "(SELECT id FROM feature_requests WHERE build_id = ANY(%s::uuid[]))",
                        (stale_ids,),
                    )
                    cur.execute("DELETE FROM feature_requests WHERE build_id = ANY(%s::uuid[])", (stale_ids,))
                    # build_logs and dead_letter_builds rows go with the build
                    # (ON DELETE CASCADE).
                    cur.execute(_JANITOR_DELETE_SQL, (stale_ids, stale_ids))
                    deleted_ids = [str(row[0]) for row in cur.fetchall()]
                    if len(deleted_ids) != len(stale_ids):
                        # A condition changed under the lock (a child or a
                        # stamp was committed meanwhile). Deleting the related
                        # rows of a build that stays would be wrong: undo all
                        # of it and let the next run decide again.
                        conn.rollback()
                        logger.info(
                            "Build %s: cleanup of '%s' rolled back, %d of %d builds were "
                            "no longer deletable", build_id, app_name,
                            len(stale_ids) - len(deleted_ids), len(stale_ids),
                        )
                        deleted_ids = []
            finally:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (app_name,))
        conn.commit()

        # Remove the deleted builds' directories. Names come from the database,
        # so only paths that stay inside the builds root are removed, and only
        # the per-build directory: a directory named after the app alone could
        # belong to another build of that name.
        for stale_id in deleted_ids:
            stale_dir = _safe_build_path(f"{app_name}-{stale_id[:8]}")
            if stale_dir is None or not stale_dir.exists():
                continue
            try:
                shutil.rmtree(stale_dir)
                logger.info("Disk cleanup: removed old build dir %s", stale_dir.name)
            except Exception as rm_exc:
                logger.warning("Disk cleanup: failed to remove %s: %s", stale_dir.name, rm_exc)

        if deleted_ids:
            logger.info("Cleaned up %d old build(s) for app '%s'", len(deleted_ids), app_name)
        return len(deleted_ids)
    except Exception as exc:
        logger.warning("Build %s: cleanup of old builds failed (non-blocking): %s", build_id, exc)
        try:
            conn.rollback()
        except Exception as _guard_exc:  # rollback guard: the connection is already unusable
            logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)
        # Session-level advisory locks survive a rollback; release ours so a
        # failed cleanup does not hold the lock until this connection closes.
        try:
            with conn.cursor() as unlock_cur:
                unlock_cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (app_name,))
            conn.commit()
        except Exception as unlock_exc:
            logger.debug("Build %s: could not release the cleanup lock for '%s': %s",
                         build_id, app_name, unlock_exc)
        return 0


# A Railway project younger than this is never treated as an orphan by the
# sweep below. An attempt records its project on the build row only when the
# build reaches preview, so for as long as an attempt can run (the longest
# tier timeout, plus slack) its project is referenced by no row, and the sweep
# may be starting while an attempt is in flight in another worker process.
_ORPHAN_SWEEP_MIN_PROJECT_AGE_S = 4 * 60 * 60


def _sweep_orphan_railway_projects(conn) -> int:
    """Delete Railway projects that no build row records. Off unless opted in.

    Runs on worker startup. Lists the account's Railway projects via the
    GraphQL API and deletes those that are recorded on no build row at all, are
    older than _ORPHAN_SWEEP_MIN_PROJECT_AGE_S and are not the platform
    project.

    Whose project that can be: one an attempt created and could not delete
    when it ended without reaching preview; one that was announced as a
    preview and then replaced when the same build row was requeued in place
    and deployed again; and any project in the account that this database
    never knew. The last case is why the sweep is dangerous.

    SAFETY: explicit opt-in required. Only the worker whose database is the
    source of truth for the Railway account may set ENABLE_ORPHAN_SWEEP=true.
    Any environment that shares the API token but has a different or empty
    database would classify every real project as an orphan and delete it.
    This sweep deleted production projects twice before the gate existed (see
    docs/RAILWAY_DISASTER_RECOVERY.md); it stays off by default.

    Looks at one page of at most 50 projects; see the comment at the query.

    Returns the number of projects deleted.
    """
    if not arming_switch("ENABLE_ORPHAN_SWEEP"):
        logger.info("Orphan sweep skipped (ENABLE_ORPHAN_SWEEP is not switched on; only runs on production worker)")
        return 0

    cas_project_id = os.environ.get("RAILWAY_PROJECT_ID", "")
    try:
        from deploy import gql, destroy_project

        # One page of at most 50 projects, and no further pages are read: a
        # project that Railway does not return in this page is never looked at,
        # so it is never deleted (the sweep errs toward leaving things alone).
        # The query does not follow a cursor because this repository cannot
        # check Railway's paging fields.
        result = gql(
            "query { projects(first: 50) { edges { node { id name createdAt } } } }",
        )
        all_projects = {
            p["node"]["id"]: p["node"]
            for p in result["projects"]["edges"]
        }

        # Every project a build row records is in use or was announced,
        # whatever the row's status: none of them is an orphan.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT railway_project_id FROM builds "
                "WHERE railway_project_id IS NOT NULL"
            )
            recorded_ids = {row[0] for row in cur.fetchall()}

        # Always keep the CAS platform project
        recorded_ids.add(cas_project_id)

        now = datetime.now(timezone.utc)
        deleted = 0
        for pid, node in all_projects.items():
            if pid in recorded_ids:
                continue
            name = node.get("name")
            try:
                created = datetime.fromisoformat(str(node.get("createdAt")).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_s = (now - created).total_seconds()
            except (TypeError, ValueError):
                age_s = None
            if age_s is None or age_s < _ORPHAN_SWEEP_MIN_PROJECT_AGE_S:
                logger.info(
                    "Orphan sweep: leaving project %s (%s): it is too young, or its age is "
                    "unknown, to rule out an attempt still in flight", pid[:8], name,
                )
                continue
            try:
                logger.info("Orphan sweep: about to delete Railway project %s (%s)", pid[:8], name)
                destroy_project(pid, token=None)
            except Exception as exc:
                logger.warning("Orphan sweep: project %s was NOT deleted: %s", pid[:8], exc)
                continue
            logger.info("Orphan sweep: deleted Railway project %s (%s)", pid[:8], name)
            deleted += 1
            _deregister_deleted_project(pid, "Orphan sweep")

        return deleted
    except Exception as exc:
        logger.warning("Orphan Railway project sweep failed (non-blocking): %s", exc)
        return 0


def _deregister_deleted_project(project_id: str, who: str) -> None:
    """Drop a Railway project that was really deleted from the monitor registry.

    Called only after destroy_project returned, which it does only when Railway
    confirmed the delete. Health checks and daily backups for that project stop
    here; nothing else removes a registry row except ten missed check cycles.
    """
    try:
        from monitor.registry import list_apps, remove_app
        for monitored in list_apps():
            if monitored.get("railway_project_id") == project_id:
                remove_app(monitored["id"])
                logger.info("%s: deregistered monitor app %s (project %s)",
                            who, monitored["app_name"], project_id[:8])
    except Exception as mon_exc:
        logger.warning("%s: monitor deregistration failed for project %s: %s",
                       who, project_id[:8], mon_exc)


# The stages a build thread moves a build through, in order. These are the only
# statuses _set_status writes. 'preview' is written by _set_preview_status,
# 'queued' by _requeue_for_retry, the supervisor's free retry and orphan
# recovery, and 'failed' by _fail_build. 'live' is absent on purpose: promoting
# a build is the web app's job, after review.
_STAGE_ORDER = ("building", "testing", "deploying")
_SET_STATUS_ALLOWED = frozenset(_STAGE_ORDER)


def _set_status(conn, build_id: str, status: str, *, attempt) -> bool:
    """Move a build forward to the pipeline stage `status`.

    Raises ValueError for anything that is not a pipeline stage (notably
    'live', 'preview', 'queued' and 'failed'), and for a missing attempt token
    (see _attempt_param). The UPDATE matches only a build that is currently in
    the same stage or an earlier one, so the write can neither move a build
    backwards (deploying to building) nor touch one that has left the worker's
    hands (requeued, previewed, failed, published), and only the attempt whose
    token is given, not a later one.

    Returns True when a row was updated. False means the build is no longer
    this thread's to advance; callers inside the pipeline go through
    _advance_stage, which stops the pipeline in that case.
    """
    if status not in _SET_STATUS_ALLOWED:
        raise ValueError(f"_set_status cannot write status {status!r}")
    attempt_value = _attempt_param(attempt)
    reachable_from = list(_STAGE_ORDER[: _STAGE_ORDER.index(status) + 1])
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE builds SET status = %s WHERE id = %s AND status = ANY(%s) "
            "AND started_at IS NOT DISTINCT FROM %s::timestamp",
            (status, build_id, reachable_from, attempt_value),
        )
        updated = cur.rowcount != 0
    conn.commit()
    if not updated:
        logger.warning(
            "Build %s: status write %r matched no row (the build is past that stage, "
            "no longer in a worker-owned status, or on a later attempt)",
            build_id, status,
        )
    return updated


def _advance_stage(conn, build_id: str, status: str, attempt) -> None:
    """_set_status for the pipeline: raise BuildNoLongerOwnedError when the
    write matches no row, so the pipeline does no further work (and above all
    does not deploy) for a build that was killed or taken over meanwhile."""
    if not _set_status(conn, build_id, status, attempt=attempt):
        raise BuildNoLongerOwnedError(
            f"status write {status!r} matched no row; this thread no longer owns the build"
        )


# On unless set to an explicit OFF word (a safety switch: the default login
# for a generated app has no password). The other readers that run inside this
# process or under it (cli/build.py, cli/test.py, cli/deploy.py) parse the
# variable with the same function, so with USE_MAGIC_LINK_AUTH=1 the generator
# builds the magic-link login this worker seeds and probes.
USE_MAGIC_LINK_AUTH = safety_switch("USE_MAGIC_LINK_AUTH")


def _generate_magic_link_token():
    """Generate a cryptographic magic link token pair.

    Returns dict with raw_token (64-char hex) and token_hash (SHA-256 hex).
    Only the hash goes into the generated app's seed.sql (and so into the
    stored build artifact). The generated app hashes the presented token the
    same way, sha256 over the hex STRING (not the decoded bytes), so the hashes
    match; tests/test_worker_pg.py::TestMagicLinkToken cross-checks this
    against Node's crypto. The raw token only ever appears in the link that is
    emailed to the customer (see _announce_preview).
    """
    raw_token = secrets.token_hex(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return {"raw_token": raw_token, "token_hash": token_hash}


# The hash is written into a SQL literal in seed.sql (seed files are plain SQL,
# not parameterized), so refuse anything outside a plain token alphabet.
_TOKEN_HASH_LITERAL_RE = re.compile(r"[A-Za-z0-9_\-]{1,128}")


def _inject_magic_link_token(build_dir, token_hash, customer_token_hash=None) -> bool:
    """Inject magic link token(s) into seed.sql for the admin user.

    Appends INSERT(s) into magic_link_tokens referencing the admin user. Tokens
    expire 24 hours after the generated app seeds them; the generated app
    consumes a token on first use.
    When customer_token_hash is provided, two tokens are injected: one for
    deploy verification (which gets consumed) and one for the customer to use.
    Returns True when the token(s) were written and False when seed.sql is
    missing (nothing was injected, so any raw token minted for it is useless).
    Raises ValueError if a hash is not a plain alphanumeric token (it is
    interpolated into SQL text).
    """
    for candidate in (token_hash, customer_token_hash):
        if candidate is not None and not _TOKEN_HASH_LITERAL_RE.fullmatch(candidate):
            raise ValueError("magic link token hash contains unexpected characters")
    seed_path = Path(build_dir) / "db" / "seed.sql"
    if not seed_path.exists():
        logger.warning("seed.sql not found in %s, skipping magic link token injection", build_dir)
        return False

    # Omit `id` column so the INSERT works against both engines:
    # v1 schema uses `id UUID DEFAULT gen_random_uuid()` and v2 compose
    # schema uses `id SERIAL`. Both auto-assign when id is absent from
    # the column list. Supplying `gen_random_uuid()` to a SERIAL column
    # raises "expression is of type uuid" and aborts the whole seed.
    insert_sql = (
        f"\n-- Magic link token for deploy verification\n"
        f"INSERT INTO magic_link_tokens (user_id, token_hash, expires_at)\n"
        f"SELECT id, '{token_hash}', NOW() + INTERVAL '24 hours'\n"
        f"FROM users WHERE role = 'admin' LIMIT 1;\n"
    )
    if customer_token_hash:
        insert_sql += (
            f"\n-- Magic link token for customer first login\n"
            f"INSERT INTO magic_link_tokens (user_id, token_hash, expires_at)\n"
            f"SELECT id, '{customer_token_hash}', NOW() + INTERVAL '24 hours'\n"
            f"FROM users WHERE role = 'admin' LIMIT 1;\n"
        )
    with open(seed_path, "a") as f:
        f.write(insert_sql)
    logger.info("Injected magic link token(s) into seed.sql")
    return True


def _build_magic_link_url(live_url, raw_token):
    """Construct the magic link URL for the deployed app."""
    return f"{live_url.rstrip('/')}/magic?token={raw_token}"


# What build_logs records about the login link of a deployed build: where it
# is. The link itself is never written to build_logs (see _announce_preview).
# Shown to the site's owner on the build's status page.
LOGIN_LINK_EMAILED_LOG_LINE = (
    "Your login link is in the email that says your website is ready for review; "
    "it is not shown in this log."
)


def _append_log(conn, build_id: str, stage: str, message: str) -> None:
    """Append a build log entry visible on the customer status page. Fire-and-forget.

    Uses a short-lived connection for each log write to avoid stale connection
    issues during long-running build_app() calls (20-40 min). Railway Postgres
    has idle timeouts that can kill the main pipeline connection, but log writes
    need to succeed throughout the entire build for the customer status page.
    """
    try:
        # Try the existing connection first
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO build_logs (build_id, stage, message) VALUES (%s, %s, %s)",
                (build_id, stage, message),
            )
        conn.commit()
    except Exception as exc:
        # Connection likely stale; try a fresh short-lived connection
        logger.warning("Build log write failed (stale conn?), retrying with fresh connection: %s", exc)
        try:
            conn.rollback()
        except Exception as _guard_exc:  # rollback guard: the connection is already unusable
            logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)
        fresh_conn = None
        try:
            fresh_conn = _get_conn(retries=2, base_delay=1.0)
            with fresh_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO build_logs (build_id, stage, message) VALUES (%s, %s, %s)",
                    (build_id, stage, message),
                )
            fresh_conn.commit()
        except Exception as exc2:
            logger.warning("Failed to write build log for %s (even with fresh conn): %s", build_id, exc2)
        finally:
            if fresh_conn:
                try:
                    fresh_conn.close()
                except Exception as _guard_exc:  # closing a connection that is already gone
                    logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)


# Patterns scrubbed from text that is persisted (build_logs, builds.error_message)
# or logged, because it can contain output from third-party tools and from the
# customer app. Best effort, not a guarantee: it targets URL credentials
# (monitor/url_redaction.py, applied first: everything between "://" and the
# last "@" of a URL's authority, a token-only userinfo included), bearer
# tokens, `name=value` pairs whose name says secret, and a few well known key
# shapes.
_SECRET_PATTERNS = (
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer ***"),
    # token=..., "password": "...", api_key: ..., including query strings
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_.\-]*(?:token|secret|password|passwd|api[_\-]?key|authorization)"
            r"[A-Za-z0-9_.\-]*[\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)"
        ),
        r"\1***",
    ),
    # Well-known key prefixes (Anthropic/OpenAI style, Stripe, Resend, GitHub, Slack, AWS)
    (
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_\-]{16,}|sk_(?:live|test)_[A-Za-z0-9]{8,}|re_[A-Za-z0-9]{16,}"
            r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,}"
            r"|AKIA[0-9A-Z]{16})\b"
        ),
        "***",
    ),
)


def _redact_secrets(text) -> str:
    """Return `text` as a string with likely credentials replaced by ***."""
    if text is None:
        return ""
    out = redact_url_credentials(str(text))
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _persist_deploy_logs(
    conn,
    build_id: str,
    dep_logs,
    reason: str = "",
    max_lines: int = 100,
    max_line_chars: int = 2000,
) -> None:
    """Persist Railway deploy log lines to build_logs for crash debugging.

    When a deploy crashes, the Railway project may be torn down before anyone
    looks at it. This helper takes already-fetched deploy logs and writes each
    line as its own `build_logs` row with stage='deploy-check', so post-mortem
    debugging can happen from the database alone.

    build_logs rows are served by /api/builds/:id/logs to the build's owner as
    well as to admins, and the lines are the customer app's own stdout, so
    every line goes through _redact_secrets before it is stored.

    Fire-and-forget. Any exception is logged and swallowed; never blocks
    the pipeline. No-op when dep_logs is empty or None.
    """
    if not dep_logs:
        return
    try:
        tail = list(dep_logs)[-max_lines:] if len(dep_logs) > max_lines else list(dep_logs)
        header = f"Captured {len(tail)} Railway deploy log lines"
        if reason:
            header += f" (reason: {reason})"
        _append_log(conn, build_id, "deploy-check", header)
        for line in tail:
            if line is None:
                continue
            text = str(line).strip()
            if not text:
                continue
            if len(text) > max_line_chars:
                text = text[:max_line_chars] + "…(truncated)"
            _append_log(conn, build_id, "deploy-check", _redact_secrets(text))
    except Exception as exc:
        logger.warning(
            "Build %s: failed to persist deploy logs (non-blocking): %s",
            build_id, exc,
        )


def _store_qa_results(conn, build_id: str, qa_result: dict) -> None:
    """Persist QA agent results (status + per-test detail) to the builds table.

    qa_status values:
      passed  - all QA checks passed
      warned  - some checks failed but build proceeds (non-blocking)
      skipped - QA could not run (setup failure, generation error, etc.)
    """
    if qa_result.get("skipped"):
        status = "skipped"
    elif qa_result.get("success"):
        status = "passed"
    else:
        status = "warned"

    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE builds SET qa_status = %s, qa_results = %s::jsonb WHERE id = %s",
                (status, json.dumps(qa_result.get("results", [])), build_id),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("Build %s: failed to store QA results: %s", build_id, exc)
        try:
            conn.rollback()
        except Exception as _guard_exc:  # rollback guard: the connection is already unusable
            logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)


def _store_design_review_results(conn, build_id: str, result: dict) -> None:
    """Persist design review results to the builds table."""
    if result.get("skipped"):
        status = "skipped"
    elif result.get("critical_count", 0) > 0:
        status = "warned"
    else:
        status = "passed"
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE builds SET design_review_status = %s, design_review_results = %s::jsonb WHERE id = %s",
                (status, json.dumps(result.get("issues", [])), build_id),
            )
        conn.commit()
    except Exception as e:
        logger.warning("Failed to store design review results: %s", e)
        try:
            conn.rollback()
        except Exception as _guard_exc:  # rollback guard: the connection is already unusable
            logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)


def _store_ui_test_results(conn, build_id: str, result: dict) -> None:
    """Persist UI test results to the builds table."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE builds SET ui_test_score = %s, ui_test_results = %s::jsonb WHERE id = %s",
                (result.get("score"), json.dumps(result), build_id),
            )
        conn.commit()
    except Exception as e:
        logger.warning("Failed to store UI test results: %s", e)
        try:
            conn.rollback()
        except Exception as _guard_exc:  # rollback guard: the connection is already unusable
            logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)


# Which failures repeat exactly when the same spec is built again is defined
# once, in ops/scanner.py, because the coordinator's automatic rebuilds ask the
# same question as the retry decision in _process_job.
_is_deterministic_failure = is_deterministic_failure


def _is_transient_error(error_message: str) -> bool:
    """Check if an error message matches known transient failure patterns."""
    lower = error_message.lower()
    return any(pattern in lower for pattern in _TRANSIENT_PATTERNS) or bool(_TRANSIENT_STATUS_RE.search(lower))


def _requeue_for_retry(conn, build_id: str, retry_count: int, error_message: str, *, attempt) -> bool:
    """Requeue a build for retry after a failed attempt.

    Only a build that is still worker-owned (building/testing/deploying) is
    requeued, and only the attempt whose token is given; a build that already
    reached preview, live or failed, or that has been requeued and claimed
    again, is left alone. Returns True when the row was requeued.
    """
    attempt_value = _attempt_param(attempt)
    new_count = retry_count + 1
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE builds
               SET status = 'queued',
                   retry_count = %s,
                   error_message = %s,
                   started_at = NULL,
                   completed_at = NULL
             WHERE id = %s
               AND status IN ('building', 'testing', 'deploying')
               AND started_at IS NOT DISTINCT FROM %s::timestamp
            """,
            (new_count, f"Retry {new_count}/{_MAX_RETRIES}: {_redact_secrets(error_message)}", build_id,
             attempt_value),
        )
        requeued = cur.rowcount != 0
    conn.commit()
    if requeued:
        logger.info("Build %s requeued for retry %d/%d", build_id, new_count, _MAX_RETRIES)
    else:
        logger.warning("Build %s: retry requeue matched no active build; leaving the row as is", build_id)
    return requeued


# Delimiter for the auto-injected design-issue block in builds.rebuild_request.
# Used by _inject_design_issues_as_rebuild_request to replace (not stack) the
# block on a subsequent retry, and to keep any customer-written revision text
# unmodified above it.
_DESIGN_QG_REBUILD_HEADER = "DESIGN REVIEW CRITICAL ISSUES (from previous attempt; must be fixed):"
_DESIGN_QG_MAX_ISSUES = 10  # cap to stay under prompt budget


def _inject_design_issues_as_rebuild_request(conn, build_id: str) -> bool:
    """Read the build's latest design_review_results, format critical issues
    into a rebuild_request block, append to any existing rebuild_request, and
    write back to builds.

    Called from the exception handler before requeueing a build that failed
    the BLOCKING_DESIGN_REVIEW gate. The next job claim reads
    builds.rebuild_request into the spec (see _process_job line ~1322), so
    build.py sees it as the HIGHEST PRIORITY section of the prompt.

    Returns True when injection wrote new text, False when it skipped (no
    critical issues, build row missing, DB error). Fail-safe: never raises.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT design_review_results, rebuild_request FROM builds WHERE id = %s",
                (build_id,),
            )
            row = cur.fetchone()
        if not row:
            return False
        issues_json, existing = row
        # design_review_results is stored as JSONB (list of issue dicts). psycopg2
        # returns it as a Python list already; defensive check for string (tests
        # or older rows) so we don't crash.
        issues = issues_json if isinstance(issues_json, list) else []
        critical = [i for i in issues if isinstance(i, dict) and i.get("severity") == "critical"]
        if not critical:
            # Fresh-generation retry can still help via stochasticity; caller
            # proceeds with requeue but without injection.
            return False

        bullets = []
        for i in critical[: _DESIGN_QG_MAX_ISSUES]:
            loc = (i.get("location") or "").strip()
            issue = (i.get("issue") or "").strip()
            fix = (i.get("fix") or "").strip()
            prefix = f"{loc}: " if loc else ""
            suffix = f" → {fix}" if fix else ""
            bullets.append(f"- {prefix}{issue}{suffix}")

        block = _DESIGN_QG_REBUILD_HEADER + "\n" + "\n".join(bullets)

        existing_text = (existing or "").strip()
        if not existing_text:
            merged = block
        elif _DESIGN_QG_REBUILD_HEADER in existing_text:
            # Replace the prior design block rather than stacking.
            idx = existing_text.index(_DESIGN_QG_REBUILD_HEADER)
            merged = (existing_text[:idx].rstrip() + "\n\n" + block).lstrip()
        else:
            # Preserve customer revision text; append design block below.
            merged = existing_text + "\n\n" + block

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE builds SET rebuild_request = %s WHERE id = %s",
                (merged, build_id),
            )
        conn.commit()
        logger.info(
            "Build %s: injected %d design issue(s) into rebuild_request",
            build_id, len(critical),
        )
        return True
    except Exception as exc:
        logger.warning(
            "Build %s: failed to inject design issues into rebuild_request: %s",
            build_id, exc,
        )
        try:
            conn.rollback()
        except Exception as _guard_exc:  # rollback guard: the connection is already unusable
            logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)
        return False


def _snapshot_build_dir(build_dir: str, reason: str) -> Optional[str]:
    """Copy the build directory to a timestamped snapshot before applying fixes.

    Creates a shallow copy of the build files so we can rollback if a fix
    makes things worse. Snapshots are stored alongside the build as
    {build_dir}.snapshot.{reason}.{timestamp}.

    Args:
        build_dir: Path to the build directory to snapshot.
        reason: Short label like 'pre-test-fix' or 'pre-deploy-fix'.

    Returns:
        Path to the snapshot directory, or None if the snapshot failed.
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        snapshot_dir = f"{build_dir}.snapshot.{reason}.{ts}"
        shutil.copytree(build_dir, snapshot_dir)
        logger.info("Snapshot saved: %s", snapshot_dir)
        return snapshot_dir
    except Exception as exc:
        logger.warning("Snapshot failed for %s (%s): %s", build_dir, reason, exc)
        return None


# ---------------------------------------------------------------------------
# Disk space management
# ---------------------------------------------------------------------------

_BUILDS_ROOT = Path(__file__).parent.parent / "builds"
_MIN_FREE_MB = 50  # Minimum free MB before triggering disk cleanup

# app_name comes from the database and becomes part of a directory name that the
# worker creates and deletes. Accept only a plain single path component.
_SAFE_DIR_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")


def _is_safe_dir_name(name) -> bool:
    """True when `name` is safe to use as one directory name under builds/.

    fullmatch, not match with a `$`: `$` also matches just before a trailing
    newline, which would let "name\n" through.
    """
    return isinstance(name, str) and bool(_SAFE_DIR_NAME_RE.fullmatch(name)) and ".." not in name


def _safe_build_path(name) -> Optional[Path]:
    """Return builds/<name> if `name` is a safe single component, else None."""
    if not _is_safe_dir_name(name):
        logger.warning("Refusing unsafe build directory name: %r", name)
        return None
    return _BUILDS_ROOT / name


def _get_free_disk_mb(path: Path) -> int:
    """Return free disk space in MB at the given path."""
    try:
        stat = shutil.disk_usage(path)
        return stat.free // (1024 * 1024)
    except Exception:
        return 9999  # assume plenty if we can't check


def _free_disk_space() -> int:
    """Delete node_modules, snapshot dirs, and stale build dirs to reclaim disk.

    Aggressively cleans the builds/ volume:
    1. Remove all node_modules (can be reinstalled)
    2. Remove snapshot dirs
    3. Remove entire build dirs that have no active/queued build in the DB

    Returns number of MB freed (approximate).
    """
    freed_mb = 0
    if not _BUILDS_ROOT.exists():
        return 0
    try:
        # Remove node_modules from all build dirs (they can be reinstalled)
        for nm_dir in _BUILDS_ROOT.glob("*/node_modules"):
            try:
                size_mb = sum(f.stat().st_size for f in nm_dir.rglob("*") if f.is_file()) // (1024 * 1024)
                shutil.rmtree(nm_dir)
                freed_mb += size_mb
                logger.info("Disk cleanup: removed %s (%dMB)", nm_dir, size_mb)
            except Exception as exc:
                logger.warning("Disk cleanup: failed to remove %s: %s", nm_dir, exc)
        # Remove snapshot dirs (e.g. builds/todo-list.snapshot.pre-test-fix.20260327...)
        for snap_dir in _BUILDS_ROOT.glob("*.snapshot.*"):
            try:
                shutil.rmtree(snap_dir)
                logger.info("Disk cleanup: removed snapshot %s", snap_dir)
            except Exception as exc:
                logger.warning("Disk cleanup: failed to remove snapshot %s: %s", snap_dir, exc)
        # Remove entire build dirs with no active build in the DB.
        # This catches accumulated dirs from failed builds that were never cleaned up.
        # Dir names are either legacy "{app_name}" or new "{app_name}-{build_id[:8]}".
        try:
            active_dir_names = set()
            conn = _get_conn()
            try:
                with conn.cursor() as cur:
                    # Collect both legacy (app_name) and new (app_name-buildid[:8]) dir names
                    cur.execute(
                        "SELECT app_name, id FROM builds "
                        "WHERE status IN ('queued', 'building', 'testing', 'deploying', 'preview', 'live')"
                    )
                    for row in cur.fetchall():
                        active_dir_names.add(row[0])  # legacy format
                        active_dir_names.add(f"{row[0]}-{str(row[1])[:8]}")  # new format
                conn.rollback()
            finally:
                conn.close()

            for build_dir in _BUILDS_ROOT.iterdir():
                if not build_dir.is_dir():
                    continue
                if ".snapshot." in build_dir.name:
                    continue  # already handled above
                if build_dir.name in active_dir_names:
                    continue
                try:
                    shutil.rmtree(build_dir)
                    logger.info("Disk cleanup: removed stale build dir %s", build_dir.name)
                except Exception as exc:
                    logger.warning("Disk cleanup: failed to remove build dir %s: %s", build_dir.name, exc)
        except Exception as exc:
            logger.warning("Disk cleanup: stale build dir removal failed: %s", exc)
    except Exception as exc:
        logger.warning("Disk cleanup failed: %s", exc)
    return freed_mb


def _startup_disk_cleanup() -> None:
    """Run full disk cleanup at worker startup.

    Clears accumulated build debris from any previous crashes:
    - All node_modules from builds/ (no longer written by the pipeline, but
      may exist from builds before the ENOSPC fix was deployed)
    - Snapshot directories
    - Stale build dirs with no active DB entry
    - /tmp/npm_test_* dirs leaked by crashed builds

    This unblocks a full volume on first deploy without manual Railway intervention.
    """
    import glob

    # 1. Clean /tmp/npm_test_* dirs from any build that crashed without cleanup
    leaked_count = 0
    for leaked_dir in glob.glob("/tmp/npm_test_*"):  # nosec B108  # removes only directories named like the worker's own npm test dirs (cli/test.py install_deps); single-tenant container
        try:
            shutil.rmtree(leaked_dir, ignore_errors=True)
            leaked_count += 1
        except Exception as _guard_exc:  # best-effort removal of one leftover; the next start tries again
            logger.debug("could not remove a leftover temp entry: %s: %s", type(_guard_exc).__name__, _guard_exc)
    if leaked_count:
        logger.info("Startup: cleaned %d leaked /tmp/npm_test_* dir(s)", leaked_count)

    # 1b. Clean /tmp/spec_*.json files from builds that crashed without cleanup
    spec_count = 0
    for spec_tmp in glob.glob("/tmp/spec_*.json"):  # nosec B108  # removes only files named like the worker's own spec temp files (_run_pipeline); single-tenant container
        try:
            Path(spec_tmp).unlink(missing_ok=True)
            spec_count += 1
        except Exception as _guard_exc:  # best-effort removal of one leftover; the next start tries again
            logger.debug("could not remove a leftover temp entry: %s: %s", type(_guard_exc).__name__, _guard_exc)
    if spec_count:
        logger.info("Startup: cleaned %d leaked /tmp/spec_*.json file(s)", spec_count)

    # 2. Clear volume debris (node_modules from old builds, snapshots, stale dirs)
    freed_mb = _free_disk_space()
    free_after = _get_free_disk_mb(_BUILDS_ROOT if _BUILDS_ROOT.exists() else Path("."))
    logger.info(
        "Startup disk cleanup: freed ~%dMB, now %dMB free on builds volume",
        freed_mb,
        free_after,
    )


def _ensure_disk_space() -> None:
    """Check disk space and free some if below threshold. Non-blocking."""
    free_mb = _get_free_disk_mb(_BUILDS_ROOT if _BUILDS_ROOT.exists() else Path("."))
    if free_mb < _MIN_FREE_MB:
        logger.warning("Low disk space: %dMB free, running cleanup", free_mb)
        freed = _free_disk_space()
        free_after = _get_free_disk_mb(_BUILDS_ROOT if _BUILDS_ROOT.exists() else Path("."))
        logger.info("Disk cleanup complete: freed ~%dMB, now %dMB free", freed, free_after)


def _cleanup_build_node_modules(build_dir: str) -> None:
    """Remove node_modules from a specific build dir to free disk. Non-blocking."""
    nm = Path(build_dir) / "node_modules"
    if nm.exists():
        try:
            shutil.rmtree(nm)
            logger.info("Removed node_modules from %s", build_dir)
        except Exception as exc:
            logger.warning("Failed to remove node_modules from %s: %s", build_dir, exc)


# ---------------------------------------------------------------------------
# compose-manifest.txt, as the worker reads it
# ---------------------------------------------------------------------------
#
# The shape the worker relies on (compose/build.py build_app writes it):
#   * UTF-8 text; header lines `key: value`, among them `files:`, `sha256:`,
#     `translation_warnings_count:` and, when the engine writes it,
#     `engine_warnings_count:`;
#   * then one line per build warning, with no line break inside a warning.
#     A line that starts with `warning: ` was written by the engine about its
#     own decisions. A line that starts with `spec-warning: ` is text the spec
#     carried. Engine lines come before spec lines.
# The file's size follows from the spec, so it is read with a cap, and every
# warning line inside the cap is copied to the build log: no fixed number of
# leading lines decides what a reviewer gets to see.

# How much of the manifest is read. A manifest is a few hundred bytes plus
# its warning lines; past this size the lint says so (an error line) and
# reports on the part it read.
_MANIFEST_MAX_BYTES = 256 * 1024
# The longest warning line copied whole; a longer one is cut and marked.
_MANIFEST_WARNING_MAX_CHARS = 1200
# The first warnings get a build-log entry each; the rest are packed, this
# many to an entry, so that a long list stays readable and stays complete.
_MANIFEST_WARNINGS_LISTED_SINGLY = 50
_MANIFEST_WARNINGS_PER_PACKED_ENTRY = 100
_ENGINE_WARNING_PREFIX = "warning: "
_SPEC_WARNING_PREFIX = "spec-warning: "


def _read_manifest(manifest_path) -> tuple:
    """(text, was_cut): at most _MANIFEST_MAX_BYTES of compose-manifest.txt.

    When the file is larger, the text ends at the last complete line inside
    the cap (half a line is not a warning) and was_cut is True.
    """
    with open(manifest_path, "rb") as handle:
        raw = handle.read(_MANIFEST_MAX_BYTES + 1)
    was_cut = len(raw) > _MANIFEST_MAX_BYTES
    if was_cut:
        raw = raw[:_MANIFEST_MAX_BYTES]
        raw = raw[:raw.rfind(b"\n") + 1]
    return raw.decode("utf-8", "replace"), was_cut


def _manifest_warning_checks(manifest: str) -> tuple:
    """(build-log entries, number of engine warnings) for a manifest's warnings.

    Every engine warning (`warning: `) is copied, then every spec warning
    (`spec-warning: `), each group in the order the manifest has it. Engine
    lines go first whatever order the file has them in, so a long list of
    spec-supplied text cannot stand between a reviewer and what the engine
    said. The first _MANIFEST_WARNINGS_LISTED_SINGLY lines are one entry
    each; the remainder is packed _MANIFEST_WARNINGS_PER_PACKED_ENTRY lines
    to an entry. Nothing is dropped.
    """
    engine, from_spec = [], []
    for line in manifest.split("\n"):
        if line.startswith(_ENGINE_WARNING_PREFIX):
            engine.append(line)
        elif line.startswith(_SPEC_WARNING_PREFIX):
            from_spec.append(line)
    ordered = [
        line if len(line) <= _MANIFEST_WARNING_MAX_CHARS
        else line[:_MANIFEST_WARNING_MAX_CHARS] + " ...(cut)"
        for line in engine + from_spec
    ]
    entries = [f"  {line}" for line in ordered[:_MANIFEST_WARNINGS_LISTED_SINGLY]]
    rest = ordered[_MANIFEST_WARNINGS_LISTED_SINGLY:]
    for start in range(0, len(rest), _MANIFEST_WARNINGS_PER_PACKED_ENTRY):
        group = rest[start:start + _MANIFEST_WARNINGS_PER_PACKED_ENTRY]
        entries.append("\n".join(f"  {line}" for line in group))
    return entries, len(engine)


# ---------------------------------------------------------------------------
# Build artifact store/restore (DB-based resumption without Railway volume)
# ---------------------------------------------------------------------------

def _lint_v2_build_output(build_dir) -> dict:
    """Post-build output lint for CAS_BUILD_ENGINE=v2. Runs a set of fast,
    deterministic checks against the emitted build directory and returns
    per-check results for inclusion in the customer-visible build log.

    Because v2 compose output is deterministic (no Claude in the build
    path), the v1 per-build Claude-driven test/design-review/QA loops
    are inapplicable. This lint replaces them with static assertions on
    the emitted artifact: required files present, manifest valid, seed
    rows match spec.seed_content counts, no raw `{Word}` italic-accent
    syntax leaked into any emitted view, style.css carries the expected
    CAS utility classes, every page template compiles to a widget
    include chain, no banned font/length-unit tokens.

    Returns a dict with:
      - "checks": list of str, per-check pass messages (user-visible)
      - "errors": list of str, per-check failure descriptions

    Non-fatal: caller logs failures but does not abort the build. As the
    v2 pipeline matures and the lint matures, individual checks can be
    promoted to fatal by raising from the caller.
    """
    from pathlib import Path as _Path
    import json as _json
    import re as _re

    checks: list = []
    errors: list = []
    bd = _Path(str(build_dir))

    # Required files: every v2 build must emit these.
    REQUIRED = [
        "compose-manifest.txt",
        "server.js",
        "package.json",
        "db_migrate.js",
        "db/schema.sql",
        "db/seed.sql",
        "views/layout-public.ejs",
        "views/layout-admin.ejs",
        "public/style.css",
        "public/logo.svg",
        "public/favicon.svg",
        "public/js/cas-reveal.js",
        "public/js/cas-form-validate.js",
    ]
    missing = [p for p in REQUIRED if not (bd / p).exists()]
    if missing:
        errors.append(f"Missing required files: {', '.join(missing)}")
    else:
        checks.append(f"Required files present ({len(REQUIRED)}/{len(REQUIRED)})")

    # Manifest: archetype/palette/font_pair + file count + sha256.
    manifest_path = bd / "compose-manifest.txt"
    if manifest_path.exists():
        manifest, manifest_cut = _read_manifest(manifest_path)
        for key in ("archetype:", "palette:", "font_pair:", "files:", "sha256:"):
            if key not in manifest:
                errors.append(f"compose-manifest.txt missing '{key}' line")
        m_files = _re.search(r"files:\s*(\d+)", manifest)
        if m_files:
            checks.append(f"Manifest reports {m_files.group(1)} files emitted")
        # Copy the build warnings from the manifest into the build log, so a
        # reviewer sees them in the review UI: every warning the engine wrote
        # about its own decisions (what the build shows to visitors, pages it
        # dropped) and the notes the spec carried. See _manifest_warning_checks
        # for which lines are copied and how many log entries they become.
        # The warning lines are copied whether or not a count header announces
        # them. The headers are matched at the start of a line, so text inside
        # a warning cannot stand in for one.
        warning_checks, engine_found = _manifest_warning_checks(manifest)
        m_warn_count = _re.search(r"(?m)^translation_warnings_count:\s*(\d+)", manifest)
        if warning_checks:
            announced = m_warn_count.group(1) if m_warn_count else "an unstated number of"
            checks.append(f"v1→v2 translation emitted {announced} warning(s); see translation-warnings.txt")
            checks.extend(warning_checks)
        elif m_warn_count and int(m_warn_count.group(1)) == 0:
            checks.append("No translation warnings (v1→v2 clean)")
        elif m_warn_count:
            errors.append(
                f"compose-manifest.txt announces {m_warn_count.group(1)} warning(s) and has no warning line"
            )
        m_engine_count = _re.search(r"(?m)^engine_warnings_count:\s*(\d+)", manifest)
        if m_engine_count and int(m_engine_count.group(1)) != engine_found:
            errors.append(
                f"compose-manifest.txt announces {m_engine_count.group(1)} engine warning(s) and "
                f"{engine_found} were found: not every engine warning is in this log"
            )
        if manifest_cut:
            errors.append(
                f"compose-manifest.txt is larger than {_MANIFEST_MAX_BYTES} bytes; only that much of it "
                "was read, and warnings past that point are not in this log "
                "(translation-warnings.txt in the artifact has them all)"
            )

    # Seed row counts match spec.seed_content expectations.
    spec_path = bd / "spec.json"
    seed_path = bd / "db" / "seed.sql"
    if spec_path.exists() and seed_path.exists():
        try:
            spec = _json.loads(spec_path.read_text())
            seed_sql = seed_path.read_text()
            sc = spec.get("seed_content", {}) or {}
            for model_key, rows in sc.items():
                if not isinstance(rows, list):
                    continue
                expected = len(rows)
                if expected == 0:
                    continue
                table = model_key  # seed uses snake_case table names
                # The key comes from the spec: it is matched as literal text,
                # never compiled as part of the pattern.
                actual = len(_re.findall(
                    r"^INSERT INTO " + _re.escape(str(table)) + r"(?![A-Za-z0-9_])",
                    seed_sql, _re.MULTILINE,
                ))
                if actual < expected:
                    errors.append(
                        f"seed.sql has {actual} INSERTs for {table}, expected >= {expected} per spec"
                    )
                else:
                    checks.append(f"Seed for {table}: {actual} rows (spec wants {expected})")
        except Exception as e:
            errors.append(f"Seed-vs-spec comparison failed: {e}")

    # No raw {Word} italic-accent syntax should leak into any EMITTED
    # view: every heading-capable widget should apply the _accent()
    # transform (or the hero-widget transform). Raw `{Word}` in a
    # rendered page means a widget missed the transform. Since widgets
    # are source-code, we look for `<%= heading %>` patterns (raw output)
    # in any emitted view: these would render literal `{Word}`.
    views_dir = bd / "views"
    if views_dir.exists():
        raw_heading_leaks = 0
        widgets_dir = views_dir / "widgets"
        if widgets_dir.exists():
            for w in widgets_dir.rglob("*.ejs"):
                body = w.read_text()
                # A widget is vulnerable if it emits `<%= heading %>` AND
                # does not declare `_accent` or `_rendered` (the two known
                # transform conventions). Hero widgets use `_rendered`.
                if "<%= heading %>" in body and "_accent" not in body and "_rendered" not in body:
                    raw_heading_leaks += 1
        if raw_heading_leaks > 0:
            errors.append(
                f"{raw_heading_leaks} widgets render `<%= heading %>` without {{Word}} transform"
            )
        else:
            checks.append("All heading-capable widgets apply the italic-accent transform")

    # style.css has the CAS utility classes we rely on downstream + no
    # banned v1-era tokens (Inter/Roboto fonts, excessive 100vh use).
    css_path = bd / "public" / "style.css"
    if css_path.exists():
        css = css_path.read_text()
        required_classes = [".cas-btn", ".cas-card-lift", ".cas-eyebrow", ".cas-reveal", ".cas-container"]
        missing_cls = [c for c in required_classes if c not in css]
        if missing_cls:
            errors.append(f"style.css missing classes: {', '.join(missing_cls)}")
        else:
            checks.append(f"style.css carries {len(required_classes)} required CAS utility classes")
        # Banned v1-era tokens
        for banned in ("font-family: Inter", "font-family: Roboto"):
            if banned in css:
                errors.append(f"style.css contains banned token: {banned!r}")
        vh100 = css.count("100vh")
        if vh100 > 2:
            errors.append(f"style.css uses 100vh {vh100} times: prefer 100dvh (iOS address-bar-safe)")
        else:
            checks.append(f"No banned font/length tokens (0 Inter/Roboto, {vh100} x 100vh)")

    # Every page view in views/*.ejs should consist of <%- include(...) %>
    # chains (widget composition). A view that falls back to static HTML
    # would be a pipeline regression.
    if views_dir.exists():
        # Top-level page views + any B2b custom pages under views/custom/.
        # All page views must be widget-composed (include-chain) so the
        # v2 deterministic-emission contract holds. Per-item detail views
        # (`*-detail.ejs` emitted by emit_page_detail_view for db-backed
        # listing pages) are exempt: they render a single DB record with
        # tolerant column reads, not a widget-grid composition.
        page_views = [v for v in views_dir.glob("*.ejs")
                      if v.name not in ("layout-public.ejs", "layout-admin.ejs",
                                        "error-404.ejs", "error-500.ejs",
                                        "error-csrf.ejs", "login.ejs",
                                        # Static legal pages (privacy / terms)
                                        # render plain boilerplate copy, not
                                        # widget-grid compositions.
                                        "privacy.ejs", "terms.ejs")
                      and not v.name.endswith("-detail.ejs")]
        custom_dir = views_dir / "custom"
        if custom_dir.is_dir():
            page_views.extend(custom_dir.glob("*.ejs"))
        non_compositional = []
        for v in page_views:
            body = v.read_text().strip()
            if not body:
                continue
            if "<%- include(" not in body:
                non_compositional.append(v.name)
        if non_compositional:
            errors.append(
                f"{len(non_compositional)} page view(s) not composed from widgets: {non_compositional[:3]}"
            )
        else:
            checks.append(f"All {len(page_views)} page views composed from widgets")

        # Page views must use `locals.X` (not bare `typeof X`) when
        # consuming res.render-passed locals. express-ejs-layouts can
        # render the page in a layout-only scope where bare access of
        # route-passed locals returns undefined, producing silent
        # empty-state renders.
        try:
            from compose.build import audit_emitted_views_for_bare_locals
            file_pairs = []
            for v in page_views:
                try:
                    rel = str(v.relative_to(bd))
                    file_pairs.append((rel, v.read_text()))
                except (OSError, ValueError):
                    continue
            locals_violations = audit_emitted_views_for_bare_locals(file_pairs)
            if locals_violations:
                offenders = sorted({v["path"] for v in locals_violations})[:5]
                errors.append(
                    f"{len(locals_violations)} bare `typeof X` violation(s) "
                    f"in page views (use locals.X): {offenders}"
                )
            else:
                checks.append(
                    f"All {len(page_views)} page views read render-passed locals via locals.X"
                )
        except Exception as _audit_exc:
            checks.append(f"locals.X audit skipped: {_audit_exc}")

    # CAS attribution present. The link lives in the shared
    # shell/cas-attribution.ejs partial that every footer family includes;
    # the classic shell/footer.ejs carries no inline 'Powered by' line.
    cas_partial = bd / "views" / "widgets" / "shell" / "cas-attribution.ejs"
    if cas_partial.exists() and "craftandship.io" not in cas_partial.read_text():
        errors.append("CAS attribution partial missing craftandship.io link")
    elif cas_partial.exists():
        checks.append("CAS 'made with Craft & Ship' attribution present in shared footer partial")
    else:
        # Backstop for older builds: classic footer carried the inline link.
        footer = bd / "views" / "widgets" / "shell" / "footer.ejs"
        if footer.exists() and "craftandship.io" not in footer.read_text():
            errors.append("Footer widget missing craftandship.io attribution")
        elif footer.exists():
            checks.append("CAS attribution present in footer")

    # ---- admin/public alignment check ---------------------------------
    # Every CRUD model emitted should have at least one public consumer:
    #   (a) a public listing handler in routes/pages.js (SELECT FROM <table>),
    #   (b) a public form handler in routes/pages.js that INSERTs into <table>,
    #   (c) a `<table>` reference in any non-admin view template, OR
    #   (d) the model's own route file has a public-ish GET handler that
    #       renders the manage view to anonymous visitors.
    #
    # When NONE of those hold, the keeper sees an admin tab with no
    # real-world use, and visitors have no way to produce rows except
    # by the keeper logging them manually. This is an advisory warning
    # so the SE reviewer can flag it before launch.
    routes_dir = bd / "routes"
    if routes_dir.exists() and views_dir.exists():
        SYSTEM_ROUTES = {
            "admin-dashboard", "admin-settings", "auth", "pages", "upload",
        }
        # Aggregate everything publicly consumed: routes/pages.js text
        # + every non-admin view text. Cheap O(n) string contains check.
        pages_js = (routes_dir / "pages.js").read_text() if (routes_dir / "pages.js").exists() else ""
        public_views_text = "".join(
            v.read_text() for v in views_dir.rglob("*.ejs")
            if "admin" not in v.parts
        )
        consumed_haystack = pages_js + "\n" + public_views_text
        orphan_admin_tabs = []
        for route_file in routes_dir.glob("*.js"):
            slug = route_file.stem
            if slug in SYSTEM_ROUTES:
                continue
            table = slug.replace("-", "_")
            route_text = route_file.read_text()
            # Public consumer signals (any one is enough):
            #   - routes/pages.js or any public view mentions the
            #     <table> name (covers SELECT FROM, INSERT INTO, and
            #     EJS data refs)
            #   - the route's own file has a `base_path: ... !isAdmin`
            #     branch (covers Sessions/SoundBaths admin-tabs that
            #     ALSO render their list view publicly)
            consumed_via_pages = table in consumed_haystack
            renders_publicly = (
                f"isAdmin ? '/admin/{slug}'" in route_text
                or f'isAdmin ? "/admin/{slug}"' in route_text
            )
            # If there's a form action targeting `/<slug>` from any
            # non-admin view, that's also a public consumer.
            has_public_form = (
                f'action="/{slug}' in public_views_text
                or f"action='/{slug}" in public_views_text
            )
            if not (consumed_via_pages or renders_publicly or has_public_form):
                orphan_admin_tabs.append(slug)
        if orphan_admin_tabs:
            checks.append(
                f"alignment-warn: admin tab(s) with no public consumer: {', '.join(orphan_admin_tabs)}"
            )
        else:
            checks.append("Admin tabs all have a public consumer (alignment check)")

    return {"checks": checks, "errors": errors}


def _store_build_artifact(conn, build_id: str, build_dir: str) -> None:
    """Compress build directory and store as BYTEA in builds.build_artifact.

    Enables pipeline resumption after worker restarts without a Railway volume.
    Leaves out everything under a node_modules or .git directory at any depth
    (dependencies can be reinstalled) to keep the artifact small, and stores
    every other file and directory exactly once.
    Skips, with a warning, if the directory is missing or the artifact exceeds
    50 MB.
    """
    import io
    import tarfile as _tarfile

    build_path = Path(build_dir)
    if not build_path.is_dir():
        logger.warning("Build %s: artifact store skipped, dir not found: %s", build_id, build_dir)
        return

    buf = io.BytesIO()
    try:
        with _tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for item in sorted(build_path.rglob("*")):
                rel = item.relative_to(build_path.parent)
                if any(p in ("node_modules", ".git") for p in rel.parts):
                    continue
                # recursive=False: rglob already yields every entry. Letting
                # tar.add descend into a directory stored each nested file
                # once per ancestor directory, node_modules included.
                tar.add(item, arcname=str(rel), recursive=False)
    except Exception as exc:
        logger.warning("Build %s: failed to create artifact tarball: %s", build_id, exc)
        return

    artifact = buf.getvalue()
    size_kb = len(artifact) // 1024
    if size_kb > 50_000:
        logger.warning("Build %s: artifact %d KB too large, skipping DB store", build_id, size_kb)
        return

    logger.info("Build %s: storing build artifact (%d KB)", build_id, size_kb)
    try:
        import psycopg2
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE builds SET build_artifact = %s WHERE id = %s",
                (psycopg2.Binary(artifact), build_id),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("Build %s: failed to store artifact in DB: %s", build_id, exc)
        try:
            conn.rollback()
        except Exception as _guard_exc:  # rollback guard: the connection is already unusable
            logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)


def _restore_build_artifact(conn, build_id: str, build_dir_name: str) -> bool:
    """Fetch build artifact from DB and extract to disk.

    Args:
        conn: Database connection.
        build_id: Build UUID.
        build_dir_name: Directory name under builds/ (e.g. ``app-name-8f54ef04``).

    Returns True if the artifact was fetched and extracted successfully.
    """
    import io
    import tarfile as _tarfile

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT build_artifact FROM builds WHERE id = %s", (build_id,))
            row = cur.fetchone()
    except Exception as exc:
        logger.warning("Build %s: failed to fetch artifact from DB: %s", build_id, exc)
        return False

    if not row or row[0] is None:
        return False

    artifact = bytes(row[0])
    builds_root = _BUILDS_ROOT
    builds_root.mkdir(exist_ok=True)

    try:
        buf = io.BytesIO(artifact)
        with _tarfile.open(fileobj=buf, mode="r:gz") as tar:
            safe_root = builds_root.resolve()
            safe_members = []
            for member in tar.getmembers():
                member_path = (builds_root / member.name).resolve()
                # Containment must compare path components: a plain string
                # prefix test would accept a sibling such as "builds-evil/".
                inside = member_path == safe_root or safe_root in member_path.parents
                # Artifacts are written by _store_build_artifact from regular
                # files and directories only; refuse links and device nodes.
                regular = member.isreg() or member.isdir()
                if not inside or not regular:
                    logger.warning("Build %s: skipping unsafe tar entry: %s", build_id, member.name)
                    continue
                safe_members.append(member)
            # Where the runtime supports it, also apply the stdlib "data" filter
            # (Python 3.12+, backported to 3.10.12 and 3.11.4): a second layer
            # that strips setuid bits and refuses anything escaping the target.
            extract_kwargs = {"filter": "data"} if hasattr(_tarfile, "data_filter") else {}
            tar.extractall(path=builds_root, members=safe_members, **extract_kwargs)  # nosec B202  # members pre-validated above: contained and regular files only
    except Exception as exc:
        logger.warning("Build %s: failed to restore artifact from DB: %s", build_id, exc)
        return False

    logger.info("Build %s: restored artifact from DB (%d KB) for %s", build_id, len(artifact) // 1024, build_dir_name)
    return True


def _clear_build_artifact(conn, build_id: str, attempt) -> bool:
    """Remove the stored artifact so that the next attempt composes the build afresh.

    Called only before a retry of a build whose attempt failed with a code
    error (not after a deploy: a deployed build keeps its artifact for the
    source download). The artifact belongs to the build, not to the attempt, so
    the UPDATE is guarded like every other write of an attempt: it matches
    only a build that is still worker-owned and still on the attempt whose
    token is given (see _attempt_param). An attempt that lost the build (an
    admin killed it and requeued it, a newer attempt was claimed) clears
    nothing, and it must not remove the working directory either.

    Returns True when the artifact was cleared, which also says that this
    attempt still owned the build a moment ago. False means it did not, or
    that the write failed (logged): in both cases the caller leaves the
    directory alone.
    """
    attempt_value = _attempt_param(attempt)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE builds SET build_artifact = NULL WHERE id = %s "
                "AND status IN ('building', 'testing', 'deploying') "
                "AND started_at IS NOT DISTINCT FROM %s::timestamp",
                (build_id, attempt_value),
            )
            cleared = cur.rowcount != 0
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception as _guard_exc:  # rollback guard: the connection is already unusable
            logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)
        logger.warning("Build %s: failed to clear build artifact: %s", build_id, exc)
        return False
    if not cleared:
        logger.warning(
            "Build %s: not clearing the stored artifact: the build left the worker-owned "
            "statuses or is on a later attempt", build_id,
        )
    return cleared


# The stage-resume marker. A requeue that wants the next attempt to skip
# validate and build says so in builds.error_message, which is otherwise free
# text (error messages, "Retry 2/6: ..."). The column is shared with the web
# app, so the marker is a contract between two code bases:
#   * the worker's orphan recovery writes  "Recovered: resuming from <stage> stage"
#   * the ops API's requeue-from-stage route (web/src/routes/ops-api.ts) writes
#                                          "Requeued: resuming from <stage> stage"
# with <stage> one of the two resumable stages. The whole column value must be
# exactly one of those strings: an error message that merely contains the
# words (a retry note quoting a log line, for instance) is not a marker.
_RESUMABLE_STAGES = ("testing", "deploying")
_RESUME_MARKER_PREFIXES = ("Recovered", "Requeued")
_RESUME_MARKER_RE = re.compile(
    "(?:" + "|".join(_RESUME_MARKER_PREFIXES) + "): resuming from ("
    + "|".join(_RESUMABLE_STAGES) + ") stage"
)


def _resume_marker(stage: str) -> str:
    """The marker orphan recovery writes for a build to resume from `stage`."""
    if stage not in _RESUMABLE_STAGES:
        raise ValueError(f"not a resumable stage: {stage!r}")
    return f"Recovered: resuming from {stage} stage"


def _parse_resume_marker(error_message) -> Optional[str]:
    """The stage named by a stage-resume marker, or None if this is not one.

    Exact format only (see the contract above): no surrounding text, no other
    case, no other stage.
    """
    if not isinstance(error_message, str):
        return None
    found = _RESUME_MARKER_RE.fullmatch(error_message)
    return found.group(1) if found else None


def _resume_stage(conn, job: dict, build_dir_name: str) -> Optional[str]:
    """Stage a claimed build resumes from: 'testing', 'deploying', or None.

    None means a full run (validate, build, test, deploy). A stage is returned
    only when the build's files are on disk, or were just restored from
    builds.build_artifact; otherwise there is nothing to resume from.

    Two things ask for a resume:
      * A stage-resume marker in the job's error_message, as the claim query
        returned it (_parse_resume_marker). Orphan recovery writes one
        (_recover_orphaned_builds), and so does the ops API's
        requeue-from-stage route in the web app.
      * No marker, but a stored artifact for this build: a requeued build
        (a transient-error retry, a manual retry) skips validate and build and
        resumes from 'testing'.
    """
    build_id = job["id"]
    requested = _parse_resume_marker(job.get("error_message"))

    on_disk = (_BUILDS_ROOT / build_dir_name / "package.json").exists()

    def _restored() -> bool:
        try:
            return bool(_restore_build_artifact(conn, build_id, build_dir_name))
        except Exception as restore_exc:
            logger.warning("Build %s: artifact restore raised: %s", build_id, restore_exc)
            return False

    if requested:
        if on_disk:
            logger.info("Build %s: resuming from %s stage (skipping validate+build)", build_id, requested)
            return requested
        if _restored():
            logger.info("Build %s: restored artifact from DB, resuming from %s", build_id, requested)
            return requested
        logger.warning(
            "Build %s: resume requested from %s but no artifacts found, full restart",
            build_id, requested,
        )
        return None

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT build_artifact IS NOT NULL AS has_artifact FROM builds WHERE id = %s", (build_id,))
            row = cur.fetchone()
        has_db_artifact = bool(row and row[0])
    except Exception as check_exc:
        logger.warning("Build %s: failed to check DB artifact: %s", build_id, check_exc)
        return None
    if not has_db_artifact:
        return None
    if on_disk or _restored():
        logger.info("Build %s: DB artifact exists (requeued build), resuming from testing", build_id)
        return "testing"
    logger.warning("Build %s: stored artifact could not be restored, full restart", build_id)
    return None


# ---------------------------------------------------------------------------
# What an attempt did outside the database
# ---------------------------------------------------------------------------

def _attempt_state(job: dict) -> dict:
    """The attempt's own record of what it created outside the database.

    It lives in the job dict, so it belongs to exactly one claim of one build.
    The build thread fills it in; _process_job and the supervisor read it.

      railway      _railway_record of the Railway project that deploy() created
                   in THIS attempt, or None. This, not the build row, is what
                   the worker consults before it destroys a project.
      announced    True once the preview write that records that project on
                   the build row has landed. An announced project is never
                   destroyed by the worker.
      announced_projects
                   Ids of every project a preview write recorded for this
                   attempt, including one the supervisor's probe found and
                   announced while the build thread was still inside deploy()
                   and had not reported its own project yet. A project in this
                   set is never destroyed by the worker, whatever the build
                   row says by the time the thread ends: a newer attempt's
                   preview write may have replaced the row's project since.
      discarded    True once that project was deleted again.
      abandoned    True once the supervisor stopped waiting for the build
                   thread; the thread then cleans up after itself when it ends.
      thread_ended True once the build thread passed its last look at
                   `abandoned` (the end of _run_pipeline). Both flags change
                   only under `handoff`, so exactly one side, the thread or
                   the supervisor, owns the clean-up.
      login_token  The customer's raw login token, once its hash is seeded.
      lock         Serializes everything that decides the fate of the
                   attempt's project: the discard (look-up and delete) in the
                   build thread, and the supervisor's preview write for a
                   project its probe found. It is held across a database
                   look-up and a Railway call, so nothing else may wait on it
                   without a bound (_ATTEMPT_LOCK_WAIT_S).
      handoff      Guards `abandoned` and `thread_ended` only; never held
                   across I/O.

    Nothing here is persisted. If the worker process dies, an attempt's
    unannounced project is known to nobody: it is not on the build row either
    (see _set_preview_status), so it is left running until an operator, or the
    opt-in orphan sweep, removes it.
    """
    state = job.get("attempt_state")
    if state is None:
        state = job["attempt_state"] = {
            "railway": None,
            "announced": False,
            "announced_projects": set(),
            "discarded": False,
            "abandoned": False,
            "thread_ended": False,
            "login_token": None,
            "lock": threading.Lock(),
            "handoff": threading.Lock(),
        }
    return state


# How long the supervisor waits for the attempt's lock before it gives up on
# announcing a project its probe found. The longest holder is _discard_unannounced_project:
# its look-up on a connection that connects within 15 s, then destroy_project, which
# reads the project's name (up to 5 tries of 30 s with 1, 2, 4 and 8 s between them)
# and sends the delete (one try of 30 s): about 200 s. On a timeout the supervisor
# treats the probe as having found nothing, which is the safe side: the build is
# requeued or failed, and the thread's discard still removes the project.
_ATTEMPT_LOCK_WAIT_S = 240.0


# Railway projects that attempts in this process created and that are not on a
# build row (yet). The zombie probe matches projects by name, and its exclusion
# list is read from the build rows, so without this set a second build of the
# same app name in this process could have its fresh project adopted by a stuck
# build's supervisor. (An attempt in ANOTHER worker process is not covered.)
_UNRECORDED_ATTEMPT_PROJECT_IDS: set = set()
_UNRECORDED_ATTEMPT_PROJECT_IDS_LOCK = threading.Lock()


def _note_attempt_project(job: dict, deploy_result: dict) -> None:
    """Record the project deploy() just created in the attempt's state."""
    project_id = deploy_result.get("railway_project_id")
    if not project_id:
        return
    _attempt_state(job)["railway"] = _railway_record(
        project_id,
        deploy_result.get("railway_service_id"),
        deploy_result.get("railway_environment_id"),
        deploy_result.get("database_url"),
        deploy_result.get("live_url"),
    )
    with _UNRECORDED_ATTEMPT_PROJECT_IDS_LOCK:
        _UNRECORDED_ATTEMPT_PROJECT_IDS.add(project_id)


def _record_announcement(state: dict, railway: Optional[dict]) -> None:
    """A preview write that recorded `railway` landed for the attempt `state` belongs to.

    Every project a preview write recorded for the attempt is remembered by id
    (`announced_projects`), also when the thread has not reported a project of
    its own yet (the supervisor's probe found the deployment while the thread
    was still inside deploy()): when the thread later reports the same project,
    the worker knows it was announced even if the build row has since been
    given another project by a newer attempt. If the announced project is the
    one this attempt created, `announced` says so too. If it is another one,
    the attempt's own project, should it create one, stays unannounced.

    Callers that race with _discard_unannounced_project hold state["lock"].
    """
    project_id = (railway or {}).get("project_id")
    if project_id:
        state["announced_projects"].add(project_id)
        own = state.get("railway") or {}
        if own.get("project_id") == project_id:
            state["announced"] = True
        with _UNRECORDED_ATTEMPT_PROJECT_IDS_LOCK:
            _UNRECORDED_ATTEMPT_PROJECT_IDS.discard(project_id)


def _mark_attempt_project_announced(job: dict, railway: Optional[dict]) -> None:
    """A preview write that recorded `railway` landed for this build (see _record_announcement)."""
    _record_announcement(_attempt_state(job), railway)


def _discard_unannounced_project(conn, job: dict, why: str) -> bool:
    """Delete the Railway project THIS attempt created, unless it was announced.

    Apart from deploy()'s clean-up after its own failed call and the opt-in
    orphan sweep, this is the only place where the worker deletes a Railway
    project. The project comes from the attempt's own state, never from the
    build row: the row's railway_project_id is the deployment the customer was
    last told about, and after an in-place requeue of a build that was in
    preview that is the previous, working site with its database.

    Nothing is deleted when
      * the attempt created no project, or already discarded it;
      * a preview write announced the project, for this attempt or (by the
        supervisor's probe) for the build while this attempt's thread was
        still inside deploy();
      * any build row records the project (a project announced by some other
        route);
      * that lookup cannot run. Not knowing is not permission.

    `conn` must be usable by the calling thread. A thread that the supervisor
    gave up on cannot use the connection it started with (the wrapper closed
    it when _process_job returned), so it goes through
    _discard_after_abandonment, which opens its own.

    The attempt's lock is held from the first check to the end of the delete,
    which is also how the supervisor's preview write for a probed project is
    ordered against this call (_write_probed_preview): the write lands before
    the look-up, and the project is then recorded, or it comes after the
    delete and sees `discarded`.

    Returns True when Railway confirmed the delete. A delete that was refused
    or failed leaves the project running; it is logged with its id so an
    operator can find it, since no build row will name it.
    """
    state = _attempt_state(job)
    build_id = job.get("id")
    with state["lock"]:
        railway = state.get("railway")
        if not railway or not railway.get("project_id") or state["discarded"]:
            return False
        project_id = railway["project_id"]
        if state["announced"] or project_id in state["announced_projects"]:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM builds WHERE railway_project_id = %s LIMIT 1",
                    (project_id,),
                )
                recorded = cur.fetchone() is not None
            conn.commit()
        except Exception as lookup_exc:
            try:
                conn.rollback()
            except Exception as _guard_exc:  # rollback guard: the connection is already unusable
                logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)
            logger.warning(
                "Build %s: could not check whether Railway project %s is recorded on a build "
                "(%s); NOT deleting it. It was created by this attempt (%s) and is left for "
                "the operator.",
                build_id, project_id, lookup_exc, why,
            )
            return False
        if recorded:
            logger.info(
                "Build %s: Railway project %s is recorded on a build row (it was announced); "
                "leaving it",
                build_id, project_id,
            )
            state["announced"] = True
            state["announced_projects"].add(project_id)
            return False
        if not os.getenv("RAILWAY_API_TOKEN", "").strip():
            # Without a token destroy_project cannot even look the project up
            # (deploy.get_token() raises DeployConfigError).
            logger.warning(
                "Build %s: RAILWAY_API_TOKEN is not set, so Railway project %s (created by "
                "this attempt, never announced: %s) cannot be deleted. No build row records "
                "it; it is left running for the operator.",
                build_id, project_id, why,
            )
            return False
        logger.info(
            "Build %s: deleting Railway project %s, which this attempt created and which was "
            "never announced (%s)",
            build_id, project_id, why,
        )
        try:
            from deploy import destroy_project
            destroy_project(project_id, token=None)
        except Exception as destroy_exc:
            logger.warning(
                "Build %s: Railway project %s was NOT deleted (%s). No build row records it; "
                "it is left running for the operator.",
                build_id, project_id, destroy_exc,
            )
            return False
        state["discarded"] = True
        with _UNRECORDED_ATTEMPT_PROJECT_IDS_LOCK:
            _UNRECORDED_ATTEMPT_PROJECT_IDS.discard(project_id)
        logger.info("Build %s: deleted Railway project %s", build_id, project_id)
        return True


def _discard_after_abandonment(job: dict, why: str) -> bool:
    """The clean-up of an attempt whose thread the supervisor stopped waiting for.

    Runs on that thread, as its last act. The connection the thread started
    with is not usable any more: _run_build_in_thread closed it when
    _process_job returned after the supervisor's decision. So the look-up that
    must precede a delete runs on a connection of its own. It is opened only
    when the attempt created a project that is neither announced nor already
    deleted, and it is closed before returning.

    The connection attempt is bounded: _get_conn(retries=3, base_delay=1.0)
    tries three times, each for at most 15 seconds, with 1 and 2 seconds
    between the tries (about 48 seconds). If no connection can be had the
    project is left running, and the log line names it and the attempt so an
    operator can find it: not knowing is not permission to delete.
    """
    state = _attempt_state(job)
    railway = state.get("railway") or {}
    project_id = railway.get("project_id")
    if (not project_id or state["discarded"] or state["announced"]
            or project_id in state["announced_projects"]):
        return False
    build_id = job.get("id")
    own_conn = None
    try:
        own_conn = _get_conn(retries=3, base_delay=1.0)
    except Exception as conn_exc:
        logger.warning(
            "Build %s: no database connection to check whether Railway project %s is recorded "
            "on a build (%s); NOT deleting it. It was created by this attempt (%s) and is left "
            "for the operator.",
            build_id, project_id, conn_exc, why,
        )
        return False
    try:
        return _discard_unannounced_project(own_conn, job, why)
    finally:
        try:
            own_conn.close()
        except Exception as _guard_exc:  # closing a connection that is already gone
            logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)


# Raised (as the message of a RuntimeError) when a deployed app never produced
# a healthy /health response. The wording matters: "timeout" is one of
# _TRANSIENT_PATTERNS, so the attempt is requeued and uses a retry, keeps its
# stored artifact, and is not tagged [v2-deterministic].
_DEPLOY_NEVER_HEALTHY = (
    "Deploy health check timeout: the deployed app did not answer /health with a healthy "
    "response within the wait window (last result: {last}). It was not announced as a preview."
)

# Railway ended the deployment as FAILED: its build step or its start step did
# not complete, so the generated app never ran. The text is the same on every
# attempt on purpose: it is not one of _TRANSIENT_PATTERNS, so the build gets
# one fresh attempt and, if Railway says FAILED again, _process_job's
# repeated-error check ends the series instead of spending every retry.
_DEPLOY_PLATFORM_FAILED = (
    "Railway reported the deployment FAILED: its build or start step did not complete, so the "
    "generated app never ran. Nothing shows the app to be at fault. It was not announced as a preview."
)

# Railway ended the deployment as CRASHED and no log of it could be read (no
# deployment to ask about, the log request failed, or the log has no lines),
# so nothing shows whether the generated app or the platform's Postgres is at
# fault. Unknown is not "deterministic": the text is the same on every attempt
# and is not a transient pattern, so, like _DEPLOY_PLATFORM_FAILED, it gets one
# fresh attempt and the repeated-error check ends the series if the crash
# repeats.
_DEPLOY_CRASH_CAUSE_UNKNOWN = (
    "Railway reported the deployment CRASHED, but no log of it could be read, so it is not known "
    "whether the generated app is at fault. It was not announced as a preview."
)

# Lines in a crashed deployment's log that mean the app could not reach its
# database: a problem of the freshly provisioned Postgres service, not of the
# generated code.
_DB_UNREACHABLE_LOG_PATTERNS = (
    "could not connect to database",
    "database connection attempt",
    "connection timeout",
    "connection terminated due to connection timeout",
    "econnrefused",
    "the database system is starting up",
)


def _logs_show_database_unreachable(deploy_logs) -> bool:
    log_text = " ".join(str(line) for line in (deploy_logs or []) if line).lower()
    return any(pattern in log_text for pattern in _DB_UNREACHABLE_LOG_PATTERNS)


def _v2_deploy_failure(deploy_result: dict, crash_logs, app_name: str) -> RuntimeError:
    """The error that ends a v2 attempt whose deploy never answered healthy.

    A v2 build is deterministic, so an app that crashes on start will crash
    the same way from the same spec: that failure is tagged [v2-deterministic]
    and never retried (the customer is told, the attempt's project removed).
    That verdict is only right when the generated app is what failed, so it is
    taken from what deploy() reported about the deployment, not from the word
    "failed" in a health result:

      * deployment_status FAILED: Railway did not get a container running.
        The app never ran. Retried (_DEPLOY_PLATFORM_FAILED).
      * deployment_status CRASHED and no log line could be read (`crash_logs`
        is None, or has no line with text): nothing shows whose fault it is.
        One fresh attempt (_DEPLOY_CRASH_CAUSE_UNKNOWN).
      * deployment_status CRASHED with a log that shows the database was
        unreachable: the platform's Postgres, not the app. Retried as a
        transient failure (_DEPLOY_NEVER_HEALTHY).
      * anything else (CRASHED with a log that shows no such thing, or a
        running deployment whose /health never said ok): the generated app.
        Deterministic.

    `crash_logs` is the deployment's log lines, or None when the log could not
    be read at all.
    """
    status = deploy_result.get("deployment_status")
    if status == "FAILED":
        return RuntimeError(_DEPLOY_PLATFORM_FAILED)
    if status == "CRASHED":
        if not any(str(line).strip() for line in (crash_logs or []) if line is not None):
            return RuntimeError(_DEPLOY_CRASH_CAUSE_UNKNOWN)
        if _logs_show_database_unreachable(crash_logs):
            return RuntimeError(_DEPLOY_NEVER_HEALTHY.format(last="its database was unreachable"))
    # The [v2-deterministic] tag is recognized by the retry decision in
    # _process_job: same input + same engine = same output, so a retry would
    # only create another Railway project.
    return RuntimeError(
        f"[v2-deterministic] v2 app deployed but crashes on startup. "
        f"deploy_fix skipped for CAS_BUILD_ENGINE=v2 (compose output "
        f"is deterministic; fix belongs in compose/, not a runtime "
        f"AI patch). See persisted deploy logs for '{app_name}'."
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _run_pipeline(job: dict, conn, _metrics_out: list = None) -> dict:
    """Run validate -> build -> test -> deploy for one job.

    Updates builds.status in PostgreSQL at each stage. Returns a dict with
    live_url and Railway metadata on success; raises on any failure, and raises
    BuildNoLongerOwnedError as soon as a stage write shows that the build has
    left the worker's hands.

    Supports stage-based resumption (see _resume_stage): when the build's files
    are available, validate and build are skipped.
    """
    build_id = job["id"]
    spec = job["spec"]  # already a dict (psycopg2 decodes JSONB)
    app_name = job["app_name"]
    tier = job["tier"]
    customer_email = job["customer_email"]
    customer_name = job["customer_name"]
    # Attempt token from the claim; guards this run's status writes.
    attempt = job.get("attempt")
    attempt_state = _attempt_state(job)
    # Key of this attempt's cancel event (see _process_job). The fix loops and
    # the model client poll it; None means nobody registered one.
    cancel_key = job.get("cancel_key")

    # Build directory is unique per build_id to prevent path collisions when
    # multiple builds for the same app_name run concurrently.
    build_dir_name = f"{app_name}-{build_id[:8]}"

    resume_from = _resume_stage(conn, job, build_dir_name)

    # Pre-compute build_dir so the except block can always clean up node_modules.
    build_dir = str(
        (_BUILDS_ROOT / build_dir_name).resolve()
    )

    # Write spec to a temp file so CLI modules can read it.
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix=f"spec_{build_id}_",
        dir="/tmp",  # nosec B108  # NamedTemporaryFile creates the file exclusively with mode 0600; fixed dir so _startup_disk_cleanup can find leftovers
        delete=False,
    )
    spec_path = tmp.name
    try:
        # Inject customer email as admin so the generated app's seed admin
        # uses the actual customer's email (they can log in as admin immediately).
        spec_with_admin = dict(spec)
        spec_with_admin["admin_email"] = customer_email
        if job.get("rebuild_request"):
            spec_with_admin["rebuild_request"] = job["rebuild_request"]
        json.dump(spec_with_admin, tmp)
        tmp.close()
        logger.info("Build %s: spec written to %s", build_id, spec_path)

        # Persist admin_email back to builds.spec so the status page can display it
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE builds SET spec = spec || %s::jsonb WHERE id = %s",
                    (json.dumps({"admin_email": customer_email}), build_id),
                )
            conn.commit()
        except Exception as exc:
            logger.warning("Build %s: failed to persist admin_email to spec: %s", build_id, exc)

        # Start collecting API cost metrics for this build
        from api_client import BuildCancelled, start_metrics_collection, get_collected_metrics
        start_metrics_collection()
        # BuildCancelled is what a fix loop or a model call raises once the
        # supervisor has fired this attempt's cancel event. The handlers
        # around the fix loops below treat most errors as "this stage is
        # optional, carry on"; a cancellation is re-raised past each of them so
        # that the thread ends instead of moving on to the next stage.

        # Stage timing tracker
        stage_timings = {}

        def _start_stage(stage_name):
            stage_timings[stage_name] = {"start": datetime.now(timezone.utc).isoformat()}

        def _end_stage(stage_name):
            if stage_name in stage_timings:
                now = datetime.now(timezone.utc)
                stage_timings[stage_name]["end"] = now.isoformat()
                start = datetime.fromisoformat(stage_timings[stage_name]["start"])
                stage_timings[stage_name]["duration_s"] = round((now - start).total_seconds(), 1)

        # CAS_BUILD_ENGINE dispatch gates BOTH validation and build stages.
        #   "v2" (default, also when unset): compose.build.validate_spec +
        #     compose/build.py (static compose; no AI in the build path).
        #   "v1" (legacy, explicit opt-in): cli/validate.py + cli/build.py
        #     (Claude Opus per build, then the registered post-processors).
        # There is no automatic fallback between engines: a spec the selected
        # engine rejects fails the build. Any other value raises below.
        _engine = (os.environ.get("CAS_BUILD_ENGINE") or "v2").strip().lower()
        if _engine not in ("v1", "v2"):
            raise ValueError(f"CAS_BUILD_ENGINE must be 'v1' or 'v2', got {_engine!r}")

        if not resume_from:
            # Stage 1: validate (engine-specific schema)
            _append_log(conn, build_id, "queued", f"Validating app specification (engine={_engine})")
            if _engine == "v2":
                import json as _json
                from compose.build import BuildSpec, validate_spec as _v2_validate
                with open(spec_path) as _f:
                    _spec_dict = _json.load(_f)
                # v1→v2 translation: if the spec came from the v1 intake
                # flow (has spec_version / user_roles / data_models but no
                # archetype/palette/font_pair), transparently translate it
                # via compose.intake.translate_v1_spec() so the v2 pipeline
                # can consume it. Preserves tier + admin_email from the
                # original v1 spec. This is the bridge that lets production
                # customer intake → v2 engine work without changes to the
                # customer-facing web/intake flow.
                _needs_translation = (
                    "archetype" not in _spec_dict
                    and ("spec_version" in _spec_dict or "user_roles" in _spec_dict or "data_models" in _spec_dict)
                )
                if _needs_translation:
                    _append_log(conn, build_id, "queued", "Translating v1 spec → v2 (archetype + palette + font-pair)")
                    try:
                        from compose.intake import translate_v1_spec
                        _orig_tier = _spec_dict.get("tier")
                        _orig_admin = _spec_dict.get("admin_email")
                        _orig_app = _spec_dict.get("app_name")
                        _spec_dict = translate_v1_spec(_spec_dict, admin_email=_orig_admin, app_name=_orig_app)
                        # Preserve tier: v2 doesn't use it in build-time
                        # composition but the worker / review / stripe layers
                        # may still reference it downstream.
                        if _orig_tier and "tier" not in _spec_dict:
                            _spec_dict["tier"] = _orig_tier
                        # Write the translated spec back to the spec_path
                        # file so downstream compose_build_app reads it.
                        with open(spec_path, "w") as _wf:
                            _json.dump(_spec_dict, _wf, indent=2)
                        _append_log(conn, build_id, "queued",
                                    f"v1→v2 translation complete (archetype={_spec_dict.get('archetype')}, "
                                    f"palette={_spec_dict.get('palette')}, font_pair={_spec_dict.get('font_pair')})")
                        logger.info("Build %s: v1→v2 translated (archetype=%s)", build_id, _spec_dict.get("archetype"))
                    except Exception as _tx_exc:
                        raise RuntimeError(
                            f"Spec validation failed with 1 error(s): v1→v2 translation failed: {_tx_exc}"
                        )
                try:
                    _v2_spec = BuildSpec.from_dict(_spec_dict)
                    errors = _v2_validate(_v2_spec)
                except Exception as _e:
                    errors = [str(_e)]
            else:
                from validate import load_and_validate
                _spec_data, errors = load_and_validate(spec_path)
            if errors:
                raise RuntimeError(
                    f"Spec validation failed with {len(errors)} error(s): {errors[0]}"
                )
            logger.info("Build %s: spec validated (engine=%s)", build_id, _engine)
            _append_log(conn, build_id, "queued", "Spec validated successfully")

            # Stage 2: build
            _start_stage("building")
            _advance_stage(conn, build_id, "building", attempt)
            logger.info("Build %s: building (engine=%s)", build_id, _engine)
            send_build_progress(customer_email, customer_name, app_name, "building", build_id=build_id)
            if _engine == "v2":
                _append_log(conn, build_id, "building",
                            f"Composing static-compose v2 build (tier={tier}, no Claude calls)")
                from compose.build import build_app as compose_build_app
                # v2 compose doesn't vary output by tier today; all tiers
                # get the same archetype + widget composition. Tier is
                # recorded in the build manifest for audit so SE reviewers
                # and the customer dashboard can see which pricing tier
                # produced this build. Downstream tier-gated features
                # (revision limits, review SLA, Stripe) read from
                # builds.tier in the DB: unchanged by engine choice.
                compose_build_app(spec_path, output_dir=build_dir, tier=tier)
                _append_log(conn, build_id, "building",
                            "Generation complete")
            else:
                _append_log(conn, build_id, "building", "Generating code with Claude (v1 engine)")
                from build import build_app
                build_app(spec_path, output_dir=build_dir, tier=tier)
                _append_log(conn, build_id, "building", "Code generation complete")
            _end_stage("building")
        else:
            logger.info("Build %s: skipped validate+build (resuming)", build_id)
            _append_log(conn, build_id, "building", f"Resuming from {resume_from} stage")

        # Stage 3: test (with self-correction loop)
        # Skip if resuming from deploying stage (tests already passed)
        # (build_dir is pre-computed before this try block)

        # Stage 2.5: review (runs after build before tests)
        # Checks generated code against spec: routes, CRUD, auth, seed data.
        # A critical finding that matches a known "will-fail-deploy" pattern
        # fails the build here (rollback: BLOCK_ON_CODE_REVIEW_CRITICAL=false).
        #
        # v2 engine: review.py assumes the v1 Claude-emitted output format and
        # compose output is deterministic by construction. Skipped for v2.
        if not resume_from and _engine == "v1":
            try:
                from review import review_build
                review_result = review_build(build_dir, spec, tier=tier)
                critical = review_result.get("critical_count", 0)
                warnings = review_result.get("warning_count", 0)
                summary = review_result.get("summary", "")
                skipped = review_result.get("skipped", False)
                if not skipped:
                    # Log technical details to server logger only, not to customer build logs
                    logger.info("Build %s: review complete, %s (%d critical, %d warnings)",
                                build_id, summary, critical, warnings)
                    if critical > 0:
                        issues = review_result.get("issues", [])
                        critical_issues = [i for i in issues if i.get("severity") == "critical"]
                        for issue in critical_issues:
                            logger.info("Build %s: review critical, [%s] %s",
                                        build_id, issue.get("file", "?"), issue.get("issue", ""))
                        # Feed review findings into KB so recurring issues become build rules
                        try:
                            from failure_kb import record_review_issues
                            record_review_issues(critical_issues, app_name=app_name)
                        except Exception as kb_exc:
                            logger.debug("Build %s: failure KB not updated: %s", build_id, kb_exc)

                        # Deploy-blocking gate: fail the build when a
                        # critical finding matches a pattern we know fix
                        # loops can't recover from at runtime. Gate is
                        # ON by default; only rolls back on an explicit
                        # rollback token (false/0/no/off/disabled).
                        if _block_gate_on("BLOCK_ON_CODE_REVIEW_CRITICAL"):
                            blocking = _matching_blocking_critical_findings(critical_issues)
                            if blocking:
                                summaries = "; ".join(
                                    f"[{i.get('file','?')}] {i.get('issue','')[:120]}"
                                    for i in blocking[:5]
                                )
                                _append_log(
                                    conn, build_id, "building",
                                    "Quality review found issues that need a rebuild. "
                                    "Flagging this build for retry with fresh generation.",
                                )
                                raise ReviewCriticalBlockError(
                                    f"Code review flagged {len(blocking)} "
                                    f"deploy-blocking critical finding(s): {summaries}"
                                )

                        # Customer-facing: reassuring message with no technical details
                        _append_log(conn, build_id, "building",
                                    "Quality checks complete, applying improvements...")
                    else:
                        _append_log(conn, build_id, "building", "Quality checks complete")
            except ReviewCriticalBlockError:
                # Propagate to _process_job's exception handler so the
                # normal code-error retry path runs (clears build artifact,
                # rebuilds fresh: exactly what a schema/query drift needs).
                raise
            except Exception as review_exc:
                logger.warning("Build %s: review stage error (non-fatal): %s", build_id, review_exc)

        # Stage 2.75: Design Review (non-blocking)
        # Checks visual quality: color contrast, typography, spacing, responsive.
        # v2 engine: skip, design is deterministic (palette/font-pair/widget
        # CSS live in compose/data/). design_fix.py would call Claude.
        if not resume_from and _engine == "v1":
            try:
                _start_stage("design_review")
                _append_log(conn, build_id, "building", "Reviewing design quality...")
                from design_review import review_design
                design_result = review_design(str(build_dir), spec)

                if design_result.get("critical_count", 0) > 0:
                    from design_fix import fix_design
                    max_design_fixes = env_int("MAX_DESIGN_FIXES", 3, min_val=0)
                    design_fix_attempts = 0
                    # WATCHDOG1 Layer 2: per-loop wall-clock budget. Prevents a
                    # pegged design_fix loop from consuming the whole tier timeout.
                    _design_fix_budget_start = time.monotonic()
                    _design_fix_budget = _FIX_LOOP_BUDGETS_S["design_fix"]
                    while design_result.get("critical_count", 0) > 0 and design_fix_attempts < max_design_fixes:
                        _design_fix_elapsed = time.monotonic() - _design_fix_budget_start
                        if _design_fix_elapsed >= _design_fix_budget:
                            _append_log(
                                conn, build_id, "building",
                                "One of our fix steps took longer than expected. Retrying with a fresh attempt.",
                            )
                            raise FixLoopBudgetExceeded(
                                "design_fix", _design_fix_elapsed, _design_fix_budget,
                            )
                        design_fix_attempts += 1
                        _append_log(conn, build_id, "building", "Enhancing design elements...")
                        fix_result = fix_design(
                            str(build_dir), spec, design_result.get("issues", []),
                            cancel_key=cancel_key,
                        )
                        if not fix_result.get("success") and not fix_result.get("files_modified"):
                            break  # fix agent has no ideas
                        prev_critical = design_result.get("critical_count", 0)
                        # Re-run post-processors after design fix
                        try:
                            from build import reapply_post_processors
                            reapply_post_processors(build_dir, spec)
                        except Exception as pp_exc:
                            logger.warning("Build %s: post-processor re-run after design_fix failed: %s", build_id, pp_exc)
                        design_result = review_design(str(build_dir), spec)
                        # RESUME_SUBSTAGE1: persist after each iteration so an interrupted
                        # design_fix loop leaves the latest state in DB. If the worker is
                        # killed mid-loop, qa/resume logic can read the last known state.
                        _store_design_review_results(conn, build_id, design_result)
                        # design-fix-loop-kb-recording: record the fix outcome in failure_kb
                        # so design fix patterns accumulate for future build prompt injection.
                        design_fix_succeeded = design_result.get("critical_count", 0) < prev_critical
                        try:
                            from failure_kb import record_fix
                            issues_summary = "; ".join(
                                i.get("issue", "")[:80]
                                for i in design_result.get("issues", [])[:3]
                            ) or "design review critical issues"
                            record_fix(
                                issues_summary,
                                f"design_fix applied {len(fix_result.get('files_modified', []))} file(s)",
                                fix_result.get("files_modified", []),
                                fix_category="design",
                                succeeded=design_fix_succeeded,
                                app_name=app_name,
                            )
                        except Exception as kb_exc:
                            logger.debug("Build %s: failure KB not updated: %s", build_id, kb_exc)
                        # Early exit if no progress after 2+ attempts
                        if design_result.get("critical_count", 0) >= prev_critical and design_fix_attempts >= 2:
                            break

                _store_design_review_results(conn, build_id, design_result)

                if design_result.get("critical_count", 0) == 0:
                    _append_log(conn, build_id, "building", "Design quality verified!")
                else:
                    _append_log(conn, build_id, "building", "Design review complete")

                # Blocking design review gate (env-var gated, default off).
                # Raises DesignQualityGateError (subclass of QualityGateError) so
                # the exception handler can distinguish design-gate failures and
                # allow one retry with critical issues injected as a rebuild_request.
                if _blocking_design_review():
                    if design_result.get("critical_count", 0) > 0:
                        raise DesignQualityGateError(
                            f"Design review: {design_result['critical_count']} critical issues after fix attempts"
                        )

                _end_stage("design_review")
            except (QualityGateError, FixLoopBudgetExceeded, BuildCancelled):
                _end_stage("design_review")
                raise  # re-raise so it reaches _process_job's except block
            except Exception as dr_exc:
                logger.warning("Build %s: design review error (non-fatal): %s", build_id, dr_exc)
                _store_design_review_results(conn, build_id, {"skipped": True})
                _end_stage("design_review")

        # Store artifact after fresh build + reviews so the pipeline can resume
        # if the worker restarts before deploy (no Railway volume needed).
        if not resume_from:
            _store_build_artifact(conn, build_id, build_dir)

        if _engine == "v2":
            # v2 compose output is deterministic by construction (widgets +
            # contracts pass at test-time in compose/tests/). Skip the v1
            # per-build Claude-driven test/fix/design-review loops. Instead
            # run a fast post-build output lint that verifies the emitted
            # artifact against the compose contracts: catches regressions
            # like widget-transform misses, missing seed rows, missing
            # required files, and surfaces real per-check results in the
            # customer-visible build log.
            _advance_stage(conn, build_id, "testing", attempt)
            _start_stage("testing")
            _append_log(conn, build_id, "testing", "Running v2 build-output lint (deterministic, 0 Claude calls)")
            try:
                _v2_lint = _lint_v2_build_output(build_dir)
            except Exception as _lint_exc:
                logger.warning("Build %s: v2 lint error (non-fatal): %s", build_id, _lint_exc)
                _v2_lint = {"checks": [], "errors": [f"Lint runner crashed: {_lint_exc}"]}
            for _line in _v2_lint.get("checks", []):
                _append_log(conn, build_id, "testing", _line)
            if _v2_lint.get("errors"):
                for _err in _v2_lint["errors"]:
                    _append_log(conn, build_id, "testing", f"LINT FAIL: {_err}")
                # v2 lint is warning-level for now (don't fail the build);
                # surface prominently so the failures are visible.
                logger.warning("Build %s: v2 lint surfaced %d failures", build_id, len(_v2_lint["errors"]))
            else:
                _append_log(conn, build_id, "testing",
                            f"v2 lint PASSED ({len(_v2_lint.get('checks', []))} checks, 0 failures)")
            logger.info("Build %s: v2 lint complete (%d checks, %d failures)",
                        build_id, len(_v2_lint.get("checks", [])), len(_v2_lint.get("errors", [])))
            _end_stage("testing")
            _append_log(conn, build_id, "testing",
                        "Skipped v1 Claude-driven QA/design-review loops (v2 compose has no LLM variance)")
        elif resume_from == "deploying":
            logger.info("Build %s: skipped testing (resuming from deploying)", build_id)
            _append_log(conn, build_id, "testing", "Skipped (resuming from deploy stage)")
        else:
            _start_stage("testing")
            _advance_stage(conn, build_id, "testing", attempt)
            logger.info("Build %s: testing", build_id)
            send_build_progress(customer_email, customer_name, app_name, "testing", build_id=build_id)
            _append_log(conn, build_id, "testing", "Installing dependencies and running tests")
            # Free disk space before npm install to prevent ENOSPC failures
            _ensure_disk_space()
            import test as test_module

            max_fix_attempts = 4
            test_result = test_module.test_app(build_dir)
            fixes_attempted = 0

            # Fast-fail for infrastructure errors that the fix agent cannot solve.
            # npm install failures and app startup crashes are not code bugs;
            # the fix loop would waste 4 Claude API calls per retry.
            _infra_errors = ("npm install failed", "npm error", "ENOENT", "ENOSPC", "ENOMEM")
            _is_infra_failure = (
                not test_result["success"]
                and len(test_result["results"]) == 1
                and test_result["results"][0][0] == "app startup"
                and any(kw in str(test_result["results"][0][2]) for kw in _infra_errors)
            )
            if _is_infra_failure:
                logger.info("Build %s: infrastructure failure (not fixable by code), skipping fix loop", build_id)
                _append_log(conn, build_id, "testing",
                            "Infrastructure issue detected, retrying with fresh environment...")

            if not test_result["success"] and not _is_infra_failure:
                _snapshot_build_dir(build_dir, "pre-test-fix")
                _append_log(conn, build_id, "testing", "Making some final adjustments to your website...")
                # WATCHDOG1 Layer 2: per-loop wall-clock budget.
                _fix_budget_start = time.monotonic()
                _fix_budget = _FIX_LOOP_BUDGETS_S["fix"]
                for fix_attempt in range(max_fix_attempts):
                    _fix_elapsed = time.monotonic() - _fix_budget_start
                    if _fix_elapsed >= _fix_budget:
                        _append_log(
                            conn, build_id, "testing",
                            "One of our fix steps took longer than expected. Retrying with a fresh attempt.",
                        )
                        raise FixLoopBudgetExceeded("fix", _fix_elapsed, _fix_budget)
                    fixes_attempted = fix_attempt + 1
                    logger.info(
                        "Build %s: %d test(s) failed, attempting self-correction (%d/%d)",
                        build_id, test_result["failed"], fixes_attempted, max_fix_attempts,
                    )

                    try:
                        from fix import attempt_fix
                        fix_result = attempt_fix(
                            build_dir=build_dir,
                            test_results=test_result["results"],
                            app_stderr=test_result.get("app_stderr", ""),
                            spec=spec,
                            cancel_key=cancel_key,
                        )

                        if not fix_result["fixed"]:
                            logger.info("Build %s: no fixes produced, stopping", build_id)
                            break

                        # Re-run critical post-processors (fix.py may overwrite
                        # layout.ejs, migrate.js, style.css without post-processing)
                        try:
                            from build import reapply_post_processors
                            reapply_post_processors(build_dir, spec)
                        except Exception as pp_exc:
                            logger.warning("Build %s: post-processor re-run failed: %s", build_id, pp_exc)

                        logger.info(
                            "Build %s: fix applied (%s), re-testing",
                            build_id, fix_result["description"],
                        )
                        _append_log(conn, build_id, "testing", "Almost there, running final checks...")
                        test_result = test_module.test_app(build_dir)

                        # Record true fix outcome now that we know if tests passed
                        try:
                            from failure_kb import record_fix
                            record_fix(
                                fix_result.get("error_summary", fix_result["description"]),
                                fix_result["description"],
                                fix_result.get("files_changed", []),
                                fix_category=fix_result.get("fix_category", "unknown"),
                                succeeded=test_result["success"],
                                app_name=app_name,
                            )
                        except Exception as kb_exc:
                            logger.debug("Build %s: failure KB not updated: %s", build_id, kb_exc)

                        if test_result["success"]:
                            logger.info("Build %s: self-correction succeeded on attempt %d", build_id, fixes_attempted)
                            _append_log(conn, build_id, "testing", "All tests passing after fix")
                            break

                    except BuildCancelled:
                        raise
                    except Exception as fix_exc:
                        logger.error("Build %s: self-correction failed: %s", build_id, fix_exc)
                        break
            else:
                _append_log(conn, build_id, "testing", "All tests passed")

            if not test_result["success"]:
                failed_details = [
                    f"{name}: {err[:150]}" if err else name
                    for name, passed, err in test_result["results"] if not passed
                ]
                raise RuntimeError(
                    f"Test suite failed after {fixes_attempted} fix attempt(s); "
                    f"deployment aborted. Failed: {'; '.join(failed_details)}"
                )

            # Store test screenshots if captured
            test_screenshots = test_result.get("screenshots", [])
            if test_screenshots:
                _append_log(conn, build_id, "testing", f"Captured {len(test_screenshots)} screenshot(s)")
                _store_screenshots(conn, build_id, test_screenshots)

            # Update stored artifact after tests pass (includes any fix changes).
            # If deploy fails and the build is requeued, it resumes from deploying.
            _store_build_artifact(conn, build_id, build_dir)

            # Stage 3.5: Deep QA (spec-aware, non-blocking)
            # Runs after smoke tests pass. Failures warn but never abort the build.
            # RESUME_SUBSTAGE1: skip QA entirely when resuming and QA already ran.
            # On resume-from-testing the smoke tests above have just re-passed,
            # but QA may have completed in the prior run; avoid re-spending
            # Haiku API budget on a completed sub-stage.
            _qa_already_done = False
            if resume_from:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT qa_status FROM builds WHERE id = %s", (build_id,)
                        )
                        _qa_row = cur.fetchone()
                        if _qa_row and _qa_row[0] and _qa_row[0] != "pending":
                            _qa_already_done = True
                            logger.info(
                                "Build %s: QA already completed (%s) before resume, skipping",
                                build_id, _qa_row[0],
                            )
                except Exception as _qa_check_exc:
                    logger.debug("Build %s: QA status check failed, will re-run QA: %s",
                                 build_id, _qa_check_exc)

            if _qa_already_done:
                _append_log(conn, build_id, "testing", "Quality checks previously completed")
            else:
                _append_log(conn, build_id, "testing", "Running quality checks")

            if not _qa_already_done:
                try:
                    from qa import run_qa
                    qa_result = run_qa(build_dir, spec, tier=tier)

                    if qa_result.get("skipped"):
                        _append_log(conn, build_id, "testing",
                                    f"QA skipped: {qa_result.get('qa_report', '')}")
                    elif qa_result["success"]:
                        _append_log(conn, build_id, "testing",
                                    f"QA passed ({qa_result['passed']} checks)")
                    else:
                        qa_snapshot_dir = _snapshot_build_dir(build_dir, "pre-qa-fix")
                        _append_log(conn, build_id, "testing",
                                    "Polishing a few details...")
                        logger.info("Build %s: %d QA failure(s), attempting qa_fix",
                                    build_id, qa_result["failed"])

                        try:
                            from qa_fix import attempt_qa_fix
                            max_qa_fixes = env_int("MAX_QA_FIXES", 4, min_val=0)
                            # WATCHDOG1 Layer 2: per-loop wall-clock budget.
                            _qa_fix_budget_start = time.monotonic()
                            _qa_fix_budget = _FIX_LOOP_BUDGETS_S["qa_fix"]
                            for qa_fix_attempt in range(max_qa_fixes):
                                _qa_fix_elapsed = time.monotonic() - _qa_fix_budget_start
                                if _qa_fix_elapsed >= _qa_fix_budget:
                                    _append_log(
                                        conn, build_id, "testing",
                                        "One of our fix steps took longer than expected. Retrying with a fresh attempt.",
                                    )
                                    raise FixLoopBudgetExceeded(
                                        "qa_fix", _qa_fix_elapsed, _qa_fix_budget,
                                    )
                                fix_result = attempt_qa_fix(
                                    build_dir=build_dir,
                                    qa_results=qa_result["results"],
                                    spec=spec,
                                    cancel_key=cancel_key,
                                )

                                if not fix_result["fixed"]:
                                    _append_log(conn, build_id, "testing",
                                                "Your website passed core checks with minor notes")
                                    break

                                # Re-run critical post-processors after qa_fix
                                try:
                                    from build import reapply_post_processors
                                    reapply_post_processors(build_dir, spec)
                                except Exception as pp_exc:
                                    logger.warning("Build %s: post-processor re-run after qa_fix failed: %s", build_id, pp_exc)

                                _append_log(conn, build_id, "testing", "Verifying improvements...")
                                # Re-run smoke tests first to guard against regression
                                smoke_recheck = test_module.test_app(build_dir)
                                if not smoke_recheck["success"]:
                                    # Smoke tests regressed: record as failed and restore
                                    try:
                                        from failure_kb import record_fix
                                        record_fix(
                                            fix_result.get("error_summary", fix_result["description"]),
                                            fix_result["description"],
                                            fix_result.get("files_changed", []),
                                            fix_category=fix_result.get("fix_category", "qa"),
                                            succeeded=False,
                                            app_name=app_name,
                                        )
                                    except Exception as kb_exc:
                                        logger.debug("Build %s: failure KB not updated: %s", build_id, kb_exc)
                                    _append_log(conn, build_id, "testing",
                                                "Reverting adjustment, re-checking quality...")
                                    if qa_snapshot_dir and Path(qa_snapshot_dir).is_dir():
                                        shutil.copytree(qa_snapshot_dir, build_dir, dirs_exist_ok=True)
                                        logger.info("Build %s: restored pre-qa-fix snapshot", build_id)
                                    # Re-run QA on the clean snapshot to get fresh failure context.
                                    # Without this, the next attempt_qa_fix() receives the same stale
                                    # qa_result["results"], producing an identical broken fix.
                                    qa_result = run_qa(build_dir, spec, tier=tier)
                                    if qa_result.get("success"):
                                        break  # snapshot code actually passes QA now
                                    continue  # next attempt gets fresh QA results

                                qa_recheck = run_qa(build_dir, spec, tier=tier)
                                qa_fix_succeeded = qa_recheck["success"]
                                # Record true QA fix outcome in KB
                                try:
                                    from failure_kb import record_fix
                                    record_fix(
                                        fix_result.get("error_summary", fix_result["description"]),
                                        fix_result["description"],
                                        fix_result.get("files_changed", []),
                                        fix_category=fix_result.get("fix_category", "qa"),
                                        succeeded=qa_fix_succeeded,
                                        app_name=app_name,
                                    )
                                except Exception as kb_exc:
                                    logger.debug("Build %s: failure KB not updated: %s", build_id, kb_exc)

                                qa_result = qa_recheck
                                if qa_fix_succeeded:
                                    _append_log(conn, build_id, "testing", "QA passed after fix")
                                    break
                                elif qa_fix_attempt < max_qa_fixes - 1:
                                    remaining = qa_recheck.get("failed", 0)
                                    logger.info("Build %s: %d QA issue(s) remain, attempt %d/%d",
                                                build_id, remaining, qa_fix_attempt + 2, max_qa_fixes)
                                    _append_log(conn, build_id, "testing",
                                                "Applying additional refinements...")
                                else:
                                    _append_log(conn, build_id, "testing",
                                                "Your website passed core checks with minor notes")

                        except FixLoopBudgetExceeded as qa_fix_budget_exc:
                            # A qa_fix budget overrun FAILS the build instead of
                            # shipping unfixed QA: a fix loop that burned its
                            # budget on a hung API call has not landed its fix,
                            # and shipping known-broken code is worse than
                            # failing and retrying cleanly. Roll back with
                            # BLOCK_ON_QA_FIX_BUDGET=false.
                            #
                            # Always kill orphan Node/Chromium proc groups so
                            # the worker frees resources before exiting.
                            try:
                                from qa_fix import kill_proc_group, _last_qa_proc
                                kill_proc_group(_last_qa_proc)
                            except Exception as _kpg_exc:
                                logger.debug(
                                    "Build %s: kill_proc_group after qa_fix budget exceeded: %s",
                                    build_id, _kpg_exc,
                                )
                            if _block_gate_on("BLOCK_ON_QA_FIX_BUDGET"):
                                logger.error(
                                    "Build %s: qa_fix budget exceeded (%ds > %ds), FAILING build "
                                    "(set BLOCK_ON_QA_FIX_BUDGET=false to roll back)",
                                    build_id,
                                    int(qa_fix_budget_exc.elapsed_s),
                                    int(qa_fix_budget_exc.budget_s),
                                )
                                raise
                            logger.warning(
                                "Build %s: qa_fix budget exceeded (%ds > %ds), continuing to deploy "
                                "(BLOCK_ON_QA_FIX_BUDGET=false)",
                                build_id,
                                int(qa_fix_budget_exc.elapsed_s),
                                int(qa_fix_budget_exc.budget_s),
                            )
                            _append_log(conn, build_id, "testing",
                                        "Your website passed core checks with minor notes")
                        except BuildCancelled:
                            raise
                        except Exception as qa_fix_exc:
                            # An API call timeout or wall-clock overrun gets the same
                            # escalation as FixLoopBudgetExceeded above: the fix did
                            # not land. Detect it via lazy imports (api_client and
                            # anthropic are only needed on this path).
                            _is_api_timeout = False
                            try:
                                import anthropic
                                _is_api_timeout = isinstance(
                                    qa_fix_exc, anthropic.APITimeoutError,
                                )
                            except ImportError:
                                pass
                            try:
                                from api_client import CallApiWallClockExceeded
                                if isinstance(qa_fix_exc, CallApiWallClockExceeded):
                                    _is_api_timeout = True
                            except ImportError:
                                pass
                            if _is_api_timeout and _block_gate_on("BLOCK_ON_QA_FIX_BUDGET"):
                                try:
                                    from qa_fix import kill_proc_group, _last_qa_proc
                                    kill_proc_group(_last_qa_proc)
                                except Exception as kill_exc:
                                    logger.debug("Build %s: kill_proc_group after a qa_fix API timeout: %s",
                                                 build_id, kill_exc)
                                logger.error(
                                    "Build %s: qa_fix API call timeout/budget "
                                    "exceeded: FAILING build: %s",
                                    build_id, qa_fix_exc,
                                )
                                raise
                            logger.warning("Build %s: qa_fix error: %s", build_id, qa_fix_exc)
                            _append_log(conn, build_id, "testing",
                                        "Wrapping up quality checks...")

                    _store_qa_results(conn, build_id, qa_result)

                    # Blocking QA gate (env-var gated, default off).
                    # Raises QAQualityGateError so the exception handler keeps
                    # QA failures non-retryable (structural, not stochastic).
                    if _blocking_qa():
                        if not qa_result.get("success", True):
                            raise QAQualityGateError(
                                f"QA: {qa_result.get('failed', 0)} failures after fix attempts"
                            )

                except (QualityGateError, FixLoopBudgetExceeded, BuildCancelled):
                    # Re-raise so it reaches _process_job's except block. Fix loop
                    # budget violations are code errors, not non-fatal QA noise.
                    raise
                except Exception as qa_exc:
                    logger.warning("Build %s: QA stage error (non-fatal): %s", build_id, qa_exc)
                    _append_log(conn, build_id, "testing", "Wrapping up quality checks...")
                    _store_qa_results(conn, build_id, {
                        "skipped": True, "success": True, "passed": 0, "failed": 0,
                        "results": [], "qa_report": f"QA error: {qa_exc}",
                    })

            # Stage 3.75: UI Tests (non-blocking)
            # Checks visual quality score after QA, before deploy.
            try:
                _start_stage("ui_test")
                _append_log(conn, build_id, "testing", "Running visual quality checks...")
                from ui_test import run_ui_tests
                ui_result = run_ui_tests(str(build_dir), spec, cancel_key=cancel_key)

                _store_ui_test_results(conn, build_id, ui_result)

                ui_score = ui_result.get("score")
                if ui_score is not None:
                    _append_log(conn, build_id, "testing", f"Visual quality score: {ui_score}/10")
                else:
                    _append_log(conn, build_id, "testing", "Visual checks complete")

                _end_stage("ui_test")
            except Exception as ui_exc:
                logger.warning("Build %s: UI test error (non-fatal): %s", build_id, ui_exc)
                _store_ui_test_results(conn, build_id, {"skipped": True, "score": None})
                _end_stage("ui_test")

            _end_stage("testing")

        # Stage 3.9: Magic link token injection (before deploy)
        # Two tokens are generated: one for deploy verification
        # (verify_magic_link_auth signs in with it once, which uses it up) and
        # one for the customer to use for first login.
        verify_token = None
        customer_token = None
        if USE_MAGIC_LINK_AUTH:
            try:
                verify_pair = _generate_magic_link_token()
                customer_pair = _generate_magic_link_token()
                injected = _inject_magic_link_token(
                    build_dir,
                    verify_pair["token_hash"],
                    customer_token_hash=customer_pair["token_hash"],
                )
                if injected:
                    verify_token = verify_pair["raw_token"]
                    customer_token = customer_pair["raw_token"]
                    # Kept in the attempt state too, so the supervisor can put
                    # the login link in the customer's email if it announces a
                    # deploy that this thread finished while stuck.
                    attempt_state["login_token"] = customer_token
                    logger.info("Build %s: magic link tokens generated (verify + customer)", build_id)
                else:
                    # Never hand out a raw token whose hash is not in the seed:
                    # the link would read "invalid or expired" for the customer.
                    logger.warning("Build %s: magic link tokens not injected (no seed.sql); no login link", build_id)
                    _append_log(conn, build_id, "building", "Warning: login link could not be prepared")
            except Exception as ml_exc:
                logger.warning("Build %s: magic link token injection failed (non-fatal): %s", build_id, ml_exc)

        # Stage 4: deploy
        # LOCAL BUILD GUARD: if RAILWAY_API_TOKEN is absent or empty, Railway deploy
        # is impossible. Mark the build as preview (artifact is ready) rather than
        # failing and retrying, which wastes retries on a config gap, not a code bug.
        if not os.getenv("RAILWAY_API_TOKEN", "").strip():
            _start_stage("deploying")
            conn = _ensure_conn(conn)
            _append_log(conn, build_id, "deploying",
                        "Local build: Railway deploy skipped (no RAILWAY_API_TOKEN set)")
            logger.info("Build %s: no RAILWAY_API_TOKEN, skipping deploy, marking preview", build_id)
            # Same guarded write as the deployed path: the build goes (back)
            # into review. There is no deployed URL to look at, so
            # _process_job sends no "review needed" or "preview ready" email
            # for a local build.
            if not _set_preview_status(conn, build_id, preview_url=None, attempt=attempt):
                raise BuildNoLongerOwnedError(
                    "preview write matched no row; this thread no longer owns the build"
                )
            # A local build sends no email, and the login link is not written
            # to build_logs or to the process log either. The site run locally
            # issues one at /request-magic-link and, with no mail key, prints
            # it to its own console.
            if customer_token:
                _append_log(conn, build_id, "preview",
                            "Local build: no login link is recorded. Request one at /request-magic-link "
                            "on the running site; without a mail key the site prints it to its console.")
            return {"live_url": None, "local_preview": True}

        _start_stage("deploying")
        conn = _ensure_conn(conn)
        _advance_stage(conn, build_id, "deploying", attempt)
        logger.info("Build %s: deploying", build_id)
        send_build_progress(customer_email, customer_name, app_name, "deploying", build_id=build_id)
        _append_log(conn, build_id, "deploying", "Deploying to Railway")
        from deploy import deploy
        # A v2 app seeds its admin without a password (magic-link login only),
        # so deploy() must not run its password-login probe against it: the
        # probe posts the v1 default password to the app's /login, cannot
        # succeed, and would be reported as a failed health check.
        deploy_result = deploy(build_dir, password_login_probe=(_engine != "v2"))

        # deploy() returns a dict with live_url, the Railway ids and the health
        # verdict. Anything else carries no verdict, and no verdict is not a
        # pass.
        if not isinstance(deploy_result, dict):
            raise RuntimeError(
                "deploy() returned no deployment details; the deploy cannot be verified"
            )

        # From here on this attempt has a Railway project of its own. It is
        # kept in the attempt's state, not on the build row: the row's Railway
        # columns describe the deployment the customer was last told about and
        # change only when this build reaches preview (_set_preview_status).
        # If this attempt ends any other way, its project is deleted again
        # (_discard_unannounced_project), and only its project.
        _note_attempt_project(job, deploy_result)

        # deploy() can take many minutes. If the build left this attempt's
        # hands meanwhile (an admin kill, a supervisor that gave up and
        # requeued or failed it, a later attempt), stop before any more work
        # is done against it.
        conn = _ensure_conn(conn)
        _advance_stage(conn, build_id, "deploying", attempt)

        # Stage 5: post-deploy self-healing
        # If the app deployed but health check failed (crashes on startup),
        # try to diagnose and fix the code, then redeploy.
        #
        # `health` is "ok" only after a healthy /health response was seen: by
        # deploy() itself, by one of the re-probes below, or by a redeploy.
        # Every other value keeps the build out of preview. A deploy result
        # that reports nothing is treated as "failed".
        health = deploy_result.get("health_check") or "failed"

        # v2 recovery probe: deploy() reports a non-ok health_check in three
        # cases: "failed" without probing /health at all, when Railway reported
        # the deployment CRASHED or FAILED on two consecutive polls (see
        # wait_for_deployment, which notes that Railway can report CRASHED
        # during the first restart after the schema migration and then
        # settle); "failed" when /health answered but was not healthy within
        # verify_health's window; and "infra" when it never answered or its
        # body pointed at the database. For v2 any of these trips the
        # fail-fast gate below, so re-probe the live URL once with a 180s
        # budget. Only an explicit healthy response promotes the result to
        # "ok"; anything else keeps the original classification.
        if _engine == "v2" and health != "ok" and deploy_result.get("live_url"):
            _append_log(
                conn, build_id, "deploying",
                "Deploy reported not-ready; re-probing health (v2 apps often "
                "recover after an initial Railway restart cycle)...",
            )
            logger.info(
                "Build %s: v2 recovery probe (deploy_result.health_check=%s, "
                "re-verifying with 180s budget)",
                build_id, health,
            )
            try:
                from deploy import verify_health as _vh_recover
                recovered = _vh_recover(
                    deploy_result["live_url"], timeout=180,
                )
                if recovered == "ok":
                    logger.info(
                        "Build %s: v2 recovery probe confirmed healthy, "
                        "overriding deploy_result.health_check from %s to 'ok'",
                        build_id, health,
                    )
                    _append_log(
                        conn, build_id, "deploying",
                        "v2 recovery probe: app is responding healthy; "
                        "proceeding to preview",
                    )
                    health = "ok"
                else:
                    logger.info(
                        "Build %s: v2 recovery probe still %s; falling "
                        "through to original failure path",
                        build_id, recovered,
                    )
            except Exception as rec_exc:
                logger.warning(
                    "Build %s: v2 recovery probe error (non-blocking): %s",
                    build_id, rec_exc,
                )

        if health == "ok":
            # Verify all spec routes work (not just /health).
            #
            # v2 gate: the v2 intake translator rewrites customer-declared
            # `pages` into an archetype-specific page set and drops pages that
            # do not fit, so the original spec's page list is not the ground
            # truth for what v2 emitted (compose-manifest.txt is). Checking it
            # would report false 404s for every dropped page. The v2 lint in the
            # testing stage already validates route emission deterministically.
            if _engine == "v2":
                _append_log(conn, build_id, "deploying",
                            "Deploy successful (v2 lint validates route emission; skipping v1 spec-route check)")
                logger.info("Build %s: skipping verify_routes (v2 engine; v2 lint already asserted widget-composed page views)", build_id)
            else:
                try:
                    from deploy import verify_routes
                    route_result = verify_routes(deploy_result.get("live_url", ""), spec, timeout=60)
                    if route_result["status"] != "ok":
                        failing = route_result.get("failing_routes", [])
                        _append_log(conn, build_id, "deploying",
                                    "Optimizing your website for the best experience...")
                        logger.warning("Build %s: route verification failed: %s", build_id, failing[:5])
                        health = "failed"  # triggers deploy-fix loop below
                    else:
                        _append_log(conn, build_id, "deploying",
                                    f"Deploy successful, all {route_result['passed']} routes verified")
                except Exception as rv_exc:
                    logger.warning("Build %s: route verification error (non-blocking): %s", build_id, rv_exc)
                    _append_log(conn, build_id, "deploying", "Deploy successful, health check passed")

            # Verify magic link auth works on deployed app (non-blocking)
            # Uses the verify_token (which gets consumed), not the customer_token
            if health == "ok" and verify_token:
                try:
                    from deploy import verify_magic_link_auth
                    ml_result = verify_magic_link_auth(
                        deploy_result.get("live_url", ""), verify_token, timeout=20)
                    if ml_result == "ok":
                        _append_log(conn, build_id, "deploying",
                                    "Login link verified")
                    elif ml_result == "failed":
                        logger.warning("Build %s: magic link verification failed", build_id)
                        _append_log(conn, build_id, "deploying",
                                    "Warning: login link verification failed")
                except Exception as ml_exc:
                    logger.warning("Build %s: magic link check error: %s", build_id, ml_exc)

            # Verify password-based auth works (non-blocking warning in magic link mode).
            #
            # v2 gate: v2 seeds the admin with no password hash (magic-link
            # only), so verify_auth would always warn: log noise plus ~20s of
            # latency per build. v1 still needs it (Claude-generated apps can
            # have genuine password-login regressions).
            if health == "ok" and _engine != "v2":
                try:
                    from deploy import verify_auth
                    auth_result = verify_auth(deploy_result.get("live_url", ""), spec, timeout=20)
                    if auth_result == "ok":
                        _append_log(conn, build_id, "deploying", "Admin login verified")
                    elif auth_result == "warning":
                        logger.warning("Build %s: password auth check failed (magic link mode, non-blocking)", build_id)
                        _append_log(conn, build_id, "deploying", "Warning: password login unavailable (magic link mode)")
                    elif auth_result == "failed":
                        logger.warning("Build %s: auth verification failed", build_id)
                        _append_log(conn, build_id, "deploying", "Warning: admin login verification failed")
                except Exception as auth_exc:
                    logger.warning("Build %s: auth check error: %s", build_id, auth_exc)

            # Verify seed data is present on CRUD pages (non-blocking).
            #
            # v2 gate: verify_data derives CRUD paths from spec.data_models, but
            # the v2 translator remaps models to archetype tables, so it would
            # produce false "missing data" warnings; the v2 lint already asserts
            # seed row counts upstream.
            if health == "ok" and _engine != "v2":
                try:
                    from deploy import verify_data
                    data_result = verify_data(deploy_result.get("live_url", ""), spec, timeout=15)
                    if data_result["status"] == "warning":
                        logger.warning("Build %s: data verification warning: %s", build_id, data_result["details"])
                        _append_log(conn, build_id, "deploying",
                                    "Warning: some pages appear to have missing data")
                except Exception as dv_exc:
                    logger.warning("Build %s: data verification error: %s", build_id, dv_exc)

            # Capture live deploy screenshots
            try:
                from deploy import capture_deploy_screenshots
                live_url_for_ss = deploy_result.get("live_url", "")
                if live_url_for_ss:
                    deploy_ss = capture_deploy_screenshots(live_url_for_ss, spec, build_dir)
                    if deploy_ss:
                        _append_log(conn, build_id, "deploying", f"Captured {len(deploy_ss)} live screenshot(s)")
                        _store_screenshots(conn, build_id, deploy_ss)
            except Exception as ss_exc:
                logger.warning("Build %s: deploy screenshots failed: %s", build_id, ss_exc)

            # Rendered-screenshot verification gate.
            #
            # The HTTP-only verify_routes / verify_auth / verify_data checks
            # cannot see what the customer sees: cream-on-cream hero text,
            # broken layout, hardcoded contact info from a template are all
            # invisible to a 200-status-code grep. verify_render loads the
            # homepage + one internal page in a real browser, takes full-page
            # screenshots, and stores them on builds.screenshots. Failures
            # are downgraded to warnings (the build still reaches preview);
            # the screenshots themselves are the gate that the senior-engineer
            # reviewer enforces at /admin/reviews.
            try:
                from deploy import verify_render
                live_url_for_render = deploy_result.get("live_url", "")
                if live_url_for_render:
                    render_result = verify_render(live_url_for_render, spec, build_dir, timeout=60)
                    render_screens = render_result.get("screenshots", []) if isinstance(render_result, dict) else []
                    if render_screens:
                        _store_screenshots(conn, build_id, render_screens)
                    if isinstance(render_result, dict) and render_result.get("status") != "ok":
                        details = render_result.get("details", "")
                        logger.warning(
                            "Build %s: render verification warning: %s",
                            build_id,
                            details or "no detail",
                        )
                        _append_log(
                            conn, build_id, "deploying",
                            "Warning: rendered-screenshot gate degraded "
                            "(captured %d screenshot(s); review at /admin/reviews)" % len(render_screens),
                        )
                    else:
                        _append_log(
                            conn, build_id, "deploying",
                            "Captured %d rendered screenshot(s) for review" % len(render_screens),
                        )
            except Exception as rv_exc:
                logger.warning("Build %s: render verification error: %s", build_id, rv_exc)
        # Infrastructure issue (DB maintenance, connection timeout): wait and retry
        # health check instead of wasting Claude API calls on a non-code problem.
        if health == "infra" and deploy_result.get("live_url"):
            _append_log(conn, build_id, "deploying",
                        "Infrastructure issue detected (DB may be under maintenance), polling for recovery")
            logger.info("Build %s: infra issue detected, polling for recovery", build_id)
            import time as _time
            from deploy import verify_health
            for _infra_poll in range(6):
                health = verify_health(deploy_result["live_url"], timeout=90)
                if health == "ok":
                    break
                _time.sleep(30)
            if health == "ok":
                _append_log(conn, build_id, "deploying", "Health check passed after infra wait")

        # Still "infra": the app never answered, or it answers but its database
        # is unreachable. That is not a code bug, so no fix loop runs (on
        # either engine), and it is not a working site, so there is no preview:
        # the attempt ends as a transient failure and the build is retried.
        if health == "infra":
            _append_log(conn, build_id, "deploying",
                        "The deployed site did not come up healthy; retrying the deploy")
            logger.warning(
                "Build %s: deployed app never answered healthy (still 'infra' after the wait); "
                "ending the attempt as a transient failure",
                build_id,
            )
            raise RuntimeError(_DEPLOY_NEVER_HEALTHY.format(last="infra"))

        if health != "ok" and deploy_result.get("railway_project_id"):
            max_deploy_fixes = 3
            project_id = deploy_result["railway_project_id"]
            environment_id = deploy_result["railway_environment_id"]
            service_id = deploy_result["railway_service_id"]
            live_url = deploy_result.get("live_url", "")

            # v2 gate: v2 output is deterministic and deploy_fix is a v1-era
            # Claude loop that patches files under v1 assumptions (for example it
            # expects views/layout.ejs where v2 emits views/layout-public.ejs).
            # Running it on v2 artifacts corrupts them and drops routes v2 had
            # emitted correctly. The right response to a v2 deploy crash is to
            # fail fast so the signal feeds back into the compose engine, not a
            # runtime patch that masks the bug. Persist the deploy logs once
            # before raising so the post-mortem works even if the Railway
            # project is cleaned up.
            if _engine == "v2":
                logger.info(
                    "Build %s: deploy crashed but skipping deploy_fix "
                    "(CAS_BUILD_ENGINE=v2; fix belongs in compose/, not a runtime patch)",
                    build_id,
                )
                _append_log(
                    conn, build_id, "deploying",
                    "Deploy crashed. Surfacing crash logs for diagnosis "
                    "(v2 engine is deterministic; no AI patching).",
                )
                # None until a log has been read: a log that could not be read
                # is not an empty log, and _v2_deploy_failure treats the two
                # alike only as "nothing shows the app to be at fault".
                _v2_dep_logs = None
                try:
                    from deploy import get_latest_deployment, get_deployment_logs
                    _v2_dep_id = get_latest_deployment(service_id, environment_id, None)
                    # Ask Railway for up to 500 lines. Railway needs a few
                    # seconds to flush post-crash stderr, so wait 8s before the
                    # first fetch and refetch once if the result is thin;
                    # fetching immediately tends to miss the actual error line.
                    if _v2_dep_id:
                        time.sleep(8)
                        _v2_dep_logs = get_deployment_logs(
                            _v2_dep_id, None, limit=500, raise_on_error=True)
                        if len(_v2_dep_logs) < 30:
                            time.sleep(7)
                            try:
                                _retry_logs = get_deployment_logs(
                                    _v2_dep_id, None, limit=500, raise_on_error=True)
                            except Exception as _retry_exc:
                                logger.warning(
                                    "Build %s: second read of the crash log failed, keeping the "
                                    "first (%s)", build_id, _retry_exc)
                                _retry_logs = []
                            if len(_retry_logs) > len(_v2_dep_logs):
                                _v2_dep_logs = _retry_logs
                except Exception as _v2_log_exc:
                    logger.warning(
                        "Build %s: the v2 deploy-crash log could not be read (non-blocking): %s",
                        build_id,
                        _v2_log_exc,
                    )
                _persist_deploy_logs(
                    conn,
                    build_id,
                    _v2_dep_logs,
                    reason="v2-deploy-crash-no-fix-loop",
                    max_lines=500,
                )
                raise _v2_deploy_failure(deploy_result, _v2_dep_logs, app_name)

            # Before attempting code fixes, check Railway deploy logs for DB connectivity
            # issues. If the app crashed solely because Railway's DB wasn't ready, waiting
            # is the right response, not asking Claude to fix perfectly good code.
            _db_never_recovered = False
            try:
                from deploy import get_latest_deployment, get_deployment_logs
                _pre_dep_id = get_latest_deployment(service_id, environment_id, None)
                _pre_dep_logs = get_deployment_logs(_pre_dep_id, None, limit=100) if _pre_dep_id else []
                # Persist raw logs to build_logs so DEPLOY_CRASH1 builds are
                # debuggable from the DB alone (Railway project may be torn
                # down by auto-cleanup before we can re-query).
                _persist_deploy_logs(
                    conn, build_id, _pre_dep_logs, reason="health-check-failed",
                )
                if _logs_show_database_unreachable(_pre_dep_logs):
                    logger.info(
                        "Build %s: deploy crash is DB connectivity issue (not code bug), polling for recovery",
                        build_id,
                    )
                    _append_log(conn, build_id, "deploying",
                                "Starting up, waiting for the database to become ready...")
                    import time as _time
                    from deploy import verify_health as _vh
                    for _poll in range(6):
                        _post_wait_health = _vh(live_url, timeout=90)
                        if _post_wait_health == "ok":
                            break
                        _time.sleep(30)
                    if _post_wait_health == "ok":
                        _append_log(conn, build_id, "deploying",
                                    "Database ready, continuing deployment")
                        health = "ok"
                        logger.info("Build %s: DB recovered after wait, skipping deploy fix", build_id)
                    elif _post_wait_health == "infra":
                        # Still no healthy answer and still not a code problem:
                        # asking the model to patch working code would be
                        # wrong, and so would a preview.
                        _db_never_recovered = True
                        logger.warning(
                            "Build %s: database still unreachable after the wait; "
                            "ending the attempt as a transient failure",
                            build_id,
                        )
                    else:
                        logger.info(
                            "Build %s: DB still down after wait (%s), proceeding to deploy fix",
                            build_id, _post_wait_health,
                        )
            except Exception as _pre_check_exc:
                logger.warning(
                    "Build %s: pre-deploy-fix log check failed (non-blocking): %s",
                    build_id, _pre_check_exc,
                )
            if _db_never_recovered:
                _append_log(conn, build_id, "deploying",
                            "The deployed site did not come up healthy; retrying the deploy")
                raise RuntimeError(_DEPLOY_NEVER_HEALTHY.format(last="infra"))

        # Enter deploy-fix loop only if health is still not ok after the DB-crash wait above.
        if health != "ok" and deploy_result.get("railway_project_id"):
            # Early exit: skip fix loop if failure_kb shows 0% success rate for this error
            # pattern (5+ recent attempts). Saves ~$0.70/build in wasted Opus API calls.
            try:
                from failure_kb import (
                    normalize_error,
                    _effective_rate,
                    _load_kb,
                    extract_deploy_crash_signature,
                    is_generic_crash_signature,
                )
                _kb_log_sample = locals().get("_pre_dep_logs", [])
                # Prefer the structured-error extractor over joining the last N
                # log lines: generic signatures (for example `deploy-crash;
                # CRASHED`) bucket distinct causes into one poisoned entry. The
                # extractor prefers lines that look like real errors.
                _kb_err_raw = extract_deploy_crash_signature(_kb_log_sample)
                _kb_sig = normalize_error(_kb_err_raw)
                if _kb_sig:
                    _kb_data = _load_kb()
                    _kb_entry = _kb_data.get(_kb_sig)
                    if _kb_entry:
                        _kb_rate, _kb_total = _effective_rate(_kb_entry)
                        if _kb_total >= 5 and _kb_rate == 0.0:
                            # Only trust the 0% history for specific signatures.
                            # Generic ones (CRASHED, FAILED, crash on startup)
                            # bucket distinct crashes, so run the fix loop
                            # normally for those.
                            if is_generic_crash_signature(_kb_sig):
                                logger.info(
                                    "Build %s: KB early-exit suppressed for generic signature "
                                    "(%d historical attempts in poisoned bucket): %.80s",
                                    build_id, _kb_total, _kb_sig,
                                )
                            else:
                                logger.info(
                                    "Build %s: skipping deploy_fix, failure_kb shows 0%% success rate "
                                    "(%d recent attempts) for signature: %.80s",
                                    build_id, _kb_total, _kb_sig,
                                )
                                _append_log(conn, build_id, "deploying",
                                            "Automatic repair is skipped for this deploy failure")
                                max_deploy_fixes = 0
            except Exception as _kb_early_exc:
                logger.warning(
                    "Build %s: KB early-exit check failed (non-blocking): %s",
                    build_id, _kb_early_exc,
                )

            _snapshot_build_dir(build_dir, "pre-deploy-fix")
            _append_log(conn, build_id, "deploying", "Fine-tuning your website's configuration...")
            # WATCHDOG1 Layer 2: per-loop wall-clock budget.
            _deploy_fix_budget_start = time.monotonic()
            _deploy_fix_budget = _FIX_LOOP_BUDGETS_S["deploy_fix"]
            for deploy_fix_attempt in range(max_deploy_fixes):
                _deploy_fix_elapsed = time.monotonic() - _deploy_fix_budget_start
                if _deploy_fix_elapsed >= _deploy_fix_budget:
                    _append_log(
                        conn, build_id, "deploying",
                        "One of our fix steps took longer than expected. Retrying with a fresh attempt.",
                    )
                    raise FixLoopBudgetExceeded(
                        "deploy_fix", _deploy_fix_elapsed, _deploy_fix_budget,
                    )
                logger.info(
                    "Build %s: app crashed post-deploy, attempting deploy fix (%d/%d)",
                    build_id, deploy_fix_attempt + 1, max_deploy_fixes,
                )

                try:
                    # Get deployment logs for diagnosis
                    from deploy import get_latest_deployment, get_deployment_logs, redeploy
                    dep_id = get_latest_deployment(service_id, environment_id, None)
                    dep_logs = get_deployment_logs(dep_id, None, limit=100) if dep_id else []
                    # Persist per-iteration logs so we can reconstruct the
                    # full crash → fix → redeploy → crash trajectory.
                    _persist_deploy_logs(
                        conn,
                        build_id,
                        dep_logs,
                        reason=f"deploy-fix-iteration-{deploy_fix_attempt + 1}",
                    )

                    from deploy_fix import attempt_deploy_fix
                    fix_result = attempt_deploy_fix(
                        build_dir=build_dir,
                        deploy_logs=dep_logs,
                        health_status=health,
                        spec=spec,
                        cancel_key=cancel_key,
                    )

                    if not fix_result["fixed"]:
                        logger.info("Build %s: no deploy fixes produced, stopping", build_id)
                        break

                    # Re-run critical post-processors after deploy_fix
                    try:
                        from build import reapply_post_processors
                        reapply_post_processors(build_dir, spec)
                    except Exception as pp_exc:
                        logger.warning("Build %s: post-processor re-run after deploy_fix failed: %s", build_id, pp_exc)

                    logger.info(
                        "Build %s: deploy fix applied (%s), redeploying",
                        build_id, fix_result["description"],
                    )

                    redeploy_result = redeploy(
                        build_dir, project_id, environment_id, service_id, live_url
                    )

                    if redeploy_result.get("success"):
                        # Also verify all routes work after fix
                        routes_ok = True
                        try:
                            from deploy import verify_routes
                            rv = verify_routes(live_url, spec, timeout=45)
                            if rv["status"] != "ok":
                                logger.warning(
                                    "Build %s: deploy-fix health OK but routes broken: %s",
                                    build_id, rv.get("failing_routes", [])[:3],
                                )
                                routes_ok = False
                                health = "failed"
                        except Exception as _guard_exc:  # route verify failure is non-blocking after fix
                            logger.warning("route check after the deploy fix did not complete: %s: %s", type(_guard_exc).__name__, _guard_exc)

                        # Record deploy-fix outcome
                        fix_succeeded = routes_ok
                    else:
                        fix_succeeded = False
                        health = "failed"

                    try:
                        from failure_kb import record_fix
                        record_fix(
                            fix_result.get("error_summary", fix_result["description"]),
                            fix_result["description"],
                            fix_result.get("files_changed", []),
                            fix_category=fix_result.get("fix_category", "deploy"),
                            succeeded=fix_succeeded,
                            app_name=app_name,
                        )
                    except Exception as kb_exc:
                        logger.debug("Build %s: failure KB not updated: %s", build_id, kb_exc)

                    if redeploy_result.get("success") and routes_ok:
                        logger.info(
                            "Build %s: post-deploy self-healing succeeded on attempt %d",
                            build_id, deploy_fix_attempt + 1,
                        )
                        health = "ok"
                        break
                    elif redeploy_result.get("success") and not routes_ok:
                        continue  # try next fix attempt

                except (FixLoopBudgetExceeded, BuildCancelled):
                    # Budget violation is a code-error; propagate to _process_job
                    # instead of logging and continuing silently.
                    raise
                except Exception as deploy_fix_exc:
                    logger.error(
                        "Build %s: post-deploy fix failed: %s", build_id, deploy_fix_exc
                    )
                    break

            if health != "ok":
                raise RuntimeError(
                    f"App deployed but crashes on startup after {max_deploy_fixes} fix attempt(s). "
                    f"Check Railway deployment logs for {app_name}."
                )

        # Nothing above may hand a deployment to _process_job (and from there to
        # the reviewer and the customer) without a healthy response on record.
        # Every branch that ends unhealthy raises on its own; this catches the
        # one that would otherwise fall through (a deploy result that names no
        # Railway project to repair).
        if health != "ok":
            raise RuntimeError(_DEPLOY_NEVER_HEALTHY.format(last=health))

        _end_stage("deploying")
        # Keep build artifact for customer source code download (not clearing after deploy)

        # Save stage timings to DB
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE builds SET stage_timings = %s WHERE id = %s",
                    (json.dumps(stage_timings), build_id),
                )
            conn.commit()
            logger.info("Build %s: stage timings saved: %s", build_id,
                        {k: v.get("duration_s", "?") for k, v in stage_timings.items()})
        except Exception as st_exc:
            logger.warning("Build %s: failed to save stage timings: %s", build_id, st_exc)

        if customer_token:
            deploy_result["magic_link_raw_token"] = customer_token
        return deploy_result

    finally:
        # Drain thread-local metrics into the shared list BEFORE this thread exits.
        # get_collected_metrics() reads threading.local(), which is invisible to the
        # calling thread in _process_job, so we must export here, inside the pipeline
        # thread, while the thread-local data is still accessible.
        if _metrics_out is not None:
            try:
                _metrics_out.extend(get_collected_metrics())
            except Exception as metrics_exc:
                logger.debug("Build %s: could not export API metrics: %s", build_id, metrics_exc)
        # If the supervisor stopped waiting for this thread it decided the
        # build's fate without it, and nobody will look at what this thread
        # returns or raises. A Railway project it created that no build row
        # records (the supervisor's probe did not adopt and announce it) is
        # then this thread's to remove, on a connection of its own: the one
        # this thread started with was closed when _process_job returned.
        # `thread_ended` and `abandoned` change together under `handoff`, so a
        # supervisor that gives up just after this look sees the thread ended
        # and does the clean-up itself (see _process_job).
        with attempt_state["handoff"]:
            attempt_state["thread_ended"] = True
            was_abandoned = attempt_state["abandoned"]
        if was_abandoned:
            try:
                _discard_after_abandonment(
                    job, "its build thread finished after the supervisor gave up on it")
            except Exception as discard_exc:
                logger.warning("Build %s: clean-up after an abandoned attempt failed: %s",
                               build_id, discard_exc)
        try:
            Path(spec_path).unlink(missing_ok=True)
        except Exception as cleanup_exc:
            logger.warning("Build %s: failed to delete temp file %s: %s", build_id, spec_path, cleanup_exc)


# ---------------------------------------------------------------------------
# Per-job processor
# ---------------------------------------------------------------------------

# Cooperative cancellation is per attempt, not per build. The supervisor may
# requeue a build while the thread of the attempt it gave up on is still
# running, and the next attempt can start before that thread ends. With one
# event per build id the new attempt would either inherit a fired event or
# replace it and so un-cancel the old thread. Each attempt therefore registers
# its own key, which begins with the build id so that log lines stay greppable.
def _new_cancel_key(build_id: str) -> str:
    return f"{build_id}#{secrets.token_hex(4)}"


# The retry time budget. A build is not retried once this much time has passed
# since the first attempt of its current series of retries. The series starts
# at a claim with retry_count 0 (a new build, or one a person requeued) and
# its start is remembered in this process only: after a worker restart, or
# when another worker claims the retry, the clock starts again at that claim.
# The hard bound on retries is _MAX_RETRIES; this budget only ends a series
# early. (The age of the build ROW is no measure of it: a row is created when
# the intake conversation starts, often days before it is first queued, and a
# manual retry reuses a row of any age.)
_RETRY_TIME_BUDGET_S = 2 * 60 * 60
_RETRY_SERIES_STARTED: dict = {}
_RETRY_SERIES_LOCK = threading.Lock()


def _note_attempt_start(build_id: str, retry_count: int) -> None:
    """Start the retry clock at the first attempt of a series (or the first this process sees)."""
    with _RETRY_SERIES_LOCK:
        if retry_count == 0 or build_id not in _RETRY_SERIES_STARTED:
            _RETRY_SERIES_STARTED[build_id] = time.monotonic()


def _retry_series_age_s(build_id: str) -> float:
    """Seconds since the build's current retry series started in this process."""
    with _RETRY_SERIES_LOCK:
        started = _RETRY_SERIES_STARTED.get(build_id)
    return 0.0 if started is None else time.monotonic() - started


def _end_retry_series(build_id: str) -> None:
    with _RETRY_SERIES_LOCK:
        _RETRY_SERIES_STARTED.pop(build_id, None)


def _announce_preview(conn, job: dict, live_url: str, railway: Optional[dict],
                      login_token: Optional[str]) -> None:
    """Tell everyone that a build arrived at preview. Never raises.

    Call it only after _set_preview_status returned True for this attempt: if
    that write matched no row, the build left the worker's hands (an admin
    kill, a failure or a preview written by someone else) and announcing a
    preview for it would be false.

    In order: monitor registration (health checks and daily backups start),
    the "ready for review" log line, the admin's "review needed" email (once
    per arrival at preview, so again after every rebuild; not sent when
    CAS_HARBOR_AUTO_REVIEW=true, see _initial_review_status), the janitor, a
    build_logs row saying where the login link is (the link itself goes into
    the customer's "preview ready" email only) and that email. Every step is
    best effort: the build is already in preview and nothing here may send it
    down the retry or failure path.

    The normal path and the supervisor's zombie recovery both end here, so a
    deploy that a stuck thread finished is announced like any other.
    """
    build_id = job["id"]
    app_name = job["app_name"]
    tier = job["tier"]
    customer_email = job["customer_email"]
    customer_name = job["customer_name"]
    admin_email = os.environ.get("ADMIN_EMAIL", "")
    try:
        if railway and railway.get("project_id"):
            if railway.get("service_id") and railway.get("environment_id"):
                try:
                    from monitor.registry import register_app
                    register_app(
                        app_name=app_name,
                        live_url=live_url,
                        railway_project_id=railway["project_id"],
                        railway_service_id=railway["service_id"],
                        railway_environment_id=railway["environment_id"],
                    )
                    logger.info("Build %s: registered for monitoring", build_id)
                except Exception as reg_exc:
                    logger.error("Build %s: failed to register for monitoring: %s", build_id, reg_exc)
            else:
                logger.error(
                    "Build %s: NOT registered for monitoring (service or environment id of "
                    "Railway project %s unknown): no health checks and no daily backups "
                    "for this preview until it is registered by hand",
                    build_id, railway["project_id"],
                )

        _append_log(conn, build_id, "preview", "Your website is ready for review")
        logger.info("Build %s is ready for preview at %s", build_id, live_url)

        if _auto_review_enabled():
            logger.info("Build %s: auto-review on; skipping admin review_pending email", build_id)
        else:
            try:
                send_review_pending(
                    build_id=build_id,
                    app_name=app_name,
                    customer_email=customer_email,
                    tier=tier,
                    preview_url=live_url,
                )
            except Exception as mail_exc:
                logger.warning(
                    "Build %s: send_review_pending failed (non-blocking): %s",
                    build_id,
                    mail_exc,
                )

        # Delete old failed duplicates of this app (rows only; see _cleanup_old_builds).
        # On its own guard: a database that cannot be reached here must not
        # cost the customer the email below.
        try:
            conn = _ensure_conn(conn)
            _cleanup_old_builds(conn, build_id, app_name)
        except Exception as janitor_exc:
            logger.warning("Build %s: clean-up of old builds skipped: %s", build_id, janitor_exc)

        # The login link is a credential (opening it signs in as the site's
        # admin), so it goes into the customer's email and nowhere else: not
        # into build_logs, which the owner's log API and the admin pages
        # show, and not into email_log, where the mailer stores the body
        # with its token redacted.
        magic_link_url = ""
        if login_token and live_url:
            magic_link_url = _build_magic_link_url(live_url, login_token)
            _append_log(conn, build_id, "preview", LOGIN_LINK_EMAILED_LOG_LINE)
            logger.info("Build %s: login link generated for the customer email", build_id)

        try:
            send_preview_ready(customer_email, customer_name, app_name, live_url, build_id, admin_email, tier, magic_link_url=magic_link_url)
        except Exception as mail_exc:
            logger.warning(
                "Build %s: send_preview_ready failed (non-blocking): %s", build_id, mail_exc,
            )
    except Exception as announce_exc:
        logger.error(
            "Build %s: announcing the preview stopped part way (the build stays in preview): %s",
            build_id, announce_exc,
        )


def _after_supervisor_decision(job: dict, verdict: str, committed: bool, probe_result,
                               pipeline_done: bool) -> None:
    """What _process_job still owes a build after the supervisor decided its fate.

    Runs on a fresh connection: the one the job started with belongs to the
    build thread, which may still be using it. If no connection can be opened
    the follow-up still runs without one: the emails do not need the database,
    the steps that do (build_logs rows, the janitor, the environment id) log
    that they were skipped, and no project is deleted, because the check that
    must precede a delete cannot run.

    * "preview" (the probe found the deploy the stuck thread finished, and the
      preview write landed): the build is announced exactly as on the normal
      path, including monitor registration and the customer's email. If the
      attempt's own record of its project matches the probed project, that
      record (it has the environment id and the login token) is used; if the
      thread is still inside deploy(), the environment id is looked up, and a
      registration that cannot be completed is logged as such.
    * Anything else: if the build thread has already ended, the project it may
      have created is discarded here. If it is still running, it does that
      itself when it ends (the attempt is marked abandoned).
    """
    build_id = job["id"]
    state = _attempt_state(job)
    fresh_conn = None
    try:
        try:
            fresh_conn = _get_conn(retries=3, base_delay=1.0)
        except Exception as conn_exc:
            logger.warning(
                "Build %s: no database connection for the follow-up after the supervisor's "
                "decision (%s); continuing without one",
                build_id, conn_exc,
            )
        if committed and verdict == "preview" and probe_result and probe_result.get("live_url"):
            live_url = probe_result["live_url"]
            own = state.get("railway")
            if own and own.get("project_id") == probe_result.get("project_id"):
                railway = dict(own)
            else:
                railway = _railway_record(
                    probe_result.get("project_id"), probe_result.get("service_id"),
                    live_url=live_url,
                )
                railway["environment_id"] = _lookup_environment_id(build_id, railway["project_id"])
            _mark_attempt_project_announced(job, railway)
            if railway.get("environment_id"):
                _record_environment_id(fresh_conn, build_id, railway)
            # The login token was seeded by this attempt. It opens the probed
            # deployment unless the attempt is known to have created a
            # different project than the one the probe found.
            login_token = state.get("login_token")
            if own and own.get("project_id") != probe_result.get("project_id"):
                login_token = None
            _announce_preview(fresh_conn, job, live_url, railway, login_token)
        elif pipeline_done:
            _discard_unannounced_project(
                fresh_conn, job, "the supervisor gave up on the attempt and its thread has ended")
    except Exception as exc:
        logger.error("Build %s: follow-up after the supervisor's decision failed: %s", build_id, exc)
    finally:
        if fresh_conn is not None:
            try:
                fresh_conn.close()
            except Exception as _guard_exc:  # closing a connection that is already gone
                logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)


def _lookup_environment_id(build_id: str, project_id) -> Optional[str]:
    """Ask Railway for a project's environment id (the probe does not return it)."""
    if not project_id or not os.getenv("RAILWAY_API_TOKEN", "").strip():
        return None
    try:
        from deploy import get_environment_id
        return get_environment_id(project_id, None)
    except Exception as exc:
        logger.warning("Build %s: could not read the environment id of Railway project %s: %s",
                       build_id, project_id, exc)
        return None


def _record_environment_id(conn, build_id: str, railway: dict) -> None:
    """Add an environment id learned after the preview write to the row that names its project."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE builds SET railway_environment_id = %s "
                "WHERE id = %s AND railway_project_id = %s AND railway_environment_id IS NULL",
                (railway["environment_id"], build_id, railway["project_id"]),
            )
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception as _guard_exc:  # rollback guard: the connection is already unusable
            logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)
        logger.warning("Build %s: could not record the Railway environment id: %s", build_id, exc)


def _process_job(job: dict, conn) -> None:
    """Process a single claimed job and record its outcome on the build.

    Raises ValueError for a job without an attempt token, before any work is
    done; nothing else is expected to escape (the caller, _run_build_in_thread,
    turns whatever does into a terminal failure of the build).
    """
    build_id = job["id"]
    app_name = job["app_name"]
    tier = job["tier"]
    retry_count = job.get("retry_count", 0)
    customer_email = job["customer_email"]
    customer_name = job["customer_name"]
    # Attempt token from the claim; guards every status write for this job.
    # A job without one is refused here, before anything runs.
    attempt = job.get("attempt")
    _attempt_param(attempt)
    attempt_state = _attempt_state(job)
    # Unset ADMIN_EMAIL means "no admin": mailer skips the admin notification and
    # the BCC rather than falling back to a hard-coded address.
    admin_email = os.environ.get("ADMIN_EMAIL", "")

    # app_name is database input and becomes a directory name that the worker
    # creates and deletes. Refuse anything that is not a plain single name
    # before any path is built from it.
    if not _is_safe_dir_name(app_name):
        logger.error("Build %s: refusing unsafe app_name %r; failing the build", build_id, app_name)
        _fail_build(
            conn, build_id,
            "Invalid app_name: expected letters, digits, dots, hyphens or underscores",
            customer_email, customer_name, "", admin_email, retry_count=retry_count,
            attempt=attempt,
        )
        return

    # Pre-compute build_dir so the except block can always clean up node_modules.
    build_dir = str(
        (_BUILDS_ROOT / f"{app_name}-{build_id[:8]}").resolve()
    )

    logger.info(
        "Processing build %s (app=%s, tier=%s, customer=%s, retry=%d)",
        build_id, app_name, tier, customer_email, retry_count,
    )

    _note_attempt_start(build_id, retry_count)

    # Cooperative cancellation. Register this attempt's cancel event BEFORE
    # submitting the pipeline and hand its key to the pipeline in the job, so
    # the fix loops and the model client inside _run_pipeline can poll it at
    # their checkpoints. The supervisor fires it before its DB write. The
    # event is removed when the pipeline thread ends, not when this function
    # returns: a thread the supervisor gave up on is exactly the one that
    # still has to see it.
    cancel_key = _new_cancel_key(build_id)
    job["cancel_key"] = cancel_key
    try:
        cli_cancellation.register(cancel_key)
    except Exception as cancel_exc:
        logger.warning(
            "Build %s: cancellation registry register failed: %s",
            build_id, cancel_exc,
        )

    def _drop_cancel_event(_future) -> None:
        try:
            cli_cancellation.clear(cancel_key)
        except Exception as cancel_exc:
            logger.warning(
                "Build %s: cancellation registry clear failed: %s",
                build_id, cancel_exc,
            )

    timeout_minutes = _TIER_TIMEOUTS.get(tier, 25)

    # The outer supervisor deadline (tier timeout + grace) is the sole timeout
    # mechanism, paired with cooperative cancellation via cli_cancellation.fire()
    # and the per-fix-loop wall-clock budgets (_FIX_LOOP_BUDGETS_S). A thread
    # blocked in a C-level syscall cannot be interrupted from Python; it is
    # abandoned as a daemon-like "zombie" and the supervisor decides the fate
    # of the build row without it.
    supervisor_deadline_s = timeout_minutes * 60 + _SUPERVISOR_GRACE_SECONDS
    pipeline_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix=f"build-{build_id[:8]}"
    )
    _pipeline_metrics: list = []  # populated by pipeline thread via _metrics_out
    pipeline_future = pipeline_executor.submit(_run_pipeline, job, conn, _pipeline_metrics)
    pipeline_future.add_done_callback(_drop_cancel_event)
    supervisor_timed_out = False
    # True when the build went back to the queue: its retry clock keeps running.
    requeued = False

    try:
        # Wait for the pipeline until the supervisor deadline, then ASK the
        # future whether it finished. The verdict is not taken from the class
        # of an exception: from Python 3.11 concurrent.futures.TimeoutError
        # IS the builtin TimeoutError, so `except TimeoutError` around
        # future.result(timeout=...) also caught a TimeoutError the pipeline
        # itself raised (a socket read, say) and treated that failed attempt
        # as a hung build, while before 3.11 (the worker image is built on
        # Ubuntu 22.04, whose Python is 3.10) the same error took the ordinary
        # failure path.
        try:
            pipeline_future.exception(timeout=supervisor_deadline_s)
        except Exception as _guard_exc:  # the wait gave up; done() below is what decides
            logger.debug("the wait for the build thread ended: %s: %s", type(_guard_exc).__name__, _guard_exc)
        if not pipeline_future.done():
            # Build thread is stuck past its deadline. Decide the build's fate
            # on a fresh DB conn: the original `conn` is owned by the stuck
            # sub-thread and is NOT thread-safe to touch from here.
            supervisor_timed_out = True
            # From now on nobody waits for the build thread; when it ends it
            # removes a Railway project it created and nobody announced (see
            # the end of _run_pipeline). If it had already passed that point
            # when the deadline fell, the supervisor's follow-up does it.
            with attempt_state["handoff"]:
                thread_had_ended = attempt_state["thread_ended"]
                if not thread_had_ended:
                    attempt_state["abandoned"] = True
            logger.error(
                "Build %s: supervisor deadline (%ds) exceeded; force-failing "
                "stuck build thread",
                build_id, supervisor_deadline_s,
            )
            committed, verdict, probe_result = _supervisor_force_fail(
                build_id, tier, timeout_minutes,
                customer_email=customer_email,
                customer_name=customer_name,
                admin_email=admin_email,
                attempt=attempt,
                cancel_key=cancel_key,
                attempt_railway=attempt_state.get("railway"),
                attempt_state=attempt_state,
            )
            requeued = committed and verdict == "retry"
            # verdict == 'preview': the (now zombie) thread completed the
            # Railway deploy, the app answers and the build is in preview
            # awaiting review. It is announced below like any other preview.
            # verdict == 'failed': _supervisor_force_fail went through
            # _fail_build, which wrote the dead-letter row and sent the
            # customer's "build failed" email. Nothing more to send here.
            # verdict == 'retry' (first-hang free retry) sends NO email: the
            # build is re-queued and a later run will email the real outcome.
            _after_supervisor_decision(
                job, verdict, committed, probe_result,
                thread_had_ended or pipeline_future.done())
            # Return early: the original `conn` is poisoned by the stuck
            # sub-thread. Skip the happy-path, the normal except handler
            # (which uses `conn`), and the finally's API cost write.
            return
        # The pipeline is done: its result, or whatever it raised (which the
        # handlers below deal with like any other failure of the attempt).
        deploy_result = pipeline_future.result()
        live_url = (deploy_result.get("live_url") or "") if isinstance(deploy_result, dict) else (deploy_result or "")

        # The pipeline thread may have reconnected via _ensure_conn mid-build
        # (PostgreSQL idle timeout / Railway restart), closing the original conn
        # this function still holds. Re-ensure before any post-pipeline write so
        # a deployed build is never mis-marked failed by a write on a dead conn.
        # _ensure_conn is a cheap SELECT 1 when the conn is alive.
        conn = _ensure_conn(conn)

        # Local build (no RAILWAY_API_TOKEN): _run_pipeline already moved the build
        # to preview through _set_preview_status, or raised if that write matched
        # no row. Skip the duplicate status write, Railway metadata storage and
        # the admin and customer emails: there is no deployed URL to share.
        if isinstance(deploy_result, dict) and deploy_result.get("local_preview"):
            _append_log(conn, build_id, "preview", "Local build ready for review (no Railway deploy)")
            logger.info("Build %s: local preview (no Railway token); pipeline complete", build_id)
            return

        # The pipeline normally notes its Railway project itself, right after
        # deploy() returns; this covers a pipeline that only reported it.
        if isinstance(deploy_result, dict) and attempt_state["railway"] is None:
            _note_attempt_project(job, deploy_result)
        railway = attempt_state["railway"]

        # One guarded write moves the build to preview and records the Railway
        # project it is announced with. If it matches no row, the build left
        # the worker's hands while it was deploying (an admin kill, a failure
        # or a preview written by someone else): nothing is announced, and the
        # project this attempt deployed for it, which the row does not record,
        # is deleted again.
        if not _set_preview_status(conn, build_id, preview_url=live_url, attempt=attempt,
                                   railway=railway):
            logger.warning(
                "Build %s: pipeline finished but this thread no longer owns the build "
                "(it left the worker-owned statuses or is on a later attempt); no preview "
                "is announced.",
                build_id,
            )
            _discard_unannounced_project(conn, job, "the preview write matched no row")
            return
        _mark_attempt_project_announced(job, railway)

        login_token = deploy_result.get("magic_link_raw_token") if isinstance(deploy_result, dict) else None
        _announce_preview(conn, job, live_url, railway, login_token or attempt_state.get("login_token"))

    except BuildNoLongerOwnedError as not_owned:
        # A guarded status write inside the pipeline matched no row. Whoever
        # moved the build (an admin kill, another writer) decided its fate, so
        # there is nothing to retry, fail or announce here.
        logger.warning(
            "Build %s: %s; stopping without a retry, a failure record or an email",
            build_id, not_owned,
        )
        _cleanup_build_node_modules(build_dir)
        # If the attempt got as far as deploying, its project is not the
        # build's any more (whoever took the build over never heard of it).
        try:
            conn = _ensure_conn(conn)
        except Exception as conn_exc:
            logger.warning("Build %s: no database connection for the clean-up: %s", build_id, conn_exc)
        _discard_unannounced_project(conn, job, "the build left this attempt's hands")

    except Exception as exc:
        # Redact once, up front: the text is logged, stored in
        # builds.error_message / the dead-letter table, and compared against the
        # previous attempt's stored (already redacted) message.
        error_message = _redact_secrets(str(exc))
        logger.error("Build %s failed: %s", build_id, error_message)

        # The pipeline thread may have reconnected mid-build, leaving this
        # function's `conn` closed; re-ensure before the failure-handling DB
        # writes (_fail_build / requeue) below so the failure is recorded
        # instead of itself throwing on a dead conn.
        conn = _ensure_conn(conn)

        # Report to Sentry with build context
        sentry_sdk.set_tag("build_id", str(build_id))
        sentry_sdk.set_tag("tier", tier)
        sentry_sdk.capture_exception(exc)

        # deploy.create_project could not learn whether Railway created the
        # project (see cli/deploy.py RailwayOutcomeUnknown). If it did, no
        # build row and no attempt state names it, so say so where an
        # operator will find it: in this build's log.
        _unrecorded = getattr(exc, "unrecorded_project_candidates", None)
        if _unrecorded is not None:
            _named = ", ".join(str(pid) for pid in _unrecorded) or "none found by name"
            _append_log(
                conn, build_id, "deploying",
                "Railway did not confirm a project creation for this attempt. Projects with this "
                f"app's name created at that time: {_named}. If one of them is in use by no "
                "build, delete it by hand.",
            )

        # Auto-retry: requeue if under retry limit and within time budget.
        # Both transient and code errors get retried (code errors rebuild fresh).
        # Quality gate policy:
        #   - QAQualityGateError: non-retryable. Structural test failures
        #     (missing CSRF, auth bypass, etc.) don't self-heal via rebuild.
        #   - DesignQualityGateError: ONE retry with the critical issues
        #     injected into builds.rebuild_request so the next build's
        #     prompt treats them as HIGHEST PRIORITY. Design regeneration
        #     is stochastic and guided rebuilds empirically resolve it
        #     (see admin AI Fix flow).
        # Infrastructure errors (npm install, ENOENT, ENOSPC) get only 1 retry
        # since they are unlikely to self-resolve and waste time + API calls.
        is_design_quality_gate = isinstance(exc, DesignQualityGateError)
        is_qa_quality_gate = isinstance(exc, QAQualityGateError)
        # Base QualityGateError (or any other subclass not listed above) stays
        # non-retryable as a safe default.
        is_generic_quality_gate = (
            isinstance(exc, QualityGateError)
            and not is_design_quality_gate
            and not is_qa_quality_gate
        )
        # Infrastructure / resource-exhaustion errors get only 1 retry: they
        # rarely self-resolve and re-running the full pipeline wastes time + API
        # spend. OOM ("killed" / "signal 9" / "out of memory" / "enomem") is
        # included so an out-of-memory kill is not misclassified as a generic
        # transient error and retried the full _MAX_RETRIES times. Matched
        # case-insensitively (errno strings appear in mixed case).
        _INFRA_ERROR_KEYWORDS = (
            "npm install failed", "npm error", "enoent", "enospc", "enomem",
            "killed", "signal 9", "out of memory",
        )
        _err_lower = error_message.lower()
        is_infra_error = any(kw in _err_lower for kw in _INFRA_ERROR_KEYWORDS)

        if is_qa_quality_gate:
            max_retries_for_this = 0
            logger.info("Build %s: QA quality gate failure, not retrying", build_id)
        elif is_generic_quality_gate:
            max_retries_for_this = 0
            logger.info("Build %s: quality gate failure, not retrying", build_id)
        elif is_design_quality_gate or is_infra_error:
            max_retries_for_this = 1
        else:
            max_retries_for_this = _MAX_RETRIES
        can_retry = retry_count < max_retries_for_this

        if is_design_quality_gate and not can_retry:
            logger.info("Build %s: design quality gate failure, 1 retry exhausted", build_id)
        if is_infra_error and not can_retry:
            logger.info("Build %s: infrastructure error, max 1 retry exhausted", build_id)

        # v2-deterministic early-exit: errors tagged `[v2-deterministic]` are
        # raised by deterministic failure paths in the v2 compose engine (e.g.
        # the CAS_BUILD_ENGINE=v2 deploy_fix gate when a v2 artifact crashes).
        # Retrying a deterministic error reproduces it exactly; same spec +
        # same compose engine + same commit = same output. Skip retry entirely
        # and go straight to _fail_build so we don't create orphan Railway
        # projects on every attempt. v2 builds with genuinely transient
        # failures (DB blip, network) still go through _is_transient_error
        # and retry as usual, only the explicit deterministic tag disables
        # retry.
        if can_retry and _is_deterministic_failure(error_message):
            can_retry = False
            logger.info(
                "Build %s: deterministic failure, skipping retry "
                "(same input + deterministic engine = same output; retrying "
                "would only create an orphan Railway project)",
                build_id,
            )
            _append_log(
                conn, build_id, "failed",
                "v2 compose engine produced a deterministic failure; retry "
                "would reproduce the same error. Fix belongs in compose/, "
                "not a build retry.",
            )

        # Repeated-error early-exit: if the current error is identical to the
        # previous attempt's error, the fix loop didn't help and retrying will
        # just waste another 30 min.  Bail after 2+ consecutive identical errors.
        # Exclude transient errors: they can legitimately repeat (DB down, network
        # blip) and may self-resolve on the next try.
        is_transient = _is_transient_error(error_message)
        if can_retry and retry_count >= 1 and not is_transient:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT error_message FROM builds WHERE id = %s", (build_id,))
                    row = cur.fetchone()
                    if row and row[0]:
                        prev_msg = re.sub(r"^Retry \d+/\d+: ", "", row[0])
                        if prev_msg == error_message:
                            can_retry = False
                            logger.info("Build %s: identical error across 2 consecutive attempts, not retrying", build_id)
                            _append_log(conn, build_id, "failed",
                                        "Same error repeated across consecutive attempts; fix loop is not resolving the issue")
            except Exception as guard_exc:
                # Fail open: if the previous error cannot be read, allow the retry.
                logger.warning("Build %s: repeated-error check failed, allowing retry: %s", build_id, guard_exc)

        if can_retry and _retry_series_age_s(build_id) > _RETRY_TIME_BUDGET_S:
            # See _RETRY_TIME_BUDGET_S for what the clock measures.
            can_retry = False
            logger.info("Build %s: exceeded the retry time budget, not retrying", build_id)
            _append_log(conn, build_id, "failed",
                        "Build exceeded 2-hour time budget; no more retries")

        # Always clean up node_modules after any failure to reclaim disk space.
        # node_modules can be reinstalled on the next attempt.
        _cleanup_build_node_modules(build_dir)

        if can_retry:
            try:
                if is_design_quality_gate:
                    # DESIGN_QG_RETRY1: inject the latest critical issue list
                    # into builds.rebuild_request so the next attempt's
                    # build prompt has concrete guidance. Best-effort; on
                    # injection failure we still requeue for stochastic re-gen.
                    _inject_design_issues_as_rebuild_request(conn, build_id)
                    _append_log(
                        conn, build_id, "building",
                        "Design review blocked deploy; retrying with guided rebuild",
                    )
                is_code_error = not _is_transient_error(error_message)
                if is_code_error:
                    # Code errors: clear artifact and on-disk build dir so rebuild starts truly fresh.
                    # Leftover files from failed builds can cause ENOENT/corruption on retry.
                    # The artifact and the directory are shared by every attempt of the
                    # build, so only an attempt that still owns the build touches them:
                    # the guarded clear says whether it does. (The build can still change
                    # hands between that answer and the removal below; the requeue that
                    # follows is guarded again, and nothing else is lost than the
                    # directory, which the next attempt restores from the artifact or
                    # composes again.)
                    if _clear_build_artifact(conn, build_id, attempt):
                        bd = Path(build_dir)
                        if bd.exists():
                            try:
                                shutil.rmtree(bd)
                                logger.info("Build %s: removed stale build dir %s for fresh retry", build_id, bd)
                            except Exception as rm_exc:
                                logger.warning("Build %s: failed to remove build dir: %s", build_id, rm_exc)
                        _append_log(conn, build_id, "testing",
                                    f"Code error on attempt {retry_count + 1}/{_MAX_RETRIES}, auto-rebuilding fresh")
                requeued = _requeue_for_retry(conn, build_id, retry_count, error_message, attempt=attempt)
            except Exception as retry_exc:
                logger.error("Build %s: failed to requeue for retry: %s", build_id, retry_exc)
                _fail_build(conn, build_id, error_message, customer_email, customer_name, app_name, admin_email,
                            retry_count=retry_count, attempt=attempt)
        else:
            _fail_build(conn, build_id, error_message, customer_email, customer_name, app_name, admin_email,
                        retry_count=retry_count, attempt=attempt)

        # However this attempt ended, it did not reach preview. If it got as
        # far as creating a Railway project, that project was never announced
        # and the next attempt will create its own: delete it. This is the
        # attempt's project from its own state; the project the build row
        # records (an earlier, announced deployment, if there is one) is not
        # touched.
        _discard_unannounced_project(conn, job, "the attempt ended without reaching preview")

    finally:
        if not requeued:
            _end_retry_series(build_id)
        # Write API cost metrics (runs on both success and failure paths).
        # SKIPPED when supervisor_timed_out is True: the build thread still
        # owns `conn` as a zombie, and touching it from here would corrupt
        # the psycopg2 connection state.
        if not supervisor_timed_out:
            try:
                # _pipeline_metrics was populated by the pipeline thread via _metrics_out
                # before _run_pipeline returned. Reading threading.local() here would see
                # the wrong thread's (empty) storage: hence the shared-list approach.
                all_metrics = _pipeline_metrics
                logger.info("Build %s: collected %d API metrics", build_id, len(all_metrics))
                total_cost = sum(m.estimated_cost_usd for m in all_metrics) if all_metrics else 0.0
                conn = _ensure_conn(conn)
                with conn.cursor() as cur:
                    # Accumulate: a build that was retried spent API money on
                    # every attempt, and the daily spend cap reads this column.
                    cur.execute(
                        "UPDATE builds SET total_api_cost_usd = "
                        "COALESCE(total_api_cost_usd, 0) + %s WHERE id = %s",
                        (total_cost, build_id),
                    )
                conn.commit()
                if all_metrics:
                    import json as _json
                    cost_data = [
                        {"module": m.module, "model": m.model, "stage": m.stage,
                         "input_tokens": m.input_tokens, "output_tokens": m.output_tokens,
                         "cache_read": m.cache_read_tokens, "cache_write": m.cache_write_tokens,
                         "api_mode": m.api_mode, "cost_usd": round(m.estimated_cost_usd, 6)}
                        for m in all_metrics
                    ]
                    _append_log(conn, build_id, "api_costs", _json.dumps(cost_data))
                    # Build advisor cost breakdown (only entries with advisor data)
                    breakdown = {}
                    for m in all_metrics:
                        if getattr(m, 'advisor_model', ''):
                            breakdown[m.stage or m.module] = {
                                "advisor_model": m.advisor_model,
                                "advisor_input_tokens": m.advisor_input_tokens,
                                "advisor_output_tokens": m.advisor_output_tokens,
                                "advisor_cost_usd": round(m.advisor_cost_usd, 6),
                                "executor_model": m.model,
                                "executor_cost_usd": round(m.executor_cost_usd, 6),
                                "total_cost_usd": round(m.estimated_cost_usd, 6),
                            }
                    if breakdown:
                        conn = _ensure_conn(conn)
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE builds SET cost_breakdown = %s WHERE id = %s",
                                (_json.dumps(breakdown), build_id),
                            )
                        conn.commit()
                logger.info("Build %s: API cost $%.4f (%d calls)", build_id, total_cost, len(all_metrics))
            except Exception as cost_exc:
                logger.warning("Build %s: failed to write API costs: %s", build_id, cost_exc)

        # Shut down the supervisor executor. wait=False so we don't block on
        # a zombie build thread that is stuck in a C-level syscall. Daemon
        # threads die on worker process exit. (The attempt's cancel event is
        # dropped by the pipeline future's done-callback, see above.)
        try:
            pipeline_executor.shutdown(wait=False, cancel_futures=False)
        except Exception as shutdown_exc:
            logger.debug("Build %s: pipeline executor shutdown: %s", build_id, shutdown_exc)


def _classify_error(error_message: str) -> str:
    """Classify error into category for dead letter queue."""
    lower = (error_message or "").lower()
    if _is_transient_error(error_message):
        return "transient"
    if "timeout" in lower or "sigalrm" in lower or "timed out" in lower:
        return "timeout"
    if "deploy" in lower or "railway" in lower or "health check" in lower:
        return "infra"
    return "code_error"


def _recorded_railway_projects(cur, app_name: str) -> set:
    """Railway project ids recorded on any build row with this app_name.

    The zombie-deploy probe matches Railway projects by name, and app_name is
    not unique across customers or across one customer's rebuilds. A project
    that a build row records is a deployment that reached preview: another
    build's, or an earlier deployment of the very build the supervisor is
    deciding about (it was previewed, requeued in place and is deploying
    again). Neither is what the stuck attempt is creating, so none of them
    may be adopted as its "zombie". An attempt's own project is never on a
    row before the preview write (see _set_preview_status).
    """
    cur.execute(
        "SELECT railway_project_id FROM builds "
        "WHERE app_name = %s AND railway_project_id IS NOT NULL",
        (app_name,),
    )
    claimed = set()
    for row in (cur.fetchall() or []):
        try:
            project_id = row[0]
        except (TypeError, IndexError, KeyError):
            continue
        if isinstance(project_id, str) and project_id:
            claimed.add(project_id)
    return claimed


def _supervisor_reality_probe(
    app_name: str,
    started_at,
    completed_at=None,
    hard_timeout_s: int = _SUPERVISOR_REALITY_PROBE_DEADLINE_S,
    exclude_project_ids=None,
):
    """Thin wrapper delegating to queue.watchdog_probe.probe_for_zombie_deploy.

    Kept for backward-compat so all call sites in _supervisor_force_fail are
    unchanged. The shared implementation lives in queue/watchdog_probe.py so
    coordinator/agents/reconciliation.py can reuse it without duplication.

    Called from ``_supervisor_force_fail`` BEFORE any DB write so that a zombie
    thread that completed the deploy post-kill isn't buried under a false
    ``status=failed``. See docs/15_PROD_BUILD_PATTERNS.md §2.1
    (ZOMBIE_THREAD_STATUS_OVERWRITE1) for the original rationale.

    Algorithm:
      1. List all Railway projects for the current token.
      2. Filter to ``project.name == app_name`` whose ``createdAt`` falls in
         ``[started_at - 5min, (completed_at or now) + 30min]``.
      3. For each match (in createdAt order, freshest first), resolve the first
         public service domain and GET ``https://{domain}/health``.
      4. The first healthy answer wins (HTTP 200 with a JSON body whose
         status is "ok": deploy.health_response_ok, the same definition a
         fresh deploy has to meet); return ``{"live_url": ..., "project_id":
         ..., "service_id": ...}``.
      5. Fail SAFE: every exception, network error, unhealthy answer, and the
         ``hard_timeout_s`` wall-clock deadline all return ``None``. This
         function MUST NOT raise: the supervisor cannot afford to hang
         here, so any ambiguity falls through to the existing failed-path.

    Args:
        app_name: The CAS build's app_name (== Railway project name).
        started_at: ``builds.started_at`` (datetime or None). If None we probe
            anyway with only an upper bound; better to surface a zombie-live
            than miss it.
        completed_at: ``builds.completed_at`` (datetime or None). None means
            "use now()" for the upper bound.
        hard_timeout_s: Deadline for the ENTIRE probe (list + resolve + /health
            loop), enforced via ``time.monotonic()`` checks between steps.

    Returns:
        dict with keys ``live_url``, ``project_id``, ``service_id`` on first
        live match, else ``None``.
    """
    from watchdog_probe import probe_for_zombie_deploy  # queue/ is on sys.path
    extra = {"exclude_project_ids": exclude_project_ids} if exclude_project_ids else {}
    return probe_for_zombie_deploy(
        app_name=app_name,
        started_at=started_at,
        completed_at=completed_at,
        hard_timeout_s=hard_timeout_s,
        **extra,
    )


def _write_probed_preview(conn, build_id: str, preview_url: str, attempt, railway: dict,
                          state: Optional[dict]) -> Optional[bool]:
    """The preview write for a deployment the supervisor's probe found.

    Ordered against the build thread's own clean-up of the project it created:
    both take the attempt's lock (state["lock"]), the thread across its
    look-up and delete (_discard_unannounced_project), this function across
    the write and the note that the project is announced. So either the write
    lands first, and the thread's look-up finds the project recorded (or its
    state says it was announced), or the thread deleted the project first and
    this function sees `discarded` and writes nothing.

    Returns True when the write landed, False when it matched no row (the
    build left the worker-owned statuses or this attempt), and None when the
    project was deleted by the thread, or the lock could not be had within
    _ATTEMPT_LOCK_WAIT_S (the thread is still deleting): in both cases nothing
    is announced, and the caller treats the probe as having found nothing.
    Without a state (a caller that has none) the write is unordered.
    """
    lock = (state or {}).get("lock")
    if lock is not None and not lock.acquire(timeout=_ATTEMPT_LOCK_WAIT_S):
        logger.warning(
            "Build %s: the attempt's thread still holds the lock on its Railway project after "
            "%.0fs; not announcing the probed deployment at %s",
            build_id, _ATTEMPT_LOCK_WAIT_S, preview_url,
        )
        return None
    try:
        own = (state or {}).get("railway") or {}
        if state is not None and state["discarded"] and own.get("project_id") == railway.get("project_id"):
            logger.warning(
                "Build %s: the attempt's thread deleted Railway project %s after the probe saw it "
                "answer; not announcing %s",
                build_id, railway.get("project_id"), preview_url,
            )
            return None
        landed = _set_preview_status(conn, build_id, preview_url=preview_url, attempt=attempt,
                                     railway=railway)
        if landed and state is not None:
            _record_announcement(state, railway)
        return landed
    finally:
        if lock is not None:
            lock.release()


def _supervisor_force_fail(
    build_id: str,
    tier: str,
    timeout_minutes: int,
    *,
    customer_email: str = "",
    customer_name: str = "",
    admin_email: str = "",
    attempt,
    cancel_key: Optional[str] = None,
    attempt_railway: Optional[dict] = None,
    attempt_state: Optional[dict] = None,
) -> tuple[bool, str, dict | None]:
    """Decide the fate of a stuck build, on a FRESH DB connection.

    Called by the supervisor in _process_job when the build thread has run past
    its tier timeout plus grace. The thread may still be blocked in a C-level
    call (subprocess.wait, socket recv, SSL_read), so it cannot be relied on to
    raise, and the connection it owns must not be touched from here.

    Order of work:
      1. Fire the attempt's cancel event (cli_cancellation.fire with the
         attempt's cancel_key), so a fix loop or model call inside the stuck
         thread stops at its next checkpoint instead of working on against a
         build that is being failed. Only the v1 fix loops and the model
         client poll that event; a thread blocked in the Railway CLI or in a
         socket read sees it only when that call returns.
      2. Read the build row in a short transaction that is committed at once.
         No row lock and no open transaction is held during step 3.
      3. Probe Railway (_supervisor_reality_probe; docs/15_PROD_BUILD_PATTERNS.md
         section 2.1): a thread stuck in a C-level call can have finished the
         deploy. Any probe error or timeout counts as "nothing found".
      4. Write one outcome. Each outcome is an UPDATE guarded by the build still
         being in building/testing/deploying and still on the attempt whose
         token is given. The stuck thread's own writes only move a build
         between those stages; anything else that happened during the probe
         (an admin kill, a requeue, a requeue followed by a new claim) fails
         one of the two guards, and then the supervisor writes nothing and
         reports committed=False.

    Outcomes:
      - "preview": the probe found a deployment whose /health answers. The
        build goes to preview through _set_preview_status, so it enters review
        like any other arrival at preview, with preview_url set to the probed
        URL and the probed project recorded on the row in the same statement.
        live_url stays unset until the web app publishes the build. The caller
        announces it (_after_supervisor_decision).
      - "retry": first hang (supervisor_retry_used is not TRUE and retry_count
        is below _MAX_RETRIES). The build is requeued with
        supervisor_retry_used = TRUE and retry_count + 1, and no dead-letter
        row is written. Every build gets this free retry at most once.
      - "failed": second hang, or no retries left. _fail_build writes the
        dead-letter row (category 'timeout'), sets 'failed' and emails the
        customer. No Railway project is deleted here: the one recorded on the
        row is an announced deployment, and the one the stuck thread may be
        creating is that thread's to remove when it ends (_run_pipeline).

    customer_email, customer_name and admin_email are used for that email only.
    attempt is the stuck job's attempt token (job["attempt"]) and is required.
    cancel_key is the attempt's cancel event key. attempt_railway is the
    attempt's own record of the project it created, if deploy() had already
    returned when the thread got stuck: that project is never excluded from
    the probe, and its record (which has the environment id and the database
    URL) is what gets written to the row when the probe finds it.
    attempt_state is the attempt's state (_attempt_state). With it, the write
    that announces a probed project is ordered against the thread's own
    deletion of the project it created (_write_probed_preview): the project
    is announced before the thread looks it up, or the thread has deleted it
    and nothing is announced.

    Returns ``(committed, verdict, probe_result)``:
      - ``committed`` is True if the supervisor's write landed, False if the
        row was missing, already out of the worker-owned statuses, or a write
        failed.
      - ``verdict`` is "preview", "retry" or "failed"; it is "failed" whenever
        ``committed`` is False.
      - ``probe_result`` is the dict from ``_supervisor_reality_probe``
        (``live_url``, ``project_id``, ``service_id``) on a "preview" verdict,
        else ``None``.
    """
    attempt_value = _attempt_param(attempt)

    # Fire cancellation before touching the database, so a cooperative consumer
    # inside the stuck thread sees it at the earliest possible checkpoint.
    if cancel_key:
        try:
            cli_cancellation.fire(cancel_key)
        except Exception as cancel_exc:
            logger.warning(
                "Build %s: cancellation registry fire failed: %s",
                build_id, cancel_exc,
            )

    try:
        fresh_conn = _get_conn(retries=3, base_delay=1.0)
    except Exception as exc:
        logger.error(
            "Build %s: supervisor could not open fresh DB conn to force-fail: %s",
            build_id, exc,
        )
        return False, "failed", None

    try:
        claimed_elsewhere: set = set()
        probe_allowed = True
        with fresh_conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, retry_count, supervisor_retry_used,
                       app_name, started_at, completed_at
                  FROM builds
                 WHERE id = %s
                """,
                (build_id,),
            )
            row = cur.fetchone()
            if row is not None and row[0] in _STAGE_ORDER:
                try:
                    # Projects already recorded on a build row of this app
                    # name are announced deployments, not this attempt's
                    # zombie (app_name is not unique).
                    claimed_elsewhere = _recorded_railway_projects(cur, row[3])
                    # Nor are the fresh projects of other attempts running in
                    # this process, which are on no build row yet.
                    own_project_id = (attempt_railway or {}).get("project_id")
                    with _UNRECORDED_ATTEMPT_PROJECT_IDS_LOCK:
                        claimed_elsewhere |= {
                            pid for pid in _UNRECORDED_ATTEMPT_PROJECT_IDS
                            if pid != own_project_id
                        }
                except Exception as claimed_exc:
                    # Without the exclusion list a probe could adopt another
                    # build's deployment, so skip the probe altogether.
                    probe_allowed = False
                    logger.warning(
                        "Build %s: could not list Railway projects of other builds (%s); "
                        "skipping the reality probe",
                        build_id, claimed_exc,
                    )
        # End the read transaction here: nothing is locked and no transaction
        # is open while Railway is probed below.
        fresh_conn.commit()

        if row is None:
            logger.warning(
                "Build %s: supervisor fired but row not found; no-op",
                build_id,
            )
            return False, "failed", None

        (
            status,
            retry_count,
            supervisor_retry_used,
            probe_app_name,
            probe_started_at,
            probe_completed_at,
        ) = row
        if status not in _STAGE_ORDER:
            logger.warning(
                "Build %s: supervisor fired but the build is already %r "
                "(no longer in a worker-owned status); no-op",
                build_id, status,
            )
            return False, "failed", None
        if probe_started_at != attempt_value:
            logger.warning(
                "Build %s: supervisor fired for an attempt that is no longer the "
                "current one (the build was requeued and claimed again); no-op",
                build_id,
            )
            return False, "failed", None

        retry_count = retry_count or 0
        supervisor_retry_used = bool(supervisor_retry_used)

        # ----------------------------------------------------------------
        # Reality check: probe Railway before any write. Fail-safe: a probe
        # error leaves probe_result None and the retry/terminal branches run.
        # ----------------------------------------------------------------
        probe_result = None
        if probe_allowed:
            try:
                probe_kwargs = (
                    {"exclude_project_ids": claimed_elsewhere} if claimed_elsewhere else {}
                )
                probe_result = _supervisor_reality_probe(
                    app_name=probe_app_name,
                    started_at=probe_started_at,
                    completed_at=probe_completed_at,
                    **probe_kwargs,
                )
            except Exception as probe_exc:
                logger.warning(
                    "Build %s: supervisor reality probe raised (%s); falling "
                    "through to failed-path",
                    build_id, probe_exc,
                )
                probe_result = None

        if probe_result and probe_result.get("live_url"):
            preview_url = probe_result["live_url"]
            if attempt_railway and attempt_railway.get("project_id") == probe_result.get("project_id"):
                probed_railway = dict(attempt_railway)
            else:
                # The stuck thread has not reported its project (it is still
                # inside deploy()). The probe knows the project and the
                # service; the environment id is looked up by the caller.
                probed_railway = _railway_record(
                    probe_result.get("project_id"), probe_result.get("service_id"),
                    live_url=preview_url,
                )
            landed = _write_probed_preview(
                fresh_conn, build_id, preview_url, attempt, probed_railway, attempt_state)
            if landed is None:
                # The thread deleted that project (or is deleting it) after
                # the probe saw it answer: there is no deployment to announce.
                probe_result = None
            elif not landed:
                logger.warning(
                    "Build %s: supervisor found a healthy deployment at %s but the build "
                    "left the worker-owned statuses (or this attempt) during the probe; no-op",
                    build_id, preview_url,
                )
                return False, "failed", None
        if probe_result and probe_result.get("live_url"):
            preview_note = (
                f"Supervisor reality check recovered zombie deploy: "
                f"build thread exceeded {timeout_minutes}min deadline but "
                f"Railway /health returned 200 at {preview_url}. Routed to "
                f"preview for SE review."
            )[:4000]
            # The build is in preview now, with the probed project recorded by
            # the preview write itself. The note is bookkeeping on top of that,
            # in its own transaction, and only for the row as that write left
            # it (same project, still in preview).
            try:
                with fresh_conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE builds
                           SET error_message = %s
                         WHERE id = %s
                           AND status = 'preview'
                           AND railway_project_id IS NOT DISTINCT FROM %s
                        """,
                        (
                            preview_note,
                            build_id,
                            probed_railway["project_id"],
                        ),
                    )
                fresh_conn.commit()
            except Exception as note_exc:
                try:
                    fresh_conn.rollback()
                except Exception as _guard_exc:  # rollback guard: the connection is already unusable
                    logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)
                logger.warning(
                    "Build %s: supervisor could not record its note on the build: %s",
                    build_id, note_exc,
                )
            _append_log(
                fresh_conn, build_id, "supervisor",
                "We detected that your website actually deployed "
                "successfully. Your preview is ready for review.",
            )
            logger.warning(
                "Build %s: supervisor reality-check ROUTED TO PREVIEW "
                "(app=%s, live_url=%s)",
                build_id, probe_app_name, preview_url,
            )
            try:
                sentry_sdk.capture_message(
                    f"Build {build_id}: supervisor reality-check recovered "
                    f"zombie deploy at {preview_url} (tier={tier})",
                    level="warning",
                )
            except Exception as _guard_exc:  # Sentry is best effort; reporting must not change the outcome
                logger.debug("Sentry report failed: %s: %s", type(_guard_exc).__name__, _guard_exc)
            return True, "preview", probe_result

        # ----------------------------------------------------------------
        # First-hang branch: one free retry, no dead-letter row.
        # ----------------------------------------------------------------
        if (not supervisor_retry_used) and retry_count < _MAX_RETRIES:
            retry_error_message = (
                f"Supervisor timeout: build exceeded {timeout_minutes}min "
                f"tier deadline + {_SUPERVISOR_GRACE_SECONDS}s grace. "
                f"Build thread appeared blocked in a C-level call "
                f"(subprocess/socket/SSL); issuing one free supervised "
                f"retry before permanent failure."
            )[:4000]
            with fresh_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE builds
                       SET status = 'queued',
                           supervisor_retry_used = TRUE,
                           retry_count = COALESCE(retry_count, 0) + 1,
                           error_message = %s,
                           started_at = NULL,
                           completed_at = NULL
                     WHERE id = %s
                       AND status IN ('building', 'testing', 'deploying')
                       AND supervisor_retry_used IS NOT TRUE
                       AND started_at IS NOT DISTINCT FROM %s::timestamp
                    """,
                    (retry_error_message, build_id, attempt_value),
                )
                requeued = cur.rowcount != 0
            fresh_conn.commit()
            if not requeued:
                logger.warning(
                    "Build %s: supervisor free retry matched no row (the build left the "
                    "worker-owned statuses during the probe); no-op",
                    build_id,
                )
                return False, "failed", None
            _append_log(
                fresh_conn, build_id, "supervisor",
                "We hit an unexpected pause and are automatically "
                "retrying this build.",
            )
            logger.warning(
                "Build %s: supervisor issued free retry "
                "(tier=%s, timeout=%dmin, retry_count=%d)",
                build_id, tier, timeout_minutes, retry_count + 1,
            )
            try:
                sentry_sdk.capture_message(
                    f"Build {build_id}: supervisor free-retry after "
                    f"{timeout_minutes}min hang (tier={tier})",
                    level="warning",
                )
            except Exception as _guard_exc:  # Sentry is best effort; reporting must not change the outcome
                logger.debug("Sentry report failed: %s: %s", type(_guard_exc).__name__, _guard_exc)
            return True, "retry", None

        # ----------------------------------------------------------------
        # Second-hang branch: terminal, through the one terminal-failure path.
        # ----------------------------------------------------------------
        terminal_error_message = (
            f"Supervisor timeout: build exceeded {timeout_minutes}min tier "
            f"deadline + {_SUPERVISOR_GRACE_SECONDS}s grace and has no "
            f"supervised retry left. Build thread appears blocked in a C-level "
            f"call (subprocess/socket/SSL); supervised terminal force-fail."
        )
        failed = _fail_build(
            fresh_conn, build_id, terminal_error_message,
            customer_email, customer_name, probe_app_name or "", admin_email,
            retry_count=retry_count,
            category="timeout",
            attempt=attempt,
        )
        if not failed:
            return False, "failed", None
        _append_log(
            fresh_conn, build_id, "supervisor",
            "We were unable to complete this build after "
            "automatic retries. Please reach out to support.",
        )
        logger.error(
            "Build %s: supervisor force-failed stuck build TERMINALLY "
            "(tier=%s, timeout=%dmin, retry_count=%d, supervisor_retry_used=%s)",
            build_id, tier, timeout_minutes, retry_count, supervisor_retry_used,
        )
        try:
            sentry_sdk.capture_message(
                f"Build {build_id}: supervisor TERMINAL force-fail after "
                f"{timeout_minutes}min timeout (tier={tier})",
                level="error",
            )
        except Exception as _guard_exc:  # Sentry is best effort; reporting must not change the outcome
            logger.debug("Sentry report failed: %s: %s", type(_guard_exc).__name__, _guard_exc)
        return True, "failed", None
    except Exception as exc:
        logger.error(
            "Build %s: supervisor force-fail write failed: %s",
            build_id, exc,
        )
        try:
            fresh_conn.rollback()
        except Exception as _guard_exc:  # rollback guard: the connection is already unusable
            logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)
        return False, "failed", None
    finally:
        try:
            fresh_conn.close()
        except Exception as _guard_exc:  # closing a connection that is already gone
            logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)


def _has_prior_timeout_failure(conn, build_id: str) -> bool:
    """Check whether this build_id has already burned its one free supervisor retry.

    WATCHDOG1 Layer 2: the authoritative marker is the new
    `builds.supervisor_retry_used` BOOLEAN column, NOT the DLQ. A build that
    has supervisor_retry_used=TRUE has already been given its free retry
    and re-queued once, so orphan recovery must NOT re-queue it again; that
    would bypass the "exactly one retry" contract.

    The caller's signature stays `(conn, build_id)` to match every call site
    in this file (see _recover_orphaned_builds).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT supervisor_retry_used
                  FROM builds
                 WHERE id = %s
                """,
                (build_id,),
            )
            row = cur.fetchone()
            return bool(row and row[0])
    except Exception as exc:
        logger.warning(
            "Build %s: failed to read supervisor_retry_used: %s",
            build_id, exc,
        )
        return False


# The one statement in the worker that sets a build to 'failed'. It matches only
# a build that is still building, testing or deploying: not one that is queued
# (waiting for its next attempt), previewed, published or already failed, and
# only the attempt whose token is the last placeholder (see _attempt_param).
_MARK_FAILED_SQL = (
    "UPDATE builds SET status = 'failed', error_message = %s, completed_at = NOW() "
    "WHERE id = %s AND status IN ('building', 'testing', 'deploying') "
    "AND started_at IS NOT DISTINCT FROM %s::timestamp"
)


def _fail_build(conn, build_id: str, error_message: str, customer_email: str,
                customer_name: str, app_name: str, admin_email: str,
                retry_count: int = 0, *, category: str = None, attempt) -> bool:
    """Terminal failure of a build: the only way the worker sets 'failed'.

    Every terminal path goes through here: a pipeline error with no retry left
    (_process_job), the supervisor's terminal timeout, the three permanent
    failures of orphan recovery, and a crash of the build thread
    (_run_build_in_thread).

    In one transaction it inserts a dead_letter_builds row and runs
    _MARK_FAILED_SQL. If that UPDATE matches no row, the build has already left
    the worker's hands (requeued, previewed, published, failed or killed) or
    is on a later attempt than the caller's; the transaction including the
    dead-letter row is rolled back, and nothing else happens: no failure
    email. If the transaction itself errors, the status flip is retried alone
    so that a problem with the dead-letter table cannot leave a dead build in
    an active status.

    Returns True when this call failed the build. Only then does it send the
    customer's "build failed" email and run the janitor over old failed
    duplicates of the same app (_cleanup_old_builds, which deletes rows only).

    It does not touch Railway. The project recorded on the build row is the
    deployment the customer was last told about: after an in-place requeue of
    a build that was in preview, that is the previous, working site, and a
    failed redeploy is no reason to delete it. The project the failing attempt
    itself created, if it got that far, is known only to that attempt and is
    removed by its caller (_discard_unannounced_project in _process_job and
    _run_build_in_thread). Orphan recovery has no such record, because the
    worker that ran the attempt is gone; it deletes nothing.

    category is the dead-letter error_category; by default it is derived from
    the message by _classify_error. attempt is required (see _attempt_param).
    """
    attempt_value = _attempt_param(attempt)
    if category is None:
        category = _classify_error(error_message)
    truncated_error = _redact_secrets(error_message)[:4000]
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO dead_letter_builds
                       (build_id, error_category, error_message, original_retry_count,
                        build_snapshot)
                   VALUES (%s, %s, %s, %s,
                       (SELECT jsonb_build_object(
                           'tier', tier, 'app_name', app_name,
                           'spec_path', spec_path)
                        FROM builds WHERE id = %s))""",
                (build_id, category, truncated_error, retry_count, build_id),
            )
            cur.execute(_MARK_FAILED_SQL, (truncated_error, build_id, attempt_value))
            transitioned = cur.rowcount != 0
        if not transitioned:
            conn.rollback()
            logger.warning(
                "Build %s: not marking failed, it is no longer in a worker-owned status "
                "(requeued, previewed, published, failed or killed) or is on a later attempt",
                build_id,
            )
            return False
        conn.commit()
    except Exception as exc:
        logger.error("Build %s: failed to write DLQ/status: %s", build_id, exc)
        # The dead-letter insert and the status flip share a transaction, so a
        # failure of either undid both. Retry the status flip on its own.
        try:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(_MARK_FAILED_SQL, (truncated_error, build_id, attempt_value))
                transitioned = cur.rowcount != 0
            conn.commit()
        except Exception as fallback_exc:
            logger.error("Build %s: fallback status update also failed: %s",
                          build_id, fallback_exc)
            try:
                conn.rollback()
            except Exception as _guard_exc:  # rollback guard: the connection is already unusable
                logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)
            # Nothing was written: the build is still in an active status and
            # orphan recovery will find it once its lease is released. Telling
            # the customer it failed, or destroying its project, would be false.
            return False
        if not transitioned:
            return False
        logger.warning("Build %s: marked failed without a dead-letter row", build_id)
    try:
        send_build_failed(customer_email, customer_name, app_name, admin_email, build_id=build_id)
    except Exception as mail_exc:
        logger.warning("Build %s: send_build_failed raised (non-blocking): %s", build_id, mail_exc)

    # Old failed duplicates of this app (rows only; keeps the newest two).
    if app_name:
        try:
            _cleanup_old_builds(conn, build_id, app_name)
        except Exception as dup_exc:
            logger.warning("Build %s: duplicate cleanup failed: %s", build_id, dup_exc)
    return True


# ---------------------------------------------------------------------------
# Background threads: health monitor + daily backups
# ---------------------------------------------------------------------------

def _start_monitor_thread() -> None:
    """Spawn the health monitor as a daemon thread."""
    interval = env_int("MONITOR_INTERVAL", 900, min_val=1)

    def _monitor_loop():
        # run_checks survives a failed registry read and a failed check on its
        # own (monitor/ping.py). Should it end anyway, it is started again
        # after a pause instead of leaving the worker without a monitor until
        # its next restart.
        restart_delay = 60.0
        while not _shutdown_event.is_set():
            try:
                from monitor.ping import run_checks
                run_checks(interval_seconds=interval, stop_event=_shutdown_event)
                if _shutdown_event.is_set():
                    return
                logger.error("Health monitor loop returned; restarting it in %.0fs", restart_delay)
            except Exception as exc:
                logger.error("Health monitor loop stopped (%s); restarting it in %.0fs",
                             exc, restart_delay, exc_info=True)
            _shutdown_event.wait(restart_delay)
            restart_delay = min(restart_delay * 2, 900.0)

    thread = threading.Thread(target=_monitor_loop, daemon=True, name="health-monitor")
    thread.start()
    logger.info("Health monitor thread started (interval: %ds)", interval)


def _start_backup_thread() -> None:
    """Spawn the daily backup scheduler as a daemon thread."""
    def _backup_loop():
        while True:
            # Sleep until 03:00 UTC
            now = datetime.now(timezone.utc)
            target = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            sleep_seconds = (target - now).total_seconds()
            logger.info("Backup thread: next run at %s (%.0fs)", target.isoformat(), sleep_seconds)
            time.sleep(sleep_seconds)

            try:
                from monitor.backup import run_daily_backups
                results = run_daily_backups()
                for r in results:
                    # Config-skip: run_daily_backups returns a {skipped: True}
                    # marker when RAILWAY_API_TOKEN is unset (an intentional
                    # pause). Log at info level; do NOT fire Sentry: this is a
                    # config state, not a bug.
                    if r.get("skipped"):
                        logger.info(
                            "Daily backups skipped: %s",
                            r.get("reason", "unknown"),
                        )
                        continue
                    if "error" in r:
                        logger.error("Backup failed: %s -> %s", r["app_name"], r["error"])
                        sentry_sdk.capture_message(
                            f"Daily backup failed for {r['app_name']}: {r['error']}",
                            level="error",
                        )
                    else:
                        logger.info("Backup complete: %s -> %s", r["app_name"], r.get("backup_path", "ok"))
            except Exception as exc:
                logger.error("Daily backup run failed: %s", exc)
                sentry_sdk.capture_exception(exc)

    thread = threading.Thread(target=_backup_loop, daemon=True, name="daily-backup")
    thread.start()
    logger.info("Daily backup thread started (runs at 03:00 UTC)")


def _start_ops_agent_thread() -> None:
    """Spawn the ops agent as a daemon thread for self-healing."""
    interval = env_int("OPS_SCAN_INTERVAL", 1800, min_val=1)
    dry_run = safety_switch("OPS_DRY_RUN")

    def _ops_loop():
        try:
            from ops.agent import run_agent
            run_agent(interval_seconds=interval, dry_run=dry_run)
        except Exception as exc:
            logger.error("Ops agent thread crashed: %s", exc)

    thread = threading.Thread(target=_ops_loop, daemon=True, name="ops-agent")
    thread.start()
    mode = "dry-run" if dry_run else "live"
    logger.info("Ops agent thread started (interval: %ds, mode: %s)", interval, mode)


def _start_coordinator_thread() -> None:
    """Spawn the Master Coordinator daemon, replacing the ops agent.

    Gated by COORDINATOR_ENABLED env var. The coordinator runs a tick loop
    that reads system state, detects work, computes budgets, and dispatches
    agents. Respects _shutdown_event for graceful shutdown.
    """
    # The same reader the tick uses (coordinator/config.py): text that is not
    # a whole number of at least one second means the default, never a worker
    # that cannot start or a loop that does not sleep. The admin settings page
    # writes this variable.
    from coordinator.config import tick_interval_s
    interval = tick_interval_s()
    dry_run = safety_switch("COORDINATOR_DRY_RUN")

    def _coordinator_loop():
        try:
            from coordinator.tick import run_tick
            from coordinator.agents.infra_engineer import register as register_infra
            from coordinator.agents.financial_manager import register as register_financial
            from coordinator.agents.customer_service import register as register_customer
            from coordinator.agents.documentation import register as register_docs
            from coordinator.agents.security_engineer import register as register_security
            from coordinator.agents.qa_engineer import register as register_qa
            from coordinator.agents.backend_engineer import register as register_backend
            from coordinator.agents.cost_analyst import register as register_cost
            from coordinator.agents.architect import register as register_architect
            from coordinator.agents.reconciliation import register as register_reconciliation
            register_infra()
            register_financial()
            register_customer()
            register_docs()
            register_security()
            register_qa()
            register_backend()
            register_cost()
            register_architect()
            register_reconciliation()

            tick_number = 0
            previous_summary = None
            # The previous tick's watermark: the database time at which that
            # tick started. A tick looks for rows newer than this, so it must
            # not be the time the tick ended: whatever was written while the
            # tick ran would then be older than the watermark and never seen.
            # None lets run_tick resume from the last tick the database has.
            last_tick_time = None

            logger.info("Coordinator daemon starting (interval=%ds, dry_run=%s)", interval, dry_run)

            while not _shutdown_event.is_set():
                tick_number += 1
                conn = None
                try:
                    conn = _get_conn()
                    summary = run_tick(
                        conn=conn,
                        tick_number=tick_number,
                        previous_summary=previous_summary,
                        last_tick_time=last_tick_time,
                        shutdown_event=_shutdown_event,
                        dry_run=dry_run,
                    )
                    previous_summary = summary
                    # A skipped tick, and one that failed before its work
                    # items were committed, return no watermark: the next tick
                    # then covers the same interval again.
                    if isinstance(summary, dict) and summary.get("watermark") is not None:
                        last_tick_time = summary["watermark"]
                except Exception as exc:
                    logger.error("Coordinator tick %d failed: %s", tick_number, exc, exc_info=True)
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception as _guard_exc:  # closing a connection that is already gone
                            logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)

                # Interruptible sleep until next tick
                if not _shutdown_event.is_set():
                    _shutdown_event.wait(interval)

        except Exception as exc:
            logger.error("Coordinator daemon thread crashed: %s", exc, exc_info=True)

    thread = threading.Thread(target=_coordinator_loop, daemon=True, name="coordinator")
    thread.start()
    mode = "dry-run" if dry_run else "live"
    logger.info("Coordinator thread started (interval: %ds, mode: %s)", interval, mode)


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------

# How many times orphan recovery resumes one build from its on-disk files.
# A resumption does not use up a retry (builds.retry_count is not incremented),
# so it is bounded separately, by counting the build's 'recovery-resume' rows
# in build_logs. An orphan that has to restart from scratch uses a retry
# instead and is bounded by _MAX_RETRIES, like every other requeue.
_MAX_ARTIFACT_RESUMES = 5

# --- Build leases: how orphan recovery tells a dead build from a live one -----
#
# A build row stuck in building/testing/deploying is an orphan only if no live
# worker is running the attempt the row is on. What is owned and leased is
# therefore one ATTEMPT of a build (the build id together with the started_at
# the claim wrote), never the build as such. A build thread is still busy
# after its attempt's row went back to the queue (emails, Railway clean-up);
# with MAX_CONCURRENT_BUILDS of 2 or more the build can be claimed again in
# that window, and the end of the earlier attempt's tail must not release
# anything that covers the later attempt.
#
# Ownership starts at the claim, on the thread that claimed
# (_claim_and_dispatch): the attempt goes into _OWN_ATTEMPTS and a
# session-level Postgres advisory lock for it is taken on a dedicated
# connection, which is then handed to the build thread and kept open until
# that thread is done. The database releases the lock the moment that
# connection or its process dies, so "is anyone still running this attempt?"
# is a lock lookup (pg_locks). Recovery skips builds whose current attempt
#   * is leased by another connection (a live worker, e.g. the old container
#     while a redeploy drains, or a second worker service);
#   * is owned by this process (see _OWN_ATTEMPTS), from the claim until the
#     build thread has finished everything, including what it does after the
#     row left the pipeline statuses;
#   * started less than _ORPHAN_MIN_AGE_S ago (the gap between the claim commit
#     and the lease being taken, which are two statements on two connections).
# A build is claimed only when a pool thread is free to run it at once, so no
# claimed build waits in the pool's queue (see _claim_and_dispatch).
# The lease is best effort, not a guarantee. If it cannot be taken
# (_acquire_build_lease returns None) the build runs anyway, and nothing
# re-checks the lease later, so if the lease connection drops mid-build the
# lock is gone while the build continues. In both cases only _OWN_ATTEMPTS
# still protects the attempt, and that covers this process only: a second
# worker process would treat it as an orphan once it is older than
# _ORPHAN_MIN_AGE_S.
_LEASE_CLASS_ID = 7301002
_ORPHAN_MIN_AGE_S = 30
_ORPHAN_RECOVERY_INTERVAL_S = 120  # how often the poll loop re-runs recovery

# Takes the lease of one attempt. The key is computed by the database, here
# and where recovery looks for it (_ORPHAN_SELECT_SQL uses the same
# expression over the row's own columns), so the two cannot format the
# timestamp differently. Placeholders: lease class id, build id, attempt
# (started_at).
_LEASE_TAKE_SQL = (
    "SELECT pg_try_advisory_lock("
    "%s, hashtext(%s || ':' || to_char(%s::timestamp, 'YYYYMMDDHH24MISSUS')))"
)

# (build id, attempt token) of every attempt this process is running.
_OWN_ATTEMPTS: set = set()
_OWN_ATTEMPTS_LOCK = threading.Lock()


def _own_attempts() -> list:
    """The attempts this process owns, as sorted (build id, attempt) pairs."""
    with _OWN_ATTEMPTS_LOCK:
        return sorted(_OWN_ATTEMPTS, key=lambda pair: (pair[0], str(pair[1])))


def _own_build_ids() -> list:
    """Ids of the builds this process owns at least one attempt of."""
    return sorted({build_id for build_id, _attempt in _own_attempts()})


def _acquire_build_lease(build_id: str, attempt):
    """Hold the advisory-lock lease for one attempt of `build_id` on a dedicated connection.

    `attempt` is the attempt token (builds.started_at as the claim set it).
    Returns the open connection (close it to release the lease), or None if the
    lease could not be taken (the database was unreachable, or another
    connection already holds it). Never raises, and the caller runs the build
    either way: without a lease, orphan recovery in another worker process
    cannot tell that the attempt is live (see the lease notes above).
    """
    lease_conn = None
    try:
        lease_conn = _get_conn(retries=2, base_delay=1.0)
        lease_conn.autocommit = True  # never sit "idle in transaction" while holding the lock
        with lease_conn.cursor() as cur:
            cur.execute(
                _LEASE_TAKE_SQL,
                (_LEASE_CLASS_ID, str(build_id), _attempt_param(attempt)),
            )
            row = cur.fetchone()
        if row and row[0]:
            return lease_conn
        logger.warning("Build %s: lease is already held by another connection", build_id)
    except Exception as exc:
        logger.warning("Build %s: could not take build lease: %s", build_id, exc)
    if lease_conn is not None:
        try:
            lease_conn.close()
        except Exception as _guard_exc:  # closing a connection that is already gone
            logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)
    return None


def _release_build_lease(lease_conn) -> None:
    """Release a lease taken by _acquire_build_lease (closing the connection frees the lock)."""
    if lease_conn is None:
        return
    try:
        lease_conn.close()
    except Exception as _guard_exc:  # closing a connection that is already gone
        logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)


# Orphan recovery reads its candidates with one statement. A build is an orphan
# candidate when
#   * it is in a worker-owned status;
#   * it started more than the minimum age ago (or has no start time);
#   * no connection holds the advisory lease of the attempt the row is on (see
#     _acquire_build_lease; a row with no started_at has no lease key at all);
#   * that attempt is not owned by this process.
# Placeholders, in order: minimum age (seconds), lease class id, then the
# attempts this process owns as two parallel lists (build ids, attempt
# tokens). What happens to each candidate is decided in
# _recover_orphaned_builds; every write there re-checks the status and that
# started_at is still the value read here (the attempt has not changed).
_ORPHAN_SELECT_SQL = """
    SELECT b.id, b.app_name, b.status, b.retry_count, c.email, c.name, b.started_at
      FROM builds b
      LEFT JOIN customers c ON c.id = b.customer_id
     WHERE b.status IN ('building', 'testing', 'deploying')
       AND (b.started_at IS NULL OR b.started_at < NOW() - make_interval(secs => %s))
       AND NOT EXISTS (
           SELECT 1 FROM pg_locks l
            WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 2
              AND l.classid = %s
              AND l.objid = (hashtext(b.id::text || ':' || to_char(b.started_at, 'YYYYMMDDHH24MISSUS')))::oid
              AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
       )
       AND NOT EXISTS (
           SELECT 1 FROM unnest(%s::text[], %s::timestamp[]) AS own(build_id, attempt)
            WHERE own.build_id = b.id::text AND own.attempt = b.started_at
       )
     ORDER BY b.created_at
"""


def _can_resume_build(app_name: str, interrupted_stage: str, build_id: str = "") -> bool:
    """Check if a build has artifacts on disk and can resume from a later stage.

    For this to be True after a worker restart, the builds/ directory must
    persist across restarts (e.g. via a Railway volume mount). Without
    persistent storage it is False and orphan recovery requeues the build
    without a resume marker.

    Args:
        app_name: The app name (used as subdirectory under builds/).
        interrupted_stage: The stage the build was in when interrupted
            ('building', 'testing', 'deploying').
        build_id: The build UUID. Used to construct the per-build directory
            name ``{app_name}-{build_id[:8]}``. Falls back to legacy
            ``{app_name}`` when empty (backward compat).

    Returns:
        True if the build directory exists with the required files to skip
        the build stage.
    """
    dir_name = f"{app_name}-{build_id[:8]}" if build_id else app_name
    build_dir = _BUILDS_ROOT / dir_name
    if not build_dir.is_dir():
        return False

    # Minimum files needed to skip the build stage: package.json + server.js
    has_package = (build_dir / "package.json").exists()
    has_server = (build_dir / "server.js").exists()

    if not (has_package and has_server):
        return False

    # Can only resume testing or deploying stages (building must restart)
    return interrupted_stage in ("testing", "deploying")


def _recover_orphaned_builds(conn) -> int:
    """Requeue, or permanently fail, builds whose worker died.

    When the worker is killed mid-build (for example a redeploy sends SIGTERM
    and then SIGKILL after the drain period), builds are left in building/
    testing/deploying status forever. Recovery runs at startup and then
    periodically from the poll loop, and it never touches a build whose
    current attempt a live worker still leases or this process is running
    (see the lease notes above _acquire_build_lease).

    For each orphan, in this order:
      * The supervisor already spent the build's one free retry
        (supervisor_retry_used): permanent failure.
      * The build's files are still on disk and it was interrupted in testing
        or deploying (_can_resume_build): requeue with a "resuming from <stage>
        stage" marker that _resume_stage reads at the next claim. retry_count
        is not incremented; the number of resumptions is capped at
        _MAX_ARTIFACT_RESUMES, after which the build fails permanently.
      * Otherwise the build is requeued without a marker (at the next claim it
        starts over, or resumes from its stored artifact if it has one) and
        that uses a retry: retry_count + 1, the same counter and the same cap
        (_MAX_RETRIES) as the retry after a pipeline error. With no retries
        left: permanent failure.

    Every permanent failure goes through _fail_build (dead-letter row, guarded
    status flip, customer email). Every requeue is an UPDATE that re-checks
    `status IN (building, testing, deploying)` and that started_at is
    unchanged, so a build that finished, or was requeued and claimed again,
    between the SELECT and the write is not pulled backwards.

    No Railway project is deleted here. The project on an orphan's row is a
    deployment that was announced earlier (see _set_preview_status); whatever
    the dead worker's attempt may have created is recorded nowhere.

    Returns the number of builds requeued.
    """
    owned = _own_attempts()
    with conn.cursor() as cur:
        cur.execute(
            _ORPHAN_SELECT_SQL,
            (_ORPHAN_MIN_AGE_S, _LEASE_CLASS_ID,
             [build_id for build_id, _attempt in owned], [attempt for _build_id, attempt in owned]),
        )
        orphans = cur.fetchall()
    conn.commit()

    admin_email = os.environ.get("ADMIN_EMAIL", "")
    recovered = 0
    for build_id, app_name, interrupted_stage, retry_count, customer_email, customer_name, started_at in orphans:
        build_id = str(build_id)
        retry_count = retry_count or 0

        # The attempt this pass decided about: the started_at it read. A row
        # that something other than the claim put into a pipeline status has
        # none, and says so explicitly; the write then matches only while
        # started_at is still NULL.
        orphan_attempt = started_at if started_at is not None else ROW_WITHOUT_STARTED_AT

        def _fail_permanently(reason: str) -> None:
            _fail_build(
                conn, build_id, reason,
                customer_email or "", customer_name or "", app_name or "", admin_email,
                retry_count=retry_count, category="infra", attempt=orphan_attempt,
            )

        # The supervisor already gave this build its one free retry after a
        # hang, and it has now been interrupted again. Requeueing it once more
        # would go past that limit.
        if _has_prior_timeout_failure(conn, build_id):
            logger.warning(
                "Build %s (%s) already used its supervised retry; failing it "
                "permanently instead of re-queueing",
                build_id, app_name,
            )
            _fail_permanently(
                "Build has prior supervisor timeout failure; not re-queueing "
                "to avoid infinite retry loop across worker restarts."
            )
            continue

        try:
            can_resume = _can_resume_build(app_name, interrupted_stage, build_id=build_id)
        except Exception as exc:
            logger.warning("Error checking resume for build %s: %s; treating as full restart",
                           build_id, exc)
            can_resume = False

        if can_resume:
            # Bound artifact-based resumptions. They do not increment
            # retry_count (no work is lost when resuming), but a build that
            # kills the worker on every resume (for example by running it out
            # of memory) would otherwise loop forever: SIGKILL leaves no trace
            # on the row and retry_count never moves. Count the resume markers
            # written below and, at the cap, fail the build permanently.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM build_logs "
                    "WHERE build_id = %s AND stage = 'recovery-resume'",
                    (build_id,),
                )
                prior_resumes = cur.fetchone()[0]
            if prior_resumes >= _MAX_ARTIFACT_RESUMES:
                logger.warning(
                    "Build %s (%s) hit the artifact-resume cap (%d resumptions); "
                    "marking permanently failed instead of resuming again",
                    build_id, app_name, _MAX_ARTIFACT_RESUMES,
                )
                _fail_permanently(
                    f"Build resumed {prior_resumes} times without completing "
                    f"(>= {_MAX_ARTIFACT_RESUMES} artifact resumptions); failing "
                    f"to avoid an infinite resume loop across worker restarts."
                )
                continue
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE builds
                       SET status = 'queued',
                           error_message = %s,
                           started_at = NULL,
                           completed_at = NULL
                     WHERE id = %s
                       AND status IN ('building', 'testing', 'deploying')
                       AND started_at IS NOT DISTINCT FROM %s
                    """,
                    (
                        _resume_marker(interrupted_stage),
                        build_id,
                        started_at,
                    ),
                )
                requeued = cur.rowcount != 0
                if requeued:
                    # Marker row counted by the cap check above on the next cycle.
                    cur.execute(
                        "INSERT INTO build_logs (build_id, stage, message) VALUES (%s, %s, %s)",
                        (build_id, "recovery-resume", f"resumed from {interrupted_stage} stage"),
                    )
            conn.commit()
            if requeued:
                recovered += 1
                logger.info(
                    "Recovered build %s (%s) for resumption from %s stage "
                    "(resume #%d, retry_count unchanged)",
                    build_id, app_name, interrupted_stage, prior_resumes + 1,
                )
            continue

        # Nothing on disk to resume from: requeue without a marker, which uses
        # one of the build's retries.
        if retry_count >= _MAX_RETRIES:
            logger.warning(
                "Build %s (%s) was interrupted by a worker restart with no retries "
                "left (%d of %d used); failing it permanently",
                build_id, app_name, retry_count, _MAX_RETRIES,
            )
            _fail_permanently(
                f"Build interrupted by a worker restart with no retries left "
                f"({retry_count} of {_MAX_RETRIES} used)"
            )
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE builds
                   SET status = 'queued',
                       error_message = 'Recovered: worker restarted during build',
                       retry_count = COALESCE(retry_count, 0) + 1,
                       started_at = NULL,
                       completed_at = NULL
                 WHERE id = %s
                   AND status IN ('building', 'testing', 'deploying')
                   AND started_at IS NOT DISTINCT FROM %s
                """,
                (build_id, started_at),
            )
            requeued = cur.rowcount != 0
        conn.commit()
        if requeued:
            recovered += 1
            logger.info(
                "Recovered build %s (%s) back to queued (not resumable from disk; "
                "retry %d of %d)",
                build_id, app_name, retry_count + 1, _MAX_RETRIES,
            )

    return recovered


# ---------------------------------------------------------------------------
# Build thread wrapper
# ---------------------------------------------------------------------------

def _own_attempt(build_id: str, attempt) -> None:
    """Mark one attempt of a build as run by this process (orphan recovery skips it)."""
    with _OWN_ATTEMPTS_LOCK:
        _OWN_ATTEMPTS.add((str(build_id), attempt))


def _disown_attempt(build_id: str, attempt) -> None:
    """Give up this process's ownership of that attempt, and of no other."""
    with _OWN_ATTEMPTS_LOCK:
        _OWN_ATTEMPTS.discard((str(build_id), attempt))


def _run_build_in_thread(job: dict, lease_conn=None) -> None:
    """Execute a single build job inside a ThreadPoolExecutor worker thread.

    The attempt is already owned by this process when this runs: the claiming
    thread put it into _OWN_ATTEMPTS and took its lease (`lease_conn`, which
    may be None if the lease could not be taken). This function releases both
    when the job is completely finished, tail included. Both are this
    attempt's alone: a later attempt of the same build that another thread is
    running by then keeps its own.

    This is the **outermost** exception boundary for every build.  It catches
    BaseException (not just Exception) so that:

    * ``SystemExit`` – raised by ``sys.exit(1)`` in a cli module that was
      written as a command (``test.test_app`` exits when the build directory
      is missing, for one).
    * ``KeyboardInterrupt`` – raised if the thread itself receives a keyboard
      signal on some platforms.

    Both are BaseException subclasses, *not* Exception subclasses.  An
    ``except Exception`` handler would silently miss them, leaving the build
    stuck at its last DB status (e.g. ``"deploying"``) forever; a zombie build
    that never gets marked ``"failed"`` and never retries.

    Logging behaviour:
    - ``SystemExit`` is logged at WARNING without a full traceback: the cli
      module printed its reason before it exited.
    - All other ``BaseException`` subclasses are logged at ERROR with a full
      traceback so genuine crashes are not silenced.
    """
    build_conn = None
    # Idempotent: the dispatcher already did this at the claim. Kept so that a
    # direct caller of this function is protected as well.
    _own_attempt(job["id"], job.get("attempt"))
    try:
        build_conn = _get_conn()
        _process_job(job, build_conn)
    except BaseException as exc:
        # SystemExit from sys.exit() in a cli module: that module printed its
        # reason, so this is a WARNING with no traceback.
        if isinstance(exc, SystemExit):
            logger.warning(
                "Build thread for %s exited via sys.exit(%s) (%s) - "
                "a cli module ended the attempt; marking build failed",
                job["id"], exc.code, type(exc).__name__,
            )
        else:
            logger.error(
                "Unhandled %s in build thread for %s: %s",
                type(exc).__name__, job["id"], exc, exc_info=True,
            )
        # Prevent zombie builds: fail the build through the one terminal path
        # (dead-letter row, guarded status flip, customer email) so it is
        # visible and can be retried rather than stuck at its last status.
        fail_conn = None
        try:
            # The pipeline thread may have replaced build_conn (see
            # _ensure_conn), so do not assume it is still usable.
            fail_conn = _ensure_conn(build_conn) if build_conn is not None else _get_conn()
            _fail_build(
                fail_conn, job["id"],
                f"Worker thread crash ({type(exc).__name__}): {exc}",
                job.get("customer_email") or "", job.get("customer_name") or "",
                job.get("app_name") or "", os.environ.get("ADMIN_EMAIL", ""),
                retry_count=job.get("retry_count") or 0,
                attempt=job.get("attempt"),
            )
            # The crashed attempt's own Railway project, if it created one and
            # the build did not reach preview with it, goes with the attempt.
            _discard_unannounced_project(fail_conn, job, "the build thread crashed")
        except Exception as fail_exc:
            logger.error("Build %s: could not record the thread crash: %s", job["id"], fail_exc)
        finally:
            if fail_conn is not None and fail_conn is not build_conn:
                try:
                    fail_conn.close()
                except Exception as _guard_exc:  # closing a connection that is already gone
                    logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)
    finally:
        if build_conn is not None:
            try:
                build_conn.close()
            except Exception as _guard_exc:  # closing a connection that is already gone
                logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)
        _release_build_lease(lease_conn)
        _disown_attempt(job["id"], job.get("attempt"))


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def _recover_orphans_once(reason: str) -> int:
    """Run one orphan-recovery pass on a short-lived connection. Never raises."""
    conn = None
    try:
        conn = _get_conn()
        recovered = _recover_orphaned_builds(conn)
        if recovered:
            logger.info("Recovered %d orphaned build(s) (%s pass)", recovered, reason)
        return recovered
    except Exception as exc:
        logger.error("Orphan recovery (%s pass) failed: %s", reason, exc)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as _guard_exc:  # closing a connection that is already gone
                logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)


def _busy_build_threads(futures: list) -> int:
    """How many dispatched builds have not finished yet (tail included)."""
    return sum(1 for future in futures if not future.done())


def _claim_and_dispatch(executor, futures: list) -> Optional[dict]:
    """One poll: claim the next queued build and hand it to a free pool thread.

    Returns the job that was dispatched, or None when nothing was claimed.

    Two limits apply, and they are different things:
      * _claim_next_job counts builds in a pipeline STATUS, across all
        workers. That is the MAX_CONCURRENT_BUILDS limit.
      * This function counts this process's busy pool THREADS. A build thread
        stays busy after its row left the pipeline statuses: it still sends
        email, runs the janitor and deletes the attempt's Railway project.
        During that tail the status count is already back to zero. Claiming
        then would park the new build in the executor's queue as 'building',
        unleased and unowned, where a recovery pass requeues it and burns one
        of its retries for an attempt that never ran. So nothing is claimed
        unless a thread is free to start the build at once.

    The claimed attempt is owned from here on, on this thread, before it is
    submitted: it is added to _OWN_ATTEMPTS and its lease is taken, and both
    are released by the build thread when it is done (or here, if the build
    could not be handed to the pool).
    """
    if _busy_build_threads(futures) >= _MAX_CONCURRENT:
        logger.debug("All %d build thread(s) busy; not claiming", _MAX_CONCURRENT)
        return None

    conn = _get_conn()
    try:
        job = _claim_next_job(conn)
    finally:
        try:
            conn.close()
        except Exception as _guard_exc:  # closing a connection that is already gone
            logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)
    if job is None:
        return None

    _own_attempt(job["id"], job["attempt"])
    lease_conn = None
    try:
        lease_conn = _acquire_build_lease(job["id"], job["attempt"])
        logger.info("Claimed build %s (%s, tier=%s), dispatching to thread pool",
                    job["id"], job["app_name"], job["tier"])
        futures.append(executor.submit(_run_build_in_thread, job, lease_conn))
    except BaseException:
        # The build was claimed but no thread will run it (the pool is shut
        # down, for instance). Give it up so recovery can requeue it.
        _release_build_lease(lease_conn)
        _disown_attempt(job["id"], job["attempt"])
        raise
    return job


def run_worker() -> None:
    """Run the worker loop continuously.

    Polls the builds table every _POLL_INTERVAL seconds when the queue is
    empty. Handles SIGTERM gracefully. On startup, recovers any builds
    orphaned by a previous crash or Railway redeploy.
    """
    # Registered here, not at import time, so importing this module never
    # changes the host process's signal handling. Must run on the main thread.
    signal.signal(signal.SIGTERM, _handle_sigterm)

    logger.info("Worker started. Polling every %ds when idle. Max concurrent: %d",
                _POLL_INTERVAL, _MAX_CONCURRENT)

    # Write worker heartbeat to DB so deploy verification can confirm worker is up
    try:
        hb_conn = _get_conn()
        git_commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
        coord_enabled = arming_switch("COORDINATOR_ENABLED")
        hb_conn.cursor().execute(
            """INSERT INTO worker_heartbeat (id, started_at, git_commit, coordinator_enabled)
               VALUES (1, NOW(), %s, %s)
               ON CONFLICT (id) DO UPDATE
                 SET started_at = NOW(), git_commit = %s, coordinator_enabled = %s""",
            (git_commit, coord_enabled, git_commit, coord_enabled),
        )
        hb_conn.commit()
        hb_conn.close()
        logger.info("Worker heartbeat recorded (commit=%s)", git_commit[:7] or "local")
    except Exception as exc:
        logger.warning("Failed to write worker heartbeat: %s", exc)

    # Recover builds orphaned by a previous shutdown/crash. Builds still owned
    # by a live worker (for example the old container while it drains) are
    # skipped here and picked up by the periodic pass in the poll loop once
    # their owner is gone.
    _recover_orphans_once("startup")
    _last_recovery = time.monotonic()

    # Sweep orphan Railway projects (not linked to any active build)
    try:
        sweep_conn = _get_conn()
        deleted = _sweep_orphan_railway_projects(sweep_conn)
        if deleted:
            logger.info("Swept %d orphan Railway project(s) on startup", deleted)
        sweep_conn.close()
    except Exception as exc:
        logger.warning("Orphan Railway project sweep failed: %s", exc)

    # Clear accumulated build debris (node_modules, stale dirs, leaked /tmp dirs).
    # This unblocks a full volume on first deploy without manual Railway intervention.
    try:
        _startup_disk_cleanup()
    except Exception as exc:
        logger.warning("Startup disk cleanup failed (non-blocking): %s", exc)

    # Start background threads for monitoring, backups, and ops/coordinator agent
    _start_monitor_thread()
    _start_backup_thread()
    if arming_switch("COORDINATOR_ENABLED"):
        _start_coordinator_thread()
    else:
        _start_ops_agent_thread()

    # Thread pool for concurrent builds. Each build gets its own thread
    # and DB connection. The _active_build_count() check in _claim_next_job()
    # (under an advisory lock) prevents over-claiming across workers, SKIP
    # LOCKED prevents double-claiming, and _claim_and_dispatch claims only
    # when one of these threads is free.
    # See module-level _run_build_in_thread for the BaseException contract.
    _executor = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT, thread_name_prefix="build")
    _futures: list[Future] = []

    # Log first successful poll to confirm DB connectivity from main thread
    _first_poll_logged = False

    logger.info("Main poll loop starting (thread pool, max_workers=%d)", _MAX_CONCURRENT)
    _poll_count = 0

    while not _shutdown_event.is_set():
        conn = None
        _poll_count += 1
        try:
            # Clean up completed futures
            _futures = [f for f in _futures if not f.done()]

            if time.monotonic() - _last_recovery >= _ORPHAN_RECOVERY_INTERVAL_S:
                _recover_orphans_once("periodic")
                _last_recovery = time.monotonic()

            if not _first_poll_logged:
                conn = _get_conn()
                with conn.cursor() as _diag_cur:
                    _diag_cur.execute("SELECT COUNT(*) FROM builds WHERE status = 'queued'")
                    _q_count = _diag_cur.fetchone()[0]
                conn.close()
                conn = None
                logger.info("First poll: DB connected, %d queued build(s) found", _q_count)
                _first_poll_logged = True

            job = _claim_and_dispatch(_executor, _futures)

            if job is None:
                if not _shutdown_event.is_set():
                    # Log every 30th idle poll (~5min) so we know the loop is alive
                    if _poll_count % 30 == 0:
                        active = _busy_build_threads(_futures)
                        logger.info("Poll loop alive, %d cycles, %d active build(s)", _poll_count, active)
                    _shutdown_event.wait(_POLL_INTERVAL)  # interruptible sleep
                continue

            # Brief pause before next claim to let DB status update propagate
            time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Worker shutting down (KeyboardInterrupt).")
            break
        except Exception as exc:
            # Safety net: log and keep looping so a DB blip doesn't kill the worker.
            logger.error("Unhandled exception in worker loop: %s", exc, exc_info=True)
            time.sleep(_POLL_INTERVAL)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as _guard_exc:  # closing a connection that is already gone
                    logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)

    # Graceful shutdown: wait for in-progress builds to finish
    active_count = len([f for f in _futures if not f.done()])
    if active_count:
        logger.info("Waiting for %d active build(s) to finish before exit...", active_count)
    _executor.shutdown(wait=True, cancel_futures=False)
    try:
        sentry_sdk.flush(timeout=5)
    except Exception as _guard_exc:  # Sentry is best effort; reporting must not change the outcome
        logger.debug("Sentry report failed: %s: %s", type(_guard_exc).__name__, _guard_exc)
    logger.info("Worker exited cleanly.")


if __name__ == "__main__":
    run_worker()
