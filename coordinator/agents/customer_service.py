# coordinator/agents/customer_service.py: Customer Service agent
#
# Handles beta feedback triage and support KB gap detection.
# Triage is keyword-based (no model call). The only model call is the Haiku
# draft of a cached support answer for a detected KB gap; that draft is
# validated (coordinator.support_knowledge.kb_rejection_reason) before it is
# stored, because support_kb answers are served to other customers.

import logging
import os

from coordinator import config
from coordinator.agents.base import Agent, AgentResult, Effects
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators

logger = logging.getLogger("coordinator.agents.customer_service")

# Cap how many KB-gap answers we draft per agent_initiative run. Each
# answer costs one Haiku call; a flood of unanswered questions in a week
# shouldn't blow the daily budget.
_KB_GAP_DRAFT_CAP_PER_RUN = 5

# Truncate the question used to look up an existing KB match. Mirrors the
# 50-char prefix used elsewhere in this module so dedupe stays consistent
# between detection and post-insert lookups.
_KB_LOOKUP_PREFIX_LEN = 50


class CustomerService(Agent):
    name = "customer_service"
    description = "Triages customer feedback, updates support KB, drafts responses"
    requires_review = False
    # Both categories can draft a support answer, which is a model call.
    # Checked against the source by tests/test_coordinator_model_free_tasks.py.
    model_categories = frozenset({"customer_feedback", "agent_initiative"})

    def execute(self, task: WorkItem, state: WorldState, model: str,
                actuators: Actuators) -> AgentResult:
        category = task.category

        if category == "customer_feedback":
            return self._handle_feedback(task.context, actuators, model)
        elif category == "agent_initiative":
            return self._handle_initiative(task.context, actuators, model)
        else:
            return AgentResult(
                success=False,
                output={"error": f"Unknown category: {category}"},
                learnings=[f"CustomerService received unhandled category: {category}"],
            )

    def _handle_feedback(self, context: dict, actuators: Actuators,
                         model: str) -> AgentResult:
        """Triage feedback: classify severity, check if it maps to a KB gap."""
        feedback_id = context.get("feedback_id", "")
        feedback_category = context.get("feedback_category", "")
        message_preview = context.get("message_preview", "")

        # Read full feedback + check existing KB
        try:
            feedback_rows = actuators.query_readonly(
                "SELECT bf.message, bf.category, bf.page_url, c.email, c.name "
                "FROM beta_feedback bf "
                "JOIN customers c ON c.id = bf.customer_id "
                "WHERE bf.id = %s",
                (feedback_id,),
            )
        except Exception:
            feedback_rows = []

        if not feedback_rows:
            return AgentResult(
                success=False,
                output={"error": f"Feedback {feedback_id} not found"},
            )

        feedback = feedback_rows[0]
        message = feedback.get("message", message_preview)
        category = feedback.get("category", feedback_category)
        page_url = feedback.get("page_url", "")
        customer_name = feedback.get("name", "")

        # Check if this maps to a support KB question
        kb_match = []
        try:
            kb_match = actuators.query_readonly(
                "SELECT question, answer FROM support_kb "
                "WHERE question_normalized ILIKE %s "
                "ORDER BY usage_count DESC LIMIT 1",
                (f"%{message[:50]}%",),
            )
        except Exception as lookup_error:
            logger.warning("support KB lookup failed, answering without it: %s", lookup_error)

        # Classify severity based on category and content
        severity = "low"
        if category == "bug":
            severity = "high" if any(w in message.lower() for w in
                                      ["crash", "error", "broken", "stuck", "lost", "can't"]) else "medium"
        elif category == "suggestion":
            severity = "low"
        elif category == "question":
            severity = "medium" if not kb_match else "low"

        # For bugs, send an internal alert
        effects = Effects()
        if severity in ("high", "medium") and category == "bug":
            effects.alert(
                actuators,
                f"[CAS] {severity.upper()} feedback: {category} from {customer_name}",
                f"Customer: {customer_name}\n"
                f"Category: {category}\n"
                f"Page: {page_url}\n"
                f"Message: {message}\n\n"
                f"KB match: {'Yes' if kb_match else 'No'}",
            )

        # If question with no KB match, flag as KB gap
        kb_gap = category == "question" and not kb_match
        learnings = []
        kb_drafted = False
        if kb_gap:
            learnings.append(f"Support KB gap: customer asked about '{message[:80]}' with no matching entry")
            # Close the loop: draft an answer with Haiku and INSERT the
            # Q&A pair into support_kb so that, once an operator approves
            # it, the next keeper who asks the same thing gets the cached
            # answer immediately.
            kb_drafted = self._draft_and_record_kb_answer(
                question=message, model=model, actuators=actuators,
            )
            if kb_drafted:
                learnings.append(
                    f"Closed support KB gap by drafting answer for: '{message[:80]}'"
                )
        if severity == "high":
            learnings.append(f"High-severity bug report from {customer_name}: {message[:100]}")
        learnings.extend(effects.learnings())

        return AgentResult(
            success=True,
            output={
                "feedback_id": feedback_id,
                "severity": severity,
                "category": category,
                "kb_match": bool(kb_match),
                "kb_gap": kb_gap,
                "kb_drafted": kb_drafted,
                "customer": customer_name,
                "action": effects.action("alert", "alert") if effects.entries else "classified",
            },
            actions_taken=actuators.actions,
            effects=effects.entries,
            learnings=learnings,
            escalate=severity == "high",
            escalation_reason=f"High-severity bug from {customer_name}: {message[:100]}" if severity == "high" else "",
        )


    def _handle_initiative(self, context: dict,
                           actuators: Actuators,
                           model: str = "claude-haiku-4-5") -> AgentResult:
        """Proactive customer service review: feedback patterns, KB gaps, pain points.

        Scans recent beta feedback for trends (high-severity bugs piling up,
        questions with no KB answer, repeated page/category signals) and surfaces
        them as learnings so the self-learning loop has signal to act on.
        """
        findings = []
        learnings = []

        # Recent feedback volume + category breakdown
        try:
            recent = actuators.query_readonly(
                "SELECT category, message, page_url "
                "FROM beta_feedback "
                "WHERE created_at > NOW() - INTERVAL '7 days' "
                "ORDER BY created_at DESC LIMIT 100"
            )
        except Exception as exc:
            recent = []
            findings.append(f"Feedback query error: {exc}")

        total = len(recent)
        bugs = [r for r in recent if r.get("category") == "bug"]
        questions = [r for r in recent if r.get("category") == "question"]
        suggestions = [r for r in recent if r.get("category") == "suggestion"]

        # High-severity bug signal
        high_sev_keywords = ("crash", "error", "broken", "stuck", "lost", "can't")
        high_sev_bugs = [
            b for b in bugs
            if any(w in (b.get("message") or "").lower() for w in high_sev_keywords)
        ]
        if high_sev_bugs:
            findings.append(
                f"{len(high_sev_bugs)} high-severity bug reports in last 7 days"
            )
            learnings.append(
                f"Customer service pattern: {len(high_sev_bugs)} high-severity bugs "
                f"(crash/error/broken/stuck) in last 7 days"
            )

        # KB gap signal: questions without a KB match. When we find a
        # gap, also CLOSE it: draft a Haiku-grounded answer and INSERT
        # into support_kb so that, once an operator approves it, the next
        # keeper who asks the same thing gets a cached response. Capped at _KB_GAP_DRAFT_CAP_PER_RUN
        # per agent_initiative tick to bound cost.
        kb_gap_count = 0
        kb_drafted_count = 0
        sample_gap = ""
        for q in questions:
            full_msg = (q.get("message") or "").strip()
            msg = full_msg[:_KB_LOOKUP_PREFIX_LEN]
            if not msg:
                continue
            try:
                match = actuators.query_readonly(
                    "SELECT question FROM support_kb "
                    "WHERE question_normalized ILIKE %s LIMIT 1",
                    (f"%{msg}%",),
                )
            except Exception:
                match = []
            if not match:
                kb_gap_count += 1
                if not sample_gap:
                    sample_gap = msg

                if kb_drafted_count < _KB_GAP_DRAFT_CAP_PER_RUN and full_msg:
                    if self._draft_and_record_kb_answer(
                        question=full_msg, model=model, actuators=actuators,
                    ):
                        kb_drafted_count += 1

        if kb_gap_count:
            findings.append(
                f"{kb_gap_count} customer questions with no KB match "
                f"(e.g. '{sample_gap}')"
            )
            learnings.append(
                f"Support KB gap: {kb_gap_count} unanswered questions in last 7 days"
            )
            if kb_drafted_count:
                findings.append(
                    f"Drafted {kb_drafted_count} support_kb answers to close the loop"
                )
                learnings.append(
                    f"Closed {kb_drafted_count} of {kb_gap_count} KB gaps "
                    "by drafting cached answers"
                )

        # Pain-point signal: repeated page_url in bug reports
        page_counts: dict[str, int] = {}
        for b in bugs:
            url = b.get("page_url") or ""
            if url:
                page_counts[url] = page_counts.get(url, 0) + 1
        hotspot_pages = [(u, c) for u, c in page_counts.items() if c >= 3]
        if hotspot_pages:
            hotspot_pages.sort(key=lambda x: x[1], reverse=True)
            top = hotspot_pages[0]
            findings.append(
                f"Bug hotspot: {top[0]} has {top[1]} bug reports in last 7 days"
            )
            learnings.append(
                f"Customer pain point: page {top[0]} has {top[1]} recent bug reports"
            )

        effects = Effects()
        if findings:
            effects.alert(
                actuators,
                "[CAS Customer Service] Weekly feedback review",
                "Findings:\n" + "\n".join(f"- {f}" for f in findings),
            )
        learnings.extend(effects.learnings())

        if not learnings:
            learnings.append(
                f"Customer service initiative: reviewed {total} recent feedback items, "
                "no actionable patterns detected"
            )

        return AgentResult(
            success=True,
            output={
                "initiative": "feedback_pattern_review",
                "feedback_reviewed": total,
                "bugs": len(bugs),
                "questions": len(questions),
                "suggestions": len(suggestions),
                "high_severity_bugs": len(high_sev_bugs),
                "kb_gap_count": kb_gap_count,
                "kb_drafted_count": kb_drafted_count,
                "bug_hotspots": [{"page": u, "count": c} for u, c in hotspot_pages],
                "findings": findings,
            },
            actions_taken=actuators.actions,
            effects=effects.entries,
            learnings=learnings,
        )

    def _draft_and_record_kb_answer(self, question: str, model: str,
                                    actuators: Actuators) -> bool:
        """Draft a Haiku-grounded answer for a KB gap and INSERT it into
        support_kb. Returns True when the actuator accepted the insert, and
        False under dry run (no draft is requested and nothing is stored).

        Failures (no API key, transport error, empty draft, permission
        denied, DB unavailable) are swallowed and logged so the surrounding
        feedback-triage flow keeps succeeding even when the loop-closing
        side effect can't run.
        """
        from coordinator.support_knowledge import MAX_KB_QUESTION_CHARS
        if len(question.strip()) > MAX_KB_QUESTION_CHARS:
            # support_kb_insert would reject it; do not pay for the draft.
            logger.info("KB draft skipped: question longer than %d chars", MAX_KB_QUESTION_CHARS)
            return False

        # The draft is a model call: make it only when the actuator layer
        # allows one (never under dry run, where nothing would be stored).
        try:
            if not actuators.authorize_model_call("support_kb_draft", model or "claude-haiku-4-5"):
                return False
        except PermissionError as exc:
            logger.warning("KB draft blocked by permission gate: %s", exc)
            return False

        try:
            answer = self._draft_kb_answer(question, model)
        except Exception as exc:
            logger.warning("KB draft failed for %r: %s", question[:80], exc)
            return False

        if not answer:
            return False

        try:
            return actuators.support_kb_insert(question, answer)
        except PermissionError as exc:
            logger.warning(
                "support_kb_insert blocked by permission gate: %s", exc
            )
            return False
        except Exception as exc:
            logger.warning("support_kb_insert raised: %s", exc)
            return False

    def _draft_kb_answer(self, question: str, model: str) -> str | None:
        """Call Anthropic Haiku with the question grounded in the support
        knowledge corpus. Returns the drafted answer text, or None when
        the call cannot be made (no API key, missing SDK, transport error,
        empty response). Callers MUST treat None as "skip the insert."

        Override this method in tests to avoid real API calls.
        """
        if not question or not question.strip():
            return None

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None

        try:
            import anthropic  # type: ignore
        except ImportError:
            logger.warning("anthropic SDK not installed; cannot draft KB answer")
            return None

        try:
            from coordinator.support_knowledge import generate_knowledge
            knowledge = generate_knowledge()
        except Exception as exc:
            logger.warning("generate_knowledge() failed: %s", exc)
            return None

        from coordinator.support_knowledge import (
            MAX_KB_QUESTION_CHARS, SUPPORT_EMAIL,
        )
        from coordinator.untrusted import NO_LINKS_NOTICE, UNTRUSTED_NOTICE, wrap_untrusted

        system_prompt = (
            "You are drafting a cached support-chat answer for Craft & Ship.\n"
            "Answer ONLY using facts from the knowledge below. If the "
            "knowledge doesn't cover the question, write a brief reply "
            f"pointing the customer to {SUPPORT_EMAIL}. Keep the "
            "answer to 4 short sentences or fewer. Do not use em dashes; "
            "use commas, semicolons, or periods. Speak in the affirmative; "
            "describe what Craft & Ship does, not what it isn't.\n"
            "The customer's question arrives inside <customer_question> tags. "
            f"{UNTRUSTED_NOTICE} {NO_LINKS_NOTICE}\n\n"
            "## Knowledge\n"
            f"{knowledge}"
        )
        user_content = wrap_untrusted(
            "customer_question", question.strip(), MAX_KB_QUESTION_CHARS)

        try:
            client = anthropic.Anthropic(api_key=api_key, timeout=config.MODEL_CALL_TIMEOUT_S,
                                         max_retries=config.MODEL_CALL_MAX_RETRIES)
            resp = client.messages.create(
                model=model or "claude-haiku-4-5",
                max_tokens=400,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
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


def register():
    from coordinator.tick import register_agent
    register_agent("customer_service", CustomerService)
