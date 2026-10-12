# coordinator/agents/security_engineer.py: Security Engineer agent
#
# Rule-based checks: no model call, no scanner, and it does not run npm audit
# or read generated code. The post-deploy audit reads one build row and applies
# four rules: the live URL is https, how many pages the spec marks public,
# field names that look sensitive (password, secret, token, ...), and security
# keywords in the stored QA results. The daily scan sends a HEAD request to
# each live app and reports the ones that are unreachable or answer 5xx.
# High-severity results go out as an alert email and a coordinator_findings row.

import logging
import time

from coordinator.agents.base import Agent, AgentResult, Effects
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators

logger = logging.getLogger("coordinator.agents.security_engineer")

# The daily scan asks each live app one question (a HEAD of its URL). Each
# request is one bounded exchange of at most PROBE_DEADLINE_S seconds, and the
# scan as a whole stops asking after SCAN_BUDGET_S; apps it did not get to are
# reported as not checked.
PROBE_DEADLINE_S = 10
SCAN_BUDGET_S = 600


class SecurityEngineer(Agent):
    name = "security"
    description = "Rule checks on a deployed build's row and a daily reachability probe of live apps"
    requires_review = True  # Security fixes need architecture review
    # Model-free in every category (checked against the source by
    # tests/test_coordinator_model_free_tasks.py).
    model_categories = frozenset()

    def execute(self, task: WorkItem, state: WorldState, model: str,
                actuators: Actuators) -> AgentResult:
        category = task.category

        if category == "post_deploy_audit":
            return self._handle_post_deploy_audit(task.context, actuators)
        elif category == "security_scan":
            return self._handle_daily_scan(state, actuators)
        elif category == "agent_initiative":
            return self._handle_daily_scan(state, actuators)
        else:
            return AgentResult(
                success=False,
                output={"error": f"Unknown category: {category}"},
                learnings=[f"SecurityEngineer received unhandled category: {category}"],
            )

    def _handle_post_deploy_audit(self, context: dict,
                                  actuators: Actuators) -> AgentResult:
        """Audit a newly deployed app for security issues."""
        build_id = context.get("build_id", "")
        app_name = context.get("app_name", "")

        # Query build details
        try:
            builds = actuators.query_readonly(
                "SELECT live_url, spec, qa_results, tier "
                "FROM builds WHERE id = %s",
                (build_id,),
            )
        except Exception:
            builds = []

        if not builds:
            return AgentResult(
                success=False,
                output={"error": f"Build {build_id} not found"},
            )

        build = builds[0]
        live_url = build.get("live_url", "")
        spec = build.get("spec") or {}
        tier = build.get("tier", "build")

        findings = []

        # Check 1: Verify HTTPS
        if live_url and not live_url.startswith("https://"):
            findings.append({
                "severity": "high",
                "issue": "App not served over HTTPS",
                "url": live_url,
            })

        # Check 2: Look for auth-required pages without auth in spec
        pages = spec.get("pages", []) if isinstance(spec, dict) else []
        public_pages = [p for p in pages if isinstance(p, dict) and not p.get("auth_required", True)]
        if len(public_pages) > 3:
            findings.append({
                "severity": "medium",
                "issue": f"{len(public_pages)} public pages; verify none expose sensitive data",
                "pages": [p.get("name", "?") for p in public_pages[:5]],
            })

        # Check 3: Look for data models with sensitive fields
        models = spec.get("data_models", []) if isinstance(spec, dict) else []
        for model_def in models:
            if not isinstance(model_def, dict):
                continue
            fields = model_def.get("fields", [])
            if isinstance(fields, list):
                sensitive = [f for f in fields if isinstance(f, dict) and
                             any(kw in (f.get("name", "") or "").lower()
                                 for kw in ("password", "secret", "token", "ssn", "credit_card"))]
                if sensitive:
                    findings.append({
                        "severity": "high",
                        "issue": f"Model '{model_def.get('name', '?')}' has sensitive fields: "
                                 f"{[f.get('name') for f in sensitive]}",
                    })

        # Check 4: QA results for security-related failures
        qa_results = build.get("qa_results") or []
        if isinstance(qa_results, list):
            security_failures = [r for r in qa_results if isinstance(r, dict) and
                                 any(kw in str(r).lower() for kw in ("xss", "injection", "csrf", "auth"))]
            if security_failures:
                findings.append({
                    "severity": "high",
                    "issue": f"{len(security_failures)} security-related QA failures",
                    "details": [str(r)[:100] for r in security_failures[:3]],
                })

        # Alert on high-severity findings
        effects = Effects()
        high_findings = [f for f in findings if f["severity"] == "high"]
        if high_findings:
            alert_body = f"Post-deploy audit for {app_name} ({build_id[:8]}):\n\n"
            for f in findings:
                alert_body += f"[{f['severity'].upper()}] {f['issue']}\n"
            effects.alert(
                actuators,
                f"[CAS Security] {len(high_findings)} high-severity findings: {app_name}",
                alert_body,
            )

            # Record as a DB finding regardless of email success; findings need tracking
            effects.finding(
                actuators,
                finding_key=f"security-{build_id[:8]}",
                title=f"Security findings in {app_name}: {len(high_findings)} high-severity",
                description=(
                    f"Post-deploy security audit for {app_name} ({build_id[:8]}) "
                    f"found {len(high_findings)} high-severity issues:\n"
                    + "\n".join(f"- [{f['severity'].upper()}] {f['issue']}" for f in findings)
                ),
                priority="high",
                category="security",
            )

        learnings = []
        if not findings:
            learnings.append(f"Post-deploy audit clean for {app_name} ({tier})")
        else:
            learnings.append(f"Post-deploy audit for {app_name}: "
                             f"{len(high_findings)} high, "
                             f"{len(findings) - len(high_findings)} medium/low findings")

        return AgentResult(
            success=True,
            output={
                "build_id": build_id,
                "app_name": app_name,
                "findings_count": len(findings),
                "high_count": len(high_findings),
                "findings": findings,
            },
            actions_taken=actuators.actions,
            effects=effects.entries,
            learnings=learnings + effects.learnings(),
            escalate=len(high_findings) > 2,
            escalation_reason=(f"Multiple high-severity security findings in {app_name}"
                               if len(high_findings) > 2 else ""),
        )

    def _handle_daily_scan(self, state: WorldState,
                           actuators: Actuators) -> AgentResult:
        """Daily security scan: check all monitored apps for issues."""
        # Query all live apps
        try:
            live_apps = actuators.query_readonly(
                "SELECT b.id::text, b.app_name, b.live_url, b.tier, b.qa_status "
                "FROM builds b "
                "WHERE b.status = 'live' AND b.live_url IS NOT NULL "
                "ORDER BY b.updated_at DESC",
            )
        except Exception as exc:
            return AgentResult(
                success=False,
                output={"error": f"Failed to query live apps: {exc}"},
            )

        total_apps = len(live_apps)
        apps_with_issues = 0
        apps_not_checked = 0
        all_findings = []
        scan_deadline = time.monotonic() + SCAN_BUDGET_S

        for app in live_apps:
            # Basic health check on each live URL
            url = app.get("live_url", "")
            if not url:
                continue
            if time.monotonic() >= scan_deadline:
                # The apps answer one after another on this thread; a fleet of
                # slow ones must not hold the task until the tick abandons it.
                apps_not_checked += 1
                continue

            # Check if app is still responding. live_url is database-sourced and
            # the app behind it is customer code: only plain public http(s)
            # URLs are probed, through the guarded session, which refuses a
            # redirect to a non-public address and ends the whole exchange
            # (lookup, redirects, headers) at PROBE_DEADLINE_S.
            try:
                from monitor.ping import guarded_session, probe_url_problem
                if probe_url_problem(url):
                    raise ValueError("URL not probe-safe")
                session = guarded_session()
                try:
                    # A 4xx is an answer, not "unreachable".
                    status = session.head(
                        url, timeout=PROBE_DEADLINE_S, allow_redirects=True,
                        headers={"User-Agent": "CAS-SecurityScan/1.0"},
                    ).status_code
                finally:
                    session.close()
            except Exception as probe_error:
                logger.info("Daily scan: no answer from %s (%s)",
                            app.get("app_name", "?"), type(probe_error).__name__)
                status = 0

            if status == 0 or status >= 500:
                all_findings.append({
                    "app": app.get("app_name", "?"),
                    "issue": f"App unreachable or error (status={status})",
                    "severity": "high",
                })
                apps_with_issues += 1

        if apps_not_checked:
            all_findings.append({
                "app": f"{apps_not_checked} app(s)",
                "issue": f"Not checked: the scan's {SCAN_BUDGET_S}s budget ran out",
                "severity": "medium",
            })

        effects = Effects()
        if all_findings:
            body = (f"Daily security scan: {total_apps - apps_not_checked} of {total_apps} apps "
                    f"checked, {apps_with_issues} with issues\n\n")
            for f in all_findings:
                body += f"[{f['severity'].upper()}] {f['app']}: {f['issue']}\n"
            effects.alert(actuators, "[CAS Security] Daily scan results", body)

        return AgentResult(
            success=True,
            output={
                "total_apps": total_apps,
                "apps_with_issues": apps_with_issues,
                "apps_not_checked": apps_not_checked,
                "findings": all_findings,
            },
            actions_taken=actuators.actions,
            effects=effects.entries,
            learnings=[f"Daily scan: {total_apps} apps, {apps_with_issues} with issues"] + effects.learnings(),
        )


def register():
    from coordinator.tick import register_agent
    register_agent("security", SecurityEngineer)
