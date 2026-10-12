# tests/test_watchdog_probe.py: Unit tests for queue/watchdog_probe.py
#
# probe_for_zombie_deploy is the shared Railway /health probe used by both:
#   - queue/worker_pg.py  _supervisor_reality_probe (Fix #1)
#   - coordinator/agents/reconciliation.py  ReconciliationAgent (Fix #3)
#
# All tests mock the Railway API client so no network calls are made.
# Every test path must respect the fail-safe contract: the function MUST NOT raise.

from __future__ import annotations

import http.server
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Make queue/ importable as flat modules (same convention as test_worker_pg.py)
_ROOT = Path(__file__).parent.parent
_QUEUE_DIR = str(_ROOT / "queue")
_CLI_DIR = str(_ROOT / "cli")
for _p in (_QUEUE_DIR, _CLI_DIR, str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import requests

import watchdog_probe as _wp
from deploy import health_response_ok as _real_health_response_ok
from monitor.ping import guarded_session as _real_guarded_session
from tests._guarded_session_mock import health_answer, patched_probe_get
from tests.test_probe_guard import loopback_counts_as_public  # noqa: F401  (a fixture)
from watchdog_probe import probe_for_zombie_deploy

# What a healthy generated app answers at /health.
HEALTHY_BODY = b'{"status":"ok"}'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(year=2026, month=4, day=10, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _make_deploy(projects=None, domain_info=None, token="tok"):
    """Return a minimal mock for the deploy module. The definition of a
    healthy answer is the real one (cli/deploy.py health_response_ok)."""
    m = MagicMock()
    m.health_response_ok = _real_health_response_ok
    m.get_token.return_value = token
    m.list_projects.return_value = projects or []
    m.get_service_domain.return_value = domain_info
    return m


def _project(name="my-app", created_at=None, proj_id="proj-1"):
    created_at = created_at or "2026-04-10T12:05:00+00:00"
    return {"id": proj_id, "name": name, "createdAt": created_at}


# ---------------------------------------------------------------------------
# 1. Empty / falsy app_name → immediate None
# ---------------------------------------------------------------------------

class TestEmptyAppName:
    def test_empty_string_returns_none(self):
        assert probe_for_zombie_deploy("", started_at=_utc()) is None

    def test_none_app_name_returns_none(self):
        assert probe_for_zombie_deploy(None, started_at=_utc()) is None


# ---------------------------------------------------------------------------
# 2. deploy module unavailable → None (fail-safe, no raise)
# ---------------------------------------------------------------------------

class TestDeployModuleUnavailable:
    def test_returns_none_when_deploy_not_importable(self):
        """Both import paths fail → probe returns None without raising."""
        with patch.dict("sys.modules", {"deploy": None, "cli.deploy": None}):
            # Remove cached module entries to force reimport
            sys.modules.pop("deploy", None)
            sys.modules.pop("cli", None)
            with patch("builtins.__import__", side_effect=ImportError("no module")):
                result = probe_for_zombie_deploy("my-app", started_at=_utc())
        assert result is None


# ---------------------------------------------------------------------------
# 3. RAILWAY_API_TOKEN not set → None
# ---------------------------------------------------------------------------

class TestTokenNotSet:
    def test_the_real_get_token_without_a_token_returns_none(self, monkeypatch):
        """deploy.get_token raises DeployConfigError when no token is set;
        the probe reads that as "no probe"."""
        import deploy as real_deploy
        monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
        mock_deploy = _make_deploy()
        mock_deploy.get_token.side_effect = real_deploy.get_token

        with patch.dict("sys.modules", {"deploy": mock_deploy}):
            result = probe_for_zombie_deploy("my-app", started_at=_utc())
        assert result is None
        mock_deploy.list_projects.assert_not_called()

    def test_exception_from_get_token_returns_none(self):
        mock_deploy = _make_deploy()
        mock_deploy.get_token.side_effect = RuntimeError("no token")

        with patch.dict("sys.modules", {"deploy": mock_deploy}):
            result = probe_for_zombie_deploy("my-app", started_at=_utc())
        assert result is None


# ---------------------------------------------------------------------------
# 4. list_projects fails → None
# ---------------------------------------------------------------------------

class TestListProjectsFails:
    def test_exception_returns_none(self):
        mock_deploy = _make_deploy()
        mock_deploy.list_projects.side_effect = RuntimeError("Railway down")

        with patch.dict("sys.modules", {"deploy": mock_deploy}):
            result = probe_for_zombie_deploy("my-app", started_at=_utc())
        assert result is None


# ---------------------------------------------------------------------------
# 5. No matching projects → None
# ---------------------------------------------------------------------------

class TestNoCandidates:
    def test_empty_project_list_returns_none(self):
        mock_deploy = _make_deploy(projects=[])
        with patch.dict("sys.modules", {"deploy": mock_deploy}):
            result = probe_for_zombie_deploy("my-app", started_at=_utc())
        assert result is None

    def test_name_mismatch_returns_none(self):
        proj = _project(name="other-app", created_at="2026-04-10T12:05:00+00:00")
        mock_deploy = _make_deploy(projects=[proj])
        with patch.dict("sys.modules", {"deploy": mock_deploy}):
            result = probe_for_zombie_deploy("my-app", started_at=_utc())
        assert result is None

    def test_project_outside_time_window_returns_none(self):
        """Project created 2 hours before started_at - PROBE_WINDOW_BEFORE_S → excluded."""
        started = _utc(hour=12)
        # created_at is 2h before started - 5min → outside lower bound
        outside = started - timedelta(hours=2)
        proj = _project(
            name="my-app",
            created_at=outside.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        )
        mock_deploy = _make_deploy(projects=[proj])
        with patch.dict("sys.modules", {"deploy": mock_deploy}):
            result = probe_for_zombie_deploy("my-app", started_at=started)
        assert result is None

    def test_project_far_after_window_end_returns_none(self):
        """Project created well after (completed_at + 30min) → excluded."""
        started = _utc(hour=10)
        completed = _utc(hour=11)
        # 2 hours after completed → outside upper bound
        outside = completed + timedelta(hours=2)
        proj = _project(
            name="my-app",
            created_at=outside.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        )
        mock_deploy = _make_deploy(projects=[proj])
        with patch.dict("sys.modules", {"deploy": mock_deploy}):
            result = probe_for_zombie_deploy(
                "my-app", started_at=started, completed_at=completed
            )
        assert result is None


# ---------------------------------------------------------------------------
# 6. Candidate found but /health returns non-2xx → None
# ---------------------------------------------------------------------------

class TestHealthNon2xx:
    def test_404_returns_none(self):
        started = _utc(hour=10)
        completed = _utc(hour=11)
        proj = _project(
            name="my-app",
            created_at="2026-04-10T10:30:00+00:00",
        )
        mock_deploy = _make_deploy(
            projects=[proj],
            domain_info={"domain": "my-app.railway.app", "service_id": "svc-1"},
        )
        http_err = health_answer(404, "Not Found")

        with patch.dict("sys.modules", {"deploy": mock_deploy}), \
             patched_probe_get("monitor.ping", return_value=http_err):
            result = probe_for_zombie_deploy(
                "my-app", started_at=started, completed_at=completed
            )
        assert result is None

    def test_network_error_returns_none(self):
        started = _utc(hour=10)
        completed = _utc(hour=11)
        proj = _project(name="my-app", created_at="2026-04-10T10:30:00+00:00")
        mock_deploy = _make_deploy(
            projects=[proj],
            domain_info={"domain": "my-app.railway.app", "service_id": "svc-1"},
        )

        with patch.dict("sys.modules", {"deploy": mock_deploy}), \
             patched_probe_get("monitor.ping",
                   side_effect=OSError("connection refused")):
            result = probe_for_zombie_deploy(
                "my-app", started_at=started, completed_at=completed
            )
        assert result is None


# ---------------------------------------------------------------------------
# 7. Candidate found and /health returns 200 → dict with expected keys
# ---------------------------------------------------------------------------

class TestHealthy:
    def test_200_returns_live_url_dict(self):
        started = _utc(hour=10)
        completed = _utc(hour=11)
        proj = _project(
            name="my-app",
            created_at="2026-04-10T10:30:00+00:00",
            proj_id="proj-abc",
        )
        mock_deploy = _make_deploy(
            projects=[proj],
            domain_info={"domain": "my-app.up.railway.app", "service_id": "svc-xyz"},
        )
        mock_resp = health_answer(200, HEALTHY_BODY)

        with patch.dict("sys.modules", {"deploy": mock_deploy}), \
             patched_probe_get("monitor.ping", return_value=mock_resp):
            result = probe_for_zombie_deploy(
                "my-app", started_at=started, completed_at=completed
            )

        assert result is not None
        assert result["live_url"] == "https://my-app.up.railway.app"
        assert result["project_id"] == "proj-abc"
        assert result["service_id"] == "svc-xyz"

    def test_freshest_project_tried_first(self):
        """When two projects match, the one with the later createdAt is tried first."""
        started = _utc(hour=10)
        completed = _utc(hour=11)
        older = _project(
            name="my-app",
            created_at="2026-04-10T10:30:00+00:00",
            proj_id="proj-old",
        )
        newer = _project(
            name="my-app",
            created_at="2026-04-10T10:45:00+00:00",  # later
            proj_id="proj-new",
        )
        mock_deploy = _make_deploy(
            projects=[older, newer],  # older listed first, but newer should be tried first
            domain_info={"domain": "my-app.railway.app", "service_id": "svc-1"},
        )
        tried_ids = []

        def fake_get_domain(project_id, token, deadline=None):
            tried_ids.append(project_id)
            return {"domain": "my-app.railway.app", "service_id": "svc-1"}

        mock_deploy.get_service_domain.side_effect = fake_get_domain
        mock_resp = health_answer(200, HEALTHY_BODY)

        with patch.dict("sys.modules", {"deploy": mock_deploy}), \
             patched_probe_get("monitor.ping", return_value=mock_resp):
            probe_for_zombie_deploy("my-app", started_at=started, completed_at=completed)

        assert tried_ids[0] == "proj-new", "Freshest project must be tried first"


# ---------------------------------------------------------------------------
# 8. get_service_domain failure → skip candidate, not raise
# ---------------------------------------------------------------------------

class TestServiceDomainFailure:
    def test_exception_skips_candidate(self):
        started = _utc(hour=10)
        completed = _utc(hour=11)
        proj = _project(name="my-app", created_at="2026-04-10T10:30:00+00:00")
        mock_deploy = _make_deploy(projects=[proj])
        mock_deploy.get_service_domain.side_effect = RuntimeError("Railway API error")

        with patch.dict("sys.modules", {"deploy": mock_deploy}):
            result = probe_for_zombie_deploy(
                "my-app", started_at=started, completed_at=completed
            )
        assert result is None

    def test_none_domain_info_skips_candidate(self):
        started = _utc(hour=10)
        completed = _utc(hour=11)
        proj = _project(name="my-app", created_at="2026-04-10T10:30:00+00:00")
        mock_deploy = _make_deploy(projects=[proj], domain_info=None)

        with patch.dict("sys.modules", {"deploy": mock_deploy}):
            result = probe_for_zombie_deploy(
                "my-app", started_at=started, completed_at=completed
            )
        assert result is None

    def test_missing_domain_key_skips_candidate(self):
        started = _utc(hour=10)
        completed = _utc(hour=11)
        proj = _project(name="my-app", created_at="2026-04-10T10:30:00+00:00")
        mock_deploy = _make_deploy(
            projects=[proj],
            domain_info={"service_id": "svc-1"},  # no "domain" key
        )

        with patch.dict("sys.modules", {"deploy": mock_deploy}):
            result = probe_for_zombie_deploy(
                "my-app", started_at=started, completed_at=completed
            )
        assert result is None


# ---------------------------------------------------------------------------
# 9. Deadline exceeded mid-probe → None (fail-safe, no stuck probe)
# ---------------------------------------------------------------------------

class TestDeadlineEnforcement:
    def test_deadline_exceeded_after_list_projects_returns_none(self):
        """Probe returns None if deadline fires between list_projects and filtering."""
        started = _utc(hour=10)
        completed = _utc(hour=11)
        proj = _project(name="my-app", created_at="2026-04-10T10:30:00+00:00")
        mock_deploy = _make_deploy(projects=[proj])

        # Monotonic time: deadline = start + 20s; simulate time jumping past deadline
        call_count = [0]
        base = time.monotonic()

        def fake_monotonic():
            call_count[0] += 1
            # First few calls return base (setup), then jump past deadline
            if call_count[0] > 4:
                return base + 9999
            return base

        with patch.dict("sys.modules", {"deploy": mock_deploy}), \
             patch("watchdog_probe.time.monotonic", side_effect=fake_monotonic):
            result = probe_for_zombie_deploy(
                "my-app", started_at=started, completed_at=completed,
                hard_timeout_s=20,
            )
        assert result is None


# ---------------------------------------------------------------------------
# 10. started_at=None uses upper-bound only (no lower bound filtering)
# ---------------------------------------------------------------------------

class TestNullStartedAt:
    def test_none_started_at_skips_lower_bound_filter(self):
        """When started_at is None, no lower bound is applied, any matching name + created < window_end is a candidate."""
        proj = _project(
            name="my-app",
            created_at="2026-04-10T11:00:00+00:00",
            proj_id="proj-1",
        )
        mock_deploy = _make_deploy(
            projects=[proj],
            domain_info={"domain": "my-app.railway.app", "service_id": "svc-1"},
        )
        mock_resp = health_answer(200, HEALTHY_BODY)

        # completed_at in window; started_at=None should not exclude this candidate
        completed = _utc(hour=12)
        with patch.dict("sys.modules", {"deploy": mock_deploy}), \
             patched_probe_get("monitor.ping", return_value=mock_resp):
            result = probe_for_zombie_deploy(
                "my-app", started_at=None, completed_at=completed
            )
        assert result is not None


# ---------------------------------------------------------------------------
# Review round R1-P2: the "must not raise" contract, resource handling, input checks
# ---------------------------------------------------------------------------

class TestNeverRaises:
    def test_garbage_timestamps_return_none_instead_of_raising(self):
        mock_deploy = _make_deploy(projects=[_project()])
        with patch.dict("sys.modules", {"deploy": mock_deploy}):
            assert probe_for_zombie_deploy("my-app", started_at="not-a-datetime") is None
            assert probe_for_zombie_deploy("my-app", started_at=_utc(), completed_at=object()) is None


class TestProbeSessionIsClosed:
    @pytest.mark.parametrize("outcome", [
        {"return_value": health_answer(200, HEALTHY_BODY)},
        {"side_effect": requests.exceptions.Timeout("exchange deadline exceeded")},
    ])
    def test_the_session_is_closed_whether_or_not_the_app_answered(self, outcome):
        proj = _project(name="my-app", created_at="2026-04-10T10:30:00+00:00")
        mock_deploy = _make_deploy(
            projects=[proj], domain_info={"domain": "my-app.up.railway.app", "service_id": "svc-1"}
        )
        probe = patched_probe_get("monitor.ping", **outcome)
        with patch.dict("sys.modules", {"deploy": mock_deploy}), probe:
            probe_for_zombie_deploy("my-app", started_at=_utc(hour=10), completed_at=_utc(hour=11))
        probe.session.close.assert_called_once()


class TestMalformedDomain:
    @pytest.mark.parametrize("domain", ["evil.example.test/steal#", "user@evil.example.test", "a b.example.test", "-bad.example.test", 42])
    def test_non_hostname_domains_are_never_requested(self, domain):
        proj = _project(name="my-app", created_at="2026-04-10T10:30:00+00:00")
        mock_deploy = _make_deploy(projects=[proj], domain_info={"domain": domain, "service_id": "svc-1"})
        with patch.dict("sys.modules", {"deploy": mock_deploy}), \
             patched_probe_get("monitor.ping") as urlopen:
            result = probe_for_zombie_deploy("my-app", started_at=_utc(hour=10), completed_at=_utc(hour=11))
        assert result is None
        urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# Ownership and probe-target guards
# ---------------------------------------------------------------------------

class TestOwnershipAndTargetGuards:
    """app_name is not unique, so a name match alone is not ownership; and the
    probed app is customer code, so the request goes through the URL guard."""

    def _window(self):
        return _utc(hour=10), _utc(hour=11)

    def test_project_recorded_on_another_build_is_never_probed(self):
        started, completed = self._window()
        other = _project(name="my-app", created_at="2026-04-10T10:40:00+00:00", proj_id="proj-other")
        mock_deploy = _make_deploy(
            projects=[other],
            domain_info={"domain": "my-app.up.railway.app", "service_id": "svc-other"},
        )
        with patch.dict("sys.modules", {"deploy": mock_deploy}), \
             patched_probe_get("monitor.ping") as opener:
            result = probe_for_zombie_deploy(
                "my-app", started_at=started, completed_at=completed,
                exclude_project_ids={"proj-other"},
            )
        assert result is None
        mock_deploy.get_service_domain.assert_not_called()
        opener.assert_not_called()

    def test_own_project_is_still_found_next_to_an_excluded_one(self):
        started, completed = self._window()
        other = _project(name="my-app", created_at="2026-04-10T10:50:00+00:00", proj_id="proj-other")
        mine = _project(name="my-app", created_at="2026-04-10T10:30:00+00:00", proj_id="proj-mine")
        mock_deploy = _make_deploy(
            projects=[other, mine],
            domain_info={"domain": "my-app.up.railway.app", "service_id": "svc-mine"},
        )
        mock_resp = health_answer(200, HEALTHY_BODY)
        with patch.dict("sys.modules", {"deploy": mock_deploy}), \
             patched_probe_get("monitor.ping", return_value=mock_resp):
            result = probe_for_zombie_deploy(
                "my-app", started_at=started, completed_at=completed,
                exclude_project_ids=["proj-other"],
            )
        assert result is not None and result["project_id"] == "proj-mine"
        mock_deploy.get_service_domain.assert_called_once()
        assert mock_deploy.get_service_domain.call_args[0][0] == "proj-mine"

    @pytest.mark.parametrize("domain", ["web.railway.internal", "localhost", "2130706433", "10.0.0.5", "postgres"])
    def test_internal_domain_from_railway_is_not_probed(self, domain):
        started, completed = self._window()
        proj = _project(name="my-app", created_at="2026-04-10T10:30:00+00:00")
        mock_deploy = _make_deploy(projects=[proj], domain_info={"domain": domain, "service_id": "svc-1"})
        with patch.dict("sys.modules", {"deploy": mock_deploy}), \
             patched_probe_get("monitor.ping") as opener:
            result = probe_for_zombie_deploy("my-app", started_at=started, completed_at=completed)
        assert result is None
        opener.assert_not_called()

    def test_probe_sends_through_the_guarded_session_with_a_deadline_and_a_cap(self):
        started, completed = self._window()
        proj = _project(name="my-app", created_at="2026-04-10T10:30:00+00:00")
        mock_deploy = _make_deploy(
            projects=[proj], domain_info={"domain": "my-app.up.railway.app", "service_id": "svc-1"},
        )
        mock_resp = health_answer(200, HEALTHY_BODY)
        probe = patched_probe_get("monitor.ping", return_value=mock_resp)
        with patch.dict("sys.modules", {"deploy": mock_deploy}), \
             patch("urllib.request.urlopen", side_effect=AssertionError("unguarded urlopen used")), \
             patch("requests.get", side_effect=AssertionError("unguarded requests.get used")), \
             probe as get:
            result = probe_for_zombie_deploy("my-app", started_at=started, completed_at=completed)
        assert result is not None
        assert get.call_args[0][0] == "https://my-app.up.railway.app/health"
        assert 0 < get.call_args.kwargs["timeout"] <= _wp.PROBE_PER_URL_S
        assert get.call_args.kwargs["allow_redirects"] is False
        assert probe.guarded_session.call_args.kwargs == {"max_body_bytes": _wp.PROBE_MAX_BODY_BYTES}


# ---------------------------------------------------------------------------
# 8. A redirect is not a healthy answer: the real guarded session, on loopback
# ---------------------------------------------------------------------------

class _HealthApp(http.server.BaseHTTPRequestHandler):
    """Stands in for a deployed app on loopback. With `health_redirects` set,
    /health answers 302 to /landing, which answers the healthy body; otherwise
    /health answers the healthy body itself. Every path asked for is recorded."""

    seen = None
    health_redirects = False

    def do_GET(self):
        self.seen.append(self.path)
        if self.path == "/health" and self.health_redirects:
            self.send_response(302)
            self.send_header("Location", "/landing")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = HEALTHY_BODY
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def health_app():
    """Returns (port, paths, set_redirects): the loopback port, the list of
    paths asked for, and a function that chooses whether /health redirects."""
    seen = []
    handler = type("HealthApp", (_HealthApp,), {"seen": seen})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def set_redirects(value):
        handler.health_redirects = value

    yield server.server_address[1], seen, set_redirects
    server.shutdown()
    server.server_close()


def _probe_loopback_app(port):
    """probe_for_zombie_deploy against the loopback app. The project and its
    domain come from the Railway mock; the probe still sends
    https://<domain>/health through the real guarded session, and this
    wrapper only sends that one request to the loopback listener instead."""
    proj = _project(name="my-app", created_at="2026-04-10T10:30:00+00:00", proj_id="proj-loop")
    mock_deploy = _make_deploy(
        projects=[proj],
        domain_info={"domain": "my-app.up.railway.app", "service_id": "svc-loop"},
    )

    def loopback_session(**kwargs):
        session = _real_guarded_session(**kwargs)
        real_get = session.get

        def get(url, **get_kwargs):
            assert url == "https://my-app.up.railway.app/health"
            return real_get(f"http://127.0.0.1:{port}/health", **get_kwargs)

        session.get = get
        return session

    with patch.dict("sys.modules", {"deploy": mock_deploy}), \
         patch("monitor.ping.guarded_session", side_effect=loopback_session):
        return probe_for_zombie_deploy("my-app", started_at=_utc(hour=10), completed_at=_utc(hour=11))


class TestRedirectIsNotHealthy:
    def test_a_healthy_answer_on_loopback_is_found(self, health_app, loopback_counts_as_public):
        port, seen, set_redirects = health_app
        set_redirects(False)
        result = _probe_loopback_app(port)
        assert result is not None and result["live_url"] == "https://my-app.up.railway.app"
        assert seen == ["/health"]

    def test_a_302_to_a_healthy_page_is_not_a_healthy_answer(self, health_app, loopback_counts_as_public):
        """The 302 is the answer, and it is not healthy. The probe does not
        follow it to /landing, so it returns None and asks for nothing else."""
        port, seen, set_redirects = health_app
        set_redirects(True)
        result = _probe_loopback_app(port)
        assert result is None
        assert seen == ["/health"]
