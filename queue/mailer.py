# queue/mailer.py
# Sends transactional email via Resend REST API.
# Falls back to a log line (and an email_log row with status 'skipped') if
# RESEND_API_KEY is not set. Checks the customer unsubscribe preference and
# logs every attempt to the email_log table.
#
# Contract for callers (the build worker): every public send_* function is
# failure-isolated. It never raises, whatever the arguments or the network do,
# because an email problem must not change the outcome of a build.
#
# Configuration (environment):
#   RESEND_API_KEY      Resend key. Unset: nothing is sent, attempts are logged.
#   RESEND_FROM_EMAIL   From address (default below; Resend rejects it unless
#                       the domain is verified on your account).
#   ADMIN_EMAIL         Operator mailbox: receives the "review needed" email and
#                       a BCC of customer lifecycle emails. Unset: no admin
#                       email and no BCC (there is no hard-coded fallback).
#   BASE_URL            Public site URL used in links (default below).

import functools
import html as html_module
import logging
import os
import re
import sys
import time
from pathlib import Path


# This file is imported as the top-level module "mailer" (queue/ is put on
# sys.path, see queue/worker_pg.py), so the repository root may not be on the
# path yet when it is imported on its own.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from coordinator.envflags import arming_switch
from monitor.bounded_fetch import provider_post

logger = logging.getLogger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"
# One send is one bounded exchange with the mail provider: at most this many
# seconds for the whole request and at most this much of its answer (an id or
# an error message). Past either, the send is logged as failed.
_RESEND_DEADLINE_S = 10
_RESEND_MAX_ANSWER_BYTES = 64 * 1024
_DEFAULT_FROM = "Craft & Ship <noreply@craftandship.io>"
_DEFAULT_BASE_URL = "https://craftandship.io"


def _get_config():
    api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("RESEND_FROM_EMAIL", _DEFAULT_FROM)
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
    return api_key, from_email, admin_email


def _base_url() -> str:
    return (os.environ.get("BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _esc(value) -> str:
    """HTML-escape any value for text or attribute context; None becomes ''."""
    return html_module.escape("" if value is None else str(value))


def _header_safe(value) -> str:
    """Single-line text for subjects: drops CR/LF so a value can never add a header line."""
    return " ".join(("" if value is None else str(value)).splitlines()).strip()


def _never_raises(func):
    """Decorator: log and swallow any exception from a public send_* function."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # contract: email never breaks a build
            logger.error("[email] %s failed unexpectedly: %s", func.__name__, exc)
            return None

    return wrapper


def _contact_line(admin_email: str) -> str:
    """Closing 'questions?' sentence; the mailto link only exists when an admin email is configured."""
    if not admin_email:
        return "<p>If you have any questions, just reply to this email.</p>"
    safe_admin = _esc(admin_email)
    return (
        "<p>If you have any questions, reply here or contact your account manager at\n"
        f'<a href="mailto:{safe_admin}">{safe_admin}</a>.</p>'
    )


def _get_db_conn():
    """Get a psycopg2 connection for unsubscribe checks and email logging."""
    try:
        import psycopg2
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            return None
        return psycopg2.connect(db_url, connect_timeout=5)
    except Exception:
        return None


def _is_unsubscribed(to_email: str) -> bool:
    """Check if the customer has unsubscribed from emails."""
    conn = _get_db_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT email_unsubscribed FROM customers WHERE email = %s",
                (to_email,),
            )
            row = cur.fetchone()
            return bool(row and row[0])
    except Exception as exc:
        logger.warning("[email] Unsubscribe check failed for %s: %s", to_email, exc)
        return False
    finally:
        try:
            conn.close()
        except Exception as _guard_exc:
            logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)


# The web app's redactSecretParams (web/src/utils/recent-errors.ts), in
# Python: the value of every secret-bearing URL parameter (the token= of a
# login link, among others) is replaced by "[redacted]" and the rest of the
# text, the links included, is kept. A name is matched after "?" or "&" in
# the forms a URL takes inside HTML or inside another URL (plain,
# entity-encoded, percent-encoded once or twice), and a value runs to the
# next separator, quote, angle bracket or blank. One linear pass.
_SECRET_PARAM = r"(?:[a-z0-9_.-]*token|api[_-]?key|key|secret|signature|sig|code|password|session_id)"
_PARAM_SEPARATOR = r"(?:[?&]|&amp;|&#0*38;|&#x0*26;|%(?:25)?3f|%(?:25)?26)"
_PARAM_EQUALS = r"(?:=|%(?:25)?3d)"
_SECRET_PARAM_VALUE_RE = re.compile(
    rf"({_PARAM_SEPARATOR}{_SECRET_PARAM}{_PARAM_EQUALS})(?:(?!%(?:25)?(?:26|3f))[^\s\"'<>&#;])+",
    re.IGNORECASE,
)


def _redact_secret_params(text: str) -> str:
    """`text` with the value of every secret-bearing URL parameter replaced by "[redacted]"."""
    return _SECRET_PARAM_VALUE_RE.sub(r"\1[redacted]", text or "")


def _log_email(*, template: str, to_email: str, subject: str,
               status: str, status_code: int = 0, error_message: str = "",
               resend_id: str = "", duration_ms: int = 0, html_body: str = "") -> None:
    """Log email send attempt to the email_log table (matches web pattern).

    The stored HTML has its secret URL parameters redacted
    (_redact_secret_params), as the web app's sendEmail does, so the table and
    /admin/emails cannot be used to replay a login link. Only the stored copy
    is changed; the email itself carries the working link.
    """
    conn = _get_db_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO email_log
                       (template, to_email, subject, status, status_code,
                        error_message, resend_id, duration_ms, html_body)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (template, to_email, subject, status, status_code,
                 error_message, resend_id, duration_ms, _redact_secret_params(html_body)),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("[email] Failed to log email to DB: %s", exc)
        try:
            conn.rollback()
        except Exception as _guard_exc:
            logger.debug("rollback failed, the connection was already unusable: %s: %s", type(_guard_exc).__name__, _guard_exc)
    finally:
        try:
            conn.close()
        except Exception as _guard_exc:
            logger.debug("close failed, the connection was already gone: %s: %s", type(_guard_exc).__name__, _guard_exc)


def _send(*, to: str, subject: str, html: str, bcc: str | None = None,
          template: str = "unknown", check_unsubscribe: bool = True) -> None:
    """Send an email via Resend REST API. Never raises; logs errors instead.

    Checks unsubscribe preference before sending (skipped for internal ops mail
    to the admin, which a customer must not be able to suppress) and logs all
    attempts to the email_log table (matching the web service pattern).
    """
    api_key, from_email, _ = _get_config()
    subject = _header_safe(subject)

    if not to:
        logger.warning("[email] No recipient for '%s' (%s); skipping", subject, template)
        return

    # Check unsubscribe preference
    if check_unsubscribe and _is_unsubscribed(to):
        logger.info("[email] Customer %s is unsubscribed; skipping '%s'", to, subject)
        _log_email(template=template, to_email=to, subject=subject,
                   status="skipped", html_body=html)
        return

    if not api_key:
        logger.info(
            "[email] RESEND_API_KEY not set; skipping send to %s, subject: %s",
            to,
            subject,
        )
        _log_email(template=template, to_email=to, subject=subject,
                   status="skipped", html_body=html)
        return

    payload = {
        "from": from_email,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if bcc:
        payload["bcc"] = [bcc]

    start_ms = int(time.time() * 1000)
    try:
        response = provider_post(
            _RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_RESEND_DEADLINE_S,
            max_body_bytes=_RESEND_MAX_ANSWER_BYTES,
        )
        duration_ms = int(time.time() * 1000) - start_ms
        resend_id = ""
        if response.ok:
            logger.info("[email] Sent '%s' to %s", subject, to)
            try:
                resend_id = response.json().get("id", "")
            except (ValueError, AttributeError):
                # The mail was accepted; an answer that is not a JSON object
                # only means there is no provider id to store.
                logger.info("[email] Resend accepted '%s' without a readable id", subject)
            _log_email(template=template, to_email=to, subject=subject,
                       status="sent", status_code=response.status_code,
                       resend_id=resend_id, duration_ms=duration_ms, html_body=html)
        else:
            logger.error(
                "[email] Resend API error %d: %s", response.status_code, response.text
            )
            _log_email(template=template, to_email=to, subject=subject,
                       status="failed", status_code=response.status_code,
                       error_message=response.text[:500], duration_ms=duration_ms,
                       html_body=html)
    except Exception as exc:
        duration_ms = int(time.time() * 1000) - start_ms
        logger.error("[email] Failed to send '%s' to %s: %s", subject, to, exc)
        _log_email(template=template, to_email=to, subject=subject,
                   status="failed", error_message=str(exc)[:500],
                   duration_ms=duration_ms, html_body=html)


def _login_section(magic_link_url: str, login_email: str) -> str:
    """HTML block with the magic-link button, or a fallback note without one.

    `login_email` is the account the link signs in as: the customer's own
    address, which the worker seeds as the generated site's admin user.
    """
    safe_login_email = _esc(login_email)
    if magic_link_url:
        safe_magic_link = _esc(magic_link_url)
        return f"""
<h3>Log in to your website</h3>
<p><a href="{safe_magic_link}" style="display:inline-block;background:#6366f1;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">Log In to Your Website</a></p>
<p style="font-size:0.85rem;color:#71717a;">This link expires in 24 hours. After logging in, you will set your own password.</p>
<p>Admin account: {safe_login_email}</p>
"""
    return f"""
<h3>Login</h3>
<p>A login link is not attached to this email. Reply here and we will send you a fresh one. Admin account: {safe_login_email}</p>
"""


@_never_raises
def send_preview_ready(
    to_email: str,
    name: str,
    app_name: str,
    preview_url: str,
    build_id: str,
    admin_email: str,
    tier: str,
    magic_link_url: str = "",
) -> None:
    """Send a notification email when a build is ready for customer preview."""
    _, _, bcc_email = _get_config()
    safe_name = _esc(name)
    safe_app_name = _esc(app_name)
    safe_preview_url = _esc(preview_url)
    safe_build_id = _esc(build_id)
    short_id = (build_id or "")[:8]
    subject = f"Your website '{app_name}' is ready for review! [{short_id}]" if short_id else f"Your website '{app_name}' is ready for review!"
    login_section = _login_section(magic_link_url, to_email)
    status_page_url = _esc(f"{_base_url()}/builds/{build_id}")
    html = f"""
<p>Hi {safe_name},</p>

<p>Your website <strong>{safe_app_name}</strong> has been built, tested, and deployed
to a preview URL. Please take a look and make sure everything is how you want it:</p>

<p><a href="{safe_preview_url}">{safe_preview_url}</a></p>

{login_section}

<h3>What to check</h3>
<ul>
  <li>Does the website match what you described?</li>
  <li>Do the pages and features look right?</li>
  <li>Is the design and layout what you expected?</li>
</ul>

<p>Your website needs a recorded review approval before it can go live. When the review is done
you will get another email, and your
<a href="{status_page_url}">build status page</a>
will then offer <strong>Approve and Go Live</strong>.</p>

<p>If anything needs changing, you can request modifications from the same page.</p>

{_contact_line(admin_email)}

<p>The Craft & Ship Team</p>
"""
    _send(to=to_email, subject=subject, html=html, bcc=bcc_email, template="preview_ready")


@_never_raises
def send_build_progress(
    to_email: str,
    name: str,
    app_name: str,
    stage: str,
    build_id: str = "",
) -> None:
    """Send a progress notification email during the build pipeline.

    Disabled by default: the status page shows real-time build logs, making
    progress emails redundant. Set SEND_BUILD_PROGRESS_EMAILS=true to enable.
    This saves ~3 emails per build (~30/day at 10 builds).
    """
    if not arming_switch("SEND_BUILD_PROGRESS_EMAILS"):
        logger.info("[email] Build progress emails disabled; skipping %s for %s", stage, app_name)
        return

    safe_name = _esc(name)
    short_id = (build_id or "")[:8]
    id_tag = f" [{short_id}]" if short_id else ""

    stage_messages = {
        "building": {
            "subject": f"Building '{app_name}': code generation started{id_tag}",
            "body": "We have started generating the code for your website. This typically takes 5 to 10 minutes.",
        },
        "testing": {
            "subject": f"Testing '{app_name}': running quality checks{id_tag}",
            "body": "Your website has been built and we are now running automated tests to make sure everything works correctly.",
        },
        "deploying": {
            "subject": f"Deploying '{app_name}': going live soon{id_tag}",
            "body": "All tests passed! We are now deploying your website to production. You will receive a final email with your live URL shortly.",
        },
    }

    info = stage_messages.get(stage)
    if not info:
        logger.warning("[email] Unknown build stage: %s", stage)
        return

    html = f"""
<p>Hi {safe_name},</p>

<p>{info['body']}</p>

<p>You can track progress on your dashboard at any time.</p>

<p>The Craft & Ship Team</p>
"""
    _send(to=to_email, subject=info["subject"], html=html, template="build_progress")


@_never_raises
def send_build_failed(
    to_email: str,
    name: str,
    app_name: str,
    admin_email: str,
    build_id: str = "",
) -> None:
    """Send a notification email when a build fails."""
    _, _, bcc_email = _get_config()
    safe_name = _esc(name)
    safe_app_name = _esc(app_name) or "your website"
    short_id = (build_id or "")[:8]
    base_subject = f"Build update for '{app_name}'" if app_name else "Build update for your website"
    subject = f"{base_subject} [{short_id}]" if short_id else base_subject
    if admin_email:
        safe_admin = _esc(admin_email)
        reach_out = (
            "<p>In the meantime, feel free to reach out directly at\n"
            f'<a href="mailto:{safe_admin}">{safe_admin}</a>.</p>'
        )
    else:
        reach_out = "<p>In the meantime, feel free to reply to this email.</p>"
    html = f"""
<p>Hi {safe_name},</p>

<p>We're sorry, but the build for your website <strong>{safe_app_name}</strong> did not
complete successfully. This is not the experience we want to deliver, and we
sincerely apologise for the inconvenience.</p>

<p>A member of our team will personally follow up with you within <strong>24
hours</strong> to diagnose the issue and get things back on track. If we are
unable to resolve it to your satisfaction, you will receive a <strong>full
refund</strong>, no questions asked.</p>

{reach_out}

<p>The Craft & Ship Team</p>
"""
    _send(to=to_email, subject=subject, html=html, bcc=bcc_email, template="build_failed")


@_never_raises
def send_review_pending(
    build_id: str,
    app_name: str,
    customer_email: str,
    tier: str,
    preview_url: str,
) -> None:
    """Admin-facing 'review needed' email, sent when a build arrives at preview.

    Notifies ADMIN_EMAIL that a build has staged to preview and is awaiting
    the senior engineer review gate. The email contains the app name, customer,
    tier, preview URL, and a deep link to /admin/reviews/:build_id.

    This is a transactional ops email: it deliberately does NOT check the
    customer unsubscribe table (the recipient is the internal admin, not the
    customer) and always writes a row to email_log (status 'sent' on success,
    'failed' on Resend errors or network failures, 'skipped' without an API
    key). With no ADMIN_EMAIL configured there is nobody to notify, so it logs
    that and returns.

    Called by queue/worker_pg.py::_process_job after _set_preview_status moved
    the build to preview, so it is sent once per arrival at preview: once for a
    first build and again after every rebuild, never for a duplicate call that
    matched no row. Not called when CAS_HARBOR_AUTO_REVIEW=true. Fire-and-forget:
    it never raises.
    """
    _, _, admin_email = _get_config()
    if not admin_email:
        logger.warning(
            "[email] ADMIN_EMAIL not set; no review_pending notification for build=%s", build_id
        )
        return

    subject = _header_safe(f"Review needed: {app_name}")
    safe_app_name = _esc(app_name)
    safe_customer_email = _esc(customer_email)
    safe_tier = _esc(tier)
    safe_preview_url = _esc(preview_url)
    safe_build_id = _esc(build_id)
    review_deep_link = _esc(f"{_base_url()}/admin/reviews/{build_id}")

    html = f"""
<p>A new build is waiting for senior engineer review.</p>

<p>
  <strong>App:</strong> {safe_app_name}<br/>
  <strong>Customer:</strong> {safe_customer_email}<br/>
  <strong>Tier:</strong> {safe_tier}<br/>
  <strong>Build ID:</strong> {safe_build_id}
</p>

<p><strong>Preview URL:</strong> <a href="{safe_preview_url}">{safe_preview_url}</a></p>

<p>
  <a href="{review_deep_link}"
     style="display:inline-block;background:#22c55e;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">
    Open Review
  </a>
</p>

<p style="font-size:0.85rem;color:#71717a;">
  This build will stay in preview until you approve or request changes from the
  admin reviews page. The customer cannot promote it to live until the gate is
  cleared.
</p>
"""

    # Internal ops notification to ADMIN_EMAIL, not customer mail: no
    # unsubscribe check. Everything else (skip without a key, email_log row,
    # error capture) is shared with the customer sends.
    _send(to=admin_email, subject=subject, html=html, template="review_pending",
          check_unsubscribe=False)
