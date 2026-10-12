# ops/fixer.py - Apply fixes via the Railway API
#
# Railway: triggers a redeployment and sets an environment variable via
# GraphQL. Both respect dry_run mode.
#
# This module is where every automatic redeploy and variable write of the
# Python side ends up: the two ops playbooks (ops/playbooks/) call it, whether
# the legacy ops agent runs them (ops/agent.py) or the coordinator does
# (Actuators.run_playbook). The limits on those actions are therefore enforced
# here, in the functions that make the call, and not in a caller:
#   * trigger_redeploy and upsert_env_var act only on the platform's own
#     services in its own environment (and project), as configured in
#     RAILWAY_WEB_SERVICE_ID / RAILWAY_WORKER_SERVICE_ID /
#     RAILWAY_ENVIRONMENT_ID / RAILWAY_PROJECT_ID, and upsert_env_var sets only
#     the names in SETTABLE_ENV_VARS.
# A request outside those limits is refused and logged, in dry run as well.
#
# Nothing here commits to a repository, and no module in ops/ or coordinator/
# reads a GitHub token: a playbook has no way to write to a branch. (The
# roadmap executor under scripts/ is a separate program with its own gates.)
#
# Configuration:
#   RAILWAY_API_TOKEN  Railway API token (redeploy / env var upsert)

import json
import logging
import os
from typing import Optional
from urllib.request import Request

from monitor.bounded_fetch import bounded_urlopen

logger = logging.getLogger("ops.fixer")

_RAILWAY_API = "https://backboard.railway.app/graphql/v2"
# Deadline of one Railway call, whole exchange, and how much of its answer is
# read (a mutation answers with a few hundred bytes).
_RAILWAY_DEADLINE_S = 30
_RAILWAY_MAX_ANSWER_BYTES = 256 * 1024

# The variables the env_config playbook may set on the worker service.
SETTABLE_ENV_VARS = frozenset({"DATABASE_URL", "RESEND_API_KEY", "ADMIN_EMAIL"})


def _platform_services() -> set:
    """The configured ids of the platform's own web and worker services."""
    return {
        value for value in (
            os.environ.get("RAILWAY_WEB_SERVICE_ID", "").strip(),
            os.environ.get("RAILWAY_WORKER_SERVICE_ID", "").strip(),
        ) if value
    }


def _railway_target_refusal(service_id, environment_id) -> Optional[str]:
    """Why (service, environment) may not be acted on, or None when it may.

    Fails closed: with the ids not configured, nothing is a platform service.
    """
    environment = os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip()
    if not environment or environment_id != environment:
        return "the environment is not the configured RAILWAY_ENVIRONMENT_ID"
    if not service_id or service_id not in _platform_services():
        return ("the service is not the configured RAILWAY_WEB_SERVICE_ID "
                "or RAILWAY_WORKER_SERVICE_ID")
    return None


def _railway_gql(query: str, variables: dict, token: str) -> dict:
    """Execute a Railway GraphQL query: one bounded exchange of at most
    _RAILWAY_DEADLINE_S seconds and _RAILWAY_MAX_ANSWER_BYTES of answer."""
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = Request(
        _RAILWAY_API,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with bounded_urlopen(req, timeout=_RAILWAY_DEADLINE_S,
                         max_body_bytes=_RAILWAY_MAX_ANSWER_BYTES) as resp:
        return json.loads(resp.read())


def trigger_redeploy(
    service_id: str,
    environment_id: str,
    *,
    token: Optional[str] = None,
    dry_run: bool = False,
) -> bool:
    """Trigger a redeployment of one of the platform's own Railway services.

    service_id must be the configured RAILWAY_WEB_SERVICE_ID or
    RAILWAY_WORKER_SERVICE_ID and environment_id the configured
    RAILWAY_ENVIRONMENT_ID. Any other target (a customer app's service
    included) is refused, in dry run as well.

    Returns True on success.
    """
    refusal = _railway_target_refusal(service_id, environment_id)
    if refusal:
        logger.error("Refusing to redeploy: %s", refusal)
        return False
    rw_token = token or os.environ.get("RAILWAY_API_TOKEN", "")
    if not rw_token:
        logger.error("RAILWAY_API_TOKEN not set; cannot redeploy")
        return False

    if dry_run:
        logger.info("[DRY RUN] Would redeploy service %s", service_id)
        return True

    try:
        result = _railway_gql(
            """
            mutation($serviceId: String!, $environmentId: String!) {
                serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
            }
            """,
            {"serviceId": service_id, "environmentId": environment_id},
            rw_token,
        )
        if result.get("errors"):
            logger.error("Railway redeploy failed: %s", result["errors"])
            return False
        logger.info("Triggered redeploy for service %s", service_id)
        return True
    except Exception as exc:
        logger.error("Railway redeploy failed: %s", exc)
        return False


def upsert_env_var(
    project_id: str,
    service_id: str,
    environment_id: str,
    var_name: str,
    var_value: str,
    *,
    token: Optional[str] = None,
    dry_run: bool = False,
) -> bool:
    """Set an environment variable on one of the platform's own Railway services.

    project_id must be the configured RAILWAY_PROJECT_ID, the service and
    environment must pass the same check as trigger_redeploy, and var_name
    must be one of SETTABLE_ENV_VARS. Anything else is refused, in dry run as
    well. The value is never logged.

    Returns True on success.
    """
    platform_project = os.environ.get("RAILWAY_PROJECT_ID", "").strip()
    refusal = None
    if not platform_project or project_id != platform_project:
        refusal = "the project is not the configured RAILWAY_PROJECT_ID"
    elif var_name not in SETTABLE_ENV_VARS:
        refusal = f"{var_name!r} is not one of {sorted(SETTABLE_ENV_VARS)}"
    else:
        refusal = _railway_target_refusal(service_id, environment_id)
    if refusal:
        logger.error("Refusing to set a variable: %s", refusal)
        return False
    rw_token = token or os.environ.get("RAILWAY_API_TOKEN", "")
    if not rw_token:
        logger.error("RAILWAY_API_TOKEN not set; cannot set env var")
        return False

    if dry_run:
        logger.info("[DRY RUN] Would set %s on service %s", var_name, service_id)
        return True

    try:
        result = _railway_gql(
            """
            mutation($input: VariableCollectionUpsertInput!) {
                variableCollectionUpsert(input: $input)
            }
            """,
            {
                "input": {
                    "projectId": project_id,
                    "serviceId": service_id,
                    "environmentId": environment_id,
                    "variables": {var_name: var_value},
                }
            },
            rw_token,
        )
        if result.get("errors"):
            logger.error("Railway env var upsert failed: %s", result["errors"])
            return False
        logger.info("Set env var %s on service %s", var_name, service_id)
        return True
    except Exception as exc:
        logger.error("Railway env var upsert failed: %s", exc)
        return False
