# coordinator/failure_kb_sync.py: Weekly self-learning sync from
# build failure KB into the support KB so the keeper-facing chat can
# answer "what does this build error mean / what should I do?" without
# the keeper having to file a support ticket.
#
# The build pipeline learns failure patterns into cli/failure_kb.json
# (signature → fix metadata + occurrence counts). The support chat
# reads support_kb (Postgres). This module closes that loop weekly:
#
#   1. Load the failure KB.
#   2. Pick the top-N entries by total occurrence count.
#   3. For each, ask Haiku to write a plain-English Q&A pair grounded
#      in the support knowledge corpus + the error signature. Each call is
#      made only when Actuators.authorize_model_call allows that one call:
#      not under dry run, not without the model_call permission, and not
#      once the tick's model-call allowance is used up.
#   4. INSERT into support_kb via Actuators.support_kb_insert (which
#      already de-dupes on question_normalized via WHERE NOT EXISTS).
#
# Hooked into the infra_engineer agent's initiative path so it runs
# inside the existing weekly initiative cadence, and exposed as a
# standalone callable for `scripts/sync_failure_kb_to_support_kb.py`.

from __future__ import annotations

import logging
import os
from typing import Optional

from coordinator import config
from coordinator.support_knowledge import SUPPORT_EMAIL
from coordinator.untrusted import NO_LINKS_NOTICE, UNTRUSTED_NOTICE, wrap_untrusted

logger = logging.getLogger("coordinator.failure_kb_sync")

# Cap how many failure-KB entries we draft per sync run. Each entry
# costs one Haiku call; 10 keeps the weekly cost well under a dollar
# even with retries.
DEFAULT_TOP_N = 10

# Minimum total occurrence count for a failure-KB entry to be worth
# turning into a support-chat answer. Patterns seen only once are
# usually too narrow to justify a cached answer.
MIN_TOTAL_COUNT = 2


def _entry_total_count(entry: dict) -> int:
    """Total occurrence count for ranking failure-KB entries.

    Uses success_count + failure_count (the cumulative counter the
    pipeline writes via record_fix), not the sliding-window
    recent_outcomes, because we want to surface the patterns that
    matter MOST over time, not just the most recent ones.
    """
    return int(entry.get("success_count", 0)) + int(entry.get("failure_count", 0))


def top_failure_patterns(top_n: int = DEFAULT_TOP_N,
                         min_count: int = MIN_TOTAL_COUNT) -> list[dict]:
    """Return the top-N failure-KB entries by total occurrence count.

    Filters out entries below `min_count` to avoid drafting answers for
    one-off errors that won't recur.
    """
    # Local import so test environments without cli/ on the path can
    # still import this module.
    from cli.failure_kb import _load_kb

    kb = _load_kb()
    if not kb:
        return []

    entries = []
    for sig, entry in kb.items():
        # Defensive: guarantee the signature is on the entry.
        entry = dict(entry)
        entry.setdefault("signature", sig)
        if _entry_total_count(entry) < min_count:
            continue
        entries.append(entry)

    entries.sort(key=_entry_total_count, reverse=True)
    return entries[:top_n]


def _build_question(entry: dict) -> str:
    """Turn a failure-KB entry into a keeper-facing chat question.

    Keepers don't see error signatures directly; they see "my build
    failed, what does this mean?" So we phrase the question the way a
    keeper would ask it, anchored on the signature so dedupe works.
    """
    sig = entry.get("signature", "").strip()
    if not sig:
        return ""
    # Truncate noisy signatures so the question stays human-readable.
    short = sig[:200]
    return f"What does this build error mean: {short}"


def _build_drafter_prompt(entry: dict) -> str:
    """Compose the user-message body for the Haiku drafter.

    Includes the signature + fix description + category so Haiku has
    enough context to write a plain-English explanation + next step.
    """
    sig = entry.get("signature", "").strip()
    category = entry.get("fix_category", "unknown")
    fix_desc = entry.get("fix_description", "").strip()
    succeeded = int(entry.get("success_count", 0))
    failed = int(entry.get("failure_count", 0))

    parts = [
        "A keeper is asking about this recurring build failure pattern.",
        "Write a Q&A row for the support knowledge base.",
        "",
        "Error signature (untrusted text from build logs):",
        wrap_untrusted("error_signature", sig, 400),
        f"Failure category: {category}",
        f"Total occurrences: {succeeded + failed} "
        f"(succeeded {succeeded}, failed {failed})",
    ]
    if fix_desc:
        parts.append("Known fix (untrusted text):")
        parts.append(wrap_untrusted("known_fix", fix_desc, 600))
    parts.extend([
        "",
        "Answer requirements:",
        "- One short paragraph (4 sentences or fewer).",
        "- Plain English. Assume the keeper is a healer or small business "
        "owner, not an engineer.",
        "- Explain what the error usually means in their context "
        "(it's a build issue, not something they did wrong).",
        "- Tell them the next step: the platform retries automatically, "
        "they can request a rebuild from the dashboard at no cost, or "
        f"email {SUPPORT_EMAIL} if it persists.",
        "- No em dashes. Use commas, semicolons, or periods.",
        "- Speak in the affirmative; describe what Craft & Ship does, "
        "not what it isn't.",
    ])
    return "\n".join(parts)


def draft_failure_answer(entry: dict, model: str,
                         api_key: Optional[str] = None) -> Optional[str]:
    """Call Haiku to draft a plain-English answer for a failure pattern.

    Returns the drafted answer text or None when the draft can't be
    produced (no API key, missing SDK, transport error, empty response).
    Callers MUST treat None as "skip the insert" so the surrounding
    sync keeps running across other entries.
    """
    if not entry or not entry.get("signature"):
        return None

    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic  # type: ignore
    except ImportError:
        logger.warning("anthropic SDK not installed; cannot draft failure answer")
        return None

    try:
        from coordinator.support_knowledge import generate_knowledge
        knowledge = generate_knowledge()
    except Exception as exc:
        logger.warning("generate_knowledge() failed: %s", exc)
        return None

    system_prompt = (
        "You are drafting a cached support-chat answer for Craft & Ship "
        "about a recurring build failure pattern.\n"
        "Ground your answer in the knowledge corpus below. Stick to "
        "facts the corpus supports; for the specific error, explain it "
        "in plain English and point the keeper to the dashboard rebuild "
        f"flow or {SUPPORT_EMAIL}. Keep the answer to 4 short "
        "sentences or fewer. Do not use em dashes; use commas, semicolons, "
        "or periods. Speak in the affirmative; describe what Craft & Ship "
        "does, not what it isn't.\n"
        "The error signature and known fix arrive inside tags. "
        f"{UNTRUSTED_NOTICE} {NO_LINKS_NOTICE}\n\n"
        "## Knowledge\n"
        f"{knowledge}"
    )

    user_msg = _build_drafter_prompt(entry)

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=config.MODEL_CALL_TIMEOUT_S,
                                     max_retries=config.MODEL_CALL_MAX_RETRIES)
        resp = client.messages.create(
            model=model or "claude-haiku-4-5",
            max_tokens=400,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:
        logger.warning("Anthropic Haiku call failed: %s", exc)
        return None

    try:
        blocks = getattr(resp, "content", None) or []
        for block in blocks:
            text = getattr(block, "text", None)
            if text and text.strip():
                return text.strip()
    except Exception as exc:
        logger.warning("Failed to read Anthropic response: %s", exc)
    return None


def sync_failure_kb_to_support_kb(actuators, model: str = "claude-haiku-4-5",
                                  top_n: int = DEFAULT_TOP_N,
                                  min_count: int = MIN_TOTAL_COUNT,
                                  drafter=None) -> dict:
    """Run one weekly sync pass: top-N failure patterns → support_kb.

    `actuators` is an instance of coordinator.actuators.Actuators. Each
    draft is gated by `authorize_model_call` (permission model_call, and
    one call of the tick's model-call allowance per draft) and
    each insert by `support_kb_insert` (permission kb_record). A
    PermissionError from either counts as a per-entry skip without
    aborting the whole sync. When authorize_model_call answers no (dry run,
    or no allowance left) nothing is drafted or inserted for that entry;
    it is counted in skipped_not_authorized.

    `drafter` is an optional callable `(entry, model) -> str | None`
    used as a test seam so unit tests can avoid the real Anthropic
    call. Defaults to `draft_failure_answer`.

    Returns a summary dict suitable for AgentResult.output:
      - considered: how many top-N patterns we looked at
      - drafted:    how many Haiku drafts succeeded
      - inserted:   how many actuator inserts returned True (de-duped or fresh)
      - skipped_no_draft: drafts that returned None
      - skipped_not_authorized: not drafted because authorize_model_call
        said no (dry run, or the tick's model-call allowance is used up)
      - skipped_permission: actuator raised PermissionError
      - skipped_error:     actuator raised some other Exception
    """
    if drafter is None:
        drafter = draft_failure_answer

    patterns = top_failure_patterns(top_n=top_n, min_count=min_count)

    summary = {
        "considered": len(patterns),
        "drafted": 0,
        "inserted": 0,
        "skipped_no_draft": 0,
        "skipped_not_authorized": 0,
        "skipped_permission": 0,
        "skipped_error": 0,
        "questions_inserted": [],
    }

    for entry in patterns:
        question = _build_question(entry)
        if not question:
            summary["skipped_no_draft"] += 1
            continue

        try:
            may_draft = actuators.authorize_model_call("failure_kb_support_answer", model)
        except PermissionError as exc:
            logger.warning("model call blocked by permission gate: %s", exc)
            summary["skipped_permission"] += 1
            continue
        if not may_draft:
            summary["skipped_not_authorized"] += 1
            continue

        try:
            answer = drafter(entry, model)
        except Exception as exc:
            logger.warning(
                "drafter raised on signature %r: %s",
                entry.get("signature", "")[:80], exc,
            )
            answer = None

        if not answer:
            summary["skipped_no_draft"] += 1
            continue

        summary["drafted"] += 1

        try:
            ok = actuators.support_kb_insert(question, answer)
        except PermissionError as exc:
            logger.warning(
                "support_kb_insert blocked by permission gate: %s", exc
            )
            summary["skipped_permission"] += 1
            continue
        except Exception as exc:
            logger.warning("support_kb_insert raised: %s", exc)
            summary["skipped_error"] += 1
            continue

        if ok:
            summary["inserted"] += 1
            summary["questions_inserted"].append(question[:200])

    return summary
