"""What cli/deploy.py does when Railway's answer is missing, bad or ambiguous.

Four behaviors, each tested through the function that has it:

  * gql() never sends a mutation twice unless sending it twice is harmless.
    A read timeout after projectCreate used to be retried, which could create
    a second project that nothing recorded;
  * deploy() reports what Railway said about the deployment (CRASHED, FAILED),
    taken from the status Railway returned and not from the words in an error
    text, so the worker can tell a crash of the generated app from a platform
    failure;
  * a healthy /health answer is HTTP 200 with a JSON body whose status is
    "ok", for a fresh deploy (verify_health) and for a deploy the supervisor
    finds afterwards (queue/watchdog_probe.py): the same function decides;
  * the environment deploy() gives a generated app holds every variable the
    emitted code reads (the names are taken from compose/build.py here), and
    still not the platform's mail key.

No network: requests.post, the probe session and the Railway helpers are
replaced. Nothing here needs a database.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
for _entry in (str(ROOT), str(ROOT / "cli"), str(ROOT / "queue")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import deploy
import watchdog_probe
from tests._guarded_session_mock import health_answer, patched_probe_get

CREATE = "mutation ProjectCreate($input: ProjectCreateInput!) { projectCreate(input: $input) { id name } }"
UPSERT = "mutation VariableCollectionUpsert($input: X!) { variableCollectionUpsert(input: $input) }"
QUERY = "query Deployments($id: String!) { deployments { edges { node { id } } } }"


def _answer(status=200, data=None):
    response = MagicMock(status_code=status)
    response.json.return_value = {"data": data if data is not None else {"ok": True}}
    response.text = json.dumps(response.json.return_value)
    return response


@pytest.fixture()
def no_sleep(monkeypatch):
    monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)


def _post(side_effect):
    return patch("deploy.provider_post", side_effect=side_effect)


# ---------------------------------------------------------------------------
# gql(): a mutation is not sent twice on an unknown outcome
# ---------------------------------------------------------------------------

class TestMutationsAreNotBlindlyRetried:
    @pytest.mark.parametrize("failure", [
        requests.exceptions.ReadTimeout("read timed out"),
        requests.exceptions.ConnectionError("connection aborted"),
        requests.exceptions.ChunkedEncodingError("response ended early"),
    ])
    def test_a_mutation_whose_answer_never_arrives_is_sent_once(self, failure, no_sleep):
        with _post([failure, _answer(data={"projectCreate": {"id": "second"}})]) as post, \
                patch("builtins.print"):
            with pytest.raises(deploy.RailwayOutcomeUnknown, match="ProjectCreate"):
                deploy.gql(CREATE, {"input": {"name": "sample-app"}}, token="t")
        assert post.call_count == 1, "the mutation was sent again after an unknown outcome"

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_a_mutation_answered_with_a_server_error_is_sent_once(self, status, no_sleep):
        with _post([_answer(status), _answer(data={"projectCreate": {"id": "second"}})]) as post, \
                patch("builtins.print"):
            with pytest.raises(deploy.RailwayOutcomeUnknown):
                deploy.gql(CREATE, {"input": {"name": "sample-app"}}, token="t")
        assert post.call_count == 1

    def test_a_mutation_is_sent_again_when_it_cannot_have_been_applied(self, no_sleep):
        """Rate limited (429), or the connection was never established."""
        for first in (_answer(429), requests.exceptions.ConnectTimeout("no route")):
            with _post([first, _answer(data={"projectCreate": {"id": "p-1"}})]) as post, \
                    patch("builtins.print"):
                assert deploy.gql(CREATE, {"input": {"name": "sample-app"}}, token="t") == \
                    {"projectCreate": {"id": "p-1"}}
            assert post.call_count == 2

    @pytest.mark.parametrize("first", [
        requests.exceptions.ReadTimeout("read timed out"),
        _answer(502),
    ])
    def test_a_query_and_a_repeatable_mutation_are_retried(self, first, no_sleep):
        with _post([first, _answer()]) as post, patch("builtins.print"):
            assert deploy.gql(QUERY, {"id": "x"}, token="t") == {"ok": True}
        assert post.call_count == 2
        with _post([first, _answer()]) as post, patch("builtins.print"):
            assert deploy.gql(UPSERT, {"input": {}}, token="t", repeatable=True) == {"ok": True}
        assert post.call_count == 2

    def test_an_unknown_outcome_is_a_runtime_error_that_names_no_secret(self, no_sleep):
        secret = "railway-" + "token-" + "value"
        with _post([requests.exceptions.ReadTimeout(f"timeout talking to host with {secret}")]), \
                patch("builtins.print"):
            with pytest.raises(RuntimeError) as raised:
                deploy.gql(CREATE, {"input": {"name": "sample-app"}}, token=secret)
        assert secret not in str(raised.value)

    def test_every_mutation_in_the_module_is_classified(self):
        """Each mutation deploy.py sends, and whether it may be sent twice.
        A new mutation has to be added here on purpose."""
        expected = {
            "ProjectCreate": False,
            "ServiceCreate": False,           # sent for the app service and for Postgres
            "ServiceDomainCreate": False,
            "CustomDomainCreate": False,
            "ProjectTokenCreate": False,
            "ProjectDelete": False,
            "Redeploy": True,                 # of the fresh Postgres service
            "VariableCollectionUpsert": True,
        }
        found = {}
        for node in ast.walk(ast.parse((ROOT / "cli" / "deploy.py").read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "gql"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            kind, name = deploy._operation(node.args[0].value)
            if kind != "mutation":
                continue
            repeatable = any(k.arg == "repeatable" and isinstance(k.value, ast.Constant) and k.value.value is True
                             for k in node.keywords)
            assert found.setdefault(name, repeatable) == repeatable, f"{name} is classified two ways"
        assert found == expected

    @pytest.mark.parametrize("document, expected", [
        ("\n        mutation ProjectCreate($input: X!) { projectCreate }", ("mutation", "ProjectCreate")),
        ("mutation($id: String!) { projectDelete(id: $id) }", ("mutation", "")),
        ("query { projects { edges { node { id } } } }", ("query", "")),
        ("query($id: String!) { project(id: $id) { id } }", ("query", "")),
        ("{ me { id } }", ("query", "")),
    ])
    def test_the_kind_of_a_document_is_read_from_its_first_word(self, document, expected):
        assert deploy._operation(document) == expected


class TestAnUnconfirmedProjectIsNeverCreatedTwice:
    def test_create_project_fails_and_names_what_railway_may_have_made(self, no_sleep):
        """The scenario: projectCreate is applied, the answer is lost. The old
        code sent it again and returned the second project's id; the first
        was on no build row and in no attempt's state."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        listing = {"projects": {"edges": [
            {"node": {"id": "made-just-now", "name": "sample-app", "createdAt": now.isoformat()}},
            {"node": {"id": "an-older-deploy", "name": "sample-app",
                      "createdAt": (now - timedelta(days=3)).isoformat()}},
            {"node": {"id": "another-app", "name": "other-app", "createdAt": now.isoformat()}},
        ]}}
        sent = []

        def post(url, json=None, headers=None, timeout=None):
            sent.append(json["query"])
            if "ProjectCreate" in json["query"]:
                raise requests.exceptions.ReadTimeout("read timed out")
            return _answer(data=listing)

        with _post(post), patch("builtins.print"):
            with pytest.raises(deploy.RailwayOutcomeUnknown) as raised:
                deploy.create_project("sample-app", "t")
        assert sum("ProjectCreate" in query for query in sent) == 1
        assert raised.value.unrecorded_project_candidates == ["made-just-now"]

    def test_deploy_fails_the_attempt_and_deletes_nothing_it_did_not_create(self, tmp_path, no_sleep):
        build_dir = tmp_path / "app"
        build_dir.mkdir()
        (build_dir / "package.json").write_text('{"name": "sample-app"}')
        unknown = deploy.RailwayOutcomeUnknown("Railway did not confirm the mutation ProjectCreate")
        unknown.unrecorded_project_candidates = ["made-just-now"]
        with patch("deploy.check_railway_cli"), patch("deploy.get_token", return_value="t"), \
                patch("deploy.create_project", side_effect=unknown) as create, \
                patch("deploy.destroy_project") as destroy, patch("builtins.print"):
            with pytest.raises(deploy.RailwayOutcomeUnknown) as raised:
                deploy.deploy(str(build_dir))
        create.assert_called_once()
        destroy.assert_not_called()
        assert raised.value.unrecorded_project_candidates == ["made-just-now"]


# ---------------------------------------------------------------------------
# deploy(): what Railway said about the deployment
# ---------------------------------------------------------------------------

def _deploy_with(tmp_path, wait_effect, spec=None, env_capture=None):
    build_dir = tmp_path / "app"
    build_dir.mkdir(exist_ok=True)
    (build_dir / "package.json").write_text('{"name": "sample-app"}')
    if spec is not None:
        (build_dir / "spec.json").write_text(json.dumps(spec))

    def capture(project_id, environment_id, service_id, variables, token):
        if env_capture is not None:
            env_capture.update(variables)

    with patch("deploy.check_railway_cli"), patch("deploy.get_token", return_value="t"), \
            patch("deploy.create_project", return_value="proj-1"), \
            patch("deploy.get_environment_id", return_value="env-1"), \
            patch("deploy.create_service", return_value="svc-1"), \
            patch("deploy.provision_postgres", return_value=("db-1", "postgresql://u:p@db.example.test/x")), \
            patch("deploy.set_env_vars", side_effect=capture), \
            patch("deploy.deploy_source", return_value="dep-1"), \
            patch("deploy.wait_for_deployment", side_effect=wait_effect), \
            patch("deploy.generate_domain", return_value="app.example.test"), \
            patch("deploy.verify_health", return_value="ok") as health, \
            patch("deploy.destroy_project") as destroy, \
            patch("builtins.print"):
        try:
            result = deploy.deploy(str(build_dir), password_login_probe=False)
        except Exception as error:  # the test inspects it
            result = error
    return result, health, destroy


class TestDeployReportsRailwaysOwnVerdict:
    def test_a_deployment_that_succeeds_says_so(self, tmp_path):
        result, _health, destroy = _deploy_with(tmp_path, None)
        assert (result["deployment_status"], result["health_check"]) == ("SUCCESS", "ok")
        destroy.assert_not_called()

    @pytest.mark.parametrize("status", ["CRASHED", "FAILED"])
    def test_a_crashed_or_failed_deployment_keeps_its_project_and_carries_the_status(self, tmp_path, status):
        result, health, destroy = _deploy_with(tmp_path, deploy.DeploymentEnded(status, 42))
        assert result["deployment_status"] == status
        assert result["health_check"] == "failed" and result["railway_project_id"] == "proj-1"
        health.assert_not_called()
        destroy.assert_not_called()

    @pytest.mark.parametrize("error", [
        RuntimeError("GraphQL error: the deployment query failed, try again"),
        RuntimeError("Railway API 400: request crashed the validator"),
        RuntimeError("Deployment timed out after 300s"),
        deploy.DeploymentEnded("REMOVED", 12),
    ])
    def test_an_error_that_only_mentions_failed_or_crashed_is_not_a_crashed_app(self, tmp_path, error):
        """deploy() used to look for the words in the error text, so an API
        error was reported as "the app crashed on startup"."""
        result, _health, destroy = _deploy_with(tmp_path, error)
        assert result is error, "the error must reach the caller as a failed deploy"
        destroy.assert_called_once()

    def test_wait_for_deployment_raises_the_typed_end_state(self, no_sleep):
        statuses = iter([{"deployment": {"id": "d", "status": status}}
                         for status in ("DEPLOYING", "FAILED", "FAILED")])
        with patch("deploy.gql", side_effect=lambda *a, **k: next(statuses)), \
                patch("deploy.get_deployment_logs", return_value=[]), patch("builtins.print"):
            with pytest.raises(deploy.DeploymentEnded) as raised:
                deploy.wait_for_deployment("d", "t", timeout=60)
        assert raised.value.status == "FAILED"
        assert isinstance(raised.value, RuntimeError) and str(raised.value).startswith("Deployment failed after")


# ---------------------------------------------------------------------------
# The health gate
# ---------------------------------------------------------------------------

HEALTHY = '{"status":"ok"}'
NOT_HEALTHY = [
    (200, "<html><body>Application is booting, looks ok so far</body></html>"),
    (200, '{"token": "abc", "state": "starting"}'),      # "ok" inside "token"
    (200, '{"status": "db_error"}'),
    (200, '{"status": "booking"}'),
    (200, '["ok"]'),
    (200, '"ok"'),
    (200, ""),
    (200, '{"status": {"ok": true}}'),
    (503, HEALTHY),
    (204, HEALTHY),
    (302, HEALTHY),
]


class TestOneDefinitionOfHealthy:
    @pytest.mark.parametrize("body", [HEALTHY, '{"status": "ok", "timestamp": "2026-01-01T00:00:00Z"}',
                                      '{"status": "OK"}'])
    def test_a_json_status_of_ok_with_200_is_healthy(self, body):
        assert deploy.health_response_ok(200, body) is True

    @pytest.mark.parametrize("status, body", NOT_HEALTHY)
    def test_everything_else_is_not(self, status, body):
        assert deploy.health_response_ok(status, body) is False

    def _verify_health(self, monkeypatch, status, body, timeout=0.2):
        session = MagicMock()
        session.get.return_value = MagicMock(status_code=status, text=body)
        monkeypatch.setattr(deploy, "guarded_session", lambda: session)
        monkeypatch.setattr(deploy, "probe_url_problem", lambda url: None)
        monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)
        with patch("builtins.print"):
            return deploy.verify_health("https://app.example.test", timeout=timeout)

    def test_verify_health_passes_a_healthy_app(self, monkeypatch):
        assert self._verify_health(monkeypatch, 200, HEALTHY) == "ok"

    @pytest.mark.parametrize("status, body", [case for case in NOT_HEALTHY if case[0] == 200
                                              and case[1] != '{"status": "db_error"}'])
    def test_verify_health_does_not_pass_a_200_that_merely_contains_ok(self, monkeypatch, status, body):
        """The gate was `"ok" in resp.text.lower()`."""
        assert self._verify_health(monkeypatch, status, body) == "failed"

    def test_the_apps_own_database_error_is_an_infrastructure_result(self, monkeypatch):
        """A generated app answers 503 {"status": "db_error"} when its database
        does not answer. That used to come back as "failed", which under v2
        ends the build as a deterministic failure."""
        assert self._verify_health(monkeypatch, 503, '{"status": "db_error"}') == "infra"

    def _probe(self, status, body):
        from datetime import datetime, timezone
        started = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
        fake_deploy = MagicMock()
        fake_deploy.get_token.return_value = "t"
        fake_deploy.list_projects.return_value = [
            {"id": "proj-1", "name": "sample-app", "createdAt": "2026-04-10T12:05:00+00:00"}]
        fake_deploy.get_service_domain.return_value = {"domain": "sample-app.example.test", "service_id": "svc-1"}
        fake_deploy.health_response_ok = deploy.health_response_ok   # the real definition
        probe = patched_probe_get("monitor.ping", return_value=health_answer(status, body))
        with patch.dict("sys.modules", {"deploy": fake_deploy}), probe as get, \
                patch("monitor.ping.probe_url_problem", return_value=None):
            result = watchdog_probe.probe_for_zombie_deploy("sample-app", started_at=started)
        get.assert_called_once()   # the verdict below is about this answer, not about a refused request
        return result

    def test_the_supervisors_probe_accepts_a_healthy_app(self):
        assert self._probe(200, HEALTHY) == {
            "live_url": "https://sample-app.example.test", "project_id": "proj-1", "service_id": "svc-1"}

    @pytest.mark.parametrize("status, body", [case for case in NOT_HEALTHY if 200 <= case[0] < 300])
    def test_the_supervisors_probe_does_not_accept_any_2xx(self, status, body):
        """The probe used to take any 2xx as a live deployment and send the
        build to preview, past the gate a fresh deploy has to meet."""
        assert self._probe(status, body) is None


# ---------------------------------------------------------------------------
# The environment a generated app is given
# ---------------------------------------------------------------------------

def _names_the_emitted_app_reads() -> set:
    """Every process.env.NAME in the code compose/build.py emits."""
    return set(re.findall(r"process\.env\.([A-Z][A-Z0-9_]*)", (ROOT / "compose" / "build.py").read_text("utf-8")))


SPEC = {"app_name": "sample-app", "admin_email": "owner@example.test",
        "settings": {"site_name": "Sample Studio"}}


class TestTheGeneratedAppGetsWhatItReads:
    def _sent(self, tmp_path, monkeypatch, spec=SPEC, app_key="app-only-" + "mail-key", domain="mail.example.test"):
        monkeypatch.setenv("RESEND_API_KEY", "platform-" + "mail-key")
        monkeypatch.setenv("ADMIN_EMAIL", "platform-admin@example.test")
        for name, value in ((deploy.GENERATED_APP_MAIL_KEY_ENV, app_key),
                            (deploy.GENERATED_APP_MAIL_DOMAIN_ENV, domain)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        sent = {}
        result, _health, _destroy = _deploy_with(tmp_path, None, spec=spec, env_capture=sent)
        assert isinstance(result, dict), result
        return sent

    def test_every_variable_the_emitted_code_reads_is_set(self, tmp_path, monkeypatch):
        """services/notify.js reads ADMIN_EMAIL, SITE_NAME and
        RESEND_FROM_DOMAIN; deploy() set none of them, so a generated site's
        form notifications never sent and its login mail came from the mail
        provider's shared sender."""
        reads = _names_the_emitted_app_reads()
        assert {"ADMIN_EMAIL", "SITE_NAME", "RESEND_FROM_DOMAIN", "RESEND_API_KEY", "DATABASE_URL",
                "SESSION_SECRET"} <= reads, "the emitted code no longer reads what this test is about"
        sent = self._sent(tmp_path, monkeypatch)
        assert reads <= set(sent), f"the emitted app reads variables deploy() does not set: {reads - set(sent)}"
        assert sent["ADMIN_EMAIL"] == "owner@example.test"
        assert sent["SITE_NAME"] == "Sample Studio"
        assert sent["RESEND_FROM_DOMAIN"] == "mail.example.test"

    def test_the_platforms_own_mail_key_and_admin_address_are_not_handed_over(self, tmp_path, monkeypatch):
        sent = self._sent(tmp_path, monkeypatch)
        assert "platform-" + "mail-key" not in sent.values()
        assert "platform-admin@example.test" not in sent.values()
        assert sent["RESEND_API_KEY"] == "app-only-" + "mail-key"

    def test_without_a_dedicated_key_the_app_gets_no_key_and_no_sending_domain(self, tmp_path, monkeypatch):
        sent = self._sent(tmp_path, monkeypatch, app_key=None)
        assert "RESEND_API_KEY" not in sent and "RESEND_FROM_DOMAIN" not in sent
        assert sent["ADMIN_EMAIL"] == "owner@example.test"

    @pytest.mark.parametrize("spec, missing", [
        ({"admin_email": "not an address"}, "ADMIN_EMAIL"),
        ({"admin_email": "owner@example.test\nBcc: x@example.test"}, "ADMIN_EMAIL"),
        ({"admin_email": ["owner@example.test"]}, "ADMIN_EMAIL"),
        ({"admin_email": "owner@example.test", "settings": {"site_name": "Studio <x@example.test>"}}, "SITE_NAME"),
        ({"admin_email": "owner@example.test", "settings": {"site_name": "x" * 101}}, "SITE_NAME"),
        ({"admin_email": "owner@example.test", "settings": "none"}, "SITE_NAME"),
    ])
    def test_a_value_that_is_not_plain_text_is_left_out(self, tmp_path, monkeypatch, spec, missing):
        assert missing not in self._sent(tmp_path, monkeypatch, spec=spec)

    @pytest.mark.parametrize("domain", ["not a domain", "https://mail.example.test", "mail.example.test/x",
                                        "-mail.example.test", "localhost"])
    def test_a_sending_domain_that_is_not_a_plain_host_name_is_ignored(self, tmp_path, monkeypatch, domain):
        assert "RESEND_FROM_DOMAIN" not in self._sent(tmp_path, monkeypatch, domain=domain)

    def test_a_build_directory_without_a_spec_still_deploys(self, tmp_path, monkeypatch):
        sent = self._sent(tmp_path, monkeypatch, spec=None)
        assert "ADMIN_EMAIL" not in sent and "SITE_NAME" not in sent
        assert sent["DATABASE_URL"]
