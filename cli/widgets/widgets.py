"""Concrete widget subclasses - one per field type in VALID_FIELD_TYPES.

Each class is decorated with @register("<type>") so importing this module
populates cli.widgets.WIDGETS. The four large widgets (image_upload_zone,
enum_select, datetime_input, text_input) load their HTML shells from
sibling .ejs.tmpl files at import time; the rest inline their markup
because a single <input> / <textarea> is shorter in code than on disk.

EJS conventions:
    <%= %> escapes output (runtime user values)
    <%- %> raw output (rarely used here)
    <%  %> control flow
    <%# %> comment

Python string formatting conventions: we use str.format(...) with named
placeholders, so any EJS '{' or '}' in the template file must be doubled
({{ / }}) when the template carries both EJS and format() placeholders.
The templates in cli/templates/widgets/ avoid that collision by keeping
EJS placeholders like <%= foo %> which have no braces.
"""

from __future__ import annotations

import html
from pathlib import Path

from cli.widgets import Widget, register
from cli.widgets.labels import humanize


# ---------------------------------------------------------------------------
# Template loading - cached at module import time, no I/O at render time.
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "widgets"


def _load_template(name: str) -> str:
    return (_TEMPLATES_DIR / name).read_text()


_IMAGE_UPLOAD_ZONE_TMPL = _load_template("image_upload_zone.ejs.tmpl")
_ENUM_SELECT_TMPL = _load_template("enum_select.ejs.tmpl")
_DATETIME_INPUT_TMPL = _load_template("datetime_input.ejs.tmpl")
_TEXT_INPUT_TMPL = _load_template("text_input.ejs.tmpl")


# ---------------------------------------------------------------------------
# Helpers shared across widgets
# ---------------------------------------------------------------------------


def _field_id(name: str) -> str:
    """Stable HTML id derived from the field name."""
    return f"field-{name}"


def _required_attr(spec: dict) -> str:
    return " required" if spec.get("required") else ""


def _value_expr(name: str) -> str:
    """Default EJS expression used to read the current field value.

    `_v(name)` is the runtime helper emitted by section-04's admin settings
    page so every field read goes through a single safe accessor. For CRUD
    new/edit pages section-04 will swap this for `item.<name>` - the widget
    does not need to know which surface it is on.
    """
    return f"_v('{name}')"


# ---------------------------------------------------------------------------
# 14 widget subclasses
# ---------------------------------------------------------------------------


class _InputWidget(Widget):
    """Internal base for simple <input type="X"> widgets."""

    input_type = "text"
    extra_attrs = ""

    def render(self) -> str:
        name = self.spec["name"]
        return _TEXT_INPUT_TMPL.format(
            field_id=_field_id(name),
            field_name=name,
            label=humanize(name),
            input_type=self.input_type,
            value_expr=_value_expr(name),
            required_attr=_required_attr(self.spec),
            extra_attrs=self.extra_attrs,
        )


@register("string")
class StringWidget(_InputWidget):
    input_type = "text"


@register("email")
class EmailWidget(_InputWidget):
    input_type = "email"


@register("password")
class PasswordWidget(_InputWidget):
    input_type = "password"


@register("url")
class UrlWidget(_InputWidget):
    input_type = "url"


@register("integer")
class IntegerWidget(_InputWidget):
    input_type = "number"
    extra_attrs = ' step="1"'


@register("float")
class FloatWidget(_InputWidget):
    input_type = "number"
    extra_attrs = ' step="any"'


_LABEL_CLS = 'class="block text-sm font-medium mb-1"'
_INPUT_CLS = 'class="input-premium w-full"'


@register("text")
class TextWidget(Widget):
    """Long-form text - renders a <textarea>."""

    def render(self) -> str:
        name = self.spec["name"]
        return (
            f'<label for="{_field_id(name)}" {_LABEL_CLS}>{humanize(name)}</label>\n'
            f'<textarea id="{_field_id(name)}" name="{name}" {_INPUT_CLS}'
            f'{_required_attr(self.spec)} rows="4">'
            f"<%= {_value_expr(name)} %>"
            f"</textarea>"
        )


@register("json")
class JsonWidget(Widget):
    """JSON blob - renders a <textarea>. Server must JSON.parse on submit."""

    def render(self) -> str:
        name = self.spec["name"]
        return (
            f'<label for="{_field_id(name)}" {_LABEL_CLS}>{humanize(name)}</label>\n'
            f'<textarea id="{_field_id(name)}" name="{name}" {_INPUT_CLS}'
            f'{_required_attr(self.spec)} rows="6" '
            f'data-widget="json">'
            f"<%= {_value_expr(name)} %>"
            f"</textarea>"
        )


@register("boolean")
class BooleanWidget(Widget):
    def render(self) -> str:
        name = self.spec["name"]
        # `checked` attribute is conditional on the runtime value; we use a
        # standard EJS `<% if %>` block. The <label> wraps the checkbox so
        # the rendered output satisfies both "has <label>" and "no
        # snake_case in label text" contracts.
        return (
            f'<label for="{_field_id(name)}" {_LABEL_CLS}>{humanize(name)}</label>\n'
            f'<input type="checkbox" id="{_field_id(name)}" name="{name}" '
            f'class="h-4 w-4 rounded" '
            f"value=\"true\"<% if ({_value_expr(name)}) {{ %> checked<% }} %>>"
        )


@register("timestamp")
class TimestampWidget(Widget):
    # EJS output wraps the stored value in `_dt(...)` to coerce it to
    # datetime-local's YYYY-MM-DDTHH:MM shape. The helper is injected by
    # emit_* via cli.widgets.helpers.HELPER_SNIPPETS['_dt'] whenever any
    # composed widget declares '_dt' in its required_helpers.
    required_helpers = frozenset({"_dt"})

    def render(self) -> str:
        name = self.spec["name"]
        return _DATETIME_INPUT_TMPL.format(
            field_id=_field_id(name),
            field_name=name,
            label=humanize(name),
            value_expr=f"_dt({_value_expr(name)})",
            required_attr=_required_attr(self.spec),
        )


@register("datetime")
class DatetimeWidget(Widget):
    required_helpers = frozenset({"_dt"})

    def render(self) -> str:
        name = self.spec["name"]
        return _DATETIME_INPUT_TMPL.format(
            field_id=_field_id(name),
            field_name=name,
            label=humanize(name),
            value_expr=f"_dt({_value_expr(name)})",
            required_attr=_required_attr(self.spec),
        )


@register("enum")
class EnumWidget(Widget):
    def render(self) -> str:
        name = self.spec["name"]
        choices = self.spec.get("choices") or []
        # Render options as literal <option> tags so each choice appears in
        # the final HTML byte-for-byte. We also mark the current selection
        # via an inline EJS ternary on each option.
        option_lines = []
        for choice in choices:
            safe = html.escape(str(choice), quote=True)
            option_lines.append(
                f'  <option value="{safe}"'
                f"<% if ({_value_expr(name)} == \"{safe}\") {{ %>"
                f" selected<% }} %>>{safe}</option>"
            )
        choices_ejs = "\n".join(option_lines)
        return _ENUM_SELECT_TMPL.format(
            field_id=_field_id(name),
            field_name=name,
            label=humanize(name),
            required_attr=_required_attr(self.spec),
            choices_ejs=choices_ejs,
        )


@register("foreign_key")
class ForeignKeyWidget(Widget):
    """<select> driven by a collection the route handler puts in view locals.

    Spec must carry `options_model` - the name of the EJS variable that
    will contain the list at render time (e.g. "users" -> the route does
    `res.render(..., { users })`). Each row must have `.id` and `.name` or
    equivalent; section-04 will add an optional `options_label_field` knob
    if a model uses a different display column.
    """

    def render(self) -> str:
        name = self.spec["name"]
        model = self.spec.get("options_model") or "options"
        label_field = self.spec.get("options_label_field", "name")
        # EJS loop over the view-local collection. The selected attribute
        # echoes the currently saved foreign key id.
        return (
            f'<label for="{_field_id(name)}" {_LABEL_CLS}>{humanize(name)}</label>\n'
            f'<select id="{_field_id(name)}" name="{name}" {_INPUT_CLS}'
            f"{_required_attr(self.spec)}>\n"
            f"<% if (typeof {model} !== 'undefined') {{ "
            f"for (const _opt of {model}) {{ %>"
            f'  <option value="<%= _opt.id %>"'
            f"<% if (_opt.id == {_value_expr(name)}) {{ %>"
            f" selected<% }} %>><%= _opt.{label_field} %></option>"
            f"<% }} }} %>\n"
            f"</select>"
        )


@register("image_url")
class ImageUrlWidget(Widget):
    """Drag-drop image upload zone.

    Output matches cli/build.py::_UPLOAD_ZONE_HTML byte-for-byte so the
    existing `_convert_raw_file_inputs_to_upload_zones` and
    `_ensure_upload_form_ui` post-processors become no-ops on widget
    output. In particular the hidden <input type="hidden" name="{name}">
    MUST be present - the downstream processor scans for it to decide
    whether the form is already wired.
    """

    def render(self) -> str:
        name = self.spec["name"]
        field_id = name  # downstream post-processors key off name == id
        # The EJS value expression used inside the preview conditional.
        # _v() returns a string or '', so we can compare it directly.
        value_expr = _value_expr(name)
        return _IMAGE_UPLOAD_ZONE_TMPL.format(
            field_id=field_id,
            field_name=name,
            label=humanize(name),
            value=f"<%= {value_expr} %>",
            value_expr=value_expr,
            required="true" if self.spec.get("required") else "false",
        )
