"""Test helper: run cli/intake.py's run_conversation() for one turn.

run_conversation() is an interactive loop, and the design enrichment of a
finished spec sits inside it. This helper drives one pass through the loop
with the model, the keyboard and the disk replaced, and returns the spec that
reaches save_spec(), so a test can look at what the real enrichment did:

    saved = spec_saved_by_a_conversation({"spec_version": "0.1", "design": {}})

By default the real design_data.get_design_context supplies the palette, fonts
and style; pass `design_context` to hand the loop a fixed context, or
`context_error` to make it raise.
"""

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

_CLI_DIR = str(Path(__file__).parent.parent / "cli")
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

_REAL = object()


def spec_saved_by_a_conversation(spec, design_context=_REAL, context_error=None):
    """Return the spec that run_conversation() hands to save_spec() when the
    model's reply carries `spec` (spec_version is added when missing)."""
    import intake

    reply = "Here is your spec:\n```json\n" + json.dumps({"spec_version": "0.1", **spec}) + "\n```"
    saved = []
    with ExitStack() as stack:
        stack.enter_context(patch.object(intake, "create_client", return_value=MagicMock()))
        stack.enter_context(patch.object(intake, "call_api_with_retry", return_value=reply))
        stack.enter_context(patch.object(
            intake, "save_spec", side_effect=lambda s: saved.append(s) or Path("saved.json")))
        stack.enter_context(patch("validate.validate_spec", return_value=[]))
        stack.enter_context(patch("builtins.input", return_value="describe my site"))
        if design_context is not _REAL or context_error is not None:
            stack.enter_context(patch(
                "design_data.get_design_context",
                return_value=None if design_context is _REAL else design_context,
                side_effect=context_error))
        intake.run_conversation()
    assert len(saved) == 1, "run_conversation() should have saved exactly one spec"
    return saved[0]
