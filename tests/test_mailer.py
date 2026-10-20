"""Tests for queue/mailer.py::send_review_pending.

Section 06 of the Senior Engineer Review Gate: the worker's admin notification
that a new build has staged to preview and is awaiting review.

All Resend API calls and DB writes are mocked; no real emails are sent, no
real DB writes happen. The tests lock down five load-bearing invariants:

  1. The email is actually POSTed to Resend with the admin recipient and the
     spec-defined subject.
  2. The HTML body contains a deep link to /admin/reviews/:build_id so Ocean
     can click through from inbox to the review page.
  3. Every attempt (sent, skipped, failed) writes a row to email_log so the
     admin email log view reflects the gate state.
  4. Resend / network failures are fully swallowed; the caller (worker) must
     never see an exception from this fire-and-forget path because the build
     preview transition cannot hinge on email delivery.
  5. The customer unsubscribe table is NOT consulted. This is an internal ops
     email to ADMIN_EMAIL, not a customer email; it is deliberately immune to
     customer-side unsubscribe. (See docs/plans/se-review-gate-spec.md.)
"""

import os
import secrets
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests as requests_module

ROOT = Path(__file__).parent.parent
# queue/mailer.py can't be imported as `queue.mailer` because `queue` collides
# with Python's stdlib module of the same name. The worker loads it by adding
# queue/ to sys.path first and then `from mailer import ...`; we do the same
# here so we hit the exact same module object the worker hits in prod.
sys.path.insert(0, str(ROOT / "queue"))
sys.path.insert(0, str(ROOT))

# Pre-import so patching targets the loaded module
import mailer as _mailer_module


class TestSendReviewPending:
    """send_review_pending is the admin-facing 'review needed' notification."""

    def _kwargs(self, **overrides):
        base = {
            "build_id": "b-1",
            "app_name": "Test App",
            "customer_email": "customer@example.com",
            "tier": "build",
            "preview_url": "https://test-app-preview.up.railway.app",
        }
        base.update(overrides)
        return base

    def test_calls_resend_with_admin_recipient_and_subject(self):
        """Happy path: Resend is POSTed with ADMIN_EMAIL as recipient and the
        'Review needed: <app_name>' subject line from the plan.
        """
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "resend_abc"}

        with patch.dict(
            os.environ,
            {"RESEND_API_KEY": "re_test_key", "ADMIN_EMAIL": "admin@test.example"},
        ):
            with patch.object(_mailer_module, "_log_email"):
                with patch.object(_mailer_module, "provider_post", return_value=mock_response
                ) as mock_post:
                    _mailer_module.send_review_pending(**self._kwargs(app_name="Test App"))

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        payload = call_kwargs[1]["json"]
        assert payload["to"] == ["admin@test.example"]
        assert payload["subject"] == "Review needed: Test App"

    def test_body_includes_deep_link_to_admin_review(self):
        """Body HTML must contain /admin/reviews/:build_id so the admin can
        click straight through from the inbox to the review page.
        """
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "resend_abc"}

        with patch.dict(
            os.environ,
            {"RESEND_API_KEY": "re_test_key", "ADMIN_EMAIL": "admin@test.example"},
        ):
            with patch.object(_mailer_module, "_log_email"):
                with patch.object(_mailer_module, "provider_post", return_value=mock_response
                ) as mock_post:
                    _mailer_module.send_review_pending(**self._kwargs(build_id="b-1"))

        payload = mock_post.call_args[1]["json"]
        assert "/admin/reviews/b-1" in payload["html"]
        # And it should be wired to the canonical production host, matching
        # the existing mailer pattern (send_preview_ready).
        assert "https://craftandship.io/admin/reviews/b-1" in payload["html"]

    def test_logs_to_email_log_on_success(self):
        """Every successful send writes a status='sent' row to email_log with
        template='review_pending'.
        """
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "resend_abc"}

        with patch.dict(
            os.environ,
            {"RESEND_API_KEY": "re_test_key", "ADMIN_EMAIL": "admin@test.example"},
        ):
            with patch.object(_mailer_module, "_log_email") as mock_log:
                with patch.object(_mailer_module, "provider_post", return_value=mock_response
                ):
                    _mailer_module.send_review_pending(**self._kwargs())

        mock_log.assert_called_once()
        log_kwargs = mock_log.call_args[1]
        assert log_kwargs["template"] == "review_pending"
        assert log_kwargs["to_email"] == "admin@test.example"
        assert log_kwargs["status"] == "sent"
        assert log_kwargs["status_code"] == 200
        assert log_kwargs["resend_id"] == "resend_abc"

    def test_suppresses_resend_failure_and_logs_as_failed(self):
        """Resend returning a non-OK response must NOT raise. The function
        swallows the error, logs a status='failed' row to email_log so the
        admin email log view shows the failure, and returns cleanly so the
        worker's preview transition is never blocked.
        """
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch.dict(
            os.environ,
            {"RESEND_API_KEY": "re_test_key", "ADMIN_EMAIL": "admin@test.example"},
        ):
            with patch.object(_mailer_module, "_log_email") as mock_log:
                with patch.object(_mailer_module, "provider_post", return_value=mock_response
                ):
                    # Must not raise
                    _mailer_module.send_review_pending(**self._kwargs())

        mock_log.assert_called_once()
        log_kwargs = mock_log.call_args[1]
        assert log_kwargs["template"] == "review_pending"
        assert log_kwargs["status"] == "failed"
        assert log_kwargs["status_code"] == 500
        assert "Internal Server Error" in log_kwargs["error_message"]

    def test_suppresses_network_error_and_logs_as_failed(self):
        """A raised ConnectionError from requests.post must also be swallowed
        and recorded as a failed send.
        """
        with patch.dict(
            os.environ,
            {"RESEND_API_KEY": "re_test_key", "ADMIN_EMAIL": "admin@test.example"},
        ):
            with patch.object(_mailer_module, "_log_email") as mock_log:
                with patch.object(_mailer_module, "provider_post",
                    side_effect=requests_module.exceptions.ConnectionError("refused"),
                ):
                    # Must not raise
                    _mailer_module.send_review_pending(**self._kwargs())

        mock_log.assert_called_once()
        log_kwargs = mock_log.call_args[1]
        assert log_kwargs["status"] == "failed"
        assert "refused" in log_kwargs["error_message"]

    def test_skips_send_when_api_key_missing(self):
        """In dev / missing RESEND_API_KEY, the function must not call Resend
        at all, but it must still log a 'skipped' row so the email log view
        reflects the intended send.
        """
        env = os.environ.copy()
        env.pop("RESEND_API_KEY", None)
        env["ADMIN_EMAIL"] = "admin@test.example"

        with patch.dict(os.environ, env, clear=True):
            with patch.object(_mailer_module, "_log_email") as mock_log:
                with patch.object(_mailer_module, "provider_post") as mock_post:
                    _mailer_module.send_review_pending(**self._kwargs())

        mock_post.assert_not_called()
        mock_log.assert_called_once()
        assert mock_log.call_args[1]["status"] == "skipped"
        assert mock_log.call_args[1]["template"] == "review_pending"

    def test_not_subject_to_customer_unsubscribe(self):
        """The admin notification must NOT consult the customer unsubscribe
        table. This is an internal ops email to ADMIN_EMAIL, and customers
        should never be able to suppress senior-engineer review notifications
        by unsubscribing themselves.
        """
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "resend_abc"}

        with patch.dict(
            os.environ,
            {"RESEND_API_KEY": "re_test_key", "ADMIN_EMAIL": "admin@test.example"},
        ):
            with patch.object(_mailer_module, "_log_email"):
                with patch.object(
                    _mailer_module, "_is_unsubscribed"
                ) as mock_unsub:
                    with patch.object(_mailer_module, "provider_post", return_value=mock_response
                    ) as mock_post:
                        _mailer_module.send_review_pending(**self._kwargs())

        # _is_unsubscribed must not be called anywhere in this code path
        mock_unsub.assert_not_called()
        # And the email must have been sent regardless
        mock_post.assert_called_once()

    def test_html_escapes_app_name_in_body(self):
        """XSS canary: app_name flows into HTML, so it must be escaped. The
        admin email is internal but we still don't want a malicious spec to
        inject markup into ADMIN_EMAIL's inbox.
        """
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "resend_abc"}

        with patch.dict(
            os.environ,
            {"RESEND_API_KEY": "re_test_key", "ADMIN_EMAIL": "admin@test.example"},
        ):
            with patch.object(_mailer_module, "_log_email"):
                with patch.object(_mailer_module, "provider_post", return_value=mock_response
                ) as mock_post:
                    _mailer_module.send_review_pending(
                        **self._kwargs(app_name="<script>alert(1)</script>")
                    )

        payload = mock_post.call_args[1]["json"]
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in payload["html"]
        assert "<script>alert(1)</script>" not in payload["html"]


# ---------------------------------------------------------------------------
# Review round R1-P2: failure isolation, escaping, configuration, recipients
# ---------------------------------------------------------------------------

_SEND_FUNCTIONS = {
    "send_preview_ready": lambda m, **kw: m.send_preview_ready(None, None, None, None, None, None, None),
    "send_build_progress": lambda m, **kw: m.send_build_progress(None, None, None, "building"),
    "send_build_failed": lambda m, **kw: m.send_build_failed(None, None, None, None),
    "send_review_pending": lambda m, **kw: m.send_review_pending(None, None, None, None, None),
}


class TestEmailNeverRaises:
    """Contract: an email problem must never change the outcome of a build."""

    @pytest.mark.parametrize("name", sorted(_SEND_FUNCTIONS))
    def test_none_arguments_do_not_raise(self, name, monkeypatch):
        monkeypatch.setenv("SEND_BUILD_PROGRESS_EMAILS", "true")
        monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
        with patch.object(_mailer_module, "_send"):
            _SEND_FUNCTIONS[name](_mailer_module)  # customers.name etc. can be NULL

    @pytest.mark.parametrize("name", sorted(_SEND_FUNCTIONS))
    def test_internal_failures_are_swallowed(self, name, monkeypatch):
        monkeypatch.setenv("SEND_BUILD_PROGRESS_EMAILS", "true")
        monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
        with patch.object(_mailer_module, "_send", side_effect=RuntimeError("boom")):
            _SEND_FUNCTIONS[name](_mailer_module)

    def test_public_functions_are_wrapped(self):
        for name in _SEND_FUNCTIONS:
            assert hasattr(getattr(_mailer_module, name), "__wrapped__"), name


class TestCustomerEmailContent:
    def _capture(self, func, *args, **kwargs):
        with patch.object(_mailer_module, "_send") as send:
            func(*args, **kwargs)
        send.assert_called_once()
        return send.call_args.kwargs

    def test_login_section_names_the_customer_account_not_the_platform_admin(self, monkeypatch):
        """The magic link signs in as the seeded admin user, whose address is
        the customer's own. The platform admin address must not be shown as
        'Admin account'."""
        monkeypatch.setenv("ADMIN_EMAIL", "ops@example.test")
        sent = self._capture(
            _mailer_module.send_preview_ready,
            "customer@example.test", "Pat", "demo", "https://demo.example.test", "b-1",
            "ops@example.test", "build", magic_link_url="https://demo.example.test/magic?token=t",
        )
        assert "Admin account: customer@example.test" in sent["html"]
        assert "Admin account: ops@example.test" not in sent["html"]
        # ...while the support contact line still uses the platform admin.
        assert 'mailto:ops@example.test' in sent["html"]

    def test_every_interpolated_value_is_escaped(self, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAIL", "ops@example.test")
        evil = '<img src=x onerror=alert(1)>"\''
        sent = self._capture(
            _mailer_module.send_preview_ready,
            "c@example.test", evil, evil, 'https://x.example.test/"><script>1</script>',
            'b"><script>2</script>', evil, evil, magic_link_url='https://x.example.test/m?t="><b>',
        )
        html = sent["html"]
        assert "<script>" not in html and "<img" not in html and "<b>" not in html
        assert "onerror=alert(1)>" not in html

    def test_build_failed_escapes_and_handles_missing_names(self, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAIL", "ops@example.test")
        sent = self._capture(_mailer_module.send_build_failed, "c@example.test", "<b>N</b>", "", "ops@example.test")
        assert "<b>N</b>" not in sent["html"] and "&lt;b&gt;N&lt;/b&gt;" in sent["html"]
        assert sent["subject"] == "Build update for your website"
        assert "<strong>your website</strong>" in sent["html"]

    def test_subject_cannot_carry_a_header_line(self):
        # _send is where subjects are normalized, so exercise it directly.
        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch.object(_mailer_module, "_log_email"), patch.object(
                _mailer_module, "_is_unsubscribed", return_value=False
            ), patch.object(_mailer_module, "provider_post") as post:
                post.return_value = MagicMock(ok=True, status_code=200, json=MagicMock(return_value={"id": "1"}))
                _mailer_module._send(
                    to="c@example.test", subject="Hello\r\nBcc: evil@example.test", html="<p>x</p>", template="t",
                )
        subject = post.call_args[1]["json"]["subject"]
        assert "\r" not in subject and "\n" not in subject

    def test_links_use_base_url_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("BASE_URL", "https://cas.example.test/")
        monkeypatch.setenv("ADMIN_EMAIL", "ops@example.test")
        sent = self._capture(
            _mailer_module.send_preview_ready,
            "c@example.test", "N", "demo", "https://demo.example.test", "b-1", "ops@example.test", "build",
        )
        assert 'href="https://cas.example.test/builds/b-1"' in sent["html"]
        review = self._capture(
            _mailer_module.send_review_pending, "b-2", "demo", "c@example.test", "build", "https://demo.example.test",
        )
        assert "https://cas.example.test/admin/reviews/b-2" in review["html"]

    def test_no_hard_coded_admin_fallback(self, monkeypatch):
        """Without ADMIN_EMAIL there is no BCC and no admin mail: customer
        emails (which carry login links) must never be copied to a default
        address baked into the source."""
        monkeypatch.delenv("ADMIN_EMAIL", raising=False)
        sent = self._capture(
            _mailer_module.send_preview_ready,
            "c@example.test", "N", "demo", "https://demo.example.test", "b-1", "", "build",
            magic_link_url="https://demo.example.test/magic?token=t",
        )
        assert not sent.get("bcc")
        assert "mailto:" not in sent["html"]
        with patch.object(_mailer_module, "_send") as send:
            _mailer_module.send_review_pending("b-1", "demo", "c@example.test", "build", "https://demo.example.test")
        send.assert_not_called()
        assert "craftandship.io" not in _mailer_module._get_config()[2]

    def test_customer_mail_is_bcc_to_the_configured_admin(self, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAIL", "ops@example.test")
        sent = self._capture(
            _mailer_module.send_build_failed, "c@example.test", "N", "demo", "ops@example.test", "b-1",
        )
        assert sent["bcc"] == "ops@example.test"


class TestSendBehaviour:
    def test_without_api_key_nothing_is_posted_and_the_attempt_is_logged(self, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        with patch.object(_mailer_module, "_is_unsubscribed", return_value=False), \
             patch.object(_mailer_module, "_log_email") as log, \
             patch.object(_mailer_module, "provider_post") as post:
            _mailer_module._send(to="c@example.test", subject="s", html="<p>x</p>", template="t")
        post.assert_not_called()
        assert log.call_args.kwargs["status"] == "skipped"

    def test_missing_recipient_is_skipped_without_raising(self):
        with patch.object(_mailer_module, "provider_post") as post:
            _mailer_module._send(to="", subject="s", html="<p>x</p>", template="t")
        post.assert_not_called()

    def test_unsubscribed_customers_are_skipped_but_logged(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
        with patch.object(_mailer_module, "_is_unsubscribed", return_value=True), \
             patch.object(_mailer_module, "_log_email") as log, \
             patch.object(_mailer_module, "provider_post") as post:
            _mailer_module._send(to="c@example.test", subject="s", html="<p>x</p>", template="t")
        post.assert_not_called()
        assert log.call_args.kwargs["status"] == "skipped"

    def test_database_connections_have_a_connect_timeout(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "post" + "gresql://localhost/none")
        with patch("psycopg2.connect") as connect:
            _mailer_module._get_db_conn()
        assert connect.call_args.kwargs.get("connect_timeout")


class TestLoginFallbackIsTruthful:
    def test_no_link_means_no_claim_that_instructions_were_sent_elsewhere(self, monkeypatch):
        monkeypatch.setenv("ADMIN_EMAIL", "ops@example.test")
        with patch.object(_mailer_module, "_send") as send:
            _mailer_module.send_preview_ready(
                "c@example.test", "N", "demo", "https://demo.example.test", "b-1", "ops@example.test", "build",
            )
        html = send.call_args.kwargs["html"]
        assert "sent separately" not in html
        assert "Reply here" in html


class TestStoredBodyKeepsNoLoginToken:
    """email_log.html_body is shown on /admin/emails. The login link in the
    "preview ready" email signs in as the site's admin, so the stored copy
    has its token redacted the way the web app's sendEmail redacts it; the
    email itself carries the working link."""

    TOKEN = secrets.token_hex(32)

    class _Connection:
        def __init__(self, rows):
            self.rows = rows

        def cursor(self):
            connection = self

            class _Cursor:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def execute(self, sql, params):
                    connection.rows.append(params)

            return _Cursor()

        def commit(self):
            pass

        def close(self):
            pass

    def _send_preview_ready(self, monkeypatch, api_key):
        if api_key:
            monkeypatch.setenv("RESEND_API_KEY", api_key)
        else:
            monkeypatch.delenv("RESEND_API_KEY", raising=False)
        stored, posted = [], []
        answer = MagicMock(ok=True, status_code=200)
        answer.json.return_value = {"id": "re-1"}

        def post(url, **kwargs):
            posted.append(kwargs["json"])
            return answer

        with patch.object(_mailer_module, "_get_db_conn", return_value=self._Connection(stored)), \
             patch.object(_mailer_module, "_is_unsubscribed", return_value=False), \
             patch.object(_mailer_module, "provider_post", side_effect=post):
            _mailer_module.send_preview_ready(
                "c@example.test", "N", "demo", "https://demo.example.test", "b-1", "", "build",
                magic_link_url="https://demo.example.test/magic?token=" + self.TOKEN,
            )
        assert len(stored) == 1
        return stored[0], posted

    @pytest.mark.parametrize("api_key", ["test-mail-key-value", None])
    def test_the_stored_body_has_the_link_without_its_token(self, monkeypatch, api_key):
        row, posted = self._send_preview_ready(monkeypatch, api_key)
        status, html_body = row[3], row[8]
        assert status == ("sent" if api_key else "skipped")
        assert self.TOKEN not in html_body
        assert "https://demo.example.test/magic?token=[redacted]" in html_body
        if api_key:
            assert self.TOKEN in posted[0]["html"], "the email itself keeps the working link"


# The cases the web app's own tests give redactSecretParams
# (web/tests/unit/utils.redaction.test.ts), and a few more.
_SECRET = "s3cr3t" + "Value" + "9"
_REDACTION_CORPUS = [
    f"/reset-password?token={_SECRET}",
    f"/unsubscribe?email=a%40example.com&token={_SECRET}",
    f"/x?token={_SECRET}&keep=1",
    f'<a href="https://example.com/r?a=1&amp;token={_SECRET}">x</a>',
    f'<a href="https://example.com/r?a=1&#38;token={_SECRET}">x</a>',
    f'<a href="https://example.com/r?a=1&#x26;token={_SECRET}">x</a>',
    f"https://example.com/go?next=%2Freset-password%3Ftoken%3D{_SECRET}%26verified%3D1",
    f"https://example.com/go?next=%2Fa%3fx%3d1%26token%3d{_SECRET}",
    f"https://example.com/go?u=%252Freset-password%253Ftoken%253D{_SECRET}",
    f"/x?TOKEN={_SECRET} and /y?Reset_Token={_SECRET}#frag",
    "/gallery?page=2&sort=new", "/try?utm_source=mail&utm_campaign=spring", "/x?monkey=1&zipcode=78701",
    f"<p>/magic?token={_SECRET};rest</p> '/a?code={_SECRET}' <b>/b?sig={_SECRET}</b>",
] + [f"/x?{name}={_SECRET}" for name in (
    "reset_token", "access_token", "api_key", "api-key", "apikey", "key", "secret",
    "signature", "sig", "code", "password", "session_id")]

_WEB_REDACTION_DRIVER = r"""
const fs = require('fs');
const path = require('path');
const webDir = process.argv[1];
const ts = require(path.join(webDir, 'node_modules', 'typescript'));
const source = fs.readFileSync(path.join(webDir, 'src', 'utils', 'recent-errors.ts'), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const mod = { exports: {} };
new Function('exports', 'require', 'module', compiled)(mod.exports, require, mod);
const corpus = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(corpus.map((text) => mod.exports.redactSecretParams(text))));
"""


def test_the_redaction_is_the_web_apps():
    """The same corpus through the web app's redactSecretParams (compiled
    from its TypeScript source) and through the mailer's port of it."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    web = ROOT / "web"
    if node is None or not (web / "node_modules" / "typescript").exists():
        pytest.skip("needs node and web/node_modules/typescript (run `cd web && npm ci`)")
    proc = subprocess.run([node, "-e", _WEB_REDACTION_DRIVER, str(web)], input=json.dumps(_REDACTION_CORPUS),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-800:]
    web_results = json.loads(proc.stdout)
    assert [_mailer_module._redact_secret_params(text) for text in _REDACTION_CORPUS] == web_results
    assert sum(_SECRET in text for text in web_results) == 0
