# coordinator/agents/infra_engineer.py: Infra Engineer agent
#
# A rule-based handler, not a planner: it classifies a failure with the regex
# classifier in ops/scanner.py, looks the category up in the fixed playbook
# table in ops/playbook.py, and runs that playbook's fixer through
# Actuators.run_playbook (which decides whether the run is live). The other
# categories send an alert email with a few read-only counts. Its one model
# use is the weekly failure-KB sync (coordinator/failure_kb_sync.py), a
# cheap-tier draft per KB entry, made only when Actuators.authorize_model_call
# allows it.

import logging
import sys
from pathlib import Path

from coordinator.agents.base import Agent, AgentResult, Effects
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators

# Ensure ops/ is importable
_ROOT = str(Path(__file__).parent.parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logger = logging.getLogger("coordinator.agents.infra_engineer")


class InfraEngineer(Agent):
    name = "infra_engineer"
    description = "Handles build failures, app downtime, stuck builds, and backup issues"
    requires_review = True  # Code changes need security review
    # Only the weekly initiative reaches a model call (the failure-KB sync);
    # the other categories are model-free. Checked against the source by
    # tests/test_coordinator_model_free_tasks.py.
    model_categories = frozenset({"agent_initiative"})

    def execute(self, task: WorkItem, state: WorldState, model: str,
                actuators: Actuators) -> AgentResult:
        """Dispatch infrastructure fixes using the existing ops playbook system."""
        category = task.category
        context = task.context

        if category == "build_failure":
            return self._handle_build_failure(context, actuators)
        elif category == "app_down":
            return self._handle_app_down(context, actuators, state)
        elif category == "backup_overdue":
            return self._handle_backup_overdue(context, actuators)
        elif category == "coordinator_stalled":
            return self._handle_coordinator_stalled(context, actuators)
        elif category == "agent_initiative":
            return self._handle_initiative(context, actuators)
        else:
            return AgentResult(
                success=False,
                output={"error": f"Unknown category: {category}"},
                learnings=[f"InfraEngineer received unhandled category: {category}"],
            )

    def _handle_build_failure(self, context: dict, actuators: Actuators) -> AgentResult:
        """Handle a failed build using the ops playbook system."""
        error_message = context.get("error_message", "")
        build_id = context.get("build_id", "")

        try:
            from ops.scanner import classify_error
            from ops.playbook import match_playbook, load_fixer

            # Classify the error
            category = classify_error(error_message)

            # Match against known playbooks
            entry = match_playbook(category)
            effects = Effects()
            if entry is None:
                # Record as a DB finding for human investigation
                finding_key = f"infra-no-playbook-{category}-{build_id[:8]}"
                effects.finding(
                    actuators,
                    finding_key=finding_key,
                    title=f"Build failure with no playbook: {category}",
                    description=(
                        f"Build {build_id[:8]} failed with category '{category}' "
                        f"but no ops playbook exists to fix it automatically.\n"
                        f"Error: {error_message[:400]}\n\n"
                        f"Consider adding a playbook for this failure pattern in ops/playbook.py."
                    ),
                    priority="medium",
                    category="no_playbook",
                )
                return AgentResult(
                    success=False,
                    output={
                        "category": category,
                        "message": f"No playbook for category '{category}'",
                        "build_id": build_id,
                    },
                    escalate=True,
                    escalation_reason=f"No playbook for build failure category: {category}",
                    effects=effects.entries,
                    learnings=[f"Build failure category '{category}' has no playbook"] + effects.learnings(),
                )

            # Load the fixer module
            fixer_fn = load_fixer(entry)
            if fixer_fn is None:
                return AgentResult(
                    success=False,
                    output={"category": category, "message": "Fixer module failed to load"},
                    escalate=True,
                    escalation_reason=f"Fixer module for '{category}' failed to load",
                )

            # Execute the fix through the actuator layer, which decides whether
            # the playbook runs live: not in dry run, not without the
            # playbook's permission (so never in COORDINATOR_ANALYSIS_ONLY
            # mode), and not past the ops circuit breaker.
            fix_result, ran_live = actuators.run_playbook(category, fixer_fn, {
                "error_message": error_message,
                "build_ids": [build_id] if build_id else [],
                "category": category,
            })
            effective_dry_run = not ran_live

            # Record in failure KB only what was actually applied; a dry-run
            # "success" must not teach the KB that a fix works.
            if not effective_dry_run:
                # An agent without the kb_record permission is refused; the
                # outcome is in `effects`.
                effects.kb_record(
                    actuators,
                    category,
                    error_msg=error_message,
                    fix_desc=fix_result.description,
                    files=[],          # a playbook changes a Railway setting, never a file
                    success=fix_result.success,
                )

            # Send alert
            if fix_result.success and not effective_dry_run:
                effects.alert(
                    actuators,
                    f"[CAS Ops] Fix applied: {category}",
                    f"Build {build_id}: {fix_result.description}",
                )

            learnings = effects.learnings()
            if fix_result.success and not effective_dry_run:
                learnings.append(
                    f"Successfully fixed '{category}' in build {build_id[:8]}: {fix_result.description}"
                )
            elif fix_result.success:
                learnings.append(
                    f"Dry run: would fix '{category}' in build {build_id[:8]}: {fix_result.description}"
                )

            return AgentResult(
                success=fix_result.success,
                output={
                    "category": category,
                    "description": fix_result.description,
                    "requires_redeploy": fix_result.requires_redeploy,
                    "build_id": build_id,
                },
                actions_taken=actuators.actions,
                effects=effects.entries,
                learnings=learnings,
            )

        except Exception as exc:
            logger.error("Build failure handler error: %s", exc)
            return AgentResult(
                success=False,
                output={"error": str(exc)[:500], "build_id": build_id},
                escalate=True,
                escalation_reason=f"Exception in build failure handler: {exc}",
            )

    def _handle_app_down(self, context: dict, actuators: Actuators,
                         state: WorldState) -> AgentResult:
        """Handle an app that is down."""
        app_name = context.get("app_name", "")
        consecutive_failures = context.get("consecutive_failures", 0)

        # For now, escalate app-down events with context
        # Future: integrate with monitor/diagnose.py for automated diagnosis
        #
        # The alert is this task's whole purpose: the task succeeded only if
        # the email was sent (or, under dry run, recorded as one that would be
        # sent), and its "action" says which of those happened.
        effects = Effects()
        effects.alert(
            actuators,
            f"[CAS] App down: {app_name} ({consecutive_failures} failures)",
            f"App {app_name} has been down for {consecutive_failures} consecutive checks.",
        )

        return AgentResult(
            success=effects.delivered("alert"),
            output={
                "app_name": app_name,
                "consecutive_failures": consecutive_failures,
                "action": effects.action("alert", "alert"),
            },
            actions_taken=actuators.actions,
            effects=effects.entries,
            escalate=consecutive_failures > 10,
            escalation_reason=f"App {app_name} down for {consecutive_failures} consecutive failures" if consecutive_failures > 10 else "",
            learnings=[f"App {app_name} went down; {consecutive_failures} consecutive failures"]
            + effects.learnings(),
        )

    def _handle_backup_overdue(self, context: dict, actuators: Actuators) -> AgentResult:
        """Handle overdue backup."""
        age_hours = context.get("last_backup_age_hours", 0)

        effects = Effects()
        effects.alert(
            actuators,
            "[CAS] Backup overdue",
            f"Last successful backup was {age_hours:.1f} hours ago (threshold: 48h).",
        )

        return AgentResult(
            success=effects.delivered("alert"),
            output={"action": effects.action("alert", "alert"), "backup_age_hours": age_hours},
            actions_taken=actuators.actions,
            effects=effects.entries,
            escalate=age_hours > 96,
            escalation_reason=f"Backup overdue by {age_hours:.1f}h" if age_hours > 96 else "",
            learnings=effects.learnings(),
        )


    def _handle_coordinator_stalled(self, context: dict,
                                    actuators: Actuators) -> AgentResult:
        """Diagnose why the coordinator hasn't dispatched work in 30+ minutes."""
        ticks_checked = context.get("ticks_checked", 0)
        window_min = context.get("window_minutes", 30)
        recent_errors = context.get("recent_errors", [])

        diagnostics = []

        # Check if there's actually pending work in the DB
        try:
            pending = actuators.query_readonly(
                "SELECT COUNT(*) as cnt FROM agent_tasks WHERE status = 'pending'"
            )
            pending_count = pending[0]["cnt"] if pending else 0
            diagnostics.append(f"Pending tasks in queue: {pending_count}")
        except Exception as exc:
            diagnostics.append(f"DB query failed: {exc}")

        # Check recent tick errors
        if recent_errors:
            diagnostics.append(f"Recent tick errors: {'; '.join(recent_errors[:3])}")

        # Check if feedback exists but isn't being detected
        try:
            feedback = actuators.query_readonly(
                "SELECT COUNT(*) as cnt FROM beta_feedback "
                "WHERE created_at > NOW() - INTERVAL '1 hour'"
            )
            recent_feedback = feedback[0]["cnt"] if feedback else 0
            diagnostics.append(f"Feedback in last hour: {recent_feedback}")
        except Exception as query_error:
            logger.warning("monitor diagnosis: feedback count not read: %s", query_error)
            diagnostics.append("Feedback in last hour: not read")

        # Check if any builds failed recently
        try:
            failed = actuators.query_readonly(
                "SELECT COUNT(*) as cnt FROM builds "
                "WHERE status = 'failed' AND updated_at > NOW() - INTERVAL '1 hour'"
            )
            recent_failed = failed[0]["cnt"] if failed else 0
            diagnostics.append(f"Failed builds in last hour: {recent_failed}")
        except Exception as query_error:
            logger.warning("monitor diagnosis: failed-build count not read: %s", query_error)
            diagnostics.append("Failed builds in last hour: not read")

        summary = "; ".join(diagnostics)
        effects = Effects()
        effects.alert(
            actuators,
            f"[CAS] Coordinator stalled: {ticks_checked} ticks / {window_min}min with 0 dispatches",
            f"Diagnostics:\n{chr(10).join(diagnostics)}",
        )

        return AgentResult(
            success=True,
            output={
                "action": "diagnosed",
                "ticks_checked": ticks_checked,
                "diagnostics": diagnostics,
            },
            actions_taken=actuators.actions,
            effects=effects.entries,
            learnings=[f"Coordinator stalled diagnostic: {summary}"] + effects.learnings(),
        )


    def _handle_initiative(self, context: dict, actuators: Actuators) -> AgentResult:
        """Run the weekly infra-engineer initiatives.

        Today: sync the top-N entries of the build failure KB into the
        keeper-facing support KB so the support chat can answer
        "what does this build error mean?" without escalation.

        The drafts are written by the sync's own cheap-tier model, not by the
        model this agent's task was dispatched with: the task's tier (Sonnet
        for this agent) only sets what the tick reserves for the task, and one
        task can draft up to DEFAULT_TOP_N answers. Each draft is one call of
        the tick's model-call allowance, granted through `actuators`.

        Composes well with future weekly initiatives (e.g. backup
        sweeps, log cleanup) by appending to the same AgentResult.
        """
        from coordinator.failure_kb_sync import (
            sync_failure_kb_to_support_kb,
        )

        sync_summary = sync_failure_kb_to_support_kb(actuators=actuators)

        learnings = [
            "Failure KB → support KB sync: "
            f"considered={sync_summary['considered']}, "
            f"drafted={sync_summary['drafted']}, "
            f"inserted={sync_summary['inserted']}"
        ]
        if sync_summary["skipped_not_authorized"]:
            learnings.append(
                f"Sync left {sync_summary['skipped_not_authorized']} entries undrafted: "
                "no model call was authorized (dry run, or the tick's allowance was used up)"
            )
        if sync_summary["skipped_permission"]:
            learnings.append(
                f"Sync skipped {sync_summary['skipped_permission']} entries "
                "due to actuator permission gate"
            )
        if sync_summary["skipped_error"]:
            learnings.append(
                f"Sync skipped {sync_summary['skipped_error']} entries "
                "due to actuator errors"
            )

        return AgentResult(
            success=True,
            output={
                "initiative": "failure_kb_to_support_kb_sync",
                "sync": sync_summary,
            },
            actions_taken=actuators.actions,
            learnings=learnings,
        )


# Auto-register with the coordinator
def register():
    from coordinator.tick import register_agent
    register_agent("infra_engineer", InfraEngineer)
