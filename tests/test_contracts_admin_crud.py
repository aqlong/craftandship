"""Section-05: admin_crud contract tests (§4.3)."""

from __future__ import annotations

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

from cli.contracts import ContractViolation  # noqa: E402
from cli.contracts import admin_crud as _admin_crud  # noqa: E402
from cli.widgets.forms import emit_all  # noqa: E402


CANONICAL_SPEC_PATH = (
    Path(__file__).parent / "golden" / "medicine-waves-canonical" / "spec.json"
)


def _canonical_spec() -> dict:
    return json.loads(CANONICAL_SPEC_PATH.read_text())


def _widget_artifact() -> types.SimpleNamespace:
    spec = _canonical_spec()
    art = types.SimpleNamespace(
        spec=spec,
        files=[],
        widgets_snippets={},
        metadata={},
        build_dir="/tmp/test",
    )
    emit_all(art)
    return art


def _remove_file(artifact, path: str) -> None:
    artifact.files = [item for item in artifact.files if item[0] != path]


def _replace(artifact, path: str, new_content: str) -> None:
    for i, item in enumerate(artifact.files):
        if item[0] == path:
            artifact.files[i] = (path, new_content)
            return
    artifact.files.append((path, new_content))


def _get(artifact, path: str) -> str:
    for p, c in artifact.files:
        if p == path:
            return c
    raise KeyError(path)


# ---------------------------------------------------------------------------
# Post-universalization regression: non-MW specs must also pass admin_crud.
# This locks in the widget-universalization change made after
# non-MW builds failed with "views/admin/<slug>/new.ejs: file missing".
# ---------------------------------------------------------------------------


def test_admin_crud_passes_on_non_mw_spec_after_universal_widget_emission(monkeypatch):
    """Widget emission on a non-MW spec (no "medicine waves" in description
    and no medicine-waves-* app_name) produces the admin CRUD views for
    every writable data model, so contracts/admin_crud.check() raises no
    violations. Regression guard for the archetype-gate universalization."""
    monkeypatch.delenv("WIDGETS_ENABLED", raising=False)
    spec = {
        "app_name": "small-business-invoicer",
        "description": "invoice management for small businesses",
        "data_models": [
            {"name": "Client", "fields": [
                {"name": "name", "type": "text", "required": True},
                {"name": "email", "type": "email", "required": True},
                {"name": "phone", "type": "text", "required": False},
            ]},
            {"name": "Invoice", "fields": [
                {"name": "title", "type": "text", "required": True},
                {"name": "amount", "type": "integer", "required": True},
                {"name": "due_date", "type": "timestamp", "required": False},
                {"name": "paid", "type": "boolean", "required": False},
            ]},
        ],
    }
    art = types.SimpleNamespace(
        spec=spec, files=[], widgets_snippets={}, metadata={},
        build_dir="/tmp/non-mw-test",
    )
    emit_all(art)
    violations: list[str] = []
    _admin_crud.check(art, collect_all=True, violations=violations)
    assert not violations, (
        "admin_crud contract must pass on widget-emitted non-MW artifact; "
        f"got violations: {violations}"
    )


def test_admin_crud_requires_new_and_edit_for_writable_models():
    """admin_crud.check() requires new.ejs + edit.ejs for every writable model."""
    art = _widget_artifact()
    _remove_file(art, "views/admin/events/new.ejs")
    with pytest.raises(ContractViolation) as exc_info:
        _admin_crud.check(art, collect_all=False)
    assert "views/admin/events/new.ejs" in str(exc_info.value)


def test_admin_crud_forbids_new_edit_for_read_only_models():
    """admin_crud.check() forbids new.ejs / edit.ejs for read-only models."""
    art = _widget_artifact()
    # ContactMessage is read_only=True in canonical spec - slug = contact-messages.
    # Inject a new.ejs that should NOT be there.
    _replace(
        art,
        "views/admin/contact-messages/new.ejs",
        '<form action="/admin/contact-messages" method="POST"><input name="_csrf"></form>',
    )
    with pytest.raises(ContractViolation) as exc_info:
        _admin_crud.check(art, collect_all=False)
    assert "contact-messages" in str(exc_info.value) or "read-only" in str(exc_info.value)


def test_admin_crud_fails_on_orphan_file_input():
    """admin_crud.check() fails on orphan <input type="file" name="*_file"> next to hidden input.

    Orphan = <input type="file" name="image_url"> with NO paired
    <input type="hidden" name="image_url">.
    """
    # Build a synthetic spec where Event.image_url is explicitly type=image_url
    # (canonical MW spec declares it as `text`, in which case the
    # CRUD-layer contract intentionally skips upload-zone enforcement).
    spec = {
        "data_models": [
            {
                "name": "Event",
                "fields": [
                    {"name": "title", "type": "string", "required": True},
                    {"name": "image_url", "type": "image_url", "required": False},
                ],
            },
        ],
    }
    orphan_new = (
        '<form action="/admin/events" method="POST">\n'
        '<input type="hidden" name="_csrf" value="x">\n'
        '<input type="file" name="image_url">\n'  # no paired hidden name="image_url"
        "</form>\n"
    )
    orphan_edit = (
        '<form action="/admin/events/<%= item.id %>" method="POST">\n'
        '<input type="hidden" name="_csrf" value="x">\n'
        '<input type="hidden" name="image_url" value="">\n'
        '<div class="image-upload-zone" data-target="image_url"></div>\n'
        "</form>\n"
    )
    art = types.SimpleNamespace(
        spec=spec,
        files=[
            ("views/admin/events/new.ejs", orphan_new),
            ("views/admin/events/edit.ejs", orphan_edit),
        ],
        widgets_snippets={},
        metadata={},
        build_dir="/tmp/test",
    )
    with pytest.raises(ContractViolation) as exc_info:
        _admin_crud.check(art, collect_all=False)
    assert "events" in str(exc_info.value)


def test_admin_crud_fails_on_drifted_action_url():
    """admin_crud.check() fails when widget-emitted action URL drifts from /admin/<slug>."""
    art = _widget_artifact()
    src = _get(art, "views/admin/events/new.ejs")
    drifted = src.replace(
        'action="/admin/events"',
        'action="/admin/wrong-slug"',
    )
    _replace(art, "views/admin/events/new.ejs", drifted)
    with pytest.raises(ContractViolation):
        _admin_crud.check(art, collect_all=False)


def test_admin_crud_passes_on_widget_canonical_output():
    """admin_crud.check() passes when widget emits literal "/admin/<slug>" action."""
    art = _widget_artifact()
    # Widget output is the canonical shape. Must pass fail-fast.
    _admin_crud.check(art, collect_all=False)


def test_admin_crud_fails_when_csrf_missing():
    """admin_crud.check() fails when CSRF token missing in new.ejs or edit.ejs."""
    art = _widget_artifact()
    src = _get(art, "views/admin/events/edit.ejs")
    tainted = src.replace('name="_csrf"', 'name="OOPS_csrf"')
    _replace(art, "views/admin/events/edit.ejs", tainted)
    with pytest.raises(ContractViolation) as exc_info:
        _admin_crud.check(art, collect_all=False)
    assert "_csrf" in str(exc_info.value)
