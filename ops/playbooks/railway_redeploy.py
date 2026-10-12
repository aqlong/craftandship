# ops/playbooks/railway_redeploy.py - Trigger Railway redeployment
#
# Simple fix: restarts the crashed service. Used when OOM kills,
# signal 9, or other container-level crashes are detected.

import logging
import os

from ops.fixer import trigger_redeploy
from ops.playbook import FixResult

logger = logging.getLogger("ops.playbooks.railway_redeploy")


def fix(context: dict) -> FixResult:
    """Trigger a redeployment of the worker service.

    The service and environment come from RAILWAY_WORKER_SERVICE_ID and
    RAILWAY_ENVIRONMENT_ID; with either unset the playbook declines.
    """
    dry_run = context.get("dry_run", False)

    service_id = os.environ.get("RAILWAY_WORKER_SERVICE_ID", "").strip()
    environment_id = os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip()
    if not service_id or not environment_id:
        return FixResult(
            success=False,
            description="RAILWAY_WORKER_SERVICE_ID / RAILWAY_ENVIRONMENT_ID not set; cannot redeploy",
        )

    success = trigger_redeploy(
        service_id=service_id,
        environment_id=environment_id,
        dry_run=dry_run,
    )

    if success:
        return FixResult(
            success=True,
            description="Triggered worker service redeployment",
            requires_redeploy=False,  # Already redeploying
        )
    return FixResult(
        success=False,
        description="Failed to trigger Railway redeployment",
    )
