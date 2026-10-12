#!/usr/bin/env python3
"""Wait for CAS Railway services to finish deploying the latest git commit.

Uses the /health endpoint (web) and worker heartbeat instead of the Railway
GraphQL API, since Railway tokens expire frequently.

Strategy:
  - Web: GET /health returns a JSON response with `git_commit` field.
    Verify the deployed commit matches local HEAD.
  - Worker: /health also exposes worker heartbeat with its own `git_commit`.

If auto-deploy doesn't trigger within the timeout, falls back to manually
triggering a serviceInstanceRedeploy via Railway GraphQL API.

The --service flag controls which service(s) to wait for. This avoids false
timeouts when only worker files change (web commit stays the same) or vice versa.

Usage:
  python3 scripts/wait_for_deploy.py                    # wait for both (default)
  python3 scripts/wait_for_deploy.py --service worker   # worker-only changes
  python3 scripts/wait_for_deploy.py --service web      # web-only changes
  python3 scripts/wait_for_deploy.py --once              # check once; exit 1 if not ready
  python3 scripts/wait_for_deploy.py --timeout 5         # custom timeout in minutes
  python3 scripts/wait_for_deploy.py --redeploy          # skip wait, trigger redeploy immediately

Configuration (all via environment; nothing is hardcoded):
  CAS_PROD_URL               base URL of the deployed web app, e.g. https://your-app.example.com
  RAILWAY_API_TOKEN          only needed for the redeploy fallback
  RAILWAY_WEB_SERVICE_ID     only needed for the redeploy fallback
  RAILWAY_WORKER_SERVICE_ID  only needed for the redeploy fallback
  RAILWAY_ENVIRONMENT_ID     only needed for the redeploy fallback
  CAS_STAGING_URL, STAGING_WEB_SERVICE_ID, STAGING_WORKER_SERVICE_ID,
  STAGING_ENVIRONMENT_ID     the same four, used with --staging

Exit codes:
  0 = requested service(s) appear healthy after deploy
  1 = timeout or error
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

BASE_URL = os.environ.get("CAS_PROD_URL", "").rstrip("/")

# Railway service IDs, used only by the manual-redeploy fallback.
WEB_SERVICE_ID = os.environ.get("RAILWAY_WEB_SERVICE_ID", "")
WORKER_SERVICE_ID = os.environ.get("RAILWAY_WORKER_SERVICE_ID", "")
ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")
RAILWAY_GQL_URL = "https://backboard.railway.app/graphql/v2"

# Staging overrides (from env vars)
STAGING_BASE_URL = os.environ.get("CAS_STAGING_URL", "")
STAGING_WEB_SERVICE_ID = os.environ.get("STAGING_WEB_SERVICE_ID", "")
STAGING_WORKER_SERVICE_ID = os.environ.get("STAGING_WORKER_SERVICE_ID", "")
STAGING_ENVIRONMENT_ID = os.environ.get("STAGING_ENVIRONMENT_ID", "")


def _get_env_key(key_name, env_prefix=None):
    """Read a key from environment, falling back to .env files."""
    key = os.environ.get(key_name, "")
    if key:
        return key
    prefix = env_prefix or key_name
    # Search multiple .env files (web/.env, root .env)
    for env_rel in ("web/.env", ".env"):
        env_path = os.path.join(os.path.dirname(__file__), "..", env_rel)
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(f"{prefix}=") and not line.startswith("#"):
                        key = line.split("=", 1)[1].strip().strip("'\"")
                        if key:
                            return key
        except FileNotFoundError:
            pass
    return key


POLL_INTERVAL = 15  # seconds
DEFAULT_TIMEOUT_MINUTES = 10
# After initial timeout, trigger redeploy and wait this many additional minutes
REDEPLOY_EXTRA_MINUTES = 5


def get_local_commit(ref="origin/master"):
    """Return the commit hash to compare deployed services against.

    Defaults to ``origin/master`` (what was last pushed). A push from a
    feature branch directly to master (via ``git push origin branch:master``
    or ``CAS_PROMOTE=1 git push``) does NOT advance local master, so local
    HEAD goes stale immediately after the push. If we used local HEAD as
    the target, the script would see the deployed services running a NEWER
    commit than local master, flag them as "wrong", and trigger a spurious
    auto-redeploy. Origin refs post-fetch are the authoritative "what should
    be deployed" state.

    Falls back to local HEAD if the origin ref can't be resolved (offline
    case, or when running from a checkout without an origin remote).
    """
    # Refresh origin so the local ref is up-to-date. Network failures here
    # are tolerated: we still try to rev-parse the cached origin ref.
    if ref.startswith("origin/"):
        remote_branch = ref.split("/", 1)[1]
        try:
            subprocess.run(
                ["git", "fetch", "origin", remote_branch, "--quiet"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            pass  # cached origin/<branch> may still resolve

    # Try the requested ref first; fall back to HEAD if it doesn't resolve
    # (e.g. offline + no cached origin ref, or non-standard checkout).
    for target in (ref, "HEAD"):
        result = subprocess.run(
            ["git", "rev-parse", target],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            sha = result.stdout.strip()
            if target != ref:
                print(f"[deploy-wait] WARN: {ref} not resolvable, falling back to local HEAD ({sha[:7]})",
                      file=sys.stderr)
            return sha

    print(f"[deploy-wait] git rev-parse failed for {ref} and HEAD", file=sys.stderr)
    sys.exit(1)


def fetch_json(url, headers=None, timeout=15):
    """Fetch a URL and parse JSON response."""
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310  # operator-configured CAS_PROD_URL / CAS_STAGING_URL health endpoint; dev script
        return json.loads(resp.read())


def _is_descendant_or_equal(target_commit, deployed_commit, fetched=None):
    """Return True if deployed_commit equals target_commit or descends from it.

    The autonomous executor frequently lands additional commits on master
    within seconds of a push. Railway then auto-deploys each, so the
    deployed SHA at observation time may be a descendant of the original
    push target. Treating a descendant as satisfying the target avoids
    spurious timeouts when prod is in fact running newer-or-equal code.

    Strategy:
      1. Compare full and 7-char prefixes (cheap, no git call).
      2. If different, attempt `git merge-base --is-ancestor <target> <deployed>`.
         The local repo may not have the deployed SHA cached, so we fetch
         origin once before retrying. ``fetched`` is a dict used as an
         out-parameter to ensure we only fetch once per polling loop.
      3. On any git error (unknown SHA, no remote, etc.), return False so
         the caller stays in "not ready" state and keeps polling. False
         negatives mean an extra poll cycle, never a wrong-positive ship.

    Args:
      target_commit: Full SHA we expected to deploy.
      deployed_commit: Full or short SHA reported by /health.
      fetched: Optional mutable dict; if provided, reuses fetch state
        across calls so descendant lookups don't re-fetch origin every poll.

    Returns:
      True if deployed_commit is the target or a descendant; False otherwise.
    """
    if not target_commit or not deployed_commit:
        return False

    if deployed_commit == target_commit:
        return True
    if deployed_commit.startswith(target_commit[:7]):
        return True
    if target_commit.startswith(deployed_commit[:7]):
        # /health reported a short SHA that matches a prefix of target.
        # Treat as equal: same commit, just truncated.
        return True

    # Different SHAs: check if deployed descends from target.
    def _is_ancestor(ancestor, descendant):
        try:
            result = subprocess.run(
                ["git", "merge-base", "--is-ancestor", ancestor, descendant],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    if _is_ancestor(target_commit, deployed_commit):
        return True

    # Deployed SHA may not be in our local object DB yet. Fetch origin once
    # and retry. Using the once-flag avoids fetching every polling cycle.
    if fetched is not None and not fetched.get("done"):
        fetched["done"] = True
        try:
            subprocess.run(
                ["git", "fetch", "origin", "--quiet"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            pass  # tolerated; the retry below just returns False
        if _is_ancestor(target_commit, deployed_commit):
            return True

    return False


def _commit_match_status(target_commit, deployed_commit, fetched=None):
    """Classify a deployed commit against the target.

    Returns one of:
      "equal": exact match (or short-prefix match).
      "descendant": deployed is a descendant of target.
      "diverged": deployed is neither equal nor a descendant.
    """
    if not target_commit or not deployed_commit:
        return "diverged"
    if deployed_commit == target_commit:
        return "equal"
    if deployed_commit.startswith(target_commit[:7]):
        return "equal"
    if target_commit.startswith(deployed_commit[:7]):
        return "equal"
    if _is_descendant_or_equal(target_commit, deployed_commit, fetched=fetched):
        return "descendant"
    return "diverged"


def check_web_health(target_commit, fetched=None):
    """Check if the web service is healthy and has the target commit deployed.

    A deployed commit that is a descendant of ``target_commit`` is treated
    as satisfying the wait: the autonomous executor frequently lands
    follow-up commits on master and Railway auto-deploys each, so by the
    time we poll, prod may be on a newer-than-target SHA that still
    contains the target's changes.

    Returns (ready, info_string).
    """
    try:
        data = fetch_json(f"{BASE_URL}/health")
        status = data.get("status", "unknown")
        deployed_commit = data.get("git_commit", "")

        if status != "ok":
            return False, f"health={status}"

        if deployed_commit:
            short = deployed_commit[:7]
            target_short = target_commit[:7]
            match = _commit_match_status(target_commit, deployed_commit, fetched=fetched)
            if match == "equal":
                return True, f"healthy, commit={short} [OK]"
            if match == "descendant":
                return True, f"healthy, commit={short} [OK descendant of {target_short}]"
            return False, f"healthy, commit={short} (want {target_short})"
        else:
            # No commit info in health response; assume OK if healthy
            return True, "healthy (no commit info)"

    except Exception as exc:
        return False, f"error: {str(exc)[:60]}"


def check_worker_deployed(target_commit, fetched=None):
    """Check if the worker has redeployed by reading its heartbeat from /health.

    The worker writes its git commit and start time to worker_heartbeat table
    on startup. The /health endpoint exposes this. A deployed commit that is
    a descendant of ``target_commit`` is treated as satisfying the wait
    (see ``check_web_health`` for rationale).

    Returns (ready, info_string).
    """
    try:
        data = fetch_json(f"{BASE_URL}/health")
        worker = data.get("worker")
        if not worker:
            return False, "no heartbeat yet"

        worker_commit = worker.get("git_commit", "")
        started_at = worker.get("started_at", "")

        if worker_commit:
            short = worker_commit[:7]
            target_short = target_commit[:7]
            match = _commit_match_status(target_commit, worker_commit, fetched=fetched)
            if match == "equal":
                return True, f"commit={short}, started={started_at} [OK]"
            if match == "descendant":
                return True, f"commit={short}, started={started_at} [OK descendant of {target_short}]"
            return False, f"commit={short} (want {target_short}), started={started_at}"
        else:
            # No commit info (local dev or pre-heartbeat deploy)
            return True, f"no commit info, started={started_at}"

    except Exception as exc:
        return False, f"error: {str(exc)[:60]}"


def _get_railway_token():
    """Get Railway API token from environment or web/.env."""
    return _get_env_key("RAILWAY_API_TOKEN")


def trigger_redeploy(service_id, service_name="service"):
    """Trigger a Railway serviceInstanceRedeploy via GraphQL API.

    Returns True if the mutation succeeded, False otherwise.
    """
    token = _get_railway_token()
    if not token:
        print(f"[deploy-wait] Cannot redeploy {service_name}: RAILWAY_API_TOKEN not set",
              file=sys.stderr)
        return False
    if not service_id or not ENVIRONMENT_ID:
        print(f"[deploy-wait] Cannot redeploy {service_name}: Railway service/environment "
              f"ID not set (see the Configuration section in --help)", file=sys.stderr)
        return False

    query = """
        mutation Redeploy($serviceId: String!, $environmentId: String!) {
            serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
        }
    """
    variables = {"serviceId": service_id, "environmentId": ENVIRONMENT_ID}
    payload = json.dumps({"query": query, "variables": variables}).encode()

    req = urllib.request.Request(
        RAILWAY_GQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "CAS-Deploy/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310  # fixed https Railway GraphQL URL (RAILWAY_GQL_URL)
            data = json.loads(resp.read())
            if "errors" in data:
                print(f"[deploy-wait] Redeploy {service_name} GraphQL error: {data['errors']}",
                      file=sys.stderr)
                return False
            print(f"[deploy-wait] Triggered redeploy for {service_name}")
            return True
    except Exception as exc:
        print(f"[deploy-wait] Redeploy {service_name} failed: {exc}", file=sys.stderr)
        return False


def wait_for_deploy(timeout_minutes=DEFAULT_TIMEOUT_MINUTES, once=False,
                    service="both", auto_redeploy=True, target_ref="origin/master"):
    """Wait for CAS services to deploy.

    After a push to master, Railway auto-deploys both web and worker.
    Web typically takes 2-3 minutes, worker 3-5 minutes.

    If auto-deploy doesn't trigger within the timeout and auto_redeploy is True,
    falls back to manually triggering serviceInstanceRedeploy via Railway API,
    then waits an additional REDEPLOY_EXTRA_MINUTES for the deploy to complete.

    Args:
        service: "web", "worker", or "both". Controls which service(s) must
                 match the target commit. Skipped services are reported as
                 "(skip)" in status output.
        auto_redeploy: If True, trigger manual redeploy on timeout before giving up.

    Returns 0 on success, 1 on timeout/failure.
    """
    wait_web = service in ("web", "both")
    wait_worker = service in ("worker", "both")

    target_commit = get_local_commit(ref=target_ref)
    short = target_commit[:7]
    svc_label = service if service != "both" else "web + worker"
    print(f"[deploy-wait] Waiting for {svc_label} to deploy commit {short}...")

    # Wait a minimum of 30s for Railway to start the build
    if not once:
        print("[deploy-wait] Waiting 30s for Railway to start building...")
        time.sleep(30)

    deadline = time.time() + timeout_minutes * 60
    redeploy_triggered = False

    while True:
        # Per-iteration descendant-fetch state. Shared across web + worker
        # checks so we fetch origin at most once per polling cycle (only
        # when a deployed SHA fails the cheap prefix check).
        fetched = {"done": False}

        if wait_web:
            web_ready, web_info = check_web_health(target_commit, fetched=fetched)
        else:
            web_ready, web_info = True, "(skip)"

        if wait_worker:
            worker_ready, worker_info = check_worker_deployed(target_commit, fetched=fetched)
        else:
            worker_ready, worker_info = True, "(skip)"

        status = f"web: {web_info} | worker: {worker_info}"

        if web_ready and worker_ready:
            print(f"[deploy-wait] Ready: {status}")
            return 0

        if once:
            print(f"[deploy-wait] Not ready: {status}", file=sys.stderr)
            return 1

        if time.time() > deadline:
            if auto_redeploy and not redeploy_triggered:
                # Auto-deploy didn't trigger; fall back to manual redeploy
                print(f"[deploy-wait] Auto-deploy timed out after {timeout_minutes}m. "
                      f"Triggering manual redeploy...")
                triggered_any = False
                if wait_web and not web_ready:
                    if trigger_redeploy(WEB_SERVICE_ID, "web"):
                        triggered_any = True
                if wait_worker and not worker_ready:
                    if trigger_redeploy(WORKER_SERVICE_ID, "worker"):
                        triggered_any = True

                if triggered_any:
                    redeploy_triggered = True
                    deadline = time.time() + REDEPLOY_EXTRA_MINUTES * 60
                    print(f"[deploy-wait] Waiting {REDEPLOY_EXTRA_MINUTES}m more after redeploy...")
                    time.sleep(POLL_INTERVAL)
                    continue
                else:
                    print("[deploy-wait] No redeploy triggered (missing token or API error)",
                          file=sys.stderr)
                    return 1
            else:
                total = timeout_minutes + (REDEPLOY_EXTRA_MINUTES if redeploy_triggered else 0)
                print(f"[deploy-wait] Timed out after {total}m: {status}",
                      file=sys.stderr)
                return 1

        print(f"[deploy-wait] {status}")
        time.sleep(POLL_INTERVAL)


def main():
    parser = argparse.ArgumentParser(
        description="Wait for CAS Railway services to deploy the latest commit.",
        epilog="Configure via CAS_PROD_URL (and the RAILWAY_* variables for the "
               "redeploy fallback); see the module docstring.",
    )
    parser.add_argument("--once", action="store_true",
                        help="Check once and exit 1 if not ready")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MINUTES,
                        help=f"Timeout in minutes (default: {DEFAULT_TIMEOUT_MINUTES})")
    parser.add_argument("--service", choices=["web", "worker", "both"],
                        default="both",
                        help="Which service(s) to wait for (default: both)")
    parser.add_argument("--redeploy", action="store_true",
                        help="Skip wait and trigger redeploy immediately")
    parser.add_argument("--no-auto-redeploy", action="store_true",
                        help="Disable automatic redeploy fallback on timeout")
    parser.add_argument("--staging", action="store_true",
                        help="Target the staging environment instead of production")
    args = parser.parse_args()

    # Switch to staging IDs if --staging flag is set
    global BASE_URL, WEB_SERVICE_ID, WORKER_SERVICE_ID, ENVIRONMENT_ID
    if args.staging:
        if not STAGING_BASE_URL:
            print("[deploy-wait] ERROR: CAS_STAGING_URL env var required for --staging")
            return 1
        BASE_URL = STAGING_BASE_URL.rstrip("/")
        if STAGING_WEB_SERVICE_ID:
            WEB_SERVICE_ID = STAGING_WEB_SERVICE_ID
        if STAGING_WORKER_SERVICE_ID:
            WORKER_SERVICE_ID = STAGING_WORKER_SERVICE_ID
        if STAGING_ENVIRONMENT_ID:
            ENVIRONMENT_ID = STAGING_ENVIRONMENT_ID
        print(f"[deploy-wait] Targeting staging: {BASE_URL}")

    if not BASE_URL:
        print("[deploy-wait] ERROR: CAS_PROD_URL env var (base URL of the deployed web app) is required",
              file=sys.stderr)
        return 1

    # Target ref: origin/staging when targeting staging env, origin/master otherwise.
    # Using origin/<branch> (fetched post-push) instead of local HEAD ensures we
    # compare deployed services against what was actually pushed, not against
    # a stale local master that may not have been pulled yet after the push.
    target_ref = "origin/staging" if args.staging else "origin/master"

    if args.redeploy:
        # Immediate redeploy mode
        print("[deploy-wait] Triggering immediate redeploy...")
        ok = True
        if args.service in ("web", "both"):
            ok = trigger_redeploy(WEB_SERVICE_ID, "web") and ok
        if args.service in ("worker", "both"):
            ok = trigger_redeploy(WORKER_SERVICE_ID, "worker") and ok
        if not ok:
            return 1
        print("[deploy-wait] Redeploy triggered. Waiting for services...")
        return wait_for_deploy(
            timeout_minutes=REDEPLOY_EXTRA_MINUTES,
            service=args.service,
            auto_redeploy=False,
            target_ref=target_ref,
        )

    return wait_for_deploy(
        timeout_minutes=args.timeout,
        once=args.once,
        service=args.service,
        auto_redeploy=not args.no_auto_redeploy,
        target_ref=target_ref,
    )


if __name__ == "__main__":
    sys.exit(main())
