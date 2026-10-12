# coordinator/agents/backend_engineer.py: Backend Engineer agent
#
# Handles recurring QA warnings, failed builds that are not an infrastructure
# problem, the periodic test build and, on its turn of initiative work, a
# read-only look at the last seven days of builds. It can queue a rebuild
# (bounded per build, per lineage and per customer, see
# Actuators.queue_rebuild) and a new sample build. It has no way to approve or
# promote a build. The handlers are rule-based: this agent makes no model
# calls, so the model the scheduler selects for it is not used.

import logging
import sys
from pathlib import Path

from coordinator.agents.base import Agent, AgentResult, Effects, RECORDED, REFUSED
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators

_ROOT = str(Path(__file__).parent.parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logger = logging.getLogger("coordinator.agents.backend_engineer")


class BackendEngineer(Agent):
    name = "backend_engineer"
    description = "Handles recurring QA warnings, failed builds, test builds and a review of recent builds"
    requires_review = True
    # Model-free in every category (checked against the source by
    # tests/test_coordinator_model_free_tasks.py).
    model_categories = frozenset()

    def execute(self, task: WorkItem, state: WorldState, model: str,
                actuators: Actuators) -> AgentResult:
        category = task.category
        context = task.context

        if category == "recurring_qa_warning":
            return self._handle_recurring_qa(context, actuators, state)
        elif category == "build_failure":
            return self._handle_build_failure(context, actuators)
        elif category == "test_build":
            return self._handle_test_build(context, actuators)
        elif category == "agent_initiative":
            return self._handle_initiative(context, actuators, state)
        else:
            return AgentResult(
                success=False,
                output={"error": f"Unknown category: {category}"},
                learnings=[f"BackendEngineer received unhandled category: {category}"],
            )

    def _handle_recurring_qa(self, context: dict, actuators: Actuators,
                             state: WorldState) -> AgentResult:
        """Analyze recurring QA warnings and queue rebuilds for affected apps."""
        warning_type = context.get("warning_type", "")
        occurrences = context.get("occurrences", 0)

        # Find builds affected by this warning pattern
        try:
            affected = actuators.query_readonly(
                "SELECT id, app_name, qa_results FROM builds "
                "WHERE qa_status = 'warned' AND status IN ('preview', 'live') "
                "ORDER BY completed_at DESC LIMIT 5"
            )
        except Exception as exc:
            return AgentResult(
                success=False,
                output={"error": f"DB query failed: {exc}"},
            )

        rebuild_count = 0
        rebuild_ids = []
        effects = Effects()

        for build in affected:
            qa_results = build.get("qa_results", [])
            if not isinstance(qa_results, list):
                continue

            has_warning = any(
                isinstance(r, dict) and (
                    r.get("type") == warning_type or
                    r.get("name") == warning_type
                )
                for r in qa_results
            )

            if has_warning:
                new_id, outcome = effects.rebuild(
                    actuators,
                    str(build["id"]),
                    rebuild_request=f"Fix recurring QA warning: {warning_type}",
                )
                if new_id:
                    rebuild_count += 1
                    rebuild_ids.append(new_id)
                elif outcome == REFUSED:
                    # Analysis mode: record as a DB finding for admin review
                    app_name = build.get("app_name", "unknown")
                    finding_key = f"qa-warn-{warning_type[:30].replace(' ', '-')}-{str(build.get('id', ''))[:8]}"
                    effects.finding(
                        actuators,
                        finding_key=finding_key,
                        title=f"Fix recurring QA warning in {app_name}: {warning_type}",
                        description=(
                            f"QA warning '{warning_type}' detected in {occurrences} recent builds. "
                            f"App: {app_name} (build {str(build.get('id', ''))[:8]}). "
                            f"Manual rebuild with fix needed."
                        ),
                        priority="medium",
                        category="qa_warning",
                    )

        effects.alert(
            actuators,
            f"[CAS] Recurring QA warning: {warning_type} ({occurrences}x)",
            f"Warning '{warning_type}' found in {occurrences} recent builds. "
            f"Queued {rebuild_count} rebuild(s).",
        )

        return AgentResult(
            success=True,
            output={
                "warning_type": warning_type,
                "occurrences": occurrences,
                "rebuilds_queued": rebuild_count,
                "rebuild_ids": rebuild_ids,
            },
            actions_taken=actuators.actions,
            effects=effects.entries,
            learnings=[
                f"Recurring QA warning '{warning_type}': queued {rebuild_count} rebuilds"
            ] + effects.learnings(),
        )


    def _handle_build_failure(self, context: dict, actuators: Actuators) -> AgentResult:
        """Handle build failures by queuing a fresh rebuild."""
        build_id = context.get("build_id", "")
        error_message = context.get("error_message", "")
        finding_flagged = False
        effects = Effects()

        new_id, outcome = effects.rebuild(
            actuators,
            build_id,
            rebuild_request=f"Automated retry after build failure: {error_message[:200]}",
        )
        if new_id:
            effects.alert(
                actuators,
                f"[CAS] Build failure auto-retry",
                f"Build {build_id[:8]} failed. Queued rebuild {new_id[:8]}.",
            )
        elif outcome == REFUSED:
            # Analysis mode: record as a DB finding for manual action
            finding_flagged = effects.finding(
                actuators,
                finding_key=f"build-fail-{build_id[:8]}",
                title=f"Investigate and retry failed build {build_id[:8]}",
                description=(
                    f"Build {build_id[:8]} failed and needs a manual retry or investigation.\n"
                    f"Error: {error_message[:400]}"
                ),
                priority="high",
                category="build_failure",
            ) == RECORDED

        if new_id:
            action = "rebuild_queued"
        elif finding_flagged:
            action = "finding_flagged"
        elif outcome == REFUSED:
            action = "rebuild_refused"
        elif actuators.dry_run:
            # Nothing was attempted: queue_rebuild only recorded what it would
            # do. The tick marks this task's row as a dry-run row, and the
            # failure circuit does not count it.
            action = "dry_run"
        else:
            action = "rebuild_failed"

        return AgentResult(
            success=new_id is not None,
            output={
                "build_id": build_id,
                "new_build_id": new_id,
                "action": action,
            },
            effects=effects.entries,
            actions_taken=actuators.actions,
            learnings=[
                f"Auto-retry for build {build_id[:8]}: {'queued' if new_id else action}"
            ] + effects.learnings(),
        )


    def _handle_test_build(self, context: dict, actuators: Actuators) -> AgentResult:
        """Queue an automated test build with a realistic spec from the pool."""
        import psycopg2
        import os

        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            return AgentResult(success=False, output={"error": "No DATABASE_URL"})

        conn = psycopg2.connect(db_url)
        try:
            from coordinator.sample_build_specs import get_next_spec, get_test_customer_id

            # This connection only picks the spec and the test customer (two
            # SELECTs); the build itself is queued through the actuator below.
            # A read-only session makes Postgres refuse anything else on it.
            conn.set_session(readonly=True)
            customer_id = get_test_customer_id(conn)
            if not customer_id:
                return AgentResult(
                    success=False,
                    output={"error": "Test customer not found"},
                    learnings=["Test customer e2e@craftandship.example not in DB"],
                )

            spec = get_next_spec(conn)
            if not spec:
                return AgentResult(
                    success=True,
                    output={"message": "All specs have recent builds, skipping"},
                    learnings=["All test specs have active/recent builds"],
                )

            new_id = actuators.queue_new_build(
                customer_id=customer_id,
                app_name=spec["app_name"],
                spec=spec,
                tier="build",
            )

            if new_id:
                return AgentResult(
                    success=True,
                    output={"build_id": new_id, "app_name": spec["app_name"]},
                    actions_taken=actuators.actions,
                    learnings=[f"Queued test build: {spec['app_name']}"],
                )
            else:
                return AgentResult(
                    success=False,
                    output={"error": "queue_new_build returned None"},
                )
        except Exception as exc:
            return AgentResult(
                success=False,
                output={"error": str(exc)},
                learnings=[f"Test build failed: {exc}"],
            )
        finally:
            conn.close()

    def _handle_initiative(self, context: dict, actuators: Actuators,
                           state: WorldState) -> AgentResult:
        """Initiative work: a read-only summary of the last seven days of builds."""
        initiative = context.get("initiative", context.get("description", ""))
        title = context.get("title", "Backend initiative")

        # Analyze recent build quality to identify improvements
        findings = []
        try:
            # Check recent failure patterns
            failed = actuators.query_readonly(
                "SELECT error_message, COUNT(*) as cnt FROM builds "
                "WHERE status = 'failed' AND created_at > NOW() - INTERVAL '7 days' "
                "GROUP BY error_message ORDER BY cnt DESC LIMIT 5"
            )
            if failed:
                findings.append(f"Top failure patterns: {len(failed)} unique errors in last 7 days")
                for f in failed[:3]:
                    findings.append(f"  - ({f['cnt']}x) {(f['error_message'] or '')[:100]}")

            # Check QA warning patterns
            warned = actuators.query_readonly(
                "SELECT COUNT(*) as cnt FROM builds "
                "WHERE qa_status = 'warned' AND created_at > NOW() - INTERVAL '7 days'"
            )
            if warned and warned[0]["cnt"] > 0:
                findings.append(f"QA warnings: {warned[0]['cnt']} builds warned in last 7 days")

            # Check retry rates
            retries = actuators.query_readonly(
                "SELECT AVG(retry_count) as avg_retries FROM builds "
                "WHERE created_at > NOW() - INTERVAL '7 days'"
            )
            if retries and float(retries[0].get("avg_retries", 0)) > 0.3:
                findings.append(f"Retry rate elevated: {float(retries[0]['avg_retries']):.1f} avg")

        except Exception as exc:
            findings.append(f"Analysis error: {exc}")

        # Send findings report
        effects = Effects()
        effects.alert(
            actuators,
            f"[CAS] Backend Initiative: {title}",
            f"Initiative: {initiative}\n\nFindings:\n" + "\n".join(findings) if findings else "No issues found.",
        )

        return AgentResult(
            success=True,
            output={
                "initiative": initiative,
                "title": title,
                "findings": findings,
            },
            actions_taken=actuators.actions,
            effects=effects.entries,
            learnings=[f"Initiative '{title}': found {len(findings)} items to address"] + effects.learnings(),
        )


def register():
    from coordinator.tick import register_agent
    register_agent("backend_engineer", BackendEngineer)
