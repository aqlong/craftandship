"""Widget registry - schema-driven admin form renderer.

Importing this package registers all 14 widgets (text/string/integer/float/
boolean/timestamp/datetime/email/password/image_url/url/enum/json/
foreign_key) in the `WIDGETS` dict. Section 04 will compose them into full
admin surfaces (`cli/widgets/forms.py`). Section 05 will assert on widget
output via BS4 contracts.

Public API:
    register(type_name)     - class decorator populating WIDGETS
    render_field(spec, *, strict=True)
                            - dispatch: WIDGETS[spec['type']](spec).render()
    WIDGETS                 - dict[str, type[Widget]]
    Widget                  - base class (subclass per field type)
    UnknownWidgetError      - ValueError subclass raised by strict dispatch

Import side effects are limited to WIDGETS population plus the four
.ejs.tmpl files loaded at module scope in cli.widgets.widgets. No network,
no stdout/stderr, no filesystem writes.
"""

from __future__ import annotations

from collections.abc import Callable


class Widget:
    """Base class for every field-type widget.

    Subclasses override .render() to return EJS-ready HTML. The constructor
    stores the raw field spec (a dict like
    {"name":..., "type":..., "choices":[...], "required":..., ...}) unchanged
    so subclasses can read any optional key they need.

    Runtime-requirements manifest (new 2026-04-18):
        required_helpers : frozenset[str]
            Identifiers this widget's emitted EJS references that must
            be defined by a preceding `<% var X = function(...) %>` block
            in the same file. The emit_* functions in cli/widgets/forms
            consult this attribute on each composed widget to compute
            the union of helpers to inject (via
            cli/widgets/helpers.HELPER_SNIPPETS). A widget that adds a
            new reference WITHOUT declaring it here will trigger the
            contract check in cli/contracts/widget_helpers.check.
    """

    required_helpers: frozenset = frozenset()

    def __init__(self, spec: dict) -> None:
        self.spec = spec

    def render(self) -> str:
        raise NotImplementedError(
            f"{type(self).__name__} must implement .render()"
        )


class UnknownWidgetError(ValueError):
    """Raised by render_field() in strict mode when spec['type'] is not
    registered. Message names the field, the type, and points readers at
    cli/widgets/widgets.py where new widgets must be declared.
    """


WIDGETS: dict[str, type[Widget]] = {}


def register(type_name: str) -> Callable[[type[Widget]], type[Widget]]:
    """Class decorator. Registers `cls` under `type_name` in WIDGETS.

    Re-registering the same type raises ValueError so accidental duplicate
    decorators surface immediately instead of silently overwriting.
    """

    def _register(cls: type[Widget]) -> type[Widget]:
        if type_name in WIDGETS:
            raise ValueError(
                f"Widget type '{type_name}' already registered to "
                f"{WIDGETS[type_name].__name__}; refusing to overwrite with "
                f"{cls.__name__}"
            )
        WIDGETS[type_name] = cls
        return cls

    return _register


def render_field(spec: dict, *, strict: bool = True) -> str:
    """Instantiate WIDGETS[spec['type']] with `spec` and return .render().

    Parameters
    ----------
    spec : dict
        Field spec. Must contain at least 'name' and 'type'; individual
        widgets may require additional keys ('choices' for enum,
        'options_model' for foreign_key).
    strict : bool, default True
        True  -> raise UnknownWidgetError for unregistered types.
        False -> return '<!-- unknown field: {name}:{type} -->' placeholder
                 so a single bad spec does not abort full-page emission.
    """
    type_name = spec.get("type", "")
    field_name = spec.get("name", "")
    cls = WIDGETS.get(type_name)
    if cls is None:
        if strict:
            raise UnknownWidgetError(
                f"No widget registered for field '{field_name}' with type "
                f"'{type_name}'. Register one in cli/widgets/widgets.py."
            )
        return f"<!-- unknown field: {field_name}:{type_name} -->"
    return cls(spec).render()


# Import the concrete widget module for its @register side effects. This is
# the only import-time action beyond WIDGETS population; the sub-module
# itself only reads .ejs.tmpl files off disk and declares classes.
from cli.widgets import widgets as _widgets  # noqa: E402,F401
