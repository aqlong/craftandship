"""Tests for deploy_source() deployment-id extraction (cli/deploy.py).

Regression for v2-deploy-source-deployment-id-fix (2026-05-02): the railway CLI
"Build Logs" URL has the form
  https://railway.com/project/<project_id>/service/<service_id>?id=<deployment_id>
The deployment_id lives in the `?id=<uuid>` query string, NOT in the path. The
prior regex matched the path-segment UUID first, which captured the service_id
and broke wait_for_deployment with 'Deployment not found'. Tests assert
extraction prefers the query-string id and only falls through to legacy
patterns when no `?id=<uuid>` is present.
"""

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)


SERVICE_ID = "11111111-2222-3333-4444-555555555555"
DEPLOYMENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PROJECT_ID = "99999999-8888-7777-6666-555555555555"


def _run_deploy_source(stdout, stderr=""):
    """Run cli.deploy.deploy_source() with a stubbed subprocess + project token."""
    from deploy import deploy_source

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = stdout
    fake_result.stderr = stderr

    with patch("deploy.create_project_token", return_value="proj-tok-123"), \
         patch("deploy.subprocess.run", return_value=fake_result):
        return deploy_source(
            build_dir="/tmp/build",
            project_id=PROJECT_ID,
            environment_id="env-id",
            service_id=SERVICE_ID,
            token="api-tok",
        )


class TestDeploySourceIdExtraction:
    def test_prefers_query_string_id_over_path_uuid(self):
        """The canonical Build Logs URL is
        `https://railway.com/project/P/service/S?id=D`. Returning S
        (service_id) instead of D (deployment_id) is the bug.
        Asserts the query-string id wins."""
        stdout = (
            "Indexed\nCompressed [====] 100%\nUploaded\n"
            f"Build Logs: https://railway.com/project/{PROJECT_ID}/service/{SERVICE_ID}?id={DEPLOYMENT_ID}\n"
        )
        result = _run_deploy_source(stdout)
        assert result == DEPLOYMENT_ID
        assert result != SERVICE_ID

    def test_handles_amp_id_query_param(self):
        """When the query string has multiple params, `&id=<uuid>` works
        the same as `?id=<uuid>`."""
        stdout = f"Build Logs: https://railway.com/project/p/service/s?env=production&id={DEPLOYMENT_ID}\n"
        result = _run_deploy_source(stdout)
        assert result == DEPLOYMENT_ID

    def test_falls_through_to_path_uuid_when_no_query_string(self):
        """Older CLI versions emitted a UUID path segment without
        `?id=`. The fallback still works (with the caveat documented
        in the source: the caller's get_latest_deployment retry loop
        recovers if this happens to be a service_id, not a deployment
        id)."""
        stdout = f"Build Logs: https://railway.com/project/p/{DEPLOYMENT_ID}\n"
        result = _run_deploy_source(stdout)
        assert result == DEPLOYMENT_ID

    def test_falls_through_to_standalone_uuid_when_no_url(self):
        """Last-ditch: any UUID in the text."""
        stdout = f"Deployment created: {DEPLOYMENT_ID}\n"
        result = _run_deploy_source(stdout)
        assert result == DEPLOYMENT_ID

    def test_returns_none_when_no_uuid_in_output(self):
        result = _run_deploy_source("Some unrelated output without an id\n")
        assert result is None

    def test_query_string_id_wins_when_path_also_has_uuid(self):
        """The bug case directly: path UUID is service_id, query UUID is
        deployment_id. Path UUID must NOT win."""
        stdout = f"https://railway.com/project/{PROJECT_ID}/service/{SERVICE_ID}?id={DEPLOYMENT_ID}\n"
        result = _run_deploy_source(stdout)
        assert result == DEPLOYMENT_ID, (
            f"Expected query-string id ({DEPLOYMENT_ID}), got {result}. "
            f"This means the regex matched the path segment (service_id) first."
        )

    def test_raises_on_nonzero_exit(self):
        from deploy import deploy_source
        fake = MagicMock()
        fake.returncode = 1
        fake.stdout = ""
        fake.stderr = "auth failed"
        with patch("deploy.create_project_token", return_value="t"), \
             patch("deploy.subprocess.run", return_value=fake):
            with pytest.raises(RuntimeError, match="railway up failed"):
                deploy_source(
                    build_dir="/tmp/build", project_id="p", environment_id="e",
                    service_id="s", token="t",
                )
