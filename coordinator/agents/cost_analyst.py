# coordinator/agents/cost_analyst.py: Cost Analyst agent
#
# Arithmetic, not analysis by a model (it makes no model call): SQL sums and
# averages over the cost columns of builds and agent_tasks, a projection that
# multiplies the average build cost by fixed monthly build counts, a few
# threshold rules that each add a canned recommendation, and an email with the
# result. The per-stage token averages and prices below are constants written
# into this file, not measurements it takes. Runs on a periodic trigger.

import logging
from datetime import datetime, timezone

from coordinator.agents.base import Agent, AgentResult, Effects
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators

logger = logging.getLogger("coordinator.agents.cost_analyst")

# Anthropic API pricing per 1M tokens (as of 2026-04, matches cli/api_client.py PRICING table)
MODEL_PRICING = {
    "claude-opus-4-6":   {"input": 5.00,  "output": 25.00, "cached_input": 0.50},
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00, "cached_input": 0.30},
    "claude-haiku-4-5":  {"input": 1.00,  "output": 5.00,  "cached_input": 0.10},
}

# Average tokens per build stage (empirical from CAS pipeline)
AVG_TOKENS_PER_BUILD = {
    "build_prompt":  {"input": 8000, "output": 25000, "model": "claude-sonnet-4-6"},
    "review":        {"input": 12000, "output": 3000, "model": "claude-sonnet-4-6"},
    "test_fix":      {"input": 10000, "output": 8000, "model": "claude-opus-4-6"},
    "deploy_fix":    {"input": 8000, "output": 6000, "model": "claude-opus-4-6"},
    "qa":            {"input": 5000, "output": 4000, "model": "claude-haiku-4-5"},
    "intake":        {"input": 3000, "output": 2000, "model": "claude-opus-4-6"},
    "coordinator":   {"input": 500, "output": 200, "model": "claude-haiku-4-5"},
}


class CostAnalyst(Agent):
    name = "cost_analyst"
    description = "Sums recorded API costs, projects them at fixed build volumes, and emails the report"
    requires_review = False
    # Model-free in every category (checked against the source by
    # tests/test_coordinator_model_free_tasks.py).
    model_categories = frozenset()

    def execute(self, task: WorkItem, state: WorldState, model: str,
                actuators: Actuators) -> AgentResult:
        category = task.category

        if category == "cost_projection":
            return self._handle_cost_projection(task.context, actuators, state)
        elif category == "cost_report":
            return self._handle_cost_report(task.context, actuators, state)
        elif category == "agent_initiative":
            return self._handle_cost_projection(task.context, actuators, state)
        else:
            return AgentResult(
                success=False,
                output={"error": f"Unknown category: {category}"},
            )

    def _handle_cost_projection(self, context: dict, actuators: Actuators,
                                state: WorldState) -> AgentResult:
        """Generate cost projections for scaling scenarios."""
        # Get recent build volume and costs
        try:
            build_stats = actuators.query_readonly(
                "SELECT COUNT(*) as total, "
                "COUNT(CASE WHEN status = 'live' THEN 1 END) as live, "
                "COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed, "
                "COALESCE(AVG(total_api_cost_usd), 0) as avg_cost, "
                "COALESCE(SUM(total_api_cost_usd), 0) as total_cost "
                "FROM builds WHERE created_at > NOW() - INTERVAL '7 days'"
            )
        except Exception as exc:
            return AgentResult(
                success=False,
                output={"error": f"Could not query build stats: {exc}"},
            )

        stats = build_stats[0] if build_stats else {}
        weekly_builds = stats.get("total", 0)
        avg_cost = float(stats.get("avg_cost", 0))
        total_cost_7d = float(stats.get("total_cost", 0))
        live_count = stats.get("live", 0)
        failed_count = stats.get("failed", 0)

        # Get coordinator agent costs
        try:
            agent_stats = actuators.query_readonly(
                "SELECT COALESCE(SUM(actual_cost_usd), 0) as agent_cost, "
                "COUNT(*) as agent_tasks "
                "FROM agent_tasks "
                "WHERE created_at > NOW() - INTERVAL '7 days' "
                "AND status IN ('completed', 'failed')"
            )
        except Exception:
            agent_stats = [{"agent_cost": 0, "agent_tasks": 0}]

        agent_cost_7d = float(agent_stats[0].get("agent_cost", 0)) if agent_stats else 0
        agent_tasks_7d = agent_stats[0].get("agent_tasks", 0) if agent_stats else 0

        # Compute projections for scaling scenarios
        projections = {}
        for label, monthly_builds in [("current", weekly_builds * 4),
                                       ("10_customers", 40),
                                       ("50_customers", 200),
                                       ("100_customers", 500),
                                       ("500_customers", 2500)]:
            build_cost = monthly_builds * max(avg_cost, self._estimate_build_cost())
            # Agent costs scale sub-linearly (most are per-tick, not per-build)
            agent_monthly = agent_cost_7d * 4 * (1 + (monthly_builds / max(weekly_builds * 4, 1)) * 0.3)
            # Intake costs (per conversation)
            intake_cost = monthly_builds * self._estimate_stage_cost("intake")
            total_monthly = build_cost + agent_monthly + intake_cost

            projections[label] = {
                "monthly_builds": monthly_builds,
                "build_cost_usd": round(build_cost, 2),
                "agent_cost_usd": round(agent_monthly, 2),
                "intake_cost_usd": round(intake_cost, 2),
                "total_monthly_usd": round(total_monthly, 2),
                "cost_per_build_usd": round(total_monthly / max(monthly_builds, 1), 2),
            }

        # Build report
        report = {
            "period": "last_7_days",
            "weekly_builds": weekly_builds,
            "live_builds": live_count,
            "failed_builds": failed_count,
            "avg_build_cost_usd": round(avg_cost, 2),
            "total_build_cost_7d_usd": round(total_cost_7d, 2),
            "agent_cost_7d_usd": round(agent_cost_7d, 2),
            "agent_tasks_7d": agent_tasks_7d,
            "projections": projections,
            "recommendations": self._generate_recommendations(
                weekly_builds, avg_cost, failed_count, live_count, projections
            ),
        }

        # Send email report. The report is the task's result and is stored
        # whether or not the email goes out; `effects` says what became of it.
        effects = Effects()
        effects.alert(actuators, "[CAS] Weekly Cost Projection Report", self._format_report_email(report))

        return AgentResult(
            success=True,
            output=report,
            actions_taken=actuators.actions,
            effects=effects.entries,
            learnings=[
                f"Weekly cost: ${total_cost_7d:.2f} builds + ${agent_cost_7d:.2f} agents "
                f"({weekly_builds} builds, avg ${avg_cost:.2f}/build)"
            ] + effects.learnings(),
        )

    def _handle_cost_report(self, context: dict, actuators: Actuators,
                            state: WorldState) -> AgentResult:
        """Daily cost report from the same figures the budget runs on.

        The worker's ledger is read with the ledger's own two statements
        (spend_tracker.LEDGER_BUILDS_SQL and LEDGER_AGENT_TASKS_SQL) through
        Actuators.query_readonly, and the Anthropic cost report through
        spend_tracker.get_anthropic_spend when ANTHROPIC_ADMIN_KEY is set. If
        the ledger cannot be read the task fails and says so; if the key is
        set and the cost report cannot be read, the report carries the ledger
        figures and states that they leave out what the web tier spent.
        """
        from coordinator import config, spend_tracker

        try:
            build_rows = actuators.query_readonly(spend_tracker.LEDGER_BUILDS_SQL)
            agent_rows = actuators.query_readonly(spend_tracker.LEDGER_AGENT_TASKS_SQL)
            tracked = spend_tracker.ledger_figures(
                (build_rows[0]["usd"], build_rows[0]["n"]) if build_rows else None,
                (agent_rows[0]["usd"], agent_rows[0]["n"]) if agent_rows else None,
            )
        except Exception as exc:
            return AgentResult(
                success=False, output={"error": f"Could not read the spend ledger: {exc}"})
        builds_today = tracked["builds_today"]
        build_cost = tracked["build_pipeline_usd"]
        agent_cost = tracked["agent_tasks_usd"]
        total_tracked = tracked["total_tracked_usd"]

        anthropic = spend_tracker.get_anthropic_spend()
        provider_failed = anthropic is None and spend_tracker.provider_spend_configured()

        daily_budget = config.daily_budget_usd()
        total_spend = anthropic["today_usd"] if anthropic else total_tracked
        utilization = (total_spend / daily_budget * 100) if daily_budget > 0 else 0

        report = {
            "date": context.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
            "builds_today": builds_today,
            "build_pipeline_usd": round(build_cost, 2),
            "agent_tasks_usd": round(agent_cost, 2),
            "total_tracked_usd": round(total_tracked, 2),
            "budget_utilization_pct": round(utilization, 1),
            "budget_remaining_usd": round(max(0, daily_budget - total_spend), 2),
            "spend_source": "anthropic_api" if anthropic else "internal_tracking",
            "provider_failed": provider_failed,
        }

        if anthropic:
            report["anthropic_today_usd"] = anthropic["today_usd"]
            report["anthropic_mtd_usd"] = anthropic["month_to_date_usd"]

        # Build alert body
        lines = [
            f"Builds: {builds_today} (${build_cost:.2f})",
            f"Agent tasks: ${agent_cost:.2f}",
        ]
        if anthropic:
            lines.append(f"Anthropic billing: ${anthropic['today_usd']:.2f} today, ${anthropic['month_to_date_usd']:.2f} MTD")
        elif provider_failed:
            lines.append(f"Total tracked: ${total_tracked:.2f}. The Anthropic cost report could not be read, "
                         "so this leaves out what the web tier spent; the coordinator gets no budget "
                         "while that lasts.")
        else:
            lines.append(f"Total tracked: ${total_tracked:.2f} (set ANTHROPIC_ADMIN_KEY for full billing)")
        lines.append(f"Budget: {utilization:.0f}% of ${daily_budget:.0f}, ${report['budget_remaining_usd']:.2f} remaining")

        effects = Effects()
        effects.alert(
            actuators,
            f"[CAS] Daily Cost Report - ${total_spend:.2f} spent",
            "\n".join(lines),
        )

        return AgentResult(
            success=True,
            output=report,
            actions_taken=actuators.actions,
            effects=effects.entries,
            learnings=[f"Daily spend: ${total_spend:.2f} ({report['spend_source']})"] + effects.learnings(),
        )


    def _estimate_build_cost(self) -> float:
        """Estimate average cost of a single build using token averages."""
        total = 0.0
        for stage, tokens in AVG_TOKENS_PER_BUILD.items():
            total += self._estimate_stage_cost(stage)
        return total

    def _estimate_stage_cost(self, stage: str) -> float:
        """Estimate cost for a single pipeline stage."""
        tokens = AVG_TOKENS_PER_BUILD.get(stage, {})
        model = tokens.get("model", "claude-haiku-4-5")
        pricing = MODEL_PRICING.get(model, MODEL_PRICING["claude-haiku-4-5"])
        input_cost = tokens.get("input", 0) / 1_000_000 * pricing["input"]
        output_cost = tokens.get("output", 0) / 1_000_000 * pricing["output"]
        return input_cost + output_cost

    def _generate_recommendations(self, weekly_builds, avg_cost, failed, live, projections):
        """Generate actionable recommendations based on data."""
        recs = []

        failure_rate = failed / max(weekly_builds, 1)
        if failure_rate > 0.3:
            recs.append(
                f"Failure rate is {failure_rate:.0%}. Improving build prompts and "
                "failure KB could reduce wasted API spend significantly."
            )

        if avg_cost > 2.0:
            recs.append(
                f"Average build cost (${avg_cost:.2f}) is high. Consider: "
                "prompt caching, Sonnet for Phase 1, batch API for non-urgent builds."
            )

        proj_100 = projections.get("100_customers", {})
        if proj_100.get("total_monthly_usd", 0) > 5000:
            recs.append(
                f"At 100 customers, projected cost is ${proj_100['total_monthly_usd']:.0f}/mo. "
                "Consider volume pricing or caching strategies before scaling."
            )

        if not recs:
            recs.append("Costs are within expected range. No immediate optimizations needed.")

        return recs

    def _format_report_email(self, report: dict) -> str:
        """Format the cost projection report as email body."""
        lines = [
            f"CAS Weekly Cost Projection - {report['period']}",
            "",
            f"Current Activity ({report['weekly_builds']} builds/week):",
            f"  Build costs: ${report['total_build_cost_7d_usd']:.2f}",
            f"  Agent costs: ${report['agent_cost_7d_usd']:.2f}",
            f"  Avg per build: ${report['avg_build_cost_usd']:.2f}",
            f"  Success rate: {report['live_builds']}/{report['weekly_builds']}",
            "",
            "Scaling Projections (monthly):",
        ]

        for label, proj in report.get("projections", {}).items():
            lines.append(
                f"  {label}: {proj['monthly_builds']} builds/mo = "
                f"${proj['total_monthly_usd']:.0f}/mo "
                f"(${proj['cost_per_build_usd']:.2f}/build)"
            )

        lines.append("")
        lines.append("Recommendations:")
        for rec in report.get("recommendations", []):
            lines.append(f"  - {rec}")

        return "\n".join(lines)


def register():
    from coordinator.tick import register_agent
    register_agent("cost_analyst", CostAnalyst)
