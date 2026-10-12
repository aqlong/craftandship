"""Tests for cli/review.py respecting spec `read_only` flag.

Regression guard for v20's false-positive: the design reviewer flagged
`ContactMessage model missing create/update routes` even though the
spec had `read_only: true` on the ContactMessage model. The widget
layer correctly skipped emitting new/edit views; the reviewer's
checklist didn't respect the spec's intent and burned a QA-fix
iteration on a non-issue.

Fix: `_build_review_prompt` must differentiate read-only models and
tell the reviewer NOT to expect create/update/delete for them.
"""
import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

import review  # noqa: E402


def _mw_like_spec_with_read_only_contact_messages() -> dict:
    return {
        "app_name": "medicine-waves-test",
        "description": "Medicine Waves sound journey",
        "pages": [
            {"name": "Home", "path": "/", "auth_required": False},
            {"name": "Contact", "path": "/contact", "auth_required": False},
        ],
        "data_models": [
            {"name": "Event", "fields": [{"name": "title", "type": "text"}]},
            {
                "name": "ContactMessage",
                "fields": [{"name": "message", "type": "text"}],
                "read_only": True,
            },
        ],
        "roles": ["admin", "visitor"],
    }


def _spec_with_access_read_only() -> dict:
    """Same spec but uses `access: 'read-only'` convention (second naming)."""
    spec = _mw_like_spec_with_read_only_contact_messages()
    for m in spec["data_models"]:
        if m["name"] == "ContactMessage":
            m.pop("read_only", None)
            m["access"] = "read-only"
    return spec


# ---------------------------------------------------------------------------
# Prompt-shape tests
# ---------------------------------------------------------------------------


def test_prompt_writable_model_asks_for_full_crud():
    """Sanity: a writable model (no read_only flag) should still be
    included in the CRUD checklist with create/update/delete expected."""
    spec = _mw_like_spec_with_read_only_contact_messages()
    prompt = review._build_review_prompt(spec, source_files=[])
    assert "Event: list, create, read, update, delete" in prompt, (
        "writable Event model lost its full-CRUD expectation"
    )


def test_prompt_read_only_model_does_not_request_create_update_delete():
    """Regression: read_only=True model MUST NOT appear with the literal
    'create, update, delete' CRUD expectation that the v20 reviewer took
    as a missing-route signal."""
    spec = _mw_like_spec_with_read_only_contact_messages()
    prompt = review._build_review_prompt(spec, source_files=[])
    assert "ContactMessage: list, create, read, update, delete" not in prompt, (
        "read_only ContactMessage still described as needing full CRUD; "
        "reviewer will false-positive on missing create/update routes"
    )


def test_prompt_read_only_model_is_explicitly_read_only_documented():
    """The prompt must POSITIVELY tell the reviewer that ContactMessage
    is read-only and new/edit views/routes are EXPECTED TO BE ABSENT."""
    spec = _mw_like_spec_with_read_only_contact_messages()
    prompt = review._build_review_prompt(spec, source_files=[])
    lower = prompt.lower()
    assert "contactmessage" in lower
    assert "read-only" in lower or "read_only" in lower, (
        "prompt does not mention the read-only nature of ContactMessage"
    )


def test_prompt_access_read_only_treated_same_as_read_only_true():
    """Both spec conventions (read_only: True and access: 'read-only')
    must be recognized by the review prompt-builder."""
    spec = _spec_with_access_read_only()
    prompt = review._build_review_prompt(spec, source_files=[])
    assert "ContactMessage: list, create, read, update, delete" not in prompt


def test_prompt_read_only_reminder_positioned_near_model_name():
    """The read-only notice must be co-located with the model name in the
    CRUD checklist region, not buried elsewhere in the prompt. Otherwise
    the reviewer may miss the cross-reference when scanning routes."""
    spec = _mw_like_spec_with_read_only_contact_messages()
    prompt = review._build_review_prompt(spec, source_files=[])
    # Find the ContactMessage line: expect 'read-only' or 'read_only'
    # within ~120 chars of it, so the two appear on the same or adjacent
    # checklist line.
    cm_idx = prompt.find("ContactMessage")
    window = prompt[cm_idx:cm_idx + 200].lower()
    assert "read-only" in window or "read_only" in window, (
        f"read-only annotation not near ContactMessage:\n{window!r}"
    )
