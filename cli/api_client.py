"""Shared API client for CraftAndShip build pipeline.

Section 01: ApiCallMetrics dataclass, PRICING table, estimate_cost().
Section 02: call_api() with realtime mode, retry, token continuation.
Section 05: Batch mode, polling, fallback, kill switch.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import anthropic
import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Section 02: Timeout + wall-clock budget constants
# ---------------------------------------------------------------------------

# Explicit httpx.Timeout applied to every Anthropic client built by
# ``make_client``. Bounds each phase of the HTTP exchange so no single
# socket call can block past its cap.
_DEFAULT_TIMEOUT = httpx.Timeout(
    600.0,       # total bound
    connect=10.0,
    read=300.0,
    write=60.0,
    pool=10.0,
)

# Default wall-clock budget for a single ``call_api`` invocation (seconds).
# Bounds compounded retries + continuation calls inside one call_api.
_DEFAULT_WALL_CLOCK_BUDGET_S = 1200.0  # 20 minutes
_WALL_CLOCK_ENV_VAR = "CAS_CALL_API_WALL_CLOCK_S"


# ---------------------------------------------------------------------------
# Section 02: New exceptions
# ---------------------------------------------------------------------------


class CallApiWallClockExceeded(anthropic.APITimeoutError):
    """Raised when call_api exceeds its wall_clock_budget_s.

    Subclasses ``anthropic.APITimeoutError`` so every existing
    ``except APITimeoutError`` handler catches it unchanged. The parent
    class's ``__init__`` expects an ``httpx.Request`` on the installed
    SDK, so we bypass it via ``Exception.__init__`` and carry the
    structured fields as attributes.
    """

    def __init__(self, build_id, model, elapsed_s, budget_s):
        self.build_id = build_id
        self.model = model
        self.elapsed_s = elapsed_s
        self.budget_s = budget_s
        msg = (
            f"call_api wall-clock budget exceeded: "
            f"build_id={build_id} model={model} "
            f"elapsed={elapsed_s:.1f}s budget={budget_s:.1f}s"
        )
        Exception.__init__(self, msg)


class BuildCancelled(Exception):
    """Raised by call_api (and fix loops) when the cancellation
    registry has fired for this build_id. Distinct from APITimeoutError
    so callers can distinguish 'hung' from 'supervisor asked us to stop'.
    """

    def __init__(self, build_id):
        self.build_id = build_id
        super().__init__(f"Build cancelled: build_id={build_id}")


# ---------------------------------------------------------------------------
# Section 02: Shared Anthropic client factory
# ---------------------------------------------------------------------------


def make_client(api_key: str | None = None) -> anthropic.Anthropic:
    """Shared constructor for every Anthropic client in cli/.

    Reads ``ANTHROPIC_API_KEY`` from the environment if ``api_key`` is
    not passed. Applies the module-level ``_DEFAULT_TIMEOUT`` so no HTTP
    phase can block past its cap. Leaves ``max_retries`` at the SDK
    default of 2; the wall-clock budget in ``call_api`` is the real
    bound on compounding retries.

    All Anthropic clients in ``cli/`` MUST be constructed here.
    Enforced by ``tests/test_api_client.py::TestNoBypassStatic`` in §04.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError(
            "ANTHROPIC_API_KEY is not set and no api_key argument was passed"
        )
    return anthropic.Anthropic(api_key=key, timeout=_DEFAULT_TIMEOUT)


@dataclass
class ApiCallMetrics:
    """Captures token usage and cost for a single Anthropic API call."""
    module: str          # "build", "review", "qa", "fix", etc.
    model: str           # "claude-opus-4-6", "claude-haiku-4-5", etc.
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    api_mode: str        # "realtime" or "batch"
    estimated_cost_usd: float
    build_id: str
    stage: str           # "build_phase_1", "review", "qa", "fix_attempt_2", etc.
    timestamp: datetime
    # Advisor fields (defaults for backward compatibility)
    advisor_model: str = ""
    advisor_input_tokens: int = 0
    advisor_output_tokens: int = 0
    advisor_cost_usd: float = 0.0
    executor_cost_usd: float = 0.0


# Pricing last verified: 2026-03-30
# Maps (model, mode, token_type) -> USD per token
PRICING = {
    # Claude Opus 4.6
    ("claude-opus-4-6", "realtime", "input"):  5.00 / 1_000_000,
    ("claude-opus-4-6", "realtime", "output"): 25.00 / 1_000_000,
    ("claude-opus-4-6", "batch", "input"):     2.50 / 1_000_000,
    ("claude-opus-4-6", "batch", "output"):    12.50 / 1_000_000,
    # Claude Sonnet 4.6
    ("claude-sonnet-4-6", "realtime", "input"):  3.00 / 1_000_000,
    ("claude-sonnet-4-6", "realtime", "output"): 15.00 / 1_000_000,
    ("claude-sonnet-4-6", "batch", "input"):     1.50 / 1_000_000,
    ("claude-sonnet-4-6", "batch", "output"):    7.50 / 1_000_000,
    # Claude Haiku 4.5
    ("claude-haiku-4-5", "realtime", "input"):  1.00 / 1_000_000,
    ("claude-haiku-4-5", "realtime", "output"): 5.00 / 1_000_000,
    ("claude-haiku-4-5", "batch", "input"):     0.50 / 1_000_000,
    ("claude-haiku-4-5", "batch", "output"):    2.50 / 1_000_000,
}

# Cache pricing multipliers (applied to realtime input rate regardless of api_mode)
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


def estimate_cost(
    model: str,
    api_mode: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Calculate estimated USD cost for an API call.

    Cache pricing uses the realtime input rate as the base regardless of api_mode.
    Returns 0.0 for unknown model/mode combinations (logs a warning).
    """
    input_rate = PRICING.get((model, api_mode, "input"))
    output_rate = PRICING.get((model, api_mode, "output"))

    if input_rate is None or output_rate is None:
        logger.warning("Unknown model/mode combo: %s/%s", model, api_mode)
        return 0.0

    cost = (input_tokens * input_rate) + (output_tokens * output_rate)

    # Cache pricing based on realtime input rate
    realtime_input_rate = PRICING.get((model, "realtime", "input"), input_rate)
    cost += cache_read_tokens * realtime_input_rate * CACHE_READ_MULTIPLIER
    cost += cache_write_tokens * realtime_input_rate * CACHE_WRITE_MULTIPLIER

    return cost


def estimate_cost_with_advisor(
    executor_model: str,
    advisor_model: str,
    api_mode: str,
    executor_input_tokens: int,
    executor_output_tokens: int,
    advisor_input_tokens: int,
    advisor_output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> tuple:
    """Calculate split billing for an advisor-enabled API call.

    Returns (total_cost, executor_cost, advisor_cost).
    Advisor tokens are always billed at realtime rates (advisor runs server-side).
    Cache tokens are attributed to the executor.
    """
    executor_cost = estimate_cost(
        model=executor_model,
        api_mode=api_mode,
        input_tokens=executor_input_tokens,
        output_tokens=executor_output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    # Advisor always billed at realtime rates regardless of outer api_mode
    advisor_cost = estimate_cost(
        model=advisor_model,
        api_mode="realtime",
        input_tokens=advisor_input_tokens,
        output_tokens=advisor_output_tokens,
    )
    return (executor_cost + advisor_cost, executor_cost, advisor_cost)


def _extract_advisor_usage(response) -> dict:
    """Parse usage.iterations to split executor vs advisor tokens.

    Returns dict with keys: advisor_input_tokens, advisor_output_tokens,
    executor_input_tokens, executor_output_tokens.
    Returns all zeros if iterations is missing or empty.
    """
    result = {
        "advisor_input_tokens": 0,
        "advisor_output_tokens": 0,
        "executor_input_tokens": 0,
        "executor_output_tokens": 0,
    }
    iterations = getattr(getattr(response, "usage", None), "iterations", None)
    if not iterations:
        return result

    def _field(obj, key, default=0):
        # Handle both dict (test mocks) and Pydantic BetaMessageIterationUsage (real SDK)
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    for entry in iterations:
        entry_type = _field(entry, "type", "")
        if entry_type == "advisor_message":
            result["advisor_input_tokens"] += _field(entry, "input_tokens", 0) or 0
            result["advisor_output_tokens"] += _field(entry, "output_tokens", 0) or 0
        else:
            result["executor_input_tokens"] += _field(entry, "input_tokens", 0) or 0
            result["executor_output_tokens"] += _field(entry, "output_tokens", 0) or 0
    return result


def _normalize_advisor_response(response):
    """Strip advisor blocks; concatenate all TextBlock.text values.

    Returns a modified response where content[0].text works for downstream callers.
    Preserves the original response.usage for billing extraction.
    """
    texts = []
    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "text" and hasattr(block, "text"):
            texts.append(block.text)
    concatenated = "".join(texts)
    response.content = [_TextBlock(concatenated)]
    return response


# ---------------------------------------------------------------------------
# Thread-local metrics collector
# ---------------------------------------------------------------------------

_thread_metrics = threading.local()


def start_metrics_collection():
    """Begin collecting ApiCallMetrics for the current thread."""
    _thread_metrics.collecting = True
    _thread_metrics.metrics = []


def get_collected_metrics() -> list:
    """Return all metrics collected since start_metrics_collection() and stop collecting."""
    metrics = getattr(_thread_metrics, "metrics", [])
    _thread_metrics.collecting = False
    _thread_metrics.metrics = []
    return metrics


def _record_metrics(metrics: 'ApiCallMetrics'):
    """Append metrics to thread-local collector if active."""
    if getattr(_thread_metrics, "collecting", False):
        _thread_metrics.metrics.append(metrics)


# ---------------------------------------------------------------------------
# Section 02: call_api(), realtime mode, retry, token continuation
# ---------------------------------------------------------------------------

MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 2
DEFAULT_MAX_TOKENS = 16384

# Section 05: Batch API configuration
MAX_BATCH_WAIT_SECONDS = 300      # 5 minutes
BATCH_POLL_INITIAL_SECONDS = 10   # Starting poll interval
BATCH_POLL_MAX_SECONDS = 120      # Maximum poll interval

TIER_API_CONFIG = {
    "build": {"use_batch": False},   # Realtime for speed (cheapest tier)
    "grow":  {"use_batch": True},    # Batch for savings
    "scale": {"use_batch": True},    # Batch for savings
}


def should_use_batch(tier: str) -> bool:
    """Return whether a given tier should use batch mode."""
    if os.environ.get("DISABLE_BATCH_API", "").lower() == "true":
        return False
    config = TIER_API_CONFIG.get(tier, {"use_batch": False})
    return config["use_batch"]


def _is_retryable(error) -> bool:
    """Check if an Anthropic API error is transient and worth retrying."""
    return isinstance(error, (
        anthropic.RateLimitError,
        anthropic.InternalServerError,
        anthropic.APITimeoutError,
        anthropic.APIConnectionError,
    ))


class CombinedResponse:
    """Synthetic response combining multiple continuation API calls."""

    def __init__(self, text, stop_reason, usage):
        self.content = [_TextBlock(text)]
        self.stop_reason = stop_reason
        self.usage = usage


class _TextBlock:
    def __init__(self, text):
        self.text = text


class _CombinedUsage:
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0

    def add(self, usage):
        self.input_tokens += getattr(usage, "input_tokens", 0)
        self.output_tokens += getattr(usage, "output_tokens", 0)
        self.cache_creation_input_tokens += getattr(usage, "cache_creation_input_tokens", 0)
        self.cache_read_input_tokens += getattr(usage, "cache_read_input_tokens", 0)


def call_api(
    client,
    messages: list,
    model: str,
    system=None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    use_batch: bool = False,
    module: str = "",
    build_id: str = "",
    stage: str = "",
    handle_continuation: bool = True,
    advisor_model: str = None,
    advisor_max_uses: int = 2,
    advisor_caching: dict = None,
    *,
    wall_clock_budget_s: float | None = None,
    **kwargs,
) -> tuple:
    """Shared API call function for all pipeline modules.

    `build_id` is the key the call checks for cancellation between retries.
    The worker passes the attempt's cancel key (it begins with the build id),
    so cancelling one attempt does not reach a later attempt of the same build.

    Returns:
        Tuple of (response, ApiCallMetrics).

    Raises:
        CallApiWallClockExceeded if wall_clock_budget_s is exceeded
            (subclass of anthropic.APITimeoutError: legacy handlers
            keep working).
        BuildCancelled if cli.cancellation fired for this build_id.
        anthropic.APIError subclasses after retry exhaustion.
        ValueError if advisor_model set with handle_continuation=True.
        Never calls sys.exit().
    """
    # ------------------------------------------------------------------
    # Section 02: budget + cancel resolution
    # ------------------------------------------------------------------
    if wall_clock_budget_s is None:
        raw = os.environ.get(_WALL_CLOCK_ENV_VAR)
        try:
            wall_clock_budget_s = (
                float(raw) if raw else _DEFAULT_WALL_CLOCK_BUDGET_S
            )
        except (TypeError, ValueError):
            wall_clock_budget_s = _DEFAULT_WALL_CLOCK_BUDGET_S

    # Resolve cancel event once at entry. Local import avoids circular
    # imports if cli.cancellation ever needs to import api_client.
    from cli import cancellation
    cancel_event = cancellation.get(build_id or None)

    # Use perf_counter (not monotonic) for the wall-clock budget so
    # pre-existing tests that mock ``api_client.time.monotonic`` for
    # the batch polling loop are not perturbed by new entry-time clock
    # reads. Both clocks are monotonic and equally suitable for elapsed
    # measurements.
    start_ts = time.perf_counter()

    def _check_cancelled():
        if cancel_event.is_set():
            raise BuildCancelled(build_id)

    def _check_budget():
        elapsed = time.perf_counter() - start_ts
        if elapsed > wall_clock_budget_s:
            raise CallApiWallClockExceeded(
                build_id=build_id,
                model=model,
                elapsed_s=elapsed,
                budget_s=wall_clock_budget_s,
            )

    # ------------------------------------------------------------------
    # Test hook: gated by BOTH env vars. Used by §07 supervisor tests
    # to simulate a long C-level hang. The wait loop is cancellable so
    # a supervisor firing the cancel event mid-hang unblocks within ~1s.
    # ------------------------------------------------------------------
    if (
        os.environ.get("CAS_ALLOW_TEST_HOOKS") == "1"
        and os.environ.get("ANTHROPIC_SIMULATE_HANG_SECONDS")
    ):
        try:
            hang_seconds = int(os.environ["ANTHROPIC_SIMULATE_HANG_SECONDS"])
        except ValueError:
            hang_seconds = 0
        if hang_seconds > 0:
            logger.warning(
                "[test-hook] simulating hang for %d seconds (build_id=%s)",
                hang_seconds,
                build_id,
            )
            for _ in range(hang_seconds):
                # cancel_event.wait(1.0) makes the hang itself cancellable.
                if cancel_event.wait(1.0):
                    raise BuildCancelled(build_id)

    # Advisor + continuation guard
    if advisor_model and handle_continuation:
        raise ValueError(
            "Advisor mode is not compatible with token continuation. "
            "Set handle_continuation=False or remove advisor_model."
        )

    # Inject advisor tool into kwargs when advisor_model is set
    if advisor_model:
        advisor_tool = {
            "type": "advisor_20260301",
            "name": "advisor",
            "model": advisor_model,
            "max_uses": advisor_max_uses,
        }
        if advisor_caching:
            advisor_tool["caching"] = advisor_caching
        existing_tools = kwargs.get("tools", [])
        kwargs["tools"] = [advisor_tool] + list(existing_tools)

    # Determine if batch mode should be used
    effective_batch = use_batch and not os.environ.get("DISABLE_BATCH_API", "").lower() == "true"
    api_mode = "batch" if effective_batch else "realtime"

    response = None
    if effective_batch:
        try:
            response = _submit_and_poll_batch(
                client, messages, model, system, max_tokens,
                advisor_model=advisor_model, **kwargs,
            )
        except TimeoutError:
            # Watchdog timeout: re-raise for worker to handle
            raise
        except Exception as exc:
            # Batch creation or polling failed; fall back to realtime
            logger.warning("Batch failed, falling back to realtime: %s", exc)
            response = None
            api_mode = "realtime"

    if response is None:
        api_mode = "realtime"
        # Boundary 1: cancel + budget BEFORE initial realtime call
        _check_cancelled()
        _check_budget()
        response = _call_realtime_with_retry(
            client, messages, model, system, max_tokens,
            advisor_model=advisor_model,
            wall_clock_budget_s=wall_clock_budget_s,
            start_ts=start_ts,
            build_id=build_id,
            **kwargs,
        )
        # Boundary 2: cancel + budget immediately after initial call
        # returns. Budget check catches the case where the call itself
        # blocked past the budget even though the pre-sleep fail-fast
        # inside the retry loop never tripped.
        _check_cancelled()
        _check_budget()

    # Extract advisor billing data BEFORE normalization (preserves usage.iterations)
    advisor_usage = None
    if advisor_model:
        advisor_usage = _extract_advisor_usage(response)
        response = _normalize_advisor_response(response)

    # Track combined usage across continuations
    combined_usage = _CombinedUsage()
    combined_usage.add(response.usage)
    all_text = response.content[0].text

    # Token continuation loop (only when advisor is not active)
    if handle_continuation:
        working_messages = list(messages)
        while response.stop_reason == "max_tokens":
            working_messages.append({"role": "assistant", "content": response.content[0].text})
            working_messages.append({
                "role": "user",
                "content": "Continue generating from where you left off. "
                           "Resume with the next --BEGINFILE-- block or finish the current file.",
            })
            # Boundary 3: cancel + budget BEFORE every continuation call
            _check_cancelled()
            _check_budget()
            response = _call_realtime_with_retry(
                client, working_messages, model, system, max_tokens,
                wall_clock_budget_s=wall_clock_budget_s,
                start_ts=start_ts,
                build_id=build_id,
                **kwargs,
            )
            # Boundary 4: cancel + budget immediately after continuation
            # returns, mirroring Boundary 2.
            _check_cancelled()
            _check_budget()
            combined_usage.add(response.usage)
            all_text += "\n" + response.content[0].text

    # Build metrics
    cache_read = getattr(combined_usage, "cache_read_input_tokens", 0)
    cache_write = getattr(combined_usage, "cache_creation_input_tokens", 0)

    if advisor_model and advisor_usage:
        total_cost, exec_cost, adv_cost = estimate_cost_with_advisor(
            executor_model=model,
            advisor_model=advisor_model,
            api_mode=api_mode,
            executor_input_tokens=advisor_usage["executor_input_tokens"],
            executor_output_tokens=advisor_usage["executor_output_tokens"],
            advisor_input_tokens=advisor_usage["advisor_input_tokens"],
            advisor_output_tokens=advisor_usage["advisor_output_tokens"],
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
    else:
        total_cost = estimate_cost(
            model=model,
            api_mode=api_mode,
            input_tokens=combined_usage.input_tokens,
            output_tokens=combined_usage.output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        exec_cost = total_cost
        adv_cost = 0.0

    metrics = ApiCallMetrics(
        module=module,
        model=model,
        input_tokens=combined_usage.input_tokens,
        output_tokens=combined_usage.output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        api_mode=api_mode,
        estimated_cost_usd=total_cost,
        build_id=build_id,
        stage=stage,
        timestamp=datetime.now(),
        advisor_model=advisor_model or "",
        advisor_input_tokens=advisor_usage["advisor_input_tokens"] if advisor_usage else 0,
        advisor_output_tokens=advisor_usage["advisor_output_tokens"] if advisor_usage else 0,
        advisor_cost_usd=adv_cost,
        executor_cost_usd=exec_cost,
    )

    _record_metrics(metrics)

    # If continuation happened, return a combined response
    if handle_continuation and all_text != response.content[0].text:
        final_response = CombinedResponse(
            text=all_text,
            stop_reason=response.stop_reason,
            usage=combined_usage,
        )
        return (final_response, metrics)

    return (response, metrics)


def _call_realtime_with_retry(
    client,
    messages,
    model,
    system,
    max_tokens,
    advisor_model=None,
    wall_clock_budget_s: float | None = None,
    start_ts: float | None = None,
    build_id: str | None = None,
    **kwargs,
):
    """Call client.messages.create with exponential backoff retry.

    When advisor_model is set, uses client.beta.messages.create with the
    advisor beta flag instead of the standard endpoint.

    When ``wall_clock_budget_s`` and ``start_ts`` are supplied, the
    retry loop fails FAST before each backoff sleep if the sleep alone
    would push us past the remaining budget; raising
    ``CallApiWallClockExceeded`` instead of sleeping.
    """
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            call_kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system is not None:
                call_kwargs["system"] = system
            call_kwargs.update(kwargs)

            if advisor_model:
                call_kwargs["betas"] = ["advisor-tool-2026-03-01"]
                return client.beta.messages.create(**call_kwargs)
            else:
                return client.messages.create(**call_kwargs)
        except anthropic.APIError as error:
            last_error = error
            if _is_retryable(error) and attempt < MAX_RETRIES - 1:
                wait = INITIAL_BACKOFF_SECONDS * (2 ** attempt)
                # Pre-sleep fail-fast: if sleeping would blow the
                # wall-clock budget, raise instead of napping.
                if (
                    wall_clock_budget_s is not None
                    and start_ts is not None
                ):
                    elapsed = time.perf_counter() - start_ts
                    if elapsed + wait > wall_clock_budget_s:
                        raise CallApiWallClockExceeded(
                            build_id=build_id,
                            model=model,
                            elapsed_s=elapsed,
                            budget_s=wall_clock_budget_s,
                        )
                time.sleep(wait)
                continue
            raise
    raise last_error  # Should not reach here, but safety net


# ---------------------------------------------------------------------------
# Section 05: Batch mode, submit, poll, cancel, fallback
# ---------------------------------------------------------------------------

def _submit_and_poll_batch(client, messages, model, system, max_tokens, advisor_model=None, **kwargs):
    """Submit a single-request batch and poll for completion.

    Returns the message response on success, None on timeout (signals fallback).
    Raises TimeoutError if watchdog fires (for worker to handle).
    When advisor_model is set, uses client.beta.messages.batches endpoints.
    """
    # Build batch request params
    params = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system is not None:
        params["system"] = system
    params.update(kwargs)

    # Select batch endpoint (beta when advisor is active)
    if advisor_model:
        batch_api = client.beta.messages.batches
    else:
        batch_api = client.messages.batches

    batch = batch_api.create(
        requests=[{"custom_id": "req_0", "params": params}]
    )
    batch_id = batch.id

    # Poll with exponential backoff
    poll_interval = BATCH_POLL_INITIAL_SECONDS
    start = time.monotonic()

    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= MAX_BATCH_WAIT_SECONDS:
                # Timeout: cancel and signal fallback
                try:
                    batch_api.cancel(batch_id)
                except Exception:
                    pass
                return None

            time.sleep(poll_interval)
            poll_interval = min(poll_interval * 2, BATCH_POLL_MAX_SECONDS)

            try:
                batch_status = batch_api.retrieve(batch_id)
            except anthropic.APIConnectionError:
                # Network blip during polling: retry poll, not the batch
                continue

            if batch_status.processing_status == "ended":
                return _extract_batch_result(batch_api, batch_id)

    except TimeoutError:
        # Watchdog fired: cancel batch and re-raise
        try:
            batch_api.cancel(batch_id)
        except Exception:
            pass
        raise


def _extract_batch_result(batch_api, batch_id):
    """Extract the single result from a completed batch."""
    for result in batch_api.results(batch_id):
        if result.result.type == "succeeded":
            return result.result.message
        else:
            # Batch result was an error: raise it so caller can handle
            error_msg = getattr(result.result, "error", {})
            raise RuntimeError(f"Batch result error: {error_msg}")
    raise RuntimeError("Batch completed but no results found")
