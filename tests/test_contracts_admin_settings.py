"""Section-05: admin_settings contract tests (§4.2)."""

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
from cli.contracts import admin_settings as _admin_settings  # noqa: E402
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


def _get(artifact, path: str) -> str:
    for p, c in artifact.files:
        if p == path:
            return c
    raise KeyError(path)


def _replace(artifact, path: str, new_content: str) -> None:
    for i, item in enumerate(artifact.files):
        if item[0] == path:
            artifact.files[i] = (path, new_content)
            return
    artifact.files.append((path, new_content))


def test_admin_settings_check_passes_on_widget_artifact():
    """admin_settings.check() passes on widget-emitted settings.ejs."""
    art = _widget_artifact()
    # Fail-fast mode (collect_all=False) must return without raising.
    _admin_settings.check(art, collect_all=False)


def test_admin_settings_check_fails_when_csrf_missing():
    """admin_settings.check() fails when CSRF token input missing."""
    art = _widget_artifact()
    src = _get(art, "views/admin/settings.ejs")
    broken = src.replace('name="_csrf"', 'name="OOPS_csrf"')
    _replace(art, "views/admin/settings.ejs", broken)
    with pytest.raises(ContractViolation) as exc_info:
        _admin_settings.check(art, collect_all=False)
    assert "_csrf" in str(exc_info.value)


def test_admin_settings_check_fails_on_snake_case_label():
    """admin_settings.check() fails on snake_case <label>hero_image_url</label>."""
    art = _widget_artifact()
    src = _get(art, "views/admin/settings.ejs")
    tainted = src + "\n<label>hero_image_url</label>\n"
    _replace(art, "views/admin/settings.ejs", tainted)
    with pytest.raises(ContractViolation) as exc_info:
        _admin_settings.check(art, collect_all=False)
    assert "snake" in str(exc_info.value).lower() or "hero_image_url" in str(exc_info.value)


def test_admin_settings_check_fails_on_raw_url_input_for_image_field():
    """admin_settings.check() fails on <input type="url" name="hero_image_url">."""
    art = _widget_artifact()
    src = _get(art, "views/admin/settings.ejs")
    tainted = src + '\n<input type="url" name="hero_image_url" value="">\n'
    _replace(art, "views/admin/settings.ejs", tainted)
    with pytest.raises(ContractViolation) as exc_info:
        _admin_settings.check(art, collect_all=False)
    assert "hero_image_url" in str(exc_info.value)


def test_admin_settings_check_fails_when_upload_zone_missing():
    """admin_settings.check() fails when upload-zone missing for any spec image field."""
    art = _widget_artifact()
    src = _get(art, "views/admin/settings.ejs")
    # Widget output uses data-target="..." on .image-upload-zone. Rename
    # the attr across the board so contract selectors no longer match.
    tainted = src.replace('data-target="hero_image_url"', 'data-target="NUKED"')
    tainted = tainted.replace('data-target="about_photo_url"', 'data-target="NUKED2"')
    tainted = tainted.replace('data-target="nav_logo_url"', 'data-target="NUKED3"')
    _replace(art, "views/admin/settings.ejs", tainted)
    with pytest.raises(ContractViolation):
        _admin_settings.check(art, collect_all=False)


def test_admin_settings_check_fails_when_form_action_missing():
    """admin_settings.check() fails when <form action="/admin/settings"> missing."""
    art = _widget_artifact()
    src = _get(art, "views/admin/settings.ejs")
    # Drop the form's action attribute entirely.
    tainted = src.replace(
        '<form action="/admin/settings" method="POST"',
        '<form action="/admin/nonsense" method="POST"',
    )
    _replace(art, "views/admin/settings.ejs", tainted)
    with pytest.raises(ContractViolation):
        _admin_settings.check(art, collect_all=False)


def test_admin_settings_check_fails_when_helpers_missing():
    """admin_settings.check() fails when _v() / _has() helper definitions missing from top of file."""
    art = _widget_artifact()
    src = _get(art, "views/admin/settings.ejs")
    # Drop the helper block by slicing past it (helpers live in the first
    # ~200 chars of widget output).
    # Easiest: rebuild from an arbitrary content without helpers.
    tainted = (
        '<form action="/admin/settings" method="POST">\n'
        '<input type="hidden" name="_csrf" value="x">\n'
        "</form>\n"
    )
    _replace(art, "views/admin/settings.ejs", tainted)
    with pytest.raises(ContractViolation):
        _admin_settings.check(art, collect_all=False)
