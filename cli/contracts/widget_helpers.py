"""Contract that asserts every widget-emitted .ejs references only
identifiers defined earlier in the same file OR on the route-handler's
locals whitelist.

Catches widget-contract drift at build time (runs in the tail of the
DAG via `_run_contracts`), before the artifact reaches `preview`.
Previously this class of bug surfaced only at HTTP render time,
after build + contracts + deploy had already passed, see v21
`_dt is not defined` and `item is not defined` runtime crashes that
every artifact-level test missed.

Scan strategy:
- Iterate every path under `views/` whose content begins with the
  widget-admin marker (`<!-- widgets:admin-form -->`).
- Extract every helper-style reference: `<%= IDENT(...) %>`,
  `<%- IDENT(...) %>`, and `<% ... IDENT(...) %>` call-site forms.
- For each referenced identifier, assert ONE of:
    (a) It's defined earlier in the same file via
        `<% var IDENT = function(...) %>` or `<% var IDENT = ... %>`.
    (b) It's a declared JS builtin (String, Date, Math, ...).
    (c) It's on the known-route-handler-locals whitelist
        (csrfToken, item, settings, title, error, ...).

A reference that satisfies none of these is a ContractViolation with
the file path, identifier name, and approximate line/column.
"""
from __future__ import annotations

import re


JS_BUILTINS = frozenset({
    "String", "Number", "Date", "Boolean", "Array", "Object", "Math",
    "JSON", "RegExp", "Map", "Set", "Promise", "Error", "TypeError",
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "encodeURI", "decodeURI",
    "console", "typeof",
    # EJS exposes the rendering locals object; direct `locals` access
    # rare but legitimate.
    "locals",
})

# JS keywords / control-flow tokens that appear immediately before `(`
# in widget EJS but are NOT function calls (e.g. `<% if (expr) { %>`).
# The call-site regex is intentionally permissive; this set filters
# keywords out of the referenced-identifiers collection.
JS_KEYWORDS = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "break",
    "continue", "return", "function", "var", "let", "const",
    "new", "delete", "throw", "try", "catch", "finally",
    "typeof", "instanceof", "in", "of", "yield", "async", "await",
    "this", "super", "class", "extends", "static", "import", "export",
    "true", "false", "null", "undefined", "void",
})

# Known route-handler-provided locals. Every generated Express route
# handler passes at minimum `csrfToken` + context-specific row/collection
# + standard view helpers (`title`, `error`, `message`). Widget templates
# reference these; widgets themselves should NOT reference anything here
# without also declaring it explicitly, but the contract is permissive
# for these known names.
ROUTE_HANDLER_LOCALS = frozenset({
    "csrfToken", "title", "error", "message", "req", "res", "next",
    "item", "items", "settings", "user", "users", "admin",
    # EJS loop vars (declared inline by `<% for (const X of Y) { %>`).
    "c", "k", "v", "_opt", "_i", "_c",
    # Common list/detail row bindings Claude emits.
    "event", "events", "photo", "photos", "video", "videos",
    "contact_message", "contact_messages", "contactmessage",
    "contactMessages", "contactMessage",
    "model", "rows", "row", "result",
})


# Identifiers defined inline via `<% var X = ...` or `<% let X = ...` or
# `<% const X = ...`. Captures the name for later comparison with uses.
_INLINE_VAR_DEF_RE = re.compile(
    r"<%\s*(?:var|let|const)\s+([a-zA-Z_$][a-zA-Z_$0-9]*)\s*="
)

# Identifiers defined via `var IDENT = function(...)` inside an EJS tag.
_EJS_FUNC_DEF_RE = re.compile(
    r"<%\s*var\s+([a-zA-Z_$][a-zA-Z_$0-9]*)\s*=\s*function"
)

# Call-site: `<%= IDENT(...` or `<%- IDENT(...` or bare `<% IDENT(...`.
# The tag opener may be `<%=`, `<%-`, `<%_`, `<%`, optionally followed by
# whitespace and an optional parenthesized prefix (ternary, etc.).
_CALL_SITE_RE = re.compile(
    r"<%[=\-_]?\s*"                        # EJS opener
    r"(?:\([^)]*\?\s*)?"                   # optional leading `(cond ? `
    r"(?:[a-zA-Z_$][a-zA-Z_$0-9]*\s*\+\s*)?"  # optional `foo + ` prefix
    r"([a-zA-Z_$][a-zA-Z_$0-9]*)"          # captured IDENT
    r"\s*\("                               # then `(`
)

# Property-access reference: `<%= IDENT.PROP %>`. IDENT must exist as a
# local or variable. Only relevant for the well-known case like
# `<%= item.id %>`.
_PROPERTY_REF_RE = re.compile(
    r"<%[=\-_]?\s*"
    r"(?:\([^)]*\?\s*)?"
    r"([a-zA-Z_$][a-zA-Z_$0-9]*)"
    r"\s*\."
)


class ContractViolation(AssertionError):
    """Local alias: re-exported in cli/contracts/__init__.py."""


def _widget_marker_present(content: str) -> bool:
    # cli.widgets.forms.WIDGET_ADMIN_MARKER is the canonical constant; use
    # a literal to avoid the import cycle forms -> build -> contracts.
    return "<!-- widgets:admin-form -->" in content


def _collect_defined(content: str) -> set:
    """Every identifier defined via `<% var/let/const X = %>` anywhere
    in the file. We don't enforce precedes-use strictly at the regex
    level (parsing EJS+JS order correctly is non-trivial); instead, we
    require MERE presence in the same file. The node-render harness
    test (tests/test_widget_runtime_rendering.py) enforces actual
    rendering without ReferenceError, which is the strict check."""
    return set(_INLINE_VAR_DEF_RE.findall(content)) | set(
        _EJS_FUNC_DEF_RE.findall(content)
    )


def _collect_referenced(content: str) -> set:
    """Every identifier referenced as a function call or property base
    in an EJS expression tag."""
    refs = set(_CALL_SITE_RE.findall(content))
    refs |= set(_PROPERTY_REF_RE.findall(content))
    return refs


def check(artifact, *, collect_all: bool = False, violations=None) -> None:
    """Assert every widget-emitted .ejs has a definition (or whitelist
    entry) for every helper-style reference it uses.

    `artifact.files` may be dict-shaped or list-of-tuples; match sibling
    contract modules' handling.
    """
    from cli.contracts import ContractViolation as _ContractViolation

    files = artifact.files if isinstance(artifact.files, dict) else dict(artifact.files)

    for path, content in files.items():
        if not path.endswith(".ejs") or not isinstance(content, str):
            continue
        if not path.startswith("views/"):
            continue
        if not _widget_marker_present(content):
            continue

        defined = _collect_defined(content)
        referenced = _collect_referenced(content)

        undeclared = referenced - defined - JS_BUILTINS - JS_KEYWORDS - ROUTE_HANDLER_LOCALS
        if undeclared:
            msg = (
                f"{path}: widget EJS references undeclared identifier(s) "
                f"{sorted(undeclared)!r}. Either add to "
                f"cli/widgets/helpers.HELPER_SNIPPETS + the widget's "
                f"required_helpers, or add the name to "
                f"cli/contracts/widget_helpers.ROUTE_HANDLER_LOCALS if "
                f"the route handler provides it. See "
                f"docs/internals/WIDGETS_AND_CONTRACTS.md."
            )
            if collect_all and violations is not None:
                violations.append(msg)
            else:
                raise _ContractViolation(msg)
