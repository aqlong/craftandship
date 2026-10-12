"""Widget emission - composes widgets (from cli/widgets/__init__.py) into
three target surfaces: admin settings page, admin CRUD new/edit forms, and
the dashboard settings CTA section.

Depends on:
- cli.widgets.render_field (dispatcher, section-03)
- cli.widgets.labels.humanize + LABEL_MAP (section-03)

Called by:
- cli.build._emit_widgets @processor (this section, section-04)

Design notes:
- Form actions are LITERAL, not EJS ternary. new.ejs uses /admin/<slug>;
  edit.ejs uses /admin/<slug>/<%= item.id %>. Routes are dual-mounted at
  /admin/<slug> by _ensure_admin_slug_mount so the canonical URL resolves.
- Widgets emit value expressions as `_v('<name>')` (the settings helper).
  For CRUD new/edit forms we post-process the rendered widget markup to
  swap those expressions for ('' in new, `item.<name> || ''` in edit) so
  the forms pre-populate from route-locals item.
- Emission is idempotent: emit_all(emit_all(a)) is byte-identical. The
  metadata timestamp overwrites the same key; the full file dict is
  regenerated each call from the sorted spec so second call == first.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

from cli.widgets import render_field
from cli.widgets.labels import humanize


# Six-section structure from the widget-emission section of docs/plans/b-plus-c/claude-plan.md.
# Python owns the layout decision (no Claude input). Order matters: it is
# the rendered order in the UI.

# Signal that downstream post-processors use to detect widget-emitted admin
# surfaces and skip destructive rewrites. Specifically,
# _fix_admin_crud_link_prefix (FIX C) strips /admin/ from href/action
# attributes, but widgets deliberately emit canonical /admin/<slug> action
# URLs (matched to the dual-mount added by _ensure_admin_slug_mount) that
# contracts/admin_crud.py asserts verbatim. The marker lets FIX C leave
# widget output alone while continuing to fix Claude-emitted admin list
# views that still need the /admin/ prefix stripped.
#
# Marker is a plain HTML comment so it survives BS4 parsing untouched and
# is invisible in the rendered DOM.
WIDGET_ADMIN_MARKER = "<!-- widgets:admin-form -->"
_WIDGET_MARKER = WIDGET_ADMIN_MARKER


SECTION_LAYOUT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Site Identity",   ("site_name", "site_url", "site_description")),
    ("Hero Section",    ("hero_tagline", "hero_description", "hero_image_url")),
    ("Logo & About",    ("nav_logo_url", "about_title", "about_body", "about_photo_url")),
    ("Contact Info",    ("business_name", "contact_email", "contact_phone", "contact_address")),
    ("Social Media",    ("instagram_url", "facebook_url", "youtube_url",
                         "tiktok_url", "twitter_url", "linkedin_url")),
)


# The two runtime helpers the settings template relies on to gate each
# field. _has(k) returns true when key k exists in the `settings` view local.
# _v(k) returns the stored value or ''. Ported from the canonical
# cli/templates/views_admin_settings.ejs (keeping the bodies verbatim so
# runtime behavior is unchanged).
# Helper identifier sets per surface. emit_* computes the FULL set by
# unioning these with `required_helpers` from every composed widget, then
# calls cli.widgets.helpers.render_helpers() to emit the corresponding
# `<% var X = function(...) %>` blocks at the top of the file. This keeps
# widget->helper coupling machine-checkable (see
# cli/contracts/widget_helpers.py) rather than baked into inline string
# constants.
_SETTINGS_SURFACE_HELPERS = frozenset({"_v_settings", "_has_settings"})
_CRUD_SURFACE_HELPERS = frozenset()  # per-widget via required_helpers


def _collect_widget_helpers(field_specs) -> frozenset:
    """Walk field_specs, instantiate each widget, and return the union of
    their declared required_helpers. Unknown types raise
    UnknownWidgetError (propagated up)."""
    from cli.widgets import WIDGETS
    reqs: set = set()
    for fs in field_specs:
        cls = WIDGETS.get(fs.get("type"))
        if cls is None:
            continue
        reqs |= set(getattr(cls, "required_helpers", frozenset()))
    return frozenset(reqs)


# --- Settings inference -----------------------------------------------------


def _settings_keys_for(spec: dict) -> list[str]:
    """Collect the settings keys the spec expects to render.

    Three sources, deduplicated in insertion order:
      1. spec['_seed_settings']: list of {"key": ..., "value": ...}
      2. spec['settings']: dict of key -> value
      3. Keys from every SECTION_LAYOUT group (always considered so the
         template gracefully shows standard sections when present).
    """
    keys: list[str] = []
    seen: set[str] = set()

    def _add(k: str) -> None:
        if k and k not in seen:
            seen.add(k)
            keys.append(k)

    for item in spec.get("_seed_settings") or []:
        if isinstance(item, dict):
            _add(str(item.get("key", "")))

    settings = spec.get("settings")
    if isinstance(settings, dict):
        for k in settings.keys():
            _add(str(k))

    for _section_name, section_keys in SECTION_LAYOUT:
        for k in section_keys:
            _add(k)

    return keys


def _is_image_key(key: str) -> bool:
    """Return True when the key names an image field.

    Matches the image-zone contract enforced by _ensure_upload_form_ui:
    key ends with _image_url, _photo_url, _logo_url, or is exactly one of
    those bare identifiers. Widget emission dispatches those keys through
    ImageUrlWidget (same EJS shape as the FIX N post-processor output).
    """
    suffixes = ("_image_url", "_photo_url", "_logo_url")
    bare = {"image_url", "photo_url", "logo_url"}
    return key in bare or key.endswith(suffixes)


def _field_spec_for_settings_key(key: str) -> dict:
    """Translate a settings key into a minimal widget field spec.

    Image keys go through ImageUrlWidget. Long-text keys use TextWidget
    (textarea). URL-suffixed keys use UrlWidget (type=url). Everything
    else uses StringWidget (type=text).
    """
    if _is_image_key(key):
        return {"name": key, "type": "image_url"}
    if key.endswith("_body") or key.endswith("_description") or key == "site_description":
        return {"name": key, "type": "text"}
    if key.endswith("_url"):
        return {"name": key, "type": "url"}
    return {"name": key, "type": "string"}


# --- emit_admin_settings ----------------------------------------------------


def _render_settings_field_block(key: str) -> str:
    """Render one field inside a section: widget gated by _has(key)."""
    field_spec = _field_spec_for_settings_key(key)
    rendered = render_field(field_spec, strict=True)
    return (
        f'<% if (_has("{key}")) {{ %>\n'
        f"{rendered}\n"
        f"<% }} %>"
    )


def _render_settings_section(section_name: str, keys: tuple[str, ...] | list[str]) -> str:
    """Render one <section> wrapping its keys in a _has() guard each."""
    if not keys:
        return ""
    blocks = "\n".join(_render_settings_field_block(k) for k in keys)
    # Section heading; the whole section is also gated so empty sections
    # do not leave stray <h2> headings behind. `card-premium admin-settings-
    # card` maps to premium-base.css + the <style> block at the top of the
    # settings page (dark/light theme auto-flip). Fields inside the card
    # are stacked with space-y-4: simpler than a grid and better for
    # admin forms whose fields have varied lengths.
    guard_expr = " || ".join(f'_has("{k}")' for k in keys)
    return (
        f'<% if ({guard_expr}) {{ %>\n'
        f'<div class="card-premium admin-settings-card p-6 rounded-xl border">\n'
        f'<h2 class="font-display text-lg font-semibold mb-4">{section_name}</h2>\n'
        f'<div class="space-y-4">\n'
        f"{blocks}\n"
        f"</div>\n"
        f"</div>\n"
        f"<% }} %>"
    )


# Embedded <style> block adapted verbatim from the pre-B+C canonical
# cli/templates/views_admin_settings.ejs so the widget-emitted page flips
# between dark and light themes via body class without relying on a
# particular post-processor firing. _fix_admin_dark_stat_cards still
# layers on top where present; this fallback keeps widget output
# self-contained.
_ADMIN_SETTINGS_STYLE = """<style>
/* Admin settings surface: auto-flips between dark and light theme via primary lum. */
.admin-settings-card {
  background: rgba(255, 255, 255, 0.6);
  border-color: rgba(0, 0, 0, 0.08);
  color: #1f2937;
}
.admin-settings-card h2 { color: #0f172a; }
.admin-settings-card label { color: #334155; }
.admin-settings-card p { color: #64748b; }

/* Dark-theme override kicks in when body has dark primary */
body.bg-slate-950 .admin-settings-card,
body[class*="bg-primary"] .admin-settings-card,
[data-theme="dark"] .admin-settings-card {
  background: rgba(26, 36, 86, 0.35);
  border-color: rgba(255, 255, 255, 0.1);
  color: #e2e8f0;
}
body.bg-slate-950 .admin-settings-card h2,
body[class*="bg-primary"] .admin-settings-card h2,
[data-theme="dark"] .admin-settings-card h2 { color: #ffffff; }
body.bg-slate-950 .admin-settings-card label,
body[class*="bg-primary"] .admin-settings-card label,
[data-theme="dark"] .admin-settings-card label { color: #cbd5e1; }

/* v17 FIX F: dark-theme input contrast. */
body.bg-slate-950 .admin-settings-card .input-premium,
body.bg-slate-950 .admin-settings-card input,
body.bg-slate-950 .admin-settings-card textarea,
body.bg-slate-950 .admin-settings-card select,
body[class*="bg-primary"] .admin-settings-card .input-premium,
body[class*="bg-primary"] .admin-settings-card input,
body[class*="bg-primary"] .admin-settings-card textarea,
body[class*="bg-primary"] .admin-settings-card select,
[data-theme="dark"] .admin-settings-card .input-premium,
[data-theme="dark"] .admin-settings-card input,
[data-theme="dark"] .admin-settings-card textarea,
[data-theme="dark"] .admin-settings-card select {
  background: rgba(15, 23, 42, 0.8);
  color: #e2e8f0;
  border-color: rgba(255, 255, 255, 0.2);
}
body.bg-slate-950 .admin-settings-card .input-premium::placeholder,
body[class*="bg-primary"] .admin-settings-card .input-premium::placeholder,
[data-theme="dark"] .admin-settings-card .input-premium::placeholder {
  color: #94a3b8;
}
</style>"""


def emit_admin_settings(spec: dict) -> str:
    """Emit the full string content of views/admin/settings.ejs.

    - Preserves top-of-file _has(key) and _v(key) helper definitions.
    - Iterates SECTION_LAYOUT; for each key, wraps the widget render in
      `<% if (_has("<key>")) { %> ... <% } %>`.
    - Keys present in the spec but not in any SECTION_LAYOUT group go into
      an auto-created "Additional Settings" section at the end (sorted
      alphabetically for determinism).
    - Form element is literal: <form action="/admin/settings" method="POST">.
    - CSRF token input included.
    """
    all_keys = _settings_keys_for(spec)
    layout_keys: set[str] = set()
    for _section_name, section_keys in SECTION_LAYOUT:
        layout_keys.update(section_keys)

    body_parts: list[str] = []
    for section_name, section_keys in SECTION_LAYOUT:
        body_parts.append(_render_settings_section(section_name, section_keys))

    # "Additional Settings" for any key the spec carries that is not in the
    # canonical six-section layout. Sorted for deterministic output.
    extra_keys = sorted(k for k in all_keys if k not in layout_keys)
    if extra_keys:
        body_parts.append(_render_settings_section("Additional Settings", tuple(extra_keys)))

    body = "\n".join(p for p in body_parts if p)

    # Helper injection: every settings widget implicitly uses _v+_has
    # (the surface contract), plus any helpers the composed widgets
    # declare (e.g. _dt for timestamp fields). render_helpers sorts for
    # deterministic output.
    from cli.widgets.helpers import render_helpers
    all_field_specs = [_field_spec_for_settings_key(k) for k in all_keys]
    helpers = _SETTINGS_SURFACE_HELPERS | _collect_widget_helpers(all_field_specs)
    helper_block = render_helpers(set(helpers))

    return (
        f"{_WIDGET_MARKER}\n"
        f"{helper_block}"
        f"{_ADMIN_SETTINGS_STYLE}\n"
        f'<div class="max-w-4xl mx-auto px-4 py-8">\n'
        f'  <div class="flex items-center justify-between mb-6">\n'
        f'    <h1 class="font-display text-2xl font-bold">Site Settings</h1>\n'
        f'    <a href="/admin" class="text-sm text-[var(--color-accent,#5a80c0)] hover:underline">&larr; Back to Dashboard</a>\n'
        f'  </div>\n'
        f'  <% if (typeof flash !== \'undefined\' && flash) {{ %>\n'
        f'    <div class="mb-6 px-4 py-3 rounded-lg border" style="background:rgba(16,185,129,0.08);border-color:rgba(16,185,129,0.3);color:#6ee7b7;">\n'
        f'      <%= flash %>\n'
        f'    </div>\n'
        f'  <% }} %>\n'
        f'  <form action="/admin/settings" method="POST" class="admin-settings-form space-y-6">\n'
        f'    <input type="hidden" name="_csrf" value="<%= csrfToken %>">\n'
        f"{body}\n"
        f'    <div class="flex justify-end">\n'
        f'      <button type="submit" class="btn-primary">Save Settings</button>\n'
        f'    </div>\n'
        f"  </form>\n"
        f"</div>\n"
    )


# --- emit_crud_new_edit -----------------------------------------------------


def _model_is_read_only(model: dict) -> bool:
    """Detect read-only models under both spec conventions.

    - access == 'read-only' / 'readonly' / 'ro'
    - read_only is truthy
    - permissions is 'read' / 'view' / 'read-only'
    """
    access = str(model.get("access", "")).lower()
    if access in {"read-only", "readonly", "ro"}:
        return True
    if model.get("read_only"):
        return True
    perms = model.get("permissions")
    if isinstance(perms, str) and perms.lower() in {"read", "view", "read-only"}:
        return True
    return False


def _slug_from_model(model: dict) -> str:
    """Derive the URL slug for a model.

    Prefers an explicit model['slug']; otherwise hyphenates the name and
    pluralizes (camelCase -> kebab-case + 's' suffix). Mirrors the slug
    convention used by _ensure_crud_forms_complete.
    """
    explicit = str(model.get("slug") or "").strip().lower()
    if explicit:
        return explicit
    name = str(model.get("name") or "").strip()
    if not name:
        raise ValueError("emit_crud_new_edit: model is missing both 'slug' and 'name'")
    # camelCase -> kebab-case
    parts: list[str] = []
    cur = ""
    for ch in name:
        if ch.isupper() and cur:
            parts.append(cur.lower())
            cur = ch
        else:
            cur += ch
    if cur:
        parts.append(cur.lower())
    slug = "-".join(parts) if parts else name.lower()
    # Pluralize
    if slug.endswith("y") and len(slug) > 1 and slug[-2] not in "aeiou":
        return slug[:-1] + "ies"
    if slug.endswith(("ch", "sh", "ss")) or slug[-1:] in ("s", "x", "z"):
        return slug + "es"
    return slug + "s"


# Regex to swap settings _v('<name>') reads for CRUD value expressions.
# Matches single or double quotes; spec matches how widgets.py emits _v().
_VALUE_EXPR_RE = re.compile(r"""_v\(\s*(['"])([^'"]+)\1\s*\)""")


def _rewrite_value_expr(rendered: str, entity_var: str | None) -> str:
    """Post-process widget markup so value reads work on CRUD surfaces.

    entity_var is None for new forms (empty string value), or the EJS
    identifier for edit forms (e.g. 'item' -> `item.<name> || ''`).
    """
    if entity_var is None:
        return _VALUE_EXPR_RE.sub("''", rendered)

    def _swap(m: re.Match) -> str:
        name = m.group(2)
        return f"({entity_var} && {entity_var}.{name}) || ''"

    return _VALUE_EXPR_RE.sub(_swap, rendered)


def _render_crud_field(field: dict, entity_var: str | None) -> str:
    """Render one field for a CRUD form with the correct value binding."""
    rendered = render_field(field, strict=True)
    return _rewrite_value_expr(rendered, entity_var)


def emit_crud_new_edit(spec: dict, model: dict) -> tuple[str, str] | None:
    """Emit (new_ejs, edit_ejs) strings for a writable data model.

    Returns None for read-only models.

    Form actions:
      new.ejs:   <form action="/admin/<slug>" method="POST">
      edit.ejs:  <form action="/admin/<slug>/<%= item.id %>" method="POST">

    (Literal URL prefix - no EJS ternary on action attribute. This is
    what contracts/admin_crud.py asserts. Routes are dual-mounted at
    /admin/<slug> by _ensure_admin_slug_mount.)

    Both include CSRF token input. Image-type fields render as drag-drop
    zones via render_field (which dispatches to ImageUrlWidget).
    """
    if _model_is_read_only(model):
        return None

    name = model.get("name")
    if not name:
        raise ValueError("emit_crud_new_edit: model is missing 'name'")

    slug = _slug_from_model(model)
    fields = model.get("fields") or []

    display_name = str(name)
    new_body = "\n".join(_render_crud_field(f, entity_var=None) for f in fields)
    edit_body = "\n".join(_render_crud_field(f, entity_var="item") for f in fields)

    # Map model name to Claude's JS var-naming conventions so the widget
    # `item` alias resolves regardless of whether the route handler passes
    # `item:`, the lowercase singular (`event:`), or snake_case
    # (`contact_message:`). Observed v21 runtime failure: handler passes
    # `video: result.rows[0]` but widget edit.ejs references `item.id`.
    # Solution: emit an alias block at the top of each widget-owned file
    # that coalesces these conventions into `item`.
    singular_lower = str(name).lower()
    import re as _re
    snake = _re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name)).lower()
    alias_candidates = [singular_lower]
    if snake != singular_lower:
        alias_candidates.append(snake)
    # Short-circuit fall-through: first defined candidate wins.
    alias_chain = "".join(
        f" || (typeof {c} !== 'undefined' && {c})"
        for c in alias_candidates
    )
    item_alias = (
        "<% var item = (typeof item !== 'undefined' && item)"
        f"{alias_chain}"
        " || {}; %>\n"
    )

    # Helper injection: walk composed widgets, union their required_helpers,
    # render the corresponding <% var X = ... %> blocks deterministically.
    from cli.widgets.helpers import render_helpers
    helpers = _CRUD_SURFACE_HELPERS | _collect_widget_helpers(fields)
    helper_block = render_helpers(set(helpers))

    # Shared page shell for both new.ejs and edit.ejs. `max-w-3xl` is a
    # touch tighter than settings since CRUD forms typically have fewer
    # fields. Form body stacks with space-y-4. Back-link points at the
    # admin list view; widget's _ensure_admin_slug_mount guarantees
    # /admin/<slug> resolves.
    def _shell(heading: str, form_action: str, body: str, cta: str) -> str:
        return (
            f"{_WIDGET_MARKER}\n"
            f"{helper_block}"
            f"{item_alias}"
            f'<div class="max-w-3xl mx-auto px-4 py-8">\n'
            f'  <div class="flex items-center justify-between mb-6">\n'
            f'    <h1 class="font-display text-2xl font-bold">{heading}</h1>\n'
            f'    <a href="/admin/{slug}" class="text-sm text-[var(--color-accent,#5a80c0)] hover:underline">&larr; Back to {display_name} list</a>\n'
            f'  </div>\n'
            f'  <% if (typeof error !== \'undefined\' && error) {{ %>\n'
            f'    <div class="mb-6 px-4 py-3 rounded-lg border" style="background:rgba(239,68,68,0.08);border-color:rgba(239,68,68,0.3);color:#fca5a5;">\n'
            f'      <%= error %>\n'
            f'    </div>\n'
            f'  <% }} %>\n'
            f'  <section class="admin-crud-form card-premium p-6 rounded-xl border">\n'
            f'    <form action="{form_action}" method="POST" class="space-y-4">\n'
            f'      <input type="hidden" name="_csrf" value="<%= csrfToken %>">\n'
            f"{body}\n"
            f'      <div class="flex justify-end pt-2">\n'
            f'        <button type="submit" class="btn-primary">{cta}</button>\n'
            f'      </div>\n'
            f'    </form>\n'
            f'  </section>\n'
            f'</div>\n'
        )

    new_ejs = _shell(
        heading=f"Create {display_name}",
        form_action=f"/admin/{slug}",
        body=new_body,
        cta=f"Create {display_name}",
    )
    edit_ejs = _shell(
        heading=f"Edit {display_name}",
        form_action=f"/admin/{slug}/<%= item.id %>",
        body=edit_body,
        cta=f"Save {display_name}",
    )
    return (new_ejs, edit_ejs)


# --- emit_dashboard_settings_section ----------------------------------------


def emit_dashboard_settings_section(spec: dict) -> str:
    """Emit the dashboard's 'Site Settings' CTA card snippet.

    Returns an HTML/EJS fragment (not a full file). Must contain:
      - <a href="/admin/settings"> CTA.
      - No <form> element (contracts/admin_dashboard.py asserts this).

    Caller stores the snippet at artifact.widgets_snippets[
    "dashboard_settings_section"]; _inject_dashboard_settings_section
    splices it into views/admin/dashboard.ejs.
    """
    _ = spec  # spec unused today; reserved for future per-archetype CTA copy
    return (
        '<section class="dashboard-settings-cta">\n'
        '<h2>Site Settings</h2>\n'
        "<p>Manage your site identity, hero, contact details, and social links.</p>\n"
        '<a href="/admin/settings" class="btn-primary">\n'
        "Manage Site Settings\n"
        "</a>\n"
        "</section>\n"
    )


# --- emit_all ----------------------------------------------------------------


def _set_file(artifact: Any, path: str, content: str) -> None:
    """Write path=content into artifact.files without caring about list vs dict.

    The artifact contract in cli/pipeline.Artifact declares
    files: dict[str, str | bytes], but runtime build_app threads a
    list[tuple[str, str | bytes]] through the pipeline. emit_all must
    support both shapes so it can run under test fixtures and real builds.
    """
    files = artifact.files
    if isinstance(files, dict):
        files[path] = content
        return
    # list of (path, content) tuples: replace existing entry or append.
    replaced = False
    for i, item in enumerate(files):
        if item and item[0] == path:
            files[i] = (path, content)
            replaced = True
            break
    if not replaced:
        files.append((path, content))


def emit_all(artifact: Any) -> Any:
    """Top-level entrypoint. Mutates artifact in place and returns it.

    - Writes admin settings file at 'views/admin/settings.ejs'.
    - For each writable model M in sorted(spec['data_models'],
      key=lambda m: m['name']), writes 'views/admin/<slug>/new.ejs' and
      'views/admin/<slug>/edit.ejs' to artifact.files.
    - Stores dashboard snippet at
      artifact.widgets_snippets['dashboard_settings_section'] (NOT in
      artifact.files - the artifact.files filter excludes __widgets/*
      paths but snippets are not files at all).
    - Sets artifact.metadata['widgets_emitted_at_generation'] to an ISO
      timestamp (e.g. datetime.datetime.utcnow().isoformat() + 'Z').
    - Idempotent: emit_all(emit_all(a)) == emit_all(a) byte-for-byte.
      Determinism requires sorted iteration over data_models.
    """
    spec = getattr(artifact, "spec", None) or {}

    # 1. Settings surface.
    _set_file(artifact, "views/admin/settings.ejs", emit_admin_settings(spec))

    # 2. CRUD new/edit per writable model, sorted alphabetically by name.
    data_models = spec.get("data_models") or []
    sorted_models = sorted(data_models, key=lambda m: str(m.get("name") or ""))
    for model in sorted_models:
        result = emit_crud_new_edit(spec, model)
        if result is None:
            continue
        new_ejs, edit_ejs = result
        slug = _slug_from_model(model)
        _set_file(artifact, f"views/admin/{slug}/new.ejs", new_ejs)
        _set_file(artifact, f"views/admin/{slug}/edit.ejs", edit_ejs)

    # 3. Dashboard snippet - NOT a file.
    snippets = getattr(artifact, "widgets_snippets", None)
    if snippets is None:
        snippets = {}
        try:
            artifact.widgets_snippets = snippets
        except AttributeError:
            pass
    snippets["dashboard_settings_section"] = emit_dashboard_settings_section(spec)

    # 4. Metadata timestamp.
    metadata = getattr(artifact, "metadata", None)
    if metadata is None:
        metadata = {}
        try:
            artifact.metadata = metadata
        except AttributeError:
            pass
    # Timezone-aware UTC timestamp; .isoformat() on an aware datetime
    # returns '+00:00' rather than 'Z', so explicitly replace the offset.
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    metadata["widgets_emitted_at_generation"] = now.isoformat().replace(
        "+00:00", "Z"
    )

    return artifact
