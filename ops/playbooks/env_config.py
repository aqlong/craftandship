# ops/playbooks/env_config.py - Fix Railway environment variable issues
#
# Detects missing or incorrect env vars on Railway services and
# sets them via the Railway GraphQL API.

import logging
import os
import re

from ops.fixer import upsert_env_var
from ops.playbook import FixResult

logger = logging.getLogger("ops.playbooks.env_config")

# Known env var fixes: error pattern -> (var_name, description)
# Values are NOT hardcoded here; they are sourced from the running env
# or require admin input.
_KNOWN_VARS = {
    r"database_url.*not set": ("DATABASE_URL", "Database connection URL"),
    r"resend_api_key.*not set": ("RESEND_API_KEY", "Resend email API key"),
    r"admin_email.*not set": ("ADMIN_EMAIL", "Admin notification email"),
}


def fix(context: dict) -> FixResult:
    """Fix a missing Railway environment variable.

    For security, this only sets env vars whose values are available
    in the current process environment. It never generates or guesses
    secret values.
    """
    error_msg = context.get("error_message", "").lower()
    dry_run = context.get("dry_run", False)

    # Target ids come from the environment (RAILWAY_PROJECT_ID,
    # RAILWAY_ENVIRONMENT_ID, RAILWAY_WORKER_SERVICE_ID); decline when unset.
    project_id = os.environ.get("RAILWAY_PROJECT_ID", "").strip()
    environment_id = os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip()
    worker_service_id = os.environ.get("RAILWAY_WORKER_SERVICE_ID", "").strip()
    if not (project_id and environment_id and worker_service_id):
        return FixResult(
            success=False,
            description=(
                "RAILWAY_PROJECT_ID / RAILWAY_ENVIRONMENT_ID / "
                "RAILWAY_WORKER_SERVICE_ID not set; cannot set env vars"
            ),
        )

    for pattern, (var_name, description) in _KNOWN_VARS.items():
        if not re.search(pattern, error_msg):
            continue

        # Get the value from the current environment
        value = os.environ.get(var_name)
        if not value:
            return FixResult(
                success=False,
                description=f"{var_name} not available in current environment; cannot propagate",
            )

        # Set on the worker service (most likely target)
        success = upsert_env_var(
            project_id=project_id,
            service_id=worker_service_id,
            environment_id=environment_id,
            var_name=var_name,
            var_value=value,
            dry_run=dry_run,
        )

        if success:
            return FixResult(
                success=True,
                description=f"Set {var_name} on worker service ({description})",
                requires_redeploy=False,
            )
        return FixResult(
            success=False,
            description=f"Failed to set {var_name} via Railway API",
        )

    return FixResult(
        success=False,
        description="No known env var fix for this error",
    )
