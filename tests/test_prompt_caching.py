"""What four v1 stages send to the model as their system prompt.

Each of review.py, qa.py, fix.py and qa_fix.py wraps the shared API client.
These tests call the wrapper with a stand-in client and check what reaches
it: the stage's own instructions as the system text, the caller's messages
unchanged, and, where the wrapper takes one, a caller-supplied prompt in
place of the default.

The file's name is historical. Every one of these system blocks also carries
a `cache_control` marker, and this file used to assert that the marker was
there. docs/audits/prompt-caching-2026-08-06.md measured the four prompts
against the model each stage runs on and found all of them below the minimum
cacheable prefix: the marker is accepted by the API and caches nothing. A test
that pins it pins a setting with no effect, so nothing here asserts on it.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))


def _stand_in_client(text="OK"):
    """An Anthropic client whose messages.create returns one text block."""
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    response.stop_reason = "end_turn"
    response.usage = MagicMock(
        input_tokens=100, output_tokens=50,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    client = MagicMock()
    client.messages.create.return_value = response
    return client


def _sent(client) -> dict:
    """The keyword arguments of the one request the wrapper made."""
    client.messages.create.assert_called_once()
    return client.messages.create.call_args.kwargs


def _system_text(client) -> str:
    system = _sent(client)["system"]
    assert isinstance(system, list) and len(system) == 1 and system[0]["type"] == "text"
    return system[0]["text"]


class TestReview:
    @patch("review._create_client")
    def test_the_review_instructions_and_the_source_reach_the_model(self, create_client, tmp_path):
        import review
        client = _stand_in_client(
            '{"passed": true, "critical_count": 0, "warning_count": 0, "summary": "ok", "issues": []}')
        create_client.return_value = client
        (tmp_path / "server.js").write_text("const express = require('express'); // marker-7f3a")

        result = review.review_build(str(tmp_path), {"pages": [], "data_models": []})

        assert _system_text(client) == review.REVIEW_SYSTEM_PROMPT
        messages = _sent(client)["messages"]
        assert [m["role"] for m in messages] == ["user"]
        assert "marker-7f3a" in messages[0]["content"], "the build's source is what gets reviewed"
        assert result["passed"] is True and result["critical_count"] == 0


class TestQa:
    def test_the_qa_instructions_reach_the_model_with_the_callers_messages(self):
        import qa
        client = _stand_in_client("def run_qa_tests(page, base_url): pass")
        messages = [{"role": "user", "content": "test"}]

        qa._call_api(client, messages)

        assert _system_text(client) == qa.QA_SYSTEM_PROMPT
        assert _sent(client)["messages"] == messages


class TestFix:
    def test_the_fix_instructions_are_the_default_system_prompt(self):
        import fix
        client = _stand_in_client("no changes needed")
        messages = [{"role": "user", "content": "fix this"}]

        fix._call_api(client, messages)

        assert _system_text(client) == fix.FIX_SYSTEM_PROMPT
        assert _sent(client)["messages"] == messages

    def test_a_callers_system_prompt_replaces_the_default(self):
        """deploy_fix.py and the advisor path hand fix._call_api their own prompt."""
        import fix
        client = _stand_in_client("fixed")

        fix._call_api(client, [{"role": "user", "content": "fix"}],
                      system_prompt="Custom system prompt for deploy fix")

        assert _system_text(client) == "Custom system prompt for deploy fix"


class TestQaFix:
    def test_the_qa_fix_instructions_are_the_default_system_prompt(self):
        import qa_fix
        client = _stand_in_client("no fix needed")

        qa_fix._call_api(client, [{"role": "user", "content": "fix qa"}])

        assert _system_text(client) == qa_fix.QA_FIX_SYSTEM_PROMPT

    def test_a_callers_system_prompt_replaces_the_default(self):
        import qa_fix
        client = _stand_in_client("no fix needed")

        qa_fix._call_api(client, [{"role": "user", "content": "fix qa"}],
                         system_prompt="Advisor-wrapped instructions")

        assert _system_text(client) == "Advisor-wrapped instructions"
