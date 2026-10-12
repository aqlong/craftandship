"""Tests for diagnose.py _escalate() email integration.

All Resend API calls are mocked; no real emails are sent.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests as requests_module

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Pre-import to ensure module is loaded before patching
import monitor.diagnose as _diagnose_module


class TestEscalateEmail:
    def setup_method(self):
        # Reset escalation cooldown state before each test so tests are independent.
        _diagnose_module._last_escalated.clear()

    def _make_app(self):
        return {
            "app_name": "test-app",
            "live_url": "https://test-app.up.railway.app",
        }

    def test_sends_email_when_api_key_set(self):
        from monitor.diagnose import _escalate

        mock_response = MagicMock()
        mock_response.ok = True

        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key", "ADMIN_EMAIL": "admin@test.example"}):
            with patch.object(_diagnose_module, "provider_post", return_value=mock_response) as mock_post:
                _escalate(
                    self._make_app(),
                    {"diagnosis": "service crashed"},
                    {"fix_applied": False},
                )

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs[0][1]
        assert payload["to"] == ["admin@test.example"]
        assert "test-app" in payload["subject"]
        assert "test-app" in payload["html"]

    def test_skips_email_when_no_api_key(self):
        from monitor.diagnose import _escalate

        env = os.environ.copy()
        env.pop("RESEND_API_KEY", None)

        with patch.dict(os.environ, env, clear=True):
            with patch.object(_diagnose_module, "provider_post") as mock_post:
                _escalate(self._make_app(), {}, None)

        mock_post.assert_not_called()

    def test_handles_resend_api_error_gracefully(self):
        from monitor.diagnose import _escalate

        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key", "ADMIN_EMAIL": "admin@test.example"}):
            with patch.object(_diagnose_module, "provider_post", return_value=mock_response):
                # Should not raise
                _escalate(self._make_app(), {}, None)

    def test_handles_network_error_gracefully(self):
        from monitor.diagnose import _escalate

        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key"}):
            with patch.object(_diagnose_module, "provider_post", side_effect=requests_module.exceptions.ConnectionError("refused")):
                # Should not raise
                _escalate(self._make_app(), {}, None)

    def test_email_contains_tier3_and_tier4_results(self):
        from monitor.diagnose import _escalate

        mock_response = MagicMock()
        mock_response.ok = True

        tier3 = {"diagnosis": "OOM kill", "fixable": False}
        tier4 = {"fix_applied": False, "escalate": True}

        with patch.dict(os.environ, {"RESEND_API_KEY": "re_test_key", "ADMIN_EMAIL": "admin@test.example"}):
            with patch.object(_diagnose_module, "provider_post", return_value=mock_response) as mock_post:
                _escalate(self._make_app(), tier3, tier4)

        payload = mock_post.call_args[1]["json"]
        assert "OOM kill" in payload["html"]
        assert "escalate" in payload["html"]

    def test_skips_email_when_no_admin_email(self):
        """ADMIN_EMAIL has no default: with it unset nothing is sent anywhere."""
        from monitor.diagnose import _escalate

        env = os.environ.copy()
        env.pop("ADMIN_EMAIL", None)
        env["RESEND_API_KEY"] = "re_test_key"

        with patch.dict(os.environ, env, clear=True):
            with patch.object(_diagnose_module, "provider_post") as mock_post:
                _escalate(self._make_app(), {}, None)

        mock_post.assert_not_called()
