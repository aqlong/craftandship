# monitor/registry.py - PostgreSQL registry of monitored apps
#
# Stores metadata for every deployed app: URLs, Railway IDs, and health status.
# DATABASE_URL is intentionally NOT stored here; it is fetched fresh from the
# Railway API each time it is needed (see backup.get_service_database_url).
#
# Migrated from SQLite (ephemeral on Railway) to PostgreSQL for persistence
# across redeploys.

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _get_conn():
    """Get a psycopg2 connection using DATABASE_URL."""
    import psycopg2
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(db_url)


def _row_to_dict(cur, row) -> dict:
    """Convert a DB row tuple to a dict using cursor description."""
    if row is None:
        return None
    return {col.name: val for col, val in zip(cur.description, row)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_app(
    app_name: str,
    live_url: str,
    railway_project_id: str,
    railway_service_id: str,
    railway_environment_id: str,
) -> str:
    """Register a new app in the registry and return its assigned UUID.

    Args:
        app_name: Human-readable name for the app.
        live_url: Public HTTPS URL (e.g. https://myapp.up.railway.app).
        railway_project_id: Railway project ID for this app.
        railway_service_id: Railway service ID for the app service.
        railway_environment_id: Railway environment ID (usually production).

    Returns:
        UUID string for the newly registered app.
    """
    app_id = str(uuid.uuid4())
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # Upsert on railway_project_id: rebuild of the same app updates the
            # existing row instead of creating a duplicate monitored entry.
            cur.execute(
                """
                INSERT INTO monitor_apps
                    (id, app_name, live_url, railway_project_id, railway_service_id,
                     railway_environment_id, status, consecutive_failures)
                VALUES (%s, %s, %s, %s, %s, %s, 'unknown', 0)
                ON CONFLICT (railway_project_id) DO UPDATE SET
                    app_name = EXCLUDED.app_name,
                    live_url = EXCLUDED.live_url,
                    railway_service_id = EXCLUDED.railway_service_id,
                    railway_environment_id = EXCLUDED.railway_environment_id,
                    status = 'unknown',
                    consecutive_failures = 0
                RETURNING id
                """,
                (
                    app_id,
                    app_name,
                    live_url,
                    railway_project_id,
                    railway_service_id,
                    railway_environment_id,
                ),
            )
            row = cur.fetchone()
            # If a conflict was resolved, return the existing row's id.
            if row:
                app_id = row[0]
        conn.commit()
    finally:
        conn.close()
    return app_id


def get_app(app_id: str) -> dict:
    """Return a single app record by ID.

    Args:
        app_id: UUID of the app.

    Returns:
        Dict representation of the app row.

    Raises:
        KeyError: If no app with that ID exists.
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM monitor_apps WHERE id = %s", (app_id,))
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"No app found with id '{app_id}'")
            return _row_to_dict(cur, row)
    finally:
        conn.close()


def list_apps(status: str = None) -> list:
    """Return all registered apps, optionally filtered by status.

    Args:
        status: If provided, return only apps with this status string
                ('healthy', 'degraded', 'down', or 'unknown').

    Returns:
        List of app dicts, ordered by created_at ascending.
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            if status is not None:
                cur.execute(
                    "SELECT * FROM monitor_apps WHERE status = %s ORDER BY created_at ASC",
                    (status,),
                )
            else:
                cur.execute(
                    "SELECT * FROM monitor_apps ORDER BY created_at ASC"
                )
            rows = cur.fetchall()
            return [_row_to_dict(cur, row) for row in rows]
    finally:
        conn.close()


def update_status(app_id: str, status: str, note: str = None, latency_ms: int = None,
                  *, check_result: bool = True) -> dict:
    """Update the health status of an app and refresh last_checked.

    consecutive_failures counts consecutive health-check cycles in which the
    app did not answer; monitor.ping.run_checks drops an app from the registry
    when it reaches 10. With check_result=True (the outcome of a health check):
      * 'healthy' or 'degraded' means the app answered (at once, or on a
        retry), so the counter is reset to 0;
      * 'down' adds one.
    With check_result=False the status is recorded but the counter is left
    alone. monitor.diagnose uses that: it runs after a failed check that has
    already been counted, and its own 'degraded' ("a fix was applied") or
    'down' ("escalated") is not another failed cycle.

    Updates last_healthy when status becomes 'healthy'.
    Tracks latency and computes baseline using exponential moving average.

    Args:
        app_id: UUID of the app to update.
        status: New status string: 'healthy', 'degraded', 'down', or 'unknown'.
        note: Optional diagnostic note (currently logged but not stored in DB).
        latency_ms: Response time in milliseconds from the health check.
        check_result: False when the status does not come from a health check.

    Returns:
        Dict with 'latency_degraded' (bool) if latency >2x baseline.
    """
    now = datetime.now(timezone.utc)
    app = get_app(app_id)
    current_failures = app["consecutive_failures"]

    last_healthy = now if status == "healthy" else app["last_healthy"]
    if not check_result:
        new_failures = current_failures
    elif status in ("healthy", "degraded"):
        new_failures = 0
    elif status == "down":
        new_failures = current_failures + 1
    else:
        new_failures = current_failures

    if note:
        logger.info("[registry] %s status -> %s: %s", app_id, status, note)

    # Latency tracking: exponential moving average for baseline
    baseline = app.get("baseline_latency_ms")
    latency_degraded = False
    if latency_ms is not None and status == "healthy":
        if baseline is None:
            baseline = latency_ms
        else:
            # Evaluate degradation against the PRE-update baseline, then fold
            # the new sample in. Folding first pulls the baseline toward the
            # spike, diluting the `2 * baseline` threshold so a genuine 2x spike
            # could slip under it on first occurrence.
            if latency_ms > 2 * baseline:
                latency_degraded = True
            # EMA with alpha=0.2 (slow-moving baseline)
            baseline = int(0.8 * baseline + 0.2 * latency_ms)

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE monitor_apps
                SET status = %s,
                    last_checked = %s,
                    last_healthy = %s,
                    consecutive_failures = %s,
                    baseline_latency_ms = %s,
                    last_latency_ms = %s
                WHERE id = %s
                """,
                (status, now, last_healthy, new_failures,
                 baseline, latency_ms, app_id),
            )
        conn.commit()
    finally:
        conn.close()

    return {"latency_degraded": latency_degraded}


def remove_app(app_id: str) -> None:
    """Delete an app from the registry.

    Args:
        app_id: UUID of the app to remove.

    Raises:
        KeyError: If no app with that ID exists.
    """
    get_app(app_id)  # raises KeyError if not found
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM monitor_apps WHERE id = %s", (app_id,))
        conn.commit()
    finally:
        conn.close()
