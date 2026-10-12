"""Auto-categorize uncategorized build failures using Claude Haiku.

When a build fails and ops/scanner.classify_error returns 'unknown' (or no
category exists yet), this module pulls the last ~500 chars of build_logs
and asks Claude Haiku to classify the failure into one of 5 categories:
infra / code_error / spec_error / timeout / app_logic.

This module reads (the build log) and calls the model. It writes nothing
itself: the model call is made only after Actuators.authorize_model_call
allows it, and the result is persisted through two gated actuator methods:
  - Actuators.set_build_error_category: builds.error_category (a cache for
    fast lookup on the failed build row)
  - Actuators.record_failure_category: failure_kb.json (the categorization is
    kept with the other learned failure patterns)
Under dry run (the default) none of the three happens and the category stays
'unknown'. The same is true when the tick's model-call allowance does not cover
the call (authorize_model_call then answers no).

Cost: ~$0.001 per call at Haiku 4.5 pricing (assumes <500 input + ~5 output
tokens). Also skipped when ANTHROPIC_API_KEY is absent, so local/test
environments never hit the network.

Trigger: invoked from coordinator.work_detection._check_failed_builds when
the existing regex classifier returns no category, at most
work_detection.MAX_TRIAGE_CALLS_PER_TICK times per tick.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional, Tuple

from coordinator import config
from coordinator.untrusted import UNTRUSTED_NOTICE, wrap_untrusted

logger = logging.getLogger("coordinator.build_failure_categorizer")

# Allowed category labels Haiku must pick from. 'unknown' is the safe
# fallback when the API call fails or returns an unexpected value.
VALID_CATEGORIES: tuple[str, ...] = (
    "infra",
    "code_error",
    "spec_error",
    "timeout",
    "app_logic",
)

# Default Haiku model used for categorization.
_DEFAULT_MODEL = "claude-haiku-4-5"

# Haiku 4.5 list pricing (USD per 1M tokens).
_HAIKU_INPUT_USD_PER_MTOK = 1.00
_HAIKU_OUTPUT_USD_PER_MTOK = 5.00

CATEGORIZATION_SYSTEM_PROMPT = (
    "You classify CAS build failures into ONE of 5 categories.\n\n"
    "Categories:\n"
    "- infra: Infrastructure or deployment issues. Railway/network/DB "
    "connectivity, OOM, container crash, missing env vars, deploy failures "
    "at the platform layer.\n"
    "- code_error: Generated app code is broken. SyntaxError, TypeError, "
    "ReferenceError, undefined variable, import failure, route handler "
    "crash on boot.\n"
    "- spec_error: The build spec is malformed. Validation failed, missing "
    "required fields, malformed JSON, schema rejection before code "
    "generation runs.\n"
    "- timeout: An operation exceeded its time budget. Claude API call "
    "timeout, build stage timeout, health check timeout. NOT a deploy "
    "crash (that is infra).\n"
    "- app_logic: App started fine but behaves wrong. Test assertion "
    "failed, route returns wrong content, missing seed data, CRUD wrong "
    "response.\n\n"
    "Read the build log excerpt (inside <build_log> tags) and respond with "
    "EXACTLY ONE WORD: the category name from the list above. No "
    "punctuation, no explanation, no prose. "
    + UNTRUSTED_NOTICE
)


def _read_text_block(resp) -> str:
    """Extract the first non-empty text block from an Anthropic response."""
    blocks = getattr(resp, "content", None) or []
    for block in blocks:
        text = getattr(block, "text", None)
        if text and text.strip():
            return text.strip()
    return ""


def _normalize_category(raw: str) -> str:
    """Map raw model output to a valid category, or 'unknown'.

    Haiku occasionally adds punctuation, quotes, or a trailing period. We
    strip non-letter characters (keeping underscore so 'code_error' survives)
    and accept the result if it is in VALID_CATEGORIES. As a last resort,
    we scan for any valid category appearing as a substring.
    """
    if not raw:
        return "unknown"
    cleaned = re.sub(r"[^a-z_]", "", raw.lower().strip())
    if cleaned in VALID_CATEGORIES:
        return cleaned
    for cat in VALID_CATEGORIES:
        if cat in cleaned:
            return cat
    return "unknown"


def _estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        (input_tokens / 1_000_000) * _HAIKU_INPUT_USD_PER_MTOK
        + (output_tokens / 1_000_000) * _HAIKU_OUTPUT_USD_PER_MTOK
    )


def categorize_log(
    log_excerpt: str,
    *,
    client=None,
    model: str = _DEFAULT_MODEL,
    max_tokens: int = 20,
) -> Tuple[str, float]:
    """Classify a build log excerpt using Claude Haiku.

    Args:
        log_excerpt: The last ~500 chars of build_logs for a failed build.
        client: Optional anthropic.Anthropic client (for tests). When None,
            lazily constructs from ANTHROPIC_API_KEY. Returns ('unknown',
            0.0) when the key is missing so test/local environments never
            hit the network.
        model: Model id (default Haiku 4.5).
        max_tokens: Output cap; we only need one word so 20 tokens is plenty.

    Returns:
        (category, cost_usd). category is one of VALID_CATEGORIES or
        'unknown'. cost_usd is 0.0 when no API call was made.
    """
    if not log_excerpt or not log_excerpt.strip():
        return ("unknown", 0.0)

    if client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.debug(
                "ANTHROPIC_API_KEY missing; skipping Haiku categorization"
            )
            return ("unknown", 0.0)
        try:
            import anthropic  # type: ignore
        except ImportError:
            logger.warning("anthropic SDK not installed; cannot categorize")
            return ("unknown", 0.0)
        try:
            client = anthropic.Anthropic(api_key=api_key, timeout=config.MODEL_CALL_TIMEOUT_S,
                                         max_retries=config.MODEL_CALL_MAX_RETRIES)
        except Exception as exc:
            logger.warning("Failed to construct Anthropic client: %s", exc)
            return ("unknown", 0.0)

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=CATEGORIZATION_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": wrap_untrusted("build_log", log_excerpt, 1500),
                }
            ],
        )
    except Exception as exc:
        logger.warning("Haiku categorization call failed: %s", exc)
        return ("unknown", 0.0)

    raw = _read_text_block(resp)
    category = _normalize_category(raw)

    usage = getattr(resp, "usage", None)
    in_tokens = getattr(usage, "input_tokens", 0) if usage else 0
    out_tokens = getattr(usage, "output_tokens", 0) if usage else 0
    cost_usd = _estimate_cost_usd(in_tokens, out_tokens)

    logger.info(
        "Haiku categorization: raw=%r -> %s (in=%d out=%d cost=$%.4f)",
        raw,
        category,
        in_tokens,
        out_tokens,
        cost_usd,
    )
    return (category, cost_usd)


def fetch_log_excerpt(
    conn,
    build_id: str,
    *,
    max_chars: int = 500,
    log_row_limit: int = 50,
) -> str:
    """Return the last `max_chars` characters of build_logs for a build.

    Pulls up to `log_row_limit` most recent log rows, joins them in
    chronological order, and truncates to the last `max_chars` characters.
    Returns empty string if no logs exist or the query fails (caller falls
    back to error_message).
    """
    if not build_id:
        return ""
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT message FROM build_logs "
                "WHERE build_id = %s ORDER BY id DESC LIMIT %s",
                (build_id, log_row_limit),
            )
            rows = cur.fetchall()
        finally:
            cur.close()
    except Exception as exc:
        logger.warning(
            "fetch_log_excerpt query failed for %s: %s",
            str(build_id)[:8],
            exc,
        )
        return ""
    if not rows:
        return ""
    # Rows are most-recent-first; reverse to chronological order before join.
    messages = [r[0] for r in reversed(rows) if r and r[0]]
    if not messages:
        return ""
    joined = "\n".join(messages)
    return joined[-max_chars:] if len(joined) > max_chars else joined


def enrich_uncategorized(
    conn,
    fb: dict,
    current_category: Optional[str],
    *,
    actuators,
    client=None,
) -> str:
    """Top-level entry: enrich a failed-build dict with a Haiku category.

    When `current_category` is None or 'unknown', pulls the last 500 chars
    of build_logs (falling back to error_message) and asks Haiku to
    classify. `actuators` (a coordinator.actuators.Actuators, required) gates
    every effect: no model call unless authorize_model_call returns True, and
    the result is persisted to builds.error_category and failure_kb.json
    through set_build_error_category and record_failure_category. A write the
    caller is not permitted to make is skipped; the category is still returned
    so the failure can be routed.

    Returns the (possibly-updated) category. When the call is gated off (dry
    run, missing permission, missing key) or fails, returns the original
    `current_category` or 'unknown'.
    """
    if current_category and current_category != "unknown":
        return current_category

    # Skip when no API key is configured. Avoids cost in test/local env
    # and lets _check_failed_builds run unmodified when offline.
    if client is None and not os.environ.get("ANTHROPIC_API_KEY"):
        return current_category or "unknown"

    build_id = fb.get("id")
    if not build_id:
        return current_category or "unknown"

    try:
        if not actuators.authorize_model_call("build_failure_categorization", _DEFAULT_MODEL):
            return current_category or "unknown"
    except PermissionError as exc:
        logger.info("Haiku categorization not permitted: %s", exc)
        return current_category or "unknown"

    excerpt = fetch_log_excerpt(conn, build_id)
    if not excerpt:
        excerpt = (fb.get("error_message") or "")[:500]
    if not excerpt:
        return current_category or "unknown"

    category, _cost = categorize_log(excerpt, client=client)
    if category == "unknown":
        return current_category or "unknown"

    for persist in (
        lambda: actuators.set_build_error_category(build_id, category),
        lambda: actuators.record_failure_category(fb.get("error_message", "") or excerpt, category),
    ):
        try:
            persist()
        except PermissionError as exc:
            logger.info("Categorization not persisted: %s", exc)
    return category
