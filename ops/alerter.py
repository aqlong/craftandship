# ops/alerter.py - Escalation emails for the ops agent
#
# Sends alert emails to the admin when guardrails trigger, fixes are
# applied, or unfixable errors are detected. Uses Resend REST API
# directly (same pattern as monitor/diagnose.py).

import html as _html
import json
import logging
import os
import time
from typing import Optional
from urllib.request import Request
from urllib.error import URLError

from coordinator.envflags import safety_switch
from monitor.bounded_fetch import bounded_urlopen

logger = logging.getLogger("ops.alerter")

_DEFAULT_FROM = "Craft & Ship Ops <noreply@craftandship.io>"

# Cooldown: don't re-alert on the same unfixable category more than once per 4 hours.
_UNFIXABLE_COOLDOWN_SECONDS = 14400
_last_unfixable_alert: dict = {}  # category -> time.monotonic() of the last alert


def _esc(value) -> str:
    """HTML-escape any value interpolated into an alert email body."""
    return _html.escape(str(value), quote=True)


def _from_address() -> str:
    return os.environ.get("RESEND_FROM_EMAIL") or _DEFAULT_FROM


def _is_dry_run() -> bool:
    """Check if the ops agent is in dry-run mode."""
    return safety_switch("OPS_DRY_RUN")


def _send_email(to: Optional[str], subject: str, html: str) -> bool:
    """Send an email via Resend REST API. Returns True on success."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logger.warning("RESEND_API_KEY not set; skipping alert email")
        return False
    if not to:
        logger.warning("ADMIN_EMAIL not set; skipping alert email")
        return False

    payload = json.dumps({
        "from": _from_address(),
        "to": [to],
        "subject": subject,
        "html": html,
    }).encode()

    req = Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        # One bounded exchange: 10 seconds for the whole request, 64 KiB of answer.
        with bounded_urlopen(req, timeout=10, max_body_bytes=64 * 1024) as resp:
            if resp.status in (200, 201):
                logger.info("Alert email sent: %s", subject)
                return True
            logger.warning("Resend returned %d for alert: %s", resp.status, subject)
            return False
    except (URLError, OSError) as exc:
        logger.error("Failed to send alert email: %s", exc)
        return False


def _admin_email() -> Optional[str]:
    """Return the admin email address from ADMIN_EMAIL, or None when unset."""
    return os.environ.get("ADMIN_EMAIL", "").strip() or None


def send_admin_alert(subject: str, body: str) -> bool:
    """Email a plain-text alert to ADMIN_EMAIL. Used by coordinator.actuators.

    The recipient is fixed (ADMIN_EMAIL); callers cannot choose it. The body is
    HTML-escaped and rendered in a <pre> block because it routinely contains
    customer-written text (feedback messages, build errors).
    """
    html = f'<pre style="font-family:inherit;white-space:pre-wrap;">{_esc(body[:5000])}</pre>'
    return _send_email(_admin_email(), subject[:200], html)


def alert_fix_applied(
    category: str,
    description: str,
    dry_run: bool = False,
) -> None:
    """Notify admin that a fix was applied (or would be in dry-run mode)."""
    if dry_run:
        logger.info("Dry-run: skipping fix_applied alert for %s", category)
        return
    mode = ""
    subject = f"Ops Agent: fix applied ({category})"
    html = f"""
    <h2>{mode}Ops Agent Fix Applied</h2>
    <p><strong>Category:</strong> {_esc(category)}</p>
    <p><strong>Description:</strong> {_esc(description)}</p>
    <p>The ops agent automatically applied this fix to one of the platform's Railway services
    (an environment variable or a redeploy). It committed nothing to the repository.</p>
    <hr>
    <p style="color:#888;font-size:0.85em;">
        To disable auto-fixes, set OPS_DRY_RUN=true on the worker service.
    </p>
    """
    _send_email(_admin_email(), subject, html)


def alert_circuit_breaker(recent_count: int, limit: int, window_minutes: int) -> None:
    """Notify admin that the circuit breaker tripped."""
    subject = "Ops Agent: circuit breaker tripped"
    html = f"""
    <h2>Circuit Breaker Tripped</h2>
    <p>The ops agent has applied <strong>{recent_count}</strong> fixes in the last
    <strong>{window_minutes}</strong> minutes (limit: {limit}).</p>
    <p>Auto-fixes are paused until the window resets. This may indicate a
    systemic issue that needs manual investigation.</p>
    <hr>
    <p style="color:#888;font-size:0.85em;">
        Adjust limits with OPS_CIRCUIT_BREAKER_LIMIT and OPS_CIRCUIT_BREAKER_WINDOW_MINUTES.
    </p>
    """
    _send_email(_admin_email(), subject, html)


def alert_scan_summary(summary: dict, details: list, dry_run: bool = False) -> None:
    """Send a scan summary email after each cycle that found patterns.

    Args:
        summary: {patterns_found, fixes_applied, fixes_skipped, escalations}
        details: List of dicts with {category, description, action, build_count}
        dry_run: Whether the agent is in dry-run mode.
    """
    if summary["patterns_found"] == 0:
        return  # Nothing to report

    if dry_run:
        logger.info("Dry-run: skipping scan summary alert (%d patterns)", summary["patterns_found"])
        return

    mode = "[DRY RUN] " if dry_run else ""
    subject = f"{mode}Ops Agent scan: {summary['patterns_found']} patterns, {summary['fixes_applied']} fixed"

    rows = ""
    for detail in details:
        action_color = {
            "fixed": "#4ade80",
            "would_fix": "#60a5fa",
            "skipped": "#f59e0b",
            "escalated": "#f87171",
        }.get(detail.get("action", ""), "#94a3b8")

        rows += f"""
        <tr>
            <td style="padding:0.5rem 1rem;border-bottom:1px solid #334155;color:#e2e8f0;">
                {_esc(detail.get('category', 'unknown'))}
            </td>
            <td style="padding:0.5rem 1rem;border-bottom:1px solid #334155;color:#cbd5e1;">
                {_esc(detail.get('description', '')[:100])}
            </td>
            <td style="padding:0.5rem 1rem;border-bottom:1px solid #334155;">
                <span style="color:{action_color};font-weight:600;">{_esc(detail.get('action', ''))}</span>
            </td>
            <td style="padding:0.5rem 1rem;border-bottom:1px solid #334155;color:#94a3b8;text-align:center;">
                {detail.get('build_count', 0)}
            </td>
        </tr>
        """

    html = f"""
    <div style="font-family:sans-serif;max-width:600px;">
        <h2 style="color:#f1f5f9;">{mode}Ops Agent Scan Summary</h2>

        <div style="display:flex;gap:1rem;margin-bottom:1.5rem;">
            <div style="background:#1e293b;border-radius:8px;padding:0.75rem 1.25rem;text-align:center;">
                <div style="color:#64748b;font-size:0.75rem;">Patterns</div>
                <div style="color:#e2e8f0;font-size:1.5rem;font-weight:700;">{summary['patterns_found']}</div>
            </div>
            <div style="background:#1e293b;border-radius:8px;padding:0.75rem 1.25rem;text-align:center;">
                <div style="color:#64748b;font-size:0.75rem;">Fixed</div>
                <div style="color:#4ade80;font-size:1.5rem;font-weight:700;">{summary['fixes_applied']}</div>
            </div>
            <div style="background:#1e293b;border-radius:8px;padding:0.75rem 1.25rem;text-align:center;">
                <div style="color:#64748b;font-size:0.75rem;">Skipped</div>
                <div style="color:#f59e0b;font-size:1.5rem;font-weight:700;">{summary['fixes_skipped']}</div>
            </div>
            <div style="background:#1e293b;border-radius:8px;padding:0.75rem 1.25rem;text-align:center;">
                <div style="color:#64748b;font-size:0.75rem;">Escalated</div>
                <div style="color:#f87171;font-size:1.5rem;font-weight:700;">{summary['escalations']}</div>
            </div>
        </div>

        <table style="width:100%;border-collapse:collapse;background:#1e293b;border-radius:8px;overflow:hidden;">
            <thead>
                <tr style="border-bottom:2px solid #334155;">
                    <th style="padding:0.5rem 1rem;text-align:left;color:#64748b;font-size:0.8rem;">Category</th>
                    <th style="padding:0.5rem 1rem;text-align:left;color:#64748b;font-size:0.8rem;">Description</th>
                    <th style="padding:0.5rem 1rem;text-align:left;color:#64748b;font-size:0.8rem;">Action</th>
                    <th style="padding:0.5rem 1rem;text-align:center;color:#64748b;font-size:0.8rem;">Builds</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>

        <p style="color:#64748b;font-size:0.85em;margin-top:1rem;">
            {"Dry-run mode: no changes were actually made. Set OPS_DRY_RUN=false to enable auto-fixes." if dry_run else "Live mode: the fixes above were applied to the platform's Railway services (environment variables and redeploys). Nothing was committed to the repository."}
        </p>
    </div>
    """
    _send_email(_admin_email(), subject, html)


def alert_hourly_digest(conn) -> None:
    """Send an hourly status digest email with build and system stats.

    Queries the database for current build status counts, recent activity,
    and sends a summary email. Skips sending if nothing happened in the last hour.

    Args:
        conn: psycopg2 database connection.
    """
    # Skip digest emails in dry-run mode to conserve Resend quota
    if _is_dry_run():
        logger.info("Dry-run: skipping hourly digest email")
        return

    try:
        with conn.cursor() as cur:
            # Overall stats
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'queued') AS queued,
                    COUNT(*) FILTER (WHERE status = 'building') AS building,
                    COUNT(*) FILTER (WHERE status = 'testing') AS testing,
                    COUNT(*) FILTER (WHERE status = 'deploying') AS deploying,
                    COUNT(*) FILTER (WHERE status = 'live') AS live,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed
                FROM builds
            """)
            stats = cur.fetchone()
            total, queued, building, testing, deploying, live, failed = stats

            # Activity in last hour
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'live' AND completed_at >= NOW() - INTERVAL '1 hour') AS completed,
                    COUNT(*) FILTER (WHERE status = 'failed' AND completed_at >= NOW() - INTERVAL '1 hour') AS failed_recent,
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '1 hour') AS new_builds
                FROM builds
            """)
            activity = cur.fetchone()
            completed_1h, failed_1h, new_1h = activity

            # Customer count
            cur.execute("SELECT COUNT(*) FROM customers")
            customer_count = cur.fetchone()[0]

        # Skip if nothing happened
        active = queued + building + testing + deploying
        if completed_1h == 0 and failed_1h == 0 and new_1h == 0 and active == 0:
            logger.info("Hourly digest: nothing happened, skipping email")
            return

        subject = f"CAS Hourly: {live} live, {failed} failed, {active} active"

        html = f"""
        <div style="font-family:sans-serif;max-width:500px;">
            <h2 style="color:#f1f5f9;">Craft & Ship Hourly Digest</h2>

            <div style="display:flex;gap:0.75rem;margin-bottom:1.5rem;flex-wrap:wrap;">
                <div style="background:#1e293b;border-radius:8px;padding:0.6rem 1rem;text-align:center;min-width:70px;">
                    <div style="color:#64748b;font-size:0.7rem;">Live</div>
                    <div style="color:#4ade80;font-size:1.3rem;font-weight:700;">{live}</div>
                </div>
                <div style="background:#1e293b;border-radius:8px;padding:0.6rem 1rem;text-align:center;min-width:70px;">
                    <div style="color:#64748b;font-size:0.7rem;">Failed</div>
                    <div style="color:#f87171;font-size:1.3rem;font-weight:700;">{failed}</div>
                </div>
                <div style="background:#1e293b;border-radius:8px;padding:0.6rem 1rem;text-align:center;min-width:70px;">
                    <div style="color:#64748b;font-size:0.7rem;">Active</div>
                    <div style="color:#60a5fa;font-size:1.3rem;font-weight:700;">{active}</div>
                </div>
                <div style="background:#1e293b;border-radius:8px;padding:0.6rem 1rem;text-align:center;min-width:70px;">
                    <div style="color:#64748b;font-size:0.7rem;">Total</div>
                    <div style="color:#e2e8f0;font-size:1.3rem;font-weight:700;">{total}</div>
                </div>
                <div style="background:#1e293b;border-radius:8px;padding:0.6rem 1rem;text-align:center;min-width:70px;">
                    <div style="color:#64748b;font-size:0.7rem;">Customers</div>
                    <div style="color:#e2e8f0;font-size:1.3rem;font-weight:700;">{customer_count}</div>
                </div>
            </div>

            <h3 style="color:#94a3b8;font-size:0.9rem;">Last Hour</h3>
            <ul style="color:#cbd5e1;font-size:0.9rem;padding-left:1.25rem;">
                <li>{new_1h} new build(s) started</li>
                <li>{completed_1h} build(s) went live</li>
                <li>{failed_1h} build(s) failed</li>
            </ul>

            {"<p style='color:#f87171;font-weight:600;'>Active builds in progress: " + str(active) + " (" + ", ".join(filter(None, [f"{queued} queued" if queued else "", f"{building} building" if building else "", f"{testing} testing" if testing else "", f"{deploying} deploying" if deploying else ""])) + ")</p>" if active else ""}

            <p style="color:#64748b;font-size:0.8rem;margin-top:1rem;">
                Sent hourly by the CAS ops agent. Set OPS_DIGEST_INTERVAL to change frequency.
            </p>
        </div>
        """
        _send_email(_admin_email(), subject, html)

    except Exception as exc:
        logger.error("Failed to send hourly digest: %s", exc)


def alert_unfixable(
    category: str,
    sample_error: str,
    build_ids: list,
    reason: str = "No playbook available",
) -> None:
    """Notify admin of an error pattern the ops agent cannot auto-fix.

    Applies a 4-hour per-category cooldown to prevent repeated emails for
    persistent patterns that have no playbook.
    """
    if _is_dry_run():
        logger.info("Dry-run: skipping unfixable alert for %s", category)
        return
    now = time.monotonic()
    last = _last_unfixable_alert.get(category)
    # `last is None` matters: time.monotonic() is time since boot, so a default
    # of 0.0 would swallow the first alert of a fresh host for up to 4 hours.
    if last is not None and now - last < _UNFIXABLE_COOLDOWN_SECONDS:
        remaining = int(_UNFIXABLE_COOLDOWN_SECONDS - (now - last)) // 60
        logger.info(
            "alert_unfixable: cooldown active for '%s', skipping (%d min remaining)",
            category, remaining,
        )
        return
    _last_unfixable_alert[category] = now
    subject = f"Ops Agent: unfixable pattern ({category})"
    ids_str = _esc(", ".join(str(b) for b in build_ids[:5]))
    if len(build_ids) > 5:
        ids_str += f" ... and {len(build_ids) - 5} more"
    html = f"""
    <h2>Unfixable Error Pattern Detected</h2>
    <p><strong>Category:</strong> {_esc(category)}</p>
    <p><strong>Reason:</strong> {_esc(reason)}</p>
    <p><strong>Affected builds:</strong> {ids_str}</p>
    <p><strong>Sample error:</strong></p>
    <pre style="background:#1e293b;color:#e2e8f0;padding:1rem;border-radius:8px;
                overflow-x:auto;font-size:0.85rem;">{_esc(sample_error[:500])}</pre>
    <p>This requires manual investigation.</p>
    """
    _send_email(_admin_email(), subject, html)
