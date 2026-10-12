"""Tests for intake.py spec validation before saving."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)

from intake import extract_spec_json
from validate import validate_spec


class TestIntakeSpecValidation:
    def _valid_spec(self):
        return {
            "spec_version": "0.1",
            "app_name": "test-app",
            "description": "A test app",
            "user_roles": [
                {"role": "admin", "description": "Administrator", "can": ["manage_all"]}
            ],
            "pages": [
                {
                    "path": "/",
                    "title": "Home",
                    "purpose": "Landing",
                    "auth_required": False,
                    "allowed_roles": [],
                }
            ],
            "data_models": [
                {
                    "name": "User",
                    "fields": [{"name": "email", "type": "email", "required": True}],
                    "belongs_to": None,
                    "has_many": [],
                }
            ],
            "actions": [
                {
                    "name": "do_thing",
                    "description": "Does a thing",
                    "trigger": "button_click",
                    "role": "admin",
                    "steps": ["step one"],
                }
            ],
            "settings": {"auth": True, "seed_data": True},
        }

    def test_valid_spec_passes_validation(self):
        spec = self._valid_spec()
        errors = validate_spec(spec)
        assert errors == []

    def test_spec_missing_user_roles_fails(self):
        spec = self._valid_spec()
        del spec["user_roles"]
        errors = validate_spec(spec)
        assert any("user_roles" in e for e in errors)

    def test_spec_missing_pages_fails(self):
        spec = self._valid_spec()
        del spec["pages"]
        errors = validate_spec(spec)
        assert any("pages" in e for e in errors)

    def test_spec_missing_app_name_fails(self):
        spec = self._valid_spec()
        del spec["app_name"]
        errors = validate_spec(spec)
        assert any("app_name" in e for e in errors)

    def test_extract_then_validate_valid_spec(self):
        """End-to-end: extract JSON from text, then validate it."""
        import json
        spec = self._valid_spec()
        text = f"Here is the spec:\n```json\n{json.dumps(spec)}\n```"
        extracted = extract_spec_json(text)
        assert extracted is not None
        errors = validate_spec(extracted)
        assert errors == []

    def test_extract_then_validate_invalid_spec(self):
        """End-to-end: extracted spec with missing fields fails validation."""
        text = '```json\n{"spec_version": "0.1", "app_name": "broken"}\n```'
        extracted = extract_spec_json(text)
        assert extracted is not None
        errors = validate_spec(extracted)
        assert len(errors) > 0


# ---------------------------------------------------------------------------
# Section-06 of WATCHDOG1 Layer 2: cli/intake.py messages.create migration
# ---------------------------------------------------------------------------

class TestIntakeApiClientMigration:
    """Verify cli/intake.py call_api_with_retry routes through api_client.call_api
    while preserving the existing string-return contract.
    """

    def test_intake_py_no_direct_messages_create(self):
        """cli/intake.py must contain no AST-level <obj>.messages.create(...) call.

        Docstrings that quote the historical pattern as documentation are
        fine; only actual call expressions are forbidden. The AST walk in
        tests/test_api_client.py::TestNoBypassStatic is the authoritative
        cross-file check; this test scopes the same rule to intake.py so a
        bisect points at the right file.
        """
        import ast
        intake_path = Path(__file__).parent.parent / "cli" / "intake.py"
        tree = ast.parse(intake_path.read_text())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "create":
                continue
            inner = func.value
            if isinstance(inner, ast.Attribute) and inner.attr == "messages":
                offenders.append(node.lineno)
        assert offenders == [], (
            f"cli/intake.py still contains direct .messages.create(...) calls "
            f"at lines {offenders}. Section-06 requires all chat calls go "
            f"through cli.api_client.call_api()."
        )

    def test_call_api_with_retry_delegates_and_returns_text(self, monkeypatch):
        """The wrapper must return response.content[0].text (a string),
        not the raw CombinedResponse. This is the existing intake contract.
        """
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import intake as intake_mod

        fake_response = SimpleNamespace(
            content=[SimpleNamespace(text="hi there", type="text")]
        )
        fake_metrics = SimpleNamespace()
        mock_call_api = MagicMock(return_value=(fake_response, fake_metrics))
        monkeypatch.setattr(intake_mod, "call_api", mock_call_api)

        client = MagicMock(name="anthropic_client")
        messages = [{"role": "user", "content": "I want a pet shop"}]
        result = intake_mod.call_api_with_retry(client, messages)

        assert mock_call_api.call_count == 1
        assert result == "hi there"  # NOT the response object

    def test_conversation_history_forwarded_unchanged(self, monkeypatch):
        """The messages list passed to the wrapper appears verbatim as the
        positional `messages` argument to call_api (deep equality).
        """
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import intake as intake_mod

        fake_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])
        mock_call_api = MagicMock(return_value=(fake_response, SimpleNamespace()))
        monkeypatch.setattr(intake_mod, "call_api", mock_call_api)

        client = MagicMock(name="anthropic_client")
        history = [
            {"role": "user", "content": "I want a yoga app"},
            {"role": "assistant", "content": "What roles do you need?"},
            {"role": "user", "content": "students and instructors"},
        ]
        intake_mod.call_api_with_retry(client, history)

        args, kwargs = mock_call_api.call_args
        assert args[0] is client
        assert args[1] == history  # deep equality, no mutation
        assert args[2] == intake_mod.MODEL
        # System prompt with cache_control + correct max_tokens
        sysparam = kwargs.get("system")
        assert isinstance(sysparam, list) and len(sysparam) == 1
        assert sysparam[0]["text"] == intake_mod.SYSTEM_PROMPT
        assert sysparam[0]["cache_control"] == {"type": "ephemeral"}
        assert kwargs.get("max_tokens") == 4096

    def test_build_id_threaded_from_wrapper_arg(self, monkeypatch):
        """call_api_with_retry now accepts a keyword-only build_id and
        forwards it to call_api. Default None becomes empty string.
        """
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import intake as intake_mod

        fake_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])
        mock_call_api = MagicMock(return_value=(fake_response, SimpleNamespace()))
        monkeypatch.setattr(intake_mod, "call_api", mock_call_api)

        client = MagicMock(name="anthropic_client")
        messages = [{"role": "user", "content": "x"}]

        # With explicit build_id
        intake_mod.call_api_with_retry(client, messages, build_id="b-intake-1")
        _, kwargs = mock_call_api.call_args
        assert kwargs.get("build_id") == "b-intake-1"

        # With default (None) -> coerced to ""
        intake_mod.call_api_with_retry(client, messages)
        _, kwargs = mock_call_api.call_args
        assert kwargs.get("build_id") == ""
