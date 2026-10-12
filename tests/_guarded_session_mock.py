"""Test helper: stand in for the guarded HTTP session the probes send through.

monitor.ping.ping and cli/deploy.py's verify_health no longer call
requests.get directly; they send through monitor.ping.guarded_session(), which
pins every connection to an address it checked. Tests that only care about how
a probe reacts to a response (or to an exception) replace that session and
configure its get().

    with patched_probe_get("monitor.ping", return_value=response) as get:
        ping("https://app.example.test")
    get.assert_called_once()

Code that imports guarded_session at call time (queue/watchdog_probe.py, the
coordinator's security agent) is reached by patching it where it lives:
patched_probe_get("monitor.ping", ...).

What the real session does (resolve once, refuse non-public addresses, connect
to what it checked, end at its deadline, stop at its byte cap) is tested
against loopback servers in tests/test_probe_guard.py and
tests/test_bounded_fetch.py.
"""

from unittest.mock import MagicMock, patch


def health_answer(status: int = 200, body='{"status":"ok"}'):
    """A response as the guarded session hands it to a probe: a status code
    and a body that was read under the session's deadline and byte cap."""
    response = MagicMock()
    response.status_code = status
    response.text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    response.body_truncated = False
    return response


class patched_probe_get:
    """Context manager: patch `<module>.guarded_session`; yields the session's get() mock."""

    def __init__(self, module: str, **get_kwargs):
        self.session = MagicMock()
        self.session.get.configure_mock(**get_kwargs)
        self._patcher = patch(f"{module}.guarded_session", return_value=self.session)

    def __enter__(self):
        # Kept so a test can read how the session was asked for (its byte cap).
        self.guarded_session = self._patcher.__enter__()
        return self.session.get

    def __exit__(self, *exc_info):
        return self._patcher.__exit__(*exc_info)
