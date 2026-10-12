"""Tests for cli/widgets/__init__.py + cli/widgets/widgets.py + archetype.py.

Section 03 of the B+C pipeline rewrite. Covers:
  - @register() decorator populates WIDGETS
  - render_field() dispatch (strict vs permissive)
  - Per-widget HTML shape guarantees (hidden input for image_url, <select>
    for enum, checkbox for boolean, EJS loop for foreign_key, datetime
    input for timestamp/datetime, etc.)
  - Archetype gate tri-state semantics for WIDGETS_ENABLED
  - Registry import has zero side effects beyond WIDGETS population
"""

from __future__ import annotations

import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import re
import sys

import pytest

from cli.widgets import (
    WIDGETS,
    UnknownWidgetError,
    Widget,
    register,
    render_field,
)
from cli.widgets.archetype import _widgets_enabled_for
from cli.widgets.widgets import (
    BooleanWidget,
    DatetimeWidget,
    EmailWidget,
    EnumWidget,
    FloatWidget,
    ForeignKeyWidget,
    ImageUrlWidget,
    IntegerWidget,
    JsonWidget,
    PasswordWidget,
    StringWidget,
    TextWidget,
    TimestampWidget,
    UrlWidget,
)


# The 14-type set from cli/validate.py::VALID_FIELD_TYPES (added in §02).
ALL_14_TYPES = [
    "text", "string", "integer", "float", "boolean",
    "timestamp", "datetime",
    "email", "password",
    "image_url", "url",
    "enum", "json", "foreign_key",
]


def _minimal_spec(type_name: str, name: str = "my_field") -> dict:
    """Return the smallest spec dict each widget type needs to render."""
    spec: dict = {"name": name, "type": type_name}
    if type_name == "enum":
        spec["choices"] = ["alpha", "beta"]
    elif type_name == "foreign_key":
        spec["options_model"] = "users"
    return spec


# ---------------------------------------------------------------------------
# Registry: decorator + dispatch
# ---------------------------------------------------------------------------


def test_register_text_decorator_populates_widgets_with_text_class():
    assert "text" in WIDGETS
    assert WIDGETS["text"] is TextWidget


def test_render_field_dispatches_to_text_widget_render():
    out = render_field({"name": "title", "type": "text"})
    assert isinstance(out, str)
    assert "title" not in out or "Title" in out  # must emit label, not snake_case
    assert "<label" in out


def test_render_field_dispatches_correctly_for_all_14_types():
    for type_name in ALL_14_TYPES:
        spec = _minimal_spec(type_name)
        out = render_field(spec)
        assert isinstance(out, str), f"{type_name} must return str"
        assert out, f"{type_name} must return non-empty"
        assert WIDGETS[type_name].__name__.lower().startswith(
            type_name.replace("_", "")
        ) or type_name in {"text", "string", "url", "email", "password", "json"}, (
            f"WIDGETS[{type_name!r}] = {WIDGETS[type_name].__name__}"
        )


def test_render_field_strict_raises_unknown_widget_error_on_unregistered_type():
    with pytest.raises(UnknownWidgetError):
        render_field({"name": "x", "type": "unknown_foo"}, strict=True)


def test_unknown_widget_error_message_names_field_type_and_module():
    try:
        render_field({"name": "hero_image", "type": "unknown_foo"}, strict=True)
    except UnknownWidgetError as exc:
        msg = str(exc)
        assert "hero_image" in msg, msg
        assert "unknown_foo" in msg, msg
        assert "cli/widgets/widgets.py" in msg, msg
    else:
        pytest.fail("UnknownWidgetError not raised")


def test_render_field_permissive_returns_html_comment_placeholder():
    out = render_field({"name": "x", "type": "unknown_foo"}, strict=False)
    assert out == "<!-- unknown field: x:unknown_foo -->"


def test_unknown_widget_error_is_valueerror_subclass():
    assert issubclass(UnknownWidgetError, ValueError)


def test_registry_contains_exactly_14_widgets():
    assert len(WIDGETS) == 14, sorted(WIDGETS.keys())
    assert set(WIDGETS.keys()) == set(ALL_14_TYPES)


# ---------------------------------------------------------------------------
# Widget output: labels + per-type shape
# ---------------------------------------------------------------------------


def test_every_widget_emits_plain_english_label_no_snake_case_visible():
    """Iterate all registered widgets; none may emit snake_case label text."""
    snake_in_label = re.compile(r"<label[^>]*>[^<]*[a-z]_[a-z][^<]*</label>")
    for type_name, cls in WIDGETS.items():
        rendered = cls(_minimal_spec(type_name)).render()
        assert snake_in_label.search(rendered) is None, (
            f"{cls.__name__} emitted snake_case label: {rendered!r}"
        )


def test_image_url_widget_emits_upload_zone_div_not_raw_input():
    rendered = ImageUrlWidget(
        {"name": "hero_image_url", "type": "image_url"}
    ).render()
    assert "upload-zone" in rendered, rendered
    # No visible text/url input for the image URL itself (only the hidden
    # backing input is allowed).
    assert re.search(
        r'<input[^>]*\btype="(?:text|url)"[^>]*\bname="hero_image_url"',
        rendered,
    ) is None, rendered


def test_image_url_widget_emits_hidden_input_for_post_processor_compat():
    """_convert_raw_file_inputs_to_upload_zones relies on the hidden input
    so its output on widget HTML is a byte-identical no-op."""
    rendered = ImageUrlWidget(
        {"name": "hero_image_url", "type": "image_url"}
    ).render()
    assert 'type="hidden"' in rendered, rendered
    assert 'name="hero_image_url"' in rendered, rendered


def test_image_url_widget_preserves_current_value_via_value_expr_injection():
    rendered = ImageUrlWidget(
        {"name": "hero_image_url", "type": "image_url"}
    ).render()
    # EJS expression must reference the field so the hidden input renders
    # with the saved value at request time.
    assert "hero_image_url" in rendered
    assert "<%=" in rendered or "<%-" in rendered, rendered


def test_enum_widget_renders_select_with_one_option_per_choice():
    rendered = EnumWidget(
        {"name": "tier", "type": "enum", "choices": ["bronze", "silver", "gold"]}
    ).render()
    assert "<select" in rendered, rendered
    assert "</select>" in rendered, rendered
    # Each choice must appear as its own option (either literal or EJS loop).
    for choice in ("bronze", "silver", "gold"):
        assert choice in rendered, (choice, rendered)


def test_boolean_widget_renders_checkbox_input_with_checked_state():
    rendered = BooleanWidget(
        {"name": "is_active", "type": "boolean"}
    ).render()
    assert 'type="checkbox"' in rendered, rendered
    assert 'name="is_active"' in rendered, rendered
    # Must include an EJS-driven checked attribute so the stored value
    # round-trips into the form.
    assert "checked" in rendered, rendered


def test_foreign_key_widget_renders_ejs_loop_over_options_model():
    rendered = ForeignKeyWidget(
        {"name": "user_id", "type": "foreign_key", "options_model": "users"}
    ).render()
    assert "<select" in rendered, rendered
    # The route handler provides `users` in view locals; the widget must
    # iterate that collection in the EJS template.
    assert "users" in rendered, rendered
    assert "<%" in rendered, rendered


def test_timestamp_widget_renders_datetime_local_input():
    rendered = TimestampWidget(
        {"name": "scheduled_at", "type": "timestamp"}
    ).render()
    assert 'type="datetime-local"' in rendered, rendered
    assert 'name="scheduled_at"' in rendered, rendered


def test_datetime_widget_renders_datetime_local_input():
    rendered = DatetimeWidget(
        {"name": "starts_at", "type": "datetime"}
    ).render()
    assert 'type="datetime-local"' in rendered, rendered


def test_email_widget_renders_email_input():
    rendered = EmailWidget({"name": "contact_email", "type": "email"}).render()
    assert 'type="email"' in rendered, rendered


def test_password_widget_renders_password_input():
    rendered = PasswordWidget({"name": "secret", "type": "password"}).render()
    assert 'type="password"' in rendered, rendered


def test_url_widget_renders_url_input():
    rendered = UrlWidget({"name": "site_url", "type": "url"}).render()
    assert 'type="url"' in rendered, rendered


def test_integer_widget_renders_number_input():
    rendered = IntegerWidget({"name": "count", "type": "integer"}).render()
    assert 'type="number"' in rendered, rendered


def test_float_widget_renders_number_input():
    rendered = FloatWidget({"name": "price", "type": "float"}).render()
    assert 'type="number"' in rendered, rendered


def test_string_widget_renders_text_input():
    rendered = StringWidget({"name": "title", "type": "string"}).render()
    assert 'type="text"' in rendered, rendered


def test_text_widget_renders_textarea_for_long_form():
    rendered = TextWidget({"name": "body", "type": "text"}).render()
    assert "<textarea" in rendered, rendered


def test_json_widget_renders_textarea_or_text_input():
    rendered = JsonWidget({"name": "meta", "type": "json"}).render()
    assert "<textarea" in rendered or 'type="text"' in rendered, rendered


# ---------------------------------------------------------------------------
# Archetype gate
# ---------------------------------------------------------------------------


def test_archetype_gate_enables_widgets_for_medicine_waves_by_default(monkeypatch):
    """MW specs stay on the universal path (widgets enabled)."""
    monkeypatch.delenv("WIDGETS_ENABLED", raising=False)
    assert _widgets_enabled_for({"description": "Medicine Waves wellness"}) is True
    assert _widgets_enabled_for({"app_name": "medicine-waves-v20"}) is True


def test_archetype_gate_enables_widgets_for_non_mw_specs_by_default(monkeypatch):
    """Since the universal-emission change (2026-04-20), non-MW specs also
    trigger widget emission so admin_settings + admin_crud contracts pass.
    Previously returned False; change log is in archetype.py docstring."""
    monkeypatch.delenv("WIDGETS_ENABLED", raising=False)
    assert _widgets_enabled_for({"description": "bookstore"}) is True
    assert _widgets_enabled_for({"app_name": "small-business-invoicer"}) is True
    # Empty or sparse specs also enable widgets; emit_admin_settings uses
    # _has() gates so empty SECTION_LAYOUT sections do not render, and the
    # CRUD loop no-ops when data_models is absent. Safe default.
    assert _widgets_enabled_for({}) is True
    assert _widgets_enabled_for({"description": None}) is True
    assert _widgets_enabled_for({"app_name": None}) is True


def test_archetype_gate_returns_true_for_any_spec_when_env_force_on(monkeypatch):
    monkeypatch.setenv("WIDGETS_ENABLED", "true")
    assert _widgets_enabled_for({"description": "bookstore"}) is True


def test_archetype_gate_returns_false_for_any_spec_when_env_force_off(monkeypatch):
    """WIDGETS_ENABLED=false remains the emergency kill switch. Forces the
    pre-universalization behavior (legacy Claude admin files only)."""
    monkeypatch.setenv("WIDGETS_ENABLED", "false")
    assert _widgets_enabled_for({"description": "Medicine Waves"}) is False
    assert _widgets_enabled_for({"description": "bookstore"}) is False


@pytest.mark.parametrize("invalid_value", ["1", "yes", "on", "TRUE", "False", ""])
def test_archetype_gate_raises_value_error_on_invalid_env_value(monkeypatch, invalid_value):
    monkeypatch.setenv("WIDGETS_ENABLED", invalid_value)
    with pytest.raises(ValueError) as excinfo:
        _widgets_enabled_for({"description": "anything"})
    msg = str(excinfo.value)
    assert "WIDGETS_ENABLED" in msg, msg
    assert invalid_value in msg or "expected" in msg.lower(), msg


# ---------------------------------------------------------------------------
# Import side-effect guarantee
# ---------------------------------------------------------------------------


def test_import_cli_widgets_has_no_side_effects_beyond_registry():
    """Re-importing cli.widgets in a fresh subprocess must not print, write
    files, or open network sockets. We assert on stdout/stderr silence as
    the simplest proxy; filesystem writes would show up in process output.
    """
    import subprocess
    result = subprocess.run(
        [sys.executable, "-c", "import cli.widgets"],
        capture_output=True,
        text=True,
        cwd=_repo_root(),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", f"unexpected stdout: {result.stdout!r}"
    assert result.stderr == "", f"unexpected stderr: {result.stderr!r}"


def _repo_root() -> str:
    # tests/ sits at the repo root; walk up until we find cli/widgets.
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)
