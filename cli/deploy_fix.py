# cli/deploy_fix.py - Self-correction for generated apps when deployment crashes.
#
# After a generated app is deployed to Railway but crashes on startup (health
# check fails), this module collects deployment logs + source files, asks Claude
# to diagnose and fix, then writes corrected files for redeployment.
#
# Called by worker_pg.py between deploy failure and redeploy.

"""LEGACY v1 build engine (frozen): post-deploy crash fix loop. Superseded by the
v2 engine in compose/ (which fails fast instead of patching); kept for reference
only and run only when CAS_BUILD_ENGINE=v1. Do not extend it. See cli/README.md.
"""

import json
import os
from pathlib import Path
from typing import Optional

from api_client import BuildCancelled
from cli import cancellation
from fix import (
    _collect_source_files,
    _create_client,
    _call_api,
    _format_source_files,
    _parse_fix_files,
    _build_advisor_prompt,
)


def _raise_if_cancelled(build_id, *, stage):
    """Raise BuildCancelled if the supervisor has fired this attempt's cancel event.

    `build_id` is the attempt's cancel key (see the caller). With no key the
    check is a no-op: a run outside the worker has no supervisor.

    Module-local helper (intentionally duplicated across fix loops) so that
    a refactor or rename of one fix module cannot silently remove the
    cooperative cancel check from another.
    """
    if not build_id:
        return
    event = cancellation.get(build_id)
    if event.is_set():
        raise BuildCancelled(build_id)


def _get_deploy_advisor_config():
    """Return advisor parameters if USE_ADVISOR_DEPLOY_FIX is enabled, else empty dict."""
    val = os.environ.get("USE_ADVISOR_DEPLOY_FIX", "").lower()
    if val and val not in ("false", "0", "no"):
        return {"advisor_model": "claude-opus-4-6", "advisor_max_uses": 2}
    return {}


# Per-call wall-clock budget for the Anthropic request issued by
# attempt_deploy_fix. A 20-min default
# wall-clock lets a single API call burn most of the deploy_fix
# loop budget (even with retry backoff being short on its own), which
# in turn consumes the build-tier supervisor budget and burns the one
# free supervised retry. Cap at 7 min so two iterations fit inside the
# 15-min loop budget (_FIX_LOOP_BUDGETS_S["deploy_fix"] in
# queue/worker_pg.py). Overridable via env for debugging without a
# worker deploy.
_DEPLOY_FIX_PER_CALL_BUDGET_S_ENV = "CAS_DEPLOY_FIX_CALL_BUDGET_S"
_DEPLOY_FIX_PER_CALL_BUDGET_S_DEFAULT = 7 * 60


def _deploy_fix_per_call_budget_s() -> float:
    """Resolve the per-Anthropic-call wall-clock budget for deploy_fix.

    Reads on every call so a variableUpsert to the env var on a running
    worker is visible without a restart (matches the env-read pattern
    used by cli.widgets.archetype._widgets_enabled_for).
    """
    raw = os.environ.get(_DEPLOY_FIX_PER_CALL_BUDGET_S_ENV)
    if not raw:
        return float(_DEPLOY_FIX_PER_CALL_BUDGET_S_DEFAULT)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(_DEPLOY_FIX_PER_CALL_BUDGET_S_DEFAULT)
    # Clamp to a sane range. Floor at 5s (anything smaller is an
    # obvious typo / test-only: production callers never want <5s);
    # ceiling at 900s (15 min) since going higher defeats the purpose
    # of a per-call cap that lives inside the 15-min loop budget.
    if value < 5:
        return 5.0
    if value > 900:
        return 900.0
    return value

DEPLOY_FIX_SYSTEM_PROMPT = """You are a deployment fixer for CraftAndShip, a platform that generates Express.js + PostgreSQL web apps.

A generated app was deployed to Railway but crashed on startup. You are given:
1. The Railway deployment logs showing the crash output
2. The health check result (the app's /health endpoint is unreachable)
3. The relevant source files from the build directory

Your job is to diagnose why the app crashes on startup and output ONLY the fixed files.

## Common deployment crash causes

- Port binding: the app must listen on process.env.PORT (Railway sets this), not a hardcoded port
- Database connection: DATABASE_URL must be read from environment, not hardcoded
- Missing dependencies: package.json must list all require()'d modules
- Uncaught exceptions during startup: missing files, wrong paths, syntax errors
- Missing /health endpoint: server.js must have GET /health returning 200 OK
- Session/cookie config: secure cookies need trust proxy behind Railway's edge proxy
- Migration failures: schema.sql errors that prevent the app from starting
- Seed data errors: seed.sql referencing tables that do not exist yet

## Output rules

For each file you need to fix, use this exact format:

--BEGINFILE-- path/to/file.ext
complete file contents here
--ENDFILE--

CRITICAL: --BEGINFILE-- and --ENDFILE-- must appear on their own lines. Never write them inside file content.

IMPORTANT:
- Output ONLY files that need changes. Do not output unchanged files.
- Output the COMPLETE file content, not just the diff.
- The path must match the original file's path exactly (e.g. server.js, routes/auth.js).
- Focus on fixing the startup crash; do not refactor or add features.
- Ensure the app listens on process.env.PORT || 3000.
- Ensure GET /health returns { status: "ok" }.
- Ensure DATABASE_URL is read from process.env.
"""


def attempt_deploy_fix(
    build_dir: str,
    deploy_logs: list,
    health_status: str = "failed",
    app_stderr: str = "",
    spec: Optional[dict] = None,
    *,
    cancel_key: Optional[str] = None,
) -> dict:
    """Attempt to fix a generated app based on deployment crash logs.

    cancel_key is the key of the attempt's cancel event: when the worker's
    supervisor fires it, this call raises BuildCancelled at its next
    checkpoint.

    Args:
        build_dir: Path to the build directory.
        deploy_logs: List of log message strings from Railway deployment.
        health_status: Result of health check ("ok" or "failed").
        app_stderr: Any additional stderr output.
        spec: The original app spec (optional, for context).

    Returns:
        Dict with:
          - fixed: bool (True if files were written)
          - files_changed: list of file paths that were updated
          - description: str summary of what was fixed
    """
    # Key of the attempt's cancel event, handed over by the worker
    # (queue/worker_pg.py, _new_cancel_key; it begins with the build's id).
    # The model client is given the same key as its `build_id`: it uses that
    # argument to find the event and as the label on its log lines.
    build_id = cancel_key

    # Cancel guard: iteration_top. Bail before any expensive work if the
    # watchdog supervisor has already fired for this build. A hung deploy fix
    # loop once burned the entire Opus budget because the fix-loop layer never
    # observed the supervisor.
    _raise_if_cancelled(build_id, stage="deploy_fix:iteration_top")

    build_path = Path(build_dir).resolve()

    # Collect source files
    source_files = _collect_source_files(build_path)
    if not source_files:
        return {"fixed": False, "files_changed": [], "description": "No source files found"}

    if not deploy_logs and health_status == "ok":
        return {"fixed": False, "files_changed": [], "description": "No deployment issues to fix"}

    # Build the prompt
    prompt_parts = []

    prompt_parts.append("## Deployment Status\n")
    prompt_parts.append(f"Health check: {health_status}")
    prompt_parts.append("The app was deployed to Railway but crashed on startup.\n")

    # Deploy logs are the customer app's own output and can quote its database
    # URL or session secret; redact before they go into a model prompt.
    from failure_kb import redact_text

    if deploy_logs:
        prompt_parts.append("## Railway Deployment Logs (last 50 lines)\n")
        log_text = "\n".join(str(line) for line in deploy_logs[-50:])
        prompt_parts.append(redact_text(log_text[:8000]))

    if app_stderr:
        prompt_parts.append("\n\n## App stderr\n")
        prompt_parts.append(redact_text(app_stderr[:3000]))

    if spec:
        prompt_parts.append("\n\n## App Spec\n")
        prompt_parts.append(json.dumps(spec, indent=2)[:5000])

    prompt_parts.append("\n\n## Source Files\n")
    prompt_parts.append(_format_source_files(source_files))

    # Check failure KB for hints from past deploy fixes
    error_summary = (
        "deploy-crash; " + " ".join(deploy_logs[-5:])
        if deploy_logs
        else "deploy-crash; health-check-failed"
    )
    try:
        from failure_kb import get_prompt_hints
        kb_hints = get_prompt_hints(error_summary)
    except Exception:
        kb_hints = ""

    if kb_hints:
        prompt_parts.append(kb_hints)

    prompt_parts.append(
        "\n\nFix the deployment crash. The app must start successfully on Railway "
        "and respond to GET /health with 200 OK. Output only the files that need changes."
    )

    prompt = "\n".join(prompt_parts)

    # Call Claude
    client = _create_client()
    advisor_config = _get_deploy_advisor_config()
    effective_model = "claude-sonnet-4-6" if advisor_config else None
    effective_prompt = _build_advisor_prompt(DEPLOY_FIX_SYSTEM_PROMPT) if advisor_config else DEPLOY_FIX_SYSTEM_PROMPT
    # Explicit per-call wall-clock budget:
    # keep this below the loop budget so iteration 1 cannot exhaust the
    # whole deploy_fix budget window, which in turn leaves the supervised
    # retry intact if Anthropic keeps throttling.
    response = _call_api(
        client,
        [{"role": "user", "content": prompt}],
        system_prompt=effective_prompt,
        model=effective_model,
        build_id=build_id,
        wall_clock_budget_s=_deploy_fix_per_call_budget_s(),
        **advisor_config,
    )

    # Cancel guard: post_api. The API call is the longest blocking step in
    # this loop, so re-check immediately after it returns.
    _raise_if_cancelled(build_id, stage="deploy_fix:post_api")

    response_text = response.content[0].text if response.content else ""

    # Parse and write fixed files
    fixed_files = _parse_fix_files(response_text)

    # Cancel guard: pre_subprocess. Last chance to bail before we touch the
    # filesystem with Claude's suggested fixes.
    _raise_if_cancelled(build_id, stage="deploy_fix:pre_subprocess")
    if not fixed_files:
        # Record failed attempt so KB tracks errors that resist fixing
        try:
            from failure_kb import record_fix
            record_fix(error_summary, "Claude returned no file fixes", [],
                        fix_category="unknown", succeeded=False)
        except Exception:
            pass
        return {"fixed": False, "files_changed": [], "description": "Claude returned no file fixes"}

    files_changed = []
    for rel_path, content in fixed_files:
        target = build_path / rel_path
        # Path traversal protection: ensure target stays within build directory
        if not target.resolve().is_relative_to(build_path.resolve()):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        files_changed.append(rel_path)

    description = f"Fixed {len(files_changed)} file(s) for deploy: {', '.join(files_changed)}"

    try:
        from fix import _classify_fix
        category = _classify_fix(files_changed)
    except Exception:
        category = "deploy"

    # NOTE: Do NOT record succeeded=True here, the deploy may still fail health check.
    # The worker records the actual outcome after redeploy health check (see worker_pg.py).
    return {
        "fixed": True,
        "files_changed": files_changed,
        "description": description,
        "error_summary": error_summary,  # passed back so worker can record true outcome
        "fix_category": category,
    }
