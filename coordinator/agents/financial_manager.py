# coordinator/agents/financial_manager.py: Financial Manager agent
#
# Sends the budget alert, and on its turn of initiative work emails a cost
# summary. (The daily cost report is the cost analyst's.) Pure SQL
# aggregation and alert emails; this agent makes no model calls.

import logging
from datetime import datetime, timezone

from coordinator.agents.base import Agent, AgentResult, Effects
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators

logger = logging.getLogger("coordinator.agents.financial_manager")


class FinancialManager(Agent):
    name = "financial"
    description = "Sends budget alerts and a cost summary on its turn of initiative work"
    requires_review = False
    # Model-free in every category (checked against the source by
    # tests/test_coordinator_model_free_tasks.py).
    model_categories = frozenset()

    def execute(self, task: WorkItem, state: WorldState, model: str,
                actuators: Actuators) -> AgentResult:
        category = task.category

        if category == "budget_alert":
            return self._handle_budget_alert(task.context, state, actuators)
        elif category == "agent_initiative":
            return self._handle_cost_report(state, actuators)
        else:
            return AgentResult(
                success=False,
                output={"error": f"Unknown category: {category}"},
                learnings=[f"FinancialManager received unhandled category: {category}"],
            )

    def _handle_budget_alert(self, context: dict, state: WorldState,
                             actuators: Actuators) -> AgentResult:
        remaining = context.get("remaining_usd", 0)
        cost_today = context.get("cost_today_usd", 0)

        # A pipeline figure the tick could not read is None: say so, not "0".
        active_builds = "unknown" if state.active_builds is None else state.active_builds
        failed_builds = "unknown" if state.failed_builds_24h is None else len(state.failed_builds_24h)
        subject = f"[CAS] Budget alert: ${remaining:.2f} remaining"
        body = (
            f"Daily coordinator budget is running low.\n\n"
            f"Spent today: ${cost_today:.2f}\n"
            f"Remaining: ${remaining:.2f}\n"
            f"Active builds: {active_builds}\n"
            f"Failed builds (24h): {failed_builds}\n\n"
            f"The coordinator throttles discretionary work as the budget shrinks (see scorer._cost_penalty)."
        )

        # The alert is this task's whole purpose: it succeeded only if the
        # email was sent (or, under dry run, recorded as one that would be).
        effects = Effects()
        effects.alert(actuators, subject, body)

        return AgentResult(
            success=effects.delivered("alert"),
            output={"action": effects.action("alert", "budget_alert"), "remaining_usd": remaining},
            actions_taken=actuators.actions,
            effects=effects.entries,
            learnings=[f"Budget alert at ${remaining:.2f} remaining; ${cost_today:.2f} spent today"]
            + effects.learnings(),
        )

    def _handle_cost_report(self, state: WorldState,
                            actuators: Actuators) -> AgentResult:
        """Generate daily cost summary from DB aggregation. No Claude API call needed."""
        try:
            # Query build costs for last 24h
            build_costs = actuators.query_readonly(
                "SELECT tier, COUNT(*) as count, "
                "COALESCE(SUM(total_api_cost_usd), 0) as total_cost, "
                "COALESCE(AVG(total_api_cost_usd), 0) as avg_cost "
                "FROM builds WHERE created_at > NOW() - INTERVAL '24 hours' "
                "GROUP BY tier ORDER BY total_cost DESC",
            )

            # Query agent task costs for last 24h
            agent_costs = actuators.query_readonly(
                "SELECT agent, COUNT(*) as count, "
                "COALESCE(SUM(actual_cost_usd), 0) as total_cost "
                "FROM agent_tasks WHERE created_at > NOW() - INTERVAL '24 hours' "
                "AND status IN ('completed', 'failed') "
                "GROUP BY agent ORDER BY total_cost DESC",
            )

            # Total spend
            total_build_cost = sum(r.get("total_cost", 0) or 0 for r in build_costs)
            total_agent_cost = sum(r.get("total_cost", 0) or 0 for r in agent_costs)
            total_cost = total_build_cost + total_agent_cost

            # Format report
            lines = [
                f"CAS Daily Cost Report: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                f"",
                f"Total spend (24h): ${total_cost:.2f}",
                f"  Build pipeline: ${total_build_cost:.2f}",
                f"  Coordinator agents: ${total_agent_cost:.2f}",
                f"",
            ]

            if build_costs:
                lines.append("Build costs by tier:")
                for r in build_costs:
                    lines.append(f"  {r['tier']}: {r['count']} builds, "
                                 f"${float(r['total_cost']):.2f} total, "
                                 f"${float(r['avg_cost']):.2f} avg")
            else:
                lines.append("No builds in last 24h.")

            if agent_costs:
                lines.append("")
                lines.append("Agent costs:")
                for r in agent_costs:
                    lines.append(f"  {r['agent']}: {r['count']} tasks, "
                                 f"${float(r['total_cost']):.2f}")

            report = "\n".join(lines)

            effects = Effects()
            effects.alert(actuators, "[CAS] Daily Cost Report", report)

            return AgentResult(
                success=True,
                output={
                    "total_cost_24h": total_cost,
                    "build_cost": total_build_cost,
                    "agent_cost": total_agent_cost,
                    "build_breakdown": build_costs,
                    "agent_breakdown": agent_costs,
                },
                actions_taken=actuators.actions,
                effects=effects.entries,
                learnings=[f"Daily cost: ${total_cost:.2f} (builds: ${total_build_cost:.2f}, agents: ${total_agent_cost:.2f})"]
                + effects.learnings(),
            )

        except Exception as exc:
            logger.error("Cost report error: %s", exc)
            return AgentResult(
                success=False,
                output={"error": str(exc)[:500]},
                escalate=True,
                escalation_reason=f"Cost report generation failed: {exc}",
            )


def register():
    from coordinator.tick import register_agent
    register_agent("financial", FinancialManager)
