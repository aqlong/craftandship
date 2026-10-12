# queue/watchdog_probe.py: Shared zombie-deploy reality probe
#
# Used by BOTH queue/worker_pg.py (supervisor force-fail path) and
# coordinator/agents/reconciliation.py (background reconciliation sweep).
#
# When a build thread hangs past its deadline it may still have finished the
# Railway deploy. Before the supervisor writes `failed`, the probe lists Railway
# projects, keeps those whose NAME equals the build's app_name and whose
# creation time is inside the build's window, and GETs /health on each. The
# first one that answers healthy wins, by the same definition a fresh deploy
# has to meet (cli/deploy.py health_response_ok: HTTP 200 with a JSON body
# whose status is "ok"). A redirect is not an answer, so the probe does not
# follow one. The caller then routes the build to `preview` (human review),
# never to `live`.
#
# Fail-safe: any error or timeout returns None, which leaves the caller on its
# normal failure path. False positives are bounded: a match only produces a
# preview that a reviewer must still approve. The match is by project name, and
# app_name is not unique across customers, so two same-named projects created
# inside the window can be confused; the reviewer sees the URL before anything
# is published.

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("queue.watchdog_probe")

# Wall-clock budget for the whole probe (list + resolve + /health loop). Every
# step draws on it: the Railway calls are given the deadline (cli/deploy.py
# gql stops retrying at it and bounds each request with what is left), and
# each /health request is one bounded exchange of at most PROBE_PER_URL_S or
# the time left, whichever is smaller. What can still run past it is a name
# lookup that the C resolver will not give up (see monitor/bounded_fetch.py).
PROBE_DEADLINE_S: int = 20

# A Railway domain is a bare hostname; anything else is not probed.
_HOSTNAME_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?")

# Deadline of one /health request, whole exchange (lookup, connect, TLS,
# redirects, headers and body).
PROBE_PER_URL_S: int = 5

# How much of a /health body is read. A healthy answer is a few dozen bytes;
# a longer one is cut here, and a cut body does not parse as healthy.
PROBE_MAX_BODY_BYTES: int = 4096

# Time window around the build's started_at / completed_at to consider
PROBE_WINDOW_BEFORE_S: int = 5 * 60    # started_at - 5 min
PROBE_WINDOW_AFTER_S: int = 30 * 60    # completed_at + 30 min


def probe_for_zombie_deploy(
    app_name: str,
    started_at,
    completed_at=None,
    hard_timeout_s: int = PROBE_DEADLINE_S,
    exclude_project_ids=None,
) -> Optional[dict]:
    """Probe Railway for a live deployment matching *app_name* in the time window.

    Ownership: the match is by project NAME, and app_name is not unique (two
    customers can both build "my-studio", and one customer can rebuild). A
    project that is already recorded on a different build row belongs to that
    build, so callers pass those ids in ``exclude_project_ids`` and they are
    never returned. What remains uncovered is a same-named project of another
    build that is itself still mid-deploy (its id is not in the database yet);
    that needs MAX_CONCURRENT_BUILDS > 1.

    Algorithm:
      1. List all Railway projects for the current API token.
      2. Filter to ``project.name == app_name`` whose ``createdAt`` falls in
         ``[started_at - PROBE_WINDOW_BEFORE_S, (completed_at or now) + PROBE_WINDOW_AFTER_S]``.
      3. For each match (freshest first), resolve the first public service domain
         and GET ``https://{domain}/health``.
      4. The first healthy answer (``deploy.health_response_ok``) wins; return
         ``{"live_url": ..., "project_id": ..., "service_id": ...}``.
      5. Fail-safe: every exception, network error, unhealthy answer, or deadline exceeded
         returns ``None``. This function MUST NOT raise.

    Args:
        app_name: The CAS build's app_name (== Railway project name).
        started_at: ``builds.started_at`` (datetime or None).
        completed_at: ``builds.completed_at`` (datetime or None).
            None means use now() as upper bound.
        hard_timeout_s: Wall-clock budget for the whole probe (see PROBE_DEADLINE_S).
        exclude_project_ids: Railway project ids that belong to other builds.

    Returns:
        dict with keys ``live_url``, ``project_id``, ``service_id`` on a live
        match, else ``None``.
    """
    try:
        excluded = {str(pid) for pid in (exclude_project_ids or ()) if pid}
        return _probe(app_name, started_at, completed_at, hard_timeout_s, excluded)
    except Exception as exc:  # contract: the supervisor must never see an exception here
        logger.warning("watchdog_probe: unexpected error (%s); skipping", exc)
        return None


def _probe(app_name, started_at, completed_at, hard_timeout_s, excluded=frozenset()) -> Optional[dict]:
    """Implementation of probe_for_zombie_deploy; may raise on unexpected input."""
    if not app_name:
        return None

    deadline_monotonic = time.monotonic() + hard_timeout_s

    def _deadline_exceeded() -> bool:
        return time.monotonic() >= deadline_monotonic

    # Lazy-import deploy helpers so this module stays importable in test
    # environments where RAILWAY_API_TOKEN is not set.
    try:
        import deploy as _deploy  # queue sys.path injects cli/
    except Exception:
        try:
            from cli import deploy as _deploy  # fallback for package imports
        except Exception as exc:
            logger.warning(
                "watchdog_probe: deploy module unavailable (%s); skipping", exc
            )
            return None

    try:
        token = _deploy.get_token()
    except Exception as exc:  # DeployConfigError when RAILWAY_API_TOKEN is not set
        logger.warning("watchdog_probe: no Railway token (%s); skipping", exc)
        return None

    if _deadline_exceeded():
        return None

    def _as_utc(dt):
        if dt is None:
            return None
        if getattr(dt, "tzinfo", None) is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    started_utc = _as_utc(started_at)
    completed_utc = _as_utc(completed_at) or datetime.now(timezone.utc)
    window_start = (
        started_utc - timedelta(seconds=PROBE_WINDOW_BEFORE_S)
        if started_utc else None
    )
    window_end = completed_utc + timedelta(seconds=PROBE_WINDOW_AFTER_S)

    try:
        projects = _deploy.list_projects(token, deadline=deadline_monotonic) or []
    except Exception as exc:
        logger.warning(
            "watchdog_probe: list_projects failed (%s); skipping", exc
        )
        return None

    if _deadline_exceeded():
        return None

    # Filter candidates: name match + createdAt in window.
    candidates = []
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        if proj.get("name") != app_name:
            continue
        created_raw = proj.get("createdAt")
        created_dt = None
        if created_raw:
            try:
                created_dt = datetime.fromisoformat(
                    created_raw.replace("Z", "+00:00")
                )
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
            except Exception:
                created_dt = None
        if created_dt is None:
            continue
        if window_start and created_dt < window_start:
            continue
        if created_dt > window_end:
            continue
        candidates.append((created_dt, proj))

    if not candidates:
        return None

    # Freshest first: a late-arriving zombie is more likely on the newest project.
    candidates.sort(key=lambda item: item[0], reverse=True)

    from monitor.ping import guarded_session, probe_url_problem

    for _created_dt, proj in candidates:
        if _deadline_exceeded():
            return None
        project_id = proj.get("id")
        if not project_id:
            continue
        if str(project_id) in excluded:
            logger.info(
                "watchdog_probe: project %s is recorded on another build; not a candidate",
                project_id,
            )
            continue
        try:
            domain_info = _deploy.get_service_domain(project_id, token, deadline=deadline_monotonic)
        except Exception as exc:
            logger.warning(
                "watchdog_probe: get_service_domain(%s) failed (%s); "
                "skipping candidate",
                project_id, exc,
            )
            continue
        if not domain_info:
            continue
        domain = domain_info.get("domain")
        service_id = domain_info.get("service_id")
        if not domain:
            continue
        # fullmatch: with match() and a `$`, "name\n" would pass.
        if not isinstance(domain, str) or not _HOSTNAME_RE.fullmatch(domain):
            logger.warning("watchdog_probe: ignoring malformed domain %r", domain)
            continue
        health_url = f"https://{domain}/health"
        problem = probe_url_problem(health_url)
        if problem:
            logger.warning("watchdog_probe: not probing %r (%s)", domain, problem)
            continue
        seconds_left = deadline_monotonic - time.monotonic()
        if seconds_left <= 0:
            return None
        session = guarded_session(max_body_bytes=PROBE_MAX_BODY_BYTES)
        try:
            # The app behind this domain is customer code: it may answer
            # slowly or endlessly. No redirects: a healthy /health answers 200
            # itself (cli/deploy.py's health check does the same), so a 302 is
            # not healthy, whatever it points at. The guarded session bounds the
            # exchange by the timeout and connects only to an address it checked.
            resp = session.get(health_url, timeout=min(PROBE_PER_URL_S, seconds_left),
                               allow_redirects=False)
            status_code = resp.status_code
            body_text = resp.text if isinstance(resp.text, str) else ""
        except Exception as exc:
            logger.info(
                "watchdog_probe: %s unreachable (%s); trying next",
                health_url, exc,
            )
            continue
        finally:
            session.close()
        if _deploy.health_response_ok(status_code, body_text):
            logger.warning(
                "watchdog_probe: ZOMBIE DEPLOY DETECTED, %s is live at %s "
                "(project=%s); routing to preview",
                app_name, health_url, project_id,
            )
            return {
                "live_url": f"https://{domain}",
                "project_id": project_id,
                "service_id": service_id,
            }
        logger.info(
            "watchdog_probe: %s returned %s; trying next candidate",
            health_url, status_code,
        )

    return None
