"""Tests for the widget runtime-requirements contract.

Root-cause fix for the v21 bug class: widget-emitted EJS references
runtime helpers (_dt, _v) and locals (item, settings) that only exist
implicitly. When a widget adds a new reference without declaring it,
runtime crashes with ReferenceError. Previously this class of bug
surfaced only at HTTP render time, after the build + contracts + deploy
had already passed.

Contract (this file):
1. Every Widget subclass declares `required_helpers: frozenset[str]`.
2. Widgets that reference a helper in their EJS output MUST declare it.
3. emit_* functions inject all required helpers at the top of their
   output via the central `cli.widgets.helpers.HELPER_SNIPPETS` registry.
4. The renderable assembled file must, when parsed, have a `<% var X = ...`
   definition earlier in the file for every `<%= X(...) %>` call.
"""
import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

from cli.widgets import WIDGETS  # noqa: E402
from cli.widgets.widgets import (  # noqa: E402
    DatetimeWidget, TimestampWidget, EnumWidget, BooleanWidget,
    ForeignKeyWidget, ImageUrlWidget,
)


# ---------------------------------------------------------------------------
# Phase 1: every widget declares required_helpers
# ---------------------------------------------------------------------------


def test_every_registered_widget_declares_required_helpers_attr():
    """Every Widget subclass must declare a `required_helpers` class
    attribute (empty frozenset is fine). This is the contract point that
    lets emit_* compose helper definitions deterministically."""
    for type_name, cls in WIDGETS.items():
        assert hasattr(cls, "required_helpers"), (
            f"{type_name} Widget class missing required_helpers attribute"
        )
        assert isinstance(cls.required_helpers, frozenset), (
            f"{type_name}.required_helpers must be frozenset, "
            f"got {type(cls.required_helpers).__name__}"
        )


def test_datetime_and_timestamp_widgets_declare_dt_helper():
    """DatetimeWidget and TimestampWidget emit `<%= _dt(...) %>` so both
    must declare '_dt' in required_helpers."""
    assert "_dt" in DatetimeWidget.required_helpers
    assert "_dt" in TimestampWidget.required_helpers


def test_input_widgets_have_empty_or_minimal_requirements():
    """text/string/email/url/integer/float/password widgets emit plain
    <input value="<%= '' %>"> with no helper references. required_helpers
    should be empty (or at most include generic EJS locals)."""
    from cli.widgets.widgets import _InputWidget
    # Base class default
    assert _InputWidget.required_helpers == frozenset()


# ---------------------------------------------------------------------------
# Phase 2: helper registry
# ---------------------------------------------------------------------------


def test_helper_registry_exports_dt_snippet():
    """cli.widgets.helpers.HELPER_SNIPPETS['_dt'] returns a self-contained
    EJS block that defines _dt as a JS function."""
    from cli.widgets.helpers import HELPER_SNIPPETS
    assert "_dt" in HELPER_SNIPPETS
    snippet = HELPER_SNIPPETS["_dt"]
    assert "var _dt = function" in snippet
    assert snippet.strip().startswith("<%")
    assert snippet.strip().endswith("%>")


def test_helper_registry_exports_settings_accessors():
    """admin/settings.ejs needs _v (value) + _has (presence) accessors
    scoped to the `settings` view local."""
    from cli.widgets.helpers import HELPER_SNIPPETS
    assert "_v_settings" in HELPER_SNIPPETS
    assert "_has_settings" in HELPER_SNIPPETS
    assert "function(k)" in HELPER_SNIPPETS["_v_settings"]
    assert "function(k)" in HELPER_SNIPPETS["_has_settings"]


def test_render_helpers_returns_deterministic_sorted_output():
    """render_helpers(names) must concat snippets in alphabetical order
    of their names so the output is deterministic across runs."""
    from cli.widgets.helpers import render_helpers
    out1 = render_helpers({"_dt", "_v_settings", "_has_settings"})
    out2 = render_helpers({"_v_settings", "_dt", "_has_settings"})
    assert out1 == out2
    # _dt comes before _has_settings alphabetically
    dt_pos = out1.find("var _dt")
    has_pos = out1.find("var _has")
    assert 0 <= dt_pos < has_pos


def test_render_helpers_skips_unknown_names_gracefully():
    """Unknown helper names are skipped, not raised. This lets widgets
    declare forward-looking requirements without breaking emit today."""
    from cli.widgets.helpers import render_helpers
    out = render_helpers({"_dt", "_totally_unknown"})
    assert "var _dt" in out
    assert "_totally_unknown" not in out


# ---------------------------------------------------------------------------
# Phase 2: emit_* composes helpers from widget requirements
# ---------------------------------------------------------------------------


def _mw_spec():
    return {
        "app_name": "medicine-waves-test",
        "description": "MW test",
        "data_models": [
            {"name": "Event", "fields": [
                {"name": "title", "type": "text"},
                {"name": "date_time", "type": "timestamp"},
            ]},
            {"name": "Photo", "fields": [
                {"name": "title", "type": "text"},
                {"name": "image_url", "type": "image_url"},
            ]},
            {"name": "ContactMessage", "fields": [
                {"name": "message", "type": "text"},
            ], "read_only": True},
        ],
        "settings": {
            "hero_tagline": "test",
            "hero_image_url": "/img.jpg",
        },
    }


def test_emit_crud_new_edit_injects_dt_helper_when_datetime_field_present():
    """A model with a timestamp/datetime field should trigger _dt
    helper injection. A model without one should NOT inject _dt."""
    from cli.widgets.forms import emit_crud_new_edit

    spec = _mw_spec()
    event_model = next(m for m in spec["data_models"] if m["name"] == "Event")
    photo_model = next(m for m in spec["data_models"] if m["name"] == "Photo")

    event_new, event_edit = emit_crud_new_edit(spec, event_model)
    photo_new, photo_edit = emit_crud_new_edit(spec, photo_model)

    # Event has date_time → _dt injected
    for f in (event_new, event_edit):
        assert "var _dt = function" in f, "Event CRUD must inject _dt"

    # Photo has no datetime → _dt NOT needed (though may be injected defensively)
    # Don't strictly require absence; require at minimum that if _dt usage
    # exists, _dt definition also exists. Photo's output should have no
    # _dt(...) call site either.
    assert "_dt(" not in photo_new or "var _dt = function" in photo_new
    assert "_dt(" not in photo_edit or "var _dt = function" in photo_edit


def test_emit_admin_settings_injects_v_and_has_helpers():
    """admin/settings.ejs MUST define _v and _has helpers. Widget fields
    in that surface reference `_v(key)` and the section-gate uses `_has(key)`."""
    from cli.widgets.forms import emit_admin_settings
    spec = _mw_spec()
    rendered = emit_admin_settings(spec)
    assert "var _has = function" in rendered
    assert "var _v = function" in rendered


def test_every_helper_use_has_a_preceding_definition_in_same_file():
    """For every emit_* output, any `<% var X = ... %>` helper that a
    later `<%= X(...) %>` references must be defined BEFORE the first
    use site. This makes the EJS runnable without ReferenceError."""
    from cli.widgets.forms import emit_admin_settings, emit_crud_new_edit
    spec = _mw_spec()

    files = {"settings.ejs": emit_admin_settings(spec)}
    for m in spec["data_models"]:
        if m.get("read_only"):
            continue
        result = emit_crud_new_edit(spec, m)
        if result is None:
            continue
        new_ejs, edit_ejs = result
        slug = m["name"].lower()
        files[f"{slug}/new.ejs"] = new_ejs
        files[f"{slug}/edit.ejs"] = edit_ejs

    def_re = re.compile(r"<%\s*var\s+([a-zA-Z_][a-zA-Z_0-9]*)\s*=")
    # Match helper-style calls: `<%= IDENT(` or `<%- IDENT(`
    use_re = re.compile(r"<%[=-]\s*(?:\([^)]*\?\s*)?([a-zA-Z_][a-zA-Z_0-9]*)\s*\(")

    JS_BUILTINS = {"String", "Number", "Date", "Math", "JSON", "Array", "Object"}

    for path, content in files.items():
        # Find all definitions with their offsets
        def_positions = {m.group(1): m.start() for m in def_re.finditer(content)}
        # Find all use sites with their offsets
        for use_match in use_re.finditer(content):
            ident = use_match.group(1)
            if ident in JS_BUILTINS:
                continue
            if ident in def_positions:
                assert def_positions[ident] < use_match.start(), (
                    f"{path}: helper {ident} used at offset {use_match.start()} "
                    f"but defined at offset {def_positions[ident]} (must precede)"
                )
            else:
                pytest.fail(
                    f"{path}: helper {ident}(...) used at offset "
                    f"{use_match.start()} but never defined in the file"
                )
