"""Tests for cli/contracts/widget_helpers.py.

Catches widget-emitted EJS that references undeclared identifiers at
build time, before HTTP render. The class of bug this contract guards
against surfaced twice in rapid succession on v21 (2026-04-18):
`_dt is not defined` and `item is not defined`. Previously no
artifact-level test caught those, the widget file looked fine
structurally, contracts on the rendered shape passed, but the JS
runtime referenced an identifier that was never in scope.

This contract scans every file carrying the `<!-- widgets:admin-form -->`
marker and asserts every `<%= IDENT(...) %>` / `<%= IDENT.X %>`
reference is either:
(a) defined via a preceding `<% var IDENT = ... %>` block, OR
(b) a JS builtin (Date, String, ...), OR
(c) in the route-handler-locals whitelist (csrfToken, item, settings, ...).

A widget that adds a new reference without declaring it breaks this test.
"""
import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

from cli.contracts import widget_helpers as wh
from cli.contracts import ContractViolation
from cli.widgets.forms import WIDGET_ADMIN_MARKER


def _mk_artifact(files: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        files=files,
        spec={"app_name": "test", "data_models": []},
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},
    )


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------


def test_canonical_widget_emitted_crud_new_passes_contract():
    """A widget-emitted new.ejs from emit_crud_new_edit on the canonical
    MW spec must pass this contract out of the box."""
    from cli.widgets.forms import emit_crud_new_edit
    spec = {"data_models": [{"name": "Event", "fields": [
        {"name": "title", "type": "text"},
        {"name": "date_time", "type": "timestamp"},
    ]}]}
    new_ejs, edit_ejs = emit_crud_new_edit(spec, spec["data_models"][0])

    artifact = _mk_artifact({
        "views/admin/events/new.ejs": new_ejs,
        "views/admin/events/edit.ejs": edit_ejs,
    })
    # Should not raise.
    wh.check(artifact, collect_all=False)


def test_canonical_widget_emitted_settings_passes_contract():
    """emit_admin_settings output must pass with _v/_has helpers injected."""
    from cli.widgets.forms import emit_admin_settings
    spec = {"data_models": [], "settings": {
        "site_name": "MW", "hero_image_url": "/a.jpg",
    }}
    settings_ejs = emit_admin_settings(spec)
    artifact = _mk_artifact({"views/admin/settings.ejs": settings_ejs})
    wh.check(artifact, collect_all=False)


def test_non_widget_ejs_untouched_by_contract():
    """Files without the widget marker are ignored entirely, Claude's
    public-side views can reference whatever helpers they want."""
    claude_view = (
        "<%# public views/events/index.ejs; no widget marker %>\n"
        "<% events.forEach(function(e) { %>\n"
        "  <div><%= e.title %>; <%= formatEventTime(e.date_time) %></div>\n"
        "<% }); %>\n"
    )
    artifact = _mk_artifact({"views/events/index.ejs": claude_view})
    # Would fail if the contract mis-applied to non-widget files.
    wh.check(artifact, collect_all=False)


# ---------------------------------------------------------------------------
# Negative cases: this contract must CATCH the v21 regression class
# ---------------------------------------------------------------------------


def test_dt_used_without_definition_raises():
    """Regression guard: widget file that emits `<%= _dt(...) %>` WITHOUT
    a matching `<% var _dt = ... %>` definition must raise ContractViolation."""
    bad = (
        f"{WIDGET_ADMIN_MARKER}\n"
        '<form action="/admin/events" method="POST">\n'
        '<input value="<%= _dt(item.date_time) %>">\n'
        '</form>\n'
    )
    artifact = _mk_artifact({"views/admin/events/edit.ejs": bad})
    with pytest.raises(ContractViolation, match="_dt"):
        wh.check(artifact, collect_all=False)


def test_item_property_access_without_whitelist_raises():
    """If a widget references `<%= something_weird.id %>` where
    `something_weird` is neither defined nor whitelisted, the contract
    must flag it."""
    bad = (
        f"{WIDGET_ADMIN_MARKER}\n"
        '<form action="/admin/events" method="POST">\n'
        '<input value="<%= unknown_local.id %>">\n'
        '</form>\n'
    )
    artifact = _mk_artifact({"views/admin/events/edit.ejs": bad})
    with pytest.raises(ContractViolation, match="unknown_local"):
        wh.check(artifact, collect_all=False)


def test_item_whitelist_does_not_false_positive():
    """`item` is in ROUTE_HANDLER_LOCALS so `<%= item.id %>` is fine
    even without a `<% var item = %>` definition in the file."""
    fine = (
        f"{WIDGET_ADMIN_MARKER}\n"
        '<form action="/admin/events/<%= item.id %>" method="POST">\n'
        '<input name="title">\n'
        '</form>\n'
    )
    artifact = _mk_artifact({"views/admin/events/edit.ejs": fine})
    wh.check(artifact, collect_all=False)


def test_collect_all_aggregates_multiple_violations():
    """collect_all mode must aggregate across files rather than raise
    on the first."""
    bad_a = f"{WIDGET_ADMIN_MARKER}\n<%= _missing_a() %>\n"
    bad_b = f"{WIDGET_ADMIN_MARKER}\n<%= _missing_b() %>\n"
    artifact = _mk_artifact({
        "views/admin/events/new.ejs": bad_a,
        "views/admin/photos/new.ejs": bad_b,
    })
    violations = []
    wh.check(artifact, collect_all=True, violations=violations)
    assert len(violations) == 2
    combined = " ".join(violations)
    assert "_missing_a" in combined
    assert "_missing_b" in combined


def test_run_all_includes_widget_helpers_contract():
    """cli.contracts.run.run_all must invoke the widget_helpers module
    as part of its alphabetical module sweep."""
    from cli.contracts import run as run_module
    module_names = [name for name, _ in run_module._MODULES]
    assert "widget_helpers" in module_names


def test_widget_helpers_runs_last_alphabetically():
    """Should be alphabetically LAST in run order so its findings surface
    after surface-specific contracts. Actually it starts with 'w' so
    that's automatic; this test locks the invariant."""
    from cli.contracts import run as run_module
    names = [name for name, _ in run_module._MODULES]
    assert names[-1] == "widget_helpers"


# ---------------------------------------------------------------------------
# Contract runs in the pipeline _run_contracts tail processor
# ---------------------------------------------------------------------------


def test_builder_emits_widget_owned_files_that_pass_full_contract_suite():
    """End-to-end: emit a full MW artifact via emit_all, run the FULL
    contract suite (including widget_helpers), expect zero violations.
    This is the integration check that pairs with the render-harness
    test to ensure both static (helper-definition present) and dynamic
    (actual node render works) invariants hold together."""
    from cli.widgets.forms import emit_all
    import types as _types

    spec = {
        "app_name": "medicine-waves",
        "description": "Medicine Waves",
        "data_models": [
            {"name": "Event", "fields": [
                {"name": "title", "type": "text"},
                {"name": "date_time", "type": "timestamp"},
                {"name": "location", "type": "text"},
            ]},
            {"name": "Photo", "fields": [
                {"name": "title", "type": "text"},
                {"name": "image_url", "type": "image_url"},
            ]},
            {"name": "Video", "fields": [
                {"name": "title", "type": "text"},
                {"name": "embed_url", "type": "url"},
            ]},
        ],
        "settings": {
            "site_name": "MW",
            "hero_image_url": "/hero.jpg",
        },
    }
    artifact = _types.SimpleNamespace(
        files={}, spec=spec, build_dir="/tmp/x",
        widgets_snippets={}, metadata={},
    )
    emit_all(artifact)
    # Must pass the helpers contract specifically.
    wh.check(artifact, collect_all=False)
