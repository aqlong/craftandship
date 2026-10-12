"""Central registry of EJS runtime-helper snippets.

Every widget that references a helper in its emitted EJS (e.g.
`DatetimeWidget` emits `<%= _dt(...) %>`) must declare the helper via
the class attribute `Widget.required_helpers: frozenset[str]`. The
`emit_*` functions in `cli/widgets/forms.py` compute the union of
required helpers from all composed widgets and call `render_helpers`
to inject the corresponding snippets at the top of each emitted file.

This makes the widget-to-helper coupling explicit and machine-checkable:
- `cli/contracts/widget_helpers.py` asserts every `<%= IDENT(...) %>`
  in a widget-emitted file has a preceding `<% var IDENT = ... %>`
  definition or is a known route-handler-provided local.
- `tests/test_widget_runtime_rendering.py` spawns a real EJS render
  under node with minimum-viable locals; any ReferenceError surfaces
  at pytest time, not HTTP render time.

Before this registry, widgets inlined helper references without
declaration; two runtime crashes (`_dt is not defined`, `item is not
defined`) surfaced on v21 build 2026-04-18 because the implicit
contract had no build-time verification. See
docs/internals/WIDGETS_AND_CONTRACTS.md for the
wider lesson.

Canonical helper names:
- `_dt`           : datetime-local serializer; YYYY-MM-DDTHH:MM or ''.
- `_v_settings`   : settings-scope value accessor (for admin/settings.ejs).
- `_has_settings` : settings-scope presence check.

Snippets are valid EJS blocks that define a single `var IDENT = ...;`
and terminate with `%>\n`. Snippets are idempotent when duplicated
(JS `var` re-declaration is legal) but `render_helpers` de-duplicates
via set semantics anyway.
"""
from __future__ import annotations


HELPER_SNIPPETS: dict[str, str] = {
    # datetime-local expects "YYYY-MM-DDTHH:MM". Returns empty string for
    # null/undefined/empty/invalid inputs and for objects whose Date parse
    # fails, so a blank new.ejs form doesn't crash. Slice(0,16) strips
    # seconds + timezone + milliseconds.
    "_dt": (
        "<% var _dt = function(v){ "
        "if (!v) return ''; "
        "try { var d = new Date(v); return isNaN(d.getTime()) ? '' : "
        "d.toISOString().slice(0, 16); } "
        "catch (e) { return ''; } "
        "}; %>\n"
    ),
    # Settings-scope value accessor. Returns stored value if present, else ''.
    # Used by `admin/settings.ejs` only.
    "_v_settings": (
        "<% var _v = function(k){ return (typeof settings !== 'undefined' && "
        "settings[k]) || ''; }; %>\n"
    ),
    # Settings-scope presence check. True iff settings object exists AND
    # has the key as an own property. Used to gate individual field
    # sections via `<% if (_has('hero_image_url')) { %>`.
    "_has_settings": (
        "<% var _has = function(k){ return typeof settings !== 'undefined' && "
        "Object.prototype.hasOwnProperty.call(settings, k); }; %>\n"
    ),
}


def render_helpers(required: set[str]) -> str:
    """Concatenate helper snippets for the named identifiers in
    alphabetical order.

    Unknown names are silently skipped so widgets can declare
    forward-looking requirements without breaking emit today. Callers
    that need strictness should validate the set against
    `HELPER_SNIPPETS.keys()` before calling.
    """
    names = sorted(required)
    return "".join(HELPER_SNIPPETS[n] for n in names if n in HELPER_SNIPPETS)
