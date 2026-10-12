# coordinator/agents/reconciliation.py: Zombie-deploy background reconciliation
#
# A rule-based sweep with no model call. It reads builds.status='failed' rows
# whose error_message suggests a supervisor timeout (through the read-only
# actuator query), asks Railway whether the app is in fact running (the shared
# watchdog probe helper, a read), and for each build that is live files an
# approval request and sends one alert email to the admin.
#
# It does not change a build row. Moving a recovered build to 'preview' is left
# to the person who reads the request, and approval requests are advisory:
# nothing in this repository executes one when it is approved. Under dry run
# it files and sends nothing.
#
# This is the "background sweep" safety net (Option 3 in §2.1 of
# docs/15_PROD_BUILD_PATTERNS.md) behind the supervisor's own reality check in
# queue/worker_pg.py, which is the code that does write 'preview'.
#
# Disabled by default: AGENT_RECONCILIATION_ENABLED env var must be explicitly
# set to "true".

from __future__ import annotations

import logging
import sys
from pathlib import Path

from coordinator.agents.base import Agent, AgentResult, Effects, RECORDED
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators

# Ensure queue/ is on sys.path so watchdog_probe.py is importable as a flat module.
# We insert the queue/ directory directly rather than the repo root because the
# queue/ directory shadows the stdlib 'queue' module when the root is on sys.path
# (urllib3 and threading internals depend on stdlib queue.Queue).  This matches
# the import convention used by queue/worker_pg.py for all queue/ sub-modules.
_QUEUE_DIR = str(Path(__file__).parent.parent.parent / "queue")
if _QUEUE_DIR not in sys.path:
    sys.path.insert(0, _QUEUE_DIR)

# Shared probe helper: same implementation used by the supervisor in worker_pg.py.
# Imported as a flat module (watchdog_probe) because queue/ has no __init__.py.
# This import is the canonical "shared helper reused from worker" assertion in tests.
from watchdog_probe import probe_for_zombie_deploy

logger = logging.getLogger("coordinator.agents.reconciliation")


def register():
    """Register ReconciliationAgent with the coordinator tick registry."""
    from coordinator.tick import register_agent
    register_agent("reconciliation", ReconciliationAgent)

# Query window: scan failed builds up to this many hours back
_RECONCILIATION_WINDOW_HOURS = 72

# Substrings of builds.error_message that mark a build the supervisor failed on
# a timeout: the builds whose deployment may have come up after all.
_TIMEOUT_MARKERS = ("supervisor", "timeout", "timed out", "force-fail", "watchdog")


class ReconciliationAgent(Agent):
    """Scans recent failed builds for zombie deploys and asks a person to recover them.

    Trigger: RECONCILIATION_SWEEP periodic work item (every 2h by default).
    Disabled by default: set AGENT_RECONCILIATION_ENABLED=true to enable.
    """

    name = "reconciliation"
    # Column order of _candidate_query(); _process_rows unpacks rows in this order.
    _CANDIDATE_COLUMNS = ("id", "app_name", "started_at", "completed_at",
                          "error_message", "railway_project_id")
    description = ("Scans recent failed builds for zombie deploys and files an "
                   "approval request for each one found live")
    requires_review = False  # Reads, an approval request and an alert; no code changes
    # Model-free in every category (checked against the source by
    # tests/test_coordinator_model_free_tasks.py).
    model_categories = frozenset()

    def execute(
        self,
        task: WorkItem,
        state: WorldState,
        model: str,
        actuators: Actuators,
    ) -> AgentResult:
        """Execute one reconciliation sweep."""
        try:
            return self._run_sweep(actuators)
        except Exception as exc:
            logger.warning("ReconciliationAgent sweep error: %s", exc)
            return AgentResult(
                success=False,
                output={"error": str(exc)},
            )

    def _run_sweep(self, actuators: Actuators) -> AgentResult:
        """Query recent failed/timeout builds (read-only) and probe Railway for each."""
        sql, params = self._candidate_query()
        try:
            rows = actuators.query_readonly(sql, params)
        except Exception as exc:
            return AgentResult(
                success=False,
                output={"error": f"query_readonly failed: {exc}"},
            )
        # query_readonly returns dicts; _process_rows unpacks positional tuples.
        # Unpacking a dict yields its keys, which would probe Railway for an app
        # literally named "app_name".
        rows = [
            tuple(row[col] for col in self._CANDIDATE_COLUMNS) if isinstance(row, dict) else row
            for row in rows
        ]
        return self._process_rows(rows, actuators)

    @staticmethod
    def _candidate_query() -> tuple:
        """(sql, params) to find recently-failed builds that may be zombie deploys.

        The window and the error-message patterns travel as parameters.
        query_readonly always hands a parameter tuple to the driver, and the
        driver reads a percent sign in the SQL text as a placeholder, so a
        pattern such as '%supervisor%' written into the statement makes it
        fail before it reaches the database.
        """
        sql = """
            SELECT id, app_name, started_at, completed_at, error_message,
                   railway_project_id
            FROM builds
            WHERE status = 'failed'
              AND completed_at > NOW() - INTERVAL '1 hour' * %s
              AND error_message ILIKE ANY(%s)
        """
        return sql, (_RECONCILIATION_WINDOW_HOURS, [f"%{marker}%" for marker in _TIMEOUT_MARKERS])

    def _process_rows(self, rows, actuators: Actuators) -> AgentResult:
        """Probe Railway for each candidate and request recovery of the live ones."""
        recovered = []
        skipped = []
        errors = []
        dry_run = actuators.dry_run
        effects = Effects()

        for row in rows:
            build_id, app_name, started_at, completed_at, error_message, existing_project_id = row
            build_id = str(build_id)

            logger.info(
                "ReconciliationAgent: probing build %s (%s) for zombie deploy",
                build_id, app_name,
            )

            try:
                # The probe matches Railway projects by name and app_name is
                # not unique: leave out every project that a build row
                # records, this build's own row included (see
                # _recorded_projects). If that lookup fails, do not probe at
                # all.
                recorded = self._recorded_projects(app_name, actuators)
                if isinstance(existing_project_id, str) and existing_project_id:
                    # Already on the candidate row itself; said again here so
                    # the exclusion does not rest on the second query alone.
                    recorded.add(existing_project_id)
                probe_kwargs = {"exclude_project_ids": recorded} if recorded else {}
                probe_result = probe_for_zombie_deploy(
                    app_name=app_name,
                    started_at=started_at,
                    completed_at=completed_at,
                    **probe_kwargs,
                )
            except Exception as probe_exc:
                logger.warning(
                    "ReconciliationAgent: probe failed for build %s: %s",
                    build_id, probe_exc,
                )
                errors.append({"build_id": build_id, "error": str(probe_exc)})
                continue

            if not probe_result or not probe_result.get("live_url"):
                skipped.append({"build_id": build_id, "reason": "not live"})
                continue

            live_url = probe_result["live_url"]
            project_id = probe_result.get("project_id")

            logger.warning(
                "ReconciliationAgent: ZOMBIE FOUND, build %s (%s) is live at %s; "
                "requesting recovery to preview (dry_run=%s)",
                build_id, app_name, live_url, dry_run,
            )

            if not dry_run:
                request_outcome = self._request_flip_to_preview(
                    build_id, live_url, project_id, actuators, effects)
                self._send_recovery_alert(build_id, actuators, effects, request_outcome)

            recovered.append({
                "build_id": build_id,
                "app_name": app_name,
                "live_url": live_url,
                "project_id": project_id,
            })

        return AgentResult(
            success=True,
            output={
                "recovered": recovered,
                "skipped_count": len(skipped),
                "error_count": len(errors),
                "dry_run": dry_run,
            },
            actions_taken=[
                f"reconciliation_sweep: {len(recovered)} zombie(s) found live, "
                f"{len(skipped)} not live, {len(errors)} probe error(s)"
            ],
            effects=effects.entries,
            learnings=(
                [f"ReconciliationAgent found {len(recovered)} zombie deploy(s) live"]
                if recovered else []
            ) + effects.learnings(),
        )

    @staticmethod
    def _recorded_projects(app_name: str, actuators: Actuators) -> set:
        """Railway project ids recorded on any build row with this app_name.

        The worker writes builds.railway_project_id in one statement only, the
        preview write (_set_preview_status in queue/worker_pg.py), so a project
        that a row records is a deployment that was announced to its customer.
        That holds for the row of the failed build being reconciled too: a
        project on it is an earlier deployment of that build (it reached
        preview, was requeued in place and then failed), not what the failed
        attempt left behind. None of them is a zombie, so none may be offered
        for recovery. Same rule as the worker's _recorded_railway_projects.
        """
        rows = actuators.query_readonly(
            "SELECT railway_project_id FROM builds "
            "WHERE app_name = %s AND railway_project_id IS NOT NULL",
            (app_name,),
        ) or []
        claimed = set()
        for row in rows:
            project_id = row.get("railway_project_id") if isinstance(row, dict) else row[0]
            if isinstance(project_id, str) and project_id:
                claimed.add(project_id)
        return claimed

    @staticmethod
    def _request_flip_to_preview(build_id: str, live_url: str, project_id, actuators: Actuators,
                                 effects: Effects) -> str:
        """File the approval request that asks a person to move the build to preview.

        Returns what became of it (see coordinator.agents.base). The request is
        refused in COORDINATOR_ANALYSIS_ONLY mode (approval_request is not an
        analysis-only permission). The sweep goes on: the alert email that
        follows is how the person hears of the build, and it says whether a
        request was filed.
        """
        return effects.approval_request(
            actuators,
            request_type="reconciliation_flip_to_preview",
            description=(
                f"Zombie deploy found: build {build_id} is live at {live_url}. "
                f"Flip status='preview', review_status='pending'."
            ),
            context={
                "build_id": build_id,
                "live_url": live_url,
                "project_id": project_id,
            },
        )

    def _send_recovery_alert(self, build_id: str, actuators: Actuators, effects: Effects = None,
                             request_outcome: str = RECORDED) -> None:
        """Tell the admin that a failed build turned out to be running.

        The text says what was done about it: an approval request was filed
        (or one was already waiting), or none was and why.
        """
        effects = effects if effects is not None else Effects()
        if request_outcome == RECORDED:
            done = ("Nothing was changed: an approval request was filed asking for the "
                    "build to be moved to preview and reviewed.")
        else:
            done = ("Nothing was changed, and no approval request was filed "
                    f"({request_outcome}): the build has to be looked at by hand.")
        effects.alert(
            actuators,
            subject=f"Zombie deploy found: build {build_id} is live but marked failed",
            body=(
                f"ReconciliationAgent detected that build {build_id} was force-failed "
                f"by the supervisor but the app actually deployed successfully. "
                f"{done}"
            ),
        )
