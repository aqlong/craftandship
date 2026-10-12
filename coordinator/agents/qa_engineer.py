# coordinator/agents/qa_engineer.py: QA/Test Engineer agent
#
# Read-only and rule-based (no model call, no tests are run): on its turn of
# initiative work it lists the live builds that got a rebuild request within
# 48 hours of going live. The output is the task's result and learnings; it
# has no side effects and holds no permission.

import logging

from coordinator.agents.base import Agent, AgentResult
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators

logger = logging.getLogger("coordinator.agents.qa_engineer")


class QAEngineer(Agent):
    name = "qa_test"
    description = "Lists live builds that needed a rebuild within 48 hours"
    requires_review = False  # Read-only agent
    # Model-free in every category (checked against the source by
    # tests/test_coordinator_model_free_tasks.py).
    model_categories = frozenset()

    def execute(self, task: WorkItem, state: WorldState, model: str,
                actuators: Actuators) -> AgentResult:
        category = task.category

        if category == "agent_initiative":
            return self._handle_regression_check(actuators)
        else:
            return AgentResult(
                success=False,
                output={"error": f"Unknown category: {category}"},
                learnings=[f"QAEngineer received unhandled category: {category}"],
            )


    def _handle_regression_check(self, actuators: Actuators) -> AgentResult:
        """Check for builds that went live then got rebuild requests within 48h."""
        try:
            regressions = actuators.query_readonly(
                "SELECT b.id::text as original_id, b.app_name, "
                "rb.rebuild_request, rb.created_at as rebuild_at "
                "FROM builds b "
                "JOIN builds rb ON rb.parent_build_id = b.id "
                "WHERE b.status = 'live' "
                "AND rb.created_at < b.completed_at + INTERVAL '48 hours' "
                "AND rb.rebuild_request IS NOT NULL "
                "AND rb.created_at > NOW() - INTERVAL '7 days'",
            )
        except Exception as exc:
            return AgentResult(
                success=False,
                output={"error": f"Regression check failed: {exc}"},
            )

        learnings = []
        for reg in regressions:
            learnings.append(
                f"Regression: {reg['app_name']} needed rebuild within 48h: "
                f"{(reg.get('rebuild_request') or '')[:100]}"
            )

        return AgentResult(
            success=True,
            output={
                "regressions_found": len(regressions),
                "apps": [r.get("app_name") for r in regressions],
            },
            actions_taken=actuators.actions,
            learnings=learnings,
        )


def register():
    from coordinator.tick import register_agent
    register_agent("qa_test", QAEngineer)
