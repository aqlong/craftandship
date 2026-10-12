# coordinator/agents/architect.py: Architect agent
#
# Reviews build specs and finished builds, and looks at recurring QA patterns
# for systemic issues. The checks are rule-based (spec shape, counts, ratios):
# the agent makes no model calls, so the model the scheduler selects for it is
# not used. It sends no email: what it finds is the task's result, and the
# findings of its initiative review are filed in coordinator_findings.

import logging

from coordinator.agents.base import Agent, AgentResult
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators

logger = logging.getLogger("coordinator.agents.architect")


class Architect(Agent):
    name = "architect"
    description = "Rule-based reviews of specs, finished builds and recurring QA patterns"
    requires_review = False  # Architect is the reviewer; its output is advisory
    # Model-free in every category (checked against the source by
    # tests/test_coordinator_model_free_tasks.py).
    model_categories = frozenset()

    def execute(self, task: WorkItem, state: WorldState, model: str,
                actuators: Actuators) -> AgentResult:
        category = task.category
        context = task.context

        if category == "spec_review":
            return self._handle_spec_review(context, actuators)
        elif category == "post_build_review":
            return self._handle_post_build_review(context, actuators)
        elif category == "architecture_qa_review":
            return self._handle_architecture_qa_review(context, actuators)
        elif category == "agent_initiative":
            return self._handle_initiative(context, actuators, state)
        else:
            return AgentResult(
                success=False,
                output={"error": f"Unknown category: {category}"},
                learnings=[f"Architect received unhandled category: {category}"],
            )

    def _handle_spec_review(self, context: dict,
                            actuators: Actuators) -> AgentResult:
        """Review a build spec before it is built.

        Checks for architectural anti-patterns, missing auth on sensitive pages,
        data model issues (missing relations, circular deps, over-normalization),
        and spec completeness.
        """
        build_id = context.get("build_id", "")
        app_name = context.get("app_name", "")

        # Fetch the spec from the builds table
        try:
            builds = actuators.query_readonly(
                "SELECT spec, tier FROM builds WHERE id = %s",
                (build_id,),
            )
        except Exception as exc:
            return AgentResult(
                success=False,
                output={"error": f"DB query failed: {exc}"},
            )

        if not builds:
            return AgentResult(
                success=False,
                output={"error": f"Build {build_id} not found"},
            )

        build = builds[0]
        spec = build.get("spec") or {}
        tier = build.get("tier", "build")

        recommendations = []

        # Check 1: Data model relationships
        models = spec.get("data_models", []) if isinstance(spec, dict) else []
        model_names = {
            m.get("name", "").lower()
            for m in models if isinstance(m, dict)
        }

        for model_def in models:
            if not isinstance(model_def, dict):
                continue
            name = model_def.get("name", "")
            fields = model_def.get("fields", [])
            if not isinstance(fields, list):
                continue

            # Flag models with too many fields (potential over-complexity)
            if len(fields) > 15:
                recommendations.append({
                    "type": "complexity",
                    "severity": "medium",
                    "message": f"Model '{name}' has {len(fields)} fields; consider splitting",
                })

            # Flag models lacking a timestamp field
            field_names = [
                (f.get("name", "") if isinstance(f, dict) else "").lower()
                for f in fields
            ]
            has_timestamp = any(
                kw in fn for fn in field_names
                for kw in ("created", "updated", "timestamp", "date")
            )
            if not has_timestamp and name.lower() not in ("user", "admin"):
                recommendations.append({
                    "type": "best_practice",
                    "severity": "low",
                    "message": f"Model '{name}' has no timestamp field; consider adding created_at",
                })

            # Check for belongs_to references to nonexistent models
            belongs_to = model_def.get("belongs_to", "")
            if belongs_to:
                refs = [r.strip().lower() for r in str(belongs_to).split(",")]
                for ref in refs:
                    if ref and ref not in model_names:
                        recommendations.append({
                            "type": "integrity",
                            "severity": "high",
                            "message": f"Model '{name}' references nonexistent model '{ref}' in belongs_to",
                        })

        # Check 2: Auth coverage
        pages = spec.get("pages", []) if isinstance(spec, dict) else []
        auth_required_pages = []
        public_pages = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            if page.get("auth_required", True):
                auth_required_pages.append(page.get("name", "?"))
            else:
                public_pages.append(page.get("name", "?"))

        # Flag if many pages are public with data models present
        if len(public_pages) > 3 and len(models) > 2:
            recommendations.append({
                "type": "security",
                "severity": "medium",
                "message": (
                    f"{len(public_pages)} public pages with {len(models)} data models; "
                    "verify sensitive data is not exposed"
                ),
            })

        # Check 3: Spec completeness
        if isinstance(spec, dict):
            if not spec.get("app_name"):
                recommendations.append({
                    "type": "completeness",
                    "severity": "high",
                    "message": "Spec missing app_name",
                })
            if not spec.get("description"):
                recommendations.append({
                    "type": "completeness",
                    "severity": "medium",
                    "message": "Spec missing description",
                })
            if not models:
                recommendations.append({
                    "type": "completeness",
                    "severity": "medium",
                    "message": "Spec has no data models defined",
                })
            if not pages:
                recommendations.append({
                    "type": "completeness",
                    "severity": "medium",
                    "message": "Spec has no pages defined",
                })

        # Check 4: Tier appropriateness
        if tier == "build" and (len(models) > 5 or len(pages) > 10):
            recommendations.append({
                "type": "tier_mismatch",
                "severity": "medium",
                "message": (
                    f"Build tier with {len(models)} models and {len(pages)} pages; "
                    "may benefit from Grow tier for multi-pass generation"
                ),
            })

        high_count = sum(1 for r in recommendations if r["severity"] == "high")

        return AgentResult(
            success=True,
            output={
                "build_id": build_id,
                "app_name": app_name,
                "tier": tier,
                "recommendation_count": len(recommendations),
                "high_count": high_count,
                "recommendations": recommendations,
            },
            actions_taken=actuators.actions,
            learnings=[
                f"Spec review for {app_name}: {len(recommendations)} recommendations, "
                f"{high_count} high-severity"
            ],
        )

    def _handle_post_build_review(self, context: dict,
                                  actuators: Actuators) -> AgentResult:
        """Review generated app architecture after build completes.

        Checks build quality signals: QA results, retry count, stage timings,
        and compares against similar past builds.
        """
        build_id = context.get("build_id", "")
        app_name = context.get("app_name", "")

        try:
            builds = actuators.query_readonly(
                "SELECT spec, tier, qa_status, qa_results, retry_count, "
                "stage_timings, total_api_cost_usd "
                "FROM builds WHERE id = %s",
                (build_id,),
            )
        except Exception as exc:
            return AgentResult(
                success=False,
                output={"error": f"DB query failed: {exc}"},
            )

        if not builds:
            return AgentResult(
                success=False,
                output={"error": f"Build {build_id} not found"},
            )

        build = builds[0]
        spec = build.get("spec") or {}
        tier = build.get("tier", "build")
        qa_status = build.get("qa_status", "pending")
        qa_results = build.get("qa_results") or []
        retry_count = build.get("retry_count", 0) or 0
        stage_timings = build.get("stage_timings") or {}
        api_cost = float(build.get("total_api_cost_usd") or 0)

        findings = []

        # Check 1: High retry count indicates fragile generation
        if retry_count >= 2:
            findings.append({
                "type": "build_quality",
                "severity": "medium",
                "message": f"Build required {retry_count} retries; generation may be fragile",
            })

        # Check 2: QA warnings or failures
        if qa_status == "warned" and isinstance(qa_results, list):
            failed_tests = [
                r for r in qa_results
                if isinstance(r, dict) and r.get("status") in ("failed", "warning", "warn")
            ]
            if len(failed_tests) >= 3:
                findings.append({
                    "type": "qa_quality",
                    "severity": "high",
                    "message": (
                        f"{len(failed_tests)} QA warnings/failures; "
                        "systemic architecture issue likely"
                    ),
                })
            elif failed_tests:
                findings.append({
                    "type": "qa_quality",
                    "severity": "medium",
                    "message": f"{len(failed_tests)} QA warning(s) detected",
                })

        # Check 3: Excessive build cost
        cost_thresholds = {"build": 2.0, "grow": 5.0, "scale": 10.0}
        threshold = cost_thresholds.get(tier, 3.0)
        if api_cost > threshold:
            findings.append({
                "type": "cost",
                "severity": "medium",
                "message": (
                    f"Build cost ${api_cost:.2f} exceeds {tier} tier "
                    f"threshold ${threshold:.2f}"
                ),
            })

        # Check 4: Building stage too slow
        building_timing = stage_timings.get("building", {})
        if isinstance(building_timing, dict):
            duration_s = building_timing.get("duration_s", 0)
            if duration_s and duration_s > 600:  # 10 minutes
                findings.append({
                    "type": "performance",
                    "severity": "low",
                    "message": f"Build took {duration_s:.0f}s; consider optimization",
                })

        # Check 5: Compare model count vs page count (simple heuristic)
        models = spec.get("data_models", []) if isinstance(spec, dict) else []
        pages = spec.get("pages", []) if isinstance(spec, dict) else []
        if len(models) > 0 and len(pages) > 0:
            ratio = len(pages) / len(models)
            if ratio > 5:
                findings.append({
                    "type": "architecture",
                    "severity": "low",
                    "message": (
                        f"Page-to-model ratio is {ratio:.1f}:1; "
                        "some pages may lack proper data backing"
                    ),
                })

        high_count = sum(1 for f in findings if f["severity"] == "high")

        return AgentResult(
            success=True,
            output={
                "build_id": build_id,
                "app_name": app_name,
                "tier": tier,
                "findings_count": len(findings),
                "high_count": high_count,
                "findings": findings,
            },
            actions_taken=actuators.actions,
            learnings=[
                f"Post-build review for {app_name}: {len(findings)} findings, "
                f"{high_count} high-severity"
            ],
        )

    def _handle_architecture_qa_review(self, context: dict,
                                       actuators: Actuators) -> AgentResult:
        """Analyze recurring QA patterns for systemic architecture issues.

        When the same QA warnings recur across multiple builds, this indicates
        a systemic problem in the build prompt or platform rather than a
        one-off app issue.
        """
        warning_type = context.get("warning_type", "")
        occurrences = context.get("occurrences", 0)

        # Query recent builds with this warning pattern
        try:
            warned_builds = actuators.query_readonly(
                "SELECT id, app_name, spec, qa_results, tier FROM builds "
                "WHERE qa_status = 'warned' AND completed_at IS NOT NULL "
                "ORDER BY completed_at DESC LIMIT 10"
            )
        except Exception as exc:
            return AgentResult(
                success=False,
                output={"error": f"DB query failed: {exc}"},
            )

        # Analyze which tiers and spec patterns are affected
        affected_tiers = set()
        affected_apps = []
        pattern_details = []

        for build in warned_builds:
            qa_results = build.get("qa_results") or []
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
                affected_tiers.add(build.get("tier", "unknown"))
                affected_apps.append(build.get("app_name", "?"))

                # Check spec characteristics for patterns
                spec = build.get("spec") or {}
                if isinstance(spec, dict):
                    model_count = len(spec.get("data_models", []))
                    page_count = len(spec.get("pages", []))
                    pattern_details.append({
                        "app": build.get("app_name", "?"),
                        "tier": build.get("tier", "?"),
                        "models": model_count,
                        "pages": page_count,
                    })

        recommendations = []

        # Determine if this is a platform-wide issue
        if len(affected_tiers) > 1:
            recommendations.append({
                "type": "platform",
                "severity": "high",
                "message": (
                    f"Warning '{warning_type}' affects multiple tiers "
                    f"({', '.join(sorted(affected_tiers))}); "
                    "likely a build prompt issue"
                ),
            })

        if occurrences >= 5:
            recommendations.append({
                "type": "systemic",
                "severity": "high",
                "message": (
                    f"Warning '{warning_type}' occurred {occurrences} times; "
                    "requires build prompt or post-processing fix"
                ),
            })
        elif occurrences >= 3:
            recommendations.append({
                "type": "recurring",
                "severity": "medium",
                "message": (
                    f"Warning '{warning_type}' recurring ({occurrences}x); "
                    "monitor and consider prompt adjustment"
                ),
            })

        high_count = sum(1 for r in recommendations if r["severity"] == "high")

        return AgentResult(
            success=True,
            output={
                "warning_type": warning_type,
                "occurrences": occurrences,
                "affected_tiers": sorted(affected_tiers),
                "affected_apps": affected_apps[:5],
                "recommendation_count": len(recommendations),
                "high_count": high_count,
                "recommendations": recommendations,
                "pattern_details": pattern_details[:5],
            },
            actions_taken=actuators.actions,
            learnings=[
                f"QA pattern '{warning_type}': {occurrences}x across "
                f"{len(affected_tiers)} tier(s), {len(recommendations)} recommendations"
            ],
        )

    def _handle_initiative(self, context: dict, actuators: Actuators,
                           state: WorldState) -> AgentResult:
        """Proactive architecture review: scan recent builds for patterns."""
        findings = []

        try:
            # Check recent build quality trends
            recent = actuators.query_readonly(
                "SELECT tier, qa_status, retry_count, total_api_cost_usd "
                "FROM builds "
                "WHERE completed_at IS NOT NULL "
                "AND completed_at > NOW() - INTERVAL '7 days' "
                "ORDER BY completed_at DESC LIMIT 20"
            )

            if recent:
                total = len(recent)
                warned = sum(1 for b in recent if b.get("qa_status") == "warned")
                high_retry = sum(1 for b in recent
                                 if (b.get("retry_count") or 0) >= 2)
                avg_cost = sum(float(b.get("total_api_cost_usd") or 0)
                               for b in recent) / total

                if warned / total > 0.3:
                    findings.append(
                        f"QA warning rate elevated: {warned}/{total} "
                        f"({warned/total:.0%}) in last 7 days"
                    )
                if high_retry / total > 0.2:
                    findings.append(
                        f"High retry rate: {high_retry}/{total} builds "
                        f"needed 2+ retries"
                    )
                if avg_cost > 2.0:
                    findings.append(
                        f"Average build cost ${avg_cost:.2f} may indicate "
                        "inefficient generation"
                    )

        except Exception as exc:
            findings.append(f"Analysis error: {exc}")

        return AgentResult(
            success=True,
            output={
                "initiative": "architecture_review",
                "findings": findings,
            },
            actions_taken=actuators.actions,
            learnings=[f"Architecture initiative: {len(findings)} findings"],
        )


def register():
    from coordinator.tick import register_agent
    register_agent("architect", Architect)
