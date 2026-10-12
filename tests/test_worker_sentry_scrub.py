"""What the build worker is allowed to send to Sentry.

A failed deploy raises through frames that hold the Railway project token, the
customer app's generated DATABASE_URL and SESSION_SECRET, magic-link tokens and
the job itself. The SDK default (include_local_variables=True) ships the local
variables of every frame, so the worker turns that off and scrubs the text that
does go out.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
for _entry in (str(ROOT), str(ROOT / "cli"), str(ROOT / "queue")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

# Imported the same way as tests/test_worker_pg.py, so the module (and the
# cli.cancellation registry it loads) exists once per test run.
import worker_pg as worker

# Assembled at runtime so nothing here is shaped like a real credential.
_PG_PASSWORD = "pg-" + "w" * 24
_DB_URL = "postgresql://railway:" + _PG_PASSWORD + "@postgres.railway.internal:5432/railway"
_LOGIN_TOKEN = "a1" * 32
_CUSTOMER = "keeper" + "@" + "customer-site.example"


class TestSentryInitOptions:
    def test_stack_frame_locals_are_not_sent(self):
        options = worker._sentry_init_options()
        assert options["include_local_variables"] is False
        assert options["send_default_pii"] is False
        assert options["before_send"] is worker._sentry_scrub_event

    def test_options_are_accepted_by_the_installed_sdk(self):
        """Guards against a typo in an option name: the SDK rejects unknown options."""
        import sentry_sdk
        options = dict(worker._sentry_init_options())
        options["dsn"] = None  # no DSN: the client is built but sends nothing
        client = sentry_sdk.Client(**options)
        assert client.options["include_local_variables"] is False
        client.close()

    def test_sdk_default_would_have_sent_locals(self):
        """Documents why the option is set explicitly."""
        import sentry_sdk
        client = sentry_sdk.Client(dsn=None)
        assert client.options["include_local_variables"] is True
        client.close()


class TestSentryScrubEvent:
    def _event(self):
        return {
            "exception": {"values": [{
                "type": "RuntimeError",
                "value": f"GraphQL error: invalid value DATABASE_URL={_DB_URL} for {_CUSTOMER}",
            }]},
            "message": f"deploy failed for {_CUSTOMER}",
            "logentry": {
                "message": "Build %s failed: %s",
                "formatted": f"Build b1 failed: could not connect to {_DB_URL}",
                "params": ["b1", f"could not connect to {_DB_URL}"],
            },
            "breadcrumbs": {"values": [
                {"type": "http", "message": None,
                 "data": {"url": "https://app.up.railway.app/magic", "http.query": "token=" + _LOGIN_TOKEN,
                          "status_code": 200}},
                {"type": "log", "message": f"Processing build b1 (customer={_CUSTOMER})", "data": {}},
            ]},
        }

    def test_secrets_and_emails_are_removed_everywhere(self):
        scrubbed = worker._sentry_scrub_event(self._event())
        flat = repr(scrubbed)
        assert _PG_PASSWORD not in flat
        assert _LOGIN_TOKEN not in flat
        assert _CUSTOMER not in flat
        assert "<email>" in flat and "***" in flat

    def test_structure_and_non_text_values_survive(self):
        scrubbed = worker._sentry_scrub_event(self._event())
        assert scrubbed["exception"]["values"][0]["type"] == "RuntimeError"
        crumb = scrubbed["breadcrumbs"]["values"][0]
        assert crumb["data"]["status_code"] == 200
        assert crumb["data"]["url"] == "https://app.up.railway.app/magic"
        assert crumb["message"] is None

    def test_breadcrumbs_as_a_plain_list_are_handled(self):
        event = {"breadcrumbs": [{"message": f"mail to {_CUSTOMER}"}]}
        assert _CUSTOMER not in repr(worker._sentry_scrub_event(event))

    def test_minimal_event_passes_through(self):
        assert worker._sentry_scrub_event({"level": "error"}) == {"level": "error"}

    def test_unreadable_event_is_dropped_not_sent_raw(self):
        assert worker._sentry_scrub_event({"exception": "not a dict"}) is None

    def test_scrub_runs_through_the_sdk_before_send_hook(self):
        """End to end with the real SDK and an in-memory transport: an exception
        raised next to secret locals reaches the transport with neither the
        locals nor the secret in its message."""
        import sentry_sdk
        from sentry_sdk.transport import Transport

        sent = []

        class _Capture(Transport):
            def capture_envelope(self, envelope):
                for item in envelope.items:
                    payload = item.payload.json if item.payload is not None else None
                    if payload:
                        sent.append(payload)

        options = dict(worker._sentry_init_options())
        options["dsn"] = "https://public@sentry.invalid/1"
        options["transport"] = _Capture
        options["traces_sample_rate"] = 0.0
        options["default_integrations"] = False
        client = sentry_sdk.Client(**options)

        def _fail():
            project_token = _LOGIN_TOKEN            # a secret held in a local, as in deploy_source()
            database_url = _DB_URL
            raise RuntimeError(f"railway up failed for {database_url} (token {project_token[:4]})")

        try:
            _fail()
        except RuntimeError:
            from sentry_sdk.utils import event_from_exception
            event, hint = event_from_exception(sys.exc_info(), client_options=client.options)
            client.capture_event(event, hint)
        client.flush(timeout=2)
        client.close()

        assert sent, "the event did not reach the transport"
        flat = repr(sent)
        assert _PG_PASSWORD not in flat
        assert _LOGIN_TOKEN not in flat
        frames = sent[0]["exception"]["values"][0]["stacktrace"]["frames"]
        assert frames and all("vars" not in frame for frame in frames)

    def test_sdk_defaults_would_have_leaked_the_locals(self):
        """Control for the test above: with the SDK defaults the same frame's
        locals are in the event."""
        import sentry_sdk
        from sentry_sdk.utils import event_from_exception

        client = sentry_sdk.Client(dsn=None)

        def _fail():
            project_token = _LOGIN_TOKEN
            raise RuntimeError("boom")

        try:
            _fail()
        except RuntimeError:
            event, _hint = event_from_exception(sys.exc_info(), client_options=client.options)
        client.close()
        assert _LOGIN_TOKEN in repr(event)
