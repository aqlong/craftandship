"""Regression-injection tests for the contract safety-net.

Section 06 of the B+C pipeline rewrite (section 5 of
docs/plans/b-plus-c/claude-plan.md; see "Plan section labels" in cli/README.md).
Each test manufactures a known past
regression in a green widget-emitted artifact and asserts that the
contract layer catches it. This is the "regressions get reported before
they ship" proof.

Regression origins (one test per):

  1. v19 inline <form action="/admin/settings"> pasted into dashboard.ejs
     -> admin_dashboard.check() fails.
  2. v14 timezone UTC-rendered-as-CDT (date suffix 'CDT' on a UTC ISO
     timestamp) anywhere in event views -> contract or contains() fires.
  3. v13 admin-settings-paths-disconnected: a settings field's name
     attribute mutated to "wrong_key" so the widget-emitted drag-drop
     zone no longer matches any settings key -> admin_settings.check()
     fails (upload-zone[data-target="X"] missing).
  4. Snake-case <label>hero_image_url</label> text in settings.ejs
     -> admin_settings.check() fails (snake_case label detector).
  5. Raw <input type="url" name="hero_image_url"> replacing the drag-drop
     zone -> admin_settings.check() fails (raw-input-for-image-field
     forbidden).
"""

from __future__ import annotations

import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import re
import sys
import types
from pathlib import Path

import pytest

# Allow importing from cli/ without installing a package.
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

from cli.contracts import ContractViolation  # noqa: E402
from cli.contracts.run import run_all  # noqa: E402
from cli.widgets.forms import emit_all  # noqa: E402


GOLDEN_DIR = Path(__file__).parent / "golden" / "medicine-waves-canonical"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _canonical_spec() -> dict:
    return json.loads((GOLDEN_DIR / "spec.json").read_text())


def _v19_files() -> dict[str, str]:
    return dict(json.loads((FIXTURES_DIR / "claude_mw_v19_raw.json").read_text())["files"])


def _base_artifact() -> types.SimpleNamespace:
    """Build a widgets-ON artifact on the canonical MW spec.

    Baseline is widget-emitted admin files + v19 non-admin files. Then
    one regression is injected per test before run_all is called.
    """
    spec = _canonical_spec()
    # Start from v19 recorded files to populate non-admin surfaces.
    files = _v19_files()
    artifact = types.SimpleNamespace(
        spec=spec,
        files=list(files.items()),
        widgets_snippets={},
        metadata={},
        build_dir="/tmp/regression-inject",
    )
    emit_all(artifact)

    # Synthesize a clean admin dashboard that carries the widget snippet.
    snippet = artifact.widgets_snippets.get("dashboard_settings_section", "")
    file_dict = dict(artifact.files)
    file_dict["views/admin/dashboard.ejs"] = (
        "<h1>Admin Dashboard</h1>\n"
        "<!-- widgets:dashboard-settings -->\n"
        f"{snippet}\n"
        "<!-- /widgets:dashboard-settings -->\n"
    )
    artifact.files = list(file_dict.items())
    return artifact


def _mutate_file(artifact, path: str, new_content: str) -> None:
    """Rewrite one file in place inside artifact.files."""
    files = list(artifact.files)
    for i, (p, _c) in enumerate(files):
        if p == path:
            files[i] = (path, new_content)
            artifact.files = files
            return
    # Missing: append.
    files.append((path, new_content))
    artifact.files = files


def _get_file(artifact, path: str) -> str:
    for p, c in artifact.files:
        if p == path:
            return c
    raise KeyError(path)


# ---------------------------------------------------------------------------
# Regression 1: v19 inline settings <form> on dashboard.
# ---------------------------------------------------------------------------


def test_injecting_v19_inline_settings_form_on_dashboard_trips_admin_dashboard_contract(monkeypatch):
    """admin_dashboard.check must fail when dashboard has an inline
    <form action="/admin/settings">.

    v19 regression: views/admin/dashboard.ejs shipped with an inline
    settings form instead of a CTA link. The admin_dashboard contract
    asserts absence of that form, so the injection must trip it.
    """
    monkeypatch.setenv("WIDGETS_ENABLED", "true")
    monkeypatch.setenv("CAS_CONTRACTS_COLLECT", "1")
    artifact = _base_artifact()

    # Inject: dashboard wraps the widget snippet AND also contains an
    # inline settings form (v19 shape).
    dashboard = _get_file(artifact, "views/admin/dashboard.ejs")
    dashboard_mutated = dashboard + (
        '\n<form action="/admin/settings" method="POST">\n'
        '  <input type="hidden" name="_csrf" value="<%= csrfToken %>">\n'
        '  <label>Site Name</label>\n'
        '  <input type="text" name="site_name">\n'
        '  <button type="submit">Save</button>\n'
        "</form>\n"
    )
    _mutate_file(artifact, "views/admin/dashboard.ejs", dashboard_mutated)

    with pytest.raises(ContractViolation) as exc_info:
        run_all(artifact, collect_all=True)
    assert 'form[action="/admin/settings"]' in str(exc_info.value) or \
        "views/admin/dashboard.ejs" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Regression 2: v14 timezone UTC-rendered-as-CDT.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "Known gap: no per-view contract in cli/contracts/ checks timezone "
        "display. The v14 regression (a UTC timestamp with a hardcoded 'CDT' "
        "suffix) is caught at build time by the _ensure_event_time_helper "
        "post-processor, not by a contract. This test documents that the "
        "contract layer alone does not catch it; it will start to pass "
        "(strict xfail, so the run fails and the marker must be removed) once "
        "an event-time contract exists."
    ),
    strict=True,
)
def test_injecting_v14_utc_as_cdt_timezone_pattern_trips_a_contract(monkeypatch):
    """A contract must fail when a view hardcodes 'CDT' suffix on a
    UTC ISO timestamp.

    v14 regression: events viewed via the home-page/list-page rendered
    date_time without explicit timezone conversion, producing a UTC
    timestamp with 'CDT' suffix that was semantically wrong for CDT.
    """
    monkeypatch.setenv("WIDGETS_ENABLED", "true")
    monkeypatch.setenv("CAS_CONTRACTS_COLLECT", "1")
    artifact = _base_artifact()

    home = _get_file(artifact, "views/home.ejs")
    home_mutated = home + (
        '\n<p class="event-time">'
        "2026-05-13T00:00:00Z (CDT)"  # UTC stamp mis-labeled as CDT
        "</p>\n"
    )
    _mutate_file(artifact, "views/home.ejs", home_mutated)

    with pytest.raises(ContractViolation):
        run_all(artifact, collect_all=True)


# ---------------------------------------------------------------------------
# Regression 3: v13 admin-settings-paths-disconnected.
# ---------------------------------------------------------------------------


def test_injecting_v13_disconnected_settings_field_name_trips_admin_settings_contract(monkeypatch):
    """admin_settings.check must fail when a settings field name does
    NOT match any settings key.

    v13 regression: admin settings form fields used generic names
    (business_name etc.) disconnected from the spec settings keys. The
    contract asserts .image-upload-zone[data-target="<key>"] for every
    image-typed settings key; mutating one zone's data-target to a
    nonsense key breaks the assertion.
    """
    monkeypatch.setenv("WIDGETS_ENABLED", "true")
    monkeypatch.setenv("CAS_CONTRACTS_COLLECT", "1")
    artifact = _base_artifact()

    settings_ejs = _get_file(artifact, "views/admin/settings.ejs")
    # Pick the hero_image_url field's upload zone and rename its
    # data-target to "wrong_key". The field must be present in the
    # emitted settings file; assert so the test fails loudly if the
    # widget output shape shifts.
    assert 'data-target="hero_image_url"' in settings_ejs, (
        "precondition: widget-emitted settings.ejs must carry "
        "hero_image_url upload zone"
    )
    # Also swap the paired hidden input name to "wrong_key" so the
    # admin_settings image-field contract keys on the spec's
    # "hero_image_url" and does not find the zone.
    settings_mutated = settings_ejs.replace(
        'data-target="hero_image_url"', 'data-target="wrong_key"', 1,
    ).replace(
        'name="hero_image_url"', 'name="wrong_key"', 1,
    )
    _mutate_file(artifact, "views/admin/settings.ejs", settings_mutated)

    with pytest.raises(ContractViolation) as exc_info:
        run_all(artifact, collect_all=True)
    msg = str(exc_info.value)
    assert "hero_image_url" in msg or "views/admin/settings.ejs" in msg


# ---------------------------------------------------------------------------
# Regression 4: snake_case <label> text.
# ---------------------------------------------------------------------------


def test_injecting_snake_case_label_text_trips_admin_settings_contract(monkeypatch):
    """admin_settings.check must fail when a <label> text is snake_case
    (e.g. hero_image_url instead of "Hero Image URL")."""
    monkeypatch.setenv("WIDGETS_ENABLED", "true")
    monkeypatch.setenv("CAS_CONTRACTS_COLLECT", "1")
    artifact = _base_artifact()

    settings_ejs = _get_file(artifact, "views/admin/settings.ejs")
    # Insert a snake_case <label>.
    snake_label_block = (
        '<label>hero_image_url</label>\n'
        '<input type="text" name="hero_image_url">\n'
    )
    # Place it after the form opening so the contract scanner finds it.
    insert_point = settings_ejs.find("<form")
    assert insert_point >= 0
    settings_mutated = (
        settings_ejs[:insert_point]
        + snake_label_block
        + settings_ejs[insert_point:]
    )
    _mutate_file(artifact, "views/admin/settings.ejs", settings_mutated)

    with pytest.raises(ContractViolation) as exc_info:
        run_all(artifact, collect_all=True)
    msg = str(exc_info.value)
    assert "snake_case" in msg or "hero_image_url" in msg


# ---------------------------------------------------------------------------
# Regression 5: raw <input type="url"> for image field.
# ---------------------------------------------------------------------------


def test_injecting_raw_url_input_for_image_field_trips_admin_settings_contract(monkeypatch):
    """admin_settings.check must fail when an image-typed settings key
    has a raw <input type="url"> instead of a drag-drop upload zone.

    Widget emits the drag-drop zone; this test replaces it with a raw
    type=url input to simulate the recurring pre-widget regression.
    """
    monkeypatch.setenv("WIDGETS_ENABLED", "true")
    monkeypatch.setenv("CAS_CONTRACTS_COLLECT", "1")
    artifact = _base_artifact()

    settings_ejs = _get_file(artifact, "views/admin/settings.ejs")
    # Strip the widget-emitted hero_image_url upload zone and replace with
    # a naked URL input. The zone block is wrapped between the
    # <% if (_has("hero_image_url")) { %> and <% } %> markers.
    zone_re = re.compile(
        r'<% if \(_has\("hero_image_url"\)\) \{ %>.*?<% \} %>',
        re.DOTALL,
    )
    assert zone_re.search(settings_ejs), (
        "precondition: widget-emitted settings.ejs must carry the "
        "hero_image_url _has() gated block"
    )
    settings_mutated = zone_re.sub(
        '<label>Hero Image</label>\n'
        '<input type="url" name="hero_image_url">\n',
        settings_ejs,
        count=1,
    )
    _mutate_file(artifact, "views/admin/settings.ejs", settings_mutated)

    with pytest.raises(ContractViolation) as exc_info:
        run_all(artifact, collect_all=True)
    msg = str(exc_info.value)
    assert "hero_image_url" in msg or "views/admin/settings.ejs" in msg
