# tests/test_deploy_fix.py - Tests for post-deploy self-correction module.

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure cli/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))


class TestAttemptDeployFix:
    """Tests for deploy_fix.attempt_deploy_fix()."""

    def test_no_source_files_returns_not_fixed(self, tmp_path):
        from deploy_fix import attempt_deploy_fix

        result = attempt_deploy_fix(
            build_dir=str(tmp_path),
            deploy_logs=["Error: Cannot find module './routes/auth'"],
        )
        assert result["fixed"] is False
        assert "No source files" in result["description"]

    def test_no_issues_returns_not_fixed(self, tmp_path):
        (tmp_path / "server.js").write_text("const app = require('express')();")

        from deploy_fix import attempt_deploy_fix

        result = attempt_deploy_fix(
            build_dir=str(tmp_path),
            deploy_logs=[],
            health_status="ok",
        )
        assert result["fixed"] is False

    @patch("deploy_fix._call_api")
    @patch("deploy_fix._create_client")
    def test_writes_fixed_files(self, mock_client, mock_api, tmp_path):
        # Create a source file
        (tmp_path / "server.js").write_text("const app = require('express')();")
        (tmp_path / "package.json").write_text('{"name": "test-app"}')

        # Mock Claude response with fix
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=(
                    "--BEGINFILE-- server.js\n"
                    "const app = require('express')();\n"
                    "const PORT = process.env.PORT || 3000;\n"
                    "app.get('/health', (req, res) => res.json({status: 'ok'}));\n"
                    "app.listen(PORT);\n"
                    "--ENDFILE--"
                )
            )
        ]
        mock_api.return_value = mock_response

        from deploy_fix import attempt_deploy_fix

        result = attempt_deploy_fix(
            build_dir=str(tmp_path),
            deploy_logs=["Error: listen EADDRINUSE: address already in use :::8080"],
            health_status="failed",
        )

        assert result["fixed"] is True
        assert "server.js" in result["files_changed"]
        # Verify file was actually written
        content = (tmp_path / "server.js").read_text()
        assert "process.env.PORT" in content

    @patch("deploy_fix._call_api")
    @patch("deploy_fix._create_client")
    def test_no_fixes_returned(self, mock_client, mock_api, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000);")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="I cannot determine the issue.")]
        mock_api.return_value = mock_response

        from deploy_fix import attempt_deploy_fix

        result = attempt_deploy_fix(
            build_dir=str(tmp_path),
            deploy_logs=["CRASHED"],
        )
        assert result["fixed"] is False

    @patch("deploy_fix._call_api")
    @patch("deploy_fix._create_client")
    def test_path_traversal_blocked(self, mock_client, mock_api, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000);")

        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=(
                    "--BEGINFILE-- ../../../etc/passwd\n"
                    "malicious content\n"
                    "--ENDFILE--"
                )
            )
        ]
        mock_api.return_value = mock_response

        from deploy_fix import attempt_deploy_fix

        result = attempt_deploy_fix(
            build_dir=str(tmp_path),
            deploy_logs=["CRASHED"],
        )
        assert result["fixed"] is False

    @patch("deploy_fix._call_api")
    @patch("deploy_fix._create_client")
    def test_prompt_includes_deploy_logs(self, mock_client, mock_api, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000);")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No fixes needed.")]
        mock_api.return_value = mock_response

        from deploy_fix import attempt_deploy_fix

        attempt_deploy_fix(
            build_dir=str(tmp_path),
            deploy_logs=["TypeError: Cannot read property 'id' of undefined"],
        )

        # Verify the prompt sent to Claude includes the deploy logs
        call_args = mock_api.call_args
        prompt = call_args[0][1][0]["content"]
        assert "TypeError: Cannot read property" in prompt
        assert "Railway Deployment Logs" in prompt

    @patch("deploy_fix._call_api")
    @patch("deploy_fix._create_client")
    def test_prompt_includes_spec(self, mock_client, mock_api, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000);")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No fixes.")]
        mock_api.return_value = mock_response

        from deploy_fix import attempt_deploy_fix

        attempt_deploy_fix(
            build_dir=str(tmp_path),
            deploy_logs=["CRASHED"],
            spec={"app_name": "my-app", "pages": []},
        )

        call_args = mock_api.call_args
        prompt = call_args[0][1][0]["content"]
        assert "my-app" in prompt


    @patch("deploy_fix._call_api")
    @patch("deploy_fix._create_client")
    def test_prompt_redacts_secrets_from_customer_app_logs(self, mock_client, mock_api, tmp_path):
        """Deploy logs are the customer app's own output. A database URL or key
        in them must not travel to the model provider inside the prompt."""
        (tmp_path / "server.js").write_text("app.listen(3000);")
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No fixes.")]
        mock_api.return_value = mock_response
        password = "pw-" + "q" * 20
        db_url = "postgresql://railway:" + password + "@postgres.railway.internal:5432/railway"

        from deploy_fix import attempt_deploy_fix

        attempt_deploy_fix(
            build_dir=str(tmp_path),
            deploy_logs=[f"Error: connect ECONNREFUSED {db_url}", "at Pool.connect (pg-pool/index.js:45)"],
            app_stderr=f"DATABASE_URL={db_url}",
        )

        prompt = mock_api.call_args[0][1][0]["content"]
        assert password not in prompt
        assert "ECONNREFUSED" in prompt and "pg-pool/index.js" in prompt  # the useful part survives


class TestRedeploy:
    """Tests for deploy.redeploy()."""

    @patch("deploy.verify_health", return_value="ok")
    @patch("deploy.get_deployment_logs", return_value=["Started successfully"])
    @patch("deploy.wait_for_deployment")
    @patch("deploy.get_latest_deployment", return_value="dep-123")
    @patch("deploy.deploy_source", return_value="dep-123")
    @patch("deploy.get_token", return_value="test-token")
    @patch("deploy.check_railway_cli")
    def test_redeploy_success(self, mock_cli, mock_token, mock_source, mock_latest,
                               mock_wait, mock_logs, mock_health, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000);")
        (tmp_path / "package.json").write_text('{"name": "test"}')

        from deploy import redeploy

        result = redeploy(
            str(tmp_path), "proj-1", "env-1", "svc-1", "https://test.up.railway.app"
        )

        assert result["success"] is True
        assert result["health_check"] == "ok"
        assert result["deployment_id"] == "dep-123"

    @patch("deploy.verify_health", return_value="failed")
    @patch("deploy.get_deployment_logs", return_value=["Error: port in use"])
    @patch("deploy.wait_for_deployment")
    @patch("deploy.deploy_source", return_value="dep-456")
    @patch("deploy.get_token", return_value="test-token")
    @patch("deploy.check_railway_cli")
    def test_redeploy_health_fails(self, mock_cli, mock_token, mock_source,
                                    mock_wait, mock_logs, mock_health, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000);")
        (tmp_path / "package.json").write_text('{"name": "test"}')

        from deploy import redeploy

        result = redeploy(
            str(tmp_path), "proj-1", "env-1", "svc-1", "https://test.up.railway.app"
        )

        assert result["success"] is False
        assert result["health_check"] == "failed"

    @patch("deploy.get_deployment_logs", return_value=["Build failed"])
    @patch("deploy.wait_for_deployment", side_effect=RuntimeError("Deployment failed after 30s"))
    @patch("deploy.deploy_source", return_value="dep-789")
    @patch("deploy.get_token", return_value="test-token")
    @patch("deploy.check_railway_cli")
    def test_redeploy_deployment_crashes(self, mock_cli, mock_token, mock_source,
                                         mock_wait, mock_logs, tmp_path):
        (tmp_path / "server.js").write_text("app.listen(3000);")
        (tmp_path / "package.json").write_text('{"name": "test"}')

        from deploy import redeploy

        result = redeploy(
            str(tmp_path), "proj-1", "env-1", "svc-1", "https://test.up.railway.app"
        )

        assert result["success"] is False
        assert "Deployment failed" in result["deploy_logs"][-1]


class TestKBEarlyExit:
    """Tests for failure_kb zero-success-rate early exit before deploy_fix loop."""

    def test_effective_rate_returns_zero_for_all_failures(self):
        """_effective_rate returns 0.0 when recent_outcomes are all False."""
        from failure_kb import _effective_rate

        entry = {"recent_outcomes": [False] * 10}
        rate, total = _effective_rate(entry)
        assert rate == 0.0
        assert total == 10

    def test_effective_rate_uses_window_when_5_or_more(self):
        """With 5+ recent_outcomes, _effective_rate uses the window, not aggregate counts."""
        from failure_kb import _effective_rate

        # Historical counts say 80% success, but recent window is all failures
        entry = {
            "success_count": 80,
            "failure_count": 20,
            "recent_outcomes": [False, False, False, False, False],
        }
        rate, total = _effective_rate(entry)
        assert rate == 0.0
        assert total == 5

    def test_effective_rate_falls_back_to_aggregate_when_fewer_than_5(self):
        """With <5 recent_outcomes, _effective_rate falls back to aggregate counts."""
        from failure_kb import _effective_rate

        entry = {
            "success_count": 3,
            "failure_count": 1,
            "recent_outcomes": [False, False],  # only 2, not enough
        }
        rate, total = _effective_rate(entry)
        assert rate == 0.75  # 3/(3+1)
        assert total == 4

    def test_normalize_error_produces_stable_deploy_crash_sig(self):
        """normalize_error strips variable parts from deploy-crash log lines."""
        from failure_kb import normalize_error

        sig1 = normalize_error("deploy-crash; CRASHED")
        sig2 = normalize_error("deploy-crash; CRASHED")
        assert sig1 == sig2
        assert len(sig1) > 0

    def test_normalize_error_strips_uuids_from_deploy_logs(self):
        """UUIDs in deploy logs are stripped so same error class maps to same signature."""
        from failure_kb import normalize_error

        sig1 = normalize_error(
            "deploy-crash; TypeError: Cannot read property id of "
            "undefined at /app/routes/12345678-1234-1234-1234-123456789abc.js"
        )
        sig2 = normalize_error(
            "deploy-crash; TypeError: Cannot read property id of "
            "undefined at /app/routes/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.js"
        )
        assert sig1 == sig2

    def test_deploy_fix_logs_increased_to_50_lines(self, tmp_path):
        """deploy_fix sends last 50 log lines (not 20) to Claude for better diagnostics."""
        from unittest.mock import MagicMock, patch

        (tmp_path / "server.js").write_text("app.listen(3000);")

        # Generate 60 log lines to verify truncation at 50
        logs = [f"log line {i}" for i in range(60)]

        captured_prompt = []

        def fake_call_api(client, messages, system_prompt=None, **kwargs):
            captured_prompt.append(messages[0]["content"])
            mock_resp = MagicMock()
            mock_resp.content = [MagicMock(text="No fixes.")]
            return mock_resp

        with patch("deploy_fix._create_client"), patch("deploy_fix._call_api", side_effect=fake_call_api):
            from deploy_fix import attempt_deploy_fix
            attempt_deploy_fix(build_dir=str(tmp_path), deploy_logs=logs)

        assert captured_prompt, "Claude was not called"
        prompt = captured_prompt[0]
        # Should include log line 59 (last) but approach confirms 50-line window
        assert "log line 59" in prompt
        assert "log line 10" in prompt  # 60-50=10, so line 10 should be included
        # Line 9 is outside the 50-line window (lines 10-59)
        # Note: "log line 9" could match "log line 9" or "log line 90" etc. via substring,
        # so check the specific 50-line header is present
        assert "last 50 lines" in prompt


class TestAdvisorIntegration:
    """Tests for advisor tool integration in deploy_fix.py."""

    def _make_mock_response(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No fixes needed.")]
        mock_metrics = MagicMock()
        return (mock_response, mock_metrics)

    def test_advisor_enabled_switches_model_and_passes_params(self, tmp_path):
        from deploy_fix import attempt_deploy_fix
        (tmp_path / "server.js").write_text("const express = require('express');")

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("fix._shared_call_api", side_effect=capture_call), \
             patch("deploy_fix._create_client"), \
             patch.dict(os.environ, {"USE_ADVISOR_DEPLOY_FIX": "true"}):
            attempt_deploy_fix(str(tmp_path), deploy_logs=["crash on startup"])

        assert "sonnet" in captured.get("model", "")
        assert captured.get("advisor_model") == "claude-opus-4-6"
        assert captured.get("advisor_max_uses") == 2

    def test_advisor_disabled_uses_opus_no_advisor(self, tmp_path):
        from deploy_fix import attempt_deploy_fix
        (tmp_path / "server.js").write_text("const express = require('express');")

        captured = {}
        def capture_call(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("fix._shared_call_api", side_effect=capture_call), \
             patch("deploy_fix._create_client"), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("USE_ADVISOR_DEPLOY_FIX", None)
            attempt_deploy_fix(str(tmp_path), deploy_logs=["crash on startup"])

        assert "opus" in captured.get("model", "")
        assert "advisor_model" not in captured

    def test_deploy_fix_uses_fix_call_api(self):
        """Verify deploy_fix imports _call_api from fix.py (shared dependency)."""
        from deploy_fix import _call_api as deploy_call_api
        from fix import _call_api as fix_call_api
        assert deploy_call_api is fix_call_api


class TestDeployFixCancelChecks:
    """Verify deploy_fix.py raises BuildCancelled at each of the three check points.

    CI regression guard against the 2026-04-11 production incident: a hung
    deploy fix loop burned the entire Opus budget on a build the worker had
    already given up on because the fix-loop layer never observed the
    supervisor's cancel event (the supervisor is described in
    docs/internals/worker.md). The fix is three
    cooperative cancellation polls (iteration_top, post_api, pre_subprocess)
    in attempt_deploy_fix; these tests lock them in so the incident cannot
    recur. deploy_fix imports _call_api from fix.py, so tests must patch
    `fix._shared_call_api` to intercept the outbound API call. The cancel key
    is the `cancel_key` argument, as the worker passes it.
    """

    def setup_method(self):
        from cli import cancellation
        self.build_id = "test-deploy-fix-cancel"
        cancellation.register(self.build_id)

    def teardown_method(self):
        from cli import cancellation
        cancellation.clear(self.build_id)

    def _spec(self):
        return {"app_name": "test-app"}

    def _make_mock_response(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="No fixes needed.")]
        mock_metrics = MagicMock()
        return (mock_response, mock_metrics)

    def test_cancels_at_iteration_top(self, tmp_path):
        """Fire cancel before attempt_deploy_fix runs; _shared_call_api must NOT run."""
        from deploy_fix import attempt_deploy_fix
        from cli import cancellation
        from api_client import BuildCancelled

        (tmp_path / "server.js").write_text("const express = require('express');")

        cancellation.fire(self.build_id)

        api_mock = MagicMock(side_effect=AssertionError(
            "_shared_call_api must not be called after cancel fired at iteration top"
        ))
        with patch("fix._shared_call_api", api_mock), \
             patch("deploy_fix._create_client"):
            with pytest.raises(BuildCancelled) as exc_info:
                attempt_deploy_fix(
                    str(tmp_path),
                    deploy_logs=["crash on startup"],
                    spec=self._spec(),
                    cancel_key=self.build_id,
                )

        assert exc_info.value.build_id == self.build_id
        assert api_mock.called is False

    def test_cancels_after_api_return(self, tmp_path):
        """Cancel fires mid _shared_call_api; post_api guard raises before parse."""
        from deploy_fix import attempt_deploy_fix
        from cli import cancellation
        from api_client import BuildCancelled

        (tmp_path / "server.js").write_text("const express = require('express');")

        def fire_and_return(**kwargs):
            cancellation.fire(self.build_id)
            return self._make_mock_response()

        parse_mock = MagicMock(side_effect=AssertionError(
            "_parse_fix_files must not be called after post_api cancel check"
        ))
        with patch("fix._shared_call_api", side_effect=fire_and_return), \
             patch("deploy_fix._create_client"), \
             patch("deploy_fix._parse_fix_files", parse_mock):
            with pytest.raises(BuildCancelled) as exc_info:
                attempt_deploy_fix(
                    str(tmp_path),
                    deploy_logs=["crash on startup"],
                    spec=self._spec(),
                    cancel_key=self.build_id,
                )

        assert exc_info.value.build_id == self.build_id
        assert parse_mock.called is False

    def test_cancels_before_subprocess(self, tmp_path):
        """API returns clean; cancel fires between parse and file-write.
        pre_subprocess guard must raise before any Path.write_text call."""
        from deploy_fix import attempt_deploy_fix
        from cli import cancellation
        from api_client import BuildCancelled

        (tmp_path / "server.js").write_text("const express = require('express');")

        def parse_and_fire(_response_text):
            cancellation.fire(self.build_id)
            return [("server.js", "const app = require('express')();")]

        with patch("fix._shared_call_api", side_effect=lambda **k: self._make_mock_response()), \
             patch("deploy_fix._create_client"), \
             patch("deploy_fix._parse_fix_files", side_effect=parse_and_fire), \
             patch("pathlib.Path.write_text") as write_mock:
            with pytest.raises(BuildCancelled) as exc_info:
                attempt_deploy_fix(
                    str(tmp_path),
                    deploy_logs=["crash on startup"],
                    spec=self._spec(),
                    cancel_key=self.build_id,
                )

        assert exc_info.value.build_id == self.build_id
        assert write_mock.called is False
