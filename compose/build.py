"""compose.build: deterministic static-compose build pipeline for CAS v2.

Takes a structured v2 spec, resolves archetype + palette + fonts + page layouts
against the decision files in compose/data/, and emits a complete Node.js +
Express + EJS + Postgres web app to builds/<app_name>/. Zero Claude API calls
in this path.

Determinism: the emitted app is a function of the spec and of the files in
this repository, with four exceptions, and there are no others. On one
machine, with the first two pinned and the fourth switched off, the same
spec produces the same bytes (compose/tests/test_spec_hardening.py builds
one spec in two interpreters with different hash seeds and compares every
file).

  (1) The build time. routes/seo.js embeds it as the sitemap <lastmod>.
  (2) The build day. Sample "upcoming" dates (the placeholder event row in
      db/seed.sql, and the sample events compose.intake scaffolds when it
      translates a spec) are counted forward from it (`_build_moment`,
      `_sample_date`).
      Setting SOURCE_DATE_EPOCH pins (1) and (2).
  (3) The raster brand assets (the favicon PNG/ICO family and
      og-default.jpg). They are emitted only when Pillow is installed and
      are drawn with whichever system font Pillow finds first, so both
      which files exist and their bytes can differ between machines; that
      has not been measured. Nothing pins it.
  (4) The external-URL lint, which `build_app` runs and `compose_files`
      does not. Unless CAS_LINT_EXTERNAL_URLS is "skip", build_app sends a
      request to each external URL in the spec and records a warning for
      each one that looks dead. The warnings go into the two build report
      files, compose-manifest.txt (its `warning: ` lines and the two
      counts that include them, translation_warnings_count and
      engine_warnings_count) and translation-warnings.txt, so those
      two files depend on the network at build time; with the value "error"
      a dead link refuses the build. No emitted app file and no other line
      of the manifest (its sha256 included) depends on the lint. The report
      says what happened to the lint in its `external_url_lint:` line:
      "skipped" (the mode asked for none), "ran" (no warning line then
      means it found nothing) or "failed" (the lint itself raised, so no
      URL was judged; a warning says why). The tests set "skip".

Trust boundary: every string in a spec comes out of a customer conversation
and is treated as hostile. `_spec_safety_errors` is the gate for values that
become identifiers, column types, route paths or file names; it runs inside
`resolve_spec`, so every ResolvedSpec that function returns has passed it
(the DDL and route emitters re-check column types and paths themselves).
The gate also reserves the names the engine uses itself: a model slug or a
page id may not be a built-in table, router, view or route, and a custom
page path may not name another origin. Custom pages are classified once
(`_classify_custom_pages`); the routes, views, sitemap and navigation are
all built from the pages that survive, and every route of an auth_required
page carries the same guards (`_custom_page_guards`). A page marked
auth_required whose path something else in the build would answer without a
login is not dropped quietly: `resolve_spec` refuses the spec
(`_auth_required_page_errors`).

Shape: a spec is JSON written by a model, so any value can stand where
another type was meant. The validator owns the type of everything an
emitter or a view reads, and `validate_spec` returning nothing means the
spec builds: it runs the gate, then `resolve_spec` on a copy for the
refusals that need the resolved layouts (`_resolution_errors`). Three
layers: the size of the whole spec is bounded before anything else walks it
(`_spec_size_errors`: depth, number of values, length of one string and of
all text, number of pages, models, fields and roles); each key the
emitters read beside page data has one accepted type (`_kind_problem` and
the `*_KEY_KINDS` tables); and page data reaches a view only through the
parameters its widget declares in compose/data/widget-registry.json, in
the declared shape (`_spec_widget_data`: a number where text is declared
becomes its text, an undeclared key is left out, anything else is
refused). `build_app` also bounds the whole DOCUMENT it is handed before
`BuildSpec.from_dict` or the link lint reads any of it, keys the engine does
not know included (`_document_size_errors`). What a value may be, beside its
JSON type, is its fit to the column it lands in: a seed row or a default
that Postgres would refuse is left out with a warning
(`_stored_value_problem`, `_default_problem`), and a column the engine adds
or reads with a fixed meaning (`id`, `created_at`, `published_at`, the link to
a parent, a slug) is refused with a type it cannot work with or handled
(`_ENGINE_COLUMN_KINDS`, `_engine_column_errors`). compose/tests/
test_spec_shape_matrix.py substitutes every JSON type, and two valid column
types, at every position of the sample specs and requires a refusal or a
site whose every route renders, on a real PostgreSQL when one can be reached.

Publication: what a build shows to visitors is never decided from a name. A
model's rows are served without a login only where the spec sets
`public_listing_path` on the model or a page layout of the archetype reads
its table (`_public_row_sources` is the one definition). A custom page that
has a model's name is an ordinary page and the model stays admin-only. A row
created through a public form is stored unpublished when the model has a
boolean `published` column (`_public_insert_sql`), every submitted value is checked
against the model before the INSERT (`_public_form_checks`), and
`resolve_spec` refuses a spec whose public form stores into a model that is
shown to visitors and has no such column (`_public_form_errors`). "Published"
has one definition, `_published_condition`: the flag is TRUE, and a row whose
flag has no value is not published; every public read applies it. A public
form page names the columns a visitor may fill in (`allowed_fields`), or the
spec is refused (`_public_form_field_errors`); a page that lists
`allowed_roles` requires a login (`_roles_imply_login`); a visitor can order a
public list only by a column it prints (`_public_sort_columns`); and a stock
photo of a specific person or place is never used, whoever names it
(`_personal_stock_photo_errors`).

Free text is escaped per emit context (`_sql_esc`, `_sql_comment`,
`_js_escape`, `_ejs_json`), link targets and raw-HTML parameters supplied by
the spec are normalized by `_sanitize_spec_content`. Every emitted file is
written by `_write_build_files`, which confines each path to the build
directory (`_safe_output_path`) and fails the build when two files would
land on one path.

Architecture: see docs/internals/CAS_V2_STATIC_COMPOSE_PLAN.md.

Output shape matches v1's builds/<app_name>/ layout so the same worker +
deploy path works unchanged.
"""

from __future__ import annotations

import dataclasses
import decimal
import hashlib
import json
import math
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from compose.utils.self_host_fonts import (
    emit_font_files,
    is_self_host_enabled,
    plan_self_hosted_fonts,
    render_font_face_css,
    render_preload_links,
)
from compose.lint_external_urls import lint_external_urls, lint_mode, ExternalUrlLintError

COMPOSE_DIR = Path(__file__).resolve().parent
DATA_DIR = COMPOSE_DIR / "data"
WIDGET_DIR = COMPOSE_DIR / "widgets"
TEMPLATE_DIR = COMPOSE_DIR / "templates"
PUBLIC_STYLE_CSS_PATH = TEMPLATE_DIR / "public-style.css"


# ---------------------------------------------------------------------------
# Decision-file loaders. Each caches at module scope after first load.
# ---------------------------------------------------------------------------

_ARCHETYPES: Optional[dict] = None
_PALETTES: Optional[dict] = None
_FONT_PAIRS: Optional[dict] = None
_LAYOUTS: Optional[dict] = None
_WIDGETS: Optional[dict] = None
_FIELD_TYPES: Optional[dict] = None


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_archetypes() -> dict:
    global _ARCHETYPES
    if _ARCHETYPES is None:
        _ARCHETYPES = _load_json(DATA_DIR / "archetypes.json")
    return _ARCHETYPES


def load_palettes() -> dict:
    global _PALETTES
    if _PALETTES is None:
        _PALETTES = _load_json(DATA_DIR / "palettes.json")
    return _PALETTES


def load_font_pairs() -> dict:
    global _FONT_PAIRS
    if _FONT_PAIRS is None:
        _FONT_PAIRS = _load_json(DATA_DIR / "font-pairs.json")
    return _FONT_PAIRS


def load_layouts() -> dict:
    global _LAYOUTS
    if _LAYOUTS is None:
        _LAYOUTS = _load_json(DATA_DIR / "page-layouts.json")
    return _LAYOUTS


_TEMPLATES = None
def load_templates() -> dict:
    """Load compose/data/templates.json (template families).

    The families are the entries of its `templates` list. The plan they
    came from is docs/internals/CAS_V2_TEMPLATE_FAMILIES_PLAN.md.
    """
    global _TEMPLATES
    if _TEMPLATES is None:
        path = DATA_DIR / "templates.json"
        if path.exists():
            _TEMPLATES = _load_json(path)
        else:
            _TEMPLATES = {"templates": []}
    return _TEMPLATES


def get_template_family(template_id: str) -> Optional[dict]:
    """Look up a template family by id; None if missing."""
    for t in load_templates().get("templates", []):
        if t.get("id") == template_id:
            return t
    return None


_FLOURISH_SETS = None
def load_flourish_sets() -> dict:
    """Load compose/data/flourish-sets.json.

    Flourish sets are opt-in CSS-only design accents bundled per
    template family. A family's `flourish_set` field names which set
    to inject; the resulting CSS is appended to layout-public.ejs's
    <style> block at compose time. Pure CSS, no JS, no fonts; honors
    prefers-reduced-motion.
    """
    global _FLOURISH_SETS
    if _FLOURISH_SETS is None:
        path = DATA_DIR / "flourish-sets.json"
        if path.exists():
            _FLOURISH_SETS = _load_json(path)
        else:
            _FLOURISH_SETS = {"sets": []}
    return _FLOURISH_SETS


def get_flourish_set(set_id: str) -> Optional[dict]:
    """Return the flourish set with this id, or None when missing.

    'none' is a valid id that returns an entry with empty CSS; treat
    it as a no-op rather than a missing set.
    """
    for s in load_flourish_sets().get("sets", []):
        if s.get("id") == set_id:
            return s
    return None


def load_widgets() -> dict:
    global _WIDGETS
    if _WIDGETS is None:
        _WIDGETS = _load_json(DATA_DIR / "widget-registry.json")
    return _WIDGETS


def load_field_types() -> dict:
    """Load the field-type registry (compose/data/field-types.json).

    The registry maps a logical field_type id (e.g. "email", "phone_us",
    "state_us") to its html_input, sql_type, pattern, sanitizer chain, and
    related metadata. Widgets + the compose pipeline resolve a field's
    rendering + storage from this registry when `field_type` is set. Older
    model definitions that only set `sql_type` still work by falling back
    to a sensible default.
    """
    global _FIELD_TYPES
    if _FIELD_TYPES is None:
        _FIELD_TYPES = _load_json(DATA_DIR / "field-types.json")
    return _FIELD_TYPES


def get_field_type(type_id: str) -> dict:
    types = load_field_types()["field_types"]
    if type_id not in types:
        raise ValueError(f"Unknown field_type: {type_id!r}")
    return types[type_id]


def get_archetype(archetype_id: str) -> dict:
    for a in load_archetypes()["archetypes"]:
        if a["id"] == archetype_id:
            return a
    raise ValueError(f"Unknown archetype: {archetype_id!r}")


def get_palette(palette_id: str) -> dict:
    for p in load_palettes()["palettes"]:
        if p["id"] == palette_id:
            return p
    raise ValueError(f"Unknown palette: {palette_id!r}")


def get_font_pair(pair_id: str) -> dict:
    for fp in load_font_pairs()["font_pairs"]:
        if fp["id"] == pair_id:
            return fp
    raise ValueError(f"Unknown font pair: {pair_id!r}")


def get_layout(layout_id: str) -> dict:
    layouts = load_layouts()["layouts"]
    if layout_id not in layouts:
        raise ValueError(f"Unknown layout: {layout_id!r}")
    return layouts[layout_id]


def get_widget(widget_id: str) -> dict:
    widgets = load_widgets()["widgets"]
    if widget_id not in widgets:
        raise ValueError(f"Unknown widget: {widget_id!r}")
    return widgets[widget_id]


# ---------------------------------------------------------------------------
# Spec shape (v2)
# ---------------------------------------------------------------------------

@dataclass
class BuildSpec:
    """The v2 spec shape. Produced by the intake flow, consumed here.

    The intake conversation fills this out via picker UI + AI-drafted copy.
    """
    app_name: str
    archetype: str
    palette: str
    font_pair: str
    admin_email: str
    settings: Dict[str, Any] = field(default_factory=dict)
    data_models: List[Dict[str, Any]] = field(default_factory=list)
    pages: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    seed_content: Dict[str, List[dict]] = field(default_factory=dict)
    nav_items: Optional[List[Dict[str, str]]] = None
    admin_nav_items: Optional[List[Dict[str, str]]] = None
    # Customer-declared pages that don't map to archetype
    # defaults (e.g. /book, /rsvp, /our-story). Each entry:
    #   {path, title, purpose, auth_required, allowed_roles}
    custom_pages: List[Dict[str, Any]] = field(default_factory=list)
    # Customer-declared user roles. Each entry:
    #   {role, description}
    user_roles: List[Dict[str, Any]] = field(default_factory=list)
    # What the SPEC carried under `_translation_warnings`: the notes of
    # whichever translator produced the spec (dropped pages, unknown field
    # types and so on), as {reason, detail} objects or plain strings. It is
    # spec text like any other. The engine never appends to this list and
    # never reads it to decide anything; build_app copies a bounded number
    # of its entries into the build report as `spec-warning:` lines
    # (`_spec_warning_lines`).
    translation_warnings: List[Dict[str, Any]] = field(default_factory=list)
    # What the ENGINE found while resolving and composing this spec (a
    # dropped page, a model whose rows an archetype page shows, a skipped
    # seed row, a dead link). Entries are {reason, detail} and are added
    # only by `_engine_warning`. `from_dict` never fills this list, so
    # nothing a spec contains can add an entry, remove one, or stand in
    # for one; build_app writes them first in the report, as `warning:`
    # lines.
    engine_warnings: List[Dict[str, str]] = field(default_factory=list)
    # Harbor design-preference hints carried through from translate_v1_spec
    # / scaffold_spec. Used at resolve_spec time to override page_layouts
    # via pick_layout_variant. Empty dict = no hints; falls back to
    # archetype default layout key + deterministic seed.
    harbor_meta: Dict[str, Any] = field(default_factory=dict)
    # Stamped by scaffold_spec via pick_template_family. Empty string = legacy
    # behavior (classic-business shell defaults, no typography scale).
    template_family_id: str = ""
    # Opt-in flag for sites whose readers need larger text. When true, emit
    # CSS that bumps body to 19px, paragraphs to ~19px, strong to bold, and
    # nudges h2 up one notch. Off by default so other sites keep the
    # editorial default.
    readability_bump: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "BuildSpec":
        # A spec that is not an object, or lacks a required key, is refused
        # with the validator's wording rather than a TypeError or KeyError
        # from this constructor: absent keys become "" here and
        # validate_spec names them.
        if not isinstance(d, dict):
            raise ValueError(
                "Spec validation failed: the spec must be a JSON object, not "
                + type(d).__name__
            )
        return cls(
            app_name=d.get("app_name", ""),
            archetype=d.get("archetype", ""),
            palette=d.get("palette", ""),
            font_pair=d.get("font_pair", ""),
            admin_email=d.get("admin_email", ""),
            # `or` rather than a .get default: an explicit JSON null means
            # "absent". A wrong non-empty type is left as-is so the safety
            # gate can name it instead of an emitter crashing on it.
            settings=d.get("settings") or {},
            data_models=d.get("data_models") or [],
            pages=d.get("pages") or {},
            seed_content=d.get("seed_content") or {},
            nav_items=d.get("nav_items"),
            admin_nav_items=d.get("admin_nav_items"),
            custom_pages=d.get("custom_pages") or [],
            user_roles=d.get("user_roles") or [],
            translation_warnings=d.get("_translation_warnings") or [],
            harbor_meta=d.get("_harbor_meta") or {},
            template_family_id=d.get("template_family_id", ""),
            readability_bump=bool(d.get("readability_bump", False)),
        )


# ---------------------------------------------------------------------------
# Spec safety gate
#
# Every string in a spec originates in a customer conversation, so it is
# attacker-influenced. The values checked here are the ones the emitters
# interpolate as CODE rather than as data: SQL identifiers and column types
# (db/schema.sql, router SQL), Express route paths and JS identifiers
# (server.js, routes/*.js), and output file names (routes/<slug>.js,
# views/<page>.ejs). Free text (titles, copy, seed values) is NOT validated
# here; it is escaped for its emit context instead.
#
# All patterns are applied with fullmatch: `re.match(r"^...$")` accepts a
# trailing newline, which is exactly the character a comment or string
# breakout needs.
#
# The upstream translator (web/src/services/harbor_to_compose.ts) applies a
# similar allowlist and DROPS what does not fit. This gate REFUSES instead:
# the engine does not trust its caller, and a spec that reaches it in this
# shape is a bug or an attack, not something to guess a repair for.
# ---------------------------------------------------------------------------

# Every pattern below is an ALLOWLIST for text that becomes code, and every
# one is compiled with re.ASCII. Without it `\d` also matches the digits of
# every other script ("VARCHAR(\uff11\uff10)" in full-width digits), `\s`
# matches every Unicode space, and IGNORECASE folds letters that are not
# ASCII onto ASCII ones (a dotless i passes for "i", a long s for "s"), so
# a type or an identifier that Postgres or node reads differently from
# what was checked would get through. The patterns that already spell
# their classes out ([A-Za-z0-9_]) carry the flag too, so that adding a
# `\d` to one of them later cannot reopen this.
_APP_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,62}", re.ASCII)
_MODEL_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,59}", re.ASCII)
_FIELD_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}", re.ASCII)
_MODEL_SLUG_RE = re.compile(r"[a-z][a-z0-9-]{0,62}", re.ASCII)
# Page ids become a route path, a view file name and part of a
# site_settings key. Underscores are allowed because spec.pages also carries
# data-only keys in that shape (e.g. "service_areas").
_PAGE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}", re.ASCII)
# A route path the engine is willing to emit: plain segments only. No dots,
# quotes, whitespace, or Express pattern characters (: * ? + ( )).
_SAFE_PATH_RE = re.compile(
    r"/[A-Za-z0-9][A-Za-z0-9_-]*(?:/[A-Za-z0-9][A-Za-z0-9_-]*){0,7}/?",
    re.ASCII,
)
# Column types passed through as `sql_type`. Mirrors SAFE_SQL_TYPE_RE in
# web/src/services/harbor_to_compose.ts; keep the two in step.
_SAFE_SQL_TYPE_RE = re.compile(
    r"(?:text|varchar|char|integer|int|bigint|smallint|serial|bigserial|"
    r"boolean|bool|numeric|decimal|real|double precision|date|time|"
    r"timestamp|timestamptz|json|jsonb|uuid)"
    r"(?:\(\s*\d{1,4}(?:\s*,\s*\d{1,4})?\s*\))?(?:\[\])?",
    re.IGNORECASE | re.ASCII,
)
# Deliberately narrow: this value is written into a SQL literal and into
# .env.example, so no quotes, whitespace or control characters.
_ADMIN_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}",
    re.ASCII,
)

# Tables the engine creates itself. A customer model that maps onto one of
# these would get an admin CRUD router (and, with public_listing_path, a
# PUBLIC listing) over users / sessions / login tokens.
_RESERVED_TABLE_NAMES = frozenset({
    "users", "user_sessions", "site_settings", "magic_link_tokens",
    "slug_aliases",
})
# Model slugs the engine owns. routes/<slug>.js would overwrite a built-in
# router; /<slug> or /admin/<slug> would shadow a built-in route; and
# views/custom/ holds one file per custom page. ("widgets" is NOT reserved:
# a Widget model's views sit beside the widget category directories without
# colliding with any of them.)
_RESERVED_MODEL_SLUGS = frozenset({
    "auth", "legal", "seo", "pages", "upload", "admin-dashboard",
    "admin-settings",
    "admin", "login", "logout", "magic", "health", "privacy", "terms",
    "set-password", "request-magic-link", "magic-link-sent", "contact",
    "newsletter-signup", "uploads", "js", "brand", "fonts", "images",
    "settings", "change-password", "clear-all",
    "custom",
})
# Paths a model's public listing may never be mounted at. Compared
# case-insensitively because Express matches mount paths that way: a router
# mounted at "/Admin/x" answers "/admin/x", and the resource router treats
# any base URL under /admin/ as the admin surface.
_RESERVED_PUBLIC_PATHS = frozenset({
    "/login", "/logout", "/magic", "/set-password", "/request-magic-link",
    "/magic-link-sent", "/privacy", "/terms", "/health", "/contact",
    "/newsletter-signup", "/uploads", "/js", "/brand", "/fonts", "/images",
})
# Page ids the engine owns. A page id becomes views/<id>.ejs and the route
# /<id>, so one of these would replace a built-in view (the login form, a
# layout, an error page) or sit on a built-in route. An archetype's own page
# ids ("contact", "events", ...) are not listed: those ARE pages.
# compose/tests/test_spec_hardening.py compares this set with the views and
# routes a build actually emits, so a new built-in cannot be added without it.
_RESERVED_PAGE_IDS = frozenset({
    # views the engine writes at views/<name>.ejs
    "login", "set-password", "request-magic-link", "magic-link-sent",
    "magic-link-confirm", "privacy", "terms", "error-404", "error-500",
    "error-csrf", "layout-admin", "layout-public",
    # built-in routes and static directories
    "admin", "logout", "magic", "health", "newsletter-signup", "uploads",
    "js", "brand", "fonts", "images",
})
# Paths a custom page may never claim, as comparison keys (see _path_key).
# These are DROPPED by _classify_custom_pages rather than refused by the
# gate: a v1 spec routinely lists "/login" or "/admin" among its pages, and
# the built-in route is what should answer there. /admin/<anything> has its
# own rule in that function.
_RESERVED_CUSTOM_PAGE_PATHS = frozenset({
    "/login", "/magic", "/magic/consume", "/logout", "/terms", "/privacy",
    "/set-password", "/request-magic-link", "/magic-link-sent",
    "/health", "/newsletter-signup", "/uploads", "/js", "/brand", "/fonts",
    "/images", "/admin",
})
# Routes the engine itself serves under /admin, besides /admin/<model slug>.
_ENGINE_ADMIN_PATHS = frozenset({
    "/admin", "/admin/settings", "/admin/upload", "/admin/change-password",
    "/admin/clear-all",
})

_ON_DELETE_ACTIONS = frozenset({"SET NULL", "CASCADE", "RESTRICT", "NO ACTION"})

# How many reasons a refusal lists. A malformed spec can be wrong in
# thousands of places; the first few say what to fix.
_MAX_REPORTED_ERRORS = 20

# The two role names the engine itself gives a meaning to: "admin" is the
# seeded owner account, "user" is the column default of users.role.
_BUILTIN_ROLES = ("admin", "user")
# users.role is VARCHAR(50); a longer name could never be stored, so a page
# restricted to it could never be opened.
_MAX_ROLE_NAME_LEN = 50
# Columns emit_schema_sql adds to every model table by itself.
_ENGINE_COLUMNS = ("id", "created_at")

_TEXT_SETTING_KEYS = (
    "site_name", "site_url", "site_description", "hero_tagline",
    "hero_description", "hero_image_url", "hero_eyebrow", "nav_logo_url",
    "about_title", "about_body", "about_photo_url", "contact_email",
    "contact_phone", "contact_address", "business_name", "phone",
    "support_email", "address", "instagram_url", "facebook_url",
    "youtube_url", "tiktok_url", "twitter_url", "linkedin_url",
    "venmo_handle", "owner_name", "services_intro", "contact_intro",
    "journal_title", "events_title", "gallery_title",
    "brand_logo_color", "brand_logo_text_color",
)

# The type of each further key the emitters read, by where it sits. The gate
# refuses a value of another type (null always means "absent"). Kinds:
#   "string"   a JSON string
#   "flag"     true or false
#   "integer"  a whole JSON number
#   "scalar"   a string, a number or a boolean
#   "strings"  a list of strings
#   "choices"  the choices of a select: a list whose entries are a string,
#              a number, or an object {value, label} of strings or numbers
# Page data has its own, per-widget declaration (`_spec_widget_data`).
_SETTING_KEY_KINDS = {"service_options": "strings"}
_MODEL_FIELD_KEY_KINDS = {
    "label": "string", "help": "string", "placeholder": "string", "group": "string",
    "field_type": "string", "required": "flag", "rows": "integer",
    "options": "choices", "default": "scalar",
}
_CUSTOM_PAGE_KEY_KINDS = {
    "title": "string", "purpose": "string", "description": "string",
    "form_target": "string", "submit_text": "string", "success_message": "string",
    "allowed_fields": "strings", "auth_required": "flag",
}
# Rows of a multi-line input: `rows` is written into a textarea attribute.
_MAX_TEXTAREA_ROWS = 100


def _kind_problem(kind: str, value: Any) -> Optional[str]:
    """Why `value` is not of `kind` ("must be ..."), or None when it is.
    None is always accepted: a JSON null means the key is absent."""
    if value is None:
        return None
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "string":
        return None if isinstance(value, str) else "must be a string"
    if kind == "flag":
        return None if isinstance(value, bool) else "must be true or false"
    if kind == "integer":
        return None if isinstance(value, int) and not isinstance(value, bool) else "must be a whole number"
    if kind == "scalar":
        return None if isinstance(value, (str, bool)) or is_number else "must be a string, a number or a boolean"
    if kind == "strings":
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return None
        return "must be a list of strings"
    if kind == "choices":
        def text_like(item: Any) -> bool:
            return isinstance(item, str) or _number_text(item) is not None

        def choice(item: Any) -> bool:
            if isinstance(item, dict):
                return text_like(item.get("value")) and (
                    item.get("label") is None or text_like(item.get("label"))
                )
            return text_like(item)

        if isinstance(value, list) and all(choice(item) for item in value):
            return None
        return "must be a list of strings, numbers or {value, label} objects"
    raise ValueError(f"unknown kind {kind!r}")


def _select_choices(field: dict) -> Optional[list]:
    """A field's `options` as the form templates read them: each entry a
    string, or {value, label} with string values. A number is written as
    its text (the templates test `typeof opt === 'string'`), an entry that
    is neither is left out, and None is returned when there are none."""
    options = field.get("options")
    if not isinstance(options, list):
        return None

    def as_text(item: Any) -> Optional[str]:
        return item if isinstance(item, str) else _number_text(item)

    choices: list = []
    for item in options:
        if isinstance(item, dict):
            value = as_text(item.get("value"))
            if value is None:
                continue
            label = as_text(item.get("label"))
            choices.append({"value": value, "label": label} if label is not None else {"value": value})
        else:
            text = as_text(item)
            if text is not None:
                choices.append(text)
    return choices or None


def _is_safe_route_path(path: Any) -> bool:
    """True when `path` is a plain absolute route path the engine may emit
    into server.js / routes/*.js and derive a view file name from. The
    site root "/" qualifies."""
    return isinstance(path, str) and len(path) <= 120 and (
        path == "/" or bool(_SAFE_PATH_RE.fullmatch(path))
    )


def _is_parametric_path(path: Any) -> bool:
    """A path with an Express `:param` segment (e.g. /events/:slug). Never
    emitted: `_custom_pages_filter` drops these. Tolerated by validation so
    a v1 spec that lists its detail routes still builds."""
    if not isinstance(path, str) or ":" not in path or len(path) > 120:
        return False
    return bool(_SAFE_PATH_RE.fullmatch(path.replace("/:", "/")))


def _page_id_reservation(page_id: str, archetype_pages) -> Optional[str]:
    """Why `page_id` cannot be a page of the spec's own, or None when it can.

    Two families of names are taken: the engine's built-in views and routes
    (`_RESERVED_PAGE_IDS`), and "<page>-detail" for each of the archetype's
    default pages, because views/<page>-detail.ejs is the per-item view of
    that page's listing. The gate, resolve_spec and the copy-mode translator
    in compose.intake all ask this one function.
    """
    if page_id in _RESERVED_PAGE_IDS:
        return "is reserved for a built-in view or route"
    suffix = "-detail"
    if page_id.endswith(suffix) and page_id[: -len(suffix)] in archetype_pages:
        return f"is reserved for the detail view of the {page_id[: -len(suffix)]!r} page"
    return None


def _path_key(path: str) -> str:
    """Comparison key for a route path. Express matches route paths without
    regard to letter case or one trailing slash, so "/Login/" and "/login"
    are the same route and must be compared as the same path."""
    return path.rstrip("/").lower() or "/"


_URL_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_URL_LEADING_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:", re.ASCII)


def _is_offsite_reference(value: str) -> bool:
    """True when a browser would resolve `value` to another origin (or run
    it): it starts with a URL scheme ("https://host/x", "javascript:x"), or
    it is protocol-relative ("//host/x").

    Judged the way a browser parses a link target: tabs, newlines and the
    other control characters are removed first, surrounding spaces are
    trimmed, and a backslash counts as a slash, so "/\\host" and
    "/<TAB>/host" are both "//host".
    """
    probe = _URL_CONTROL_CHARS_RE.sub("", value).strip().replace("\\", "/")
    return probe.startswith("//") or bool(_URL_LEADING_SCHEME_RE.match(probe))


def _public_listing_path_error(path: Any) -> Optional[str]:
    """Reason a model's public_listing_path is unacceptable, or None."""
    if not _is_safe_route_path(path) or path == "/":
        # "/" would mount the resource router over the whole site.
        return "is not a plain absolute path"
    lowered = path.rstrip("/").lower()
    if lowered == "/admin" or lowered.startswith("/admin/"):
        return "is under /admin (the admin surface is mounted by the engine)"
    if lowered in _RESERVED_PUBLIC_PATHS:
        return "collides with a built-in route"
    return None


# --- Bounds on a spec ---------------------------------------------------------
# A spec is read from a database row that a model wrote. The engine walks all
# of it, several emitters more than once, so its size is bounded here, in one
# pass that stops at the first bound it meets. The numbers are far above any
# real spec (the largest sample fixture has about 700 values, 16 thousand
# characters of text and 10 levels) and low enough that a build of the largest
# accepted spec stays a matter of seconds.
_MAX_SPEC_DEPTH = 32            # nesting of objects and lists
_MAX_SPEC_VALUES = 200_000      # objects, lists, strings, numbers, in total
_MAX_SPEC_STRING = 100_000      # characters in one string (or one key)
_MAX_SPEC_TEXT = 4_000_000      # characters in all strings together
# Each custom page is classified against every other page and every model
# (`_classify_custom_pages`), which is quadratic; each model emits a router
# and a set of views.
_MAX_CUSTOM_PAGES = 300
_MAX_DATA_MODELS = 100
_MAX_MODEL_FIELDS = 100
_MAX_USER_ROLES = 100
# The spec FILE build_app is handed. A spec inside every bound above fits in
# 28 MB however it is written (all of its text as six-byte escapes, twenty
# bytes of punctuation per value); a larger file is refused before it is
# read.
_MAX_SPEC_FILE_BYTES = 32 * 1024 * 1024

# A lone surrogate (JSON can spell one: "\ud800") is not text: it cannot be
# encoded as UTF-8, so the first file that carried it could not be written.
_LONE_SURROGATE_RE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")

# BuildSpec attribute -> the key it has in a spec, for messages.
_SPEC_CONTAINERS = (
    ("settings", "settings"), ("data_models", "data_models"), ("pages", "pages"),
    ("seed_content", "seed_content"), ("nav_items", "nav_items"),
    ("admin_nav_items", "admin_nav_items"), ("custom_pages", "custom_pages"),
    ("user_roles", "user_roles"), ("translation_warnings", "_translation_warnings"),
    ("harbor_meta", "_harbor_meta"), ("template_family_id", "template_family_id"),
    ("archetype", "archetype"), ("palette", "palette"), ("font_pair", "font_pair"),
)


def _json_size_errors(roots: List[Tuple[str, Any]]) -> List[str]:
    """The first bound on size that the decoded JSON values in `roots`
    (`[(name, value), ...]`) exceed together, as a one-element list, or []
    when they are within all of them: nesting, number of values, length of
    one string, length of all text.

    Also refuses what is not JSON text at all: a lone surrogate in a string
    or a key, a key that is not a string, and a value that is not a JSON
    value. Iterative on purpose: a recursive walk would itself fall over on
    the nesting it is here to refuse. The work is bounded too: the walk
    stops at the first bound it meets.
    """
    stack = [(name, value, 1) for name, value in roots]
    values = 0
    text = 0

    def at(where: str) -> str:
        # A position can be 32 keys of 60 characters long; say where it
        # starts and where it ends.
        return where if len(where) <= 160 else where[:90] + " ... " + where[-60:]

    def string_problem(where: str, value: str) -> Optional[str]:
        nonlocal text
        if len(value) > _MAX_SPEC_STRING:
            return (f"{at(where)} is {len(value)} characters long; one string of a spec may "
                    f"have at most {_MAX_SPEC_STRING}")
        text += len(value)
        if text > _MAX_SPEC_TEXT:
            return f"the spec holds more than {_MAX_SPEC_TEXT} characters of text"
        if _LONE_SURROGATE_RE.search(value):
            return f"{at(where)} contains a lone surrogate, which is not valid text"
        return None

    while stack:
        where, value, depth = stack.pop()
        values += 1
        if values > _MAX_SPEC_VALUES:
            return [f"the spec holds more than {_MAX_SPEC_VALUES} values"]
        if value is None or isinstance(value, (bool, int, float)):
            continue
        if isinstance(value, str):
            problem = string_problem(where, value)
            if problem:
                return [problem]
            continue
        if not isinstance(value, (dict, list)):
            return [f"{at(where)} is not a JSON value ({type(value).__name__})"]
        if depth > _MAX_SPEC_DEPTH:
            return [f"{at(where)} is nested more than {_MAX_SPEC_DEPTH} levels deep"]
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    return [f"{at(where)} has a key that is not a string: {str(key)[:40]!r}"]
                problem = string_problem(f"a key of {where}", key)
                if problem:
                    return [problem]
                stack.append((f"{where}.{key[:60]}", child, depth + 1))
        else:
            for index, child in enumerate(value):
                stack.append((f"{where}[{index}]", child, depth + 1))
    return []


def _spec_size_errors(spec: "BuildSpec") -> List[str]:
    """The first bound on a spec's size that this spec exceeds, as a
    one-element list, or [] when it is within all of them: the bounds of
    `_json_size_errors` over everything the spec holds, then the number of
    custom pages, models, fields of a model and roles. "Holds" means the
    containers a BuildSpec keeps; a document with keys the engine does not
    know is bounded as a whole by `_document_size_errors`, which `build_app`
    asks first."""
    errors = _json_size_errors([(name, getattr(spec, attr)) for attr, name in _SPEC_CONTAINERS])
    if errors:
        return errors
    for attr, limit in (
        ("custom_pages", _MAX_CUSTOM_PAGES), ("data_models", _MAX_DATA_MODELS),
        ("user_roles", _MAX_USER_ROLES),
    ):
        count = len(getattr(spec, attr))
        if count > limit:
            return [f"{attr} has {count} entries; a spec may have at most {limit}"]
    for model in spec.data_models:
        fields = model.get("fields") if isinstance(model, dict) else None
        if isinstance(fields, list) and len(fields) > _MAX_MODEL_FIELDS:
            return [f"a data_model has {len(fields)} fields; a model may have at most {_MAX_MODEL_FIELDS}"]
    return []


def _document_size_errors(document: Any) -> List[str]:
    """The first bound on size that a whole spec DOCUMENT (the decoded JSON
    object `build_app` was handed, before `BuildSpec.from_dict` picks the
    keys it knows out of it) exceeds, as a one-element list, or [].

    `_spec_size_errors` bounds the containers the engine reads. A document
    also carries keys it does not read (a model may write anything), and
    `build_app` itself still walks all of them: the external-URL lint walks
    the whole document, and the report reads `tier` from it. So the same
    bounds are applied to the whole thing, unknown keys included, before
    anything reads it.
    """
    return _json_size_errors([("spec", document)])


def _spec_safety_errors(spec: "BuildSpec") -> List[str]:
    """Errors for spec values that would be emitted as code or file names.

    This is the injection gate. It is called by `validate_spec` (so callers
    see the messages early) AND by `resolve_spec` (so no emitter can be
    reached without it, whoever the caller is).
    """
    errors: List[str] = []

    if not isinstance(spec.app_name, str) or not _APP_NAME_RE.fullmatch(spec.app_name):
        errors.append("app_name must be lowercase slug")

    if spec.admin_email and (
        not isinstance(spec.admin_email, str)
        or len(spec.admin_email) > 254
        or not _ADMIN_EMAIL_RE.fullmatch(spec.admin_email)
    ):
        errors.append(f"admin_email is not a plain email address: {spec.admin_email!r}")

    for attr in ("settings", "pages", "seed_content"):
        if not isinstance(getattr(spec, attr), dict):
            errors.append(f"{attr} must be an object")
    for attr in ("data_models", "custom_pages", "user_roles", "translation_warnings"):
        if not isinstance(getattr(spec, attr), list):
            errors.append(f"{attr} must be a list")
    if errors:
        # The shape checks below index into these containers.
        return errors
    # Size before shape: every check below, and every emitter after it,
    # walks the spec, so nothing else is looked at until the spec is known
    # to be of bounded size.
    errors.extend(_spec_size_errors(spec))
    if errors:
        return errors

    # An unknown archetype is reported by validate_spec; here it only means
    # there are no "<archetype page>-detail" ids to reserve.
    try:
        archetype_pages = set(get_archetype(spec.archetype).get("default_pages") or [])
    except (ValueError, TypeError):
        archetype_pages = set()
    for page_id, page in spec.pages.items():
        if not isinstance(page_id, str) or not _PAGE_ID_RE.fullmatch(page_id):
            errors.append(f"page id is not a safe slug: {page_id!r}")
        else:
            reserved_because = _page_id_reservation(page_id, archetype_pages)
            if reserved_because:
                errors.append(f"page id {page_id!r} {reserved_because}")
            # Every reader of a page's data indexes it by key.
            if not isinstance(page, dict):
                errors.append(f"pages.{page_id} must be an object")

    # Settings the emitters handle as text (slicing, splitting, regex
    # matching). A number or a list here is a malformed spec; name the key
    # instead of letting an emitter fail on it mid-build.
    for key in _TEXT_SETTING_KEYS:
        val = spec.settings.get(key)
        if val is not None and not isinstance(val, str):
            errors.append(f"settings.{key} must be a string")
    for key, kind in _SETTING_KEY_KINDS.items():
        problem = _kind_problem(kind, spec.settings.get(key))
        if problem:
            errors.append(f"settings.{key} {problem}")

    seen_tables: Dict[str, str] = {}
    for m in spec.data_models:
        if not isinstance(m, dict):
            errors.append("data_model entries must be objects")
            continue
        raw_name = m.get("name")
        name = raw_name if isinstance(raw_name, str) else ""
        if not _MODEL_NAME_RE.fullmatch(name):
            errors.append(f"data_model name is not a safe identifier: {raw_name!r}")
            continue
        slug_ok = True
        for key in ("plural", "slug"):
            val = m.get(key)
            if val and not (isinstance(val, str) and _MODEL_SLUG_RE.fullmatch(val)):
                errors.append(f"data_model {key} is not a safe slug: {val!r}")
                slug_ok = False
        if slug_ok:
            slug = _slug_for(m)
            table = slug.replace("-", "_")
            if table in _RESERVED_TABLE_NAMES:
                errors.append(
                    f"data_model {name!r} maps to reserved table {table!r}"
                )
            elif slug in _RESERVED_MODEL_SLUGS:
                errors.append(
                    f"data_model {name!r} maps to reserved route slug {slug!r}"
                )
            elif table in seen_tables:
                errors.append(
                    f"data_models {seen_tables[table]!r} and {name!r} map to "
                    f"the same table {table!r}"
                )
            else:
                seen_tables[table] = name
        plp = m.get("public_listing_path")
        if plp is not None and plp != "":
            reason = _public_listing_path_error(plp)
            if reason:
                errors.append(
                    f"data_model {name!r} public_listing_path {plp!r} {reason}"
                )
        btf = m.get("belongs_to_field")
        if btf and not (isinstance(btf, str) and _FIELD_NAME_RE.fullmatch(btf.strip())):
            errors.append(
                f"data_model {name!r} belongs_to_field is not a safe identifier: {btf!r}"
            )
        for key in ("belongs_to", "on_delete"):
            val = m.get(key)
            if val is not None and not isinstance(val, str):
                errors.append(f"data_model {name!r} {key} must be a string")
        fields = m.get("fields")
        if fields is not None and not isinstance(fields, list):
            errors.append(f"data_model {name!r} fields must be a list")
            continue
        # Column names are emitted unquoted, and Postgres folds an unquoted
        # identifier to lower case: "Email" and "email" are ONE column. Two
        # fields that fold to the same name (or one that folds onto a column
        # the engine adds itself) would make CREATE TABLE fail at first
        # boot, and a repeated name would make every admin INSERT and
        # UPDATE name the column twice.
        seen_columns: Dict[str, str] = {}
        for f in (fields or []):
            if not isinstance(f, dict):
                errors.append(f"data_model {name!r} field entries must be objects")
                continue
            fname = f.get("name")
            if not (isinstance(fname, str) and _FIELD_NAME_RE.fullmatch(fname)):
                errors.append(
                    f"data_model field name is not a safe identifier: {fname!r}"
                )
            else:
                folded = fname.lower()
                if folded in seen_columns:
                    errors.append(
                        f"data_model {name!r} declares the column {folded!r} more than "
                        f"once (fields {seen_columns[folded]!r} and {fname!r}; column "
                        "names are compared without regard to letter case)"
                    )
                else:
                    seen_columns[folded] = fname
                if folded in _ENGINE_COLUMNS and fname != folded:
                    errors.append(
                        f"data_model {name!r} field {fname!r} differs only by letter case "
                        f"from the column {folded!r} that the engine adds to every table"
                    )
                else:
                    errors.extend(_engine_column_type_errors(name, f))
            sql_type = f.get("sql_type")
            if sql_type is not None and sql_type != "" and not _is_safe_sql_type(sql_type):
                errors.append(
                    f"data_model {name!r} field {fname!r} sql_type is not a "
                    f"plain column type: {sql_type!r}"
                )
            # The rest of what the form, validation and schema emitters read
            # of a field.
            for key, kind in _MODEL_FIELD_KEY_KINDS.items():
                problem = _kind_problem(kind, f.get(key))
                if problem:
                    errors.append(f"data_model {name!r} field {fname!r} {key} {problem}")
            field_type = f.get("field_type")
            if isinstance(field_type, str) and field_type and field_type not in load_field_types()["field_types"]:
                errors.append(
                    f"data_model {name!r} field {fname!r} field_type is not a known field type: "
                    f"{field_type[:40]!r}"
                )
            rows = f.get("rows")
            if isinstance(rows, int) and not isinstance(rows, bool) and not 1 <= rows <= _MAX_TEXTAREA_ROWS:
                errors.append(
                    f"data_model {name!r} field {fname!r} rows must be between 1 and {_MAX_TEXTAREA_ROWS}"
                )

    for cp in spec.custom_pages:
        if not isinstance(cp, dict):
            # Non-dict entries are skipped by every consumer.
            continue
        path = cp.get("path")
        if path is not None and not isinstance(path, str):
            errors.append(f"custom_page path must be a string: {path!r}")
        elif isinstance(path, str) and _is_offsite_reference(path):
            # Other malformed paths are dropped with a warning (see
            # _classify_custom_pages). This shape is refused outright: a
            # page "path" that names another origin has no honest reading,
            # and it is exactly what a planted navigation link looks like.
            errors.append(
                "custom_page path must be a path on this site, not a URL: "
                f"{path[:80]!r}"
            )
        for key, kind in _CUSTOM_PAGE_KEY_KINDS.items():
            problem = _kind_problem(kind, cp.get(key))
            if problem:
                errors.append(f"custom_page {key} {problem}")
        roles = cp.get("allowed_roles")
        if roles is not None and not isinstance(roles, list):
            errors.append("custom_page allowed_roles must be a list")
        else:
            # An entry the guard cannot check is refused rather than
            # skipped: skipping it would leave the page open to every
            # signed-in account, which is wider than the spec asked for.
            for role in (roles or []):
                if not isinstance(role, str) or not role.strip():
                    errors.append(
                        f"custom_page allowed_roles entries must be role names (non-empty strings): {role!r}"
                    )
                elif len(role.strip()) > _MAX_ROLE_NAME_LEN:
                    errors.append(
                        "custom_page allowed_roles entry is longer than the "
                        f"{_MAX_ROLE_NAME_LEN} characters a role can have: {role[:60]!r}"
                    )

    errors.extend(_personal_stock_photo_errors(spec))
    return errors


# A stock photo is named by its file under /images/stock/ on this site. The
# same path after a host name ("https://other.example/images/stock/x.jpg") is
# another site's address and not a reference to the library.
_STOCK_PHOTO_REF_RE = re.compile(
    r"(?<![A-Za-z0-9._~%:-])/images/stock/([A-Za-z0-9][A-Za-z0-9._-]{0,127})", re.ASCII,
)


def _personal_stock_photo_errors(spec: "BuildSpec") -> List[str]:
    """Refuse a spec that names a stock photo of a specific person or place
    (`compose.stock_photos.is_personal_photo_name`).

    Those files are never chosen for a customer's site by the selector, and
    they are not copied in on request either: publishing a real person's
    face or a recognizable third-party venue as the customer's own is a
    content-integrity violation whoever picked the file, and a spec is
    written by a model that knows the names of the files in the library.
    Looks at every string of the spec (the settings, the pages' data, seed
    rows, custom pages, the hints), the way the copy step looks at what is
    emitted.
    """
    from compose.stock_photos import is_personal_photo_name

    errors: List[str] = []
    seen: set = set()
    stack = [(name, getattr(spec, attr)) for attr, name in _SPEC_CONTAINERS]
    while stack and len(errors) < 5:
        where, value = stack.pop()
        if isinstance(value, str):
            if "/images/stock/" not in value:
                continue
            for found in _STOCK_PHOTO_REF_RE.finditer(value):
                filename = found.group(1)
                if filename not in seen and is_personal_photo_name(filename):
                    seen.add(filename)
                    errors.append(
                        f"{where[:100]} names the stock photo {filename!r}, which shows a specific person "
                        "or place; photos like that are never used for a customer's site"
                    )
        elif isinstance(value, dict):
            stack.extend((f"{where}.{str(key)[:40]}", child) for key, child in value.items())
        elif isinstance(value, list):
            stack.extend((f"{where}[{index}]", child) for index, child in enumerate(value))
    return errors


def _is_safe_sql_type(sql_type: Any) -> bool:
    return (
        isinstance(sql_type, str)
        and len(sql_type) <= 40
        and bool(_SAFE_SQL_TYPE_RE.fullmatch(sql_type.strip()))
    )


# What a column type is, as far as the engine's own SQL is concerned: the
# base names `_SAFE_SQL_TYPE_RE` accepts, folded into the few kinds that
# behave alike. A length, a precision or "[]" after the name changes the
# kind only for "[]" (an array).
_SQL_TYPE_KINDS = {
    "text": "text", "varchar": "text", "char": "text",
    "integer": "integer", "int": "integer", "bigint": "integer", "smallint": "integer",
    "serial": "serial", "bigserial": "serial",
    "numeric": "numeric", "decimal": "numeric", "real": "numeric", "double precision": "numeric",
    "boolean": "boolean", "bool": "boolean",
    "date": "date", "time": "time", "timestamp": "timestamp", "timestamptz": "timestamp",
    "json": "json", "jsonb": "json", "uuid": "uuid",
}


def _sql_type_kind(sql_type: Any) -> str:
    """The kind of a column type: "text", "integer", "serial", "numeric",
    "boolean", "date", "time", "timestamp", "json", "uuid", "array" or
    "other" (not a type `_is_safe_sql_type` accepts).

    The one place that answers "is this column a boolean / a timestamp /
    text", so that the form, the listing, the dashboard and the public read
    of one column cannot disagree about it (a column typed "bool" is a
    BOOLEAN to Postgres, and used to be a text box in the admin form)."""
    text = str(sql_type or "").strip().lower()
    if text.endswith("[]"):
        return "array"
    base = re.sub(r"\s+", " ", text.split("(", 1)[0]).strip()
    return _SQL_TYPE_KINDS.get(base, "other")


def _field_type_kind(field: dict) -> str:
    """`_sql_type_kind` of the type a model field is created with
    (`_sql_type_for`: its sql_type, else the one of its field_type)."""
    return _sql_type_kind(_sql_type_for(field))


# Column names the engine gives a meaning to, with the kinds of type it can
# work with when a spec declares the column itself (the engine adds `id` and
# `created_at` to every table). The list is the one of the emitters'
# assumptions about a column of a model, found by reading every statement
# and every view a model's table reaches:
#
#   id            the primary key. The engine adds `id SERIAL PRIMARY KEY`
#                 to every table; a declared `id` is dropped from the model
#                 (it would be a form field, an INSERT column and a seed
#                 value for the key), and only a whole-number type agrees
#                 with what the engine adds. (`_without_engine_id`)
#   created_at    `created_at TIMESTAMPTZ DEFAULT NOW()` is added unless the
#                 spec declares it. The admin dashboard compares it with
#                 NOW() - INTERVAL '7 days' and the contact list is ordered
#                 by it, so it must be a date or a timestamp: Postgres
#                 rejects the comparison for text, a number or a time of
#                 day, and the dashboard is then a 500 for the owner.
#   published_at  a model's public listing is ordered by it. Every type
#                 sorts except json (which has no ordering operator).
#
# (`_sortable` keeps json columns out of `?sort=` for the same reason.)
#
# The columns below need no rule on the type, because every reader handles
# any type: `updated_at` (shown in the admin detail, never queried),
# `published` (only a BOOLEAN column is the publication flag; any other type
# is an ordinary column, `_publication_column`), `slug` (the detail lookup
# of `emit_routes_pages_js` casts it to text), the model's name column
# (`_display_column` skips a json column), a date column (`_date_column`
# picks one by its type). Foreign keys are checked where the parent model is
# known (`_engine_column_errors`).
_ENGINE_COLUMN_KINDS = {
    "id": (
        ("integer", "serial"), "integer, int, bigint, smallint or serial",
        "the primary key the engine adds to every table",
    ),
    "created_at": (
        ("date", "timestamp"), "date, timestamp or timestamptz",
        "the time a row was added: the admin dashboard counts the rows of the last seven days "
        "and lists the newest by it",
    ),
    "published_at": (
        ("text", "integer", "serial", "numeric", "boolean", "date", "time", "timestamp", "uuid", "array"),
        "any type but json",
        "the date a public listing is ordered by",
    ),
}


def _engine_column_type_errors(model_name: str, field: dict) -> List[str]:
    """Why a model field named like a column the engine reads cannot have
    the type the spec gave it (`_ENGINE_COLUMN_KINDS`), as messages."""
    name = field.get("name")
    if not isinstance(name, str):
        return []
    rule = _ENGINE_COLUMN_KINDS.get(name)
    if rule is None:
        return []
    kinds, accepted, meaning = rule
    if _field_type_kind(field) in kinds:
        return []
    column_type = str(_sql_type_for(field))[:40]
    return [
        f"data_model {model_name!r} field {name!r} has the type {column_type!r}, but {name} is "
        f"{meaning}; give it one of these types ({accepted}) or leave the field out"
    ]


def _without_engine_id(model: dict) -> dict:
    """The model without a field named `id`: the engine adds the primary key
    itself, so a field of that name is not a column of the model's own (the
    gate has checked that its type agrees). Returns `model` itself when it
    has no such field, so nothing is copied for the usual model."""
    fields = model.get("fields") if isinstance(model, dict) else None
    if not isinstance(fields, list) or not any(
        isinstance(f, dict) and f.get("name") == "id" for f in fields
    ):
        return model
    kept = dict(model)
    kept["fields"] = [f for f in fields if not (isinstance(f, dict) and f.get("name") == "id")]
    return kept


def validate_spec(spec: BuildSpec) -> List[str]:
    """Return list of validation errors; empty means valid."""
    errors: List[str] = []
    if not spec.app_name:
        errors.append("app_name is required")
    try:
        get_archetype(spec.archetype)
    except ValueError as e:
        errors.append(str(e))
    if spec.palette:
        try:
            get_palette(spec.palette)
        except ValueError as e:
            errors.append(str(e))
    if spec.font_pair:
        try:
            get_font_pair(spec.font_pair)
        except ValueError as e:
            errors.append(str(e))
    if not spec.admin_email:
        errors.append("admin_email is required")
    errors.extend(_spec_safety_errors(spec))
    if not errors:
        errors.extend(_resolution_errors(spec))
    return errors


def _resolution_errors(spec: BuildSpec) -> List[str]:
    """The reasons `resolve_spec` would refuse a spec that passed the
    checks above: a protected page that something else answers without a
    login, a public form into a model that cannot hold a row back, page
    data that does not fit the widget it is for. They need the resolved
    layouts and models, so resolve_spec is where they are found; this runs
    it so that `validate_spec` returning nothing means the spec builds.

    resolve_spec writes to the spec it is given in two places only (it
    replaces entries of `pages` for faithful-copy pages and records engine
    warnings), so it is run on a copy with its own `pages` mapping and
    warning list and the caller's spec is left as it was.
    """
    probe = dataclasses.replace(
        spec, pages=dict(spec.pages), engine_warnings=list(spec.engine_warnings),
    )
    try:
        resolve_spec(probe)
    except ValueError as exc:
        text = str(exc)
        prefix = "Spec validation failed: "
        return [text[len(prefix):] if text.startswith(prefix) else text]
    return []


# ---------------------------------------------------------------------------
# Resolution: archetype + palette + fonts + layouts
# ---------------------------------------------------------------------------

@dataclass
class ResolvedSpec:
    """Spec + all looked-up decision-file content."""
    spec: BuildSpec
    archetype: dict
    palette: dict
    font_pair: dict
    page_layouts: Dict[str, dict]
    data_models: List[Dict[str, Any]]
    # Populated by resolve_spec from compose/data/templates.json. None when
    # template-family selection is bypassed (legacy specs without
    # the picker wired in); emit_layout_files falls back to the
    # classic-business shell defaults in that case.
    template_family: Optional[Dict[str, Any]] = None

    @property
    def site_name(self) -> str:
        return self.spec.settings.get("site_name") or _prettify(self.spec.app_name)

    @property
    def palette_vars(self) -> Dict[str, str]:
        return self.palette["vars"]

    @property
    def fonts_url(self) -> str:
        return self.font_pair["google_fonts_url"]


def _prettify(slug: str) -> str:
    """Convert a kebab/snake/space-separated slug into a human display
    name. The trailing `-vN` / `-vN-NN` version suffix from rebuild
    naming is stripped first.

    Special-case: when "and" appears between two non-empty words in the
    slug (e.g. `fern-and-stone`, harbor's slug for "Fern & Stone"),
    the standalone token "and" is rendered as "&" so the brand display
    keeps the keeper's original ampersand. Without this, harbor's slug
    pipeline (which replaces `&` with `-and-` to produce kebab-case)
    converts "Fern & Stone" → "fern-and-stone" → "Fern And Stone"
    on render, which the keeper reads as the wrong brand name. The
    keeper's exact brand can also be supplied via `settings.site_name`
    (Opus is now prompted to emit copy.site_name with original
    punctuation; this _prettify path is the defensive fallback)."""
    raw = re.sub(r"-v\d+(?:-\d+)?$", "", slug or "")
    words = [w for w in re.split(r"[-_\s]+", raw) if w]
    if not words:
        return "Site"
    out: list = []
    for i, w in enumerate(words):
        # Standalone "and" between two non-empty words → "&". Restricted
        # to mid-position so a slug like `andrew-and-co` does not lose
        # the leading "Andrew" capitalization.
        if w.lower() == "and" and 0 < i < len(words) - 1:
            out.append("&")
        else:
            out.append(w.capitalize())
    return " ".join(out)


_INITIALISMS = {
    "url", "id", "seo", "faq", "cta", "tos", "pdf", "api", "css",
    "html", "json", "ui", "ux", "usa", "us", "dc", "ny", "nyc",
    "sla", "csv", "hvac", "llc", "inc", "co",
}


# Field-name → display label overrides (run BEFORE the generic humanize).
# Captures cases where the column name is a technical token but the display
# label should read differently to a human reader. "slug" → "Path" because
# "slug" is jargon from CMS land; users recognize "Path" (it's the URL
# segment they type). "image_url" → "Image" because we show a thumbnail
# on detail views, not the raw URL.
_FIELD_LABEL_OVERRIDES = {
    "slug": "Path",
    "image_url": "Image",
    "photo_url": "Photo",
    "avatar_url": "Avatar",
    "cover_url": "Cover",
    "thumbnail_url": "Thumbnail",
    "logo_url": "Logo",
    "hero_image_url": "Hero image",
    "about_photo_url": "About photo",
    "nav_logo_url": "Navigation logo",
    "registration_url": "Registration link",
    "booking_url": "Booking link",
    "website_url": "Website",
    "instagram_url": "Instagram",
    "facebook_url": "Facebook",
    "twitter_url": "Twitter",
    "linkedin_url": "LinkedIn",
    "youtube_url": "YouTube",
    "tiktok_url": "TikTok",
    "zip_code": "ZIP code",
    "zip_codes": "ZIP codes",
}


def _humanize(name: str) -> str:
    """Turn CamelCase, snake_case, or kebab-case into a human-readable phrase.

    1. Checks the overrides dict first (slug → "Path", image_url → "Image").
    2. Falls back to generic humanization with initialism handling
       (URL, ID, SEO, FAQ, CTA, HVAC stay uppercase).

    Examples:
        "BlogPost"        -> "Blog Post"
        "ContactInquiry"  -> "Contact Inquiry"
        "blog-posts"      -> "Blog Posts"
        "slug"            -> "Path"             (override)
        "image_url"       -> "Image"            (override)
        "published_at"    -> "Published At"     (generic)
    """
    if not name:
        return "Item"
    if name.lower() in _FIELD_LABEL_OVERRIDES:
        return _FIELD_LABEL_OVERRIDES[name.lower()]
    # Split CamelCase at boundaries (lowercase→uppercase or acronym→word).
    with_spaces = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    with_spaces = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", with_spaces)
    # Then treat hyphens/underscores as spaces.
    parts = re.split(r"[\s\-_]+", with_spaces)
    out = []
    for p in parts:
        if not p:
            continue
        out.append(p.upper() if p.lower() in _INITIALISMS else p.capitalize())
    return " ".join(out) or "Item"


def resolve_spec(spec: BuildSpec) -> ResolvedSpec:
    # The injection gate runs here as well as in validate_spec: a
    # ResolvedSpec is what the emitters take, and this function is how
    # callers (worker, CLI, scripts, tests) obtain one, so enforcing the
    # gate here covers every caller that did not run validate_spec first.
    safety_errors = _spec_safety_errors(spec)
    if safety_errors:
        raise ValueError("Spec validation failed: " + "; ".join(safety_errors))
    archetype = get_archetype(spec.archetype)
    palette_id = spec.palette or archetype["default_palette_suggestions"][0]
    palette = get_palette(palette_id)
    font_id = spec.font_pair or archetype["default_font_pair_suggestions"][0]
    fp = get_font_pair(font_id)

    # Resolve page layouts per spec page
    #
    # When the customer declares a page of the same kind as an archetype
    # default (/contact-us beside the archetype's /contact), the archetype
    # default is dropped so the site does not carry two near-identical
    # pages.
    #
    # One kind of archetype page is NOT dropped that way: a page whose
    # layout lists a model's rows (the journal, the gallery). A custom page
    # never lists rows (see `_public_row_sources`), so it cannot stand in
    # for that listing, and dropping the archetype page would take the rows
    # off the site altogether. The listing page is dropped only when the
    # spec itself lists one of the same tables at the customer page's path
    # (`public_listing_path` on the model), which is the spec's own
    # replacement for it.
    _PAGE_ALIAS_GROUPS = [
        {"journal", "blog", "news", "posts", "articles"},
        {"gallery", "photos"},
        {"contact", "contact-us", "get-in-touch"},
    ]
    archetype_defaults = archetype.get("page_layouts", {})
    _customer_page_slugs = set()
    for cp in (spec.custom_pages or []):
        if not isinstance(cp, dict):
            continue
        p = (cp.get("path") or "").strip().lstrip("/")
        if p:
            _customer_page_slugs.add(p)
    _spec_listing_tables: Dict[str, set] = {}
    for m in spec.data_models:
        if isinstance(m, dict) and m.get("public_listing_path"):
            _spec_listing_tables.setdefault(
                _path_key(m["public_listing_path"]), set()
            ).add(_table_name_for_model(m))
    _dropped_archetype_pages = set()
    for group in _PAGE_ALIAS_GROUPS:
        customer_in_group = _customer_page_slugs & group
        if not customer_in_group:
            continue
        for page_id in archetype["default_pages"]:
            if page_id not in group or page_id in customer_in_group:
                continue
            listed_tables = _layout_db_tables(archetype_defaults.get(page_id))
            if listed_tables and not any(
                listed_tables & _spec_listing_tables.get(_path_key(f"/{slug}"), set())
                for slug in customer_in_group
            ):
                continue
            _dropped_archetype_pages.add(page_id)

    page_layouts: Dict[str, dict] = {}
    # When harbor_meta carries design hints, the variant scorer can pick a
    # sibling layout in the same variant_group over the archetype's
    # hardcoded default. Falls back to the default for pages without variant
    # siblings, archetypes without harbor_meta, or unknown archetypes
    # (scorer returns None).
    from compose.intake import pick_layout_variant as _pick_variant
    harbor_meta = spec.harbor_meta if isinstance(spec.harbor_meta, dict) else {}
    _raw_exclude_pages = harbor_meta.get("exclude_pages")
    _hm_exclude_pages = {
        p for p in (_raw_exclude_pages if isinstance(_raw_exclude_pages, list) else [])
        if isinstance(p, str)
    }
    for page_id in archetype["default_pages"]:
        if page_id in _dropped_archetype_pages:
            continue
        if page_id in _hm_exclude_pages:
            continue
        layout_id = archetype_defaults.get(page_id)
        if not layout_id:
            continue
        # Try the variant scorer; only override when it returns a
        # different key than the archetype default (no-op when scorer
        # returns the same key or when no siblings exist).
        try:
            picked = _pick_variant(spec.archetype, page_id, harbor_meta, seed=spec.app_name)
            if picked and picked != layout_id:
                # Only honor the pick if it shares the variant_group
                # with the archetype default; defense against an
                # explicit hint that names a layout from a different
                # archetype.
                default_layout = get_layout(layout_id)
                picked_layout = get_layout(picked)
                if default_layout.get("variant_group") and (
                    picked_layout.get("variant_group") == default_layout.get("variant_group")
                ):
                    layout_id = picked
        except Exception:
            # Variant selection is best-effort; never break the build.
            pass
        page_layouts[page_id] = get_layout(layout_id)

    # Faithful-copy mode: any page (archetype-default or custom) whose spec
    # block carries _copy_blocks gets its layout swapped to
    # copy-faithful-default. The markdown blocks are pre-rendered through
    # markdown_safe to a single HTML string and stamped onto the page spec
    # under the "copy" key, which the content-raw-markdown widget consumes
    # via the standard source-path resolution (data_key="copy" ->
    # {copy_html, source_url}). This short-circuits the archetype's default
    # widget composition so a verbatim source import doesn't accumulate
    # fabricated archetype sections (pinned by
    # compose/tests/test_faithful_copy_mode.py).
    from compose.markdown_safe import render_markdown_blocks as _render_md
    for _page_id, _page_spec in list(spec.pages.items()):
        if not isinstance(_page_spec, dict):
            continue
        _blocks = _page_spec.get("_copy_blocks")
        if not _blocks or not isinstance(_blocks, list):
            continue
        # A faithful-copy page id is the one page id the spec (not the
        # archetype decision file) chooses, and it becomes a route path and
        # a view file name. _spec_safety_errors already refused anything
        # outside _PAGE_ID_RE or reserved (_page_id_reservation); this is the
        # emit-side restatement of that invariant, at the single point where
        # a spec key is promoted.
        if not isinstance(_page_id, str) or not _PAGE_ID_RE.fullmatch(_page_id):
            raise ValueError(f"Spec validation failed: page id is not a safe slug: {_page_id!r}")
        _reserved_because = _page_id_reservation(_page_id, archetype["default_pages"])
        if _reserved_because:
            raise ValueError(f"Spec validation failed: page id {_page_id!r} {_reserved_because}")
        # Pre-render once. The widget receives the rendered HTML string
        # under copy.copy_html so emit_page_view's standard data-key
        # resolution finds it. Stamp onto a shallow copy so resolve_spec does
        # not mutate the caller's original page dict in place (re-resolving the
        # same spec stays side-effect-free and deterministic).
        _page_copy = dict(_page_spec)
        _page_copy["copy"] = {
            "copy_html": _render_md(_blocks),
            "source_url": _page_spec.get("_copy_source_url") or "",
        }
        spec.pages[_page_id] = _page_copy
        try:
            page_layouts[_page_id] = get_layout("copy-faithful-default")
        except ValueError:
            # Layout missing should never happen in production (it ships
            # alongside this code); fall back silently to whatever the
            # archetype default would have been.
            pass

    # Data models: merge semantics. The archetype defaults are the
    # baseline; a model the spec declares replaces the default of the same
    # name (compared without regard to letter case) and any other is
    # appended. Replacing the whole default set instead would cost a yoga
    # studio that adds one Session model its Class, Instructor, Event and
    # ContactMessage models. A spec model that takes the place of a default
    # the archetype shows to visitors is reported by `_publication_warnings`.
    arch_defaults = list(archetype["default_data_models"])
    if not spec.data_models:
        data_models = arch_defaults
    else:
        customer_names = {
            (m.get("name") or "").lower()
            for m in spec.data_models
            if isinstance(m, dict)
        }
        # Keep archetype defaults the customer did NOT shadow.
        kept_defaults = [
            m for m in arch_defaults
            if isinstance(m, dict) and (m.get("name") or "").lower() not in customer_names
        ]
        # Append customer-declared models (order: archetype first, then customer).
        data_models = kept_defaults + list(spec.data_models)
    # harbor_meta.exclude_data_models lets keepers drop unwanted archetype
    # defaults (e.g. ServiceArea for a business with no service area).
    _raw_exclude_models = harbor_meta.get("exclude_data_models")
    _hm_exclude_models = {
        m.lower()
        for m in (_raw_exclude_models if isinstance(_raw_exclude_models, list) else [])
        if isinstance(m, str)
    }
    if _hm_exclude_models:
        data_models = [m for m in data_models if (m.get("name") or "").lower() not in _hm_exclude_models]

    # The engine adds the primary key itself: a field the spec declared as
    # `id` (the gate has checked its type) is not a column of the model's own.
    data_models = [_without_engine_id(m) for m in data_models]

    # After the merge, every model must own its table. A customer model can
    # shadow an archetype default by NAME (handled above), but one that
    # reuses a default's table through an explicit `plural` would emit two
    # CREATE TABLEs and two routers for one table, with different columns.
    _table_owner: Dict[str, str] = {}
    for m in data_models:
        if not isinstance(m, dict):
            continue
        _table = _table_name_for_model(m)
        if _table in _table_owner and _table_owner[_table] != m.get("name"):
            raise ValueError(
                "Spec validation failed: data_models "
                f"{_table_owner[_table]!r} and {m.get('name')!r} map to the same table {_table!r}"
            )
        _table_owner[_table] = m.get("name")

    # Publication. Nothing here (or anywhere in the engine) decides from a
    # NAME that a model's rows are public. A custom page at /clients beside
    # a model called Client is an ordinary page, and the model stays
    # admin-only: its router answers visitors with a 404 below that path
    # and hands the path itself to the page. Rows are served without a
    # login only where `_public_row_sources` says so: the spec set
    # `public_listing_path` on the model, or a page layout the archetype
    # chose reads the table. `_custom_page_warnings` puts every custom page
    # that is named like a model into the build report, because a spec
    # written with a listing in mind gets a page without one.
    #
    # A page that lists roles is a protected page, whether or not the spec
    # also said so (`_roles_imply_login`). Every reader of `auth_required`
    # below, and every emitter after this function, sees the flag set.
    _protected_pages = []
    for cp in spec.custom_pages:
        if _roles_imply_login(cp):
            _engine_warning(
                spec, "allowed_roles_imply_login",
                f"custom page {_text(cp.get('path'))[:80]!r} lists allowed_roles "
                f"({', '.join(_custom_page_roles(cp))[:80]}) but is not marked auth_required; "
                "a list of roles is a restriction, so the page requires a login and one of those roles",
            )
            cp = dict(cp, auth_required=True)
        _protected_pages.append(cp)
    spec.custom_pages = _protected_pages

    # Custom pages whose path the engine refuses to emit (anything that is
    # not a plain route path; see _custom_pages_filter) are dropped rather
    # than repaired. Record that once so the reviewer sees the page is gone.
    for cp in spec.custom_pages:
        if not isinstance(cp, dict):
            continue
        _cp_path = (cp.get("path") or "").strip()
        if _cp_path and not _is_safe_route_path(_cp_path) and not _is_parametric_path(_cp_path):
            _engine_warning(
                spec, "custom_page_path_dropped",
                f"custom page path {_cp_path[:80]!r} is not a plain route "
                "path (letters, digits, '-', '_' and '/' only); the page "
                "was not emitted",
            )

    # Resolve the template family declared on the BuildSpec; falls back to
    # None when template_family_id is empty (legacy specs) or missing from
    # templates.json. emit_layout_files reads ResolvedSpec.template_family
    # to pick the shell partials + typography scale.
    template_family: Optional[Dict[str, Any]] = None
    if spec.template_family_id:
        template_family = get_template_family(spec.template_family_id)

    resolved = ResolvedSpec(
        spec=spec,
        archetype=archetype,
        palette=palette,
        font_pair=fp,
        page_layouts=page_layouts,
        data_models=data_models,
        template_family=template_family,
    )
    # Columns the engine reads with a fixed meaning, and the order tables
    # must be created in (`_engine_column_errors`, `_models_in_creation_order`).
    _column_errors = _engine_column_errors(resolved)
    if _column_errors:
        raise ValueError("Spec validation failed: " + "; ".join(_column_errors[:_MAX_REPORTED_ERRORS]))
    # A page marked auth_required whose path something else would answer
    # without a login cannot be honored, and the build is refused rather
    # than shipped with that path public (`_auth_required_page_errors`).
    _auth_errors = _auth_required_page_errors(resolved)
    if _auth_errors:
        raise ValueError("Spec validation failed: " + "; ".join(_auth_errors))
    # A form that visitors can submit may not store into a model whose rows
    # are shown to visitors unless the model can hold a row back
    # (`_public_form_errors`). Refused for the same reason as above: the
    # site is public the moment it is deployed.
    _form_errors = _public_form_errors(resolved)
    if _form_errors:
        raise ValueError("Spec validation failed: " + "; ".join(_form_errors))
    # Page data that does not fit the parameters of the widget it is for
    # (`_page_data_errors`): a view would call a string method on a list,
    # or loop over a number, and throw when the page is requested.
    _page_errors = _page_data_errors(resolved)
    if _page_errors:
        raise ValueError(
            "Spec validation failed: " + "; ".join(_page_errors[:_MAX_REPORTED_ERRORS])
        )
    # Custom pages the engine declines to emit for a reason a reviewer needs
    # to see (a duplicate, a public page under /admin, a path that a model's
    # routes answer first, an auth_required page that was not emitted), the
    # pages that are named like a model and list nothing, and the spec's own
    # models that an archetype page shows. Recorded once here; the emitters
    # all read the same classification.
    for _warning in _custom_page_warnings(resolved) + _publication_warnings(resolved):
        _engine_warning(spec, _warning["reason"], _warning["detail"])
    return resolved


# ---------------------------------------------------------------------------
# Emission helpers
# ---------------------------------------------------------------------------

def _read_widget(widget_id: str) -> str:
    wd = get_widget(widget_id)
    return (COMPOSE_DIR / wd["file"]).read_text(encoding="utf-8")


# --- Context escapers ----------------------------------------------------
# One helper per emit context. Using the wrong one is the bug class this
# section exists to prevent: a value that is safe inside a SQL literal is not
# safe inside a SQL comment, and JSON that is valid JavaScript is not safe
# inside an EJS tag.

def _sql_esc(s: Any) -> str:
    """Body of a single-quoted Postgres string literal.

    Doubling the quote is sufficient because the emitted SQL runs with
    standard_conforming_strings on (the Postgres default since 9.1), so a
    backslash is an ordinary character. NUL is removed: Postgres cannot
    store it in text and the seed would abort at first boot.
    """
    if s is None:
        return ""
    return str(s).replace("\x00", "").replace("'", "''")


_SQL_COMMENT_UNSAFE_RE = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029]+")


def _one_line(s: Any) -> str:
    """Collapse every control character (newlines included) to a space, so
    the value occupies exactly one line of a line-oriented output."""
    if s is None:
        return ""
    return _SQL_COMMENT_UNSAFE_RE.sub(" ", str(s)).strip()


# --- Build warnings --------------------------------------------------------
# Two kinds, kept in two lists and written with two prefixes, so that one
# can never be mistaken for the other or crowd it out of the report:
#
#   engine warnings  BuildSpec.engine_warnings, added only by
#                    `_engine_warning`; written as `warning: ` lines.
#   spec warnings    BuildSpec.translation_warnings, whatever the spec
#                    carried under `_translation_warnings`; written as
#                    `spec-warning: ` lines, after every engine warning,
#                    at most `_MAX_SPEC_WARNING_LINES` of them.
#
# Every line of either kind is one physical line of printable text (see
# `_report_text`), so no warning can start a second line, and a line that
# begins with `warning: ` was written by the engine.

# Spec-supplied warnings written into the build report, and the longest
# reason and detail kept of each. A translator reports a handful; the rest
# of a longer list is counted (`spec_warnings_omitted`) and not written.
_MAX_SPEC_WARNING_LINES = 40
_MAX_SPEC_WARNING_REASON = 80
_MAX_SPEC_WARNING_DETAIL = 300
# Longest reason and detail of an engine warning. The engine's own texts
# are far shorter; the bound is for the spec values some of them quote.
_MAX_ENGINE_WARNING_REASON = 80
_MAX_ENGINE_WARNING_DETAIL = 900
# Longest `tier:` value written to the manifest (the spec can supply it).
_MAX_TIER_TEXT = 40
# Entries of a spec's `_translation_warnings` list that are looked at. A
# list can hold any number of entries that produce no line (numbers, empty
# strings); this bounds the scan itself.
_MAX_SPEC_WARNING_ENTRIES_SCANNED = 5000

# Engine warnings written first, in this order, ahead of the rest (which
# keep the order they were recorded in). These are the ones that say what
# a build shows or fails to protect; a reader that stops after N lines
# still gets them.
_ENGINE_WARNING_PRIORITY = (
    "spec_model_shown_by_archetype_page",
    "auth_required_page_not_emitted",
    "page_named_like_model",
)

# Characters with no place in a one-line report, by Unicode category:
# every control character (Cc: C0, DEL, C1), the line and paragraph
# separators (Zl, Zp), the format characters (Cf: bidirectional overrides,
# zero-width marks) and lone surrogates (Cs: they cannot be encoded as
# UTF-8 at all, so one would abort the write).
_REPORT_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _report_text(value: Any, limit: int) -> str:
    """`value` as printable text on one line, at most `limit` characters.

    Each run of characters from `_REPORT_UNSAFE_CATEGORIES` becomes one
    space, so the result has no line break of any kind, no terminal escape
    and nothing that reorders the text around it. A longer value is cut
    and ends in "..." (inside the limit). The slice before the scan keeps
    the work proportional to the limit, whatever the size of the value.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    # Unsafe runs collapse to a single space, so the cleaned text is never
    # longer than the slice it came from; 4x the limit leaves room for
    # collapsing without reading an arbitrarily long value.
    text = text[: limit * 4 + 16]
    cleaned: List[str] = []
    pending_space = False
    for ch in text:
        if unicodedata.category(ch) in _REPORT_UNSAFE_CATEGORIES:
            pending_space = True
            continue
        if pending_space and cleaned:
            cleaned.append(" ")
        pending_space = False
        cleaned.append(ch)
    out = "".join(cleaned).strip()
    if len(out) > limit:
        out = out[: max(0, limit - 3)].rstrip() + "..."
    return out


def _engine_warning(spec: "BuildSpec", reason: str, detail: Any) -> None:
    """Record a warning of the engine's own, once.

    The one way an entry gets into `BuildSpec.engine_warnings`. Compared
    for duplicates against that list only: a spec that carries a copy of an
    engine warning in `_translation_warnings` does not stop the engine from
    recording its own.
    """
    warning = {"reason": str(reason), "detail": "" if detail is None else str(detail)}
    if warning not in spec.engine_warnings:
        spec.engine_warnings.append(warning)


def _engine_warning_lines(spec: "BuildSpec") -> List[str]:
    """The engine's warnings as report lines, `warning: <reason>: <detail>`,
    the `_ENGINE_WARNING_PRIORITY` reasons first."""
    def rank(entry: Dict[str, str]) -> int:
        reason = entry.get("reason")
        if reason in _ENGINE_WARNING_PRIORITY:
            return _ENGINE_WARNING_PRIORITY.index(reason)
        return len(_ENGINE_WARNING_PRIORITY)

    lines: List[str] = []
    # sorted() is stable: within one rank the recorded order is kept.
    for entry in sorted(spec.engine_warnings, key=rank):
        reason = _report_text(entry.get("reason"), _MAX_ENGINE_WARNING_REASON) or "?"
        detail = _report_text(entry.get("detail"), _MAX_ENGINE_WARNING_DETAIL)
        lines.append(f"warning: {reason}: {detail}")
    return lines


def _spec_warning_lines(spec: "BuildSpec") -> Tuple[List[str], int]:
    """The spec's own `_translation_warnings` as report lines, and how many
    entries were left out: `(lines, omitted)`.

    A line is `spec-warning: <reason>: <detail>` for a {reason, detail}
    object and `spec-warning: <text>` for a plain string. Every other entry
    (a number, null, an empty string, a nested list) produces no line and is
    not counted. At most `_MAX_SPEC_WARNING_LINES` lines are returned.
    `omitted` counts the further entries that would have produced one among
    the first `_MAX_SPEC_WARNING_ENTRIES_SCANNED` entries of the list, plus
    every entry after those (they are not looked at).
    """
    lines: List[str] = []
    omitted = 0
    raw = spec.translation_warnings if isinstance(spec.translation_warnings, list) else []
    for entry in raw[:_MAX_SPEC_WARNING_ENTRIES_SCANNED]:
        if isinstance(entry, dict):
            reason = _report_text(entry.get("reason"), _MAX_SPEC_WARNING_REASON) or "?"
            detail = _report_text(entry.get("detail"), _MAX_SPEC_WARNING_DETAIL)
            line = f"spec-warning: {reason}: {detail}"
        elif isinstance(entry, str) and entry.strip():
            text = _report_text(entry, _MAX_SPEC_WARNING_DETAIL)
            if not text:
                continue
            line = f"spec-warning: {text}"
        else:
            continue
        if len(lines) < _MAX_SPEC_WARNING_LINES:
            lines.append(line)
        else:
            omitted += 1
    omitted += max(0, len(raw) - _MAX_SPEC_WARNING_ENTRIES_SCANNED)
    return lines, omitted


def _sql_comment(s: Any) -> str:
    """Text for a `-- ...` SQL line comment.

    A line comment ends at the first newline, so a newline in the value
    would turn the rest of it into live SQL. (Quote doubling is irrelevant
    in a comment; keeping the value on one line is what matters.)
    """
    return _one_line(s)


def _js_str(s: Any) -> str:
    """A complete JavaScript string literal (with quotes) for a .js file.

    JSON string syntax is a subset of JS string syntax, so json.dumps is a
    correct escaper for this context. Use this for anything that is not a
    validated identifier or path.
    """
    return json.dumps("" if s is None else str(s))


def _ejs_json(value: Any) -> str:
    """JSON for embedding as a JS expression INSIDE an EJS tag.

    EJS finds its delimiters by scanning the template text, before any
    JavaScript parsing, so a `%>` inside a JSON string closes the tag early
    and the view no longer compiles (every request for that page then
    fails). `<` and `>` only ever occur inside JSON strings, so replacing
    them with their \\u escapes is lossless and removes every EJS delimiter
    (`<%`, `%>`, `-%>`, `_%>`) as well as `</script>`. U+2028/U+2029 are
    escaped for older JS parsers.
    """
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _html_attr(s: Any) -> str:
    """Value for a quoted HTML attribute in a build-time-assembled view."""
    return (
        ("" if s is None else str(s))
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# --- Spec content that widgets emit as markup or as a link target ---------
# Widgets HTML-escape text with `<%= %>`, but escaping does not make a URL
# safe to navigate to, and a few widget parameters are emitted raw on
# purpose. Both are normalized here, once, at build time, for every value
# the SPEC supplies (page copy, seed rows, settings). Values an admin types
# into the running site later are not covered by this pass; for those the
# generated app relies on its CSP (no inline script, so a `javascript:` link
# does not execute) and on the `url` field type's http(s) pattern.

# Keys whose value is used as a link target or resource address.
# These two are NOT allowlists: a match sends the value through
# `_neutralize_url`, and a scheme that matches must then be on the
# allowlist below. They are left Unicode-aware on purpose, so that a
# look-alike ("java" + long s + "cript:", a key spelled with a dotless i)
# is treated as the thing it imitates and neutralized, never waved through.
_URL_KEY_RE = re.compile(r"(?:^|_)(?:url|href|link|src)$", re.IGNORECASE)
_URL_SCHEME_RE = re.compile(r"([a-z][a-z0-9+.\-]*):", re.IGNORECASE)
_ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto", "tel", "sms"})
# Widget parameters that are written to the page WITHOUT escaping.
_RAW_HTML_KEYS = frozenset({"body_html"})
# Characters a browser discards while it parses a URL's scheme, so
# "java\tscript:" and " javascript:" are both `javascript:` to it.
_URL_IGNORED_CHARS_RE = re.compile(r"[\x00-\x20\x7f]+")


def _neutralize_url(value: Any) -> Any:
    """Replace a URL whose scheme is not on the allowlist with "#".

    Relative references (/path, ./x, #frag, ?q, about.html) have no scheme
    and pass through unchanged, as do http, https, mailto, tel and sms. A
    scheme-relative "//host" is left alone: it is an ordinary external link
    and no more dangerous than "https://host".
    """
    if not isinstance(value, str):
        return value
    probe = _URL_IGNORED_CHARS_RE.sub("", value)
    match = _URL_SCHEME_RE.match(probe)
    if match and match.group(1).lower() not in _ALLOWED_URL_SCHEMES:
        return "#"
    return value


def _sanitize_spec_content(value: Any, key: str = "") -> Any:
    """Recursively normalize spec-supplied content before it is emitted.

    - A string under a URL-shaped key (`href`, `cta_href`, `image_url`,
      `url`, `link`, `src`, ...) goes through `_neutralize_url`.
    - A string under a raw-HTML key (`body_html`) goes through
      `markdown_safe.sanitize_html_fragment`.
    - `_EJSRef` values (engine-authored expressions) and everything else
      pass through untouched. Returns new containers; never mutates the
      spec it was handed.
    """
    if isinstance(value, dict):
        return {k: _sanitize_spec_content(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_spec_content(v, key) for v in value]
    if isinstance(value, str):
        if key in _RAW_HTML_KEYS:
            from compose.markdown_safe import sanitize_html_fragment
            return sanitize_html_fragment(value)
        if _URL_KEY_RE.search(key):
            return _neutralize_url(value)
    return value


def _route_path(path: Any) -> str:
    """Return `path` unchanged when it is a plain route path; raise otherwise.

    Emitters call this at the point where a path is interpolated into JS
    source or view markup. For spec-supplied paths it restates what
    _spec_safety_errors already enforced; for paths from the decision files
    it is a cheap guard against a bad data edit reaching generated code.
    """
    if not _is_safe_route_path(path):
        raise ValueError(f"Spec validation failed: not a plain route path: {path!r}")
    return path


def _safe_sql_type(field: dict) -> str:
    """Column type for a field, refusing anything that is not a plain type.

    Spec-supplied fields were already checked by _spec_safety_errors; this
    is the same check at the point of emission, so the DDL emitter does not
    depend on who built the ResolvedSpec.
    """
    col_type = field.get("sql_type") or _sql_type_for(field)
    if not _is_safe_sql_type(col_type):
        raise ValueError(
            f"Spec validation failed: field {field.get('name')!r} sql_type "
            f"is not a plain column type: {col_type!r}"
        )
    return col_type.strip()


def _slug_for(model: dict) -> str:
    return model.get("plural") or model.get("slug") or _plural(model["name"])


def _plural(name: str) -> str:
    """Convert a PascalCase model name to a kebab-case plural slug."""
    s = re.sub(r"([a-z])([A-Z])", r"\1-\2", name).lower()
    if s.endswith("y") and not s[-2:-1] in "aeiou":
        return s[:-1] + "ies"
    if s.endswith("s"):
        return s + "es"
    return s + "s"


def _singular_slug(name: str) -> str:
    """Convert PascalCase to lowercase-kebab singular."""
    return re.sub(r"([a-z])([A-Z])", r"\1-\2", name).lower()


# ---------------------------------------------------------------------------
# File emitters: one per output file type. Each pure function: takes
# resolved_spec, returns (path, contents). build_app assembles the files list.
# ---------------------------------------------------------------------------

def emit_ops_scripts(rs: ResolvedSpec) -> List[tuple]:
    """Operator scripts shipped inside every generated app.

    seed.sql only runs on an EMPTY database, so a hand-iterated build's
    seed silently rots while the live DB moves on (a stale seed can
    carry truncated URLs and shortened testimonials that a fresh boot
    would ship). Two-channel discipline:

      - db/schema.sql carries idempotent DDL (always runs on boot);
      - the content seed is REGENERATED from the live DB with
        scripts/regen-seed-from-db.js, never hand-edited.

    scripts/rehash-assets.js recomputes the ?v=<hash8> cache-bust
    query params after any hand edit to a public asset, so snapshot
    builds never serve stale CSS to returning visitors.
    """
    # In the order the tables are created (a parent before its children),
    # so the regenerated seed can be applied to an empty database.
    content_tables = [
        _slug_for(m).replace("-", "_") for m in _models_in_creation_order(rs)
    ]
    tables_js = json.dumps(content_tables)

    regen = r"""#!/usr/bin/env node
/* Regenerate db/seed.sql content from the LIVE database.
 *
 * Why: seed.sql only runs on an empty database (db_migrate skips it when
 * users is non-empty), so hand edits to the live DB rot the seed. This
 * script makes the live DB the source of truth: it dumps site_settings +
 * every content table into idempotent INSERT ... WHERE NOT EXISTS
 * statements keyed on NATURAL keys (slug/title/name/quote/...), never
 * serial ids -- a fresh DB seeded in a different order must not
 * mis-assign rows.
 *
 * Usage:
 *   DATABASE_URL=postgres://... node scripts/regen-seed-from-db.js          # rewrite db/seed.sql
 *   DATABASE_URL=postgres://... node scripts/regen-seed-from-db.js --check  # report drift, change nothing
 */
const fs = require('fs');
const path = require('path');
const db = require(path.join(__dirname, '..', 'db'));

const CONTENT_TABLES = __TABLES__;
const NATURAL_KEY_PRIORITY = ['slug', 'email', 'title', 'name', 'quote', 'question', 'label', 'city', 'author_name'];
const SKIP_COLUMNS = new Set(['id', 'created_at', 'updated_at']);

function sqlLit(v) {
  if (v === null || v === undefined) return 'NULL';
  if (typeof v === 'number') return String(v);
  if (typeof v === 'boolean') return v ? 'TRUE' : 'FALSE';
  if (v instanceof Date) return "'" + v.toISOString() + "'";
  if (typeof v === 'object') return "'" + JSON.stringify(v).replace(/'/g, "''") + "'";
  return "'" + String(v).replace(/'/g, "''") + "'";
}

async function tableColumns(table) {
  const { rows } = await db.query(
    "SELECT column_name FROM information_schema.columns WHERE table_name = $1 ORDER BY ordinal_position",
    [table]
  );
  return rows.map(r => r.column_name);
}

function naturalKeyFor(columns) {
  for (const k of NATURAL_KEY_PRIORITY) if (columns.includes(k)) return k;
  return columns.find(c => !SKIP_COLUMNS.has(c));
}

async function dumpTable(table) {
  const columns = (await tableColumns(table)).filter(c => !SKIP_COLUMNS.has(c));
  if (columns.length === 0) return [];
  const key = naturalKeyFor(columns);
  const { rows } = await db.query('SELECT * FROM ' + table + ' ORDER BY id ASC');
  const out = ['-- ' + table + ' (regenerated from live DB; keyed on ' + key + ')'];
  for (const row of rows) {
    const vals = columns.map(c => sqlLit(row[c]));
    out.push(
      'INSERT INTO ' + table + ' (' + columns.join(', ') + ')\n' +
      '  SELECT ' + vals.join(', ') + '\n' +
      '  WHERE NOT EXISTS (SELECT 1 FROM ' + table + ' WHERE ' + key + ' = ' + sqlLit(row[key]) + ');'
    );
  }
  return out;
}

async function main() {
  const checkOnly = process.argv.includes('--check');
  const seedPath = path.join(__dirname, '..', 'db', 'seed.sql');
  const lines = [
    '-- CAS v2 seed (REGENERATED from live DB by scripts/regen-seed-from-db.js)',
    '-- Do not hand-edit content below; edit the live DB and re-run the script.',
    '',
    '-- Admin users (magic-link auth; no password hashes in seed per SEC-1)',
  ];
  const admins = await db.query("SELECT email, role, email_verified FROM users WHERE role = 'admin' ORDER BY email");
  for (const u of admins.rows) {
    lines.push(
      'INSERT INTO users (email, password, password_set, role, email_verified) VALUES (' +
      sqlLit(u.email) + ", NULL, TRUE, 'admin', " + (u.email_verified ? 'TRUE' : 'FALSE') +
      ') ON CONFLICT (email) DO NOTHING;'
    );
  }
  lines.push('', '-- Site settings');
  const settings = await db.query('SELECT key, value FROM site_settings ORDER BY key');
  for (const s of settings.rows) {
    lines.push('INSERT INTO site_settings (key, value) VALUES (' + sqlLit(s.key) + ', ' + sqlLit(s.value) + ') ON CONFLICT (key) DO NOTHING;');
  }
  lines.push('');
  for (const t of CONTENT_TABLES) {
    try {
      const dumped = await dumpTable(t);
      lines.push(...dumped, '');
    } catch (e) {
      lines.push('-- ' + t + ': SKIPPED (' + e.message + ')', '');
    }
  }
  const next = lines.join('\n') + '\n';
  if (checkOnly) {
    const current = fs.existsSync(seedPath) ? fs.readFileSync(seedPath, 'utf-8') : '';
    if (current === next) {
      console.log('seed parity OK: db/seed.sql matches the live DB.');
      process.exit(0);
    }
    console.error('SEED DRIFT: db/seed.sql does not match the live DB. Run scripts/regen-seed-from-db.js to regenerate.');
    process.exit(1);
  }
  fs.writeFileSync(seedPath, next);
  console.log('Wrote ' + seedPath + ' from live DB (' + CONTENT_TABLES.length + ' content tables).');
  process.exit(0);
}

main().catch((e) => { console.error(e); process.exit(1); });
"""
    regen = regen.replace("__TABLES__", tables_js)

    rehash = r"""#!/usr/bin/env node
/* Recompute ?v=<hash8> cache-bust params after hand edits to public assets.
 *
 * Why: compose regenerates content hashes per build, but a snapshot build
 * iterated by hand keeps its original hash while the CSS changes
 * underneath -- returning visitors hold stale styles (one ?v= value
 * can survive several rounds of heavy CSS edits).
 * Run after any edit to a file in the list below; safe to re-run.
 *
 * Usage: node scripts/rehash-assets.js
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const ROOT = path.join(__dirname, '..');
const CACHE_BUSTED_PATHS = [
  '/style.css',
  '/js/cas-form-validate.js',
  '/js/cas-reveal.js',
  '/js/cas-fmt.js',
  '/js/upload-zone.js',
  '/logo.svg',
  '/favicon.svg',
  '/manifest.json',
];
const REWRITE_EXTENSIONS = ['.ejs', '.js', '.html', '.sql'];
const REWRITE_DIRS = ['views', 'routes', 'db', 'middleware', 'services'];

const hashes = {};
for (const p of CACHE_BUSTED_PATHS) {
  const onDisk = path.join(ROOT, 'public', p);
  if (!fs.existsSync(onDisk)) continue;
  hashes[p] = crypto.createHash('sha256').update(fs.readFileSync(onDisk)).digest('hex').slice(0, 8);
}

function* walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else yield full;
  }
}

let changed = 0;
for (const d of REWRITE_DIRS) {
  const dir = path.join(ROOT, d);
  if (!fs.existsSync(dir)) continue;
  for (const file of walk(dir)) {
    if (!REWRITE_EXTENSIONS.includes(path.extname(file))) continue;
    let src = fs.readFileSync(file, 'utf-8');
    let next = src;
    for (const [assetPath, h] of Object.entries(hashes)) {
      const esc = assetPath.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      // Rewrite both the bare path and any previously-hashed reference.
      next = next.replace(
        new RegExp('(["\x27])' + esc + '(\\?v=[0-9a-f]+)?(["\x27])', 'g'),
        '$1' + assetPath + '?v=' + h + '$3'
      );
    }
    if (next !== src) {
      fs.writeFileSync(file, next);
      changed++;
    }
  }
}
console.log('rehash-assets: updated ' + changed + ' file(s); ' + Object.keys(hashes).length + ' asset hash(es) computed.');
"""
    return [
        ("scripts/regen-seed-from-db.js", regen),
        ("scripts/rehash-assets.js", rehash),
    ]


def emit_package_json(rs: ResolvedSpec) -> tuple:
    deps = {
        "express": "^4.18.2",
        "express-ejs-layouts": "^2.5.1",
        "express-session": "^1.18.0",
        "connect-pg-simple": "^9.0.1",
        "pg": "^8.11.3",
        "bcrypt": "^6.0.0",
        "helmet": "^7.1.0",
        "express-rate-limit": "^7.3.1",
        "multer": "^2.1.1",
        "dotenv": "^16.3.1",
        "morgan": "^1.10.0",
        "compression": "^1.7.4",
        "ejs": "^3.1.9"
    }
    # Two entries were removed from this list because no emitted file uses
    # them: `sharp` (a native image library; nothing requires it) and the
    # npm package named `crypto` (a deprecated placeholder; every
    # `require('crypto')` in the app resolves to Node's built-in module).
    # An unused dependency is install time and supply-chain surface for
    # nothing. Versions are caret ranges and no lockfile is emitted, so an
    # install picks up the newest matching release at deploy time.
    pkg = {
        "name": rs.spec.app_name,
        "version": "0.1.0",
        "private": True,
        "scripts": {
            "start": "node server.js",
            "migrate": "node db_migrate.js"
        },
        "dependencies": deps,
        "engines": {"node": ">=20"}
    }
    return ("package.json", json.dumps(pkg, indent=2) + "\n")


_SQL_KEYWORD_DEFAULTS = {
    "NOW()",
    "CURRENT_TIMESTAMP",
    "CURRENT_DATE",
    "CURRENT_TIME",
    "TRUE",
    "FALSE",
    "NULL",
    "GEN_RANDOM_UUID()",
    "UUID_GENERATE_V4()",
}

# One complete single-quoted SQL literal: no bare quote inside (a doubled
# quote is the only way to write one), nothing before or after.
_SQL_QUOTED_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
# The same literal followed by a cast. What follows "::" is checked against
# the column-type allowlist (`_is_safe_sql_type`), so it can only be ONE
# plain type name: '[]'::jsonb, '{}'::text[], '0'::numeric(10,2). Allowing
# any run of words there would let a column constraint ride in through the
# default (`'x'::text UNIQUE`, `'1'::int REFERENCES users`).
_SQL_CAST_LITERAL_RE = re.compile(r"('(?:[^']|'')*')::(.+)")
# A plain decimal number written as a string ("0", "1.5", "-3", "1e3").
# Deliberately narrower than Python's float(): "nan", "inf" and "1_000"
# are not numbers Postgres reads as bare words.
# re.ASCII: only ASCII digits are a number to Postgres. A default written in
# another script's digits is text, and is quoted like any other string.
_SQL_NUMERIC_LITERAL_RE = re.compile(
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII,
)


def _has_column(names, column: str) -> bool:
    """True when `column` is among `names`, compared the way Postgres
    compares the unquoted identifiers the emitters write: without regard to
    letter case."""
    folded = column.lower()
    return any(isinstance(n, str) and n.lower() == folded for n in names)


def _format_sql_default(default: object) -> str:
    """Format a JSON-spec ``default`` value into a SQL DEFAULT clause body.

    The result is always ONE value expression. A spec string is emitted
    unquoted only when it is, in full, one of the shapes below; everything
    else is quoted as a string literal. (A bare word such as ``pending``
    would otherwise be read by Postgres as a column reference and abort the
    schema at first boot.)

    Rules:
    - Booleans render unquoted (``TRUE`` / ``FALSE``).
    - Integers and finite floats render unquoted; NaN and the infinities
      (which JSON parsers accept) become ``NULL``.
    - Recognized SQL function/keyword strings (``NOW()``, ``TRUE``,
      ``FALSE``, ``NULL``, ``CURRENT_TIMESTAMP``, ``GEN_RANDOM_UUID()``,
      etc.) pass through unquoted; case-insensitive match.
    - One complete quoted literal, optionally cast to one allowlisted type
      (``'[]'::jsonb``), passes through as written.
    - A plain decimal number written as a string passes through.
    - Any other string is wrapped in single quotes with every embedded
      single quote doubled.
    """
    if default is None:
        return "NULL"
    if isinstance(default, bool):
        return "TRUE" if default else "FALSE"
    if isinstance(default, float) and not math.isfinite(default):
        return "NULL"
    if isinstance(default, (int, float)):
        return str(default)
    s = str(default).strip()
    if not s:
        return "''"
    upper = s.upper()
    if upper in _SQL_KEYWORD_DEFAULTS:
        return upper
    # A quoted literal followed by a type cast is a SQL EXPRESSION, not a
    # value to re-quote: e.g. "'[]'::jsonb", "'{}'::jsonb", "'2020-01-01'::date".
    # Re-quoted, it becomes "'''[]''::jsonb'", which Postgres then tries to
    # parse as JSON at insert time and the migration stops with 22P02.
    #
    # Both pass-through shapes are matched IN FULL (a prefix test would emit
    # `'x'::text); DROP TABLE users; --` verbatim into the DDL), and the
    # cast target must be a plain column type. Anything else falls through
    # and is quoted as a string.
    if _SQL_QUOTED_LITERAL_RE.fullmatch(s):
        return s
    cast = _SQL_CAST_LITERAL_RE.fullmatch(s)
    if cast and _is_safe_sql_type(cast.group(2)):
        return f"{cast.group(1)}::{cast.group(2).strip()}"
    if _SQL_NUMERIC_LITERAL_RE.fullmatch(s):
        return s
    # String literal: escape embedded single quotes per SQL standard.
    return "'" + s.replace("'", "''") + "'"


# What Postgres takes for a value of each kind of column, written down from
# what it does with the literals `emit_seed_sql` and `_format_sql_default`
# write (compose/tests/test_stored_values.py runs every case against a real
# database, so a drift in either direction is a failing test). A seed row or a
# default that the column rejects stops the whole seed, or the whole schema,
# and the site never boots; the checks below say so while the spec is built.
_STORED_INT_RE = re.compile(r"\s*[+-]?\d+\s*", re.ASCII)
_STORED_NUMBER_RE = re.compile(r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*", re.ASCII)
_STORED_BOOLEAN_WORDS = frozenset({
    "t", "true", "y", "yes", "on", "1", "f", "false", "n", "no", "off", "0",
})
_STORED_DATE_RE = re.compile(
    r"\s*(\d{4})-(\d{2})-(\d{2})"
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:Z|[+-]\d{2}(?::?\d{2})?)?)?\s*",
    re.ASCII,
)
_STORED_TIME_RE = re.compile(r"\s*(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*", re.ASCII)
# A stored timestamp longer than this is refused before the pattern runs
# (`_stored_value_problem`). Each run of whitespace in the pattern is taken by
# one `\s*`: the one before the optional zone, or the one that follows a zone
# token. A failed match therefore backtracks in time linear in the run.
_STORED_TIMESTAMP_MAX_CHARS = 64
_STORED_TIMESTAMP_RE = re.compile(
    r"\s*(\d{4})-(\d{2})-(\d{2})"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?"
    r"\s*(?:(?:Z|[+-]\d{2}(?::?\d{2})?)\s*)?",
    re.ASCII,
)
_STORED_UUID_RE = re.compile(
    r"\{?[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}\}?", re.ASCII,
)
_STORED_PRECISION_RE = re.compile(r"(?:numeric|decimal)\s*\(\s*(\d{1,4})\s*(?:,\s*(\d{1,4})\s*)?\)", re.ASCII | re.IGNORECASE)
_STORED_INT_LIMITS = {"smallint": 2 ** 15, "int": 2 ** 31, "integer": 2 ** 31, "serial": 2 ** 31, "bigint": 2 ** 63, "bigserial": 2 ** 63}


def _valid_calendar_date(year: str, month: str, day: str) -> bool:
    import datetime
    try:
        datetime.date(int(year), int(month), int(day))
    except ValueError:
        return False
    return True


def _stored_value_problem(raw: Any, sql_type: Any) -> Optional[str]:
    """Why Postgres would refuse `raw` for a column of type `sql_type`, as a
    phrase ("is not a whole number"), or None when it takes it.

    `raw` is a value as `emit_seed_sql` writes it: null is NULL (always
    accepted), a boolean is TRUE or FALSE, a number is written as a bare
    number, and anything else (a string, or a list or object as its JSON
    text) is a quoted literal that the column's type reads. The engine
    takes nothing Postgres refuses (a test asks Postgres about every case).
    It refuses a few things Postgres takes: dates and times written in any
    form but the ISO one a spec uses (and a date in front of a time), a
    timestamp longer than `_STORED_TIMESTAMP_MAX_CHARS` characters, and an
    array of more than one dimension.
    """
    if raw is None or (isinstance(raw, float) and not math.isfinite(raw)):
        return None
    kind = _sql_type_kind(sql_type)
    text_type = str(sql_type or "").strip().lower()
    is_bool = isinstance(raw, bool)
    is_number = isinstance(raw, (int, float)) and not is_bool
    is_container = isinstance(raw, (list, dict))
    if kind == "text":
        if is_container:
            stored = json.dumps(raw)
        elif is_bool:
            stored = "true" if raw else "false"
        elif isinstance(raw, float):
            # A float is written as a bare number literal, which Postgres
            # prints in full (no exponent) when it stores it as text.
            stored = format(decimal.Decimal(repr(raw)), "f")
        else:
            stored = str(raw)
        width = _SQL_WIDTH_RE.match(text_type)
        if width and len(stored) > int(width.group(1)):
            return f"is {len(stored)} characters long, and the column holds at most {width.group(1)}"
        return None
    if kind in ("integer", "serial"):
        base = re.sub(r"\s+", " ", text_type.split("(", 1)[0]).strip()
        limit = _STORED_INT_LIMITS.get(base, 2 ** 31)
        if is_bool or is_container:
            return "is not a whole number"
        if is_number:
            return None if abs(round(raw)) < limit else "is out of range for the column"
        if not _STORED_INT_RE.fullmatch(raw):
            return "is not a whole number"
        return None if abs(int(raw.strip())) < limit else "is out of range for the column"
    if kind == "numeric":
        if is_bool or is_container:
            return "is not a number"
        if is_number:
            number = float(raw)
        elif _STORED_NUMBER_RE.fullmatch(raw):
            number = float(raw)
        else:
            return "is not a number"
        precision = _STORED_PRECISION_RE.match(text_type)
        if precision:
            digits = int(precision.group(1)) - int(precision.group(2) or 0)
            if abs(round(number, int(precision.group(2) or 0))) >= 10 ** digits:
                return "is too large for the column"
        return None
    if kind == "boolean":
        if is_bool:
            return None
        if isinstance(raw, str) and raw.strip().lower() in _STORED_BOOLEAN_WORDS:
            return None
        return "is not true or false"
    if kind == "date":
        found = _STORED_DATE_RE.fullmatch(raw) if isinstance(raw, str) else None
        return None if found and _valid_calendar_date(*found.groups()) else "is not a date (YYYY-MM-DD)"
    if kind == "time":
        found = _STORED_TIME_RE.fullmatch(raw) if isinstance(raw, str) else None
        if found:
            hour, minute, second = found.group(1), found.group(2), found.group(3) or "0"
            if int(hour) <= 24 and int(minute) < 60 and int(second) <= 60:
                return None
        return "is not a time (HH:MM)"
    if kind == "timestamp":
        found = None
        if isinstance(raw, str) and len(raw) <= _STORED_TIMESTAMP_MAX_CHARS:
            found = _STORED_TIMESTAMP_RE.fullmatch(raw)
        if found and _valid_calendar_date(*found.groups()[:3]):
            hour, minute, second = found.group(4) or "0", found.group(5) or "0", found.group(6) or "0"
            if int(hour) <= 24 and int(minute) < 60 and int(second) <= 60:
                return None
        return "is not a date and time (YYYY-MM-DD HH:MM)"
    if kind == "json":
        if is_container:
            return None
        if isinstance(raw, str):
            try:
                json.loads(raw)
                return None
            except (ValueError, RecursionError):
                # json.loads recurses on nested brackets: a deep value overflows
                # the stack, and that is a value Postgres would be handed as-is.
                pass
        return "is not JSON"
    if kind == "uuid":
        return None if isinstance(raw, str) and _STORED_UUID_RE.fullmatch(raw.strip()) else "is not a uuid"
    if kind == "array":
        return _array_literal_problem(raw, text_type[:-2]) if isinstance(raw, str) else "is not an array literal ({a,b})"
    return None


_ARRAY_ELEMENT = r'(?:"(?:[^"\\]|\\.)*"|[^\s,{}"\\](?:[^,{}"\\]*[^\s,{}"\\])?)'
_ARRAY_LITERAL_RE = re.compile(r"\s*\{\s*(?:" + _ARRAY_ELEMENT + r"(?:\s*,\s*" + _ARRAY_ELEMENT + r")*)?\s*\}\s*")
_ARRAY_ELEMENT_RE = re.compile(_ARRAY_ELEMENT)


def _array_literal_problem(text: str, element_type: str) -> Optional[str]:
    """Why `text` is not a one-dimensional array literal ({a,b} or
    {"a b","c"}) whose elements the type `element_type` takes, or None.
    A nested array is not accepted."""
    if not _ARRAY_LITERAL_RE.fullmatch(text):
        return "is not an array literal ({a,b})"
    for found in _ARRAY_ELEMENT_RE.finditer(text.strip()[1:-1]):
        element = found.group(0)
        if element.startswith('"'):
            element = re.sub(r"\\(.)", r"\1", element[1:-1])
        elif element.upper() == "NULL":
            continue
        problem = _stored_value_problem(element, element_type)
        if problem:
            return f"has an element that {problem}"
    return None


def _default_problem(default: Any, field: dict) -> Optional[str]:
    """Why the column `field` describes cannot have `default` as its DEFAULT
    (the way `_format_sql_default` writes it), as a phrase, or None. A default
    the column rejects stops the CREATE TABLE, and with it the first boot."""
    if default is None or default == "":
        return None
    expression = _format_sql_default(default)
    sql_type = _sql_type_for(field)
    kind = _sql_type_kind(sql_type)
    if expression == "NULL":
        return None
    functions = {
        "NOW()": ("timestamp", "date", "time", "text"), "CURRENT_TIMESTAMP": ("timestamp", "date", "time", "text"),
        "CURRENT_DATE": ("date", "timestamp", "text"), "CURRENT_TIME": ("time", "text"),
        "TRUE": ("boolean", "text"), "FALSE": ("boolean", "text"),
        "GEN_RANDOM_UUID()": ("uuid", "text"), "UUID_GENERATE_V4()": ("uuid", "text"),
    }
    if expression in functions:
        return None if kind in functions[expression] else f"{expression} is not a value of that column's type"
    cast = _SQL_CAST_LITERAL_RE.fullmatch(expression)
    if cast:
        return None if _sql_type_kind(cast.group(2)) == kind else "is typed as something else than the column"
    if _SQL_QUOTED_LITERAL_RE.fullmatch(expression):
        return _stored_value_problem(expression[1:-1].replace("''", "'"), sql_type)
    if _SQL_NUMERIC_LITERAL_RE.fullmatch(expression):
        number = float(expression) if re.search(r"[.eE]", expression) else int(expression)
        return _stored_value_problem(number, sql_type)
    return None


def _models_in_creation_order(rs: ResolvedSpec) -> List[dict]:
    """The models in the order their tables must be created (and their seed
    rows inserted, and their rows deleted in reverse): a model comes after
    the model it `belongs_to`, because its foreign key REFERENCES that
    table. Otherwise the spec's order is kept.

    A model may refer to itself (a category with a parent category). Two or
    more models that refer to each other in a ring have no order in which
    each table exists before the table that points to it: that is a
    ValueError ("Spec validation failed"), reported by `resolve_spec`.
    """
    models = [m for m in rs.data_models if isinstance(m, dict)]
    parent_of: Dict[int, dict] = {}
    for model in models:
        info = _belongs_to_info(rs, model)
        if info is not None and info[0] is not model:
            parent_of[id(model)] = info[0]
    ordered: List[dict] = []
    placed: set = set()
    for model in models:
        chain: List[dict] = []
        on_chain: set = set()
        current: Optional[dict] = model
        while current is not None and id(current) not in placed:
            if id(current) in on_chain:
                ring = chain[[id(c) for c in chain].index(id(current)):]
                names = " -> ".join(str(c.get("name")) for c in ring + [current])
                raise ValueError(
                    "Spec validation failed: data_models refer to each other in a ring "
                    f"through belongs_to ({names}); a table cannot point to one that is created after it"
                )
            on_chain.add(id(current))
            chain.append(current)
            current = parent_of.get(id(current))
        for member in reversed(chain):
            placed.add(id(member))
            ordered.append(member)
    return ordered


def _engine_column_errors(rs: ResolvedSpec) -> List[str]:
    """Reasons to refuse a build over the columns the engine reads with a
    fixed meaning, where more than the model's own fields has to be known
    (`_ENGINE_COLUMN_KINDS` has the ones that depend on the field alone):

      - the link to a parent model (`belongs_to`) is a column of the child
        that holds the parent's `id`. It may not be a column the engine
        adds to every table, and a field the spec declared under that name
        must be a whole-number column (the router compares it with the
        integer id of the page's row, the form fills it from the parent's
        ids);
      - the newsletter signup stores the address as text into the
        subscriber model's email column;
      - the order tables are created in (a ring is refused).
    """
    errors: List[str] = []
    for model in rs.data_models:
        info = _belongs_to_info(rs, model) if isinstance(model, dict) else None
        if info is None:
            continue
        parent, fk_col, _parent_table, _on_delete = info
        who = f"data_model {model.get('name')!r}"
        if fk_col.lower() in _ENGINE_COLUMNS:
            errors.append(
                f"{who} links to {parent.get('name')!r} through a column named {fk_col!r}, which "
                "the engine adds to every table itself; name another column with belongs_to_field"
            )
            continue
        declared = next(
            (f for f in _model_fields(model) if str(f.get("name")).lower() == fk_col.lower()), None
        )
        if declared is not None and _field_type_kind(declared) != "integer":
            errors.append(
                f"{who} declares {fk_col!r}, the column that holds the id of a {parent.get('name')!r} row, "
                f"with the type {str(_sql_type_for(declared))[:40]!r}; it must be a whole-number type "
                "(integer, int, bigint or smallint)"
            )
    if _page_with_widget(rs, "cta-newsletter-inline") is not None:
        subscribers = _newsletter_model(rs)
        email = _model_column(subscribers, "email") if subscribers is not None else None
        if email is not None:
            declared = next(f for f in _model_fields(subscribers) if f.get("name") == email)
            if _field_type_kind(declared) != "text":
                errors.append(
                    f"data_model {subscribers.get('name')!r} column {email!r} stores the address a visitor "
                    f"signs up with, as text; it has the type {str(_sql_type_for(declared))[:40]!r} "
                    "(use text, varchar or char)"
                )
    if not errors:
        try:
            _models_in_creation_order(rs)
        except ValueError as exc:
            prefix = "Spec validation failed: "
            text = str(exc)
            errors.append(text[len(prefix):] if text.startswith(prefix) else text)
    return errors


def emit_schema_sql(rs: ResolvedSpec) -> tuple:
    lines = [
        "-- CAS v2 compose-emitted schema",
        "CREATE TABLE IF NOT EXISTS users (",
        "  id SERIAL PRIMARY KEY,",
        "  email VARCHAR(255) UNIQUE NOT NULL,",
        "  password TEXT,",
        "  password_set BOOLEAN DEFAULT FALSE,",
        "  role VARCHAR(50) DEFAULT 'user',",
        "  email_verified BOOLEAN DEFAULT FALSE,",
        "  created_at TIMESTAMPTZ DEFAULT NOW()",
        ");",
        "",
        "CREATE TABLE IF NOT EXISTS user_sessions (",
        "  sid VARCHAR(255) PRIMARY KEY,",
        '  sess JSON NOT NULL,',
        "  expire TIMESTAMPTZ NOT NULL",
        ");",
        "CREATE INDEX IF NOT EXISTS user_sessions_expire_idx ON user_sessions(expire);",
        "",
        "CREATE TABLE IF NOT EXISTS site_settings (",
        "  key VARCHAR(100) PRIMARY KEY,",
        "  value TEXT DEFAULT '',",
        "  updated_at TIMESTAMPTZ DEFAULT NOW()",
        ");",
        "",
        "CREATE TABLE IF NOT EXISTS magic_link_tokens (",
        "  id SERIAL PRIMARY KEY,",
        "  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,",
        "  token_hash VARCHAR(255) NOT NULL,",
        "  expires_at TIMESTAMPTZ NOT NULL,",
        "  used_at TIMESTAMPTZ,",
        "  created_at TIMESTAMPTZ DEFAULT NOW()",
        ");",
        "",
    ]
    # Per-model table. Models with belongs_to=X emit
    # a `<parent_slug>_id INTEGER REFERENCES <parent_table>(id) ON DELETE
    # SET NULL` FK column. Deleting a parent row nulls the child's FK so
    # the child survives; admins can reassign. Use CASCADE only when the
    # customer explicitly declares it.
    # System columns the emitter appends to every per-model table. If the
    # customer's v1 spec already declared one of these by name (e.g.
    # a ContactInquiry model that lists `created_at` as a
    # field), skip the duplicate append so
    # the CREATE TABLE stays valid. Preserve the customer-declared type
    # if they were explicit; otherwise fall back to our default. (The two
    # columns are `_ENGINE_COLUMNS`; the gate refuses a field that differs
    # from one of them only by letter case.)
    for m in _models_in_creation_order(rs):
        slug = _slug_for(m).replace("-", "_")
        tbl = slug
        cols = ["  id SERIAL PRIMARY KEY"]
        schema_fields = m.get("fields") or _default_fields(m["name"])
        declared_names = set()
        customer_has_created_at = False
        for f in schema_fields:
            col_name = f["name"]
            # Skip id (we always emit SERIAL PRIMARY KEY above). Allow
            # created_at through so the customer's declared type +
            # NOT-NULL preferences are honored, but flag it so we do not
            # append our default after.
            if col_name == "id":
                continue
            if col_name == "created_at":
                customer_has_created_at = True
                # Ensure the column has a DEFAULT NOW() even if the
                # customer did not declare one, so inserts without an
                # explicit created_at still populate it.
                if not f.get("default"):
                    f = dict(f)
                    f["default"] = "NOW()"
            if col_name in declared_names:
                # Translator shouldn't emit duplicates, but belt-and-suspenders.
                continue
            declared_names.add(col_name)
            col_type = _safe_sql_type(f)
            null = "NOT NULL" if f.get("required") else ""
            default = f.get("default")
            # "No default" is an absent key, null or an empty string. A JSON
            # false or 0 IS a default (testing truthiness here would drop
            # `"default": false` and leave the column defaulting to NULL).
            has_default = default is not None and default != ""
            problem = _default_problem(default, f) if has_default else None
            if problem:
                # A default the column rejects would stop the CREATE TABLE
                # and the site with it: the column has none, and the report
                # says so.
                _engine_warning(
                    rs.spec, "column_default_ignored",
                    f"{m['name']}.{col_name}: the default {str(default)[:40]!r} cannot be stored in a "
                    f"{col_type} column ({problem}); the column has no default",
                )
                has_default = False
            default_sql = f" DEFAULT {_format_sql_default(default)}" if has_default else ""
            cols.append(f"  {col_name} {col_type}{default_sql} {null}".rstrip())
        bt_info = _belongs_to_info(rs, m)
        if bt_info is not None:
            _parent_model, fk_col, parent_tbl, on_delete = bt_info
            if not _has_column(declared_names, fk_col):
                cols.append(
                    f"  {fk_col} INTEGER REFERENCES {parent_tbl}(id) ON DELETE {on_delete}"
                )
        if not customer_has_created_at:
            cols.append("  created_at TIMESTAMPTZ DEFAULT NOW()")
        lines.append(f"CREATE TABLE IF NOT EXISTS {tbl} (")
        lines.append(",\n".join(cols))
        lines.append(");")
        lines.append("")
    # Evergreen renames: a slug rename must 301 every
    # historical URL or shared links + search results rot. Operators add
    # rows here (old_path -> new_path); the slug-aliases middleware
    # serves the redirects with a 60s in-memory cache.
    lines.append("CREATE TABLE IF NOT EXISTS slug_aliases (")
    lines.append("  id SERIAL PRIMARY KEY,")
    lines.append("  old_path TEXT UNIQUE NOT NULL,")
    lines.append("  new_path TEXT NOT NULL,")
    lines.append("  created_at TIMESTAMPTZ DEFAULT NOW()")
    lines.append(");")
    lines.append("")
    return ("db/schema.sql", "\n".join(lines))


_SUBMISSION_TARGET_SUFFIXES = (
    "inquiry", "inquiries",
    "message", "messages",
    "submission", "submissions",
    "request", "requests",
    "booking", "bookings",
    "subscriber", "subscribers",
    "registration", "registrations",
    "rsvp", "rsvps",
    "lead", "leads",
    "application", "applications",
)


def _is_submission_target_model(model: dict) -> bool:
    """A model is a submission-target when its name ends in one of the
    submission-target suffixes (ContactInquiry, ContactMessage,
    BookingRequest, NewsletterSubscriber, etc.): a store of what visitors
    sent in.

    This is a naming convention, so it decides nothing about publication:
    whether a model's rows are public is `_public_row_sources`, which does
    not look at names. It is used for three conveniences: a plain custom
    page at such a model's path is not emitted and the path stays a 404
    (`_classify_custom_pages`, reason `model_route`), the built-in contact
    form falls back to such a model (`_contact_form_model`), and the admin
    navigation always shows it (`_is_model_publicly_visible`)."""
    if not isinstance(model, dict):
        return False
    name = (model.get("name") or "").lower()
    plural = (model.get("plural") or "").lower()
    for suffix in _SUBMISSION_TARGET_SUFFIXES:
        if name.endswith(suffix) or plural.endswith(suffix):
            return True
    return False


def _belongs_to_info(rs: ResolvedSpec, model: dict):
    """Resolve a model's belongs_to hint into (parent_model, fk_col,
    parent_table, on_delete). Returns None when the model has no
    belongs_to or the named parent is absent from rs.data_models.

    v1 shape: `{"name": "Product", "belongs_to": "Category"}`
    v2 shape can also carry `belongs_to_field` + `on_delete` overrides.
    """
    if not isinstance(model, dict):
        return None
    parent_name = (model.get("belongs_to") or "").strip()
    if not parent_name:
        return None
    parent_model = None
    needle = parent_name.lower()
    for m in rs.data_models:
        if not isinstance(m, dict):
            continue
        if (m.get("name") or "").lower() == needle:
            parent_model = m
            break
        if (m.get("plural") or "").lower() == needle:
            parent_model = m
            break
    if parent_model is None:
        return None
    parent_table = _table_name_for_model(parent_model)
    fk_col = (model.get("belongs_to_field") or "").strip()
    if not fk_col:
        fk_col = _singular_slug(parent_model["name"]).replace("-", "_") + "_id"
    on_delete = (model.get("on_delete") or "SET NULL").strip().upper()
    if on_delete not in _ON_DELETE_ACTIONS:
        on_delete = "SET NULL"
    return (parent_model, fk_col, parent_table, on_delete)


def _belongs_to_parent_fetch_js(bt_info) -> str:
    """Emit the JS snippet that fetches parent rows for a <select> in
    the admin new/edit form. When the model has no belongs_to, emits a
    constant empty array so the view template can always reference
    parent_rows without branching."""
    if bt_info is None:
        return "const parent_rows = [];"
    _parent_model, _fk_col, parent_table, _on_delete = bt_info
    display_col = _parent_display_col(_parent_model)
    return (
        "const _parentRes = await db.query("
        f"'SELECT id, {display_col} AS display FROM {parent_table} ORDER BY {display_col}'"
        ");\n"
        "    const parent_rows = _parentRes.rows;"
    )


def _display_column(model: dict) -> str:
    """The column that names a row of `model` to a person: `name`, `title`,
    `label`, `author_name` or `heading` when the model has one, else its
    first non-system VARCHAR/TEXT column, else `id`.

    Reads the model's effective fields (`_model_fields`), so a model that
    relies on the engine's default fields (the author archetype's Book) is
    labelled by its title, not by its id.
    """
    # A json column cannot label a row: Postgres has no ordering for it, and
    # the parent <select> of a belongs_to form is ordered by this column.
    fields = [f for f in _model_fields(model) if _field_type_kind(f) != "json"]
    for candidate in ("name", "title", "label", "author_name", "heading"):
        real = next((f["name"] for f in fields
                     if isinstance(f.get("name"), str) and f["name"].lower() == candidate), None)
        if real:
            return real
    for f in fields:
        sql = str(_sql_type_for(f)).upper()
        if (sql.startswith("VARCHAR") or sql == "TEXT") and isinstance(f.get("name"), str):
            if f["name"] not in _PUBLIC_FORM_SYSTEM_FIELDS:
                return f["name"]
    return "id"


def _parent_display_col(parent_model: dict) -> str:
    """Label column for the parent <select> of a belongs_to field."""
    return _display_column(parent_model)


# Default schema field shapes per data-model archetype name
def _default_fields(name: str) -> list:
    if name == "BlogPost":
        return [
            {"name": "title", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "slug", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "excerpt", "sql_type": "TEXT"},
            {"name": "body", "sql_type": "TEXT", "required": True},
            {"name": "image_url", "sql_type": "TEXT"},
            # Direct outbound link for press/podcast/interview entries:
            # listing cards prefer this over the internal
            # detail page and open it in a new tab.
            {"name": "external_url", "sql_type": "TEXT"},
            {"name": "published", "sql_type": "BOOLEAN", "default": "FALSE"},
            {"name": "published_at", "sql_type": "TIMESTAMPTZ"},
        ]
    if name == "Event":
        # Rich-content fields for a long-form event page, such as a bookstore
        # reading (slug, eyebrow, date_label, venue, intro, body,
        # photos JSONB, video_url, video_caption, closing_quote, etc.).
        # Existing time + location columns are preserved so the admin
        # dashboard's upcoming-events query (SELECT event_date, event_time,
        # end_date, end_time, location ...) and the existing card-list
        # public view keep rendering. New columns are additive: a row that
        # only populates the old fields renders the original event card;
        # a row that populates intro/body/photos/video_url/closing_quote
        # picks up the rich detail-page treatment via the new
        # photo-slideshow + video-embed-nocookie widgets.
        return [
            {"name": "slug", "field_type": "slug"},
            {"name": "title", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "eyebrow", "sql_type": "VARCHAR(255)"},
            {"name": "date_label", "sql_type": "VARCHAR(255)"},
            {"name": "event_date", "sql_type": "DATE"},
            {"name": "event_time", "sql_type": "TIME"},
            {"name": "end_date", "sql_type": "DATE"},
            {"name": "end_time", "sql_type": "TIME"},
            {"name": "venue", "sql_type": "VARCHAR(255)"},
            {"name": "venue_url", "field_type": "url"},
            {"name": "location", "sql_type": "VARCHAR(255)"},
            {"name": "hero_image_url", "sql_type": "TEXT"},
            {"name": "hero_image_alt", "sql_type": "VARCHAR(255)"},
            # Art direction: vertical focal point (0-100,
            # percent from top) for crops; the subject is not always
            # centered (one reference photo's subject sat at 38%).
            {"name": "focal_y", "sql_type": "INTEGER"},
            {"name": "intro", "sql_type": "TEXT"},
            {"name": "description", "sql_type": "TEXT"},
            {"name": "body", "sql_type": "TEXT"},
            {"name": "photos", "sql_type": "JSONB", "default": "'[]'::jsonb"},
            {"name": "video_url", "field_type": "url"},
            {"name": "video_caption", "sql_type": "VARCHAR(255)"},
            {"name": "closing_quote", "sql_type": "TEXT"},
            {"name": "registration_url", "sql_type": "TEXT"},
            {"name": "image_url", "sql_type": "TEXT"},
            {"name": "published", "sql_type": "BOOLEAN", "default": "FALSE"},
            {"name": "sort_order", "sql_type": "INTEGER", "default": "0"},
        ]
    if name == "Testimonial":
        return [
            {"name": "author_name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "quote", "sql_type": "TEXT", "required": True},
            {"name": "service", "sql_type": "VARCHAR(100)"},
            {"name": "published", "sql_type": "BOOLEAN", "default": "TRUE"},
        ]
    if name == "ContactInquiry":
        return [
            {"name": "name", "field_type": "text", "required": True},
            {"name": "email", "field_type": "email", "required": True},
            {"name": "phone", "field_type": "phone_us"},
            {"name": "service_interest", "sql_type": "VARCHAR(100)"},
            {"name": "message", "field_type": "long_text", "required": True},
        ]
    if name == "NewsletterSubscriber":
        return [
            {"name": "email", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "unsubscribed", "sql_type": "BOOLEAN", "default": "FALSE"},
        ]
    if name == "Service":
        return [
            {"name": "title", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "description", "sql_type": "TEXT"},
            {"name": "price_display", "sql_type": "VARCHAR(100)"},
            {"name": "image_url", "sql_type": "TEXT"},
        ]
    if name == "Photo":
        return [
            {"name": "title", "sql_type": "VARCHAR(255)"},
            {"name": "caption", "sql_type": "TEXT"},
            {"name": "image_url", "sql_type": "TEXT", "required": True},
            {"name": "category", "sql_type": "VARCHAR(100)"},
            {"name": "featured", "sql_type": "BOOLEAN", "default": "FALSE"},
        ]
    if name == "Review":
        return [
            {"name": "author_name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "rating", "sql_type": "INTEGER"},
            {"name": "body", "sql_type": "TEXT", "required": True},
            {"name": "service", "sql_type": "VARCHAR(100)"},
        ]
    if name == "CaseStudy":
        return [
            {"name": "title", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "client_name", "sql_type": "VARCHAR(255)"},
            {"name": "summary", "sql_type": "TEXT"},
            {"name": "body", "sql_type": "TEXT"},
            {"name": "image_url", "sql_type": "TEXT"},
        ]
    # ---- round-5 models: shared across archetypes ----
    if name == "FAQ":
        return [
            {"name": "question", "sql_type": "TEXT", "required": True},
            {"name": "answer", "sql_type": "TEXT", "required": True},
            {"name": "category", "sql_type": "VARCHAR(100)"},
            {"name": "sort_order", "sql_type": "INTEGER", "default": "0"},
        ]
    if name == "PricingPlan":
        return [
            {"name": "name", "sql_type": "VARCHAR(100)", "required": True},
            {"name": "description", "sql_type": "TEXT"},
            {"name": "price_display", "sql_type": "VARCHAR(100)"},
            {"name": "billing_period", "sql_type": "VARCHAR(50)"},
            {"name": "features", "sql_type": "TEXT"},
            {"name": "cta_text", "sql_type": "VARCHAR(100)"},
            {"name": "cta_href", "sql_type": "TEXT"},
            {"name": "featured", "sql_type": "BOOLEAN", "default": "FALSE"},
            {"name": "sort_order", "sql_type": "INTEGER", "default": "0"},
        ]
    if name == "StaffMember":
        return [
            {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "role", "sql_type": "VARCHAR(255)"},
            {"name": "bio", "sql_type": "TEXT"},
            {"name": "photo_url", "sql_type": "TEXT"},
            {"name": "specialties", "sql_type": "TEXT"},
            {"name": "booking_url", "sql_type": "TEXT"},
            {"name": "sort_order", "sql_type": "INTEGER", "default": "0"},
        ]
    if name == "Instructor":
        return [
            {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "role", "sql_type": "VARCHAR(255)"},
            {"name": "bio", "sql_type": "TEXT"},
            {"name": "photo_url", "sql_type": "TEXT"},
            {"name": "specialties", "sql_type": "TEXT"},
            {"name": "instagram_url", "sql_type": "TEXT"},
            {"name": "is_lead", "sql_type": "BOOLEAN", "default": "FALSE"},
        ]
    if name == "ClassType":
        return [
            {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "description", "sql_type": "TEXT"},
            {"name": "duration_min", "sql_type": "INTEGER"},
            {"name": "difficulty", "sql_type": "VARCHAR(50)"},
            {"name": "image_url", "sql_type": "TEXT"},
        ]
    if name == "ScheduleSlot":
        return [
            {"name": "class_name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "instructor_name", "sql_type": "VARCHAR(255)"},
            {"name": "day_of_week", "sql_type": "VARCHAR(20)", "required": True},
            {"name": "start_time", "sql_type": "TIME", "required": True},
            {"name": "end_time", "sql_type": "TIME"},
            {"name": "room", "sql_type": "VARCHAR(100)"},
        ]
    if name == "MenuSection":
        return [
            {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "description", "sql_type": "TEXT"},
            {"name": "sort_order", "sql_type": "INTEGER", "default": "0"},
        ]
    if name == "MenuItem":
        return [
            {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "description", "sql_type": "TEXT"},
            {"name": "price_display", "sql_type": "VARCHAR(50)"},
            {"name": "section_name", "sql_type": "VARCHAR(255)"},
            {"name": "dietary_tags", "sql_type": "TEXT"},
            {"name": "available", "sql_type": "BOOLEAN", "default": "TRUE"},
            {"name": "sort_order", "sql_type": "INTEGER", "default": "0"},
        ]
    if name == "ServiceArea":
        return [
            {"name": "city", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "state", "sql_type": "VARCHAR(50)"},
            {"name": "zip_codes", "sql_type": "TEXT"},
            {"name": "description", "sql_type": "TEXT"},
            {"name": "sort_order", "sql_type": "INTEGER", "default": "0"},
        ]
    if name == "Modality":
        return [
            {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "description", "sql_type": "TEXT"},
            {"name": "image_url", "sql_type": "TEXT"},
            {"name": "sort_order", "sql_type": "INTEGER", "default": "0"},
        ]
    if name == "Specialty":
        return [
            {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "description", "sql_type": "TEXT"},
            {"name": "sort_order", "sql_type": "INTEGER", "default": "0"},
        ]
    if name == "Program":
        return [
            {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "description", "sql_type": "TEXT"},
            {"name": "image_url", "sql_type": "TEXT"},
            {"name": "impact_metric", "sql_type": "VARCHAR(255)"},
            {"name": "cta_text", "sql_type": "VARCHAR(100)"},
            {"name": "cta_href", "sql_type": "TEXT"},
        ]
    if name == "Book":
        # 20 fields behind the author archetype's /admin/books CRUD.
        # `description` accepts a small set of formatting tags (p, br, em,
        # strong, i, b, lists, blockquote); the book-card widget and the
        # book detail view escape everything else before output. All retailer URLs are optional; the widget
        # composes whichever links are present into a "Buy on Amazon"
        # primary CTA + "Also at: Goodreads, Local bookstore, ..." secondary row.
        return [
            {"name": "slug", "field_type": "slug"},
            {"name": "title", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "subtitle", "sql_type": "VARCHAR(255)"},
            {"name": "series_label", "sql_type": "VARCHAR(255)"},
            {"name": "description", "sql_type": "TEXT"},
            {"name": "cover_image_url", "sql_type": "TEXT"},
            {"name": "cover_image_alt", "sql_type": "VARCHAR(255)"},
            {"name": "cover_credit", "sql_type": "VARCHAR(255)"},
            {"name": "accolade", "sql_type": "VARCHAR(255)"},
            {"name": "rating_callout", "sql_type": "VARCHAR(255)"},
            {"name": "price_display", "sql_type": "VARCHAR(100)"},
            {"name": "amazon_url", "field_type": "url"},
            {"name": "goodreads_url", "field_type": "url"},
            {"name": "local_bookstore_url", "field_type": "url"},
            {"name": "barnesnoble_url", "field_type": "url"},
            {"name": "netgalley_url", "field_type": "url"},
            {"name": "audible_url", "field_type": "url"},
            {"name": "featured_on_home", "sql_type": "BOOLEAN", "default": "FALSE"},
            {"name": "sort_order", "sql_type": "INTEGER", "default": "0"},
            {"name": "published", "sql_type": "BOOLEAN", "default": "TRUE"},
        ]
    return [{"name": "title", "sql_type": "VARCHAR(255)", "required": True}]


def _sql_type_for(field: dict) -> str:
    """Resolve a field's Postgres column type.

    Precedence:
      1. Explicit `sql_type` on the field (honors legacy specs).
      2. Look up field_type (explicit OR inferred from name) in the
         field-types registry.
      3. Default TEXT.
    """
    if field.get("sql_type"):
        return field["sql_type"]
    ft = _resolve_field_type(field)
    if ft:
        return ft.get("sql_type", "TEXT")
    return "TEXT"


_FIELD_NAME_INFERENCE = {
    # field name (lowercase) → inferred field_type id
    "email": "email",
    "contact_email": "email",
    "support_email": "email",
    "phone": "phone_us",
    "phone_number": "phone_us",
    "contact_phone": "phone_us",
    "url": "url",
    "website": "url",
    "website_url": "url",
    "booking_url": "url",
    "registration_url": "url",
    "instagram_url": "url",
    "facebook_url": "url",
    "twitter_url": "url",
    "linkedin_url": "url",
    "youtube_url": "url",
    "tiktok_url": "url",
    "zip": "zip_us",
    "zip_code": "zip_us",
    "postal_code": "zip_us",
    "state": "state_us",
    "slug": "slug",
}


def _infer_field_type(field: dict) -> Optional[str]:
    """Infer a field_type id from the field's name when none is explicitly
    set. Ensures email/phone/url/zip/state/slug fields ALWAYS get the
    registry-grade pattern + sanitize chain applied, even on legacy models
    that only declare sql_type. Safety net: a model that forgot to set
    `field_type` on its email column still gets strict email validation.
    """
    name = (field.get("name") or "").lower()
    return _FIELD_NAME_INFERENCE.get(name)


def _resolve_field_type(field: dict) -> Optional[dict]:
    """Return the full field_type dict for a model field (or None).

    Resolution order:
      1. Explicit `field_type` on the field definition.
      2. Inferred from the field name (email → email, phone → phone_us, etc).
      3. None (caller falls back to legacy heuristic).
    """
    ft_id = field.get("field_type") or _infer_field_type(field)
    if not ft_id:
        return None
    try:
        return get_field_type(ft_id)
    except Exception:
        return None


def _html_input_for(field: dict) -> str:
    """Resolve the logical HTML input kind for a field.

    Consulted by emit_admin_crud_views to pick the right crud-form
    branch. Returns values like 'input:text', 'input:email', 'textarea',
    'select:state-us', 'upload:image', etc. Falls back to sql_type
    heuristics when no explicit field_type is set.
    """
    ft = _resolve_field_type(field)
    if ft:
        return ft.get("html_input", "input:text")
    # Legacy heuristic path (preserves existing behavior for models that
    # haven't migrated to field_type yet):
    st = (field.get("sql_type") or "").upper()
    name = field.get("name", "")
    image_names = ("image_url", "photo_url", "avatar_url", "cover_url", "thumbnail_url", "logo_url")
    if "TEXT" in st and name in ("body", "description", "quote", "summary", "excerpt", "message"):
        return "textarea"
    if _sql_type_kind(st) == "boolean":
        return "input:checkbox"
    if st == "TIME" or st.startswith("TIME "):
        return "input:time"
    if "DATE" in st and "TIME" not in st:
        return "input:date"
    if "TIMESTAMP" in st and name.endswith("_at"):
        return "input:date"
    if name in image_names:
        return "upload:image"
    if name.endswith("_url"):
        return "input:url"
    if name == "email":
        return "input:email"
    return "input:text"


def emit_seed_sql(rs: ResolvedSpec) -> tuple:
    """Emit deterministic seed data + site_settings from spec content.

    All 24 canonical site_settings keys are always seeded. Data-model rows
    come from spec.seed_content; if absent, deterministic placeholders are
    generated from the archetype's canonical shape.
    """
    lines = [
        "-- CAS v2 compose-emitted seed data",
    ]
    # Surface customer-declared user_roles as a doc
    # comment block so admins + SE reviewers can see the role catalog.
    # Uses `users.role VARCHAR(50)` as-is (no enum); requireRole middleware
    # already accepts any string. Emitted above the admin INSERT so it
    # reads like a preamble.
    if rs.spec.user_roles:
        lines.append("-- Customer-declared user roles (preserved from v1 spec):")
        for r in rs.spec.user_roles:
            if not isinstance(r, dict):
                continue
            # Comment context: _sql_comment, not _sql_esc. Quote doubling
            # does nothing here; what matters is that a newline in the role
            # name would end the `--` comment and run the rest as SQL.
            role = _sql_comment(r.get("role") or "")
            desc = _sql_comment(r.get("description") or "")
            lines.append(f"--   - {role}: {desc}" if desc else f"--   - {role}")
        lines.append("")
    lines.extend([
        "-- Admin user with magic-link-only auth (no bcrypt hash in seed per SEC-1)",
        f"INSERT INTO users (email, password, password_set, role, email_verified) "
        f"VALUES ('{_sql_esc(rs.spec.admin_email)}', NULL, TRUE, 'admin', TRUE) "
        "ON CONFLICT (email) DO NOTHING;",
        "",
        "-- Site settings (24 canonical keys; always fully seeded)",
    ])
    settings = rs.spec.settings or {}
    # Canonical 24 keys
    settings_defaults = {
        "site_name": settings.get("site_name") or _prettify(rs.spec.app_name),
        "site_url": settings.get("site_url", ""),
        "site_description": settings.get("site_description", ""),
        "hero_tagline": settings.get("hero_tagline", ""),
        "hero_description": settings.get("hero_description", ""),
        "hero_image_url": settings.get("hero_image_url", ""),
        # Default to the generated /logo.svg monogram when no real logo is
        # supplied. Placeholder URLs (picsum/lorempixel/placeholder.*) are
        # treated as empty so builds ship with a decent-looking logo out of
        # the box instead of a stock stand-in.
        "nav_logo_url": (
            settings["nav_logo_url"]
            if settings.get("nav_logo_url")
            and not re.search(r"picsum|placeholder|loremflickr|via\.placeholder|lorempixel", settings["nav_logo_url"], re.I)
            else "/logo.svg"
        ),
        "about_title": settings.get("about_title", ""),
        "about_body": settings.get("about_body", ""),
        "about_photo_url": settings.get("about_photo_url", ""),
        "contact_email": settings.get("contact_email", rs.spec.admin_email),
        "contact_phone": settings.get("contact_phone", ""),
        "contact_address": settings.get("contact_address", ""),
        "business_name": settings.get("business_name", _prettify(rs.spec.app_name)),
        "phone": settings.get("phone", ""),
        "support_email": settings.get("support_email", rs.spec.admin_email),
        "address": settings.get("address", ""),
        "instagram_url": settings.get("instagram_url", ""),
        "facebook_url": settings.get("facebook_url", ""),
        "youtube_url": settings.get("youtube_url", ""),
        "tiktok_url": settings.get("tiktok_url", ""),
        "twitter_url": settings.get("twitter_url", ""),
        "linkedin_url": settings.get("linkedin_url", ""),
        "venmo_handle": settings.get("venmo_handle", ""),
    }
    # Personal-brand archetypes (author): the site IS a person. Seeding
    # owner_name switches on the Person node in the layout's JSON-LD
    # graph + the author meta (v2-seo-headers-jsonld).
    if rs.archetype.get("owner_is_person"):
        settings_defaults["owner_name"] = (
            settings.get("owner_name")
            or settings_defaults["site_name"]
        )
    for k, v in settings_defaults.items():
        # URL-valued settings (site_url, social links, image paths) are
        # rendered into href/src attributes by the shell widgets.
        if _URL_KEY_RE.search(k):
            v = _neutralize_url(v)
        lines.append(
            f"INSERT INTO site_settings (key, value) VALUES "
            f"('{k}', '{_sql_esc(v)}') ON CONFLICT (key) DO NOTHING;"
        )
    lines.append("")

    # Page-text settings (v2-full-editability-audit): every
    # customer-voice string on every public page is seeded with its
    # current copy so the admin Settings form shows real text to edit,
    # never blank fields that secretly drive baked defaults.
    page_text = _page_text_settings(rs)
    if page_text:
        lines.append("-- Page text (admin-editable copy; seeded with current copy)")
        for _pid, _entries in page_text.items():
            for _e in _entries:
                lines.append(
                    f"INSERT INTO site_settings (key, value) VALUES "
                    f"('{_e['key']}', '{_sql_esc(_e['value'])}') "
                    "ON CONFLICT (key) DO NOTHING;"
                )
        lines.append("")

    # Per-model seed rows, a parent's before its children's (a seed row may
    # carry the id of its parent).
    for m in _models_in_creation_order(rs):
        slug = _slug_for(m).replace("-", "_")
        model_name = m["name"]
        rows = rs.spec.seed_content.get(slug) or rs.spec.seed_content.get(_plural(model_name).replace("-", "_")) or []
        # Seed rows are spec data: tolerate a malformed block by ignoring
        # the entries that are not objects instead of crashing the build.
        rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        if not rows:
            rows = _placeholder_rows(model_name, rs)
        if not rows:
            continue
        schema_fields = m.get("fields") or _default_fields(model_name)
        col_names = [f["name"] for f in schema_fields]
        # When the model belongs_to a parent, the FK column is a real schema
        # column that emit_schema_sql adds separately (it is NOT in
        # schema_fields), so a seed row carrying the parent FK (e.g.
        # Testimonial.book_id) would silently drop the link. Mirror
        # emit_routes_resource_js and include the FK column, but ONLY when a
        # seed row actually provides it: that keeps the common archetype
        # placeholder rows (no FK) byte-identical and never forces a NULL or
        # mismatched FK that could trip the constraint at seed time.
        _bt_info = _belongs_to_info(rs, m)
        if _bt_info is not None:
            _fk_col = _bt_info[1]
            if not _has_column(col_names, _fk_col) and any(
                isinstance(r, dict) and r.get(_fk_col) is not None for r in rows
            ):
                col_names = col_names + [_fk_col]
        # Schema/seed shape mismatch guard. When a customer
        # spec overrides a data_model's fields with names that the
        # archetype's hardcoded seed shape doesn't supply (e.g.
        # `ServiceArea {name: required}` vs archetype's
        # `{city, state, zip_codes, ...}`), the schema emits
        # `name VARCHAR NOT NULL` but the seed loop produces NULL for
        # name -> Postgres CHECK violation -> app crashes at boot.
        # Discovered by the timberline-hvac e2e test build.
        # Fix: filter rows that lack any customer-declared required
        # column, and record an engine warning so the SE reviewer sees
        # the dropped seed.
        required_col_names = {
            f["name"] for f in schema_fields if f.get("required") is True
        }
        field_of = {f["name"]: f for f in schema_fields}
        valid_rows: list = []
        sample_missing: set = set()
        unfit: Dict[str, str] = {}
        for row in rows:
            # A JSON null is no value either: it would be written as NULL
            # into a NOT NULL column.
            missing = required_col_names - {k for k, v in row.items() if v is not None}
            if missing:
                sample_missing |= missing
                continue
            # A value the column refuses stops the whole seed script, and
            # the site with it: the row is left out instead.
            refused = {}
            for fname in col_names:
                problem = _stored_value_problem(
                    _sanitize_spec_content(row.get(fname), fname),
                    _sql_type_for(field_of[fname]) if fname in field_of else "INTEGER",
                )
                if problem:
                    refused[fname] = problem
            if refused:
                for fname, problem in refused.items():
                    unfit.setdefault(fname, problem)
                continue
            valid_rows.append(row)
        dropped = len(rows) - len(valid_rows)
        lacking = len([r for r in rows if required_col_names - {k for k, v in r.items() if v is not None}])
        if lacking:
            warning_detail = (
                f"{model_name}: dropped {lacking} of {len(rows)} seed "
                f"rows whose archetype shape lacked customer-declared "
                f"required field(s) {sorted(sample_missing)}"
            )
            # Recorded as an engine warning so the review UI shows the
            # seed drop.
            _engine_warning(rs.spec, "seed_shape_mismatch", warning_detail)
        if unfit:
            listed = "; ".join(f"{column} {problem}" for column, problem in sorted(unfit.items())[:3])
            _engine_warning(
                rs.spec, "seed_value_unfit",
                f"{model_name}: dropped {dropped - lacking} of {len(rows)} seed rows with a value its "
                f"column cannot store ({listed})",
            )
        if not valid_rows:
            # All rows filtered out. Skip seeding entirely; admin can
            # add rows via /admin/<slug> after first deploy.
            why = (
                f"customer-declared required field(s) {sorted(sample_missing)} not present in archetype seed shape"
                if sample_missing else "no row has values its columns can store"
            )
            lines.append(f"-- {model_name} seed SKIPPED: {_sql_comment(why)}; admin can populate via /admin/{slug}")
            lines.append("")
            continue
        rows = valid_rows
        lines.append(f"-- {model_name} seed")
        published_column = _publication_column(m)
        for row in rows:
            values = []
            for fname in col_names:
                # Same normalization as page copy: a seeded *_url column is
                # later rendered as a link or image address.
                raw = _sanitize_spec_content(row.get(fname), fname)
                if raw is None and fname == published_column:
                    # A seed row that does not say is content the spec put on
                    # the site: published. (A public read shows only rows
                    # whose flag is TRUE, `_published_condition`.)
                    values.append("TRUE")
                elif raw is None and fname in field_of and _field_type_kind(field_of[fname]) == "serial":
                    # A SERIAL column fills itself in, and is NOT NULL.
                    values.append("DEFAULT")
                else:
                    values.append(_seed_literal(raw))
            lines.append(
                f"INSERT INTO {slug} ({', '.join(col_names)}) "
                f"VALUES ({', '.join(values)});"
            )
        lines.append("")
    return ("db/seed.sql", "\n".join(lines))


def _seed_literal(raw: Any) -> str:
    """A seed value as the SQL literal `emit_seed_sql` writes: NULL, TRUE or
    FALSE, a bare number, or a quoted string (a list or an object as its
    JSON text). `_stored_value_problem` says whether a column takes it."""
    if raw is None:
        return "NULL"
    if isinstance(raw, bool):
        return "TRUE" if raw else "FALSE"
    if isinstance(raw, float) and not math.isfinite(raw):
        # json.load accepts NaN / Infinity; str() of those is a bare word
        # Postgres would read as a column reference.
        return "NULL"
    if isinstance(raw, (int, float)):
        return str(raw)
    if isinstance(raw, (list, dict)):
        # JSONB columns (e.g. a photos array): emit valid JSON, not Python
        # repr. `_sql_esc(["a","b"])` would produce '[''a'', ''b'']' which
        # Postgres rejects as invalid json, crashing the seed apply at app
        # boot.
        return "'" + json.dumps(raw).replace("'", "''") + "'"
    return f"'{_sql_esc(raw)}'"


def _placeholder_rows(model_name: str, rs: "ResolvedSpec" = None) -> list:
    """Minimum rows when spec doesn't provide seed_content. Deterministic for
    one build: the only calendar value, the sample event's date, is counted
    forward from the build day (see _sample_date)."""
    if model_name == "BlogPost":
        return [
            {"title": "Welcome", "slug": "welcome", "excerpt": "Our first post.", "body": "Welcome to our journal.", "image_url": None, "published": True, "published_at": "2026-01-01"},
        ]
    if model_name == "Event":
        return [
            {"title": "Open House", "event_date": _sample_date(30, rs), "location": "TBD", "description": "Come meet us.", "registration_url": None, "image_url": None},
        ]
    if model_name == "Testimonial":
        return [
            {"author_name": "Sample Client", "quote": "This changed everything for me.", "service": "1:1 Session", "published": True},
        ]
    if model_name == "Service":
        return [
            {"title": "Core Service", "description": "Our primary offering.", "price_display": "Contact for pricing", "image_url": None},
        ]
    if model_name == "Book":
        return [
            {
                "slug": "sample-book",
                "title": "Sample Book",
                "subtitle": "A working placeholder",
                "series_label": "STANDALONE",
                "description": "Replace this row in /admin/books with your first title. The widget composes Amazon as the primary CTA and Goodreads, your local bookstore, Barnes & Noble, NetGalley, and Audible as secondary links.",
                "cover_image_url": None,
                "cover_image_alt": "Sample book cover",
                "cover_credit": None,
                "accolade": None,
                "rating_callout": None,
                "price_display": "Paperback & Kindle",
                "amazon_url": None,
                "goodreads_url": None,
                "local_bookstore_url": None,
                "barnesnoble_url": None,
                "netgalley_url": None,
                "audible_url": None,
                "featured_on_home": True,
                "sort_order": 0,
                "published": True,
            },
        ]
    return []


def emit_server_js(rs: ResolvedSpec) -> tuple:
    """Express server with session + magic-link auth + dual-mounted
    resource routers (MW v25 pattern).

    Each resource router mounts at BOTH /<slug> (public) and
    /admin/<slug> (requireAdmin-guarded). Router internals branch on
    `req.baseUrl.startsWith('/admin/')` to pick admin vs public view.
    """
    # Build the requireAdmin import first so it's available for dual mounts.
    # Public mount uses public_listing_path (e.g. /gallery) when set; admin
    # mount is always /admin/<slug>.
    #
    # Always mount public + admin. When the public
    # path collides with an archetype default page, the router's list
    # handler calls `next()` to yield to the pages router (which has the
    # archetype composed page handler at the same URL). This lets
    # /events render the archetype's composed list view while /events/:id
    # still flows to this router's public detail handler; the previous
    # design (skip public mount entirely) broke the detail
    # route when the default /<plural> collided with an archetype page.
    # Also covers explicit public_listing_path collisions (e.g.
    # fitness-studio's ScheduleSlot with public_listing_path=/schedule,
    # which that design did not cover and left /schedule rendering the
    # generic CRUD card list instead of the composed weekly grid widget).
    mount_lines = []
    for m in rs.data_models:
        slug = _slug_for(m)
        public_path = _route_path(_public_model_mount(m)[0])
        mount_lines.append(f"app.use('{public_path}', require('./routes/{slug}'));")
        mount_lines.append(f"app.use('/admin/{slug}', requireAdmin, require('./routes/{slug}'));")
    mounts = "\n".join(mount_lines)
    contents = f'''require('dotenv').config();
const express = require('express');
const session = require('express-session');
const pgSession = require('connect-pg-simple')(session);
const expressLayouts = require('express-ejs-layouts');
const helmet = require('helmet');
const morgan = require('morgan');
const compression = require('compression');
const crypto = require('crypto');
const path = require('path');
const db = require('./db');
const {{ pathOnly }} = require('./middleware/logging');
const {{ createHomeCheck }} = require('./services/health');

const app = express();
// Whether this site's own home page renders; see /health below.
const homeCheck = createHomeCheck();

app.set('trust proxy', 1);
app.set('views', path.join(__dirname, 'views'));
app.set('view engine', 'ejs');
app.set('layout', 'layout-public');
app.use(expressLayouts);

// Access log: the Apache combined format with two fields narrowed. The
// request line and the Referer are written WITHOUT their query strings,
// because a query string can be a credential: the login link is
// /magic?token=<raw token>, and opening it (GET or HEAD) does not consume
// the token, so a logged link would sign in whoever reads the log. Nothing
// here depends on a list of routes; no query string is logged at all.
morgan.token('url-path', (req) => pathOnly(req.originalUrl || req.url));
morgan.token('referrer-path', (req) => pathOnly(req.headers.referer || req.headers.referrer) || '-');
app.use(morgan(
  ':remote-addr - :remote-user [:date[clf]] ":method :url-path HTTP/:http-version" ' +
  ':status :res[content-length] ":referrer-path" ":user-agent"'
));
app.use(compression());
app.use(helmet({{contentSecurityPolicy: false}}));

// Per-request CSP nonce, the same pattern the platform's own web app
// uses. Every inline `<script>` tag in
// compose-emitted widgets carries `nonce="<%= cspNonce %>"`; the
// script-src directive below trusts only scripts carrying this nonce
// (no `'unsafe-inline'`). `unsafe-inline` is INTENTIONALLY kept on
// style-src because inline `style="..."` attributes are pervasive in
// the widget set and CSP nonces don't apply to attributes.
app.use((req, res, next) => {{
  const nonce = crypto.randomBytes(24).toString('base64');
  res.locals.cspNonce = nonce;
  res.setHeader(
    'Content-Security-Policy',
    "default-src 'self'; " +
      "script-src 'self' 'nonce-" + nonce + "'; " +
      "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; " +
      "img-src 'self' data: https:; " +
      "font-src 'self' https://fonts.gstatic.com data:; " +
      "connect-src 'self'; " +
      // The only third-party frames any widget emits: the two video
      // players (content/video-embed-nocookie) and a Google Maps embed
      // (offerings/hours-location-block). `frame-src 'self'` alone, the
      // previous value, blocked all three.
      "frame-src 'self' https://www.youtube-nocookie.com https://player.vimeo.com https://www.google.com; " +
      // default-src does not cover these three. Without them an injected
      // <base> or <form action> could redirect the login form off-site.
      "base-uri 'self'; " +
      "form-action 'self'; " +
      "object-src 'none'; " +
      "frame-ancestors 'self'"
  );
  next();
}});
// Body parsers keep Express's default 100kb cap; uploads go through multer
// (routes/upload.js) with its own limit.
app.use(express.urlencoded({{ extended: true }}));
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Prevent browsers from caching HTML pages. Static assets (CSS, JS, SVG)
// already have content-hash URLs so they cache correctly; express.static
// handles them above and never reaches this middleware.
// Deliberately NOT no-store: several link-preview crawlers (Facebook
// Messenger among them) refuse to build an Open Graph card for a
// no-store resource. no-cache + must-revalidate still forces browsers
// to revalidate before reuse, so new hashed-asset URLs propagate.
app.use((req, res, next) => {{
  res.set('Cache-Control', 'no-cache, must-revalidate');
  next();
}});

// Session signing secret. There is deliberately NO built-in fallback value:
// a secret that ships in the source lets anyone forge a signed session id.
// Production refuses to boot without one. Outside production a random
// per-process secret is used, so local runs work but every restart signs
// the visitor out (set SESSION_SECRET in .env to keep sessions).
let sessionSecret = process.env.SESSION_SECRET;
if (!sessionSecret || sessionSecret.length < 16) {{
  if (process.env.NODE_ENV === 'production') {{
    console.error('FATAL: SESSION_SECRET is not set (or is shorter than 16 characters). Refusing to start.');
    process.exit(1);
  }}
  console.warn('SESSION_SECRET is not set; using a random per-process secret (development only).');
  sessionSecret = crypto.randomBytes(32).toString('hex');
}}

app.use(session({{
  store: new pgSession({{ pool: db.pool, tableName: 'user_sessions' }}),
  secret: sessionSecret,
  resave: false,
  saveUninitialized: false,
  cookie: {{
    secure: process.env.NODE_ENV === 'production',
    httpOnly: true,
    sameSite: 'lax',
    maxAge: 30 * 24 * 60 * 60 * 1000
  }}
}}));

// Site-settings + CSRF middleware
const {{ siteSettingsMiddleware }} = require('./middleware/settings');
const {{ csrfMiddleware }} = require('./middleware/csrf');
const {{ requireAdmin }} = require('./middleware/auth');
const {{ slugAliasMiddleware }} = require('./middleware/slug-aliases');
app.use(siteSettingsMiddleware);
app.use(csrfMiddleware);
// 301s for renamed slugs; mounted before the routers so a
// historical URL never 404s after a rename.
app.use(slugAliasMiddleware);

// currentPath + user helper for nav active-state + admin-aware rendering.
app.use((req, res, next) => {{
  res.locals.currentPath = req.path;
  if (req.session && req.session.userId) {{
    res.locals.user = {{ id: req.session.userId, email: req.session.email, role: req.session.role }};
  }} else {{
    res.locals.user = null;
  }}
  next();
}});

// Health. /health is served only after the listener binds below, which
// happens after migration. It answers {{status: 'ok'}} when two things
// hold, and 503 otherwise:
//   1. the database answers a query now (a broken connection is a
//      'db_error', not a misleading 200 from a server that booted past
//      a silent migration warning);
//   2. this site's own home page rendered when it was last asked for
//      (services/health.js: one request to "/" over loopback when the
//      listener comes up; a pass is remembered for the life of the
//      process, a failure is tried again on a later probe). A site whose
//      home page throws for every visitor is not healthy, whatever the
//      database says: 'home_page_error'.
app.get('/health', async (req, res) => {{
  try {{
    await db.query('SELECT 1');
  }} catch (e) {{
    // The detail goes to the log only: /health is unauthenticated, and a
    // driver error message can carry the database host, user or role.
    console.error('[health] DB check failed:', e && e.message ? e.message : e);
    res.status(503).json({{status: 'db_error'}});
    return;
  }}
  if (!(await homeCheck.ok())) {{
    res.status(503).json({{status: 'home_page_error'}});
    return;
  }}
  res.json({{status: 'ok'}});
}});

// Auth routes (login, logout)
app.use('/', require('./routes/auth'));

// Legal pages (/privacy, /terms). Mount BEFORE pages so the static
// boilerplate wins over any custom_page that tries to claim those paths.
// Every nav and footer links /privacy and /terms, so both need a
// handler.
app.use('/', require('./routes/legal'));

// SEO surface (/robots.txt, /sitemap.xml). Mount BEFORE the pages router
// so the dynamic handlers serve before any custom_page or static-asset
// resolution could shadow them.
app.use('/', require('./routes/seo'));

// Admin dashboard (manage-cards landing)
app.use('/admin', require('./routes/admin-dashboard'));

// Admin settings (separate router; mounts at /admin/settings)
app.use('/admin', require('./routes/admin-settings'));

// Admin upload endpoint (multipart image uploads; /admin/upload)
app.use('/admin', require('./routes/upload'));

// Resource routers, each mounted twice:
// the public path = a listing and detail pages, only for a model this site
//   lists publicly; for any other model the router reads no row there
// /admin/<slug> = admin manage + CRUD, requireAdmin-guarded
{mounts}

// Public pages (home, about, contact, etc.) mount LAST so dynamic
// resource routes take precedence.
app.use('/', require('./routes/pages'));

// 404 + errors
app.use((req, res) => res.status(404).render('error-404', {{ title: '404' }}));
app.use((err, req, res, next) => {{
  // A request body the parsers rejected (malformed JSON, too many fields,
  // too large) is the client's mistake, not a crash. The parser attaches
  // the raw body to the error object, and the body of the login,
  // set-password and login-link forms is a credential, so only the error
  // type is logged and the answer is the parser's own 4xx status.
  const status = err ? Number(err.status || err.statusCode) : 0;
  if (err && typeof err.type === 'string' && status >= 400 && status < 500) {{
    console.error('[request] body rejected (' + err.type + ', ' + status + '): ' + req.method + ' ' + pathOnly(req.originalUrl));
    return res.status(status).type('text/plain').send('The request could not be processed.');
  }}
  console.error(err);
  res.status(500).render('error-500', {{ title: 'Error' }});
}});

// Boot sequence: run migration (schema + seed) before starting the
// HTTP listener, so the first request never sees a half-seeded DB.
// Settings middleware caches its query for 30s on first success;
// serving traffic before seed completes caches an empty object and
// every page renders with a missing brand name. Awaiting migration
// here is the deterministic fix.
//
// Migration MUST succeed for the app to be usable; every page handler
// reads from the DB. If migration throws, log the FULL error (stack +
// every available property) and exit non-zero so the container's
// orchestrator (Railway / docker-compose / k8s) sees an honest
// failure instead of a half-running app whose every request 500s.
// Earlier behavior (catch + log .message + listen anyway) produced
// a "Server running on port 3000" line followed by Railway flagging
// CRASHED minutes later: opaque to debug because the actual error
// was swallowed.
(async () => {{
  try {{
    await require('./db_migrate')();
  }} catch (e) {{
    console.error('================================================================');
    console.error('FATAL: Schema migration failed; app cannot serve requests.');
    console.error('error.message:', e && e.message);
    console.error('error.code:', e && e.code);
    console.error('error.detail:', e && e.detail);
    console.error('error.hint:', e && e.hint);
    console.error('error.where:', e && e.where);
    console.error('error.stack:', e && e.stack);
    console.error('full error JSON:', JSON.stringify(e, Object.getOwnPropertyNames(e || {{}})));
    console.error('================================================================');
    process.exit(1);
  }}
  const PORT = process.env.PORT || 4000;
  app.listen(PORT, () => {{
    console.log('Server running on port ' + PORT);
    // Ask for the home page once, now that something is listening.
    homeCheck.start(PORT);
  }});
}})();
'''
    return ("server.js", contents)


def emit_db_js(rs: ResolvedSpec) -> tuple:
    contents = '''const { Pool } = require('pg');
// connectionTimeoutMillis bounds individual connect attempts at 10s so a
// hung connect (Railway internal-network race after fresh provision)
// fails fast and the migration retry loop in db_migrate.js can take its
// next attempt instead of waiting for Node's default ~60s socket timeout.
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  connectionTimeoutMillis: 10000,
  idleTimeoutMillis: 30000,
});
module.exports = {
  pool,
  query: (text, params) => pool.query(text, params)
};
'''
    return ("db.js", contents)


def emit_db_migrate_js(rs: ResolvedSpec) -> tuple:
    contents = '''const fs = require('fs');
const path = require('path');
const db = require('./db');

// Connection-class errors that mean "PG isn't ready yet, try again."
// Anything else (auth fail, syntax error, missing role) should fail
// loud immediately because retry won't help.
const TRANSIENT_CONNECT_CODES = new Set([
  'ECONNREFUSED',  // listener not up
  'ETIMEDOUT',     // connect attempt hung
  'ENETUNREACH',   // network not yet routable
  'EHOSTUNREACH',  // host route not yet there
  'ENOTFOUND',     // DNS not propagated yet
  'EAI_AGAIN',     // DNS temporarily unavailable
]);

function isTransientConnectError(e) {
  if (!e) return false;
  if (e.code && TRANSIENT_CONNECT_CODES.has(e.code)) return true;
  // Node 18+ AggregateError wraps multiple attempts; if ALL inner errors
  // are transient, treat the aggregate as transient.
  if (Array.isArray(e.errors) && e.errors.length > 0) {
    return e.errors.every((inner) => inner && inner.code && TRANSIENT_CONNECT_CODES.has(inner.code));
  }
  return false;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Wait for the DB to accept connections before running migration.
// Railway's freshly-provisioned PG can take 30-90s to start accepting
// connections on the internal network even after the worker's
// wait_for_deployment reports the service deployed. Retries up to ~2 min
// (12 attempts spaced 10s apart) with the Pool's per-attempt
// connectionTimeoutMillis=10s ceiling, so total budget bounded around
// 240s worst-case. Only retries on connection-class errors; auth
// failures, schema errors, etc. fail immediately.
async function waitForDb(maxAttempts) {
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      await db.query('SELECT 1');
      if (attempt > 1) console.log(`DB reachable after attempt ${attempt}/${maxAttempts}`);
      return;
    } catch (e) {
      if (!isTransientConnectError(e)) throw e;
      const codeTag = (e && e.code) || (e && Array.isArray(e.errors) && e.errors[0] && e.errors[0].code) || 'unknown';
      if (attempt === maxAttempts) {
        console.error(`waitForDb: gave up after ${maxAttempts} attempts (last error code: ${codeTag})`);
        throw e;
      }
      console.warn(`waitForDb attempt ${attempt}/${maxAttempts} failed (code=${codeTag}); retrying in 10s...`);
      await sleep(10000);
    }
  }
}

module.exports = async function migrate() {
  await waitForDb(12);
  const schema = fs.readFileSync(path.join(__dirname, 'db', 'schema.sql'), 'utf-8');
  await db.query(schema);
  console.log('Schema migration complete.');

  // Apply seed only if users table is empty (idempotent)
  const { rows } = await db.query('SELECT COUNT(*) FROM users');
  if (parseInt(rows[0].count) === 0) {
    const seed = fs.readFileSync(path.join(__dirname, 'db', 'seed.sql'), 'utf-8');
    await db.query(seed);
    console.log('Seed data loaded.');
  } else {
    console.log('Seed data skipped (users table not empty).');
  }

  // Always run db/post-migrate.sql on every boot when present. The file
  // must be self-idempotent (WHERE NOT EXISTS / ON CONFLICT / etc.) so it
  // can run safely on every boot regardless of DB state. Use this for
  // one-shot post-deploy data: settings updates that need to land on an
  // existing DB, magic-link bootstrap on a redeploy, data backfills,
  // admin password resets, etc. Missing file is a no-op (not an error)
  // so apps that don't need it stay clean.
  const postMigratePath = path.join(__dirname, 'db', 'post-migrate.sql');
  if (fs.existsSync(postMigratePath)) {
    const postMigrate = fs.readFileSync(postMigratePath, 'utf-8');
    await db.query(postMigrate);
    console.log('Post-migrate SQL applied.');
  }
};

if (require.main === module) {
  module.exports().then(() => process.exit(0)).catch(e => { console.error(e); process.exit(1); });
}
'''
    return ("db_migrate.js", contents)


def emit_middleware_logging_js(rs: ResolvedSpec) -> tuple:
    """`pathOnly`, the one helper every log line that names a request goes
    through (the access log in server.js, the CSRF-mismatch line in
    middleware/auth.js, the rejected-body line in server.js).

    It has no dependencies on purpose, so the middleware that uses it can be
    loaded on its own.
    """
    contents = '''// What is safe to log of a request target or a Referer value: everything
// before the first "?" or "#". The query string is dropped because it can
// carry a credential (the login link is /magic?token=<raw token>).
function pathOnly(target) {
  const text = typeof target === 'string' ? target : '';
  const cut = text.search(/[?#]/);
  return cut === -1 ? text : text.slice(0, cut);
}

module.exports = { pathOnly };
'''
    return ("middleware/logging.js", contents)


def emit_middleware_auth_js(rs: ResolvedSpec) -> tuple:
    contents = '''const crypto = require('crypto');
const { pathOnly } = require('./logging');

function requireAuth(req, res, next) {
  if (!req.session || !req.session.userId) {
    return res.redirect('/login');
  }
  next();
}

function requireAdmin(req, res, next) {
  if (!req.session || !req.session.userId || req.session.role !== 'admin') {
    return res.redirect('/login');
  }
  next();
}

// Passes a signed-in account whose role is ONE OF `roles`. A page that
// lists several roles gets a single requireAnyRole([...]) guard: chaining
// one requireRole per role would demand that the account hold every listed
// role at once, which no account can (users.role is a single value).
function requireAnyRole(roles) {
  const allowed = new Set(Array.isArray(roles) ? roles : [roles]);
  return function(req, res, next) {
    if (!req.session || !req.session.userId || !allowed.has(req.session.role)) {
      return res.redirect('/login');
    }
    next();
  };
}

function requireRole(role) {
  return requireAnyRole([role]);
}

// Constant-time comparison of the submitted token with the session's.
// Rejects anything that is not a non-empty string (an absent session token
// must never match an absent submitted one).
function tokensMatch(submitted, expected) {
  if (typeof submitted !== 'string' || typeof expected !== 'string' || !expected) return false;
  const a = Buffer.from(submitted);
  const b = Buffer.from(expected);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function validateCsrfToken(req, res, next) {
  const header = req.headers['x-csrf-token'];
  // Defensive: if a form somehow renders multiple `_csrf` hidden inputs
  // (e.g., a nested <form> that browsers flatten, as the admin edit form
  // once did), express body-parser delivers an array. Treat
  // all same-valued array members as a single token; reject only when the
  // values actually disagree.
  let body = req.body && req.body._csrf;
  if (Array.isArray(body)) {
    const unique = [...new Set(body.map(String))];
    body = unique.length === 1 ? unique[0] : null;
  }
  const token = header || body;
  if (!tokensMatch(token, req.session && req.session.csrfToken)) {
    // Log the mismatch so ops can diagnose if it becomes a pattern. We log
    // short prefixes (not full tokens) to avoid leaking secrets, and the
    // path without its query string for the same reason.
    try {
      console.error('[csrf-mismatch]', JSON.stringify({
        method: req.method,
        url: pathOnly(req.originalUrl),
        hasHeader: Boolean(header),
        hasBody: Boolean(body),
        bodyCsrfPrefix: body ? String(body).substring(0, 8) : null,
        sessionCsrfPrefix: req.session && req.session.csrfToken ? req.session.csrfToken.substring(0, 8) : null,
        sessionUserId: req.session && req.session.userId ? req.session.userId : null,
      }));
    } catch (_logErr) { /* never let logging itself throw */ }
    // JSON/API clients (identified by x-csrf-token header) keep the 403 JSON.
    if (header) return res.status(403).json({ error: 'Invalid CSRF token' });
    // Form submitters: the error-500 page ("Something Went Wrong") would be
    // indistinguishable from a real crash and would hide the real cause.
    // Render a dedicated 403 page that tells the user the session token
    // expired and invites them to go back and retry. The Referer header is
    // deliberately not passed to the view: for a cross-site POST (the case
    // this check exists for) it names the attacker's page.
    return res.status(403).render('error-csrf', {
      title: 'Session Expired',
      layout: 'layout-public',
    });
  }
  next();
}

module.exports = { requireAuth, requireAdmin, requireRole, requireAnyRole, validateCsrfToken };
'''
    return ("middleware/auth.js", contents)


def emit_middleware_csrf_js(rs: ResolvedSpec) -> tuple:
    contents = '''const crypto = require('crypto');

function csrfMiddleware(req, res, next) {
  if (!req.session.csrfToken) {
    req.session.csrfToken = crypto.randomBytes(32).toString('hex');
  }
  res.locals.csrfToken = req.session.csrfToken;
  next();
}

module.exports = { csrfMiddleware };
'''
    return ("middleware/csrf.js", contents)


def emit_middleware_rate_limit_js(rs: ResolvedSpec) -> tuple:
    """Per-IP request limits for the unauthenticated POST surface.

    Three limiters, each keyed on req.ip (server.js sets `trust proxy` to 1,
    so that is the client address behind one reverse proxy):

      - authLimiter: password login and magic-link consumption. Slows
        online password guessing against the single admin account.
      - magicLinkRequestLimiter: POST /request-magic-link. Each accepted
        request stores a token row and sends an email, so this bounds both
        mailbox flooding and table growth.
      - formLimiter: public form posts (contact, newsletter, custom forms),
        which write a row and may send an admin notification.

    Limits of this design, stated so nobody assumes more: the counters live
    in process memory (they reset on restart and are not shared between
    instances), and they are per address, not per account, so a guesser who
    rotates addresses is not stopped.
    """
    contents = '''const { rateLimit } = require('express-rate-limit');

function limiter(windowMinutes, limit) {
  return rateLimit({
    windowMs: windowMinutes * 60 * 1000,
    limit,
    standardHeaders: true,
    legacyHeaders: false,
    message: 'Too many requests. Please wait a few minutes and try again.',
  });
}

// Password login + magic-link consumption: 10 attempts per 15 minutes.
const authLimiter = limiter(15, 10);
// Login-link requests: 5 per hour (each one stores a token and sends mail).
const magicLinkRequestLimiter = limiter(60, 5);
// Public form posts: 20 per 15 minutes.
const formLimiter = limiter(15, 20);

module.exports = { authLimiter, magicLinkRequestLimiter, formLimiter };
'''
    return ("middleware/rate-limit.js", contents)


def emit_middleware_fields_js(rs: ResolvedSpec) -> tuple:
    """Standardized field sanitize + validate helpers.

    Every CRUD mutation and the /contact POST route run field values
    through this before hitting Postgres. The shapes of `sanitize` and
    `pattern` match what the field-types registry emits, so front-end
    HTML5 validation and back-end validation stay in sync.
    """
    # Inline the registry + state list so the middleware has zero extra
    # file I/O at runtime.
    ft_json = json.dumps(load_field_types()["field_types"])
    states_json = json.dumps([s[0] for s in load_field_types()["us_states"]])
    contents = f'''const FIELD_TYPES = {ft_json};
const US_STATE_CODES = new Set({states_json});

function _applySanitize(value, steps) {{
  if (value == null) return value;
  let v = String(value);
  for (const step of (steps || [])) {{
    if (step === 'trim') v = v.trim();
    else if (step === 'trim_multiline') v = v.split(/\\r?\\n/).map(s => s.trim()).join('\\n').trim();
    else if (step === 'lowercase') v = v.toLowerCase();
    else if (step === 'upper') v = v.toUpperCase();
    else if (step === 'digits_only') v = v.replace(/\\D+/g, '');
    else if (step === 'null_if_empty') {{ if (v === '') return null; }}
    else if (step === 'checkbox_bool') {{ return (v === '1' || v === 'on' || v === 'true'); }}
    // bcrypt_hash is applied elsewhere (auth route only); skip here.
  }}
  return v;
}}

function sanitizeField(fieldTypeId, rawValue) {{
  const ft = FIELD_TYPES[fieldTypeId];
  if (!ft) return rawValue;
  return _applySanitize(rawValue, ft.sanitize);
}}

function validateField(fieldTypeId, sanitizedValue, {{ required = false }} = {{}}) {{
  const ft = FIELD_TYPES[fieldTypeId];
  if (!ft) return {{ ok: true }};
  if (sanitizedValue == null || sanitizedValue === '') {{
    if (required) return {{ ok: false, error: 'is required' }};
    return {{ ok: true }};
  }}
  if (ft.maxlength && String(sanitizedValue).length > ft.maxlength) {{
    return {{ ok: false, error: `exceeds ${{ft.maxlength}} characters` }};
  }}
  if (ft.pattern) {{
    try {{
      const re = new RegExp(ft.pattern);
      if (!re.test(String(sanitizedValue))) {{
        return {{ ok: false, error: 'format is invalid' }};
      }}
    }} catch (e) {{}}
  }}
  if (fieldTypeId === 'state_us' && !US_STATE_CODES.has(String(sanitizedValue).toUpperCase())) {{
    return {{ ok: false, error: 'unknown US state code' }};
  }}
  return {{ ok: true }};
}}

function processForm(fieldSpec, body) {{
  // fieldSpec: array of {{ name, field_type, required? }}
  // Returns {{ values: {{col: value}}, errors: {{col: msg}} }}
  const values = {{}};
  const errors = {{}};
  for (const spec of fieldSpec) {{
    const raw = body[spec.name];
    const clean = spec.field_type ? sanitizeField(spec.field_type, raw) : (raw === '' ? null : raw);
    const check = spec.field_type ? validateField(spec.field_type, clean, {{ required: !!spec.required }}) : {{ ok: true }};
    if (!check.ok) {{
      errors[spec.name] = `${{spec.label || spec.name}} ${{check.error}}`;
      continue;
    }}
    values[spec.name] = clean;
  }}
  return {{ values, errors }};
}}
''' + _PUBLIC_FORM_FIELDS_JS.replace("__MAX_LENGTH__", str(_PUBLIC_FIELD_MAX_LENGTH))
    return ("middleware/fields.js", contents)


# The second half of middleware/fields.js. A plain string, not an f-string:
# every brace below is a JavaScript brace and is written once.
_PUBLIC_FORM_FIELDS_JS = '''
// ---- Public forms (the contact form and the custom form pages) ----
// What a visitor posts is checked field by field before any INSERT:
// against a length limit, against the field type (processForm above) and
// against what the column it is stored in can hold.
const PUBLIC_FIELD_MAX_LENGTH = __MAX_LENGTH__;
const INT_RANGES = {
  16: [-32768n, 32767n],
  32: [-2147483648n, 2147483647n],
  64: [-9223372036854775808n, 9223372036854775807n],
};

function _isCalendarDate(text) {
  const parts = /^(\\d{4})-(\\d{2})-(\\d{2})$/.exec(text);
  if (!parts) return false;
  const year = Number(parts[1]);
  const month = Number(parts[2]);
  const day = Number(parts[3]);
  const date = new Date(Date.UTC(year, month - 1, day));
  return year >= 1000 && date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

// Why `value` cannot be stored in the column `spec` describes, or null.
function columnProblem(spec, value) {
  const text = String(value);
  if (Array.isArray(spec.options) && spec.options.length > 0 && !spec.options.includes(text)) {
    return 'is not one of the choices';
  }
  switch (spec.column) {
    case 'integer': {
      if (!/^-?\\d{1,19}$/.test(text)) return 'must be a whole number';
      const range = INT_RANGES[spec.int_bits] || INT_RANGES[32];
      const number = BigInt(text);
      return (number < range[0] || number > range[1]) ? 'is out of range' : null;
    }
    case 'number':
      return /^-?\\d{1,15}(\\.\\d{1,10})?$/.test(text) ? null : 'must be a number';
    case 'date':
      return _isCalendarDate(text) ? null : 'must be a date (YYYY-MM-DD)';
    case 'time':
      return /^([01]\\d|2[0-3]):[0-5]\\d(:[0-5]\\d)?$/.test(text) ? null : 'must be a time (HH:MM)';
    case 'timestamp':
      return (text.length <= 40 && !Number.isNaN(Date.parse(text))) ? null : 'must be a date and time';
    case 'boolean':
      return (typeof value === 'boolean' || ['on', '1', 'true', 'off', '0', 'false'].includes(text.toLowerCase()))
        ? null : 'must be yes or no';
    default:
      return null;
  }
}

// fieldSpec: array of { name, field_type, required, label, max_length,
// column, int_bits?, options? }, written at build time from the model.
// Returns { values, errors, submitted }: the values to store, one message
// per rejected field, and what was typed (for showing the form again).
function processPublicForm(fieldSpec, rawBody) {
  const body = {};
  const submitted = {};
  const errors = {};
  for (const spec of fieldSpec) {
    const raw = rawBody ? rawBody[spec.name] : undefined;
    // Strings only: with the extended urlencoded parser a client can send
    // name[]=x and turn a field into an array or an object.
    const text = typeof raw === 'string' ? raw : '';
    const limit = spec.max_length || PUBLIC_FIELD_MAX_LENGTH;
    // What goes back to the form is cut to the limit, so an oversized
    // value is not kept in the session.
    submitted[spec.name] = text.slice(0, limit);
    if (text.length > limit) {
      errors[spec.name] = (spec.label || spec.name) + ' is too long (at most ' + limit + ' characters)';
    } else if (typeof raw === 'string') {
      body[spec.name] = raw;
    }
  }
  const checked = processForm(fieldSpec.filter((spec) => !errors[spec.name]), body);
  Object.assign(errors, checked.errors);
  const values = checked.values;
  for (const spec of fieldSpec) {
    const value = values[spec.name];
    if (errors[spec.name] || value === undefined || value === null || value === '') continue;
    const problem = columnProblem(spec, value);
    if (problem) errors[spec.name] = (spec.label || spec.name) + ' ' + problem;
  }
  return { values, errors, submitted };
}

module.exports = { FIELD_TYPES, sanitizeField, validateField, processForm, processPublicForm, columnProblem };
'''


def emit_middleware_slug_aliases_js(rs: ResolvedSpec) -> tuple:
    """301 redirects for renamed slugs. Reads the
    slug_aliases table with a 60s in-memory cache (same pattern as the
    settings middleware) so the per-request cost is a Map lookup. Fails
    open: a DB error never blocks a page render."""
    contents = """const db = require('../db');

let _cachedAliases = null;
let _cachedAt = 0;

async function loadAliases() {
  const now = Date.now();
  if (_cachedAliases && (now - _cachedAt) < 60000) return _cachedAliases;
  try {
    const { rows } = await db.query('SELECT old_path, new_path FROM slug_aliases');
    const map = new Map();
    for (const r of rows) {
      if (r.old_path && r.new_path && r.old_path !== r.new_path) {
        map.set(r.old_path, r.new_path);
      }
    }
    _cachedAliases = map;
    _cachedAt = now;
    return map;
  } catch (e) {
    // Fail open: missing table / cold DB must never block a render.
    return _cachedAliases || new Map();
  }
}

async function slugAliasMiddleware(req, res, next) {
  if (req.method !== 'GET' && req.method !== 'HEAD') return next();
  try {
    const aliases = await loadAliases();
    if (aliases.size === 0) return next();
    const target = aliases.get(req.path);
    if (target) return res.redirect(301, target);
  } catch (e) { /* fail open */ }
  next();
}

function invalidateAliasCache() { _cachedAliases = null; }

module.exports = { slugAliasMiddleware, invalidateAliasCache };
"""
    return ("middleware/slug-aliases.js", contents)


def emit_middleware_settings_js(rs: ResolvedSpec) -> tuple:
    # Build the palette + font constants inline for fast middleware access
    palette_vars = rs.palette_vars
    fp = rs.font_pair
    palette_json = json.dumps(palette_vars)
    font_json = json.dumps({
        "display": fp["display"],
        "body": fp["body"],
    })
    # Suppress the Google Fonts URL when this build is self-hosting the
    # WOFF2 family. Empty `_fu` makes layout-public.ejs skip the
    # external <link href="fonts.googleapis.com/..."> tag, completing
    # the round-trip elimination. Falls through to the family's
    # google_fonts_url when self-hosting isn't running for this build.
    if _self_host_fonts_plan(rs) is not None:
        fonts_url = ""
    else:
        fonts_url = fp["google_fonts_url"]
    # Inline the field-types registry so form/field.ejs can read
    # settings._field_types from every view context.
    ft_json = json.dumps(load_field_types()["field_types"])
    us_states_json = json.dumps(load_field_types()["us_states"])
    contents = f'''const db = require('../db');

const PALETTE_VARS = {palette_json};
const FONTS = {font_json};
const FONTS_URL = {json.dumps(fonts_url)};
const FIELD_TYPES = {ft_json};
const US_STATES = {us_states_json};

let _cachedSettings = null;
let _cachedAt = 0;

async function loadSettings() {{
  const now = Date.now();
  if (_cachedSettings && (now - _cachedAt) < 30000) return _cachedSettings;
  try {{
    const {{ rows }} = await db.query('SELECT key, value FROM site_settings');
    const out = {{
      _palette_vars: PALETTE_VARS, _fonts: FONTS, _fonts_url: FONTS_URL,
      _field_types: FIELD_TYPES, _us_states: US_STATES
    }};
    // Keys starting with "_" are the build-time constants set just above
    // (palette, fonts, field types). The layouts write the palette and
    // font names into a <style> block, so a database row must never be
    // able to replace them.
    for (const r of rows) {{
      if (typeof r.key === 'string' && r.key.charAt(0) !== '_') out[r.key] = r.value;
    }}
    // Don't cache an empty result. If the table exists but has no rows
    // (seed still in flight on boot), return the uncached object so the
    // next request retries the DB. Prevents "stale empty" cache locking
    // site_name=undefined for 30s across every page.
    if (rows.length > 0) {{
      _cachedSettings = out;
      _cachedAt = now;
    }}
    return out;
  }} catch (e) {{
    return {{
      _palette_vars: PALETTE_VARS, _fonts: FONTS, _fonts_url: FONTS_URL,
      _field_types: FIELD_TYPES, _us_states: US_STATES,
      site_name: 'Site', site_description: '', contact_email: ''
    }};
  }}
}}

const _NAV_ITEMS = {json.dumps(_default_nav_items(rs))};
const _ADMIN_NAV_ITEMS = {json.dumps(_default_admin_nav_items(rs))};

async function siteSettingsMiddleware(req, res, next) {{
  try {{
    res.locals.settings = await loadSettings();
  }} catch (e) {{
    console.error('settings middleware:', e);
    res.locals.settings = {{
      _palette_vars: PALETTE_VARS, _fonts: FONTS, _fonts_url: FONTS_URL,
      _field_types: FIELD_TYPES, _us_states: US_STATES,
      site_name: 'Site', site_description: '', contact_email: ''
    }};
  }}
  // requestOrigin: the proto+host the request ACTUALLY arrived on.
  // Absolute OG/canonical URLs must follow the live host, not the
  // eventual custom domain: when site_url in the DB is a future domain
  // (set before DNS cutover), og:image built from it 404s on the
  // current host and link previews show no image. The layout prefers
  // requestOrigin and falls back to site_url, so previews work on the
  // preview host now AND the custom domain later with no edit.
  try {{
    var _roProto = req.protocol || 'https';
    var _roHost = req.get && req.get('host');
    res.locals.requestOrigin = _roHost ? (_roProto + '://' + _roHost) : '';
  }} catch (e) {{
    res.locals.requestOrigin = '';
  }}
  // Auto-populate site_url from the live request when blank in DB. Social
  // crawlers (FB, iMessage, Twitter, Slack, Discord) require ABSOLUTE
  // og:url + og:image URLs to render link-preview cards; without this,
  // a fresh deploy would ship with empty site_url until an admin opens
  // /admin/settings, breaking previews on every share. We mutate a copy
  // (Object.assign) so the cached settings object stays untouched and an
  // admin's later explicit value still wins on the next cache refresh.
  if (!res.locals.settings.site_url) {{
    try {{
      var _proto = req.protocol || 'https';
      var _host = req.get && req.get('host');
      if (_host) {{
        res.locals.settings = Object.assign(
          {{}}, res.locals.settings, {{ site_url: _proto + '://' + _host }}
        );
      }}
    }} catch (e) {{ /* never block render on URL derivation */ }}
  }}
  // Always set nav regardless of DB state so layout never throws
  // "nav_items is not defined" when the DB is cold on first boot.
  res.locals.nav_items = _NAV_ITEMS;
  res.locals.admin_nav_items = _ADMIN_NAV_ITEMS;
  next();
}}

function invalidateSettingsCache() {{ _cachedSettings = null; }}

module.exports = {{ siteSettingsMiddleware, loadSettings, invalidateSettingsCache }};
'''
    return ("middleware/settings.js", contents)


# A navigation link target supplied by the spec: one leading slash, then
# path segments, an optional query string and an optional fragment, all made
# of URL-unreserved characters (plus "=", "&", "%" and "+" in the query).
# Nothing that matches can leave the site: there is no second slash or
# backslash after the first character, no colon, no whitespace and no
# control character.
_NAV_HREF_RE = re.compile(
    r"/(?:[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)*/?)?"
    r"(?:\?[A-Za-z0-9_.~=&%+-]*)?(?:#[A-Za-z0-9_.~-]*)?",
    re.ASCII,
)


def _validate_nav_items_override(raw_items, admin: bool = False) -> list:
    """Filter a list of nav entries (label + href) for safe emission.

    Customer-supplied lists pass through this gate so we can:
    - reject non-dict entries
    - require non-empty label
    - require a link target on this site (`_NAV_HREF_RE`). That refuses
      absolute URLs, `javascript:` and the like, protocol-relative targets
      in every spelling ("//host", "/\\host", a tab between the slashes),
      and parametric segments (e.g. /events/:slug, which would be a dead
      literal link)
    - dedupe by href, preserving first occurrence (same href with
      different labels keeps the first only)

    Used for both public and admin nav (admin pass enforces /admin/
    prefix on hrefs).
    """
    if not isinstance(raw_items, list):
        return []
    validated: list = []
    seen_hrefs = set()
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        raw_href = entry.get("href")
        # The trimmed value is what is checked AND what is emitted.
        href = raw_href.strip() if isinstance(raw_href, str) else ""
        if not label or len(href) > 200 or not _NAV_HREF_RE.fullmatch(href):
            continue
        if admin and not (href == "/admin" or href.startswith("/admin/")):
            # Admin nav entries must point at admin-mounted paths.
            # Use the explicit prefix-or-equals check so a path like
            # /admin-evil-prefix can't slip past startswith("/admin").
            continue
        if href in seen_hrefs:
            continue
        validated.append({"label": label, "href": href})
        seen_hrefs.add(href)
    return validated


def _default_nav_items(rs: ResolvedSpec) -> list:
    """Public nav items, MW v25 pattern. One entry per archetype
    default_page with hand-curated labels, then one entry per
    customer-declared custom page so customers actually
    see their own pages in navigation. Auth-gated pages are excluded.
    Admin link appended conditionally in the template when a user is
    in session.

    v2-spec-nav_items-honor-customer-labels:
    when `spec.nav_items` is a non-empty list, honor it verbatim
    (after path-safety filtering) instead of hardcoding labels.
    Falls back to the archetype-derived list when nav_items is
    absent, empty, or every entry fails validation.
    """
    override = _validate_nav_items_override(rs.spec.nav_items, admin=False)
    if override:
        return override

    items = []
    # Iterate rs.page_layouts keys, not the raw
    # archetype default_pages list. resolve_spec drops archetype pages
    # whose purpose overlaps with a customer-declared page (e.g.
    # /journal is dropped when customer declares /blog). Using the
    # raw archetype list here would re-add the dropped page to nav.
    pages = list(rs.page_layouts.keys())
    # Preserve the archetype's default ordering: emit pages in the
    # order they appear in default_pages, skipping any that have been
    # dropped.
    ordered_pages = [p for p in rs.archetype["default_pages"] if p in pages]
    # When keeper supplied an explicit page
    # list (carried as `_harbor_meta.nav_paths` by harbor_to_compose),
    # restrict archetype default_pages to those whose href appears in
    # that allowlist. Home (`/`) and contact stay. Custom_pages append
    # below is also gated by the same allowlist.
    nav_paths_allow = None
    _hm = rs.spec.harbor_meta if isinstance(rs.spec.harbor_meta, dict) else {}
    _raw_allow = _hm.get("nav_paths") if isinstance(_hm, dict) else None
    if isinstance(_raw_allow, list) and _raw_allow:
        nav_paths_allow = {p for p in _raw_allow if isinstance(p, str) and p.startswith("/")}
        # Always allow home + contact + about even if keeper didn't list them.
        # /about is a core content page (the practitioner's bio); excluding it
        # orphaned a real, populated /about page from the nav when the keeper's
        # requested path differed from the archetype default (keeper asked for
        # "/approach", archetype emits "/about"; a finding of the automated
        # build review, 2026-06-07. That review loop ran outside this
        # repository and is not included in it). The filter below only KEEPS
        # pages that actually exist,
        # so adding /about is a no-op for archetypes that don't emit one.
        nav_paths_allow.add("/")
        nav_paths_allow.add("/contact")
        nav_paths_allow.add("/about")
    if nav_paths_allow is not None:
        ordered_pages = [
            p for p in ordered_pages
            if (("/" if p == "home" else f"/{p}") in nav_paths_allow)
        ]
    labels = {
        "home": "Home",
        "work-with-me": "Work With Me",
        "about": "About",
        "journal": "Journal",
        "blog": "Blog",
        "events": "Events",
        "contact": "Contact",
        "services": "Services",
        "case-studies": "Case Studies",
        "reviews": "Reviews",
    }
    existing_hrefs = set()
    for p in ordered_pages:
        href = "/" if p == "home" else f"/{p}"
        items.append({"label": labels.get(p, _prettify(p)) if p != "home" else "Home", "href": href})
        existing_hrefs.add(href)

    # Append customer custom_pages. Rules:
    #   - skip auth-required pages (admin surfaces shouldn't show to visitors)
    #   - skip paths already in the archetype nav
    #   - include form pages (useful nav: /book, /rsvp, /contact-us)
    #   - include a page whose path a model's public listing answers
    #     (the spec set public_listing_path there): the link is live
    # The custom page's title becomes the nav label; falls back to the
    # prettified path slug.
    if rs.spec.custom_pages:
        # A custom page gets a nav link only when this build serves a
        # public page at its path: the page itself survived
        # _classify_custom_pages, or a page or model listing answers there
        # instead. _sitemap_public_paths is that set. A page that was
        # dropped (not a plain path, parametric, a duplicate, a reserved
        # route) therefore never leaves a link behind, whatever its path
        # string says.
        public_paths = _sitemap_public_paths(rs)
        for cp in rs.spec.custom_pages:
            if not isinstance(cp, dict):
                continue
            if cp.get("auth_required"):
                continue
            path = _text(cp.get("path"))
            if path not in public_paths or path in existing_hrefs:
                continue
            # When the keeper's nav_paths allowlist is set,
            # custom_pages whose path isn't in the allowlist are still
            # routed but not nav-linked (they remain accessible by URL).
            if nav_paths_allow is not None and path not in nav_paths_allow:
                continue
            label = (cp.get("title") or "").strip() or _prettify(_slug_from_path(path))
            items.append({"label": label, "href": path})
            existing_hrefs.add(path)
    return items


def _default_admin_nav_items(rs: ResolvedSpec) -> list:
    """Admin nav items: MW v25 pattern. Shown inside the admin
    dashboard landing AND inside admin-only pages. Links go to
    /admin/<slug> (admin-mounted paths).

    v2-spec-nav_items-honor-customer-labels:
    honor `spec.admin_nav_items` when supplied. Admin-side override
    additionally enforces that every href starts with `/admin/` so
    customer override can't accidentally expose a non-admin path
    inside the admin nav.

    When no `spec.admin_nav_items`
    override is set, restrict the auto-derived nav to publicly-
    visible models via `_publicly_visible_data_models`. The hidden
    models still have CRUD routes mounted at /admin/<slug> and are
    reachable by URL; they're just not nav-surfaced. A customer can
    override via `spec.admin_nav_items` to re-expose any hidden
    model (the override goes through unchanged after the standard
    admin-path safety filter).
    """
    override = _validate_nav_items_override(rs.spec.admin_nav_items, admin=True)
    if override:
        return override

    items = [{"label": "Dashboard", "href": "/admin"}]
    for m in _publicly_visible_data_models(rs):
        slug = _slug_for(m)
        label = _prettify(slug)
        items.append({"label": label, "href": f"/admin/{slug}"})
    items.append({"label": "Settings", "href": "/admin/settings"})
    return items


def emit_routes_auth_js(rs: ResolvedSpec) -> tuple:
    # The login link is `/magic?token=<raw token>`. The first one is seeded
    # by the worker (`_generate_magic_link_token` in queue/worker_pg.py): it
    # stores sha256(raw token) in `magic_link_tokens` through seed.sql, so
    # the routes below MUST hash the incoming token the same way.
    #
    # Contract with the deploy probe (cli/deploy.py) and with link-preview
    # crawlers: GET and HEAD /magic never consume a token (GET renders a
    # confirmation page); only POST /magic/consume does. Because a GET
    # leaves the token usable, the raw token must stay out of the logs:
    # server.js writes its access log without query strings, and the only
    # place the app prints a login link is the no-mail-key branch of
    # POST /request-magic-link below.
    contents = '''const express = require('express');
const bcrypt = require('bcrypt');
const crypto = require('crypto');
const router = express.Router();
const db = require('../db');
const { validateCsrfToken } = require('../middleware/auth');
const { authLimiter, magicLinkRequestLimiter } = require('../middleware/rate-limit');

// Session fixation defense: issue a NEW session id at the moment of login,
// so an id an attacker planted in (or read from) the browser beforehand is
// worthless afterwards. express-session's regenerate() drops the old data,
// so the identity is written onto the fresh session.
function establishSession(req, user, done) {
  req.session.regenerate((err) => {
    if (err) return done(err);
    req.session.userId = user.id;
    req.session.email = user.email;
    req.session.role = user.role;
    // Only a session started by a login link carries this stamp. It is what
    // /set-password accepts in place of the current password, for a short
    // time (see passwordChangeRule below).
    if (user.viaLoginLink) req.session.loginLinkAt = Date.now();
    req.session.save(done);
  });
}

// Where a fresh sign-in lands. /admin is the admin's dashboard and turns
// every other role away, so an account with another role (a row the operator
// added to `users`; the app has no sign-up) lands on the home page.
function landingFor(role) {
  return role === 'admin' ? '/admin' : '/';
}

// Setting a password WITHOUT typing the current one is allowed in exactly two
// cases:
//   1. the account has no password yet (the first sign-in is by login link,
//      and the seeded admin row has none), or
//   2. this session was started by a login link less than
//      PASSWORD_SETUP_WINDOW_MS ago. That is the forgotten-password path: the
//      link proves control of the mailbox, the way a reset email would.
// Every other session (a password login, a login link from earlier, a
// 30-day cookie on a shared machine) must supply the current password, the
// same rule POST /admin/change-password applies. Without this, whoever held
// a signed-in browser for a moment could replace the password and keep the
// account.
const PASSWORD_SETUP_WINDOW_MS = 15 * 60 * 1000;

async function passwordChangeRule(req) {
  const { rows } = await db.query('SELECT password FROM users WHERE id = $1', [req.session.userId]);
  if (rows.length === 0) return null;
  const hash = rows[0].password || null;
  const linkAt = Number(req.session.loginLinkAt) || 0;
  const freshLink = linkAt > 0 && (Date.now() - linkAt) < PASSWORD_SETUP_WINDOW_MS;
  return { hash, needsCurrent: !!hash && !freshLink };
}

// One message for every failed password login. Distinct messages for
// "no such account", "account has no password" and "wrong password" would
// let anyone test which email addresses are registered.
const _LOGIN_FAILED = 'Invalid email or password. If you sign in with a login link, request a new one below.';
// Compared against when the account does not exist or has no password, so
// the failure paths cost the same bcrypt work as a real mismatch. Generated
// at boot from random bytes; it is not a credential.
const _TIMING_EQUALIZER_HASH = bcrypt.hashSync(crypto.randomBytes(24).toString('hex'), 10);

// Friendly error messages for ?error=... query params set by the /magic
// handler (and, in the future, other auth flows). Mapped server-side so
// users see a human message on the login page instead of a silently-blank
// form when their magic link is expired / used / invalid.
const _LOGIN_ERROR_MESSAGES = {
  'invalid-token': 'That login link is not valid. Request a new one below or try logging in with your password.',
  'token-expired': 'That login link has expired. Request a new one below.',
  'token-already-used': 'That login link has already been used. Request a new one below.',
};
router.get('/login', (req, res) => {
  const errKey = (req.query && req.query.error) ? String(req.query.error) : '';
  const errorMsg = _LOGIN_ERROR_MESSAGES[errKey] || null;
  res.render('login', { title: 'Login', layout: 'layout-public', error: errorMsg, magic_link_available: false });
});

router.post('/login', authLimiter, validateCsrfToken, async (req, res) => {
  // typeof guards: with the extended urlencoded parser a client can send
  // email[]=x and turn these into arrays or objects.
  const email = (req.body && typeof req.body.email === 'string') ? req.body.email : '';
  const password = (req.body && typeof req.body.password === 'string') ? req.body.password : '';
  if (!email || !password) {
    return res.render('login', { title: 'Login', layout: 'layout-public', error: 'Email and password required.', magic_link_available: false });
  }
  try {
    const { rows } = await db.query('SELECT id, email, password, role FROM users WHERE email = $1', [email]);
    const user = rows.length > 0 ? rows[0] : null;
    const hasPassword = !!(user && user.password);
    const matches = await bcrypt.compare(password, hasPassword ? user.password : _TIMING_EQUALIZER_HASH);
    if (!hasPassword || !matches) {
      return res.render('login', { title: 'Login', layout: 'layout-public', error: _LOGIN_FAILED, magic_link_available: false });
    }
    establishSession(req, user, (err) => {
      if (err) {
        console.error('login session error:', err);
        return res.status(500).render('error-500', { title: 'Error' });
      }
      res.redirect(landingFor(user.role));
    });
  } catch (e) {
    console.error(e);
    res.status(500).render('error-500', { title: 'Error' });
  }
});

// Login by link. A token is issued by the worker (the seeded admin's first
// link) or by POST /request-magic-link below, and stored as its SHA-256;
// the hash here MUST match _generate_magic_link_token in
// queue/worker_pg.py.
//
// HEAD /magic returns 200 when the token is valid + unused + unexpired,
// 410 otherwise. It does NOT consume the token (no UPDATE used_at), so a
// link can be checked without burning it:
//   curl -I "https://app.example.com/magic?token=<raw>" -> 200 = good
router.head('/magic', async (req, res) => {
  const rawToken = (req.query && req.query.token) ? String(req.query.token) : '';
  if (!rawToken || rawToken.length < 16) return res.status(410).end();
  const tokenHash = crypto.createHash('sha256').update(rawToken).digest('hex');
  try {
    const { rows } = await db.query(
      `SELECT t.id, t.expires_at, t.used_at FROM magic_link_tokens t WHERE t.token_hash = $1`,
      [tokenHash]
    );
    if (rows.length === 0) return res.status(410).end();
    const row = rows[0];
    if (row.used_at) return res.status(410).end();
    if (row.expires_at && new Date(row.expires_at) < new Date()) return res.status(410).end();
    return res.status(200).end();
  } catch (e) {
    console.error('magic-link HEAD error:', e);
    return res.status(500).end();
  }
});

// GET /magic is non-consuming for ALL requests. It validates the token
// (SELECT only) and renders a confirmation page with a single "Sign in"
// button that POSTs to /magic/consume. The consume route does the SELECT +
// UPDATE used_at + session set + redirect.
//
// Why two-step: link-preview crawlers (iMessage on macOS, Slack, Discord,
// WhatsApp, Twitter/X, Facebook, LinkedIn, Telegram, Skype, and unknown
// future clients) fetch URLs via GET to build rich previews. A
// single-step GET that consumes the token burns it on the sender's
// device before the recipient ever clicks. The prior fix (UA blocklist)
// was necessarily incomplete; an always-non-consuming GET closes the
// hole for every client, known or unknown. This matches the behavior
// of Slack, Notion, Substack, and Linear.
router.get('/magic', async (req, res) => {
  const rawToken = (req.query && req.query.token) ? String(req.query.token) : '';
  if (!rawToken || rawToken.length < 16) {
    return res.redirect('/login?error=invalid-token');
  }
  const tokenHash = crypto.createHash('sha256').update(rawToken).digest('hex');
  try {
    const { rows } = await db.query(
      `SELECT t.id, t.expires_at, t.used_at
         FROM magic_link_tokens t
        WHERE t.token_hash = $1`,
      [tokenHash]
    );
    if (rows.length === 0) {
      return res.redirect('/login?error=invalid-token');
    }
    const row = rows[0];
    if (row.used_at) {
      return res.redirect('/login?error=token-already-used');
    }
    if (row.expires_at && new Date(row.expires_at) < new Date()) {
      return res.redirect('/login?error=token-expired');
    }
    // Token is valid: render the confirm view. The token is NOT consumed
    // here, only when the user clicks "Sign in" and POSTs /magic/consume.
    res.set('Cache-Control', 'no-store');
    return res.render('magic-link-confirm', {
      title: 'Confirm sign-in',
      layout: 'layout-public',
      token: rawToken,
    });
  } catch (e) {
    console.error('magic-link GET error:', e);
    res.status(500).render('error-500', { title: 'Error' });
  }
});

// POST /magic/consume is the actual consume + session-set step. CSRF-gated
// (the token + session csrfToken were stamped onto the confirm page that
// the user just submitted). Re-validates the magic-link token, sets
// used_at, and redirects to /admin or /set-password.
router.post('/magic/consume', authLimiter, validateCsrfToken, async (req, res) => {
  const rawToken = (req.body && typeof req.body.token === 'string') ? req.body.token : '';
  if (!rawToken || rawToken.length < 16) {
    return res.redirect('/login?error=invalid-token');
  }
  const tokenHash = crypto.createHash('sha256').update(rawToken).digest('hex');
  try {
    const { rows } = await db.query(
      `SELECT t.id, t.user_id, t.expires_at, t.used_at,
              u.email, u.role, u.password_set
         FROM magic_link_tokens t
         JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = $1`,
      [tokenHash]
    );
    if (rows.length === 0) {
      return res.redirect('/login?error=invalid-token');
    }
    const row = rows[0];
    if (row.used_at) {
      return res.redirect('/login?error=token-already-used');
    }
    if (row.expires_at && new Date(row.expires_at) < new Date()) {
      return res.redirect('/login?error=token-expired');
    }
    // Mark token consumed (single-use semantics). The WHERE repeats the
    // unused + unexpired conditions and the result is checked, so two
    // concurrent POSTs of the same token cannot both sign in: the row
    // lock serializes them and the second UPDATE matches nothing.
    const claimed = await db.query(
      'UPDATE magic_link_tokens SET used_at = NOW() WHERE id = $1 AND used_at IS NULL AND expires_at > NOW() RETURNING id',
      [row.id]
    );
    if (claimed.rowCount !== 1) {
      return res.redirect('/login?error=token-already-used');
    }
    const redirectTo = row.password_set ? landingFor(row.role) : '/set-password';
    establishSession(req, { id: row.user_id, email: row.email, role: row.role, viaLoginLink: true }, (err) => {
      if (err) {
        console.error('magic-link session error:', err);
        return res.status(500).render('error-500', { title: 'Error' });
      }
      res.redirect(redirectTo);
    });
  } catch (e) {
    console.error('magic-link consume error:', e);
    res.status(500).render('error-500', { title: 'Error' });
  }
});

router.post('/logout', validateCsrfToken, (req, res) => {
  req.session.destroy(() => res.redirect('/login'));
});

// Password set surface. Reached after /magic/consume redirects when
// password_set=FALSE on the user row, and directly by a signed-in user.
// Whether the form asks for the current password is decided by
// passwordChangeRule above; the view shows the extra field when it does.
router.get('/set-password', async (req, res) => {
  if (!req.session || !req.session.userId) {
    return res.redirect('/login');
  }
  try {
    const rule = await passwordChangeRule(req);
    if (!rule) return res.redirect('/login');
    res.render('set-password', { title: 'Set password', layout: 'layout-public', error: null, needs_current: rule.needsCurrent });
  } catch (e) {
    console.error('set-password error:', e);
    res.status(500).render('error-500', { title: 'Error' });
  }
});

// authLimiter: when the current password is required this route compares a
// submitted password with the stored hash, so it is limited like /login.
router.post('/set-password', authLimiter, validateCsrfToken, async (req, res) => {
  if (!req.session || !req.session.userId) {
    return res.redirect('/login');
  }
  const body = req.body || {};
  const current_password = typeof body.current_password === 'string' ? body.current_password : '';
  const new_password = typeof body.new_password === 'string' ? body.new_password : '';
  const confirm_password = typeof body.confirm_password === 'string' ? body.confirm_password : '';
  try {
    const rule = await passwordChangeRule(req);
    if (!rule) return res.redirect('/login');
    const fail = (message) => res.render('set-password', {
      title: 'Set password', layout: 'layout-public', error: message, needs_current: rule.needsCurrent
    });
    if (rule.needsCurrent) {
      // An empty or oversized value is never handed to bcrypt.
      const plausible = current_password.length > 0 && Buffer.byteLength(current_password, 'utf8') <= 72;
      if (!plausible || !(await bcrypt.compare(current_password, rule.hash))) {
        return fail('Current password is incorrect.');
      }
    }
    // bcrypt only reads the first 72 bytes; refuse longer input rather than
    // silently accepting a password whose tail does not count.
    if (new_password.length < 8 || Buffer.byteLength(new_password, 'utf8') > 72) {
      return fail('Password must be 8 to 72 characters.');
    }
    if (new_password !== confirm_password) {
      return fail('Passwords do not match.');
    }
    const hash = await bcrypt.hash(new_password, 10);
    await db.query('UPDATE users SET password = $1, password_set = TRUE WHERE id = $2', [hash, req.session.userId]);
    // The login-link allowance is spent: the next change needs the password.
    delete req.session.loginLinkAt;
    req.session.save(() => res.redirect(landingFor(req.session.role)));
  } catch (e) {
    console.error('set-password error:', e);
    res.render('set-password', { title: 'Set password', layout: 'layout-public', error: 'Could not save password. Please try again.', needs_current: true });
  }
});

// Self-service magic-link request surface. POST always renders the same
// generic confirmation regardless of whether the email is registered, to
// avoid leaking which addresses exist (anti-enumeration). When an email
// is registered, a single-use token row is inserted into magic_link_tokens
// for the matching user and the link is emailed through Resend. When no
// RESEND_API_KEY is configured nothing can be emailed, so the link is
// written to the server log instead (the only way an operator can then
// recover access). With a mail key the app does not log the token.
router.get('/request-magic-link', (req, res) => {
  res.render('request-magic-link', { title: 'Request a login link', layout: 'layout-public', error: null });
});

router.post('/request-magic-link', magicLinkRequestLimiter, validateCsrfToken, async (req, res) => {
  const email = (req.body && typeof req.body.email === 'string') ? req.body.email.trim().toLowerCase() : '';
  if (!email) {
    return res.render('request-magic-link', { title: 'Request a login link', layout: 'layout-public', error: 'Please enter your email.' });
  }
  try {
    const { rows } = await db.query('SELECT id, email FROM users WHERE LOWER(email) = $1', [email]);
    if (rows.length > 0) {
      const userId = rows[0].id;
      const realEmail = rows[0].email;
      const rawToken = crypto.randomBytes(32).toString('hex');
      const tokenHash = crypto.createHash('sha256').update(rawToken).digest('hex');
      const expiresAt = new Date(Date.now() + 24 * 60 * 60 * 1000);
      await db.query(
        'INSERT INTO magic_link_tokens (user_id, token_hash, expires_at) VALUES ($1, $2, $3)',
        [userId, tokenHash, expiresAt]
      );
      // Absolute URL for the emailed link. req.protocol honors
      // X-Forwarded-Proto from the one trusted proxy hop (`trust proxy`
      // in server.js); the host is the Host header the edge routed on.
      // X-Forwarded-Host is deliberately NOT read: it is a plain request
      // header any client can set, and trusting it would let an attacker
      // request a link for the admin's address that points at a host the
      // attacker controls, then collect the token when the admin clicks.
      const host = String(req.get('host') || '');
      const magicUrl = req.protocol + '://' + host + '/magic?token=' + rawToken;
      // A login token is a credential. The else branch below is the ONE
      // place this app writes one to its log, and it runs only when no
      // RESEND_API_KEY is set: nothing can be emailed then, and the log is
      // the owner's only way in. With a mail key the token goes to the
      // mail provider and nowhere else; if that send fails the link is
      // lost (the owner requests another one), it is not logged instead.
      if (process.env.RESEND_API_KEY) {
        console.log('[magic-link] issued for user', userId);
      } else {
        console.log('[magic-link] issued for user', userId, '(no RESEND_API_KEY, link logged for operator recovery): /magic?token=' + rawToken);
      }
      try {
        const { sendMagicLinkEmail } = require('../services/notify');
        sendMagicLinkEmail({ to: realEmail, magicUrl }).catch(err => {
          console.error('[magic-link-email] send failed:', err && err.message);
        });
      } catch (loadErr) {
        console.error('[magic-link-email] could not load services/notify:', loadErr && loadErr.message);
      }
    }
  } catch (e) {
    console.error('request-magic-link error:', e);
  }
  res.redirect('/magic-link-sent');
});

router.get('/magic-link-sent', (req, res) => {
  res.render('magic-link-sent', { title: 'Check your email', layout: 'layout-public' });
});

module.exports = router;
'''
    return ("routes/auth.js", contents)


def emit_routes_legal_js(rs: ResolvedSpec) -> tuple:
    """Emit `routes/legal.js` with GET /privacy + GET /terms handlers.

    Every generated app's nav
    + footer link to /privacy + /terms, so both need handlers. The
    reserved-path set in _custom_pages_filter lists
    them as taken (so customers can't override), and this emitter is what
    serves them. The view templates are generic boilerplate the keeper can
    edit later via /admin/settings (future work) or by direct file edit.

    Both pages render through layout-public.ejs so they pick up the
    customer's brand + palette automatically. The customer's site_name
    is interpolated server-side via res.locals.settings.site_name.
    """
    contents = '''const express = require('express');
const router = express.Router();

// Static legal pages. Generic boilerplate; the keeper can update copy
// via /admin/settings (placeholder feature) or direct view edit. Both
// pages render through layout-public for nav/footer consistency.
router.get('/privacy', (req, res) => {
  const siteName = (res.locals.settings && res.locals.settings.site_name) || 'this site';
  res.render('privacy', {
    title: siteName + ' | Privacy',
    layout: 'layout-public',
    page_title: 'Privacy',
    canonicalUrl: '/privacy',
    site_name: siteName
  });
});

router.get('/terms', (req, res) => {
  const siteName = (res.locals.settings && res.locals.settings.site_name) || 'this site';
  res.render('terms', {
    title: siteName + ' | Terms',
    layout: 'layout-public',
    page_title: 'Terms',
    canonicalUrl: '/terms',
    site_name: siteName
  });
});

module.exports = router;
'''
    return ("routes/legal.js", contents)


# /robots.txt + /sitemap.xml.
# Mounted ahead of the pages router (the static public/robots.txt that prior
# builds shipped is no longer emitted; this route owns the URL) so search
# crawlers get an explicit allow-list and a deterministic sitemap built from
# the customer's auth_required=false routes. <lastmod> is the build time so
# every redeploy advertises "this site has new content," nudging the
# crawl-frequency without hand-tuning.
def emit_routes_seo_js(rs: ResolvedSpec) -> tuple:
    """Emit `routes/seo.js` with GET /robots.txt + GET /sitemap.xml.

    /robots.txt: allow-list every public URL; disallow /admin /api /webhooks
    so the operator-only surfaces don't show up in search. Appends a
    `Sitemap:` line pointing to the live origin so crawlers discover
    /sitemap.xml without re-parsing the page tree.

    /sitemap.xml: lists every auth_required=false route derived at build
    time from page_layouts + non-auth custom_pages + /privacy + /terms +
    each data_model's public_listing_path (when set). <lastmod> = build
    time so the file gives crawlers an honest freshness signal on every
    redeploy. The origin is derived per-request from settings.site_url
    (admin-configured) with a host-header fallback so previews work
    before the admin saves a site_url.
    """
    public_paths = _sitemap_public_paths(rs)
    build_time = _build_time_iso(rs)
    # Sort + dedupe; '/' first, then the rest alphabetically so the
    # emitted file is byte-deterministic across rebuilds and tests can
    # reason about ordering.
    ordered_paths = ["/"] if "/" in public_paths else []
    ordered_paths += sorted(p for p in public_paths if p != "/")
    paths_js_literal = json.dumps(ordered_paths)
    build_time_js = _js_escape(build_time)
    contents = f'''const express = require('express');
const router = express.Router();

// Build time captured at compose time so /sitemap.xml advertises a
// deterministic freshness signal per redeploy rather than the request
// time (which would change with every crawler hit).
const BUILD_TIME = '{build_time_js}';

// Public route allow-list derived from the spec at build time.
// auth_required=true custom_pages and admin-only routes are intentionally
// excluded so the sitemap reflects what crawlers should actually index.
const PUBLIC_ROUTES = {paths_js_literal};

function siteOrigin(req, res) {{
  const cfg = (res.locals && res.locals.settings && res.locals.settings.site_url) || '';
  if (cfg) return String(cfg).replace(/\\/+$/, '');
  const proto = (req.secure || req.get('x-forwarded-proto') === 'https') ? 'https' : 'http';
  return proto + '://' + (req.get('host') || 'localhost');
}}

router.get('/robots.txt', (req, res) => {{
  const origin = siteOrigin(req, res);
  const body = [
    'User-agent: *',
    'Allow: /',
    'Disallow: /admin',
    'Disallow: /api',
    'Disallow: /webhooks',
    '',
    'Sitemap: ' + origin + '/sitemap.xml',
    ''
  ].join('\\n');
  res.set('Content-Type', 'text/plain; charset=utf-8');
  res.send(body);
}});

// site_url is admin-editable and the fallback origin is the Host header,
// so both are escaped before they are written into XML.
function xmlEscape(s) {{
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}}

router.get('/sitemap.xml', (req, res) => {{
  const origin = siteOrigin(req, res);
  const urls = PUBLIC_ROUTES.map(function(p) {{
    const loc = xmlEscape(origin + p);
    return '  <url><loc>' + loc + '</loc><lastmod>' + BUILD_TIME + '</lastmod></url>';
  }}).join('\\n');
  const xml = '<?xml version="1.0" encoding="UTF-8"?>\\n' +
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\\n' +
    urls + (urls ? '\\n' : '') +
    '</urlset>\\n';
  res.set('Content-Type', 'application/xml; charset=utf-8');
  res.send(xml);
}});

module.exports = router;
'''
    return ("routes/seo.js", contents)


def _build_moment():
    """The instant a build is stamped with (a timezone-aware UTC datetime):
    SOURCE_DATE_EPOCH when it is set (reproducible builds), otherwise the
    wall clock."""
    from datetime import datetime, timezone
    _sde = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if _sde.isdigit():
        try:
            return datetime.fromtimestamp(int(_sde), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _sample_date(days_ahead: int, rs: "ResolvedSpec" = None) -> str:
    """YYYY-MM-DD for a placeholder "upcoming" date: `days_ahead` days after
    the day of this build (or of SOURCE_DATE_EPOCH). Sample events are dated
    from the build, never with a literal calendar date, so a site composed
    next year still shows them as upcoming."""
    from datetime import date, timedelta
    return (date.fromisoformat(_build_time_iso(rs)[:10]) + timedelta(days=days_ahead)).isoformat()


def _build_time_iso(rs: "ResolvedSpec" = None) -> str:
    """ISO 8601 timestamp (seconds precision, UTC) captured once per build.
    Used as the deterministic <lastmod> value in /sitemap.xml so every
    redeploy advertises a single uniform freshness signal across all
    listed URLs.

    Memoized on the ResolvedSpec: `datetime.now()` is wall-clock, so two
    `compose_files(rs)` calls that straddle a one-second boundary would
    otherwise emit different BUILD_TIME values and break the engine's
    same-input/same-output guarantee (test_compose_files_idempotent
    failed intermittently in CI for exactly this reason). Caching on rs
    makes both calls reuse one value while a genuinely fresh build (fresh
    rs) still gets a fresh stamp.

    Reproducible builds: when SOURCE_DATE_EPOCH (the reproducible-builds.org
    convention: integer seconds since the Unix epoch) is set, it is used
    instead of the wall clock. This is the one wall-clock value in the text
    output, so pinning it makes two builds of the same spec byte-identical
    there (guarded by test_two_processes_emit_identical_text_files)."""
    if rs is not None:
        cached = getattr(rs, "_build_time_iso_cache", None)
        if cached is not None:
            return cached
    stamp = _build_moment().strftime("%Y-%m-%dT%H:%M:%SZ")
    if rs is not None:
        try:
            rs._build_time_iso_cache = stamp
        except (AttributeError, TypeError):
            pass
    return stamp


def _sitemap_public_paths(rs: ResolvedSpec) -> set:
    """Compute the set of auth_required=false routes the generated app
    serves. Source of truth for /sitemap.xml emission.

    Includes:
      - archetype default page paths (page_layouts keys)
      - custom_pages whose auth_required is falsy
      - /privacy + /terms (always emitted by emit_routes_legal_js)
      - each data_model's public_listing_path when explicitly set in the spec

    Excludes:
      - any custom_page with auth_required=true (admin pages)
      - resource detail routes (/<slug>/:id), the listing index is the
        crawlable surface; detail rows are spec-time unknowable
    """
    paths: set = set()
    # Archetype default pages (always public).
    for page_id in rs.page_layouts.keys():
        paths.add("/" if page_id == "home" else f"/{page_id}")
    # /privacy + /terms: emitted unconditionally by emit_routes_legal_js.
    paths.add("/privacy")
    paths.add("/terms")
    # Custom pages that don't require auth.
    for cp in _custom_pages_filter(rs):
        if cp.get("auth_required"):
            continue
        path = (cp.get("path") or "").strip()
        if path:
            paths.add(path)
    # Data-model listing pages explicitly published via public_listing_path.
    # Default mount (/<slug>) is only crawler-relevant when the spec
    # opted in by setting public_listing_path; otherwise the model is
    # admin-only-managed and shouldn't show up in search.
    for m in rs.data_models:
        if not isinstance(m, dict):
            continue
        plp = m.get("public_listing_path")
        if plp:
            paths.add(plp)
    return paths


def _emit_legal_views(rs: ResolvedSpec) -> List[Tuple[str, str]]:
    """Privacy + Terms boilerplate pages. Plain English; generic enough
    to apply to any small-business site; the keeper can edit later. The
    `<%= site_name %>` interpolation comes from the route handler.
    """
    privacy = '''<section class="cas-type-section" style="padding:64px 0;">
  <div class="max-site" style="max-width: 720px;">
    <h1 class="cas-type-h1" style="margin: 0 0 24px;">Privacy</h1>
    <p style="font-size: 1rem; line-height: 1.7; opacity: 0.85;">
      <%= site_name %> respects your privacy. This page explains what we collect,
      why, and how we use it.
    </p>

    <h2 class="cas-type-h2" style="margin: 40px 0 12px; font-size: 1.5rem;">What we collect</h2>
    <p style="line-height: 1.7; opacity: 0.85;">
      When you fill out a form on this site (a contact form, a booking request,
      a newsletter signup), we collect the information you provide: usually a
      name, an email address, and the message you wrote. We use this to reply
      to you and to keep your records.
    </p>

    <h2 class="cas-type-h2" style="margin: 40px 0 12px; font-size: 1.5rem;">How we use it</h2>
    <p style="line-height: 1.7; opacity: 0.85;">
      We use the information you give us to respond to your request, to confirm
      a booking, or to send you the newsletter you signed up for. We don't sell
      your data. We don't share it with third parties beyond what is required
      to run the site (email delivery, hosting).
    </p>

    <h2 class="cas-type-h2" style="margin: 40px 0 12px; font-size: 1.5rem;">Cookies</h2>
    <p style="line-height: 1.7; opacity: 0.85;">
      The site uses session cookies to keep you logged in to the admin area.
      No tracking cookies, no third-party advertising pixels.
    </p>

    <h2 class="cas-type-h2" style="margin: 40px 0 12px; font-size: 1.5rem;">Reach out</h2>
    <p style="line-height: 1.7; opacity: 0.85;">
      If you have questions about your data, or you want a copy or removal,
      use the <a href="/contact">contact page</a> and we will respond.
    </p>

    <p style="margin-top: 56px; font-size: 0.875rem; opacity: 0.6;">
      Last updated: when this site went live. The owner can revise this page
      from the admin area.
    </p>
  </div>
</section>
'''

    terms = '''<section class="cas-type-section" style="padding:64px 0;">
  <div class="max-site" style="max-width: 720px;">
    <h1 class="cas-type-h1" style="margin: 0 0 24px;">Terms</h1>
    <p style="font-size: 1rem; line-height: 1.7; opacity: 0.85;">
      By using <%= site_name %> you agree to these terms. They are short on
      purpose; the spirit is more important than the letter.
    </p>

    <h2 class="cas-type-h2" style="margin: 40px 0 12px; font-size: 1.5rem;">Use the site fairly</h2>
    <p style="line-height: 1.7; opacity: 0.85;">
      Don't use the site to harass, deceive, or harm others. Don't try to
      break or abuse the systems running it. If you submit a form, give us
      real contact information so we can respond.
    </p>

    <h2 class="cas-type-h2" style="margin: 40px 0 12px; font-size: 1.5rem;">Bookings, payments, and refunds</h2>
    <p style="line-height: 1.7; opacity: 0.85;">
      If this site collects bookings or payments, the booking is between you
      and the practitioner whose work this site represents. Cancellations,
      refunds, and rescheduling follow the practitioner's stated policy. If
      that policy is not on the site yet, ask through the
      <a href="/contact">contact page</a> before booking.
    </p>

    <h2 class="cas-type-h2" style="margin: 40px 0 12px; font-size: 1.5rem;">Content and ownership</h2>
    <p style="line-height: 1.7; opacity: 0.85;">
      The text, images, and design on this site belong to the practitioner.
      Don't copy substantial portions for republishing. Brief quotation with
      attribution is fine.
    </p>

    <h2 class="cas-type-h2" style="margin: 40px 0 12px; font-size: 1.5rem;">Limit of liability</h2>
    <p style="line-height: 1.7; opacity: 0.85;">
      The site is provided as-is. The practitioner is not liable for indirect
      losses arising from the use of the site or from the work the
      practitioner offers, beyond what is required by law in the jurisdiction
      where the practitioner operates.
    </p>

    <h2 class="cas-type-h2" style="margin: 40px 0 12px; font-size: 1.5rem;">Changes</h2>
    <p style="line-height: 1.7; opacity: 0.85;">
      These terms may be updated. Continued use of the site after changes
      means you accept the updated terms.
    </p>

    <p style="margin-top: 56px; font-size: 0.875rem; opacity: 0.6;">
      Last updated: when this site went live. The owner can revise this page
      from the admin area.
    </p>
  </div>
</section>
'''
    return [("views/privacy.ejs", privacy), ("views/terms.ejs", terms)]


def emit_services_health_js(rs: ResolvedSpec) -> tuple:
    '''services/health.js: does this site's own home page render?

    The deploy that announces a site is judged by GET /health. A check of
    the database alone passes for a site whose home page throws on every
    request (page data the view cannot handle, a view that does not
    compile), so /health also asks this module.

    The check is one GET of "/" over loopback, made when the listener comes
    up. It goes through the real middleware, route and view. A status below
    500 passes and is remembered for the life of the process, so a probe
    costs one cached flag, not a render. A failure (a 5xx, or no answer
    within `timeoutMs`) is not remembered for good: a later probe runs the
    check again, at most once every `retryAfterMs`.

    Not covered: any page but the home page, any POST, what a signed-in
    user sees, and a home page that breaks after it has passed once (an
    admin's later edit to a row or a setting).

    The module needs only node's http, so it can be loaded on its own.
    '''
    contents = '''const http = require('http');

// One GET of this site's home page over loopback. Resolves to the HTTP
// status, or to 0 when no answer arrived within timeoutMs. The body is
// discarded as it arrives.
function requestHome(port, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    const finish = (status) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(status);
    };
    const req = http.get(
      { host: '127.0.0.1', port: port, path: '/', headers: { 'user-agent': 'site-self-check' } },
      (res) => {
        res.resume();
        finish(res.statusCode || 0);
      }
    );
    req.on('error', () => finish(0));
    timer = setTimeout(() => { req.destroy(); finish(0); }, timeoutMs);
  });
}

// timeoutMs: how long one check waits for the page (a /health probe waits
// at most this long on top of the database query).
// retryAfterMs: the shortest gap between two checks after a failure.
function createHomeCheck(options) {
  const opts = options || {};
  const timeoutMs = opts.timeoutMs === undefined ? 3000 : opts.timeoutMs;
  const retryAfterMs = opts.retryAfterMs === undefined ? 10000 : opts.retryAfterMs;
  let port = null;
  let passed = false;
  let lastAt = 0;
  let inFlight = null;

  function run() {
    if (!inFlight) {
      inFlight = requestHome(port, timeoutMs).then((status) => {
        inFlight = null;
        lastAt = Date.now();
        passed = status >= 200 && status < 500;
        if (!passed) {
          console.error('[health] home page check failed: GET / answered ' + (status || 'nothing in time'));
        }
        return passed;
      });
    }
    return inFlight;
  }

  return {
    // Call once the listener is bound; runs the first check.
    start(listeningPort) {
      port = listeningPort;
      return run();
    },
    // true once a check has passed. Before that: the outcome of the check
    // in flight, or of a new one when the last failure is old enough.
    async ok() {
      if (passed) return true;
      if (port === null) return false;
      if (inFlight) return inFlight;
      if (Date.now() - lastAt < retryAfterMs) return false;
      return run();
    },
  };
}

module.exports = { createHomeCheck, requestHome };
'''
    return ("services/health.js", contents)


def emit_services_notify_js(rs: ResolvedSpec) -> tuple:
    """Fire-and-forget admin notification helper for
    customer-built apps. Used by the public form POST handlers.

    Policy:
      - Requires RESEND_API_KEY + ADMIN_EMAIL env vars. Missing either
        → log-only (console.info), no crash.
      - FROM address defaults to `noreply@${RESEND_FROM_DOMAIN}` or the
        Resend-default `onboarding@resend.dev` for local/sandbox apps.
      - Never throws; callers invoke as fire-and-forget:
        `sendAdminNotification(...).catch(err => console.error(...))`.
      - Body is plain-text + light HTML (no templating) so the customer's
        Resend domain does not need any verification beyond the default.
    """
    contents = '''// Auto-generated by compose/build.py:emit_services_notify_js
// Fire-and-forget admin notifier used by public form POST handlers.
// Uses Node's built-in fetch (Node 18+). Never throws.

const RESEND_API_URL = 'https://api.resend.com/emails';

function _escapeHtml(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Send an admin notification when a new public submission arrives.
 * @param {object} opts
 * @param {string} opts.subject - email subject line
 * @param {string} opts.summary - one-line plain-text summary
 * @param {object} [opts.fields] - submission field values to include
 * @param {string} [opts.manageUrl] - optional /admin link to the new row
 */
async function sendAdminNotification(opts) {
  const subject = (opts && opts.subject) || 'New submission';
  const summary = (opts && opts.summary) || '';
  const fields = (opts && opts.fields) || {};
  const manageUrl = (opts && opts.manageUrl) || '';

  const apiKey = process.env.RESEND_API_KEY;
  const adminEmail = process.env.ADMIN_EMAIL;
  const siteName = process.env.SITE_NAME || 'Your site';
  const fromDomain = process.env.RESEND_FROM_DOMAIN;
  const fromAddress = fromDomain
    ? ('noreply@' + fromDomain)
    : 'onboarding@resend.dev';

  // Log-only when not configured. Keeps the feature opt-in without
  // blocking the form submission or leaking stack traces into logs.
  if (!apiKey || !adminEmail) {
    console.info('[notify] admin-notify skipped (missing RESEND_API_KEY or ADMIN_EMAIL):', subject);
    return { skipped: true };
  }

  // Render a minimal HTML body: summary line + <dl> of fields + optional link.
  const rows = Object.keys(fields || {})
    .filter(k => fields[k] !== undefined && fields[k] !== '' && fields[k] !== null)
    .map(k => (
      '<tr><td style="padding:4px 8px 4px 0;color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;vertical-align:top;">' +
      _escapeHtml(k) +
      '</td><td style="padding:4px 0;color:#18181b;font-size:14px;">' +
      _escapeHtml(String(fields[k])) +
      '</td></tr>'
    ))
    .join('\\n');

  const linkBlock = manageUrl
    ? ('<p style="margin:18px 0 0 0;"><a href="' + _escapeHtml(manageUrl) +
       '" style="color:#16a34a;">View in admin →</a></p>')
    : '';

  const htmlBody =
    '<div style="font-family:-apple-system,Segoe UI,sans-serif;color:#18181b;">' +
    '<p style="margin:0 0 14px 0;font-size:15px;">' + _escapeHtml(summary) + '</p>' +
    (rows ? '<table style="border-collapse:collapse;margin-top:8px;">' + rows + '</table>' : '') +
    linkBlock +
    '</div>';

  try {
    const res = await fetch(RESEND_API_URL, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + apiKey,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        from: siteName + ' <' + fromAddress + '>',
        to: [adminEmail],
        subject: subject,
        html: htmlBody,
      }),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      console.error('[notify] admin-notify failed:', res.status, text.slice(0, 400));
      return { ok: false, status: res.status };
    }
    return { ok: true };
  } catch (e) {
    console.error('[notify] admin-notify network error:', e && e.message);
    return { ok: false, error: e && e.message };
  }
}

/**
 * Send a magic-link login email TO the user who requested one.
 * Wired by routes/auth.js POST /request-magic-link.
 * @param {object} opts
 * @param {string} opts.to - recipient email address
 * @param {string} opts.magicUrl - absolute URL like https://example.com/magic?token=<raw>
 * @param {string} [opts.siteName] - falls back to env.SITE_NAME or 'Your site'
 */
async function sendMagicLinkEmail(opts) {
  const to = (opts && opts.to) ? String(opts.to).trim() : '';
  const magicUrl = (opts && opts.magicUrl) ? String(opts.magicUrl) : '';
  if (!to || !magicUrl) {
    console.warn('[magic-link-email] skipped: missing to or magicUrl');
    return { skipped: true };
  }

  const apiKey = process.env.RESEND_API_KEY;
  const siteName = (opts && opts.siteName) || process.env.SITE_NAME || 'Your site';
  const fromDomain = process.env.RESEND_FROM_DOMAIN;
  const fromAddress = fromDomain
    ? ('noreply@' + fromDomain)
    : 'onboarding@resend.dev';

  // Log-only when Resend isn't configured. Keeps the dev/staging path
  // working without a key. Token is logged in routes/auth.js for ops
  // recovery; we don't print the URL here to avoid double-noise.
  if (!apiKey) {
    console.info('[magic-link-email] skipped (missing RESEND_API_KEY); user:', to);
    return { skipped: true };
  }

  const safeName = _escapeHtml(siteName);
  const safeUrl = _escapeHtml(magicUrl);
  const htmlBody =
    '<div style="font-family:-apple-system,Segoe UI,sans-serif;color:#18181b;line-height:1.6;">' +
    '<p style="margin:0 0 14px 0;font-size:15px;">Sign in to ' + safeName + ' by clicking the link below.</p>' +
    '<p style="margin:0 0 18px 0;"><a href="' + safeUrl + '" style="display:inline-block;padding:10px 18px;background:#18181b;color:#fff;text-decoration:none;border-radius:6px;">Sign in to ' + safeName + '</a></p>' +
    '<p style="margin:0 0 8px 0;font-size:13px;color:#6b7280;">Or copy this link into your browser:</p>' +
    '<p style="margin:0 0 18px 0;font-size:12px;word-break:break-all;color:#374151;">' + safeUrl + '</p>' +
    '<p style="margin:18px 0 0 0;font-size:12px;color:#6b7280;">This link expires in 24 hours. If you did not request it, you can ignore this email.</p>' +
    '</div>';

  try {
    const res = await fetch(RESEND_API_URL, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + apiKey,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        from: siteName + ' <' + fromAddress + '>',
        to: [to],
        subject: 'Sign in to ' + siteName,
        html: htmlBody,
      }),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      console.error('[magic-link-email] send failed:', res.status, text.slice(0, 400));
      return { ok: false, status: res.status };
    }
    return { ok: true };
  } catch (e) {
    console.error('[magic-link-email] network error:', e && e.message);
    return { ok: false, error: e && e.message };
  }
}

module.exports = { sendAdminNotification, sendMagicLinkEmail };
'''
    return ("services/notify.js", contents)


def _is_model_publicly_visible(rs: "ResolvedSpec", model: dict) -> bool:
    """True when the data_model surfaces on the public site (and therefore
    is worth promoting in the admin dashboard tiles / quick-links / nav).

    A model is "publicly visible" when ANY of the following hold:

      1. `model.public_listing_path` is set: archetype or keeper
         explicitly wired a public listing URL (e.g. /gallery, /journal).
      2. The model's backing table is the source for any widget in any
         resolved page_layout via `db:<table>`, e.g. when the home
         page's `testimonials-stack` widget reads `db:testimonials`,
         the Testimonial model surfaces on the home page.
      3. Any custom_page's `form_target` resolves to this model; 
         the submission form is on the public site.
      4. The model is a submission-target (ContactInquiry,
         NewsletterSubscriber, BookingRequest, …), even when no
         explicit form binding is detected, admins always need to
         see their inbox.

    Hides "admin-only" data_models that exist in the schema but never
    show up on the customer's public site. Without it, a healer-
    wellness build with several custom models shows a dashboard tile
    for each one (blog posts, photos, testimonials, booking intents
    and more), including content the public site does not render,
    because the archetype's `default_data_models` is a superset of
    what the keeper actually chose to surface. The hidden CRUD routes
    are still emitted and reachable by URL (`/admin/<slug>`); they're
    just not promoted in the dashboard / nav until the keeper or
    operator wires them onto a public page.
    """
    if not isinstance(model, dict):
        return False
    if model.get("public_listing_path"):
        return True
    table = _table_name_for_model(model)
    for layout in rs.page_layouts.values():
        for w in (layout.get("widgets") or []):
            if _db_source_table(w.get("source", "")) == table:
                return True
    model_aliases = {
        (model.get("name") or "").strip().lower(),
        (model.get("plural") or "").strip().lower(),
        (model.get("slug") or "").strip().lower(),
    }
    model_aliases.discard("")
    for cp in (rs.spec.custom_pages or []):
        if not isinstance(cp, dict):
            continue
        target = (cp.get("form_target") or "").strip().lower()
        if target and target in model_aliases:
            return True
    if _is_submission_target_model(model):
        return True
    return False


def _publicly_visible_data_models(rs: "ResolvedSpec") -> list:
    """Filter `rs.data_models` to the subset that surface on the public
    site per `_is_model_publicly_visible`. Used by the admin dashboard
    + admin nav so neither promotes models whose CRUD content the
    keeper's site never renders.
    """
    return [m for m in rs.data_models if _is_model_publicly_visible(rs, m)]


def emit_routes_admin_dashboard_js(rs: ResolvedSpec) -> tuple:
    """Admin dashboard router: mounted at /admin/. MW v25 pattern: /admin
    is the dashboard landing with stat cards, Upcoming Events, Quick
    Links, and bottom Change Password / Settings / Danger Zone cards.

    Also handles /admin/change-password (POST) and /admin/clear-all (POST).

    Stat cards + quick-links are restricted to
    publicly-visible models (`_publicly_visible_data_models`). Hidden
    models still have their CRUD routes mounted at /admin/<slug>; they
    are just not promoted from the dashboard. `CLEARABLE_TABLES`
    intentionally still lists every model table; the "Clear all"
    danger-zone button wipes the entire schema regardless of nav
    surfacing, since hidden tables may still hold orphan rows from
    earlier versions of the keeper's site.
    """
    visible_models = _publicly_visible_data_models(rs)
    slugs = [_slug_for(m) for m in visible_models]
    # Children before the parents they refer to: a foreign key that is
    # RESTRICT or NO ACTION stops the delete of a parent row that a child
    # still points to.
    all_tables = [
        _slug_for(m).replace("-", "_") for m in reversed(_models_in_creation_order(rs))
    ]
    # Each model gets both a total count and a
    # "new in last 7 days" count so the dashboard can surface a pill
    # when fresh submissions arrive. Uses created_at (every emitted
    # schema carries it via emit_schema_sql). Universal; works for
    # content models too, not just form_target ones, so the admin
    # sees any recent write activity at a glance.
    count_queries = "\n    ".join(
        (
            f"const {slug.replace('-','_')}Count = parseInt("
            f"(await db.query('SELECT COUNT(*) FROM {slug.replace('-','_')}'))"
            ".rows[0].count);\n    "
            f"const {slug.replace('-','_')}NewCount = parseInt("
            f"(await db.query(\"SELECT COUNT(*) FROM {slug.replace('-','_')} "
            "WHERE created_at > NOW() - INTERVAL '7 days'\"))"
            ".rows[0].count);"
        )
        for slug in slugs
    )
    stats_js = ",".join(
        (
            f'{{label: "{_prettify(slug).upper()}", '
            f'count: {slug.replace("-","_")}Count, '
            f'new_count: {slug.replace("-","_")}NewCount, '
            f'manage_href: "/admin/{slug}", slug: "{slug}"}}'
        )
        for slug in slugs
    )
    # "Recent inquiries" card: the newest rows of the contact model. The
    # card shows a name, an email and a service; each is read from the
    # model's column of that name when it has one and is NULL otherwise, so
    # the query is valid for whatever fields the spec gave the model.
    inquiry_model = _contact_form_model(rs)
    inquiries_admin_path = ""
    if inquiry_model is not None:
        inquiries_admin_path = f"/admin/{_slug_for(inquiry_model)}"

        def _inquiry_col(column: str) -> str:
            real = _model_column(inquiry_model, column)
            return _sql_ident(real) if real else "NULL"

        recent_sql = (
            f"SELECT id, {_sql_ident(_display_column(inquiry_model))} AS name, "
            f"{_inquiry_col('email')} AS email, {_inquiry_col('service_interest')} AS service, "
            f"created_at FROM {_sql_ident(_table_name_for_model(inquiry_model))} "
            "ORDER BY created_at DESC LIMIT 5"
        )
        recent_block = f'''
    let recent_inquiries = [];
    try {{
      const r = await db.query('{recent_sql}');
      recent_inquiries = r.rows;
    }} catch (e) {{
      console.error('[dashboard] recent inquiries:', e && e.message);
    }}'''
    else:
        recent_block = "    const recent_inquiries = [];"

    # Quick links: per-slug Manage + Settings + View Site
    quick_links_js = "["
    for slug in slugs:
        quick_links_js += (
            f'{{label: "Manage {_prettify(slug)}", href: "/admin/{slug}", '
            f'description: "Add, edit, publish, and remove {_prettify(slug).lower()}."}},'
        )
    quick_links_js += '{label: "Site Settings", href: "/admin/settings", description: "Update hero, logo, contact info, and social links."},'
    quick_links_js += '{label: "View Site", href: "/", description: "See your public-facing site."}'
    quick_links_js += "]"

    # "Upcoming events" card: shown when the build has an Event model with
    # a date column to order by (`_date_column`). Table, admin path and
    # columns come from that model; each optional column (times, end date,
    # location) is NULL when the model does not have it.
    event_model = next(
        (m for m in rs.data_models if isinstance(m, dict) and m.get("name") == "Event"), None
    )
    event_date_col = _date_column(event_model) if event_model is not None else None
    has_events = event_date_col is not None
    events_admin_path = ""
    if has_events:
        events_admin_path = f"/admin/{_slug_for(event_model)}"
        ev_date = _sql_ident(event_date_col)
        ev_time = _model_column(event_model, "event_time")

        def _event_col(column: str) -> str:
            real = _model_column(event_model, column)
            return f"{_sql_ident(real) if real else 'NULL'} AS {column}"

        upcoming_sql = (
            f"SELECT id, {_sql_ident(_display_column(event_model))} AS title, {ev_date} AS event_date, "
            + ", ".join(_event_col(c) for c in ("event_time", "end_date", "end_time", "location"))
            + f" FROM {_sql_ident(_table_name_for_model(event_model))} WHERE {ev_date} >= CURRENT_DATE "
            f"ORDER BY {ev_date} ASC, "
            + (f"{_sql_ident(ev_time)} ASC NULLS LAST, " if ev_time else "")
            + "id ASC LIMIT 5"
        )
        upcoming_block = f'''
    let upcoming_events = [];
    try {{
      const r = await db.query('{upcoming_sql}');
      upcoming_events = r.rows;
    }} catch (e) {{
      console.error('[dashboard] upcoming events:', e && e.message);
    }}'''
    else:
        upcoming_block = "    const upcoming_events = [];"

    contents = f'''const express = require('express');
const router = express.Router();
const bcrypt = require('bcrypt');
const db = require('../db');
const {{ requireAuth, requireRole, validateCsrfToken }} = require('../middleware/auth');

const CLEARABLE_TABLES = {json.dumps(all_tables)};

// GET /admin: dashboard landing.
router.get('/', requireAuth, requireRole('admin'), async (req, res) => {{
  try {{
    {count_queries}
    const stats = [{stats_js}];
{recent_block}
{upcoming_block}
    const quick_links = {quick_links_js};
    const flashSuccess = req.session.flash_success;
    const flashError = req.session.flash_error;
    delete req.session.flash_success;
    delete req.session.flash_error;
    res.render('admin/dashboard', {{
      title: 'Admin Dashboard',
      layout: 'layout-admin',
      stats,
      recent_inquiries,
      upcoming_events,
      events_enabled: {str(has_events).lower()},
      events_admin_path: '{events_admin_path}',
      inquiries_admin_path: '{inquiries_admin_path}',
      quick_links,
      flashSuccess,
      flashError
    }});
  }} catch (e) {{
    console.error(e);
    res.status(500).render('error-500', {{ title: 'Error' }});
  }}
}});

// POST /admin/change-password: update logged-in admin's password.
router.post('/change-password', requireAuth, requireRole('admin'), validateCsrfToken, async (req, res) => {{
  const body = req.body || {{}};
  const current_password = typeof body.current_password === 'string' ? body.current_password : '';
  const new_password = typeof body.new_password === 'string' ? body.new_password : '';
  // 72 bytes is bcrypt's input limit; see routes/auth.js set-password.
  if (new_password.length < 8 || Buffer.byteLength(new_password, 'utf8') > 72) {{
    req.session.flash_error = 'New password must be 8 to 72 characters.';
    return req.session.save(() => res.redirect('/admin'));
  }}
  try {{
    const {{ rows }} = await db.query('SELECT password FROM users WHERE id = $1', [req.session.userId]);
    if (rows.length === 0) {{
      req.session.flash_error = 'User not found.';
      return req.session.save(() => res.redirect('/admin'));
    }}
    if (rows[0].password) {{
      if (!current_password) {{
        req.session.flash_error = 'Current password is required.';
        return req.session.save(() => res.redirect('/admin'));
      }}
      const ok = await bcrypt.compare(current_password, rows[0].password);
      if (!ok) {{
        req.session.flash_error = 'Current password is incorrect.';
        return req.session.save(() => res.redirect('/admin'));
      }}
    }}
    const hash = await bcrypt.hash(new_password, 10);
    await db.query('UPDATE users SET password = $1, password_set = TRUE WHERE id = $2', [hash, req.session.userId]);
    req.session.flash_success = 'Password updated.';
    req.session.save(() => res.redirect('/admin'));
  }} catch (e) {{
    console.error(e);
    req.session.flash_error = 'Failed to update password.';
    req.session.save(() => res.redirect('/admin'));
  }}
}});

// POST /admin/clear-all: drop all content data (except users + sessions + settings).
router.post('/clear-all', requireAuth, requireRole('admin'), validateCsrfToken, async (req, res) => {{
  try {{
    for (const t of CLEARABLE_TABLES) {{
      await db.query('DELETE FROM ' + t);
    }}
    req.session.flash_success = 'All content data cleared. User accounts and settings preserved.';
    req.session.save(() => res.redirect('/admin'));
  }} catch (e) {{
    console.error(e);
    req.session.flash_error = 'Failed to clear data: ' + e.message;
    req.session.save(() => res.redirect('/admin'));
  }}
}});

module.exports = router;
'''
    return ("routes/admin-dashboard.js", contents)


def emit_routes_upload_js(rs: ResolvedSpec) -> tuple:
    """POST /admin/upload: accept a multipart image upload, save under
    public/uploads/YYYY/MM/, return JSON {url}.
    """
    contents = '''const express = require('express');
const router = express.Router();
const multer = require('multer');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');
const { requireAuth, requireRole, validateCsrfToken } = require('../middleware/auth');

// Raster image types only, decided by the file EXTENSION, because the
// extension is what express.static uses to pick the response Content-Type.
// The multipart mimetype is supplied by the client and proves nothing: the
// previous `image/*` test accepted `page.html` or `x.js` sent with an
// image mimetype, which were then served same-origin as HTML / script.
// SVG is excluded on purpose: it is a document format that can carry
// script, and it is served from this origin.
const ALLOWED_EXTENSIONS = new Set(['.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif']);
const ALLOWED_MIMETYPES = new Set(['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/avif']);

function uploadSubdir(d) {
  return String(d.getFullYear()) + '/' + String(d.getMonth() + 1).padStart(2, '0');
}

// Storage: public/uploads/YYYY/MM/<random>.<ext>. The stored name is
// generated here; nothing from the client-supplied filename is kept except
// its (allowlisted) extension.
const storage = multer.diskStorage({
  destination: function (req, file, cb) {
    const subdir = uploadSubdir(new Date());
    req.uploadSubdir = subdir;
    const dir = path.join(__dirname, '..', 'public', 'uploads', subdir);
    fs.mkdirSync(dir, { recursive: true });
    cb(null, dir);
  },
  filename: function (req, file, cb) {
    const ext = path.extname(file.originalname || '').toLowerCase();
    cb(null, Date.now() + '-' + crypto.randomBytes(8).toString('hex') + ext);
  }
});

const upload = multer({
  storage,
  limits: { fileSize: 10 * 1024 * 1024, files: 1 },
  fileFilter: function (req, file, cb) {
    const ext = path.extname(file.originalname || '').toLowerCase();
    if (!ALLOWED_EXTENSIONS.has(ext) || !ALLOWED_MIMETYPES.has(file.mimetype)) {
      const err = new Error('Only JPG, PNG, GIF, WebP or AVIF images can be uploaded.');
      err.status = 400;
      return cb(err);
    }
    cb(null, true);
  }
});

// POST /admin/upload: returns {url} relative to site root. The CSRF token
// arrives in the x-csrf-token header (the multipart body is not parsed yet
// when validateCsrfToken runs). Upload errors answer with JSON, since the
// caller is fetch() in public/js/upload-zone.js, not a page navigation.
router.post('/upload', requireAuth, requireRole('admin'), validateCsrfToken, (req, res) => {
  upload.single('file')(req, res, (err) => {
    if (err) {
      const tooLarge = err.code === 'LIMIT_FILE_SIZE';
      return res.status(tooLarge ? 413 : 400).json({
        error: tooLarge ? 'File is larger than 10 MB.' : (err.status === 400 ? err.message : 'Upload failed.')
      });
    }
    if (!req.file) return res.status(400).json({ error: 'No file uploaded' });
    res.json({ url: '/uploads/' + req.uploadSubdir + '/' + req.file.filename });
  });
});

module.exports = router;
'''
    return ("routes/upload.js", contents)


def emit_routes_admin_settings_js(rs: ResolvedSpec) -> tuple:
    """Admin settings router: mounted at /admin/. Handles GET + POST
    /admin/settings. Separate from the admin dashboard router so the
    two can evolve independently.
    """
    # Settings field layout
    settings_sections = [
        {
            "heading": "Site Identity",
            "fields": [
                {"key": "site_name", "label": "Site Name", "type": "text"},
                {"key": "site_description", "label": "Site Description", "type": "textarea"},
            ]
        },
        {
            "heading": "Hero",
            "fields": [
                {"key": "hero_tagline", "label": "Hero Tagline", "type": "text"},
                {"key": "hero_description", "label": "Hero Description", "type": "textarea"},
                {"key": "hero_image_url", "label": "Hero Image", "type": "file"},
            ]
        },
        {
            "heading": "Logo & About",
            "fields": [
                {"key": "nav_logo_url", "label": "Navigation Logo", "type": "file"},
                {"key": "about_title", "label": "About Page Title", "type": "text"},
                {"key": "about_body", "label": "About Page Body", "type": "textarea"},
                {"key": "about_photo_url", "label": "About Photo", "type": "file"},
            ]
        },
        {
            "heading": "Contact",
            "fields": [
                {"key": "contact_email", "label": "Contact Email", "type": "email"},
                {"key": "contact_phone", "label": "Phone", "type": "tel"},
                {"key": "contact_address", "label": "Address", "type": "text"},
                {"key": "venmo_handle", "label": "Venmo Handle", "type": "text"},
            ]
        },
        {
            "heading": "Social",
            "fields": [
                {"key": "instagram_url", "label": "Instagram URL", "type": "url"},
                {"key": "tiktok_url", "label": "TikTok URL", "type": "url"},
                {"key": "youtube_url", "label": "YouTube URL", "type": "url"},
                {"key": "facebook_url", "label": "Facebook URL", "type": "url"},
                {"key": "twitter_url", "label": "Twitter/X URL", "type": "url"},
                {"key": "linkedin_url", "label": "LinkedIn URL", "type": "url"},
            ]
        }
    ]
    # Page Text sections (v2-full-editability-audit): one section per
    # public page exposing every customer-voice string. Data-driven, so
    # this is a data change, not new view code; ALLOWED_KEYS derives
    # from the sections and automatically accepts the new keys.
    for _pid, _entries in _page_text_settings(rs).items():
        settings_sections.append({
            "heading": f"Page Text: {_prettify(_pid)}",
            "fields": [
                {"key": _e["key"], "label": _e["label"], "type": _e["type"]}
                for _e in _entries
            ],
        })

    sections_js = json.dumps(settings_sections, indent=0).replace('\n', ' ')
    allowed_keys = [f["key"] for s in settings_sections for f in s["fields"]]
    allowed_keys_js = json.dumps(allowed_keys)

    contents = f'''const express = require('express');
const router = express.Router();
const db = require('../db');
const {{ requireAuth, requireRole, validateCsrfToken }} = require('../middleware/auth');
const {{ invalidateSettingsCache }} = require('../middleware/settings');

const SETTINGS_SECTIONS = {sections_js};
const ALLOWED_KEYS = {allowed_keys_js};

router.get('/settings', requireAuth, requireRole('admin'), async (req, res) => {{
  try {{
    const {{ rows }} = await db.query('SELECT key, value FROM site_settings');
    const current = {{}};
    rows.forEach(r => {{ current[r.key] = r.value; }});
    const sections = SETTINGS_SECTIONS.map(s => ({{
      heading: s.heading,
      fields: s.fields.map(f => ({{ ...f, value: current[f.key] || '' }}))
    }}));
    const flashSuccess = req.session.flash_success;
    const flashError = req.session.flash_error;
    delete req.session.flash_success;
    delete req.session.flash_error;
    res.render('admin/settings', {{ title: 'Site Settings', layout: 'layout-admin', sections, flashSuccess, flashError }});
  }} catch (e) {{
    console.error(e);
    res.status(500).render('error-500', {{ title: 'Error' }});
  }}
}});

router.post('/settings', requireAuth, requireRole('admin'), validateCsrfToken, async (req, res) => {{
  try {{
    for (const key of ALLOWED_KEYS) {{
      // Omission must never be destructive: a key absent from the POST
      // body means "leave unchanged" (partial/programmatic POSTs would
      // otherwise blank every field they did not include). An explicit
      // empty-string submit still clears the value intentionally.
      if (req.body[key] === undefined) continue;
      const value = String(req.body[key]);
      await db.query(
        'INSERT INTO site_settings (key, value) VALUES ($1, $2) '
        + 'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()',
        [key, value]
      );
    }}
    invalidateSettingsCache();
    req.session.flash_success = 'Site settings saved.';
    req.session.save(() => res.redirect('/admin/settings'));
  }} catch (e) {{
    console.error(e);
    req.session.flash_error = 'Could not save settings: ' + e.message;
    req.session.save(() => res.redirect('/admin/settings'));
  }}
}});

module.exports = router;
'''
    return ("routes/admin-settings.js", contents)


def emit_routes_pages_js(rs: ResolvedSpec) -> tuple:
    """Public pages router; one handler per spec.pages entry.

    The contact page handler reads `?service=<name>` and forwards it to the
    view as `preselected_service` so form-contact.ejs can <option selected>
    the matching service option. Used by Work With Me CTAs.

    When a customer-declared custom_page path collides
    with an archetype default page (e.g. `/about` with title="Our Story"
    for the coach archetype), the customer's title wins as the <title>
    suffix instead of the archetype's hand-curated default.
    """
    # Build the page_id → customer_title override map from any custom_page
    # whose path equals an archetype page path. The same map exists for
    # per-page descriptions, so the meta tag on a customer-titled /about
    # reflects the customer's narrative blurb rather than the spec-level
    # fallback.
    archetype_default_path_to_id = {}
    for p in rs.archetype.get("default_pages", []):
        archetype_default_path_to_id["/" if p == "home" else f"/{p}"] = p
    page_title_overrides = {}
    page_description_overrides = {}
    for cp in (rs.spec.custom_pages or []):
        if not isinstance(cp, dict):
            continue
        cp_path = (cp.get("path") or "").strip()
        cp_title = (cp.get("title") or "").strip()
        cp_desc = (cp.get("description") or "").strip()
        if cp_path in archetype_default_path_to_id:
            collide_page_id = archetype_default_path_to_id[cp_path]
            if cp_title:
                page_title_overrides[collide_page_id] = cp_title
            if cp_desc:
                page_description_overrides[collide_page_id] = cp_desc

    # Spec-level meta-description fallback. Used when a page
    # (archetype-default OR custom) does not supply its own description. We
    # still emit the explicit local so the layout's runtime `<meta
    # name="description">` is deterministic per route rather than relying on
    # the layout's downstream `_s.site_description || _s.hero_tagline`
    # fallback (which renders blank until the admin saves /admin/settings).
    spec_settings = rs.spec.settings or {}
    spec_description_fallback = _truncate_meta_description_for_seo(
        spec_settings.get("site_description")
        or spec_settings.get("hero_tagline")
        or ""
    )

    handlers = []
    for page_id, layout in rs.page_layouts.items():
        # page_id is either an archetype default (decision file) or a
        # faithful-copy page id that resolve_spec checked against
        # _PAGE_ID_RE; _route_path restates that at the point of emission.
        path = "/" if page_id == "home" else _route_path(f"/{page_id}")
        # Allow `spec.pages[page_id].title` and
        # `spec.pages[page_id].description` to override the prettified
        # page-id default. Customer custom_page collisions still win
        # via page_*_overrides.
        page_spec_for_seo = rs.spec.pages.get(page_id, {}) if isinstance(rs.spec.pages, dict) else {}
        if not isinstance(page_spec_for_seo, dict):
            page_spec_for_seo = {}
        page_spec_title = _text(page_spec_for_seo.get("title"))
        page_spec_description = _text(page_spec_for_seo.get("description"))
        title_suffix = (
            page_title_overrides.get(page_id)
            or page_spec_title
            or _prettify(page_id)
        )
        title_esc = _js_escape(title_suffix)
        meta_description_raw = (
            page_description_overrides.get(page_id)
            or page_spec_description
            or spec_description_fallback
        )
        meta_description_esc = _js_escape(
            _truncate_meta_description_for_seo(meta_description_raw)
        )
        # If any widget on this page declares source: "db:<table>", SELECT
        # the rows and pass them to the view as `items`. This replaces the
        # build-time static spec items with live DB content so admin CRUD
        # edits show up on the public page immediately (no rebuild).
        db_widget_id, db_table = _page_db_source_for(layout)
        # If the db_table belongs to a model that was excluded via
        # _harbor_meta.exclude_data_models, the table won't exist in the
        # schema. Treat it as no-DB-source so the route renders with items=[].
        if db_table:
            _known_tables = {_table_name_for_model(m) for m in rs.data_models}
            if db_table not in _known_tables:
                db_table = ""
        # Per-page <title>, specific-first (v2-seo-headers-jsonld):
        # interior pages lead with the page name ("About | Site"), the
        # home page is the brand itself plus a short tagline when set.
        if page_id == "home":
            # Append the tagline ONLY when it adds something the site name
            # doesn't already say. In the v1->v2 translate path site_name
            # and hero_tagline both default to the humanized app_name when
            # neither is supplied, so a naive append doubles the title
            # ("Sample Novelist | Sample Novelist"), which reads as a stutter.
            # Dedupe case-insensitively, either
            # direction (substring or equal).
            title_js = (
                "(function(){"
                " var _sn = res.locals.settings.site_name || 'Site';"
                " var _tag = res.locals.settings.hero_tagline"
                " ? String(res.locals.settings.hero_tagline).slice(0, 60) : '';"
                " if (!_tag) return _sn;"
                " var _a = _sn.toLowerCase().trim(), _b = _tag.toLowerCase().trim();"
                " if (!_b || _a === _b || _a.indexOf(_b) !== -1 || _b.indexOf(_a) !== -1) return _sn;"
                " return _sn + ' | ' + _tag;"
                "})()"
            )
        else:
            title_js = (
                f"'{title_esc} | ' + (res.locals.settings.site_name || 'Site')"
            )
        if page_id == "contact":
            handler = f'''router.get('{path}', async (req, res) => {{
  try {{
    const preselected_service = (req.query && req.query.service) ? String(req.query.service) : '';
    res.render('{page_id}', {{
      title: {title_js},
      layout: 'layout-public',
      preselected_service,
      page_title: '{title_esc}',
      metaDescription: '{meta_description_esc}',
      canonicalUrl: '{path}'
    }});
  }} catch (e) {{
    console.error(e);
    res.status(500).render('error-500', {{ title: 'Error' }});
  }}
}});'''
        elif db_table:
            # Decorate each row with `detail_href` so listing/shelf widgets
            # link to the table's public per-item detail page. Decoration is
            # keyed on the TABLE's canonical public slug (not the current
            # page path) so a home-page cover shelf links to /books/<slug>
            # the same way the catalog page does.
            emits_detail = db_widget_id in _DETAIL_PAGE_WIDGETS
            has_slug = _table_has_slug_column(rs, db_table) if emits_detail else False
            detail_tables = _tables_with_detail_pages(rs)
            db_sources = _page_db_sources_for(rs, layout)

            def _decorate_for(table: str, var: str) -> str:
                detail_path = detail_tables.get(table)
                if not detail_path:
                    return ""
                expr = "(r.slug || r.id)" if _table_has_slug_column(rs, table) else "r.id"
                return (
                    f"\n    {var}.forEach(r => {{ r.detail_href = "
                    f"'{detail_path}/' + {expr}; }});"
                )

            # First table keeps the legacy `items` local (back-compat with
            # every existing view + test); every table ALSO lands as
            # items_<table> so multi-table pages stop feeding the first
            # table's rows to every widget (author home: books +
            # testimonials).
            select_lines = [
                f"const {{ rows: items }} = await db.query('SELECT * FROM {db_table}{_public_rows_where(rs, db_table)} ORDER BY id ASC');"
                + _decorate_for(db_table, "items")
            ]
            extra_locals = [f"items_{db_table}: items"]
            for _wid, _t in db_sources:
                if _t == db_table:
                    continue
                var = f"items_{_t}"
                select_lines.append(
                    f"\n    const {{ rows: {var} }} = await db.query('SELECT * FROM {_t}{_public_rows_where(rs, _t)} ORDER BY id ASC');"
                    + _decorate_for(_t, var)
                )
                extra_locals.append(f"{var}: {var}")
            selects_js = "".join(select_lines)
            extra_locals_js = ",\n      ".join(extra_locals)
            handler = f'''router.get('{path}', async (req, res) => {{
  try {{
    {selects_js}
    res.render('{page_id}', {{
      title: {title_js},
      layout: 'layout-public',
      page_title: '{title_esc}',
      metaDescription: '{meta_description_esc}',
      canonicalUrl: '{path}',
      items: items,
      {extra_locals_js}
    }});
  }} catch (e) {{
    console.error(e);
    res.status(500).render('error-500', {{ title: 'Error' }});
  }}
}});'''
            # Sibling public detail route. /<page>/:identifier looks up by
            # slug (when the table has one) OR integer id. Renders the
            # per-page detail view emitted by emit_page_detail_view.
            if emits_detail:
                # Same visibility rule as the listing above: a row an admin
                # has explicitly unpublished must not be readable by
                # guessing its id or slug.
                _pub_col = _table_publication_column(rs, db_table)
                _pub_and = f" AND {_published_condition(_pub_col)}" if _pub_col else ""
                if has_slug:
                    detail_sql = (
                        f"'SELECT * FROM {db_table} WHERE (slug::text = $1 OR "
                        f"id::text = $1){_pub_and} LIMIT 1'"
                    )
                else:
                    detail_sql = (
                        f"'SELECT * FROM {db_table} WHERE id::text = $1"
                        f"{_pub_and} LIMIT 1'"
                    )
                # Relation-filtered child rows: when a model
                # belongs_to this page's model (Testimonial belongs_to
                # Book), the detail page gets the children scoped to THIS
                # record as `related_rows` ("What readers are saying").
                # Only for a child whose rows are public in their own
                # right (`_related_child_info`): `belongs_to` says how two
                # tables are joined, not that the child may be shown.
                related = _related_child_info(rs, db_table)
                if related is not None:
                    _rel_table, _rel_fk = related
                    _rel_pub = _table_publication_column(rs, _rel_table)
                    _rel_and = f" AND {_published_condition(_rel_pub)}" if _rel_pub else ""
                    related_js = f'''
    let related_rows = [];
    try {{
      const _rel = await db.query('SELECT * FROM {_rel_table} WHERE {_rel_fk} = $1{_rel_and} ORDER BY id ASC', [record.id]);
      related_rows = _rel.rows;
    }} catch (e) {{ console.error('related rows:', e); }}'''
                    related_local = "\n      related_rows: related_rows,"
                else:
                    related_js = ""
                    related_local = ""
                detail_handler = f'''router.get('{path}/:identifier', async (req, res) => {{
  try {{
    const ident = String(req.params.identifier || '');
    const {{ rows }} = await db.query(
      {detail_sql},
      [ident]
    );
    if (rows.length === 0) {{
      return res.status(404).render('error-404', {{ title: '404', layout: 'layout-public' }});
    }}
    const record = rows[0];{related_js}
    const _recordSummary = (record.summary || record.excerpt || record.description || '').toString().trim();
    const _recordMeta = _recordSummary.length > 0
      ? (_recordSummary.length > 155 ? _recordSummary.slice(0, 155).replace(/\\s+\\S*$/, '').replace(/[\\s,.;:\\-]+$/, '') + '…' : _recordSummary)
      : '{meta_description_esc}';
    // Per-entity link preview: the shared
    // link for THIS record previews the record's own image, not the
    // generic site card. Tolerant column reads cover the image-field
    // names archetype models use.
    const _recordImage = (record.cover_image_url || record.image_url || record.photo_url || record.cover_url || record.thumbnail_url || record.hero_image_url || '').toString().trim();
    const _recordName = (record.title || record.name || '{title_esc}').toString();
    const _siteName = (res.locals.settings.site_name || 'Site');
    // Specific-first title, deduped: a record name already carrying the
    // site name is not doubled.
    const _detailTitle = _recordName.toLowerCase().indexOf(_siteName.toLowerCase()) !== -1
      ? _recordName
      : _recordName + ' | ' + _siteName;
    res.render('{page_id}-detail', {{
      title: _detailTitle,
      layout: 'layout-public',
      page_title: _recordName,
      metaDescription: _recordMeta,
      canonicalUrl: '{path}/' + ident,
      ogTitle: _recordName,
      ogDescription: _recordMeta,
      ogImage: _recordImage || undefined,
      ogImageAlt: _recordName,
      ogType: 'article',
      publishedAt: record.published_at || undefined,
      record: record,{related_local}
      listing_path: '{path}',
      listing_label: '{title_esc}'
    }});
  }} catch (e) {{
    console.error(e);
    res.status(500).render('error-500', {{ title: 'Error' }});
  }}
}});'''
                handlers.append(detail_handler)
        else:
            handler = f'''router.get('{path}', async (req, res) => {{
  try {{
    res.render('{page_id}', {{ title: {title_js}, layout: 'layout-public', page_title: '{title_esc}', metaDescription: '{meta_description_esc}', canonicalUrl: '{path}' }});
  }} catch (e) {{
    console.error(e);
    res.status(500).render('error-500', {{ title: 'Error' }});
  }}
}});'''
        handlers.append(handler)

    # The two built-in public forms. Each POST handler is emitted only when
    # a page of this build shows the form (so no build carries a public
    # write endpoint that nothing links to), and each takes its table and
    # columns from the model definition. Both are CSRF-checked and rate
    # limited, the same as the custom public forms below; the widgets
    # render the `_csrf` hidden input.
    contact_handler = _contact_post_handler_js(rs)
    if contact_handler:
        handlers.append(contact_handler)
    newsletter_handler = _newsletter_post_handler_js(rs)
    if newsletter_handler:
        handlers.append(newsletter_handler)

    # Customer-declared pages that don't map to
    # archetype defaults (e.g. /book, /rsvp, /our-story) get a route +
    # a view emitted here. auth_required=true pages are gated by
    # requireAuth; allowed_roles=['admin'] additionally gated by
    # requireRole('admin'). Collision with an archetype-default path is
    # resolved by _custom_pages_filter(rs): the archetype page wins
    # and the customer title survives as a settings override.
    for cp in _custom_pages_filter(rs):
        # _custom_pages_filter only returns plain route paths, so `path`
        # and the slug derived from it are safe to place in a route
        # literal, a view name and a session key.
        path = _route_path(cp["path"])
        slug = _slug_from_path(path)
        title = cp.get("title") or _prettify(slug)
        # Per-route <meta name="description">. Falls
        # back to the spec-level site_description / hero_tagline so even
        # an informational page without its own description still emits
        # a deterministic per-route meta tag rather than relying on the
        # layout's downstream fallback.
        cp_meta_description_esc = _js_escape(
            _truncate_meta_description_for_seo(
                (cp.get("description") or "").strip()
                or spec_description_fallback
            )
        )
        auth_req = bool(cp.get("auth_required"))
        # One guard list per page, used by EVERY route emitted for it below
        # (GET, and POST when the page is a form).
        guard_mw = "".join(f", {g}" for g in _custom_page_guards(cp))
        layout = "layout-admin" if auth_req else "layout-public"
        title_esc = _js_escape(title)
        purpose_esc = _js_escape(cp.get("purpose", ""))

        # A page whose form_target names a model of this build gets a GET
        # and a POST route. The POST checks the CSRF token, turns a honeypot
        # submission away, validates every field (`_public_form_checks`),
        # stores the row (unpublished, when the model has a boolean
        # `published` column: `_public_insert_sql`) and redirects back to the GET with a
        # session flash. Any other page gets the GET-only handler further
        # down.
        form_model = _find_data_model(rs, cp.get("form_target") or "")
        if form_model is not None:
            fields = _public_form_fields(cp, form_model)
            checks = _public_form_checks(form_model, fields)
            success_msg = cp.get("success_message") or (
                "Thanks for reaching out. We will get back to you soon."
            )
            session_key = "_form_submitted_" + slug.replace("-", "_")
            # With no submittable column there is nothing to insert; the
            # handler answers "Submission unavailable." before reaching it.
            insert_sql = _public_insert_sql(form_model, [f["name"] for f in fields]) if fields else ""
            insert_note = (
                "// Stored unpublished: a row created through this form is not shown\n"
                "    // on the site until the owner publishes it in the admin.\n    "
                if _publication_column(form_model) and fields else ""
            )

            get_handler = f"""router.get('{path}'{guard_mw}, async (req, res) => {{
  try {{
    // Pull prior submission + flash out of session so repopulation works
    // after a validation failure or a genuine server error.
    const submitted = req.session.{session_key} || {{}};
    delete req.session.{session_key};
    const flashSuccess = req.session.flash_success;
    const flashError = req.session.flash_error;
    delete req.session.flash_success;
    delete req.session.flash_error;
    res.render('custom/{slug}', {{
      title: '{title_esc} | ' + (res.locals.settings.site_name || 'Site'),
      layout: '{layout}',
      page_title: '{title_esc}',
      page_purpose: '{purpose_esc}',
      metaDescription: '{cp_meta_description_esc}',
      canonicalUrl: '{path}',
      submitted,
      flashSuccess,
      flashError
    }});
  }} catch (e) {{
    console.error(e);
    res.status(500).render('error-500', {{ title: 'Error' }});
  }}
}});"""

            # The POST takes the same guards as the GET above. A form on an
            # auth_required page must not accept submissions from a visitor
            # who could not have loaded the form.
            post_handler = _fill_template("""router.post('__PATH__', formLimiter__GUARDS__, validateCsrfToken, async (req, res) => {
  try {
    // Honeypot: real users leave website_url_hp blank. Bots fill every
    // visible input. Silent-success so bots do not learn we filter.
    if (req.body && req.body.website_url_hp) {
      req.session.flash_success = __SUCCESS__;
      return res.redirect('__PATH__');
    }
    const { processPublicForm } = require('../middleware/fields');
    // The model's visitor-facing columns, each with the checks a value
    // must pass before anything is stored.
    const FIELDS = __FIELDS__;
    if (FIELDS.length === 0) {
      // The model has no column a visitor may fill in.
      req.session.flash_error = 'Submission unavailable.';
      return res.redirect('__PATH__');
    }
    const { values, errors, submitted } = processPublicForm(FIELDS, req.body);
    if (Object.keys(errors).length > 0) {
      // Back to the form, with what was typed and what is wrong with it.
      req.session.__SESSION_KEY__ = submitted;
      req.session.flash_error = Object.values(errors).join(' __MIDDOT__ ');
      return res.redirect('__PATH__');
    }
    __INSERT_NOTE__const insertRes = await db.query(
      '__INSERT_SQL__ RETURNING id',
      FIELDS.map(f => (values[f.name] === undefined || values[f.name] === '') ? null : values[f.name])
    );
    req.session.flash_success = __SUCCESS__;
    // Fire-and-forget admin notification. Does NOT block the redirect;
    // missing RESEND_API_KEY logs once and skips the network call so the
    // form still works on apps that have not wired mail.
    try {
      const newId = insertRes && insertRes.rows && insertRes.rows[0] && insertRes.rows[0].id;
      const scheme = (req.secure || req.get('x-forwarded-proto') === 'https') ? 'https' : 'http';
      const manageUrl = scheme + '://' + (req.get('host') || '') + '/admin/__ADMIN_SLUG__' + (newId ? ('/' + newId) : '');
      sendAdminNotification({
        subject: 'New __TITLE__ submission',
        summary: 'Someone just submitted the __TITLE__ form on your site.',
        fields: submitted,
        manageUrl,
      }).catch(function(e) { console.error('[notify] admin-notify async failed:', e && e.message); });
    } catch (_notifyErr) {
      // Never block the redirect on a notification glitch.
    }
    res.redirect('__PATH__');
  } catch (e) {
    console.error(e);
    req.session.flash_error = 'We could not process your submission. Please try again.';
    res.redirect('__PATH__');
  }
});""", {
                "PATH": path,
                "GUARDS": guard_mw,
                # json.dumps output is ASCII-only, so both are valid
                # JavaScript literals whatever the spec text contains.
                "SUCCESS": json.dumps(success_msg),
                "FIELDS": json.dumps(checks),
                "SESSION_KEY": session_key,
                "MIDDOT": chr(0x00B7),
                "INSERT_NOTE": insert_note,
                "INSERT_SQL": insert_sql,
                # /admin/<slug> uses the dash form of the model's slug.
                "ADMIN_SLUG": _slug_for(form_model),
                "TITLE": title_esc,
            })
            handlers.append(get_handler)
            handlers.append(post_handler)
            continue

        # Informational-only custom page; title rendered via
        # res.locals.settings.site_name + customer-declared page title.
        handler = f'''router.get('{path}'{guard_mw}, async (req, res) => {{
  try {{
    res.render('custom/{slug}', {{
      title: '{title_esc} | ' + (res.locals.settings.site_name || 'Site'),
      layout: '{layout}',
      page_title: '{title_esc}',
      page_purpose: '{purpose_esc}',
      metaDescription: '{cp_meta_description_esc}',
      canonicalUrl: '{path}'
    }});
  }} catch (e) {{
    console.error(e);
    res.status(500).render('error-500', {{ title: 'Error' }});
  }}
}});'''
        handlers.append(handler)

    # If any custom page requires auth, router.js's auth middleware
    # imports must be present. We unconditionally require them so no
    # additional plumbing is needed; unused imports are fine in JS.
    header_imports = (
        "const express = require('express');\n"
        "const router = express.Router();\n"
        "const db = require('../db');\n"
        "const { requireAuth, requireRole, requireAnyRole, validateCsrfToken } = require('../middleware/auth');\n"
        "const { formLimiter } = require('../middleware/rate-limit');\n"
        "const { sendAdminNotification } = require('../services/notify');\n\n"
    )
    contents = header_imports + "\n\n".join(handlers) + "\n\nmodule.exports = router;\n"
    return ("routes/pages.js", contents)


# --- Custom page helpers -------------------------------------------------

def _custom_page_roles(cp: dict) -> List[str]:
    """A custom page's allowed_roles as the role names its guard checks:
    trimmed, de-duplicated in spec order, and with the two built-in names
    folded to lower case (a spec that writes "Admin" means the admin)."""
    roles: List[str] = []
    for role in (cp.get("allowed_roles") or []):
        if not isinstance(role, str):
            # Refused by _spec_safety_errors; skipped here so this helper
            # is safe on a spec that has not been through the gate.
            continue
        name = role.strip()
        if name.lower() in _BUILTIN_ROLES:
            name = name.lower()
        if name and name not in roles:
            roles.append(name)
    return roles


def _roles_imply_login(cp: Any) -> bool:
    """True for a custom page that lists `allowed_roles` without being
    marked `auth_required`. A list of roles is a restriction: the page is
    for the accounts that hold one of those roles, and an account is a
    login. Reading it as "public, the roles only say who manages it" would
    serve to everyone a page the spec named an audience for, so the roles
    make the page a protected one (`resolve_spec` sets `auth_required` on
    it, with a warning)."""
    return isinstance(cp, dict) and not cp.get("auth_required") and bool(_custom_page_roles(cp))


def _custom_page_guards(cp: dict) -> List[str]:
    """Express middleware guarding a custom page, as source text, in order.

    Empty for a public page. For a page that requires a login
    (`auth_required`, or roles listed, `_roles_imply_login`): `requireAuth`,
    then at most ONE role guard built from allowed_roles (`_custom_page_roles`):

      - no role listed, or "user" among them: no role guard. "user" is the
        role every account has unless it was given another one
        (`users.role` defaults to it), so it is read as "any signed-in
        account", the admin included.
      - one role: `requireRole('<role>')`.
      - several roles: `requireAnyRole([...])`, which passes an account that
        holds ANY ONE of them. An account has exactly one role, so a chain
        of one `requireRole` per listed role would let nobody through.

    Other role names are compared with `users.role` exactly. The emitted
    site seeds one account, the admin, and has no sign-up and no user
    management screen: an account with any other role exists only if the
    operator adds a row to `users` (db/post-migrate.sql is the hook for
    that), and it signs in by login link like the admin does.

    emit_routes_pages_js puts this one list on every route it emits for the
    page, so no verb of a protected page can be left open.

    A page that lists roles and is not marked auth_required is a protected
    page too (`_roles_imply_login`); `resolve_spec` records a warning for it.
    A v1 spec that meant "admin manages this, everyone can view" with
    {auth_required: false, allowed_roles: ["admin"]} gets a page that asks
    for the admin's login: the other reading serves, without any login, a
    page the spec limited to a role, and a login wall is the error that
    costs the owner a page, not their visitors' data.
    """
    if not (cp.get("auth_required") or _custom_page_roles(cp)):
        return []
    guards = ["requireAuth"]
    roles = _custom_page_roles(cp)
    if not roles or "user" in roles:
        return guards
    # A role name is free text from the spec: it is escaped for the
    # single-quoted literal it lands in, never pasted in raw.
    literals = [f"'{_js_escape(role)}'" for role in roles]
    if len(literals) == 1:
        guards.append(f"requireRole({literals[0]})")
    else:
        guards.append(f"requireAnyRole([{', '.join(literals)}])")
    return guards


def _js_escape(s: Any) -> str:
    """Body of a SINGLE-quoted JavaScript string literal in a .js file.

    Backslash and the quote are escaped; every line terminator a JS string
    literal may not contain (LF, CR, and the Unicode separators U+2028 /
    U+2029 on pre-ES2019 parsers) becomes a space, as do other control
    characters. Only valid between single quotes: it does not escape `"` or
    a backtick, so never use it inside a template literal. When in doubt use
    `_js_str`, which returns a complete double-quoted literal.
    """
    text = "" if s is None else str(s)
    text = text.replace("\\", "\\\\").replace("'", "\\'")
    return _JS_LINE_BREAK_RE.sub(" ", text)


_JS_LINE_BREAK_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")


def _text(value: Any) -> str:
    """A spec value that should be text, as text. Anything that is not a
    string (a number, a list, an object, null) is treated as absent rather
    than crashing the emitter on `.strip()`."""
    return value.strip() if isinstance(value, str) else ""


# Per-route <meta name="description">.
# Truncates a free-form page or spec description to ~155 chars on a word
# boundary so search-engine result snippets render cleanly and so the
# raw inline value never blows up the route handler line length.
_META_DESCRIPTION_MAX_LEN = 155


def _truncate_meta_description_for_seo(s: str, max_len: int = _META_DESCRIPTION_MAX_LEN) -> str:
    """Truncate a string to a clean meta-description length on a word
    boundary. Returns "" for falsy input. The cut prefers the last space
    inside the budget so we never produce mid-word ellipsis. Adds an
    ellipsis only when truncation actually happened. Mirrors the helper
    in compose/intake.py used at translate-time, but lives here so the
    route emitter is self-contained."""
    if not s:
        return ""
    s = " ".join(str(s).split())
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    space = cut.rfind(" ")
    if space >= int(max_len * 0.6):
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


def _slug_from_path(path: str) -> str:
    """Derive a URL-safe slug from a page path.
    `/book` → `book`; `/our-story` → `our-story`; `/a/b` → `a-b`."""
    s = (path or "/").strip("/")
    if not s:
        return "home"
    return s.replace("/", "-")


def _archetype_default_paths(archetype: dict, page_layouts: Optional[Dict[str, dict]] = None) -> set:
    """Set of paths the archetype's default_pages list covers (e.g.
    {'/', '/about', '/contact', '/services'}). Used to filter customer
    pages so archetype-default paths take precedence.

    When `page_layouts` is passed, intersect
    with its keys so paths that resolve_spec dropped (alias-group
    overlap with a customer page) are no longer reserved. Without
    this, a dropped archetype page like /journal would still block
    the customer from declaring /journal as their own custom page.
    Callers with a ResolvedSpec in hand pass rs.page_layouts; the
    classifier path (pre-resolve) calls with just archetype."""
    defaults = list(archetype.get("default_pages", []))
    if page_layouts is not None:
        defaults = [p for p in defaults if p in page_layouts]
    paths = set()
    for page_id in defaults:
        paths.add("/" if page_id == "home" else f"/{page_id}")
    return paths


def _public_model_mount(model: dict) -> Tuple[str, bool]:
    """`(mount path, has a public listing)` of a model's resource router.

    The one definition: emit_server_js mounts the router at this path, and
    emit_routes_resource_js and emit_admin_crud_views emit the public
    listing and detail only when the second value is true. The router is
    mounted at `public_listing_path` when the spec set one and at /<slug>
    otherwise, and it serves rows to visitors only in the first case.

    `public_listing_path` is the explicit form, and the only one: nothing in
    the engine sets it. An absent key, null and "" all mean "not listed".
    """
    listing_path = model.get("public_listing_path")
    return (listing_path or f"/{_slug_for(model)}", bool(listing_path))


def _layout_db_tables(layout_id: Any) -> set:
    """Tables the widgets of the page layout `layout_id` read from the
    database (`source: "db:<table>"`). Empty for an unknown or absent id."""
    if not layout_id:
        return set()
    try:
        layout = get_layout(layout_id)
    except ValueError:
        return set()
    tables = set()
    for w in layout.get("widgets") or []:
        table = _db_source_table(w.get("source", "")) if isinstance(w, dict) else ""
        if table:
            tables.add(table)
    return tables


def _public_row_sources(rs: ResolvedSpec) -> Dict[str, List[str]]:
    """Every way this build serves a model's rows to a visitor who is not
    signed in, as `{table: [where, ...]}`. A table that is not a key here
    has no public read at all.

    There are exactly two ways, and neither is inferred from a name the
    spec chose for something else:

      1. The spec set `public_listing_path` on the model. Its resource
         router then serves a listing at that path and a detail page per
         row (emit_routes_resource_js).
      2. A page layout of this build has a widget whose source is
         `db:<table>` (emit_routes_pages_js selects the rows for that page;
         a `_DETAIL_PAGE_WIDGETS` widget also gets `<page>/:identifier`).
         Layouts are the engine's own data (compose/data/page-layouts.json)
         and the archetype names one for each of its pages. A spec cannot
         write a layout or a widget source; it can only have a page's layout
         swapped for a sibling of the same variant group (a hint), and it
         gets the copy layout, which reads no table, for a page whose text
         it supplies. So this is the engine's list of what an archetype
         shows: journal posts, events, photos, reviews, testimonials and so
         on (compose/tests/test_spec_hardening.py enumerates it per
         archetype and walks every variant). A layout names a TABLE. A spec
         model that redefines the archetype's model of that name (or takes
         the table through `plural`) is therefore what the page shows;
         `_publication_warnings` reports each such model.

    Everything else that reads rows is one of these two or sits behind the
    admin login: the admin mounts, the dashboard, the operator scripts.
    The rows a detail page shows beside its record (a child model that
    `belongs_to` it) are read only when the child's table is itself a key
    here. The sitemap lists the paths of (1) and the pages of (2); it reads
    no rows.
    """
    sources: Dict[str, List[str]] = {}
    for m in rs.data_models:
        if not isinstance(m, dict):
            continue
        mount, has_public = _public_model_mount(m)
        if has_public:
            sources.setdefault(_table_name_for_model(m), []).append(
                f"its public listing at {mount} (public_listing_path)"
            )
    for page_id, layout in rs.page_layouts.items():
        if page_id == "contact":
            # emit_routes_pages_js gives the contact page its own handler,
            # which reads no rows.
            continue
        for widget_id, table in _page_db_sources_for(rs, layout):
            where = "the home page" if page_id == "home" else f"the /{page_id} page"
            sources.setdefault(table, []).append(f"{where} ({widget_id})")
    return sources


def _publication_warnings(rs: ResolvedSpec) -> List[Dict[str, str]]:
    """Build warnings about what this build shows to visitors that a
    reviewer could not see from the spec alone: each model the SPEC defines
    whose rows a page of the archetype shows (source 2 of
    `_public_row_sources`). The archetype's own untouched models are not
    reported; showing them is what the archetype is."""
    spec_tables = {
        _table_name_for_model(m): m.get("name")
        for m in rs.spec.data_models if isinstance(m, dict)
    }
    warnings: List[Dict[str, str]] = []
    for table, where in sorted(_public_row_sources(rs).items()):
        pages = [w for w in where if not w.startswith("its public listing")]
        if table in spec_tables and pages:
            warnings.append({
                "reason": "spec_model_shown_by_archetype_page",
                "detail": (
                    f"model {spec_tables[table]!r} is defined by the spec and uses the table "
                    f"{table!r}, which the archetype shows to visitors on {', '.join(pages)}; "
                    "its rows are public there (rows with published = false are left out)"
                ),
            })
    return warnings


def _earlier_route_owner(rs: ResolvedSpec, key: str) -> Optional[Tuple[str, bool]]:
    """Who answers a path BELOW a model's mount or below a listing page
    before the custom-page routes get a turn, as `(who, True when it serves
    rows without a login)`. None when nothing does. `key` is a `_path_key`.

    A model's resource router (emit_routes_resource_js) is mounted at
    `<mount>` ahead of routes/pages.js and answers these itself for a
    visitor:

        <mount>/new                       404
        <mount>/<x>/edit, update, delete  404
        <mount>/<x>                       the row's public detail page when
                                          the model has a public listing and
                                          404 when it has none; except that
                                          it yields when a page of the site
                                          owns <mount>

    A listing page whose widget links to detail pages
    (`_DETAIL_PAGE_WIDGETS`) has `<page>/:identifier` in routes/pages.js,
    registered before any custom page; it serves the row, or a 404.

    A custom page at any of these paths would never be reached.
    """
    page_paths = _archetype_default_paths(rs.archetype, rs.page_layouts)
    known_tables = set()
    for m in rs.data_models:
        if not isinstance(m, dict):
            continue
        known_tables.add(_table_name_for_model(m))
    for m in rs.data_models:
        if not isinstance(m, dict):
            continue
        mount, has_public = _public_model_mount(m)
        mount_key = _path_key(mount)
        if not key.startswith(mount_key + "/"):
            continue
        below = key[len(mount_key) + 1:].split("/")
        routes = f"the routes of the {m.get('name')} model"
        if below == ["new"] or (len(below) == 2 and below[1] in ("edit", "update", "delete")):
            return (routes, False)
        if len(below) == 1 and mount not in page_paths:
            if has_public:
                return (f"the public detail pages of the {m.get('name')} model", True)
            return (routes, False)
    base = key.rsplit("/", 1)[0]
    for page_id, layout in rs.page_layouts.items():
        if page_id in ("home", "contact") or _path_key(f"/{page_id}") != base:
            continue
        widget_id, table = _page_db_source_for(layout)
        if table in known_tables and widget_id in _DETAIL_PAGE_WIDGETS:
            return (f"the detail pages of the {page_id!r} page", True)
    return None


# Built-in routes a custom page may not claim that already sit behind a
# login, as path keys (everything under /admin/ is one too). A page marked
# auth_required that names one of these loses nothing: the built-in route
# answers there and asks for a login itself. /logout only has a POST.
_LOGIN_GUARDED_BUILTIN_PATHS = frozenset({"/admin", "/set-password", "/logout"})


def _classify_custom_pages(rs: ResolvedSpec) -> Tuple[list, list]:
    """Decide, once, which customer custom_pages the engine emits.

    Returns `(kept, dropped)`. `kept` is the list of page dicts the route,
    view, sitemap and navigation emitters all work from, so a page cannot
    exist for one of them and not for another. `dropped` is a list of
    `(page, reason)` pairs for `_custom_page_warnings` and
    `_auth_required_page_errors`.

    The rule behind every reason below: a page is kept only if its own
    route is the first one to answer its path. server.js mounts the static
    files, the built-in routers and every model's resource router before
    routes/pages.js, and routes/pages.js registers the site's own pages
    before the custom ones, so anything that already answers a path there
    wins and the custom page (with its guards) would never run.

    Paths are compared by `_path_key` (lower case, no trailing slash),
    because that is how Express matches them. A page is dropped when:

      - `not_plain`: the path is not a plain route path (quotes, dots,
        query strings, spaces, Express pattern characters, no leading
        slash, ...). It would be pasted into a route literal and a view
        file name. resolve_spec records a custom_page_path_dropped warning.
      - `parametric`: the path has an Express `:param` segment (e.g.
        /events/:event-slug). `_slug_from_path` would produce a view name
        containing ":", and the route would shadow the archetype's own
        detail handler without supplying a view.
      - `page_exists`: a page the build already emits (an archetype default
        that survived resolve_spec, or a faithful-copy page) owns the path.
        That page wins; the customer's title still applies through the
        title override in emit_routes_pages_js.
      - `engine_route`: the path is a built-in route (`_RESERVED_CUSTOM_PAGE_PATHS`,
        the engine's own /admin routes, /admin/<model slug> and below).
      - `public_under_admin`: any other path under /admin on a page that is
        not auth_required. The engine owns /admin; a custom page may live
        there only behind the login.
      - `model_listing`: a model's public listing is mounted at this path
        (the spec set public_listing_path on the model). The resource
        router renders the listing there, for a form page too.
      - `model_route`: the path is the mount of a submission-target model
        that has no public listing. Its router answers visitors with a 404
        there on purpose (those rows are private) and does not yield. A
        form page (form_target set) is the exception and is kept: a form at
        /rsvps that stores Rsvp rows is what that path is for, and the
        router hands GET and POST of the mount path on to it.
      - `detail_route`: the path lies below a model's mount or a listing
        page, at a spot their own routes answer first
        (`_earlier_route_owner`).
      - `duplicate`: an earlier custom page has the same path. The first
        one wins.
      - `duplicate_view`: an earlier custom page has a different path that
        maps to the same view file ("/a/b" and "/a-b" both become
        views/custom/a-b.ejs). The first one wins and this path has no
        route.

    A page at the mount path of any other model that has no public listing
    (/clients beside a Client model) is KEPT: it is the spec's page, the
    model's router hands the path to it, and it shows no rows
    (`_custom_page_warnings` reports it as `page_named_like_model`).
    """
    kept: list = []
    dropped: list = []
    if not rs.spec.custom_pages:
        return kept, dropped
    page_keys = {
        _path_key("/" if page_id == "home" else f"/{page_id}")
        for page_id in rs.page_layouts
    }
    listing_keys = set()
    private_mount_keys = set()
    admin_model_prefixes = set()
    for m in rs.data_models:
        if not isinstance(m, dict):
            continue
        mount, has_public = _public_model_mount(m)
        if has_public:
            listing_keys.add(_path_key(mount))
        elif _is_submission_target_model(m):
            private_mount_keys.add(_path_key(mount))
        admin_model_prefixes.add(f"/admin/{_slug_for(m)}".lower())
    seen_keys: set = set()
    seen_views: set = set()
    for cp in rs.spec.custom_pages:
        if not isinstance(cp, dict):
            continue
        path = _text(cp.get("path"))
        if not path:
            continue
        if _is_parametric_path(path):
            dropped.append((cp, "parametric"))
            continue
        if not _is_safe_route_path(path):
            dropped.append((cp, "not_plain"))
            continue
        key = _path_key(path)
        if key in page_keys:
            dropped.append((cp, "page_exists"))
            continue
        if key in _RESERVED_CUSTOM_PAGE_PATHS:
            dropped.append((cp, "engine_route"))
            continue
        if key.startswith("/admin/"):
            if key in _ENGINE_ADMIN_PATHS or any(
                key == prefix or key.startswith(prefix + "/")
                for prefix in admin_model_prefixes
            ):
                dropped.append((cp, "engine_route"))
                continue
            if not cp.get("auth_required"):
                dropped.append((cp, "public_under_admin"))
                continue
        if key in listing_keys:
            dropped.append((cp, "model_listing"))
            continue
        if key in private_mount_keys and not cp.get("form_target"):
            dropped.append((cp, "model_route"))
            continue
        if _earlier_route_owner(rs, key) is not None:
            dropped.append((cp, "detail_route"))
            continue
        # Two spellings of one path are one route, and the view file name
        # and the session key are derived from the slug, so both are
        # compared without regard to case.
        view_key = _slug_from_path(path).lower()
        if key in seen_keys:
            dropped.append((cp, "duplicate"))
            continue
        if view_key in seen_views:
            dropped.append((cp, "duplicate_view"))
            continue
        seen_keys.add(key)
        seen_views.add(view_key)
        # Hand the emitters the exact string that was checked above (the
        # raw value may carry surrounding whitespace).
        kept.append(cp if cp.get("path") == path else {**cp, "path": path})
    return kept, dropped


def _custom_pages_filter(rs: ResolvedSpec) -> list:
    """The custom pages this build emits; see `_classify_custom_pages`.

    This is the single choke point for custom-page emission: the routes,
    the views, the sitemap and the navigation all take their pages from
    here, so only plain route paths that no built-in owns ever reach them.
    """
    return _classify_custom_pages(rs)[0]


def _dropped_auth_page_outcome(rs: ResolvedSpec, cp: dict, reason: str) -> Tuple[str, str]:
    """What happens at the path of a dropped page that was marked
    auth_required. Returns `(outcome, text)`:

      - `("public", who)`: something else answers that path WITHOUT a
        login; `who` names it. The build is refused.
      - `("guarded", why)`: the path is answered, but only behind a login
        of its own. Nothing to report.
      - `("absent", why)`: no page was emitted and nothing public answers
        the path. Reported as a warning: the spec asked for a protected
        page and does not get one.
    """
    path = _text(cp.get("path"))
    key = _path_key(path) if path else ""
    if reason == "page_exists":
        return ("public", "a page of the site")
    if reason == "model_listing":
        return ("public", "a model's public listing")
    if reason == "engine_route":
        if key in _LOGIN_GUARDED_BUILTIN_PATHS or key.startswith("/admin/"):
            return ("guarded", "a built-in route that asks for a login itself answers there")
        return ("public", "a built-in public route")
    if reason == "detail_route":
        owner, is_public = _earlier_route_owner(rs, key)
        if is_public:
            return ("public", owner)
        return ("absent", f"its path belongs to {owner}, which answer visitors with a 404")
    if reason == "model_route":
        return ("absent", "its path is the route of a model whose rows are private, "
                          "which answers visitors with a 404")
    if reason == "parametric":
        # "<base>/:param" is exactly the shape of a detail route. When rows
        # are served publicly there, the spec asked for a login on pages the
        # build serves to everyone.
        base, _, last = path.rstrip("/").rpartition("/")
        if last.startswith(":") and ":" not in base:
            # Ask who answers one concrete path of that shape.
            found = _earlier_route_owner(rs, _path_key(base or "/").rstrip("/") + "/1")
            if found and found[1]:
                return ("public", found[0])
        return ("absent", "the engine does not emit pages whose path has a route parameter")
    if reason == "not_plain":
        return ("absent", "its path is not a plain route path")
    if reason in ("duplicate", "duplicate_view"):
        if reason == "duplicate":
            for earlier in _classify_custom_pages(rs)[0]:
                if _path_key(earlier["path"]) == key:
                    if earlier.get("auth_required"):
                        return ("guarded", "an earlier custom page with the same path is "
                                           "auth_required too; its access rule applies")
                    return ("public", "an earlier custom page with the same path, which is public")
        return ("absent", "it would use the same view file as an earlier custom page")
    return ("absent", "the page was not emitted")


def _auth_required_page_errors(rs: ResolvedSpec) -> List[str]:
    """Reasons to refuse the build: every custom page marked auth_required
    whose path would be answered without a login by something else.

    Refused, not warned about, because a build is deployed to a public
    address as soon as it finishes: a warning in the build report is read,
    if at all, after the page is already public. The spec contradicts
    itself (one part says the path needs a login, another part publishes
    it) and the engine does not pick a side.
    """
    errors: List[str] = []
    for cp, reason in _classify_custom_pages(rs)[1]:
        if not cp.get("auth_required"):
            continue
        outcome, who = _dropped_auth_page_outcome(rs, cp, reason)
        if outcome == "public":
            errors.append(
                f"custom page {_text(cp.get('path'))[:80]!r} is marked auth_required, but "
                f"that path is answered without a login by {who}, so the page cannot be "
                "protected (drop auth_required, or give the page a path of its own)"
            )
    return errors


def _pages_named_like_models(rs: ResolvedSpec) -> List[Tuple[dict, dict]]:
    """The emitted public custom pages whose path is the name of a model of
    this build, each with that model: `[(page, model), ...]`.

    A one-segment path is "named like" a model when it is the model's slug
    (/clients beside Client), its singular (/client), or the first word of
    a slug of several words (/blog beside blog-posts) when exactly one model
    starts with that word. A spec written with such a page may have meant a
    listing of the model. The page is not one (nothing is published by
    name), so `_custom_page_warnings` reports each of them.

    Left out: form pages (form_target says what the page is for),
    auth_required pages, and models the spec lists publicly itself.
    """
    by_name: Dict[str, dict] = {}
    by_first_word: Dict[str, list] = {}
    for m in rs.data_models:
        if not isinstance(m, dict):
            continue
        slug = _slug_for(m).lower()
        by_name.setdefault(slug, m)
        by_name.setdefault(_singular_slug(m["name"]), m)
        first_word = slug.split("-", 1)[0]
        if first_word != slug:
            by_first_word.setdefault(first_word, []).append(m)
    named: List[Tuple[dict, dict]] = []
    for cp in _custom_pages_filter(rs):
        if cp.get("form_target") or cp.get("auth_required"):
            continue
        name = _path_key(cp["path"])[1:]
        model = by_name.get(name)
        if model is None and len(by_first_word.get(name, [])) == 1:
            model = by_first_word[name][0]
        if model is None or _public_model_mount(model)[1]:
            continue
        named.append((cp, model))
    return named


def _custom_page_warnings(rs: ResolvedSpec) -> List[Dict[str, str]]:
    """Build warnings for the dropped custom pages a reviewer should know
    about.

    A public page that collides with a page of the site or a built-in route
    stays silent, as it always did (a v1 spec lists /login and /about as a
    matter of course). Reported:

      - a duplicate, and a public page under /admin;
      - a page whose path a model's public listing, a model's routes or a
        detail route answer first (`custom_page_route_taken_dropped`);
      - every dropped page that was marked auth_required and is not served
        behind some other login (`auth_required_page_not_emitted`). The
        case where its path would be PUBLIC never gets this far:
        `_auth_required_page_errors` refuses the build;
      - every emitted public page that is named like a model
        (`page_named_like_model`; see `_pages_named_like_models`).
    """
    warnings: List[Dict[str, str]] = []
    for cp, model in _pages_named_like_models(rs):
        warnings.append({
            "reason": "page_named_like_model",
            "detail": (
                f"custom page {_text(cp.get('path'))[:80]!r} is named like the model "
                f"{model.get('name')!r}. A page does not list a model's rows because of its "
                "name: it was emitted as an ordinary page and shows no rows. To list them "
                f"there, set public_listing_path on {model.get('name')} in the spec"
            ),
        })
    for cp, reason in _classify_custom_pages(rs)[1]:
        path = _text(cp.get("path"))[:80]
        if reason in ("duplicate", "duplicate_view"):
            warnings.append({
                "reason": "custom_page_duplicate_dropped",
                "detail": (
                    f"custom page {path!r} has the same path or view file as "
                    "an earlier custom page; the first one was emitted and "
                    "this one was not"
                ),
            })
        elif reason == "public_under_admin":
            warnings.append({
                "reason": "custom_page_under_admin_dropped",
                "detail": (
                    f"custom page {path!r} is under /admin but is not "
                    "auth_required; the engine owns /admin, so the page was "
                    "not emitted"
                ),
            })
        elif reason in ("model_route", "detail_route", "model_listing"):
            if reason == "detail_route":
                owner = _earlier_route_owner(rs, _path_key(path))[0]
            elif reason == "model_route":
                owner = "the route of a model whose rows are private"
            else:
                owner = "a model's public listing"
            warnings.append({
                "reason": "custom_page_route_taken_dropped",
                "detail": (
                    f"custom page {path!r} was not emitted: its path is "
                    f"answered first by {owner}"
                ),
            })
        if cp.get("auth_required"):
            outcome, why = _dropped_auth_page_outcome(rs, cp, reason)
            if outcome == "absent":
                warnings.append({
                    "reason": "auth_required_page_not_emitted",
                    "detail": (
                        f"custom page {path!r} is marked auth_required but was not "
                        f"emitted ({why}); no protected page exists at that path"
                    ),
                })
    return warnings


# --- Helpers for public submission forms -------------------------------

# System columns the translator must never expose on a public form. Even
# if the customer's data_model declares them, we strip them from the
# submittable field set. Everything else is allowed through unless the
# spec page carries an explicit `allowed_fields` allowlist.
_PUBLIC_FORM_SYSTEM_FIELDS = frozenset({
    "id", "created_at", "updated_at", "approved", "admin_notes",
    "status", "published", "published_at", "reviewed_at", "reviewed_by",
})

# Field types that are inappropriate for an unauthenticated public
# submission form. Image upload is intentionally excluded; public upload
# needs dedicated rate-limiting and virus-scan infra we do not emit yet.
_PUBLIC_FORM_BANNED_FIELD_TYPES = frozenset({
    "image", "password", "color_hex",
})


def _find_data_model(rs: ResolvedSpec, model_name: str):
    """Return the data_model dict matching model_name (case-insensitive
    on name, plural, or slug) or None. Used to wire a custom page's
    `form_target` hint to the actual model the form should INSERT into."""
    if not model_name:
        return None
    needle = model_name.strip().lower()
    for m in rs.data_models:
        if not isinstance(m, dict):
            continue
        candidates = {
            (m.get("name") or "").lower(),
            (m.get("plural") or "").lower(),
            (m.get("slug") or "").lower(),
        }
        if needle in candidates:
            return m
    return None


def _public_form_fields(cp: dict, model: dict) -> list:
    """Compute the submittable field list for a form bound to `model`.

    Filter order: drop system fields; drop banned field_types; apply
    explicit `cp.allowed_fields` allowlist if present; preserve the
    model's original field order otherwise. (A page that visitors can open
    without a login must carry the allowlist, `_public_form_field_errors`;
    the default of "every column" is for the built-in contact form, whose
    model holds what visitors send, and for pages behind a login.) Returns a list of dicts
    shaped for the public-submission widget ({name, field_type, label,
    required, help?, options?}).
    """
    model_fields = model.get("fields") or []
    allowlist = cp.get("allowed_fields") or None
    out = []
    for f in model_fields:
        if not isinstance(f, dict):
            continue
        name = (f.get("name") or "").strip()
        # Compared without regard to letter case, as Postgres compares the
        # column: a field spelled "Published" is the published column, and
        # must not become a checkbox a visitor can tick.
        if not name or name.lower() in _PUBLIC_FORM_SYSTEM_FIELDS:
            continue
        ftype = (f.get("field_type") or "").strip()
        if ftype in _PUBLIC_FORM_BANNED_FIELD_TYPES:
            continue
        if allowlist is not None and name not in allowlist:
            continue
        # The field type the input is rendered with AND the POST handler
        # validates against: the spec's own, else the one its name implies
        # (the same inference the admin forms use, so `email` and `phone`
        # are checked as such), else one derived from the column type.
        resolved_ft = ftype or _infer_field_type(f) or _default_widget_field_type(f)
        row = {
            "name": name,
            "field_type": resolved_ft,
            "label": f.get("label") or _prettify(name),
            "required": bool(f.get("required")),
        }
        if f.get("help"):
            row["help"] = f["help"]
        choices = _select_choices(f)
        if choices:
            row["options"] = choices
        if f.get("placeholder"):
            row["placeholder"] = f["placeholder"]
        if resolved_ft == "long_text":
            row["rows"] = int(f.get("rows") or 5)
        out.append(row)
    return out


def _default_widget_field_type(f: dict) -> str:
    """Best-effort widget field_type for a field without an explicit one.
    Used so public forms still render sensibly from a v2-shape spec that
    only carries sql_type."""
    sql = (f.get("sql_type") or "").strip().upper()
    if sql.endswith("[]"):
        return "text"
    if sql.startswith("TEXT"):
        return "long_text"
    if sql.startswith("BOOL"):
        return "boolean"
    if sql.startswith(("INT", "BIGINT", "SMALLINT")):
        return "integer"
    if sql.startswith(("NUMERIC", "DECIMAL")):
        # Two decimal places at most (the registry's pattern), which is
        # what a NUMERIC(10,2) price column holds.
        return "decimal"
    if sql.startswith("DATE"):
        return "date"
    if sql.startswith("TIMESTAMP"):
        return "datetime"
    if sql.startswith("TIME"):
        return "time"
    return "text"


# No value a visitor submits through a public form may be longer than this,
# whatever its column type (a TEXT column has no limit of its own).
_PUBLIC_FIELD_MAX_LENGTH = 10000

_SQL_WIDTH_RE = re.compile(r"(?:varchar|char)\s*\(\s*(\d{1,4})\s*\)", re.ASCII)
_SQL_COLUMN_KINDS = (
    # (prefixes of the lower-cased column type, kind, integer width in bits)
    (("smallint",), "integer", 16),
    (("integer", "int", "serial"), "integer", 32),
    (("bigint", "bigserial"), "integer", 64),
    (("numeric", "decimal", "real", "double precision"), "number", None),
    (("boolean", "bool"), "boolean", None),
    (("timestamp",), "timestamp", None),
    (("date",), "date", None),
    (("time",), "time", None),
)


def _public_form_checks(model: dict, fields: list) -> list:
    """What POST handlers of public forms check each submitted field
    against, as the `FIELDS` literal of the handler: one entry per field of
    `fields` (the output of `_public_form_fields`), in order.

      - `field_type`, `required`, `label`: the registry check the admin
        forms use too (processForm in middleware/fields.js: sanitize,
        pattern, registry maxlength).
      - `max_length`: the most characters accepted. The smallest of the
        registry's maxlength for the field type, the width of a VARCHAR(n)
        or CHAR(n) column, and `_PUBLIC_FIELD_MAX_LENGTH`.
      - `column` (and `int_bits`): what the column can hold, from its SQL
        type: "integer" with its range, "number", "boolean", "date",
        "time", "timestamp", or "text".
      - `options`: for a select field, the values it offers.

    All of it is derived from the model definition at build time, so the
    form that is rendered, the checks, and the INSERT name the same columns.
    """
    by_name = {f.get("name"): f for f in _model_fields(model)}
    checks = []
    for field in fields:
        column = by_name.get(field["name"]) or {"name": field["name"], "sql_type": "TEXT"}
        sql_type = _safe_sql_type(column).lower()
        kind, int_bits = "text", None
        if not sql_type.endswith("[]"):
            for prefixes, candidate, bits in _SQL_COLUMN_KINDS:
                if sql_type.startswith(prefixes):
                    kind, int_bits = candidate, bits
                    break
        limits = [_PUBLIC_FIELD_MAX_LENGTH]
        width = _SQL_WIDTH_RE.match(sql_type)
        if width:
            limits.append(int(width.group(1)))
        try:
            registry_max = get_field_type(field["field_type"]).get("maxlength")
        except (ValueError, KeyError, TypeError):
            registry_max = None
        if isinstance(registry_max, int) and registry_max > 0:
            limits.append(registry_max)
        check = {
            "name": field["name"],
            "field_type": field["field_type"],
            "required": bool(field.get("required")),
            "label": field["label"],
            "max_length": min(limits),
            "column": kind,
        }
        if int_bits:
            check["int_bits"] = int_bits
        if field["field_type"] == "select" and isinstance(field.get("options"), list):
            values = []
            for opt in field["options"]:
                value = opt.get("value") if isinstance(opt, dict) else opt
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    values.append(str(value))
            if values:
                check["options"] = values
        checks.append(check)
    return checks


def _fill_template(template: str, parts: Dict[str, str]) -> str:
    """Replace each `__NAME__` token of `template` with `parts[NAME]`, in
    ONE pass: text that is substituted in is never scanned again, so a spec
    value that happens to contain a token cannot pull another part into
    itself. A token with no entry in `parts` is an error."""
    return re.sub(r"__([A-Z][A-Z_]*[A-Z])__", lambda found: parts[found.group(1)], template)


def _public_insert_sql(model: dict, columns: list) -> str:
    """The INSERT a public form runs: `columns` bound as $1..$n, plus, when
    the model has a publication flag (`_publication_column`: a BOOLEAN
    column named `published`), that column set to FALSE.

    A row a visitor creates is never public on its own. Every public read
    filters on `published` (the model's listing and detail pages require
    TRUE, the composed pages leave out FALSE), so a submission stays hidden
    until the owner publishes it in the admin. A model with no such column
    cannot hold a row back; `_public_form_errors` refuses the build when
    such a model is both a form's target and publicly shown.
    """
    names = [_sql_ident(c) for c in columns]
    values = [f"${i}" for i in range(1, len(names) + 1)]
    published = _publication_column(model)
    if published and published.lower() not in {n.lower() for n in names}:
        names.append(_sql_ident(published))
        values.append("FALSE")
    return (
        f"INSERT INTO {_sql_ident(_table_name_for_model(model))} "
        f"({', '.join(names)}) VALUES ({', '.join(values)})"
    )


def _public_insert_targets(rs: ResolvedSpec) -> List[Tuple[str, dict]]:
    """Every model a visitor who is not signed in can add a row to, with a
    description of the form that does it: `[(what, model), ...]`. These are
    all the unauthenticated INSERTs into model tables a build contains:
    the custom form pages that are not auth_required, the built-in contact
    form, and the newsletter signup."""
    targets: List[Tuple[str, dict]] = []
    for cp in _custom_pages_filter(rs):
        model = _find_data_model(rs, cp.get("form_target") or "")
        if model is not None and not cp.get("auth_required"):
            targets.append((f"the form on custom page {cp['path'][:80]!r}", model))
    if _page_with_widget(rs, "form-contact") is not None:
        model = _contact_form_model(rs)
        if model is not None and _contact_form_fields(model):
            targets.append(("the contact form", model))
    if _page_with_widget(rs, "cta-newsletter-inline") is not None:
        model = _newsletter_model(rs)
        if model is not None and _model_column(model, "email"):
            targets.append(("the newsletter signup form", model))
    return targets


def _public_form_field_errors(rs: ResolvedSpec) -> List[str]:
    """Reasons to refuse the build over WHICH columns a visitor may write:
    a custom form page that is not behind a login has to name them
    (`allowed_fields`), and the names have to leave a field a visitor may
    fill in (`_public_form_fields`).

    Without a list the form would take every column of the model that is
    not one of the engine's own (`_PUBLIC_FORM_SYSTEM_FIELDS`): a flag that
    puts the row on the home page, a sort order, a price, whatever the model
    has. A model is declared for what the owner enters, so its columns say
    nothing about what a stranger may set; the spec has to. A page behind a
    login keeps the default, since the person filling it in is signed in.
    """
    errors: List[str] = []
    for cp in _custom_pages_filter(rs):
        if cp.get("auth_required") or not cp.get("form_target"):
            continue
        model = _find_data_model(rs, cp.get("form_target") or "")
        if model is None:
            continue
        where = f"the form on custom page {_text(cp.get('path'))[:80]!r}"
        listed = cp.get("allowed_fields")
        if not isinstance(listed, list) or not listed:
            errors.append(
                f"{where} lets visitors add rows to the model {model.get('name')!r} but the page names no "
                "allowed_fields; list the columns a visitor may fill in (without a list every column of the "
                "model that is not the engine's own would be one)"
            )
        elif not _public_form_fields(cp, model):
            errors.append(
                f"{where} lists allowed_fields that name no column of {model.get('name')!r} a visitor may "
                "fill in (the engine's own columns, such as id, created_at and published, and image or "
                "password fields are never offered)"
            )
    return errors


def _public_form_errors(rs: ResolvedSpec) -> List[str]:
    """Reasons to refuse the build: every public form that stores into a
    model whose rows are shown to visitors (`_public_row_sources`) when the
    model has no publication flag (`_publication_column`: a BOOLEAN column
    named `published`) to hold a new row back, and every public form page
    that does not say which columns a visitor may write
    (`_public_form_field_errors`).

    With the column, `_public_insert_sql` stores each submission as
    unpublished and nothing a visitor sends is shown until the owner
    publishes it. Without it there is no such state: whatever a visitor
    typed would be on the public site at once. The spec asks for both and
    the engine does not pick one.
    """
    public = _public_row_sources(rs)
    errors: List[str] = []
    for what, model in _public_insert_targets(rs):
        shown = public.get(_table_name_for_model(model))
        if shown and not _publication_column(model):
            name = model.get("name")
            errors.append(
                f"{what} lets a visitor add a row to the model {name!r}, whose rows are "
                f"shown to visitors ({'; '.join(shown)}), and {name} has no boolean `published` "
                "column to hold a submission back until the owner approves it (add a "
                f"boolean `published` field to {name}, or do not show its rows publicly)"
            )
    return errors + _public_form_field_errors(rs)


def _table_name_for_model(model: dict) -> str:
    """Compose's schema emits the table name by slug→snake. Keep this
    in one place so emit_schema_sql + emit_routes_pages_js agree."""
    return _slug_for(model).replace("-", "_")


def _sql_ident(name: Any) -> str:
    """Return `name` unchanged when it is a plain SQL identifier; raise
    otherwise.

    The emit-side restatement of the gate's rule for field names
    (`_FIELD_NAME_RE`; a table name, being a slug with "_" for "-", fits the
    same pattern). Used where a table or column name taken from a model
    definition is written into the SQL text of a generated route, so that
    text does not depend on who built the ResolvedSpec.
    """
    if not (isinstance(name, str) and _FIELD_NAME_RE.fullmatch(name)):
        raise ValueError(f"Spec validation failed: not a plain SQL identifier: {name!r}")
    return name


def _model_fields(model: dict) -> list:
    """The field definitions a model's table is built from: the spec's own
    list, or the engine's defaults for that model name."""
    return [f for f in (model.get("fields") or _default_fields(model["name"])) if isinstance(f, dict)]


def _model_column(model: dict, column: str) -> Optional[str]:
    """The model's own spelling of `column` when it has that column
    (compared without regard to letter case, as Postgres does), else None."""
    for f in _model_fields(model):
        name = f.get("name")
        if isinstance(name, str) and name.lower() == column.lower():
            return name
    return None


def _publication_column(model: dict) -> Optional[str]:
    """The model's own spelling of its publication flag, or None.

    The flag is a column named `published` (compared without regard to
    letter case) whose type is BOOLEAN. A model may give that name to some
    other kind of column (the date a book came out). That is an ordinary
    column: every public read compares the flag with TRUE or FALSE, which
    Postgres rejects for a DATE or a TEXT column, so the page would answer
    500 on every request. Such a model has no flag, its rows cannot be held
    back, and everything that asks "can a row of this model be hidden" asks
    this function.
    """
    name = _model_column(model, "published")
    if name is None:
        return None
    field = next(
        f for f in _model_fields(model)
        if isinstance(f.get("name"), str) and f["name"].lower() == "published"
    )
    return name if _field_type_kind(field) == "boolean" else None


def _published_condition(column: str) -> str:
    """The SQL condition every public read of a row applies, as the one
    definition of "published": the model's flag column is TRUE. A row whose
    flag is FALSE is not published, and neither is a row whose flag has no
    value (NULL): a row is public because someone published it, never
    because nobody said otherwise. The model's listing and detail routes,
    the composed pages that list its rows and their detail pages, and the
    rows related to a record all ask this one function."""
    return f"{_sql_ident(column)} = TRUE"


# Timestamps a row carries about itself. They are not what a listing of the
# model is "about", so they never count as its date column.
_BOOKKEEPING_DATE_COLUMNS = frozenset({"created_at", "updated_at", "published_at"})


def _date_column(model: dict) -> Optional[str]:
    """The column a model's rows are dated by: its first DATE or TIMESTAMP
    column in declaration order (an event's `event_date`, a class's
    `class_date`), skipping the bookkeeping timestamps. None when the model
    has no such column."""
    for f in _model_fields(model):
        name = f.get("name")
        if not isinstance(name, str) or name.lower() in _BOOKKEEPING_DATE_COLUMNS:
            continue
        sql_type = str(_sql_type_for(f)).strip().upper()
        if sql_type == "DATE" or sql_type.startswith("TIMESTAMP"):
            return name
    return None


def _listing_order_by(model: dict) -> str:
    """Default ORDER BY for a model's listing, derived from its columns and
    from nothing else (in particular not from the model's name):

      - a `published_at` column: newest publication first;
      - otherwise a date column (`_date_column`): in date order, the way a
        calendar of events, sessions or classes reads;
      - otherwise: newest row first.

    `id` breaks ties so paging through LIMIT / OFFSET never repeats or
    skips a row.
    """
    published_at = _model_column(model, "published_at")
    if published_at:
        return f"ORDER BY {_sql_ident(published_at)} DESC NULLS LAST, id DESC"
    date_col = _date_column(model)
    if date_col:
        return f"ORDER BY {_sql_ident(date_col)} ASC NULLS LAST, id ASC"
    return "ORDER BY id DESC"


def _contact_form_model(rs: ResolvedSpec) -> Optional[dict]:
    """The model the built-in contact form stores into, or None.

    The form is the `form-contact` widget, which posts to /contact. Its
    model is the one named ContactInquiry: every archetype declares it and a
    spec may redefine it with fields of its own. A build without that model
    (a spec can exclude it) falls back to the first submission-target model
    whose name mentions "contact" or "inquir" (ContactMessage, Inquiry).
    """
    fallback = None
    for m in rs.data_models:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or "").lower()
        if name == "contactinquiry":
            return m
        if fallback is None and _is_submission_target_model(m) and (
            "contact" in name or "inquir" in name
        ):
            fallback = m
    return fallback


def _contact_form_fields(model: Optional[dict]) -> list:
    """The fields of the built-in contact form, taken from its model: one
    entry per column a visitor may fill in ({name, field_type, label,
    required, ...}; see `_public_form_fields` for what is left out). The
    contact view renders exactly these inputs and POST /contact validates
    and inserts exactly these columns, so the form, the handler and the
    table cannot disagree. Empty when there is no model."""
    if model is None:
        return []
    typed = dict(model)
    typed["fields"] = [
        # Same resolution the admin CRUD uses, so `email` and `phone` get
        # their validation even when the model only declares a sql_type.
        {**f, "field_type": f.get("field_type") or _infer_field_type(f) or ""}
        for f in _model_fields(model)
    ]
    return _public_form_fields({}, typed)


def _page_with_widget(rs: ResolvedSpec, widget_id: str) -> Optional[str]:
    """Id of the first page of this build whose layout includes
    `widget_id`, or None when no page does."""
    for page_id, layout in rs.page_layouts.items():
        for w in (layout.get("widgets") or []):
            if isinstance(w, dict) and w.get("id") == widget_id:
                return page_id
    return None


def _note_build_warning(rs: ResolvedSpec, reason: str, detail: str) -> None:
    """Record an engine warning once (emitters can run more than once on one
    ResolvedSpec)."""
    _engine_warning(rs.spec, reason, detail)


def _contact_post_handler_js(rs: ResolvedSpec) -> str:
    """Source of the `POST /contact` handler, or "" when no page of this
    build shows the contact form.

    The validated fields and the INSERT statement are written from the
    contact model's definition (`_contact_form_fields`), the same list the
    contact view renders its inputs from. Table and column names are spec
    identifiers the gate has checked; `_sql_ident` restates that here.
    """
    page_id = _page_with_widget(rs, "form-contact")
    if page_id is None:
        return ""
    model = _contact_form_model(rs)
    fields = _contact_form_fields(model)
    if not fields:
        _note_build_warning(
            rs, "contact_form_without_model",
            f"page {page_id!r} shows the contact form, but the build has no contact "
            "model to store a message in (ContactInquiry was excluded and no other "
            "contact or inquiry model exists); the form answers that it is unavailable",
        )
        return '''router.post('/contact', formLimiter, validateCsrfToken, (req, res) => {
  // This build has no contact model, so a message cannot be stored. Say so
  // rather than answer as if it had been.
  console.error('[contact] message not stored: this build has no contact model');
  res.status(503).render('__VIEW__', {
    title: 'Contact',
    layout: 'layout-public',
    preselected_service: '',
    flashError: 'This form is not available right now. Please use the contact details on this page.'
  });
});'''.replace("__VIEW__", page_id)
    return _fill_template("""router.post('/contact', formLimiter, validateCsrfToken, async (req, res) => {
  try {
    const { processPublicForm } = require('../middleware/fields');
    // The contact model's visitor-facing columns, from the spec, each with
    // the checks a value must pass before anything is stored.
    const FIELDS = __FIELDS__;
    const { values, errors, submitted } = processPublicForm(FIELDS, req.body);
    const preselected_service = (submitted.service_interest || '').trim();
    if (Object.keys(errors).length > 0) {
      // Hand back what was typed so a rejected form comes back filled in.
      return res.render('__VIEW__', {
        title: 'Contact',
        layout: 'layout-public',
        preselected_service,
        submitted,
        flashError: Object.values(errors).join(' __MIDDOT__ ')
      });
    }
    await db.query(
      '__INSERT_SQL__',
      FIELDS.map(f => (values[f.name] === undefined || values[f.name] === '') ? null : values[f.name])
    );
    res.render('__VIEW__', {
      title: 'Thanks',
      layout: 'layout-public',
      preselected_service,
      flashSuccess: 'Thank you for reaching out! We will get back to you soon.'
    });
  } catch (e) {
    console.error(e);
    res.status(500).render('error-500', { title: 'Error' });
  }
});""", {
        # json.dumps output is ASCII-only, so it is a valid JavaScript
        # literal whatever a spec-supplied label contains.
        "FIELDS": json.dumps(_public_form_checks(model, fields)),
        "INSERT_SQL": _public_insert_sql(model, [f["name"] for f in fields]),
        "VIEW": page_id,
        "MIDDOT": chr(0x00B7),
    })


def _newsletter_model(rs: ResolvedSpec) -> Optional[dict]:
    """The model the newsletter signup form stores into: the one named
    NewsletterSubscriber, else the first model with "subscriber" in its
    name. None when the build has neither."""
    fallback = None
    for m in rs.data_models:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or "").lower()
        if name == "newslettersubscriber":
            return m
        if fallback is None and "subscriber" in name:
            fallback = m
    return fallback


def _newsletter_post_handler_js(rs: ResolvedSpec) -> str:
    """Source of the `POST /newsletter-signup` handler, or "" when no page
    of this build shows the signup form (the cta-newsletter-inline widget).
    The table and the email column come from the subscriber model."""
    page_id = _page_with_widget(rs, "cta-newsletter-inline")
    if page_id is None:
        return ""
    model = _newsletter_model(rs)
    email_col = _model_column(model, "email") if model is not None else None
    if not email_col:
        _note_build_warning(
            rs, "newsletter_form_without_model",
            f"page {page_id!r} shows the newsletter signup form, but the build has no "
            "subscriber model with an email column; signups are not stored",
        )
        return '''router.post('/newsletter-signup', formLimiter, validateCsrfToken, (req, res) => {
  // This build has no subscriber model with an email column, so there is
  // nowhere to store the address.
  console.error('[newsletter] signup not stored: this build has no subscriber model');
  res.redirect('/');
});'''
    # One row per address. The subscriber model is the spec's and its email
    # column has no unique constraint (ON CONFLICT would have nothing to
    # conflict on), so the statement itself looks for the address first.
    # Stored unpublished when the model has a boolean `published` column, like every
    # row a visitor creates (`_public_insert_sql`).
    table = _sql_ident(_table_name_for_model(model))
    email = _sql_ident(email_col)
    published = _publication_column(model)
    insert_sql = (
        f"INSERT INTO {table} ({email}{', ' + _sql_ident(published) if published else ''}) "
        f"SELECT $1::text{', FALSE' if published else ''} "
        f"WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE LOWER({email}::text) = LOWER($1::text))"
    )
    return _fill_template("""router.post('/newsletter-signup', formLimiter, validateCsrfToken, async (req, res) => {
  try {
    // Same sanitize + pattern check the admin CRUD applies to an email
    // column; anything that is not one plausible address is ignored.
    const { sanitizeField, validateField } = require('../middleware/fields');
    const raw = (req.body && typeof req.body.email === 'string') ? req.body.email : '';
    const email = sanitizeField('email', raw);
    if (email && validateField('email', email, { required: true }).ok) {
      // An address that is already stored is not stored again. The column
      // has no unique constraint, so two signups with one address in the
      // same instant can still both be stored.
      await db.query('__INSERT_SQL__', [email]);
    }
    res.redirect('/#newsletter-success');
  } catch (e) {
    console.error(e);
    res.redirect('/');
  }
});""", {"INSERT_SQL": insert_sql})


def _public_listing_kind(model: dict) -> str:
    """Which public list view a model with a `public_listing_path` gets:
    "photos" (a gallery), "reviews" (cards with stars) or "cards" (the
    default article list). `emit_admin_crud_views` writes the view and
    `_public_sort_columns` asks which columns it shows."""
    if model.get("name") == "Photo" or _slug_for(model) == "photos":
        return "photos"
    if model.get("name") == "Review" or _slug_for(model) == "reviews":
        return "reviews"
    return "cards"


# The columns each public list view prints, by view kind (the fields the
# views of `emit_admin_crud_views` and the widgets they include read). A
# column that is printed only in part (a summary cut at 200 characters, a
# URL) or not at all is not listed: ordering by it would tell a visitor
# something the page does not show.
_PUBLIC_LIST_COLUMNS = {
    "cards": ("title", "name", "class_name", "city", "author_name",
              "event_date", "event_time", "published_at", "created_at"),
    "reviews": ("author_name", "rating", "service", "body", "review_date", "created_at"),
    "photos": ("title", "caption"),
}


def _sortable(field: dict) -> bool:
    """True when a column can be ordered by: every type but json (which has
    no ordering operator, so ORDER BY on it is an error)."""
    return _field_type_kind(field) != "json"


def _admin_sort_columns(model: dict, extra: Optional[list] = None) -> list:
    """Columns the admin's list of a model may be sorted by (?sort=): the
    model's own columns that can be ordered, the key and the creation time.
    The admin sees every column of every row, so nothing is held back."""
    names = [
        f["name"] for f in _model_fields(model)
        if isinstance(f.get("name"), str) and _sortable(f)
    ] + list(extra or [])
    for engine_column in _ENGINE_COLUMNS:
        if engine_column not in names:
            names.append(engine_column)
    return names


def _public_sort_columns(model: dict) -> list:
    """Columns a visitor may sort the public list of a model by (?sort=):
    only columns the model declares AND the public list view prints in full
    (`_PUBLIC_LIST_COLUMNS`), plus the key (it is the link of every row) and
    the creation time when the view prints a date. A request for any other
    column sorts like a request for none: the default order. Without this a
    visitor could order the rows by a column the page never shows (an
    unpublished price, a private note) and read the order off the list."""
    printed = _PUBLIC_LIST_COLUMNS[_public_listing_kind(model)]
    declared = {
        f["name"].lower(): f["name"] for f in _model_fields(model)
        if isinstance(f.get("name"), str) and _sortable(f)
    }
    names = [declared[c] for c in printed if c in declared]
    # created_at is a column of every table; print-listed views show it
    # as the date of a row.
    if "created_at" in printed and "created_at" not in declared:
        names.append("created_at")
    return ["id"] + [n for n in names if n != "id"]


def emit_routes_resource_js(rs: ResolvedSpec, model: dict) -> tuple:
    """Per-resource CRUD router: MW v25 dual-mount pattern.

    Router is mounted at BOTH its public mount (`_public_model_mount`) and
    /admin/<slug> (requireAdmin-guarded, by server.js middleware). The
    router itself branches every render on
    `req.baseUrl.startsWith('/admin/')`.

    The two public routes serve rows ONLY when the spec set
    public_listing_path on the model. Without it the public mount reads no
    row: GET / answers 404 (or hands the path to a page of the site that
    lives there) and GET /:id answers 404 before its query.

    - GET <mount>/ → public listing view (views/<slug>/index.ejs)
    - GET /admin/<slug>/ → admin manage view (views/<slug>/manage.ejs)
    - GET <mount>/:id → public detail view (views/<slug>/detail.ejs)
    - GET /admin/<slug>/:id → admin detail view (views/<slug>/admin-detail.ejs)
    - GET /admin/<slug>/new → admin new form (views/<slug>/new.ejs)
    - GET /admin/<slug>/:id/edit → admin edit form (views/<slug>/edit.ejs)
    - POST / → admin create (only via /admin/<slug> mount)
    - POST /:id/update → admin update
    - POST /:id/delete → admin delete

    Admin-only endpoints return 404 when reached via the bare /<slug>
    mount (the MW v25 convention: bare /<slug>/new doesn't exist for
    public visitors).
    """
    name = model["name"]
    slug = _slug_for(model)
    slug_js = slug.replace("-", "_")
    schema_fields = model.get("fields") or _default_fields(name)
    col_names = [f["name"] for f in schema_fields]
    # When the model belongs_to a parent, the FK column
    # is a real schema column but it is NOT in schema_fields (emit_schema
    # adds it separately). Include it in ALLOWED_COLS so POST / +
    # POST /:id/update accept it; render a <select> in new/edit via a
    # synthetic field row below.
    bt_info = _belongs_to_info(rs, model)
    fk_only = []
    if bt_info is not None:
        _parent_model, fk_col, parent_table, _on_delete = bt_info
        if not _has_column(col_names, fk_col):
            col_names = col_names + [fk_col]
            fk_only = [fk_col]
    allowed_js_list = json.dumps(col_names)
    # What ?sort= may name: the admin's list any orderable column; the
    # public list only the columns it prints (`_public_sort_columns`).
    admin_sort_js = json.dumps(_admin_sort_columns(model, fk_only))
    public_sort_js = json.dumps(_public_sort_columns(model))
    # HTML forms DO NOT submit unchecked checkboxes; req.body[col] is
    # undefined for those cols. The INSERT/UPDATE loops below skip cols
    # whose body value is undefined, so on CREATE the DB DEFAULT fires
    # (typically TRUE for `published` → row saved as published despite
    # the user unchecking it); on UPDATE the existing value persists
    # (un-publishing has no effect). Identify boolean cols at compile
    # time so the emitted JS can FORCE-include them with FALSE when
    # absent from the body. Detection: sql_type='BOOLEAN' OR
    # field_type explicitly set to 'boolean'.
    boolean_col_names = [
        f["name"] for f in schema_fields
        if _field_type_kind(f) == "boolean"
        or (f.get("field_type") or "").lower() == "boolean"
    ]
    boolean_js_list = json.dumps(boolean_col_names)
    # JSONB columns (e.g. Event.photos) arrive in req.body as raw JSON-text
    # from the form textarea; JSON.parse() before binding so pg receives a
    # real object/array. Bad JSON falls back to [] rather than 500ing.
    jsonb_col_names = [
        f["name"] for f in schema_fields
        if (f.get("sql_type") or "").upper() in ("JSONB", "JSON")
    ]
    jsonb_js_list = json.dumps(jsonb_col_names)
    # Field spec passed to the fields.js middleware; carries field_type
    # + required + label for server-side sanitize/validate. Uses the
    # same resolve-with-inference path so legacy models (e.g. a spec that
    # only sets sql_type on `email`) still get server-side email pattern
    # enforcement. Safety net against client-side bypass.
    field_spec_js = json.dumps([
        {
            "name": f["name"],
            "field_type": f.get("field_type") or _infer_field_type(f),
            "required": bool(f.get("required", False)),
            "label": _humanize(f["name"]),
        }
        for f in schema_fields
    ])
    name_singular = name
    resource_name_human = _humanize(name)
    resource_plural_human = _humanize(slug)
    # The model has a public listing only when the spec set
    # public_listing_path on it (`_public_model_mount`). Every public read
    # in this router is behind that one value.
    effective_public_path, has_public = _public_model_mount(model)

    # Default WHERE for public listings, only show published rows when the
    # model has a publication flag (`_publication_column`). Looked up without
    # regard to letter case (Postgres folds the unquoted column name), so a
    # boolean field the spec spells "Published" is still the column that
    # hides a row.
    published_col = _publication_column(model)
    public_where = f"WHERE {_published_condition(published_col)}" if published_col else ""
    public_and = f" AND {_published_condition(published_col)}" if published_col else ""
    # Default order of the listing, from the model's own columns (a date
    # column sorts chronologically whatever the model is called).
    order_by = _listing_order_by(model)

    # If the public mount URL collides with an
    # archetype default page, the router's GET / handler must yield via
    # next() so the pages router (mounted last) can render the
    # archetype's composed view. This preserves GET /:id detail on the
    # same public mount while handing the list URL to the archetype
    # page. Covers both explicit public_listing_path collisions
    # (e.g. fitness-studio ScheduleSlot.public_listing_path=/schedule)
    # and default /<plural> collisions (e.g. healer Event → /events).
    arch_paths = _archetype_default_paths(rs.archetype, rs.page_layouts)
    collides_with_archetype_page = effective_public_path in arch_paths
    collides_js = "true" if collides_with_archetype_page else "false"

    # What a visitor gets at the mount path when the model has no public
    # listing (has_public is False):
    #
    # - a custom page of this build lives at that path: the router hands
    #   the request on (next()) and routes/pages.js serves the page. The
    #   page is the spec's own content; it shows no rows of this model.
    # - otherwise: 404.
    #
    # The custom page at this mount path, if one is emitted. Asked of the
    # pages that survive `_classify_custom_pages` and compared the way that
    # function compares paths (and Express matches them), so "/Sessions/"
    # and "/sessions" are the same page here too. For a submission-target
    # model the only page that survives there is a form (form_target set):
    # the router yields to it, and keeps its 404 in every other case.
    mount_key = _path_key(effective_public_path)
    keeper_page_at_path = any(
        _path_key(cp["path"]) == mount_key for cp in _custom_pages_filter(rs)
    )
    # A custom page at this model's mount path may be a form, and a form
    # posts to its own path. On the public mount POST / is never this
    # router's (the model has no public write), so it hands that request on
    # to routes/pages.js instead of answering 404 in front of the form.
    yields_public_post_js = "true" if (keeper_page_at_path and not has_public) else "false"
    if not has_public:
        if keeper_page_at_path:
            # Fall through so the pages router can serve the custom page.
            public_gate = f'''if (!req.baseUrl.startsWith('/admin/')) {{
      return next();
    }}'''
        else:
            public_gate = f'''if (!req.baseUrl.startsWith('/admin/')) {{
      return res.status(404).render('error-404', {{ title: '404', layout: 'layout-public' }});
    }}'''
        # No row is read for a visitor: the detail route answers 404
        # before its query runs.
        public_detail_gate = '''if (!isAdmin) {
      return res.status(404).render('error-404', { title: '404', layout: 'layout-public' });
    }'''
        # The gate above has already answered every visitor. The router of
        # a model with no public listing has no public view to render (none
        # is emitted), so the end of the handler is a 404 as well.
        public_listing_render = (
            "res.status(404).render('error-404', { title: '404', layout: 'layout-public' });"
        )
    else:
        public_gate = ""
        public_detail_gate = ""
        public_listing_render = _fill_template("""res.render('__SLUG__/index', {
      title: '__TITLE__',
      layout: 'layout-public',
      rows,
      pagination,
      flashSuccess,
      flashError
    });""", {"SLUG": slug, "TITLE": resource_plural_human})

    # Admin POST/edit/delete: refuse on public mount
    admin_only_gate = '''if (!req.baseUrl.startsWith('/admin/')) {
      return res.status(404).render('error-404', { title: '404', layout: 'layout-public' });
    }'''

    contents = f'''const express = require('express');
const router = express.Router();
const db = require('../db');
const {{ requireAuth, requireRole, validateCsrfToken }} = require('../middleware/auth');
const {{ processForm }} = require('../middleware/fields');

const ALLOWED_COLS = {allowed_js_list};
// Columns the list may be sorted by (?sort=col&order=asc|desc). The admin's
// list takes any orderable column of the model. The public list takes only
// the columns it prints: a visitor must not be able to read the order of
// rows by a column the page never shows.
const ADMIN_SORT_COLS = new Set({admin_sort_js});
const PUBLIC_SORT_COLS = new Set({public_sort_js});
// Boolean columns: forms must coerce missing checkboxes to FALSE rather
// than letting the col fall through (which on CREATE fires DB default and
// on UPDATE preserves the prior value).
const BOOLEAN_COLS = new Set({boolean_js_list});
// JSONB columns: form posts JSON text; JSON.parse() before binding so
// pg receives a real object/array. Bad JSON falls back to [].
const JSONB_COLS = new Set({jsonb_js_list});
const FIELD_SPEC = {field_spec_js};

// When true, GET / on the public mount yields to
// next() so the pages router's archetype composed page handler serves
// the URL (prevents CRUD card list from shadowing archetype composed
// views like fitness-studio's /schedule weekly grid).
const COLLIDES_WITH_ARCHETYPE_PAGE = {collides_js};

// True when a custom page lives at this router's public mount path and the
// model has no public listing there. That page's form posts to the same
// path, so a public POST / is passed on to routes/pages.js (see
// yieldPublicPost) rather than answered with this router's 404.
const CUSTOM_PAGE_AT_PUBLIC_MOUNT = {yields_public_post_js};

function yieldPublicPost(req, res, next) {{
  if (CUSTOM_PAGE_AT_PUBLIC_MOUNT && !req.baseUrl.startsWith('/admin/')) return next('router');
  next();
}}

// This router is mounted twice by server.js: at the public path, and at
// /admin/{slug} behind requireAdmin. Every handler below decides "admin or
// public" from req.baseUrl, so the admin branch must never depend on the
// mount alone being correct. Re-check the session here: a request that
// arrives under /admin/ without an admin session is turned away even if
// this router were ever mounted there without requireAdmin.
router.use((req, res, next) => {{
  if (req.baseUrl.startsWith('/admin/') && !(req.session && req.session.userId && req.session.role === 'admin')) {{
    return res.redirect('/login');
  }}
  next();
}});

// GET /: list. Branches on req.baseUrl. Admin view supports
// table sort (?sort=col&order=asc|desc) + pagination (?page=N&per_page=N);
// column whitelist prevents SQL injection on the ORDER BY.
router.get('/', async (req, res, next) => {{
  try {{
    if (!req.baseUrl.startsWith('/admin/') && COLLIDES_WITH_ARCHETYPE_PAGE) {{
      return next();
    }}
    {public_gate}
    const isAdmin = req.baseUrl.startsWith('/admin/');
    const where = isAdmin ? '' : '{public_where}';
    // Sort: reject anything not in the allow-list to keep SQL safe, and to
    // keep a visitor from ordering by a column the public list does not show.
    const allowedSort = isAdmin ? ADMIN_SORT_COLS : PUBLIC_SORT_COLS;
    let sortCol = String(req.query.sort || '').trim();
    if (!allowedSort.has(sortCol)) sortCol = '';
    const order = (String(req.query.order || 'asc').toLowerCase() === 'desc') ? 'DESC' : 'ASC';
    // Pagination: per_page capped to keep queries cheap. Admin default 25,
    // public default 20. Bad inputs fall back to defaults.
    const defaultPerPage = isAdmin ? 25 : 20;
    let perPage = parseInt(String(req.query.per_page || defaultPerPage), 10);
    if (!Number.isFinite(perPage) || perPage < 1 || perPage > 100) perPage = defaultPerPage;
    let page = parseInt(String(req.query.page || 1), 10);
    if (!Number.isFinite(page) || page < 1) page = 1;
    const offset = (page - 1) * perPage;
    // Assemble the ORDER BY + LIMIT clauses. When no explicit sort,
    // use the archetype-default ordering.
    const orderBy = sortCol ? `ORDER BY ${{sortCol}} ${{order}}` : '{order_by}';
    const countRes = await db.query(
      'SELECT COUNT(*)::int AS total FROM {slug_js} ' + where
    );
    const total = countRes.rows[0].total;
    const {{ rows }} = await db.query(
      'SELECT * FROM {slug_js} ' + where + ' ' + orderBy + ' LIMIT $1 OFFSET $2',
      [perPage, offset]
    );
    const totalPages = Math.max(1, Math.ceil(total / perPage));
    const pagination = {{
      page,
      per_page: perPage,
      total,
      total_pages: totalPages,
      has_prev: page > 1,
      has_next: page < totalPages,
      sort: sortCol || null,
      order: sortCol ? order.toLowerCase() : null,
      base_path: (isAdmin ? '/admin/{slug}' : req.baseUrl || '')
    }};
    const flashSuccess = req.session.flash_success;
    const flashError = req.session.flash_error;
    delete req.session.flash_success;
    delete req.session.flash_error;
    if (isAdmin) {{
      return res.render('{slug}/manage', {{
        title: 'Manage {resource_plural_human}',
        layout: 'layout-admin',
        rows,
        pagination,
        flashSuccess,
        flashError
      }});
    }}
    {public_listing_render}
  }} catch (e) {{
    console.error(e);
    res.status(500).render('error-500', {{ title: 'Error' }});
  }}
}});

// GET /new: admin-only create form. 404 on public mount. Fetches
// parent rows when the model belongs_to a parent so the form renders
// a populated <select>.
router.get('/new', async (req, res) => {{
  {admin_only_gate}
  try {{
    {_belongs_to_parent_fetch_js(bt_info)}
    res.render('{slug}/new', {{
      title: 'New {resource_name_human}',
      layout: 'layout-admin',
      mode: 'new',
      record: null,
      error: null,
      parent_rows
    }});
  }} catch (e) {{
    console.error(e);
    res.status(500).render('error-500', {{ title: 'Error' }});
  }}
}});

// POST /: admin-only create. processForm normalizes + validates each
// field through the shared field-types registry (email lowercased, phone
// digits-only etc.); validation errors surface as a flash on the form.
router.post('/', yieldPublicPost, validateCsrfToken, async (req, res) => {{
  {admin_only_gate}
  try {{
    const {{ values, errors }} = processForm(FIELD_SPEC, req.body);
    if (Object.keys(errors).length > 0) {{
      const errMsg = Object.values(errors).join(' · ');
      {_belongs_to_parent_fetch_js(bt_info)}
      return res.render('{slug}/new', {{
        title: 'New {resource_name_human}',
        layout: 'layout-admin',
        mode: 'new',
        record: req.body,
        error: errMsg,
        parent_rows
      }});
    }}
    const cols = [];
    const placeholders = [];
    const bound = [];
    // JSONB textarea bodies are JSON.parse()'d below. Parse errors are
    // collected so the admin sees a clear validation message; the legacy
    // silent fallback to [] hid typos and made saves look successful.
    const _jsonbParseErrors = [];
    ALLOWED_COLS.forEach((col) => {{
      const isBool = BOOLEAN_COLS.has(col);
      const isJsonb = JSONB_COLS.has(col);
      // Boolean cols ALWAYS include (force FALSE when checkbox absent);
      // other cols only when supplied (so DB defaults still fire).
      if (req.body[col] !== undefined || isBool) {{
        cols.push(col);
        placeholders.push('$' + (bound.length + 1));
        let v = values[col];
        if (v === undefined) {{
          v = req.body[col];
          if (isBool) {{
            v = (v === 'on' || v === '1' || v === 'true' || v === true);
          }} else if (v === 'on' || v === '1') {{
            v = true;
          }} else if (v === '') {{
            v = null;
          }}
        }}
        if (isJsonb && typeof v === 'string') {{
          const _trim = v.trim();
          if (_trim === '') {{ v = null; }} else {{
            try {{ v = JSON.parse(_trim); }} catch (_e) {{
              _jsonbParseErrors.push(col + ' must be valid JSON. Got: ' + _e.message);
            }}
          }}
        }}
        bound.push(v);
      }}
    }});
    if (_jsonbParseErrors.length > 0) {{
      {_belongs_to_parent_fetch_js(bt_info)}
      return res.render('{slug}/new', {{
        title: 'New {resource_name_human}',
        layout: 'layout-admin',
        mode: 'new',
        record: req.body,
        error: _jsonbParseErrors.join(' · '),
        parent_rows
      }});
    }}
    if (cols.length === 0) {{
      {_belongs_to_parent_fetch_js(bt_info)}
      return res.render('{slug}/new', {{
        title: 'New {resource_name_human}',
        layout: 'layout-admin',
        mode: 'new',
        record: null,
        error: 'At least one field is required.',
        parent_rows
      }});
    }}
    await db.query(
      'INSERT INTO {slug_js} (' + cols.join(', ') + ') VALUES (' + placeholders.join(', ') + ')',
      bound
    );
    req.session.flash_success = '{resource_name_human} created.';
    req.session.save(() => res.redirect('/admin/{slug}'));
  }} catch (e) {{
    console.error(e);
    try {{
      {_belongs_to_parent_fetch_js(bt_info)}
      res.render('{slug}/new', {{
        title: 'New {resource_name_human}',
        layout: 'layout-admin',
        mode: 'new',
        record: null,
        error: e.message,
        parent_rows
      }});
    }} catch (_e2) {{
      res.status(500).render('error-500', {{ title: 'Error' }});
    }}
  }}
}});

// GET /:id, view. Branches admin/public. When the model's plural collides
// with an archetype composed page (e.g. `/case-studies` mounted both as the
// CRUD dual-mount router AND as the composed listing page in routes/pages.js),
// the public branch yields to next() so the pages router's detail handler can
// serve the URL. Without this fallthrough, the dual-mount catches the request
// and 404s when has_public=False, masking the composed detail handler.
router.get('/:id', async (req, res, next) => {{
  try {{
    const isAdmin = req.baseUrl.startsWith('/admin/');
    if (!isAdmin && COLLIDES_WITH_ARCHETYPE_PAGE) {{
      return next();
    }}
    // Ids are SERIAL integers. Anything else ("new" is routed above; a
    // typo or a probe is not) is a 404, not a database type error.
    if (!/^[0-9]{{1,9}}$/.test(String(req.params.id))) {{
      return res.status(404).render('error-404', {{ title: '404', layout: 'layout-public' }});
    }}
    {public_detail_gate}
    // The public detail page applies the same filter as the public
    // listing, so an unpublished row cannot be read by guessing its id.
    const {{ rows }} = await db.query(
      'SELECT * FROM {slug_js} WHERE id = $1' + (isAdmin ? '' : '{public_and}'),
      [req.params.id]
    );
    if (rows.length === 0) {{
      return res.status(404).render('error-404', {{ title: '404', layout: 'layout-public' }});
    }}
    if (isAdmin) {{
      return res.render('{slug}/admin-detail', {{
        title: rows[0].title || rows[0].author_name || '{name_singular}',
        layout: 'layout-admin',
        record: rows[0]
      }});
    }}
    {("return res.render('" + slug + "/detail', { title: rows[0].title || rows[0].author_name || '" + name_singular + "', layout: 'layout-public', record: rows[0] });") if has_public else "return res.status(404).render('error-404', { title: '404', layout: 'layout-public' });"}
  }} catch (e) {{
    console.error(e);
    res.status(500).render('error-500', {{ title: 'Error' }});
  }}
}});

// GET /:id/edit, admin only. Reads + clears flash so validation
// errors from a failed POST /:id/update surface on the re-rendered form.
router.get('/:id/edit', async (req, res) => {{
  {admin_only_gate}
  try {{
    const {{ rows }} = await db.query('SELECT * FROM {slug_js} WHERE id = $1', [req.params.id]);
    if (rows.length === 0) return res.status(404).render('error-404', {{ title: '404', layout: 'layout-public' }});
    {_belongs_to_parent_fetch_js(bt_info)}
    const flashError = req.session.flash_error;
    delete req.session.flash_error;
    res.render('{slug}/edit', {{
      title: 'Edit {resource_name_human}',
      layout: 'layout-admin',
      mode: 'edit',
      record: rows[0],
      error: flashError || null,
      parent_rows
    }});
  }} catch (e) {{
    console.error(e);
    res.status(500).render('error-500', {{ title: 'Error' }});
  }}
}});

// POST /:id/update, admin only. Uses processForm for consistent
// sanitize/validate, then builds the UPDATE statement from the
// normalized values.
router.post('/:id/update', validateCsrfToken, async (req, res) => {{
  {admin_only_gate}
  try {{
    const {{ values: clean, errors }} = processForm(FIELD_SPEC, req.body);
    if (Object.keys(errors).length > 0) {{
      req.session.flash_error = Object.values(errors).join(' · ');
      return req.session.save(() => res.redirect('/admin/{slug}/' + req.params.id + '/edit'));
    }}
    const sets = [];
    const bound = [];
    // See CREATE handler for rationale: surface JSONB parse errors instead
    // of silently saving as [].
    const _jsonbParseErrors = [];
    ALLOWED_COLS.forEach(col => {{
      const isBool = BOOLEAN_COLS.has(col);
      const isJsonb = JSONB_COLS.has(col);
      // Force-include boolean cols so unchecked checkboxes update to FALSE
      // instead of preserving the prior TRUE.
      if (req.body[col] !== undefined || isBool) {{
        let v = clean[col];
        if (v === undefined) {{
          v = req.body[col];
          if (isBool) {{
            v = (v === 'on' || v === '1' || v === 'true' || v === true);
          }} else if (v === 'on' || v === '1') {{
            v = true;
          }} else if (v === '') {{
            v = null;
          }}
        }}
        if (isJsonb && typeof v === 'string') {{
          const _trim = v.trim();
          if (_trim === '') {{ v = null; }} else {{
            try {{ v = JSON.parse(_trim); }} catch (_e) {{
              _jsonbParseErrors.push(col + ' must be valid JSON. Got: ' + _e.message);
            }}
          }}
        }}
        bound.push(v);
        sets.push(col + ' = $' + bound.length);
      }}
    }});
    if (_jsonbParseErrors.length > 0) {{
      req.session.flash_error = _jsonbParseErrors.join(' · ');
      return req.session.save(() => res.redirect('/admin/{slug}/' + req.params.id + '/edit'));
    }}
    bound.push(req.params.id);
    await db.query('UPDATE {slug_js} SET ' + sets.join(', ') + ' WHERE id = $' + bound.length, bound);
    req.session.flash_success = '{resource_name_human} updated.';
    req.session.save(() => res.redirect('/admin/{slug}'));
  }} catch (e) {{
    console.error(e);
    req.session.flash_error = 'Could not save: ' + e.message;
    req.session.save(() => res.redirect('/admin/{slug}/' + req.params.id + '/edit'));
  }}
}});

// POST /:id/delete, admin only.
router.post('/:id/delete', validateCsrfToken, async (req, res) => {{
  {admin_only_gate}
  try {{
    await db.query('DELETE FROM {slug_js} WHERE id = $1', [req.params.id]);
    req.session.flash_success = '{resource_name_human} deleted.';
    req.session.save(() => res.redirect('/admin/{slug}'));
  }} catch (e) {{
    console.error(e);
    req.session.flash_error = 'Delete failed: ' + e.message;
    req.session.save(() => res.redirect('/admin/{slug}'));
  }}
}});

module.exports = router;
'''
    return (f"routes/{slug}.js", contents)


# ---------------------------------------------------------------------------
# View emitters: per-page EJS that slots widgets from the layout JSON
# ---------------------------------------------------------------------------

def _page_widget_defaults(rs: ResolvedSpec, page_id: str, widget_id: str) -> dict:
    """Return sensible default widget params derived from site_settings so
    pages render meaningful content even when the spec doesn't declare a
    per-widget data block. Page+widget combinations the spec explicitly sets
    always override these defaults.

    This is the "resilient-by-default" behavior: a customer spec that only
    supplies site_name + hero_tagline + about_body + contact_email still
    produces a working site with populated heroes + about + contact + CTAs.

    **Faithful-copy carve-out:** when a page carries
    `_copy_blocks` (faithful-copy mode driven by _harbor_meta.copy_pages),
    return an empty defaults dict. Faithful-copy ALREADY sets the keeper's
    verbatim hero on `spec.pages.<id>.hero` and any settings-derived
    `_EJSRef` overlay (e.g. `settings.hero_tagline || ...`) clobbers the
    keeper's heading at render time. The keeper's words are the source
    of truth; defaults stay out of the way.
    """
    page_spec = rs.spec.pages.get(page_id, {})
    if isinstance(page_spec, dict) and page_spec.get("_copy_blocks"):
        return {}

    s = rs.spec.settings or {}
    site_name = s.get("site_name", "")

    def _ref(key: str, fallback: str = "") -> _EJSRef:
        """Emit `(settings.<key> || <fallback_json>)` as an EJS-time JS
        expression. Admin edits to that settings key take effect on the
        next page render: no rebuild needed."""
        return _EJSRef(f"settings.{key} || {_ejs_json(fallback)}")

    defaults: dict = {}

    # HERO widgets (hero-centered, hero-split-image). For settings-backed
    # fields (heading/description/image_url on home + about), return
    # _EJSRef values so the widget resolves them from site_settings at
    # render time, not from spec values baked at build time.
    if widget_id in ("hero-centered", "hero-split-image", "hero-editorial-stacked"):
        if page_id == "home":
            defaults = {
                "eyebrow": s.get("hero_eyebrow") or (s.get("business_name") or site_name),
                "heading": _ref("hero_tagline", site_name or "Welcome"),
                "description": _ref("hero_description", ""),
                "image_url": _ref("hero_image_url", ""),
                "image_alt": _ref("site_name", site_name),
            }
        elif page_id == "about":
            # Drop-cap on the about-page hero description is opt-in. The
            # `.cas-drop-cap::first-letter` rule (font-size: 3.4em italic)
            # overlaps the next character on shorter paragraphs, so we
            # default to plain prose and enable the editorial cap only
            # when the chosen template family declares the literary
            # flourish set OR the keeper opted in explicitly via
            # `_harbor_meta.drop_cap: true`. Specs can still set
            # `drop_cap: true` on the per-page hero data block to force
            # the cap regardless of family.
            tf = rs.template_family if isinstance(rs.template_family, dict) else None
            flourish_id = (tf or {}).get("flourish_set") or ""
            hm = rs.spec.harbor_meta if isinstance(rs.spec.harbor_meta, dict) else {}
            harbor_drop_cap = hm.get("drop_cap") is True
            defaults = {
                "eyebrow": "About",
                "heading": _ref("about_title", f"About {site_name}" if site_name else "About"),
                "description": _ref("about_body", ""),
                "image_url": _ref("about_photo_url", ""),
                "image_alt": _ref("site_name", site_name),
            }
            if flourish_id == "literary" or harbor_drop_cap:
                defaults["drop_cap"] = True
        elif page_id == "work-with-me":
            defaults = {
                "eyebrow": "Services",
                "heading": "Work With Me",
                "description": s.get("services_intro", ""),
            }
        elif page_id == "contact":
            defaults = {
                "eyebrow": "Reach Out",
                "heading": "Contact",
                "description": s.get("contact_intro", ""),
            }
        elif page_id == "journal":
            defaults = {"eyebrow": "Writing", "heading": s.get("journal_title") or "Journal"}
        elif page_id == "events":
            defaults = {"eyebrow": "Upcoming", "heading": s.get("events_title") or "Events"}
        elif page_id == "gallery":
            defaults = {"eyebrow": "Gallery", "heading": s.get("gallery_title") or "Gallery"}
        elif page_id == "services":
            defaults = {"eyebrow": "Services", "heading": "Services"}

    # CTA widget (cta-inquiry-block): contact-route default
    elif widget_id == "cta-inquiry-block":
        defaults = {
            "heading": "Ready to connect?",
            "description": f"Reach out and {site_name.split(' ')[0] if site_name else 'we'} will respond personally." if site_name else "Get in touch and we'll respond soon.",
            "cta_text": "Get in Touch",
            "cta_href": "/contact",
        }

    # Newsletter widget: home-route default
    elif widget_id == "cta-newsletter-inline":
        defaults = {
            "heading": "Stay in touch",
            "description": f"Subscribe for {site_name} updates, new offerings, and reflections." if site_name else "Subscribe for updates and new offerings.",
            "placeholder": "you@example.com",
            "button_text": "Subscribe",
        }

    # Contact form widget: contact-route default. service_options drives
    # the in-form <select name="service_interest">; whenever it is empty,
    # the matching ?service=<X> URL preselect has no <option> to land on.
    # Precedence:
    #   1. settings.service_options (canonical site-wide override)
    #   2. titles harvested from any offerings/services widget items
    #      already present in spec.pages, deduped + capped + suffixed
    #      with "General Inquiry"
    # Step 2 keeps the form-contact widget in sync with the offerings-
    # grid-3col / services-editorial-table widgets without forcing
    # specs (or harbor extraction) to maintain a duplicate list. See
    # docs/internals/SERVICE_PREFILL_PATTERN.md for the full contract.
    elif widget_id == "form-contact":
        opts = list(s.get("service_options") or [])
        if not opts:
            opts = _harvest_service_options_from_offerings(rs)
        defaults = {
            "heading": "Send a Message",
            "service_options": opts,
        }

    return defaults


def _harvest_service_options_from_offerings(rs: ResolvedSpec) -> list:
    """Return a deduped list of offering titles harvested from any
    spec.pages.<page>.<offerings|services|items>.items[] block. Used as
    the contact-form `service_options` fallback when settings.service_options
    is absent so the in-form Service Interest <select> remains in sync with
    the visible offerings-grid-3col / services-editorial-table widgets.

    Order-preserving (first-seen wins), case-insensitive dedupe, and
    capped at 24 entries to keep the select usable. Appends a final
    "General Inquiry" option when at least one title was harvested so
    keepers without a matching service still have a valid landing.
    """
    pages = rs.spec.pages or {}
    seen_lower: set = set()
    titles: list = []
    for _page_id, page_block in pages.items():
        if not isinstance(page_block, dict):
            continue
        for key in ("offerings", "services", "items"):
            block = page_block.get(key)
            if not isinstance(block, dict):
                continue
            items = block.get("items")
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                title = it.get("title")
                if not isinstance(title, str):
                    continue
                title = title.strip()
                if not title:
                    continue
                lo = title.lower()
                if lo in seen_lower:
                    continue
                seen_lower.add(lo)
                titles.append(title)
                if len(titles) >= 24:
                    break
            if len(titles) >= 24:
                break
        if len(titles) >= 24:
            break
    if titles:
        # Always include General Inquiry as a graceful catch-all so a
        # keeper who lands without a ?service= query (or with an
        # unknown one) still sees a valid option to pick.
        if "general inquiry" not in seen_lower:
            titles.append("General Inquiry")
    return titles


# Canonical-alias fallback for layout `source` paths. When the resolved
# `data_key` returns nothing, try these alternates in order. This lets
# layouts standardize on one convention (e.g. `offerings`/`testimonials`)
# without breaking older fixtures or specs that used `services`/`items`/
# `reviews`. Without it a layout that reads `spec.home.services` renders
# an empty offerings block for a spec that stores the block under
# `pages.home.offerings`.
_DATA_KEY_ALIASES: Dict[str, List[str]] = {
    "offerings": ["services", "items"],
    "services": ["offerings", "items"],
    "testimonials": ["reviews"],
    "reviews": ["testimonials"],
    "items": ["offerings", "services"],
}


def _db_source_table(source: str) -> str:
    """Extract table name from `db:<table>` source string, or '' if not a
    db-backed source. db-backed sources tell emit_routes_pages_js to SELECT
    rows from <table> and emit_page_view to pass them through as a runtime
    `items` local rather than a build-time literal (so admin CRUD edits
    show up on the public page with no rebuild)."""
    if not isinstance(source, str):
        return ""
    if not source.startswith("db:"):
        return ""
    return source[3:].strip()


def _page_db_sources_for(rs: ResolvedSpec, layout: dict) -> list:
    """Ordered, de-duplicated [(widget_id, table), ...] for EVERY db-backed
    widget in a layout (not just the first). Tables excluded from the
    schema (exclude_data_models) are dropped. Lets one page mix live rows
    from several tables (the author home reads books AND testimonials;
    pre-fix, the second widget silently received the first table's rows)."""
    known = {_table_name_for_model(m) for m in rs.data_models}
    seen = set()
    out = []
    for w in layout.get("widgets") or []:
        table = _db_source_table(w.get("source", ""))
        if not table or table in seen or table not in known:
            continue
        seen.add(table)
        out.append((w.get("id", ""), table))
    return out


def _tables_with_detail_pages(rs: ResolvedSpec) -> dict:
    """Map table -> public page path for tables whose listing page emits
    per-item detail routes (listing widget in _DETAIL_PAGE_WIDGETS). The
    detail route is registered at <page_path>/:identifier, so rows of
    these tables get detail_href = <page_path>/<slug-or-id> wherever they
    appear; a home-page cover shelf links to /books/<slug> exactly like
    the catalog page does."""
    out: dict = {}
    for pid, layout in rs.page_layouts.items():
        wid, t = _page_db_source_for(layout)
        # Home is excluded: a detail route at the home path would be
        # `/:identifier`, which shadows every other top-level route. Detail
        # routes only live under a named listing page, so only those page
        # paths map to a detail_href prefix.
        if t and wid in _DETAIL_PAGE_WIDGETS and pid != "home":
            out[t] = f"/{pid}"
    return out


def _related_child_info(rs: ResolvedSpec, parent_table: str):
    """Find a model that belongs_to the model owning parent_table and
    whose rows are public in their own right (`_public_row_sources`).
    Returns (child_table, fk_col) or None. Powers relation-filtered
    sections on PUBLIC detail pages (Testimonial belongs_to Book ->
    "What readers are saying" on /books/<slug>).

    A child that is not public is skipped, whatever it belongs to: a
    Registration that belongs_to Event is never read by the event's public
    page."""
    public_tables = _public_row_sources(rs)
    for m in rs.data_models:
        bt = _belongs_to_info(rs, m)
        if bt is None:
            continue
        _parent_model, fk_col, p_table, _od = bt
        child_table = _table_name_for_model(m)
        if p_table == parent_table and child_table in public_tables:
            return (child_table, fk_col)
    return None


def _page_db_source_for(layout: dict) -> tuple:
    """Return (widget_id, table) for the first db-backed widget in a layout,
    or ('', '') if none. Pages router uses this to decide whether to run a
    SELECT before rendering the view."""
    for w in layout.get("widgets") or []:
        table = _db_source_table(w.get("source", ""))
        if table:
            return (w.get("id", ""), table)
    return ("", "")


def _public_slug_for_table(rs: ResolvedSpec, table: str) -> str:
    """Resolve the public mount path for a data_model whose backing SQL
    table matches `table` (typically the lower-cased plural). Used to
    derive the `detail_path_prefix` prop passed to db-backed widgets so
    their cards link to /<slug>/:id."""
    for m in rs.data_models:
        if _slug_for(m).replace("-", "_") == table.replace("-", "_"):
            path = m.get("public_listing_path")
            if path:
                return path
            return "/" + _slug_for(m)
    return "/" + table.replace("_", "-")


def _table_has_slug_column(rs: ResolvedSpec, db_table: str) -> bool:
    """True if the data model whose schema table matches db_table declares a
    `slug` field. Used by emit_routes_pages_js to decide whether the public
    detail handler should accept slug-keyed lookups (`WHERE slug::text = $1
    OR id::text = $1`; the cast keeps the statement valid whatever type the
    spec gave the column) or id-only (`WHERE id::text = $1`).

    Tables without slug (Event, CaseStudy, Review, ServiceArea) only resolve
    detail pages by integer id; tables with slug (BlogPost) accept either.
    """
    return _table_has_column(rs, db_table, "slug")


def _table_has_column(rs: ResolvedSpec, db_table: str, column: str) -> bool:
    """True if the data model backing `db_table` declares `column`."""
    for m in rs.data_models:
        if _table_name_for_model(m) == db_table:
            fields = m.get("fields") or _default_fields(m["name"])
            return any((f.get("name") or "").lower() == column for f in fields)
    return False


def _table_publication_column(rs: ResolvedSpec, db_table: str) -> Optional[str]:
    """The publication flag column of the data model backing `db_table`
    (`_publication_column`: a BOOLEAN column named `published`), or None."""
    for m in rs.data_models:
        if _table_name_for_model(m) == db_table:
            return _publication_column(m)
    return None



def _public_rows_where(rs: ResolvedSpec, db_table: str) -> str:
    """WHERE clause for a PUBLIC read of a content table (leading space
    included), or '' when the table has no publication flag. The condition
    is `_published_condition`, the same one the model's own listing applies:
    a row whose flag is NULL is not published. (The seed writes TRUE for a
    seed row that does not mention the flag, `emit_seed_sql`.)
    """
    column = _table_publication_column(rs, db_table)
    return f" WHERE {_published_condition(column)}" if column else ""


# Widget ids that mean "summary cards on a listing page; click each to read
# the full content on a detail page." emit_routes_pages_js uses this to
# decide whether to emit a sibling detail route + view per db-backed listing.
# Widgets that render the full content inline (reviews-grid, service-area-list,
# photo-gallery-grid, etc.) are excluded.
_DETAIL_PAGE_WIDGETS = ("resource-list-card-grid", "books-grid")


def _normalize_offering_cta_hrefs(widget_data: Any) -> Any:
    """When a widget's items list contains service-shaped entries with
    `cta_href` pointing at a bare `/contact`, append `?service=<urlencoded
    title>` so the contact form pre-selects the right service. Spec-authored
    fixtures (sample-tradesperson, sample-fitness, etc.) and `_default_services`
    for tradesperson/local-service/therapist/fitness/salon/restaurant all
    historically used flat `/contact` here. Without the query string the
    contact form has nothing to preselect, and a customer who clicks a
    specific "Book" card lands on a generic contact form with no context.

    Mutates a shallow copy; safe on any widget_data shape (dict, list, scalar).
    """
    import urllib.parse as _urlparse

    def _maybe_normalize_item(item):
        if not isinstance(item, dict):
            return item
        href = item.get("cta_href")
        title = item.get("title")
        if not isinstance(href, str) or not isinstance(title, str):
            return item
        # Only normalize bare /contact (no query, no hash). Skip /contact?...
        # which already carries something, and any external/tel:/mailto: links.
        if href.strip() == "/contact":
            slug = _urlparse.quote_plus(title)[:80]
            new_item = dict(item)
            new_item["cta_href"] = f"/contact?service={slug}"
            return new_item
        return item

    if isinstance(widget_data, dict):
        out = dict(widget_data)
        items = out.get("items")
        if isinstance(items, list):
            out["items"] = [_maybe_normalize_item(it) for it in items]
        return out
    if isinstance(widget_data, list):
        return [_maybe_normalize_item(it) for it in widget_data]
    return widget_data


# --- Widget parameters -----------------------------------------------------
# What a page widget takes from the spec is declared, per widget, under
# `params` in compose/data/widget-registry.json, in a small notation:
#
#   "text"          a display string. A JSON number becomes its text (a
#                   phone number, a price); anything else is refused.
#   "flag"          true or false.
#   "number"        a JSON number.
#   "text_or_list"  a string, or a list of "text".
#   [shape]         a list whose every element has that shape.
#   {key: shape}    an object; only the listed keys are read.
#
# `_spec_widget_data` applies it to the page data a widget instance is given
# and is the ONLY way spec page data reaches a view: a key the widget does
# not declare is left out, a declared key arrives in its declared shape or
# the spec is refused, and a JSON null means "absent". The widget templates
# call string and list methods on their parameters; this is what makes
# those calls safe whatever the spec holds. A parameter named in
# `required_params` is one its template reads without a `typeof` guard; it
# is given the empty value of its shape when the spec has none.
#
# Rows a widget reads from the database at request time (`source:
# "db:<table>"`) are not spec page data and do not pass through here.

_ABSENT = object()


def _json_kind(value: Any) -> str:
    """The JSON type of a decoded value, for error messages."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    return type(value).__name__


def _number_text(value: Any) -> Optional[str]:
    """A JSON number as display text ("5125550100", "2.5"), or None when it
    is not a finite number. Booleans are not numbers here."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(int(value)) if value.is_integer() else repr(value)
    return None


def _shaped(shape: Any, value: Any, where: str, errors: List[str]) -> Any:
    """`value` in the declared `shape`, or `_ABSENT` (after recording why in
    `errors`, unless the value was simply not there: a null anywhere, or an
    empty string where something other than text is declared)."""
    if value is None:
        return _ABSENT
    if value == "" and isinstance(value, str) and shape not in ("text", "text_or_list"):
        return _ABSENT
    if shape == "text":
        if isinstance(value, str):
            return value
        as_text = _number_text(value)
        if as_text is None:
            errors.append(f"{where} must be text, not {_json_kind(value)}")
            return _ABSENT
        return as_text
    if shape == "flag":
        if isinstance(value, bool):
            return value
        errors.append(f"{where} must be true or false, not {_json_kind(value)}")
        return _ABSENT
    if shape == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            errors.append(f"{where} must be a number, not {_json_kind(value)}")
            return _ABSENT
        return value
    if shape == "text_or_list":
        if isinstance(value, list):
            return _shaped(["text"], value, where, errors)
        return _shaped("text", value, where, errors)
    if isinstance(shape, list):
        if not isinstance(value, list):
            errors.append(f"{where} must be a list, not {_json_kind(value)}")
            return _ABSENT
        items = []
        for index, item in enumerate(value):
            if item is None:
                errors.append(f"{where}[{index}] is null")
                continue
            shaped = _shaped(shape[0], item, f"{where}[{index}]", errors)
            if shaped is not _ABSENT:
                items.append(shaped)
        return items
    if isinstance(shape, dict):
        if not isinstance(value, dict):
            errors.append(f"{where} must be an object, not {_json_kind(value)}")
            return _ABSENT
        out: Dict[str, Any] = {}
        # The spec's own key order is kept, so a spec that already fits its
        # contract is emitted exactly as written.
        for key, item in value.items():
            if key in shape:
                shaped = _shaped(shape[key], item, f"{where}.{key}", errors)
                if shaped is not _ABSENT:
                    out[key] = shaped
        return out
    raise ValueError(f"widget-registry.json declares an unknown parameter shape: {shape!r}")


def _empty_of(shape: Any) -> Any:
    """The empty value of a shape, for a required parameter the spec did
    not supply."""
    if shape == "flag":
        return False
    if shape == "number":
        return 0
    if isinstance(shape, list):
        return []
    if isinstance(shape, dict):
        return {}
    return ""


def _spec_widget_data(widget_id: str, widget: dict, raw: Any, where: str) -> Tuple[dict, List[str]]:
    """The page data one widget instance takes from the spec, in the shape
    its `params` declare, and the reasons to refuse the spec: `(data,
    errors)`. See "Widget parameters" above."""
    errors: List[str] = []
    if not isinstance(raw, dict):
        return {}, [f"{where} must be an object (the data of the {widget_id} widget), not {_json_kind(raw)}"]
    params = widget.get("params") or {}
    data = _shaped(params, raw, where, errors)
    if data is _ABSENT:
        data = {}
    for name in widget.get("required_params") or ():
        if name not in data and name in params:
            data[name] = _empty_of(params[name])
    return data, errors


def _widget_spec_source(rs: ResolvedSpec, page_id: str, w: dict) -> Tuple[str, Any, str]:
    """Where one layout widget's spec data comes from: `(data_key, raw,
    db_table)`. `raw` is the page's value under `data_key` (or under one of
    its `_DATA_KEY_ALIASES` for a static widget), a bare list wrapped under
    the key, and `{}` when the page has nothing there."""
    page_spec = rs.spec.pages.get(page_id, {})
    if not isinstance(page_spec, dict):
        raise ValueError(f"Spec validation failed: pages.{page_id} must be an object")
    widget_id = w["id"]
    source_path = w.get("source", "")
    db_table = _db_source_table(source_path)
    # For db-backed widgets, use the spec_overlay hint (if present) to
    # pull static heading/eyebrow/etc. Otherwise treat the source_path
    # as the legacy static hint (spec.<page>.<key>).
    effective_source = w.get("spec_overlay", "") if db_table else source_path
    data_key = effective_source.split(".")[-1] if effective_source else widget_id.split("-")[0]
    raw = page_spec.get(data_key, {}) if data_key else {}
    if not raw and not db_table:
        for alias in _DATA_KEY_ALIASES.get(data_key, ()):
            alt = page_spec.get(alias)
            if alt:
                raw = alt
                break
    # A source path can resolve to a bare list (`spec.faq.items` is
    # [row, row, ...]). Wrap it under the data_key, the name the widget
    # reads it by.
    if isinstance(raw, list) and raw:
        raw = {data_key: raw}
    return data_key, raw, db_table


def _page_data_errors(rs: ResolvedSpec) -> List[str]:
    """Reasons to refuse the build: every place where the page data a
    widget of this build would be given does not fit the widget's declared
    parameters. resolve_spec raises on these, so the emitters only ever
    see page data that fits."""
    errors: List[str] = []
    for page_id, layout in rs.page_layouts.items():
        if not isinstance(rs.spec.pages.get(page_id, {}), dict):
            errors.append(f"pages.{page_id} must be an object")
            continue
        for w in layout.get("widgets") or []:
            data_key, raw, _db_table = _widget_spec_source(rs, page_id, w)
            if not raw:
                continue
            _data, problems = _spec_widget_data(
                w["id"], get_widget(w["id"]), raw, f"pages.{page_id}.{data_key}",
            )
            for problem in problems:
                if problem not in errors:
                    errors.append(problem)
    return errors


def _assembled_page_widgets(rs: ResolvedSpec, page_id: str, layout: dict):
    """Yield (w, widget, data_key, widget_data, db_table) for each widget
    instance emit_page_view will render, with widget_data fully assembled
    (alias resolution, settings-backed defaults, static_data merge,
    db-source handling). Single source of truth shared by emit_page_view
    and _page_text_settings so the two can never drift."""
    for w in layout["widgets"]:
        widget_id = w["id"]
        widget = get_widget(widget_id)
        data_key, raw_data, db_table = _widget_spec_source(rs, page_id, w)
        # Skip optional widgets whose spec data is empty (only for static
        # sources: db-backed widgets always render, the SELECT may return
        # an empty array which the widget handles via its empty-state copy).
        if w.get("optional") and not raw_data and not db_table:
            continue
        # Skip widgets marked not required that have no spec data either.
        if not raw_data and not w.get("required", True) and not db_table:
            continue
        # The spec's data in the widget's declared shape, and nothing else
        # from the spec (`_spec_widget_data`). resolve_spec has already
        # refused a spec with an error here (`_page_data_errors`); the
        # check is repeated so the views do not depend on who built the
        # ResolvedSpec.
        widget_data, shape_errors = _spec_widget_data(
            widget_id, widget, raw_data or {}, f"pages.{page_id}.{data_key}",
        )
        if shape_errors:
            raise ValueError("Spec validation failed: " + "; ".join(shape_errors[:10]))
        # Normalize service-card CTA hrefs so a "Book"/"Inquire" click on a
        # service card pre-selects the service in the contact form.
        # Spec-authored items often have `cta_href: "/contact"` flat;
        # auto-append `?service=<urlencoded title>` so the contact route's
        # `preselected_service` reads from the URL. Applied to any widget
        # whose items list looks service-shaped (`title` + `cta_href` keys).
        widget_data = _normalize_offering_cta_hrefs(widget_data)
        # Per-widget/per-page settings-derived defaults so pages without
        # explicit per-widget spec data still render meaningful content
        # from the canonical site_settings. Spec values override plain
        # defaults, BUT settings-backed _EJSRef values always win so
        # admin edits to site_settings propagate at render time without
        # a rebuild.
        defaults = _page_widget_defaults(rs, page_id, widget_id)
        if defaults:
            widget_data = {**defaults, **widget_data}
            for _dk, _dv in defaults.items():
                if isinstance(_dv, _EJSRef):
                    widget_data[_dk] = _dv
        # Merge in any static_data from the page-layout widget entry
        # (e.g. {"compact": true} on form-page heroes to shrink whitespace).
        static_data = w.get("static_data") or {}
        if static_data:
            widget_data = {**widget_data, **static_data}
        # The contact form's inputs are the contact model's fields, the
        # same list POST /contact inserts (`_contact_post_handler_js`).
        # Set after the spec's page data on purpose: page copy may restyle
        # the form, but it must not be able to add or drop a column.
        if widget_id == "form-contact":
            widget_data = {**widget_data, "form_fields": _contact_form_fields(_contact_form_model(rs))}
        # For db-backed widgets, override `items` with a runtime local and
        # add count-derived mode props. The spec may have supplied a
        # heading/eyebrow via spec_overlay; we preserve those. We strip
        # any static items array the spec tried to carry.
        # Copy first: widget_data may still be an alias of the original
        # spec dict (when no defaults and no static_data intervened), and
        # pop()/assignment below would mutate the spec in-place, breaking
        # the downstream spec.json snapshot.
        if db_table:
            widget_data = dict(widget_data)
            widget_data.pop("items", None)
            widget_data.pop("rows", None)
            # menu-grouped-list (and any other widget that pre-grouped items
            # under `sections`) must also have the static array stripped, or
            # the widget renders the legacy spec rows instead of the live DB
            # rows. Observed on wren-rye-cafe where the
            # spec.menu.sections.sections leaked through despite source =
            # db:menu_items, leaving 14 stale items on /menu while admin
            # CRUD only touched 8 DB rows.
            widget_data.pop("sections", None)
            # Use locals.X (not bare typeof X); express-ejs-layouts can
            # render the page view in a layout-only scope where bare-variable
            # access of res.render locals returns undefined even when the
            # route passed the data. locals.X always works in EJS.
            # See roadmap item v2-ejs-locals-bare-variable-undefined.
            # Per-table local first (items_<table>), legacy `items`
            # fallback second: multi-table pages (author home: books +
            # testimonials) give each widget ITS OWN table's rows.
            _items_js = (
                f"(locals && Array.isArray(locals.items_{db_table}))"
                f" ? locals.items_{db_table}"
                " : ((locals && Array.isArray(locals.items)) ? locals.items : [])"
            )
            widget_data["items"] = _EJSRef(_items_js)
            # The path is spliced into an EJS expression as a quoted
            # literal. _route_path guarantees it holds only letters,
            # digits, '-', '_' and '/', so it cannot close the quote or
            # the tag (a public_listing_path such as "/x' + process..."
            # used to reach this line unchecked).
            slug = _route_path(_public_slug_for_table(rs, db_table))
            widget_data["detail_path_prefix"] = _EJSRef(
                f"({_items_js}).length > 3 ? '{slug}' : null"
            )
            widget_data["expand_inline"] = _EJSRef(
                f"({_items_js}).length <= 3"
            )
        # Last step, so it covers spec data, alias fallbacks and merged
        # defaults alike: neutralize link targets with a non-allowlisted
        # scheme and reduce raw-HTML parameters to the safe tag set.
        widget_data = _sanitize_spec_content(widget_data)
        yield (w, widget, data_key, widget_data, db_table)


# Customer-voice fields that must stay admin-editable. A page template
# carries ZERO customer-voice prose the admin can't edit
# (v2-full-editability-audit): every static string in these
# fields becomes a site_settings-backed read seeded with the current
# copy, surfaced in a per-page "Page Text" section of /admin/settings.
_CUSTOMER_VOICE_FIELDS = (
    "heading", "eyebrow", "description", "body", "tagline", "intro",
    "subheading", "welcome_text", "closing_text", "cta_text",
    "button_text", "note",
)
_PAGE_TEXT_TEXTAREA_FIELDS = {
    "description", "body", "intro", "welcome_text", "closing_text",
}


def _page_text_settings(rs: ResolvedSpec) -> dict:
    """Map every static customer-voice string on every public page to a
    deterministic site_settings key. Returns {page_id: [entry, ...]} where
    entry = {key, label, value, type, field, widget_index}.

    Faithful-copy pages (_copy_blocks) are excluded: the keeper's verbatim
    words are the source of truth there and the body is pre-rendered HTML,
    not a settings-form-editable string.
    """
    # Memoized per ResolvedSpec: emit_seed_sql, the settings route, and
    # every emit_page_view call all consume the same map; recomputing it
    # per page made compose O(pages^2) over widget assembly.
    cached = getattr(rs, "_page_text_settings_cache", None)
    if cached is not None:
        return cached
    out: dict = {}
    for page_id, layout in rs.page_layouts.items():
        page_spec = rs.spec.pages.get(page_id, {})
        if isinstance(page_spec, dict) and page_spec.get("_copy_blocks"):
            continue
        entries = []
        used: set = set()
        for idx, (w, widget, data_key, widget_data, db_table) in enumerate(
            _assembled_page_widgets(rs, page_id, layout)
        ):
            for field in _CUSTOMER_VOICE_FIELDS:
                val = widget_data.get(field)
                if not isinstance(val, str):
                    continue
                if not val.strip() or len(val) > 2000:
                    continue
                base = re.sub(
                    r"[^a-z0-9]+", "_",
                    f"pg_{page_id}_{data_key}_{field}".lower(),
                ).strip("_")
                key = base
                n = 2
                while key in used:
                    key = f"{base}_{n}"
                    n += 1
                used.add(key)
                field_type = (
                    "textarea"
                    if field in _PAGE_TEXT_TEXTAREA_FIELDS or len(val) > 90
                    else "text"
                )
                entries.append({
                    "key": key,
                    "label": f"{_prettify(data_key)}: {_prettify(field)}",
                    "value": val,
                    "type": field_type,
                    "field": field,
                    "widget_index": idx,
                })
        if entries:
            out[page_id] = entries
    try:
        rs._page_text_settings_cache = out
    except (AttributeError, TypeError):
        pass
    return out


def emit_page_view(rs: ResolvedSpec, page_id: str, layout: dict) -> tuple:
    """Emit a per-page EJS view that includes the composed widgets in order.

    Each widget reference becomes an EJS include(...) with its data passed as
    a plain object literal assembled from spec.pages[page_id].<widget_data>.
    Widgets marked optional in the layout are skipped when the spec has no
    data for them.

    `source: "db:<table>"` is a first-class source form: the widget's `items`
    prop is replaced with a runtime reference to a pages-router-provided
    local (see emit_routes_pages_js). Heading/eyebrow/etc can still come
    from spec via an optional `spec_overlay` hint pointing at the legacy
    static source path.

    Customer-voice strings are emitted as settings-backed reads
    (`settings.pg_<page>_<area>_<field>` with the spec copy as fallback)
    so the admin can edit every visible sentence without a rebuild
    (v2-full-editability-audit). The same keys are seeded with the
    current copy by emit_seed_sql and surfaced by the settings form.
    """
    page_text = _page_text_settings(rs).get(page_id, [])
    page_text_lookup = {
        (e["widget_index"], e["field"]): e for e in page_text
    }
    widget_calls = []
    for idx, (w, widget, data_key, widget_data, db_table) in enumerate(
        _assembled_page_widgets(rs, page_id, layout)
    ):
        widget_id = w["id"]
        for field in _CUSTOMER_VOICE_FIELDS:
            entry = page_text_lookup.get((idx, field))
            if entry is None:
                continue
            fallback = _ejs_json(entry["value"])
            widget_data = dict(widget_data)
            widget_data[field] = _EJSRef(
                f"(settings.{entry['key']} !== undefined"
                f" ? settings.{entry['key']} : {fallback})"
            )
        data_js = _to_ejs_data_literal(widget_data)
        # For the contact form widget, inject preselected_service +
        # submitted (server-rejected form values) dynamically from the
        # parent view locals so `/contact?service=X` pre-selects the
        # right option AND rejected submissions re-populate the form.
        if widget_id == "form-contact":
            if data_js.endswith("}"):
                body_inner = data_js[:-1].rstrip(", ")
                sep = "" if body_inner.endswith("{") else ", "
                data_js = body_inner + sep + (
                    "preselected_service: ((locals && locals.preselected_service) || ''), "
                    "submitted: ((locals && locals.submitted) || null)"
                    "}"
                )
        widget_calls.append(
            f"<%- include('./widgets/{widget['file'].split('/',1)[1]}', {data_js}) %>"
        )
    body = "\n".join(widget_calls)
    return (f"views/{page_id}.ejs", body + "\n")


# Locals that route handlers commonly pass to res.render. Page views must
# read these via `locals.X` (or `locals && locals.X`); never via bare
# `typeof X !== 'undefined'`. express-ejs-layouts wraps the page render in
# a layout-only scope where bare-variable access of res.render locals can
# return undefined even when the route passed the data, producing silent
# empty-state renders (first seen on a /services page).
_RENDER_LOCAL_NAMES = (
    "items",
    "rows",
    "record",
    "preselected_service",
    "submitted",
    "pagination",
    "parent_rows",
    "service_options",
    "flashSuccess",
    "flashError",
)


def audit_emitted_views_for_bare_locals(files):
    """Scan (path, content) tuples and flag bare `typeof X !== 'undefined'`
    patterns in top-level page views, where X is a known route-passed local.

    Excludes:
      - Layout files (views/layout-*.ejs): receive their own locals via
        express-ejs-layouts and bare typeof works in layout scope.
      - Widget partials (views/widgets/**): included via `<%- include(...) %>`
        which passes data as the include's own locals; bare typeof works
        inside an include because the include's scope is fresh.
      - Per-resource admin views (views/<slug>/*.ejs): these use bare
        typeof in their own scopes too, but are not the bug surface
        reported in v2-ejs-locals-bare-variable-undefined.

    Returns a list of {"path": str, "local": str, "snippet": str} violations.
    Empty list means the audit is clean.

    See roadmap v2-ejs-locals-bare-variable-undefined for the bug origin.
    """
    import re as _re

    violations = []
    excluded_prefixes = ("views/widgets/",)
    excluded_names = (
        "views/layout-public.ejs",
        "views/layout-admin.ejs",
    )
    for entry in files:
        if not isinstance(entry, tuple) or len(entry) != 2:
            continue
        path, content = entry
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        if not path.startswith("views/") or not path.endswith(".ejs"):
            continue
        if path in excluded_names:
            continue
        if any(path.startswith(p) for p in excluded_prefixes):
            continue
        # Only audit top-level page views: views/<page>.ejs (one segment
        # past `views/`). Nested resource views (views/<slug>/X.ejs) are
        # out of scope for this audit.
        rel = path[len("views/"):]
        if "/" in rel.rstrip("/"):
            continue
        for local in _RENDER_LOCAL_NAMES:
            pattern = _re.compile(
                rf"typeof\s+{_re.escape(local)}\s*!==?\s*['\"]undefined['\"]"
            )
            for m in pattern.finditer(content):
                start = max(0, m.start() - 20)
                end = min(len(content), m.end() + 40)
                violations.append({
                    "path": path,
                    "local": local,
                    "snippet": content[start:end].replace("\n", " "),
                })
    return violations


# Prefixes that mark a class name as a "Tailwind-utility-shape" token the
# curated style.css is responsible for defining. Any class that doesn't
# match one of these stems is treated as a component class (e.g. cas-*,
# layout-specific selectors, third-party class names) and excluded from
# the coverage audit so we don't false-positive on rules that live inside
# inline <style> blocks or self-styling component CSS.
#
# We deliberately do NOT include "border" / "rounded" as bare prefixes
# because those bare-class utilities ARE in the curated CSS already; the
# coverage check below catches the variant forms (border-2, rounded-md)
# via the trailing-segment pattern.
_TAILWIND_UTILITY_PREFIXES: tuple = (
    "p-", "px-", "py-", "pt-", "pr-", "pb-", "pl-",
    "m-", "mx-", "my-", "mt-", "mr-", "mb-", "ml-",
    "gap-", "space-x-", "space-y-",
    "w-", "h-", "min-w-", "min-h-", "max-w-", "max-h-",
    "text-", "font-", "leading-", "tracking-",
    "flex-", "items-", "justify-", "self-", "content-",
    "grid-cols-", "col-span-", "row-span-",
    "border-", "rounded-",
    "bg-", "opacity-", "shadow-",
    "cursor-", "transition-",
    "order-", "whitespace-",
    "aspect-", "object-", "overflow-",
    "top-", "left-", "right-", "bottom-", "z-",
    "uppercase", "lowercase", "capitalize",
    "block", "inline-block", "inline-flex", "inline", "flex", "grid", "hidden",
    "absolute", "relative", "sticky", "static", "fixed",
    "sr-",
)

# Variant prefixes the curated CSS supports. A class with one of these
# variant prefixes is treated as a utility too (the underlying token must
# match _TAILWIND_UTILITY_PREFIXES after stripping the variant).
_TAILWIND_VARIANT_PREFIXES: tuple = (
    "hover:", "focus:", "active:", "disabled:",
    "sm:", "md:", "lg:", "xl:", "2xl:",
)


def _looks_like_tailwind_utility(cls: str) -> bool:
    """Return True when `cls` matches the shape of a Tailwind-like utility
    class the curated CSS is responsible for defining.

    Excludes component-style classes (cas-*, mw-*, third-party names),
    EJS-template artifacts (anything containing `<%` or `%>`), and
    plain identifiers that don't start with a known utility stem.

    The shape match is deliberately strict: a class with one of the
    documented variant prefixes (`md:`, `hover:`, ...) must, after
    stripping the variant, match one of the documented utility stems.
    """
    if not cls or not isinstance(cls, str):
        return False
    if "<%" in cls or "%>" in cls or "${" in cls:
        return False
    # Component / framework / third-party prefixes; explicitly NOT
    # treated as utilities; their CSS lives outside the curated subset.
    if cls.startswith(("cas-", "mw-", "swiper-", "fa-", "icon-", "btn-", "card-", "lock-")):
        return False
    head = cls
    for vp in _TAILWIND_VARIANT_PREFIXES:
        if cls.startswith(vp):
            head = cls[len(vp):]
            # Re-apply the component-prefix exclusion on the variant body.
            if head.startswith(("cas-", "mw-")):
                return False
            break
    return head.startswith(_TAILWIND_UTILITY_PREFIXES)


def _extract_css_selectors(css: str) -> set:
    """Return the set of bare class selectors defined anywhere in a CSS
    blob. Walks `.<name>` tokens, un-escapes the standard Tailwind escape
    sequences (`\\:`, `\\.`, `\\[`, `\\]`, `\\/`), and yields one entry
    per defined class regardless of any chained pseudo-selectors that
    follow (e.g. `.foo:hover { ... }` registers `foo`).
    """
    import re as _re
    found = set()
    if not css:
        return found
    # Walk every `.<class>` token. Class names can contain alphanumerics,
    # underscores, hyphens, and the standard Tailwind escape pairs.
    pattern = _re.compile(r"\.((?:\\[\.:\[\]/]|[A-Za-z0-9_-])+)")
    for m in pattern.finditer(css):
        raw = m.group(1)
        unescaped = _re.sub(r"\\([\.:\[\]/])", r"\1", raw)
        found.add(unescaped)
    return found


def _collect_css_sources(files) -> str:
    """Aggregate the CSS the runtime will actually load: the bundled
    `public/style.css` plus inline `<style>...</style>` blocks in the
    shared layout views. The layout-inline blocks define `.font-display`
    and `.font-body` (whose font-family values depend on the chosen
    font_pair); skipping them would false-positive the coverage audit.
    """
    import re as _re
    parts = []
    inline_style_re = _re.compile(r"<style[^>]*>([\s\S]*?)</style>", _re.IGNORECASE)
    for entry in files:
        if not isinstance(entry, tuple) or len(entry) != 2:
            continue
        path, content = entry
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        if path == "public/style.css":
            parts.append(content)
        elif path in ("views/layout-public.ejs", "views/layout-admin.ejs"):
            for m in inline_style_re.finditer(content):
                parts.append(m.group(1))
    return "\n".join(parts)


def audit_aspect_ratio_min_height_conflicts(files) -> list:
    """Flag elements that pair a fixed min-height with aspect-ratio on the
    same box: aspect-ratio alone sizes correctly at every
    width; adding min-height forces computed width past narrow viewports
    and the overflow hides behind overflow:hidden with no scrollbar to
    betray it (a phone slideshow can clip by a third). Scans emitted .ejs
    views for style attributes carrying both declarations."""
    violations = []
    style_attr_re = re.compile(r'style="([^"]*)"')
    for path, contents in files:
        if not isinstance(contents, str) or not path.endswith(".ejs"):
            continue
        for m in style_attr_re.finditer(contents):
            style = m.group(1)
            if "aspect-ratio" in style and re.search(r"min-height\s*:\s*\d", style):
                violations.append({
                    "path": path,
                    "style": style[:120],
                })
    return violations


def audit_emitted_css_utility_coverage(files):
    """Scan every emitted EJS file for `class="..."` (and `class='...'`)
    attributes, extract the Tailwind-utility-shape tokens, and verify
    each one is defined in the build's aggregated CSS (public/style.css
    plus inline <style> blocks in the shared layouts).

    Returns a sorted list of {"class": str, "used_in": [str, ...]}
    descriptors, one per utility class referenced by widget markup but
    never defined in the emitted CSS. An empty list means the audit is
    clean.

    Design intent (per roadmap
    v2-compose-tailwind-curated-subset-coverage-gaps): the compose engine
    emits a curated Tailwind-like utility CSS subset, not a full Tailwind
    build. When a widget references a utility the subset doesn't carry,
    the resulting page silently renders with no rule for that property
    (e.g. `<a class="px-7 py-3">` collapses to zero horizontal padding).
    This audit is the regression guard that makes the gap visible at
    build time rather than at customer-review time.

    Component classes (cas-*, mw-*) and EJS-template artifacts are
    excluded by `_looks_like_tailwind_utility` so we only flag utilities
    the curated CSS is actually responsible for.
    """
    import re as _re
    used: dict = {}
    class_attr_re = _re.compile(r"""class\s*=\s*["']([^"']+)["']""")
    for entry in files:
        if not isinstance(entry, tuple) or len(entry) != 2:
            continue
        path, content = entry
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        if not path.endswith(".ejs"):
            continue
        for m in class_attr_re.finditer(content):
            for cls in m.group(1).split():
                cls = cls.strip()
                if not _looks_like_tailwind_utility(cls):
                    continue
                used.setdefault(cls, []).append(path)

    defined = _extract_css_selectors(_collect_css_sources(files))

    missing = []
    for cls, paths in sorted(used.items()):
        if cls in defined:
            continue
        # Dedupe + cap the per-class evidence list so reports stay
        # readable. The first 5 paths is plenty signal for triage.
        unique_paths = sorted(set(paths))[:5]
        missing.append({"class": cls, "used_in": unique_paths})
    return missing


def _book_detail_view_body() -> str:
    """Per-book public detail page (v2-author-archetype-suite).

    Cover renders at its natural 2/3 aspect with object-contain on a
    tinted backdrop (never band-cropped). Retailer CTAs match
    the books-grid card. Relation-filtered testimonials arrive as
    `related_rows` from the route; the empty state offers a path back
    instead of a bare gap ("Reviews coming soon")."""
    return (
        '<%\n'
        'function _bdEsc(s){ return String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }\n'
        '// Same allowlist as widgets/offerings/book-card.ejs: decode basic\n'
        '// entities once, escape everything, then re-admit a few\n'
        '// attribute-free formatting tags. Nothing else becomes markup.\n'
        'function _bdSafeHtml(s){\n'
        '  var decoded = String(s == null ? "" : s).replace(/&(amp|lt|gt|quot|#39);/g, function(_m, n){ return ({amp:"&", lt:"<", gt:">", quot:"\\"", "#39":"\'"})[n]; });\n'
        '  return _bdEsc(decoded).replace(/&lt;(\\/?)(p|br|em|strong|i|b|ul|ol|li|blockquote)\\s*(\\/?)&gt;/gi, function(_m, open, tag, close){ return "<" + open + tag.toLowerCase() + close + ">"; });\n'
        '}\n'
        'var _coverUrl = record.cover_image_url || record.image_url || "";\n'
        'var _coverAlt = record.cover_image_alt || ((record.title || "Book") + " cover");\n'
        'var _retailers = [];\n'
        'if (record.goodreads_url) _retailers.push(\'<a href="\' + _bdEsc(record.goodreads_url) + \'" target="_blank" rel="noopener">Goodreads</a>\');\n'
        'if (record.local_bookstore_url) _retailers.push(\'<a href="\' + _bdEsc(record.local_bookstore_url) + \'" target="_blank" rel="noopener">Local bookstore</a>\');\n'
        'if (record.barnesnoble_url) _retailers.push(\'<a href="\' + _bdEsc(record.barnesnoble_url) + \'" target="_blank" rel="noopener">Barnes &amp; Noble</a>\');\n'
        'if (record.netgalley_url) _retailers.push(\'<a href="\' + _bdEsc(record.netgalley_url) + \'" target="_blank" rel="noopener">NetGalley</a>\');\n'
        'if (record.audible_url) _retailers.push(\'<a href="\' + _bdEsc(record.audible_url) + \'" target="_blank" rel="noopener">Audible</a>\');\n'
        'var _reviews = (locals && Array.isArray(locals.related_rows)) ? locals.related_rows : [];\n'
        '%>\n'
        '<section class="py-10 md:py-16" style="background-color: var(--color-background);">\n'
        '  <div class="max-w-5xl mx-auto px-4 sm:px-6">\n'
        '    <a href="<%= listing_path %>" class="font-body text-sm font-medium inline-flex items-center mb-8 transition-opacity hover:opacity-70" style="color: var(--color-primary);">← Back to <%= listing_label %></a>\n'
        '    <div class="grid grid-cols-1 md:grid-cols-5 gap-10">\n'
        '      <% if (_coverUrl) { %>\n'
        '      <div class="md:col-span-2">\n'
        '        <img src="<%= _coverUrl %>" alt="<%= _coverAlt %>" class="w-full rounded-xl border" style="aspect-ratio: 2 / 3; object-fit: contain; background-color: var(--color-surface); border-color: var(--color-border); padding: 1.25rem;" />\n'
        '        <% if (record.cover_credit) { %><div class="font-body text-xs mt-2" style="color: var(--color-foreground-muted); font-style: italic;"><%= record.cover_credit %></div><% } %>\n'
        '      </div>\n'
        '      <% } %>\n'
        '      <div class="<%= _coverUrl ? "md:col-span-3" : "md:col-span-5" %>">\n'
        '        <% if (record.series_label) { %>\n'
        '          <div class="font-body text-xs font-bold uppercase mb-3" style="color: var(--color-foreground-muted); letter-spacing: 0.15em;"><%= record.series_label %></div>\n'
        '        <% } %>\n'
        '        <h1 class="font-display text-3xl md:text-5xl font-bold tracking-tight mb-3" style="color: var(--color-foreground); letter-spacing: -0.02em;"><%= record.title || "" %></h1>\n'
        '        <% if (record.subtitle) { %><div class="font-body text-lg italic mb-4" style="color: var(--color-foreground-muted);"><%= record.subtitle %></div><% } %>\n'
        '        <% if (record.accolade || record.rating_callout) { %>\n'
        '          <div class="font-body text-sm mb-5" style="color: var(--color-foreground);">\n'
        '            <% if (record.accolade) { %><strong><%= record.accolade %></strong><% } %>\n'
        '            <% if (record.accolade && record.rating_callout) { %> &middot; <% } %>\n'
        '            <% if (record.rating_callout) { %><span style="color: var(--color-accent-earth, var(--color-primary)); font-weight: 600;"><%= record.rating_callout %></span><% } %>\n'
        '          </div>\n'
        '        <% } %>\n'
        '        <% if (record.description) { %><div class="font-body text-base leading-relaxed mb-6" style="color: var(--color-foreground-muted);"><%- _bdSafeHtml(record.description) %></div><% } %>\n'
        '        <% if (record.price_display) { %><div class="font-body text-sm mb-5" style="color: var(--color-foreground-muted);"><%= record.price_display %></div><% } %>\n'
        '        <% if (record.amazon_url) { %>\n'
        '          <a href="<%= record.amazon_url %>" target="_blank" rel="noopener" class="inline-flex items-center justify-center font-body text-sm font-semibold rounded-md px-5 py-3 mb-4 transition-opacity hover:opacity-90" style="background-color: var(--color-primary); color: var(--color-on-primary); text-decoration: none;">Buy on Amazon</a>\n'
        '        <% } %>\n'
        '        <% if (_retailers.length > 0) { %>\n'
        '          <div class="font-body text-sm" style="color: var(--color-foreground-muted);"><em>Also at:</em> <%- _retailers.join(" &middot; ") %></div>\n'
        '        <% } %>\n'
        '      </div>\n'
        '    </div>\n'
        '    <div class="mt-14 pt-10" style="border-top: 1px solid var(--color-border);">\n'
        '      <h2 class="font-display text-2xl md:text-3xl font-bold tracking-tight mb-6" style="color: var(--color-foreground);">What readers are saying</h2>\n'
        '      <% if (_reviews.length > 0) { %>\n'
        '        <div class="space-y-6">\n'
        '          <% _reviews.forEach(function(t) { %>\n'
        '            <blockquote class="font-body text-base leading-relaxed p-6 rounded-xl border" style="background-color: var(--color-surface); border-color: var(--color-border); color: var(--color-foreground); margin: 0;">\n'
        '              <p style="margin: 0;"><%= t.quote || "" %></p>\n'
        '              <% if (t.author_name) { %><footer class="font-body text-sm mt-3" style="color: var(--color-foreground-muted);">- <%= t.author_name %></footer><% } %>\n'
        '            </blockquote>\n'
        '          <% }); %>\n'
        '        </div>\n'
        '      <% } else { %>\n'
        '        <p class="font-body text-base" style="color: var(--color-foreground-muted);">Reviews coming soon. In the meantime, <a href="<%= listing_path %>" style="color: var(--color-primary);">browse the other books</a>.</p>\n'
        '      <% } %>\n'
        '    </div>\n'
        '  </div>\n'
        '</section>\n'
    )


def emit_page_detail_view(rs: ResolvedSpec, page_id: str, layout: dict) -> tuple:
    """Emit a per-page detail view EJS for db-backed listing pages whose
    listing widget is in `_DETAIL_PAGE_WIDGETS` (today: resource-list-card-grid).
    The route handler (emit_routes_pages_js) sets `record` (a single DB row)
    on the view locals; the view renders title + image + date + body with
    tolerant column-name reads matching the generalized resource-list-card-grid
    widget (description || excerpt || summary, body || description, etc.).

    Returns ('', '') for pages that don't need a detail view so the caller
    can skip the file emit.
    """
    db_widget_id, db_table = _page_db_source_for(layout)
    if not db_table or db_widget_id not in _DETAIL_PAGE_WIDGETS:
        return ("", "")
    if db_widget_id == "books-grid":
        return (f"views/{page_id}-detail.ejs", _book_detail_view_body())
    body = (
        '<%\n'
        'function _firstStr() {\n'
        '  for (var i = 0; i < arguments.length; i++) {\n'
        '    var v = arguments[i];\n'
        '    if (v !== undefined && v !== null && String(v).trim() !== "") return String(v);\n'
        '  }\n'
        '  return "";\n'
        '}\n'
        'function _formatDate(raw) {\n'
        '  if (!raw) return "";\n'
        '  try {\n'
        '    var d = new Date(raw);\n'
        '    if (isNaN(d.getTime())) return String(raw);\n'
        '    return d.toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" });\n'
        '  } catch (e) { return String(raw); }\n'
        '}\n'
        'var _title = _firstStr(record.title, record.name, "(untitled)");\n'
        '// Body of the detail page: prefer the long-form body / description.\n'
        '// Fall back to excerpt / summary so the detail page still has\n'
        '// content even when only a short blurb was authored.\n'
        'var _body = _firstStr(record.body, record.description, record.excerpt, record.summary);\n'
        'var _imageUrl = _firstStr(record.image_url, record.photo_url, record.cover_url);\n'
        'var _date = _formatDate(_firstStr(record.published_at, record.event_date, record.created_at));\n'
        'var _byline = _firstStr(record.client_name, record.author, record.author_name);\n'
        'var _location = _firstStr(record.location);\n'
        '%>\n'
        '<section class="py-10 md:py-16" style="background-color: var(--color-background);">\n'
        '  <div class="max-w-3xl mx-auto px-4 sm:px-6">\n'
        '    <a href="<%= listing_path %>" class="font-body text-sm font-medium inline-flex items-center mb-6 transition-opacity hover:opacity-70" style="color: var(--color-primary);">← Back to <%= listing_label %></a>\n'
        '    <% if (_date) { %>\n'
        '      <div class="font-body text-xs uppercase tracking-widest mb-3" style="color: var(--color-foreground-muted);"><%= _date %></div>\n'
        '    <% } %>\n'
        '    <h1 class="font-display text-3xl md:text-5xl font-bold tracking-tight mb-4" style="color: var(--color-foreground); letter-spacing: -0.02em;"><%= _title %></h1>\n'
        '    <% if (_byline) { %>\n'
        '      <div class="font-body text-base italic mb-2" style="color: var(--color-foreground-muted);">- <%= _byline %></div>\n'
        '    <% } %>\n'
        '    <% if (_location) { %>\n'
        '      <div class="font-body text-sm mb-6" style="color: var(--color-foreground-muted);"><%= _location %></div>\n'
        '    <% } %>\n'
        '    <% if (_imageUrl) { %>\n'
        '      <% // Natural aspect within bounds: fixed 16/9\n'
        '         // crops band-destroy portrait/square sources. focal_y\n'
        '         // (percent) is art direction for the rare crop that does\n'
        '         // happen at max-height.\n'
        '         var _focalY = (record.focal_y !== undefined && record.focal_y !== null && record.focal_y !== "" && Number(record.focal_y) >= 0 && Number(record.focal_y) <= 100) ? Number(record.focal_y) : 50;\n'
        '      %>\n'
        '      <img src="<%= _imageUrl %>" alt="<%= _title %>" class="w-full rounded-xl mb-8" style="height: auto; max-height: 640px; object-fit: cover; object-position: center <%= _focalY %>%;" loading="eager" fetchpriority="high" />\n'
        '    <% } %>\n'
        '    <% if (_body) { %>\n'
        '      <div class="font-body text-base md:text-lg leading-relaxed" style="color: var(--color-foreground); white-space: pre-wrap;"><%= _body %></div>\n'
        '    <% } %>\n'
        '  </div>\n'
        '</section>\n'
    )
    return (f"views/{page_id}-detail.ejs", body)


class _EJSRef:
    """Sentinel wrapping a raw JS expression that must pass through
    _to_ejs_data_literal WITHOUT json-quoting. Used when a widget param
    needs to read from runtime settings/locals (e.g. `settings.hero_tagline`)
    so admin edits take effect without a rebuild. Example:

        _EJSRef("settings.hero_tagline || 'Welcome'")
    """
    __slots__ = ("expr",)

    def __init__(self, expr: str) -> None:
        self.expr = expr


def _to_ejs_data_literal(data: Any) -> str:
    """Convert a Python dict into a JS object literal safe for EJS
    <% include %> data args. Values that are _EJSRef instances are
    emitted as raw JS expressions (unquoted); all other values go
    through `_ejs_json`, which keeps spec text from closing the EJS tag
    it is embedded in. An _EJSRef expression is engine-authored code: never
    build one by pasting a spec string into it (use _ejs_json for the
    string part, as `_ref` does).
    """
    if not data:
        return "{}"
    if not isinstance(data, dict):
        return _ejs_json(data)
    parts = []
    for k, v in data.items():
        key_js = _ejs_json(str(k))
        if isinstance(v, _EJSRef):
            parts.append(f"{key_js}: ({v.expr})")
        elif isinstance(v, dict):
            parts.append(f"{key_js}: {_to_ejs_data_literal(v)}")
        else:
            parts.append(f"{key_js}: {_ejs_json(v)}")
    return "{" + ", ".join(parts) + "}"


def emit_layout_files(rs: ResolvedSpec) -> list:
    """Emit views/layout-public.ejs + views/layout-admin.ejs.

    layout-public.ejs is parameterized at emit time:
      - __SHELL_NAV_FILE__   -> the chosen template family's nav partial
                                file basename (no path), e.g. "nav-public"
                                or "nav-thin-statement"
      - __SHELL_FOOTER_FILE__ -> footer partial file basename
      - __TYPOGRAPHY_SCALE_CSS__     -> CSS variable declarations
      - __TYPOGRAPHY_SCALE_CLASSES__ -> CSS classes that reference them

    The chosen family is on `rs.template_family` (a dict from
    templates.json) when set; otherwise the legacy classic-business
    defaults are used (nav-public + footer + zero typography overrides).
    """
    # Read the templates
    pub = (TEMPLATE_DIR / "layout-public.ejs").read_text(encoding="utf-8")
    adm = (TEMPLATE_DIR / "layout-admin.ejs").read_text(encoding="utf-8")
    # The layouts reference '../widgets/shell/...'. Generated app has
    # views/ next to widgets/ inside views/. Rewrite to ./widgets/shell/.
    pub = pub.replace("../widgets/shell/", "./widgets/shell/")
    adm = adm.replace("../widgets/shell/", "./widgets/shell/")

    # Resolve template family (default: classic-business behavior).
    family = getattr(rs, "template_family", None) or {}
    nav_id = family.get("shell_nav_id") or "shell-nav-public"
    footer_id = family.get("shell_footer_id") or "shell-footer"
    nav_file = nav_id.replace("shell-", "", 1)        # "shell-nav-thin-statement" -> "nav-thin-statement"
    footer_file = footer_id.replace("shell-", "", 1)  # "shell-footer-minimal-line" -> "footer-minimal-line"

    # Typography-scale CSS. Declarations + a small set of classes that
    # consume the variables. New widgets author against these classes;
    # existing widgets keep their Tailwind sizing for backward compat.
    type_scale = family.get("typography_scale") or {}
    # section-y is a clamp, never a flat constant. A flat 8rem stamps
    # 250-360px dead bands between every block (a home page can end up twice
    # as tall as its content). Families keep declaring section_y_rem as
    # their "statement" intent, but the emitted value caps at 4rem desktop
    # and breathes down to 2.75rem on narrow viewports via 5vw.
    _sy_declared = float(type_scale.get('section_y_rem', 4.0) or 4.0)
    _sy_max = min(_sy_declared, 4.0)
    _sy_floor = min(2.75, _sy_max)
    section_y_clamp = f"clamp({_sy_floor}rem, 5vw, {_sy_max}rem)"
    css_vars: list = []
    if type_scale:
        css_vars = [
            f"      --type-base: {type_scale.get('base_rem', 1.0)}rem;",
            f"      --type-h1: {type_scale.get('h1_rem', 3.0)}rem;",
            f"      --type-h2: {type_scale.get('h2_rem', 2.0)}rem;",
            f"      --type-h3: {type_scale.get('h3_rem', 1.5)}rem;",
            f"      --type-section-y: {section_y_clamp};",
            f"      --type-letter-h1: {type_scale.get('letter_spacing_h1', '-0.02em')};",
            f"      --type-leading-h1: {type_scale.get('leading_h1', 1.1)};",
            f"      --type-leading-body: {type_scale.get('leading_body', 1.65)};",
        ]
    # Bake a responsive clamp() around the family's max h1/h2 so narrow
    # viewports don't overflow. Floor (lowest viewport size) preserves
    # readability at 320-375px; vw-scaling fills the middle; ceil pins
    # desktop value. Values are conservative: 2.25rem (36px) floor / 6.5vw
    # mid / max = family's declared h1_rem. h2 floor 1.75rem (28px) / 4.5vw
    # / declared h2_rem.
    h1_max = type_scale.get('h1_rem', 3.0)
    h2_max = type_scale.get('h2_rem', 2.0)
    h1_floor = 2.25 if h1_max >= 3.5 else min(h1_max, 2.25)
    h2_floor = 1.75 if h2_max >= 2.25 else min(h2_max, 1.75)
    h1_clamp = f"clamp({h1_floor}rem, 6.5vw, {h1_max}rem)"
    h2_clamp = f"clamp({h2_floor}rem, 4.5vw, {h2_max}rem)"
    css_classes: list = []
    if type_scale:
        css_classes = [
            "    body { font-size: var(--type-base, 1rem); line-height: var(--type-leading-body, 1.65); }",
            f"    .cas-type-h1 {{ font-size: {h1_clamp}; letter-spacing: var(--type-letter-h1, -0.02em); line-height: var(--type-leading-h1, 1.1); }}",
            f"    .cas-type-h2 {{ font-size: {h2_clamp}; }}",
            "    .cas-type-h3 { font-size: var(--type-h3, 1.5rem); }",
            "    .cas-type-section { padding-top: var(--type-section-y, 4rem); padding-bottom: var(--type-section-y, 4rem); }",
        ]

    # Body class for template-family-scoped CSS rules. Widgets keep their
    # Tailwind sizing (which is the safe default); CSS rules below the
    # body class can override h1/h2/section padding without touching
    # any widget. This is how the editorial-statement scale visibly takes
    # effect without retrofitting every existing widget.
    body_class = ""
    # Always-init flourish_body_class so the readability-bump branch
    # below works even when there's no family (no body_class).
    flourish_body_class = ""
    if family.get("id"):
        body_class = f"cas-template-{family['id']}"
        # Append body-class-scoped overrides so existing widget h1/h2 grow
        # to the family's scale without authoring changes.
        if type_scale:
            css_classes = css_classes + [
                # h1/h2 use clamp() so narrow viewports (320-580px) scale
                # down from the family's editorial max without overflow.
                # Upper bound preserves the desktop editorial look.
                f"    body.{body_class} h1 {{ font-size: {h1_clamp}; letter-spacing: var(--type-letter-h1, -0.02em); line-height: var(--type-leading-h1, 1.1); }}",
                f"    body.{body_class} h2 {{ font-size: {h2_clamp}; }}",
                f"    body.{body_class} h3 {{ font-size: var(--type-h3, 1.5rem); }}",
                # Scoped to main > section so nested sections never
                # double-pad (v2-template-vertical-rhythm-clamp).
                f"    body.{body_class} main > section {{ padding-top: var(--type-section-y); padding-bottom: var(--type-section-y); }}",
                # Interior page headers stay compact: the
                # family's statement h1 scale is exclusive to the home
                # hero; .cas-page-header pins interior h1s + padding so
                # real content shows above the fold at 1280x800.
                f"    body.{body_class} .cas-page-header h1 {{ font-size: clamp(1.75rem, 3.2vw, 2.5rem); }}",
                f"    body.{body_class} main > section.cas-page-header {{ padding-top: clamp(1.5rem, 2.5vw, 2rem); padding-bottom: clamp(1.5rem, 2.5vw, 2rem); }}",
            ]
        # extra_body_css: free-form CSS injected after the typography
        # rules. Used for layout-shifting rules like directory-rich's
        # 240px main-content padding on lg+ to account for the side rail.
        # Authored families control this; no escaping done here because
        # it's static curated CSS, not user input.
        extra_css = family.get("extra_body_css")
        if extra_css:
            css_classes.append(f"    {extra_css}")
        # Optional italic-serif eyebrow (item v2-mw-italic-eyebrow-with-
        # sage-dot). Default `uppercase_dim` keeps the static .cas-eyebrow
        # rule defined alongside the rest of CAS_BASE_STYLES; setting
        # `eyebrow_style: italic_serif` adds (a) a .cas-eyebrow-italic
        # utility class for new widgets to opt in directly, and (b) a
        # body-class-scoped override so existing widgets that emit
        # .cas-eyebrow / .eyebrow upgrade to the editorial pattern
        # without authoring changes. The dot uses --color-accent-earth
        # (sage in the editorial-sanctuary palette) with --color-primary
        # as a defensive fallback.
        eyebrow_style = family.get("eyebrow_style") or "uppercase_dim"
        if eyebrow_style == "italic_serif":
            css_classes.append(
                "    .cas-eyebrow-italic { display: inline-flex; align-items: center; "
                "gap: 0.625rem; font-style: italic; font-weight: 400; font-size: 0.875rem; "
                "letter-spacing: 0.04em; text-transform: none; "
                "color: var(--color-foreground-muted); font-family: var(--font-display, serif); }"
            )
            css_classes.append(
                "    .cas-eyebrow-italic::before { content: ''; width: 6px; height: 6px; "
                "border-radius: 50%; background-color: var(--color-accent-earth, var(--color-primary)); "
                "flex-shrink: 0; }"
            )
            # In the two rules below only the first segment is an f-string
            # (its brace is doubled). The segments after it are plain
            # strings, where a brace is written once: `}}` there is emitted
            # as two braces and the browser drops the rule that follows.
            # compose/tests/test_emitted_css_js_syntax.py parses every
            # emitted stylesheet for exactly this.
            css_classes.append(
                f"    body.{body_class} .cas-eyebrow, body.{body_class} .eyebrow {{ "
                "font-style: italic; font-weight: 400; font-size: 0.875rem; "
                "letter-spacing: 0.04em; text-transform: none; "
                "color: var(--color-foreground-muted); display: inline-flex; "
                "align-items: center; gap: 0.625rem; }"
            )
            css_classes.append(
                f"    body.{body_class} .cas-eyebrow::before, body.{body_class} .eyebrow::before {{ "
                "content: ''; width: 6px; height: 6px; border-radius: 50%; "
                "background-color: var(--color-accent-earth, var(--color-primary)); "
                "flex-shrink: 0; }"
            )
        # Flourish-set CSS: opt-in design accents (hover, fade-in,
        # gradient-text, etc.). Each family may declare flourish_set;
        # missing/unknown/empty -> no-op. The set's body-class scope
        # `cas-flourish-<id>` is added to the body alongside
        # cas-template-<family-id>. All sets honor prefers-reduced-motion
        # so the no-flourish default is the accessible default.
        flourish_set_id = family.get("flourish_set")
        flourish_body_class = ""
        if flourish_set_id:
            fs = get_flourish_set(flourish_set_id)
            if fs and fs.get("css"):
                css_classes.append(f"    /* Flourish set: {flourish_set_id} */")
                css_classes.append(f"    {fs['css']}")
            if fs:
                flourish_body_class = f"cas-flourish-{flourish_set_id}"

    # When spec opts in via `readability_bump: true`, emit body-class-scoped
    # CSS that bumps body to ~19px + paragraphs + strong + nudges h2 up one
    # notch. Body class `cas-readability-bump` is appended at the <body> tag
    # so rules only fire when explicitly opted in. The default 16px body is
    # too small for some audiences. Off by default so editorial sites stay
    # unchanged.
    readability_body_class = ""
    if rs.spec.readability_bump:
        readability_body_class = "cas-readability-bump"
        css_classes.append(
            "    /* Readability bump (opt-in via spec.readability_bump) */"
        )
        css_classes.append(
            "    body.cas-readability-bump { font-size: 19px; }"
        )
        css_classes.append(
            "    body.cas-readability-bump p, "
            "body.cas-readability-bump li { font-size: 1.1875rem; line-height: 1.6; }"
        )
        css_classes.append(
            "    body.cas-readability-bump strong, "
            "body.cas-readability-bump b { font-weight: 700; }"
        )
        # Bump h2 floor + max one notch so headings keep visual hierarchy
        # against the larger body. Doesn't override family-scoped h2 in
        # editorial-statement etc. (more specific selector wins), but
        # applies for any non-family-scoped rendering.
        css_classes.append(
            "    body.cas-readability-bump h2 { font-size: clamp(2rem, 4.8vw, 3.25rem); }"
        )
    # Inject body class at the existing `<body>` open tag.
    if body_class or readability_body_class:
        parts = []
        if body_class:
            parts.append(body_class)
        if flourish_body_class:
            parts.append(flourish_body_class)
        if readability_body_class:
            parts.append(readability_body_class)
        body_classes_str = " ".join(parts)
        pub = pub.replace("<body>", f'<body class="{body_classes_str}">', 1)

    pub = pub.replace("__SHELL_NAV_FILE__", nav_file)
    pub = pub.replace("__SHELL_FOOTER_FILE__", footer_file)
    pub = pub.replace("__TYPOGRAPHY_SCALE_CSS__", "\n".join(css_vars))
    pub = pub.replace("__TYPOGRAPHY_SCALE_CLASSES__", "\n".join(css_classes))

    # Self-host fonts. When
    # `_harbor_meta.self_host_fonts: true` AND the WOFF2 cache is fully
    # populated for the chosen font_pair, replace the Google Fonts
    # round-trip with a baked-in @font-face block + preload links. The
    # `_fu` middleware value is suppressed for the same build (see
    # emit_middleware_settings_js) so the runtime never emits the
    # external <link href="fonts.googleapis.com/..."> tag. Falls back to
    # the empty placeholder when the cache can't satisfy the plan.
    plan = _self_host_fonts_plan(rs)
    if plan and plan.files:
        preloads = render_preload_links(plan)
        face_css = render_font_face_css(plan)
    else:
        preloads = ""
        face_css = ""
    pub = pub.replace("__SELF_HOST_FONT_PRELOADS__", preloads)
    pub = pub.replace("__SELF_HOST_FONT_FACE__", face_css)

    return [
        ("views/layout-public.ejs", pub),
        ("views/layout-admin.ejs", adm),
    ]


_SELF_HOST_PLAN_SENTINEL = "__cas_self_host_plan_cached__"


def _self_host_fonts_plan(rs: ResolvedSpec):
    """Resolve the self-host fonts plan for this build, or None when opt-out.

    Centralizes the opt-in check + cache validation so emit_layout_files,
    emit_middleware_settings_js, and compose_files all see the same
    decision. Returns None when self-hosting is disabled OR the cache is
    incomplete (fallback path); returns a populated SelfHostFontsPlan
    when the bake should happen.

    Idempotent: the plan is cached on the ResolvedSpec after the first
    call so repeat callers don't re-walk the cache directory AND don't
    duplicate the `self_host_fonts_fallback` translation_warning.
    """
    cached = getattr(rs, _SELF_HOST_PLAN_SENTINEL, None)
    if cached is not None:
        # `False` is the explicit "we already decided: no plan" marker so
        # callers can distinguish "never computed" from "computed-as-None".
        return None if cached is False else cached

    hm = rs.spec.harbor_meta if isinstance(rs.spec.harbor_meta, dict) else {}
    if not is_self_host_enabled(hm):
        setattr(rs, _SELF_HOST_PLAN_SENTINEL, False)
        return None
    plan = plan_self_hosted_fonts(rs.font_pair)
    if plan.fallback_reason:
        # Recorded as an engine warning so SE reviewers see why the
        # build chose Google Fonts instead of self-hosting. The warning
        # is informational: the build still ships correctly with the
        # Google Fonts URL path.
        _engine_warning(rs.spec, "self_host_fonts_fallback", plan.fallback_reason)
        setattr(rs, _SELF_HOST_PLAN_SENTINEL, False)
        return None
    setattr(rs, _SELF_HOST_PLAN_SENTINEL, plan)
    return plan


def emit_widget_files(rs: ResolvedSpec) -> list:
    """Copy every widget EJS + asset under views/widgets/."""
    out = []
    for category_dir in WIDGET_DIR.iterdir():
        if not category_dir.is_dir():
            continue
        for widget_file in category_dir.iterdir():
            if widget_file.suffix not in (".ejs", ".js", ".css"):
                continue
            rel = widget_file.relative_to(WIDGET_DIR)
            content = widget_file.read_text(encoding="utf-8")
            out.append((f"views/widgets/{rel.as_posix()}", content))
    return out


_INTERNAL_PURPOSE_MARKERS = re.compile(
    r"\b(admin|booking intent|intake submission|intake response|data ?model|"
    r"lands? in|dashboard|/admin|\bcrud\b|database|back ?end|gets? stored|"
    r"stored in|persists? to|saved? to the|review queue|seed rows?)\b",
    re.IGNORECASE,
)


def _public_purpose_text(purpose: str) -> str:
    """Strip internal/operator sentences from a custom page's `purpose` before
    using it as a PUBLIC subtitle.

    `purpose` is builder-facing metadata: it often describes where a form's
    submissions land (e.g. "A simple form so visitors can request a session.
    Lands in admin's Booking Intents."). The second sentence must never appear
    on the public site. Splits into sentences, drops any that mention internal
    plumbing (admin, data models, where data is stored, etc.), and returns the
    public-safe remainder, or "" if every sentence is internal. Fixes a
    defect found by the automated build review (2026-06-07; a review loop
    that ran outside this repository and is not included in it), where
    spec.purpose jargon leaked to a public form-page subtitle.
    """
    if not purpose:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", str(purpose).strip())
    kept = [p for p in parts if p and not _INTERNAL_PURPOSE_MARKERS.search(p)]
    return " ".join(kept).strip()


def emit_custom_page_views(rs: ResolvedSpec) -> list:
    """Emit a widget-composed view for each custom page.

    Each custom page gets `views/custom/<slug>.ejs` with a hero/centered
    widget include showing the customer-declared title + purpose. The
    view is widget-composed so the v2 lint's "every page view composed
    from widgets" check still passes (see queue/worker_pg.py:_lint_v2_
    build_output). Paths that collide with archetype defaults are
    dropped by _custom_pages_filter and don't emit a file.
    """
    out = []
    for cp in _custom_pages_filter(rs):
        slug = _slug_from_path(cp["path"])
        title = cp.get("title") or _prettify(slug)
        purpose = cp.get("purpose") or ""

        # When the custom page carries a form_target and
        # we can resolve it to an actual data_model, swap the hero/centered
        # placeholder for the public-submission widget wired to the model
        # fields. The POST handler emitted by emit_routes_pages_js knows
        # the same model and validates against the same accepted field set.
        form_model = _find_data_model(rs, cp.get("form_target") or "")
        if form_model is not None:
            fields = _public_form_fields(cp, form_model)
            widget_data = {
                "heading": title,
                "description": _public_purpose_text(purpose),
                "fields": fields,
                "action": cp["path"],
                "submit_text": cp.get("submit_text") or "Send",
            }
            data_js = _to_ejs_data_literal(widget_data)
            # submitted + flash come from res.render locals wired by the
            # POST handler below; the widget reads locals.flashSuccess /
            # locals.flashError directly, and submitted via the include
            # data dict so GET-after-POST repopulates prior input.
            body = (
                "<% var _sub = (typeof submitted !== 'undefined') ? submitted : {}; %>\n"
                f"<%- include('../widgets/form/public-submission.ejs', Object.assign({data_js}, "
                "{ submitted: _sub })) %>\n"
            )
            out.append((f"views/custom/{slug}.ejs", body))
            continue

        # Fallback: informational-only custom page uses hero/centered.
        desc = _public_purpose_text(purpose)
        widget_data = {
            "eyebrow": "",
            "heading": title,
            "description": desc,
        }
        # Content-less page guard: the keeper sometimes declares a nav page
        # (e.g. /sessions, /studio) with only a title and no content, which
        # would render as a bare <h1> (a blank page, which the automated
        # build review, not part of this repository, rejected on
        # 2026-06-07). Never ship a blank page: give it a
        # warm invite + a CTA to get in touch so it is complete + on-brand.
        if not desc:
            tl = title.strip().rstrip(".")
            tl = (tl[:1].lower() + tl[1:]) if tl else "this"
            widget_data["description"] = f"Reach out any time to learn more about {tl}."
            contact_href = _primary_contact_path(rs)
            if contact_href:
                widget_data["cta_primary"] = {"text": "Get in touch", "href": contact_href}
        data_js = _to_ejs_data_literal(widget_data)
        body = f"<%- include('../widgets/hero/centered.ejs', {data_js}) %>\n"
        out.append((f"views/custom/{slug}.ejs", body))
    return out


def _primary_contact_path(rs: ResolvedSpec) -> str:
    """Best contact/booking destination for a content-less page's CTA: the
    first PUBLIC custom page with a form_target (a booking/contact form),
    else '/contact' (archetype default). A form behind auth_required is
    skipped: the CTA is shown to visitors, who would land on /login."""
    try:
        for cp in _custom_pages_filter(rs):
            if cp.get("form_target") and cp.get("path") and not cp.get("auth_required"):
                return cp["path"]
    except Exception:
        pass
    return "/contact"


def emit_error_views(rs: ResolvedSpec) -> list:
    return [
        ("views/error-404.ejs", (WIDGET_DIR / "error" / "404.ejs").read_text(encoding="utf-8")),
        ("views/error-500.ejs", (WIDGET_DIR / "error" / "500.ejs").read_text(encoding="utf-8")),
        ("views/error-csrf.ejs", (WIDGET_DIR / "error" / "csrf.ejs").read_text(encoding="utf-8")),
    ]


def emit_login_view(rs: ResolvedSpec) -> tuple:
    return ("views/login.ejs", (WIDGET_DIR / "form" / "login.ejs").read_text(encoding="utf-8"))


def emit_auth_views(rs: ResolvedSpec) -> list:
    """Emit the secondary auth surfaces (set-password, request-magic-link,
    magic-link-sent). Each view inlines the shared letterpress auth shell
    from compose/widgets/auth/letterpress.ejs. The legacy split-panel and
    centered-card shells remain available for opt-in callers. Routes that
    render them are emitted by emit_routes_auth_js."""
    return [
        ("views/set-password.ejs", (WIDGET_DIR / "auth" / "set-password.ejs").read_text(encoding="utf-8")),
        ("views/request-magic-link.ejs", (WIDGET_DIR / "auth" / "request-magic-link.ejs").read_text(encoding="utf-8")),
        ("views/magic-link-sent.ejs", (WIDGET_DIR / "auth" / "magic-link-sent.ejs").read_text(encoding="utf-8")),
        ("views/magic-link-confirm.ejs", (WIDGET_DIR / "auth" / "magic-link-confirm.ejs").read_text(encoding="utf-8")),
    ]


def emit_dashboard_view(rs: ResolvedSpec) -> tuple:
    """Emitted at views/admin/dashboard.ejs. Its sibling includes like
    `./upcoming-events.ejs` source from compose/widgets/admin/: after
    emit they live at views/widgets/admin/. Rewrite the include paths
    so EJS can resolve them at render time.
    """
    src = (WIDGET_DIR / "admin" / "dashboard.ejs").read_text(encoding="utf-8")
    # Rewrite `include('./<name>.ejs'` → `include('../widgets/admin/<name>.ejs'`
    import re
    src = re.sub(
        r"include\(\s*'\./([^']+\.ejs)'",
        r"include('../widgets/admin/\1'",
        src,
    )
    return ("views/admin/dashboard.ejs", src)


def emit_settings_view(rs: ResolvedSpec) -> tuple:
    """Emitted at views/admin/settings.ejs. Same include-path rewrite
    as emit_dashboard_view: settings-form.ejs includes upload-zone.ejs
    as a sibling in the widget dir, but at render time the settings view
    lives at views/admin/ while upload-zone.ejs lives at views/widgets/admin/.
    """
    src = (WIDGET_DIR / "admin" / "settings-form.ejs").read_text(encoding="utf-8")
    import re
    src = re.sub(
        r"include\(\s*'\./([^']+\.ejs)'",
        r"include('../widgets/admin/\1'",
        src,
    )
    return ("views/admin/settings.ejs", src)


# Admin detail-view field grouping.
# Pre-2026-05-27 the admin-detail.ejs emitter rendered every field as a flat
# stack of `label / value` rows separated only by a hairline, producing a
# phpMyAdmin-style row dump on models with 15+ fields. The grouped view bins
# fields into one of these visual sections (image / identity / description /
# marketing / links / schedule / display / other) so the admin reads more
# like a designed page than a database table.
#
# A field can opt out of inference by setting `group:` on the field spec
# explicitly (any of the keys below). Unknown explicit groups fall through
# into "other" so a typo never silently drops a field.
_ADMIN_DETAIL_GROUP_TITLES = {
    "identity": "Identity",
    "media": "Image",
    "description": "Description",
    "marketing": "Marketing",
    "links": "Links",
    "schedule": "Dates & schedule",
    "display": "Display settings",
    "other": "Other",
}
# Render order (media first so the cover image anchors the page, then
# identity facts, then prose, then operator-facing groups).
_ADMIN_DETAIL_GROUP_ORDER = (
    "media", "identity", "description", "marketing", "links",
    "schedule", "display", "other",
)
_ADMIN_DETAIL_IDENTITY_NAMES = {
    "title", "name", "slug", "subtitle", "series_label",
    "author_name", "class_name", "city", "headline",
    "company_name", "first_name", "last_name",
}
_ADMIN_DETAIL_DESCRIPTION_NAMES = {
    "description", "body", "content", "long_description", "full_text",
    "message", "excerpt", "summary", "bio", "about",
    "notes", "admin_notes",
}
_ADMIN_DETAIL_MARKETING_NAMES = {
    "accolade", "rating_callout", "price_display", "tagline", "badge",
    "callout", "promo_text",
}
_ADMIN_DETAIL_IMAGE_NAMES = (
    "image_url", "photo_url", "avatar_url", "cover_url", "thumbnail_url",
    "logo_url", "hero_image_url", "cover_image_url",
    "about_photo_url", "nav_logo_url",
)
_ADMIN_DETAIL_METADATA_NAMES = {"id", "created_at", "updated_at"}


def _field_group_for(field: dict) -> str:
    """Classify a field into one of the admin-detail UI groups.

    Resolution:
      1. Explicit `group:` on the field spec wins (any string; unknown ids
         fall through to "other" so a typo never silently drops a field).
      2. Inferred from field name + sql_type.

    Returns one of the keys in _ADMIN_DETAIL_GROUP_TITLES, or "metadata"
    for created_at/updated_at/id (rendered as a small footer, not a card).
    """
    explicit = field.get("group")
    if explicit and isinstance(explicit, str):
        g = explicit.strip().lower()
        if g in _ADMIN_DETAIL_GROUP_TITLES or g == "metadata":
            return g
        return "other"
    name = (field.get("name") or "").lower()
    st = (field.get("sql_type") or "").upper()
    if name in _ADMIN_DETAIL_METADATA_NAMES:
        return "metadata"
    if name in _ADMIN_DETAIL_IMAGE_NAMES or name.endswith("_image_url"):
        return "media"
    if name in _ADMIN_DETAIL_DESCRIPTION_NAMES:
        return "description"
    if name in _ADMIN_DETAIL_MARKETING_NAMES:
        return "marketing"
    if name in _ADMIN_DETAIL_IDENTITY_NAMES:
        return "identity"
    is_date = ("DATE" in st or "TIMESTAMP" in st or st == "TIME" or st.startswith("TIME ")) and name not in _ADMIN_DETAIL_METADATA_NAMES
    if is_date:
        return "schedule"
    if name.endswith("_url"):
        return "links"
    if _sql_type_kind(st) == "boolean" or name == "sort_order" or name.endswith("_enabled") or name.startswith("featured"):
        return "display"
    return "other"


def emit_admin_crud_views(rs: ResolvedSpec, model: dict) -> list:
    """Emit per-resource views under views/<slug>/ matching the MW v25
    convention:
      views/<slug>/index.ejs         (public listing, when public_listing_path)
      views/<slug>/detail.ejs        (public detail)
      views/<slug>/manage.ejs        (admin listing)
      views/<slug>/new.ejs           (admin create form)
      views/<slug>/edit.ejs          (admin edit form)
      views/<slug>/admin-detail.ejs  (admin read-only detail)
    """
    name = model["name"]
    slug = _slug_for(model)
    slug_js = slug.replace("-", "_")
    schema_fields = model.get("fields") or _default_fields(name)
    # Build columns for the admin list table: up to 4 display-friendly
    # fields, skipping long-body content (body/description/message/excerpt)
    # that would blow up the row height.
    #
    # A filter on `sql_type == TEXT` is wrong here, because v1
    # specs map every `type: "text"` short identifier (author_name,
    # quote, service) to sql_type TEXT. Such a filter skips every
    # short-text column on a Testimonial model and leaves only the
    # `published` boolean in the table. So this uses a NAME-based
    # skip list for known-long-body fields. Short-text identifiers
    # (name, title, author, service, email, status) surface correctly.
    _LONG_BODY_FIELD_NAMES = {
        "body", "content", "long_description", "full_text",
        "message", "excerpt", "description", "bio", "about",
        "notes", "admin_notes", "summary",
    }
    columns = []
    for f in schema_fields:
        fname = f.get("name", "")
        if fname in _LONG_BODY_FIELD_NAMES:
            continue
        # Also skip raw TEXT when the field name ends in _body/_text/_content
        # (customer-declared long-form fields not in the hardcoded set above).
        if f.get("sql_type") == "TEXT" and any(
            fname.endswith(suffix) for suffix in ("_body", "_text", "_content", "_description")
        ):
            continue
        columns.append({"key": fname, "label": _humanize(fname)})
        if len(columns) >= 4:
            break
    if not columns:
        columns = [{"key": schema_fields[0]["name"], "label": _humanize(schema_fields[0]["name"])}]

    # Admin list view: human-readable plural ("Blog Posts", "Contact Inquiries")
    resource_name_human = _humanize(name)
    resource_plural_human = _humanize(slug)  # slug is plural (e.g. blog-posts → Blog Posts)
    list_ejs = (
        "<% var resource_name_plural = '" + resource_plural_human + "'; %>\n"
        "<% var resource_slug = '" + slug + "'; %>\n"
        f"<% var columns = {_ejs_json(columns)}; %>\n"
        "<%- include('../widgets/admin/crud-list.ejs', { resource_name_plural: resource_name_plural, resource_slug: resource_slug, columns: columns, rows: rows, csrfToken: csrfToken, flashSuccess: locals.flashSuccess, flashError: locals.flashError }) %>\n"
    )

    # Form fields by schema
    image_field_names = ("image_url", "photo_url", "avatar_url", "cover_url", "thumbnail_url", "logo_url")
    form_fields_js = []
    for f in schema_fields:
        ftype = "text"
        # Resolve html_input via registry-first, fall back to heuristics.
        html_input = _html_input_for(f)
        # Translate html_input to the legacy 'type' string the crud-form
        # widget switches on. Maintains backwards compat with widgets that
        # inspect field.type === 'date' / 'time' / 'email' / etc.
        _map = {
            "textarea": "textarea",
            "input:text": "text",
            "input:email": "email",
            "input:tel": "tel",
            "input:url": "url",
            "input:number": "number",
            "input:date": "date",
            "input:time": "time",
            "input:datetime-local": "datetime-local",
            "input:checkbox": "checkbox",
            "input:color": "color",
            "input:password": "password",
            "input:hidden": "hidden",
            "upload:image": "file",
            "select:state-us": "state-us",
            "select:options": "select",
        }
        ftype = _map.get(html_input, "text")
        # JSONB columns (e.g. Event.photos) render as a JSON-array textarea.
        # The route JSON.parse()s the body before binding; the form view
        # JSON.stringify()s the row value back into the textarea.
        sql_t = (f.get("sql_type") or "").upper()
        is_jsonb = sql_t == "JSONB" or sql_t == "JSON"
        if is_jsonb:
            ftype = "textarea"
        # Pull registry metadata (pattern, placeholder, maxlength, inputmode)
        # so the widget can render richer HTML5 validation + mobile hints.
        ft_meta = _resolve_field_type(f) or {}
        form_fields_js.append({
            "name": f["name"],
            "label": _humanize(f["name"]),
            "type": ftype,
            "required": bool(f.get("required", False)),
            "pattern": ft_meta.get("pattern"),
            "placeholder": f.get("placeholder", ft_meta.get("placeholder", "")),
            "maxlength": ft_meta.get("maxlength"),
            "inputmode": ft_meta.get("inputmode"),
            "autocomplete": ft_meta.get("autocomplete"),
            "options": _select_choices(f),
            "is_jsonb": is_jsonb,
        })

    # When the model belongs_to a parent, inject a
    # synthetic `<parent>_id` field at the top of the form with a
    # dynamic-options select. The widget's `dynamic_options` branch reads
    # `locals.parent_rows` at render time so the select is populated from
    # the DB on each GET.
    bt_info = _belongs_to_info(rs, model)
    if bt_info is not None:
        _parent_model, fk_col, _parent_table, _on_delete = bt_info
        parent_label = _humanize(_parent_model["name"])
        form_fields_js.insert(0, {
            "name": fk_col,
            "label": parent_label,
            "type": "belongs_to_select",
            "required": False,
            "parent_name": _parent_model["name"],
        })

    # Emitted EJS formats Date objects from pg into ISO strings so `date` /
    # `datetime-local` inputs render valid values (otherwise a blog-post
    # update fails when `published_at` round-trips as a JS toString).
    form_ejs = (
        "<% var resource_name = '" + name + "'; %>\n"
        "<% var resource_slug = '" + slug + "'; %>\n"
        "<% var record_id = (record && record.id) ? record.id : null; %>\n"
        f"<% var fieldsTemplate = {_ejs_json(form_fields_js)}; %>\n"
        "<% var parent_rows = (typeof parent_rows !== 'undefined' && parent_rows) ? parent_rows : []; %>\n"
        "<%\n"
        "  function _fmtDateVal(v, type) {\n"
        "    if (v == null || v === '') return '';\n"
        "    if (type !== 'date' && type !== 'datetime-local') return v;\n"
        "    var d = (v instanceof Date) ? v : new Date(v);\n"
        "    if (isNaN(d.getTime())) return '';\n"
        "    var iso = d.toISOString();\n"
        "    return type === 'date' ? iso.substring(0, 10) : iso.substring(0, 16);\n"
        "  }\n"
        "  function _fmtJsonbVal(v) {\n"
        "    if (v == null || v === '') return '';\n"
        "    if (typeof v === 'string') return v;\n"
        "    try { return JSON.stringify(v, null, 2); } catch(e) { return ''; }\n"
        "  }\n"
        "  var fields = fieldsTemplate.map(function(f){\n"
        "    var raw = record ? record[f.name] : '';\n"
        "    // Materialize dynamic-options for belongs_to select from\n"
        "    // the parent_rows the router fetched; value_col='id',\n"
        "    // label_col='display'.\n"
        "    if (f.type === 'belongs_to_select') {\n"
        "      return Object.assign({}, f, {\n"
        "        type: 'select',\n"
        "        value: raw,\n"
        "        options: parent_rows.map(function(r){ return { value: r.id, label: r.display || ('#' + r.id) }; })\n"
        "      });\n"
        "    }\n"
        "    // JSONB columns (e.g. Event.photos) come back as a JS object/array\n"
        "    // from pg; stringify back to JSON text for the textarea.\n"
        "    if (f.is_jsonb) {\n"
        "      return Object.assign({}, f, {value: _fmtJsonbVal(raw)});\n"
        "    }\n"
        "    return Object.assign({}, f, {value: _fmtDateVal(raw, f.type)});\n"
        "  });\n"
        "%>\n"
        "<%- include('../widgets/admin/crud-form.ejs', { resource_name: resource_name, resource_slug: resource_slug, mode: mode, fields: fields, record_id: record_id, csrfToken: csrfToken, error: locals.error, resource_name_display: '" + resource_name_human + "' }) %>\n"
    )

    # Grouped admin-detail emitter. Pre-2026-05-27 the view was a flat
    # `label / value` stack that read like a phpMyAdmin row dump on models
    # with 15+ fields. The grouped version bins fields by _field_group_for
    # (or by an explicit `group:` annotation on the field spec) into card-
    # wrapped sections with a display-serif h2 header. Booleans render as
    # pill badges; external URL fields render as labeled link rows.
    # Empty non-boolean / non-link fields are skipped entirely so the
    # operator isn't reading walls of "-" placeholders. Entirely-empty
    # cards are skipped at render time via a sentinel `<% if (record.X ||
    # record.Y) { %>` wrapper.
    image_field_names = _ADMIN_DETAIL_IMAGE_NAMES
    # H1 source fields are still skipped from the body (they're already
    # the page title), with the exception of `subtitle` which renders as a
    # sub-header right below the H1.
    H1_SOURCE_FIELDS = {"title", "name", "author_name", "class_name", "city"}
    grouped_fields: dict = {k: [] for k in _ADMIN_DETAIL_GROUP_ORDER}
    metadata_fields: list = []
    subtitle_field = None
    for f in schema_fields:
        fname = f["name"]
        if fname == "subtitle":
            subtitle_field = f
            continue
        if fname in H1_SOURCE_FIELDS:
            continue
        g = _field_group_for(f)
        if g == "metadata":
            metadata_fields.append(f)
        else:
            grouped_fields.setdefault(g, []).append(f)

    # Top action row + header. Anchors the page with breadcrumb, Edit, and
    # Delete (Delete is the page's only <form>; sibling-not-nested, per the
    # 2026-04-22 regression). Page header uses the archetype's display
    # font (font-display class) at clamp(2rem, 4.5vw, 3rem) so it matches
    # the public site's editorial register, not the dashboard's utilitarian
    # tone.
    admin_detail_ejs = (
        "<section class='cas-admin-detail' style='background: var(--color-surface); min-height: calc(100vh - 64px); padding-top: 2.5rem; padding-bottom: 4rem;'>\n"
        "<div class='max-w-4xl mx-auto px-4 sm:px-6'>\n"
        # Top action row (Back / Edit / Delete) BEFORE any group card.
        "<div class='cas-admin-detail-actions' style='display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem;'>\n"
        "<a href='/admin/" + slug + "' class='font-body text-sm inline-flex items-center' style='color: var(--color-primary); gap: 6px; text-decoration: none;'>← Back to " + resource_plural_human + "</a>\n"
        "<div style='display: flex; gap: 0.625rem; align-items: center;'>\n"
        "<a href='/admin/" + slug + "/<%= record.id %>/edit' class='font-body inline-flex items-center' style='padding: 0.625rem 1.25rem; background-color: var(--color-primary); color: var(--color-on-primary); border-radius: 0.5rem; font-weight: 500; font-size: 0.875rem; gap: 6px; text-decoration: none; min-height: 40px;'>\n"
        "<span>Edit</span>\n"
        "<%- include('../widgets/utility/icon.ejs', { name: 'arrow-right', size: 14 }) %>\n"
        "</a>\n"
        "<form action='/admin/" + slug + "/<%= record.id %>/delete' method='POST' style='display: inline; margin: 0;' data-confirm=\"Delete this " + resource_name_human.lower() + "? This cannot be undone.\">\n"
        "<input type='hidden' name='_csrf' value='<%= csrfToken %>'>\n"
        "<button type='submit' class='font-body inline-flex items-center' style='padding: 0.625rem 1.25rem; background: transparent; border: 1px solid var(--color-error); color: var(--color-error); border-radius: 0.5rem; font-weight: 500; font-size: 0.875rem; cursor: pointer; min-height: 40px;'>Delete</button>\n"
        "</form>\n"
        "</div>\n"
        "</div>\n"
        # Page header with display-serif title + eyebrow + optional subtitle.
        "<div class='cas-admin-detail-header' style='margin-bottom: 2rem;'>\n"
        "<div class='font-body' style='font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.18em; color: var(--color-foreground-muted); margin-bottom: 0.5rem;'>" + resource_name_human + " detail</div>\n"
        "<h1 class='font-display' style='font-size: clamp(2rem, 4.5vw, 3rem); font-weight: 600; line-height: 1.1; letter-spacing: -0.02em; color: var(--color-foreground); margin: 0;'><%= record.title || record.name || record.class_name || record.city || record.author_name || ('" + resource_name_human + " #' + record.id) %></h1>\n"
    )
    if subtitle_field is not None:
        admin_detail_ejs += (
            "<% if (record.subtitle) { %>\n"
            "<p class='font-body' style='font-size: 1rem; color: var(--color-foreground-muted); margin: 0.625rem 0 0 0;'><%= record.subtitle %></p>\n"
            "<% } %>\n"
        )
    admin_detail_ejs += "</div>\n"

    # Shared CSS for the admin-detail surface. Scoped under
    # .cas-admin-detail so we don't leak typography into other admin pages.
    admin_detail_ejs += (
        "<style>\n"
        # Card + section header. The h2 already carries the font-display
        # Tailwind utility class, so we deliberately do NOT set font-family
        # here (would override font-display via specificity).
        ".cas-admin-detail .cas-admin-card { background: white; border: 1px solid var(--color-border); border-radius: 1rem; padding: 1.75rem; margin-bottom: 1.5rem; }\n"
        ".cas-admin-detail .cas-admin-card-header { font-size: 1.25rem; font-weight: 600; color: var(--color-foreground); margin: 0 0 1.25rem 0; }\n"
        ".cas-admin-detail .cas-admin-field { padding-bottom: 1.125rem; }\n"
        ".cas-admin-detail .cas-admin-field + .cas-admin-field { border-top: 1px solid var(--color-border); padding-top: 1.125rem; }\n"
        ".cas-admin-detail .cas-admin-label { font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.15em; color: var(--color-foreground-muted); margin-bottom: 0.375rem; }\n"
        ".cas-admin-detail .cas-admin-value { font-size: 0.9375rem; color: var(--color-foreground); line-height: 1.55; white-space: pre-wrap; word-wrap: break-word; }\n"
        ".cas-admin-detail .cas-admin-empty { font-size: 0.8125rem; color: var(--color-foreground-muted); font-style: italic; }\n"
        ".cas-admin-detail .cas-admin-pill { display: inline-flex; align-items: center; gap: 0.375rem; padding: 0.25rem 0.625rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600; }\n"
        ".cas-admin-detail .cas-admin-pill-yes { background: rgba(101, 163, 13, 0.12); color: #4d7c0f; }\n"
        ".cas-admin-detail .cas-admin-pill-no { background: var(--color-surface); color: var(--color-foreground-muted); font-weight: 500; border: 1px solid var(--color-border); }\n"
        ".cas-admin-detail .cas-admin-link-row { display: flex; align-items: baseline; gap: 0.875rem; padding: 0.625rem 0; border-bottom: 1px solid var(--color-border); }\n"
        ".cas-admin-detail .cas-admin-link-row:last-child { border-bottom: 0; }\n"
        ".cas-admin-detail .cas-admin-link-label { font-size: 0.8125rem; font-weight: 600; color: var(--color-foreground); min-width: 120px; }\n"
        ".cas-admin-detail .cas-admin-link-url { font-size: 0.8125rem; color: var(--color-primary); text-decoration: none; word-break: break-all; flex: 1; }\n"
        ".cas-admin-detail .cas-admin-display-grid { display: flex; flex-wrap: wrap; gap: 1rem 2.5rem; align-items: center; }\n"
        ".cas-admin-detail .cas-admin-meta-footer { display: flex; flex-wrap: wrap; gap: 1.5rem; padding: 0.875rem 1rem; font-size: 0.75rem; color: var(--color-foreground-muted); margin-top: 0.5rem; }\n"
        "</style>\n"
    )

    def _value_renderer(f: dict) -> str:
        """Per-field value renderer. Returns the EJS snippet that goes
        inside .cas-admin-value (booleans + link rows are emitted at the
        card level, not here)."""
        name = f["name"]
        st = (f.get("sql_type") or "").upper()
        is_image = name in image_field_names or name.endswith("_image_url")
        is_date = "DATE" in st and "TIME" not in st
        is_time = st == "TIME" or st.startswith("TIME ")
        is_datetime = "TIMESTAMP" in st
        if is_image:
            # Thumbnail; links to full image.
            return (
                "<a href='<%= record." + name + " %>' target='_blank' rel='noopener' style='display: inline-block; border-radius: 8px; overflow: hidden; border: 1px solid var(--color-border); background: var(--color-surface);'>\n"
                "<img src='<%= record." + name + " %>' alt='" + _humanize(name) + "' loading='lazy' style='max-width: 200px; height: auto; display: block;' />\n"
                "</a>\n"
            )
        if is_date or is_datetime:
            return (
                "<time class='cas-fmt-" + ("datetime" if is_datetime else "date") + "' datetime='<%= record." + name + " %>'><%= record." + name + " %></time>\n"
            )
        if is_time:
            return (
                "<time class='cas-fmt-time' data-time='<%= record." + name + " %>'><%= record." + name + " %></time>\n"
            )
        return "<%= record." + name + " %>\n"

    # Emit each non-empty group as a card. Cards are wrapped in
    # `<% if (record.X || record.Y || ...) { %>` so entirely-empty cards
    # don't render. The "display" group always renders (booleans carry
    # meaning when false too) and the "links" group always renders so the
    # operator sees which retailer/social slots are still empty.
    for group_id in _ADMIN_DETAIL_GROUP_ORDER:
        fields_in_group = grouped_fields.get(group_id) or []
        if not fields_in_group:
            continue
        always_render_card = group_id in ("display", "links")
        if not always_render_card:
            sentinel = " || ".join("record." + f["name"] for f in fields_in_group)
            admin_detail_ejs += "<% if (" + sentinel + ") { %>\n"
        admin_detail_ejs += (
            "<div class='cas-admin-card'>\n"
            "<h2 class='cas-admin-card-header font-display'>" + _ADMIN_DETAIL_GROUP_TITLES[group_id] + "</h2>\n"
        )

        if group_id == "links":
            # Render every link slot as a row with the label always
            # visible; the URL slot shows "Not set" italic when empty so
            # the operator immediately sees which channels need filling.
            for f in fields_in_group:
                fname = f["name"]
                flabel = _humanize(fname)
                admin_detail_ejs += (
                    "<div class='cas-admin-link-row'>\n"
                    "<span class='cas-admin-link-label'>" + flabel + "</span>\n"
                    "<% if (record." + fname + ") { %>\n"
                    "<a href='<%= record." + fname + " %>' target='_blank' rel='noopener' class='cas-admin-link-url'><%= record." + fname + " %></a>\n"
                    "<% } else { %>\n"
                    "<span class='cas-admin-empty'>Not set</span>\n"
                    "<% } %>\n"
                    "</div>\n"
                )
        elif group_id == "display":
            # Inline grid of badges + small values (booleans / sort_order /
            # *_enabled / featured_*). Booleans render as pill badges so
            # they read at a glance.
            admin_detail_ejs += "<div class='cas-admin-display-grid'>\n"
            for f in fields_in_group:
                fname = f["name"]
                flabel = _humanize(fname)
                is_bool = _field_type_kind(f) == "boolean"
                admin_detail_ejs += (
                    "<div>\n"
                    "<div class='cas-admin-label'>" + flabel + "</div>\n"
                )
                if is_bool:
                    admin_detail_ejs += (
                        "<% if (record." + fname + ") { %>\n"
                        "<span class='cas-admin-pill cas-admin-pill-yes'>● Yes</span>\n"
                        "<% } else { %>\n"
                        "<span class='cas-admin-pill cas-admin-pill-no'>○ No</span>\n"
                        "<% } %>\n"
                    )
                else:
                    admin_detail_ejs += (
                        "<% if (record." + fname + " != null && record." + fname + " !== '') { %>\n"
                        "<span class='cas-admin-value' style='font-family: ui-monospace, \"SF Mono\", Menlo, monospace; font-weight: 600;'><%= record." + fname + " %></span>\n"
                        "<% } else { %>\n"
                        "<span class='cas-admin-empty'>Not set</span>\n"
                        "<% } %>\n"
                    )
                admin_detail_ejs += "</div>\n"
            admin_detail_ejs += "</div>\n"
        else:
            # Default field rendering: label + value, skip empty.
            for f in fields_in_group:
                fname = f["name"]
                flabel = _humanize(fname)
                admin_detail_ejs += (
                    "<% if (record." + fname + ") { %>\n"
                    "<div class='cas-admin-field'>\n"
                    "<div class='cas-admin-label'>" + flabel + "</div>\n"
                    "<div class='cas-admin-value'>\n"
                    + _value_renderer(f) +
                    "</div>\n"
                    "</div>\n"
                    "<% } %>\n"
                )

        admin_detail_ejs += "</div>\n"  # close .cas-admin-card
        if not always_render_card:
            admin_detail_ejs += "<% } %>\n"

    # Metadata footer (small). Created / updated dates + record id; no
    # card wrapper so it reads as a footnote, not a section.
    if metadata_fields:
        admin_detail_ejs += "<% if (record.created_at || record.updated_at || record.id) { %>\n"
        admin_detail_ejs += "<div class='cas-admin-meta-footer'>\n"
        for f in metadata_fields:
            fname = f["name"]
            flabel = _humanize(fname)
            if fname in ("created_at", "updated_at"):
                admin_detail_ejs += (
                    "<% if (record." + fname + ") { %>\n"
                    "<div>" + flabel + ": <time class='cas-fmt-datetime' datetime='<%= record." + fname + " %>' style='color: var(--color-foreground);'><%= record." + fname + " %></time></div>\n"
                    "<% } %>\n"
                )
            elif fname == "id":
                admin_detail_ejs += (
                    "<div>" + resource_name_human + " ID: <code style='color: var(--color-foreground);'><%= record.id %></code></div>\n"
                )
        admin_detail_ejs += "</div>\n"
        admin_detail_ejs += "<% } %>\n"

    admin_detail_ejs += (
        "</div>\n"  # close .max-w-4xl
        # Client-side datetime + time formatter. Runs on every admin-detail
        # page; uses Intl.DateTimeFormat for locale-aware output in the
        # BROWSER'S local timezone. Also handles TIME columns (HH:MM:SS)
        # → "10:00 AM" without needing a Date object.
        # CSP nonce stamped via res.locals.cspNonce (typeof guard keeps the
        # tag valid when the script is rendered outside the middleware
        # chain, e.g. in pytest-level EJS-shape assertions).
        "<script<% if (typeof cspNonce !== 'undefined' && cspNonce) { %> nonce=\"<%= cspNonce %>\"<% } %>>\n"
        "(function(){\n"
        "  function fmtDate(el){\n"
        "    var v=el.getAttribute('datetime')||el.textContent.trim();\n"
        "    var d=new Date(v); if(isNaN(d.getTime()))return;\n"
        "    el.textContent=d.toLocaleDateString(undefined,{year:'numeric',month:'long',day:'numeric'});\n"
        "  }\n"
        "  function fmtDateTime(el){\n"
        "    var v=el.getAttribute('datetime')||el.textContent.trim();\n"
        "    var d=new Date(v); if(isNaN(d.getTime()))return;\n"
        "    var tz=Intl.DateTimeFormat().resolvedOptions().timeZone||'';\n"
        "    var date=d.toLocaleDateString(undefined,{year:'numeric',month:'long',day:'numeric'});\n"
        "    var time=d.toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});\n"
        "    var abbr=d.toLocaleTimeString(undefined,{timeZoneName:'short'}).split(' ').pop();\n"
        "    el.textContent=date+' \\u00B7 '+time+' '+abbr;\n"
        "  }\n"
        "  function fmtTime(el){\n"
        "    var v=(el.getAttribute('data-time')||el.textContent).trim(); if(!v)return;\n"
        "    var p=v.split(':'); var h=parseInt(p[0],10); var m=parseInt(p[1]||'0',10);\n"
        "    if(isNaN(h))return;\n"
        "    var ap=h>=12?'PM':'AM'; var hh=h%12||12;\n"
        "    el.textContent=hh+':'+(m<10?'0':'')+m+' '+ap;\n"
        "  }\n"
        "  document.querySelectorAll('.cas-fmt-date').forEach(fmtDate);\n"
        "  document.querySelectorAll('.cas-fmt-datetime').forEach(fmtDateTime);\n"
        "  document.querySelectorAll('.cas-fmt-time').forEach(fmtTime);\n"
        "})();\n"
        "</script>\n"
        "</section>\n"
    )

    views = [
        (f"views/{slug}/manage.ejs", list_ejs),
        (f"views/{slug}/new.ejs", form_ejs),
        (f"views/{slug}/edit.ejs", form_ejs),
        (f"views/{slug}/admin-detail.ejs", admin_detail_ejs),
    ]

    if model.get("public_listing_path"):
        # Public URLs use public_listing_path (e.g. /journal for BlogPost),
        # NOT the bare slug (/blog-posts) which isn't mounted.
        # Pasted into href attributes of the views assembled below.
        public_path = _html_attr(_route_path(model["public_listing_path"]).rstrip("/"))
        # Photos render via the photo-gallery-grid widget (square tiles,
        # caption on hover) rather than the card-list pattern.
        public_kind = _public_listing_kind(model)
        if public_kind == "photos":
            public_list_ejs = (
                "<section class='py-10 md:py-14' style='background-color: var(--color-background);'>\n"
                "<div class='max-w-6xl mx-auto px-4 sm:px-6'>\n"
                "<div class='text-center mb-8 md:mb-10'>\n"
                "<h1 class='font-display text-3xl md:text-4xl font-bold tracking-tight' style='color: var(--color-foreground);'>Gallery</h1>\n"
                "</div>\n"
                "<%- include('../widgets/offerings/photo-gallery-grid.ejs', { items: rows }) %>\n"
                "</div></section>\n"
            )
        elif public_kind == "reviews":
            # Reviews get a 2-up card grid with 5-star rating + body +
            # hairline-divided attribution. Much richer than the default
            # article-card-list (which hid body + showed no stars).
            public_list_ejs = (
                "<%- include('../widgets/offerings/reviews-grid.ejs', { rows: rows, heading: '" + resource_plural_human + "' }) %>\n"
            )
        else:
            # Public index view: card-list pattern. Derives
            # image/description/date from row fields. Detail links use
            # public_listing_path so they resolve against the actual mount.
            public_list_ejs = (
                "<section class='py-10 md:py-14' style='background-color: var(--color-background);'>\n"
                "<div class='max-w-6xl mx-auto px-4 sm:px-6'>\n"
                "<div class='text-center mb-8 md:mb-10'>\n"
                "<h1 class='font-display text-3xl md:text-4xl font-bold tracking-tight' style='color: var(--color-foreground);'>" + resource_plural_human + "</h1>\n"
                "</div>\n"
                "<% if (rows.length === 0) { %>\n"
                "<p class='font-body text-center' style='color: var(--color-foreground-muted);'>Nothing here yet.</p>\n"
                "<% } else { %>\n"
                "<div class='grid grid-cols-1 md:grid-cols-2 gap-6'>\n"
                "<% rows.forEach(function(row) { %>\n"
                "<article class='rounded-xl border overflow-hidden' style='background-color: var(--color-surface); border-color: var(--color-border);'>\n"
                "<% if (row.image_url) { %>\n"
                "<a href='" + public_path + "/<%= row.id %>'><img loading='lazy' src='<%= row.image_url %>' alt='<%= row.title || row.name || \"\" %>' class='w-full object-cover' style='aspect-ratio: 16/9;' /></a>\n"
                "<% } %>\n"
                "<div class='p-6'>\n"
                "<% var dt = row.event_date || row.published_at || row.created_at; %>\n"
                "<% if (dt) { %>\n"
                "<div class='font-body text-xs uppercase tracking-widest mb-2' style='color: var(--color-foreground-muted);'>\n"
                "<%= new Date(dt).toLocaleDateString('en-US', {year:'numeric', month:'long', day:'numeric'}) %>\n"
                "<% if (row.event_time) { %> · <%= (function(t){ var p=String(t).split(':'); var h=parseInt(p[0]||0,10); var m=p[1]||'00'; var ap=h>=12?'PM':'AM'; var hh=h%12||12; return hh+':'+m+' '+ap; })(row.event_time) %><% } %>\n"
                "</div>\n"
                "<% } %>\n"
                "<h2 class='font-display text-xl font-bold mb-2' style='color: var(--color-foreground);'>\n"
                "<a href='" + public_path + "/<%= row.id %>' style='color: inherit; text-decoration: none;'><%= row.title || row.name || row.class_name || row.city || row.author_name || 'Untitled' %></a>\n"
                "</h2>\n"
                "<% var summary = row.excerpt || row.description || row.summary || row.quote; %>\n"
                "<% if (summary) { %>\n"
                "<p class='font-body text-sm leading-relaxed mb-4' style='color: var(--color-foreground-muted);'><%= (summary || '').substring(0, 200) %><% if ((summary || '').length > 200) { %>…<% } %></p>\n"
                "<% } %>\n"
                "<a href='" + public_path + "/<%= row.id %>' class='inline-flex items-center font-body text-sm font-medium' style='color: var(--color-primary);'>Read more →</a>\n"
                "</div>\n"
                "</article>\n"
                "<% }); %>\n"
                "</div>\n"
                "<% } %>\n"
                "</div></section>\n"
            )
        public_detail_ejs = (
            "<section class='py-10 md:py-14' style='background-color: var(--color-background);'>\n"
            "<div class='max-w-3xl mx-auto px-4 sm:px-6'>\n"
            "<a href='" + public_path + "' class='font-body text-sm' style='color: var(--color-primary);'>← Back to " + resource_plural_human + "</a>\n"
            "<% if (record.hero_image_url) { %>\n"
            "<figure style='margin: 1.5rem 0 2rem 0;'>\n"
            "<img src='<%= record.hero_image_url %>' alt='<%= record.hero_image_alt || record.title || \"\" %>' class='w-full rounded-xl' style='height: auto; max-height: 640px; min-height: 280px; object-fit: cover; object-position: center;' loading='eager' fetchpriority='high' />\n"
            "<% if (record.hero_image_alt && record.hero_image_alt !== record.title) { %>\n"
            "<figcaption class='font-body text-center' style='color: var(--color-foreground-muted); font-style: italic; font-size: 1rem; margin-top: 0.875rem;'><%= record.hero_image_alt %></figcaption>\n"
            "<% } %>\n"
            "</figure>\n"
            "<% } else if (record.photo_url) { %>\n"
            "<div class='my-6 flex justify-center'>\n"
            "<div style='width: 16rem; height: 16rem; aspect-ratio: 1/1; overflow: hidden; border-radius: 9999px; background-color: var(--color-surface);'>\n"
            "<img src='<%= record.photo_url %>' alt='<%= record.name || record.title || \"\" %>' class='w-full h-full object-cover' />\n"
            "</div></div>\n"
            "<% } else if (record.image_url) { %><img src='<%= record.image_url %>' alt='<%= record.title || \"\" %>' class='w-full rounded-xl my-6' /><% } %>\n"
            "<% if (record.eyebrow) { %>\n"
            "<div class='font-body uppercase tracking-widest mb-3 mt-6' style='color: var(--color-accent-earth, var(--color-primary)); font-size: 0.875rem; font-weight: 700; letter-spacing: 0.18em;'>\n"
            "<%= record.eyebrow %>\n"
            "</div>\n"
            "<% } else { %>\n"
            "<% var dt = record.event_date || record.published_at || (!record.photo_url && record.created_at); %>\n"
            "<% if (dt) { %>\n"
            "<div class='font-body text-xs uppercase tracking-widest mb-3 mt-6' style='color: var(--color-foreground-muted);'>\n"
            "<%= new Date(dt).toLocaleDateString('en-US', {year:'numeric', month:'long', day:'numeric'}) %>\n"
            "<% if (record.event_time) { %> · <%= (function(t){ var p=String(t).split(':'); var h=parseInt(p[0]||0,10); var m=p[1]||'00'; var ap=h>=12?'PM':'AM'; var hh=h%12||12; return hh+':'+m+' '+ap; })(record.event_time) %><% } %>\n"
            "<% if (record.end_date) { %> thru <%= new Date(record.end_date).toLocaleDateString('en-US', {year:'numeric', month:'short', day:'numeric'}) %><% if (record.end_time) { %> <%= (function(t){ var p=String(t).split(':'); var h=parseInt(p[0]||0,10); var m=p[1]||'00'; var ap=h>=12?'PM':'AM'; var hh=h%12||12; return hh+':'+m+' '+ap; })(record.end_time) %><% } %><% } %>\n"
            "</div>\n"
            "<% } %>\n"
            "<% } %>\n"
            "<% if (record.role) { %>\n"
            "<div class='font-body text-xs font-bold uppercase tracking-widest mb-3 mt-6 text-center' style='color: var(--color-primary);'><%= record.role %></div>\n"
            "<% } %>\n"
            "<h1 class='font-display text-3xl md:text-4xl font-bold mb-6<%= record.photo_url ? \" text-center\" : \"\" %>' style='color: var(--color-foreground);'><%= record.title || record.name || record.class_name || record.city || record.author_name || 'Untitled' %></h1>\n"
            "<% if (record.venue) { %>\n"
            "<p class='font-body' style='color: var(--color-foreground-muted); font-size: 1.25rem; line-height: 1.5; margin: 0 0 2rem 0;'>\n"
            "<% if (record.venue_url) { %><a href='<%= record.venue_url %>' target='_blank' rel='noopener' style='color: var(--color-primary); border-bottom: 1px solid var(--color-accent-warm, var(--color-primary));'><%= record.venue %></a><% } else { %><%= record.venue %><% } %>\n"
            "</p>\n"
            "<% } %>\n"
            "<% var _spec = record.specialties; if (typeof _spec === 'string') { try { _spec = JSON.parse(_spec); } catch(e) { _spec = _spec.split(',').map(function(s){return s.trim();}); } } _spec = _spec || []; %>\n"
            "<% if (_spec.length > 0) { %>\n"
            "<div class='flex flex-wrap gap-2 mb-6<%= record.photo_url ? \" justify-center\" : \"\" %>'>\n"
            "<% _spec.forEach(function(s){ %>\n"
            "<span class='font-body text-xs px-3 py-1 rounded-full' style='background-color: var(--color-surface); color: var(--color-foreground); border: 1px solid var(--color-border);'><%= s %></span>\n"
            "<% }); %>\n"
            "</div>\n"
            "<% } %>\n"
            "<% if (record.intro) { %>\n"
            "<div class='font-body' style='font-size: 1.375rem; line-height: 1.7; color: var(--color-foreground); margin: 0 0 2rem 0; font-style: italic;'><%= record.intro %></div>\n"
            "<% } %>\n"
            "<% var _photos = record.photos; if (typeof _photos === 'string') { try { _photos = JSON.parse(_photos); } catch(e) { _photos = []; } } _photos = Array.isArray(_photos) ? _photos : []; %>\n"
            "<% if (_photos.length > 0) { %>\n"
            "<%- include('../widgets/content/photo-slideshow.ejs', { photos: _photos, heading_alt: record.title || 'Gallery photo' }) %>\n"
            "<% } %>\n"
            "<% if (record.bio) { %><div class='font-body text-base leading-relaxed whitespace-pre-wrap' style='color: var(--color-foreground);'><%= record.bio %></div>\n"
            "<% } else if (record.body) { %><div class='font-body text-base leading-relaxed whitespace-pre-wrap' style='color: var(--color-foreground); font-size: 1.125rem; line-height: 1.7;'><%= record.body %></div>\n"
            "<% } else if (record.description) { %><div class='font-body text-base leading-relaxed whitespace-pre-wrap' style='color: var(--color-foreground);'><%= record.description %></div><% } %>\n"
            "<% if (record.video_url) { %>\n"
            "<%- include('../widgets/content/video-embed-nocookie.ejs', { video_url: record.video_url, caption: record.video_caption || '' }) %>\n"
            "<% } %>\n"
            "<% if (record.closing_quote) { %>\n"
            "<blockquote style='font-family: var(--font-display, Georgia, serif); font-size: 1.5rem; font-style: italic; color: var(--color-foreground); line-height: 1.4; border-left: 3px solid var(--color-accent-warm, var(--color-primary)); padding: 1rem 0 1rem 1.5rem; margin: 2.5rem 0 0 0;'><%= record.closing_quote %></blockquote>\n"
            "<% } %>\n"
            "<% if (record.location) { %>\n"
            "<div class='mt-6 p-4 rounded-xl border' style='background-color: var(--color-surface); border-color: var(--color-border);'>\n"
            "<div class='font-body text-xs font-bold uppercase tracking-widest mb-1' style='color: var(--color-foreground-muted);'>Location</div>\n"
            "<div class='font-body text-sm' style='color: var(--color-foreground);'><%= record.location %></div>\n"
            "</div><% } %>\n"
            "<% if (record.instagram_url) { %>\n"
            "<div class='mt-6<%= record.photo_url ? \" text-center\" : \"\" %>'><a href='<%= record.instagram_url %>' target='_blank' rel='noopener' class='font-body text-sm' style='color: var(--color-primary);'>Instagram ↗</a></div>\n"
            "<% } %>\n"
            "<% if (record.registration_url) { %>\n"
            "<div class='mt-6'><a href='<%= record.registration_url %>' target='_blank' rel='noopener' class='inline-flex items-center font-body font-medium px-6 py-3 rounded-md' style='background-color: var(--color-primary); color: var(--color-on-primary);'>Register →</a></div>\n"
            "<% } %>\n"
            "</div></section>\n"
        )
        views.append((f"views/{slug}/index.ejs", public_list_ejs))
        views.append((f"views/{slug}/detail.ejs", public_detail_ejs))
    return views


def emit_public_assets(rs: ResolvedSpec) -> list:
    """Static files under public/: style.css, manifest.json, favicon.svg,
    logo.svg, the footer attribution mark, and the browser scripts. (robots
    and the sitemap are served by routes/seo.js, not from here.)"""
    # style.css: minimal Tailwind-like rules the widgets use (flex and grid
    # utilities, spacing), enough for the widget classes to work without a
    # build step. The stylesheet itself is templates/public-style.css.
    style_css = PUBLIC_STYLE_CSS_PATH.read_text(encoding="utf-8")
    manifest = json.dumps({
        "name": rs.site_name,
        "short_name": rs.site_name[:12],
        "start_url": "/",
        "display": "standalone",
        "background_color": rs.palette_vars.get("--color-background", "#ffffff"),
        "theme_color": rs.palette_vars["--color-primary"]
    }, indent=2)
    # /robots.txt is served by routes/seo.js so the body can reference the
    # live origin via the request,
    # rather than baking a host-less file at build time. emit_routes_seo_js
    # owns the URL; express.static would shadow the route if a static
    # public/robots.txt still existed here.
    # Logo + favicon fill colors default to the resolved palette's primary +
    # on-primary so the brand mark matches the rest of the site. The customer
    # can override via `settings.brand_logo_color` (background fill) and
    # `settings.brand_logo_text_color` (initial letters) when their wordmark
    # uses a different swatch than the site's primary CTA color (e.g. a
    # dark site whose logo wants a lighter accent). Only #RGB / #RRGGBB
    # hex strings are accepted; anything else silently falls back to the
    # palette so a typo can't break SVG rendering.
    # Roadmap item: v2-favicon-logo-uses-customer-palette.
    def _valid_logo_hex(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        candidate = value.strip()
        if re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", candidate):
            return candidate
        return None

    _logo_bg_fill = (
        _valid_logo_hex(rs.spec.settings.get("brand_logo_color"))
        or rs.palette_vars["--color-primary"]
    )
    _logo_text_fill = (
        _valid_logo_hex(rs.spec.settings.get("brand_logo_text_color"))
        or rs.palette_vars["--color-on-primary"]
    )
    favicon_svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="' + _logo_bg_fill + '"/><text x="50%" y="54%" text-anchor="middle" font-family="system-ui" font-size="34" font-weight="700" fill="' + _logo_text_fill + '">' + _html_attr(rs.site_name[:1].upper()) + '</text></svg>'
    # Nav logo: 2-letter monogram squircle in primary/on-primary. Rendered at
    # 40×40 in the nav wrapper (clipped to a circle by object-fit:cover) and at
    # larger sizes elsewhere (footer, emails). Font-family matches the display
    # font's serif/sans character so the logo's letterforms feel on-brand.
    #
    # Initials must skip non-alphanumeric "words" (& in "Wren & Rye", - in
    # "Foo - Bar", + in "A + B") because:
    #   1. "&" baked into SVG text content is invalid XML and browsers
    #      render the SVG as a broken-image placeholder (observed
    #      on a wren-rye-cafe local rebuild: logo
    #      rendered as a generic mountain icon, not "WR").
    #   2. Even if we escaped it as "&amp;", the visual result ("W&") is
    #      not what a customer expects from a "Wren & Rye" brand mark.
    # Filter to words whose first character is alphanumeric, then take the
    # first letter of the first two real words.
    _logo_name_words = [
        w for w in re.split(r"\s+", rs.site_name or "")
        if w and w[0].isalnum()
    ]
    if len(_logo_name_words) >= 2:
        _logo_initials = (_logo_name_words[0][0] + _logo_name_words[1][0]).upper()
    elif _logo_name_words:
        _logo_initials = _logo_name_words[0][:2].upper() if len(_logo_name_words[0]) >= 2 else _logo_name_words[0][:1].upper()
    else:
        _logo_initials = "•"
    _is_serif = bool(rs.font_pair.get("display", {}).get("is_serif", False))
    _logo_font = '"Playfair Display","Cormorant Garamond",Georgia,serif' if _is_serif else '"Archivo Black","Helvetica Neue",Arial,sans-serif'
    _logo_fs = "58" if len(_logo_initials) == 1 else "46"
    # Belt-and-suspenders: even with the alnum filter above, run XML escape
    # on the initials so any future change to the initials logic that
    # admits & / < / > can't break SVG rendering.
    from xml.sax.saxutils import escape as _xml_escape
    _logo_initials_escaped = _xml_escape(_logo_initials)
    logo_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128">'
        '<rect width="128" height="128" rx="24" fill="' + _logo_bg_fill + '"/>'
        '<text x="50%" y="50%" text-anchor="middle" dominant-baseline="central" '
        'font-family=' + "'" + _logo_font + "' " +
        'font-size="' + _logo_fs + '" font-weight="700" letter-spacing="-0.02em" '
        'fill="' + _logo_text_fill + '">'
        + _logo_initials_escaped +
        '</text></svg>'
    )
    # Upload-zone client runtime (from compose/widgets/utility/upload-zone.js)
    upload_zone_js = (WIDGET_DIR / "utility" / "upload-zone.js").read_text(encoding="utf-8")
    # Form validation client runtime (from compose/widgets/utility/cas-form-validate.js)
    form_validate_js = (WIDGET_DIR / "utility" / "cas-form-validate.js").read_text(encoding="utf-8")
    # Scroll-reveal runtime (from compose/widgets/utility/cas-reveal.js)
    reveal_js = (WIDGET_DIR / "utility" / "cas-reveal.js").read_text(encoding="utf-8")
    # Client-side <time> formatter runtime. Public + admin widgets emit
    # <time class="cas-fmt-*" datetime="<ISO>"> markup so dates render in
    # the visitor's locale + timezone (the Railway container lives in UTC).
    cas_fmt_js = (WIDGET_DIR / "utility" / "cas-fmt.js").read_text(encoding="utf-8")
    # CAS lighthouse mark for the footer attribution. Mirrors
    # web/public/brand-harbor/logo-mark-nav.svg so the keeper site silently
    # advertises CAS via the shared shell/cas-attribution.ejs partial.
    cas_lighthouse_svg = _CAS_LIGHTHOUSE_SVG
    return [
        ("public/style.css", style_css),
        ("public/manifest.json", manifest),
        ("public/favicon.svg", favicon_svg),
        ("public/logo.svg", logo_svg),
        ("public/brand/cas-lighthouse.svg", cas_lighthouse_svg),
        ("public/js/upload-zone.js", upload_zone_js),
        ("public/js/cas-form-validate.js", form_validate_js),
        ("public/js/cas-reveal.js", reveal_js),
        ("public/js/cas-fmt.js", cas_fmt_js),
        ("public/uploads/.gitkeep", ""),
    ]


# Vendored copy of web/public/brand-harbor/logo-mark-nav.svg. Kept inline
# so the v2 build doesn't depend on the web/ tree at compose time and is
# byte-identical across rebuilds.
_CAS_LIGHTHOUSE_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="Craft and Ship" shape-rendering="geometricPrecision">
  <title>Craft and Ship</title>
  <defs>
    <radialGradient id="lamp-glow" cx="50%" cy="50%" r="60%">
      <stop offset="0%" stop-color="#FFF6DC" stop-opacity="1"></stop>
      <stop offset="35%" stop-color="#FFE2A6" stop-opacity="1"></stop>
      <stop offset="75%" stop-color="#F0B048" stop-opacity="1"></stop>
      <stop offset="100%" stop-color="#C68A3A" stop-opacity="0.95"></stop>
    </radialGradient>
    <linearGradient id="beam" x1="0" x2="1" y1="0" y2="0">
      <stop offset="0%" stop-color="#FFE2A6" stop-opacity="0.9"></stop>
      <stop offset="45%" stop-color="#F0B048" stop-opacity="0.55"></stop>
      <stop offset="100%" stop-color="#C68A3A" stop-opacity="0"></stop>
    </linearGradient>
    <linearGradient id="wall" x1="0" x2="1" y1="0" y2="0">
      <stop offset="0%" stop-color="#EAD4A6"></stop>
      <stop offset="55%" stop-color="#EAD4A6"></stop>
      <stop offset="100%" stop-color="#C9A871"></stop>
    </linearGradient>
  </defs>

  <!-- Beam -->
  <path d="M 36 15 L 64 2 L 64 28 Z" fill="url(#beam)"></path>

  <!-- Base shadow -->
  <rect x="19" y="50" width="22" height="6" fill="#4A3A2C"></rect>

  <!-- Tower wall -->
  <path d="M 19 50 L 23 24 L 37 24 L 41 50 Z" fill="url(#wall)"></path>

  <!-- Gallery cap -->
  <rect x="23" y="20" width="14" height="4" fill="#3A2A1F"></rect>

  <!-- Lamp room (ink) -->
  <path d="M 24 20 L 24 11.5 Q 24 7.5 30 6.5 Q 36 7.5 36 11.5 L 36 20 Z" fill="#3A2A1F"></path>

  <!-- Lamp glow -->
  <rect x="25" y="12.5" width="10" height="6.5" fill="url(#lamp-glow)"></rect>

  <!-- Gallery line -->
  <rect x="25" y="15.5" width="10" height="0.5" fill="#3A2A1F" opacity="0.85"></rect>

  <!-- Antenna -->
  <rect x="29.6" y="3.5" width="0.8" height="3.4" fill="#2F2A24"></rect>
  <circle cx="30" cy="3.2" r="0.7" fill="#2F2A24"></circle>
</svg>
'''


def _hex_to_rgb(hex_str: str, fallback: tuple = (17, 17, 17)) -> tuple:
    """Tolerant `#rrggbb` / `#rgb` parser for PIL color tuples."""
    if not hex_str or not isinstance(hex_str, str):
        return fallback
    s = hex_str.strip().lstrip("#")
    try:
        if len(s) == 3:
            return (int(s[0] * 2, 16), int(s[1] * 2, 16), int(s[2] * 2, 16))
        if len(s) == 6:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        pass
    return fallback


def _logo_initials_from_site_name(site_name: str) -> str:
    """Extract 1-2 letter monogram. Mirrors emit_public_assets's logic
    (alnum-first words only) so og-default and /logo.svg use the same
    initials. Examples: "Wren & Rye" -> "WR"; "Fern" -> "FE"; "" -> "•"."""
    words = [
        w for w in re.split(r"\s+", site_name or "")
        if w and w[0].isalnum()
    ]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if words:
        return (words[0][:2] if len(words[0]) >= 2 else words[0][:1]).upper()
    return "•"


def _strip_accent_brace_tokens(text: str) -> str:
    """Remove italic-accent brace markers ({Word} -> Word) from copy that
    feeds image generators. The braces are a template convention for
    italic accents; leaked into a drawn image they render literally
    ("Sample {Name}") and a broken default share card is worse
    than none (v2-link-previews-og)."""
    return re.sub(r"[{}]", "", text or "")


# Longest footer line drawn on the social preview card.
_OG_META_LINE_MAX = 90


def emit_og_default_image(rs: ResolvedSpec):
    """Render a 1200x630 social-preview JPEG from brand mark + identity.

    Layout: large circular monogram on the left, italic serif site name +
    sub-line on the right, palette-tinted radial halo behind the monogram,
    hairline + meta footer. Used as the default `og:image` for every page;
    pages can override per-item via the layout's `ogImage` local.

    Returns ("public/brand/og-default.jpg", bytes) or None when PIL is not
    available (graceful degrade: the layout falls back to the default
    /favicon.svg via twitter:image's tolerance for missing files).
    """
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except ImportError:
        return None

    W, H = 1200, 630
    palette = rs.palette_vars
    bg = _hex_to_rgb(palette.get("--color-background", "#ffffff"), (255, 255, 255))
    primary = _hex_to_rgb(palette.get("--color-primary", "#111111"), (17, 17, 17))
    on_primary = _hex_to_rgb(palette.get("--color-on-primary", "#ffffff"), (255, 255, 255))
    fg = _hex_to_rgb(palette.get("--color-foreground", "#111111"), (17, 17, 17))
    border = _hex_to_rgb(palette.get("--color-border", "#e5e5e5"), (229, 229, 229))
    muted = _hex_to_rgb(
        palette.get("--color-foreground-muted", "#666666"), (102, 102, 102)
    )

    img = Image.new("RGB", (W, H), bg)

    # Soft radial halo using primary at low alpha. Drawn into an RGBA layer
    # then alpha-composited onto the base. Two overlapping ellipses blurred
    # with GaussianBlur to fake a soft radial gradient (PIL has no native
    # radial gradient).
    halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    halo_draw = ImageDraw.Draw(halo)
    halo_color = primary + (38,)  # ~15% alpha
    cx, cy = 360, H // 2
    halo_draw.ellipse(
        [cx - 320, cy - 320, cx + 320, cy + 320], fill=halo_color
    )
    halo = halo.filter(ImageFilter.GaussianBlur(radius=120))
    img.paste(halo, (0, 0), halo)

    draw = ImageDraw.Draw(img)

    # Logo disc: 340px diameter circle in primary. Initials in on-primary.
    disc_size = 340
    disc_x = 110
    disc_y = (H - disc_size) // 2
    draw.ellipse(
        [disc_x, disc_y, disc_x + disc_size, disc_y + disc_size],
        fill=primary,
    )

    initials = _logo_initials_from_site_name(rs.site_name)

    # Try a serif/sans display font; fall back to PIL default font when no
    # system fonts are available. Truetype paths cover both macOS test
    # environments and the Railway Linux worker image (DejaVu).
    is_serif = bool(rs.font_pair.get("display", {}).get("is_serif", False))
    serif_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
        "/Library/Fonts/Georgia.ttf",
    ]
    sans_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    body_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]

    def _load(paths, size):
        for p in paths:
            try:
                return ImageFont.truetype(p, size=size)
            except (OSError, IOError):
                continue
        try:
            return ImageFont.load_default()
        except Exception:
            return None

    monogram_font = _load(serif_paths if is_serif else sans_paths, 180)
    title_font = _load(serif_paths if is_serif else sans_paths, 80)
    sub_font = _load(body_paths, 36)
    meta_font = _load(body_paths, 24)

    def _text_size(d, text, font):
        if not text or not font:
            return (0, 0)
        try:
            l, t, r, b = d.textbbox((0, 0), text, font=font)
            return (r - l, b - t)
        except Exception:
            try:
                return d.textsize(text, font=font)
            except Exception:
                return (len(text) * 12, 16)

    if monogram_font:
        tw, th = _text_size(draw, initials, monogram_font)
        draw.text(
            (disc_x + (disc_size - tw) // 2, disc_y + (disc_size - th) // 2 - 18),
            initials,
            fill=on_primary,
            font=monogram_font,
        )

    # Right panel: site name + subtitle.
    right_x = disc_x + disc_size + 80
    right_max_w = W - right_x - 90
    # Strip italic-accent brace tokens ({Word}) before drawing: the image
    # generator would otherwise render the literal braces into the card
    # ("Sample {Name}"), and a broken default card is worse than
    # none (v2-link-previews-og).
    title_text = _strip_accent_brace_tokens(rs.site_name or "Site")[:60]
    settings = rs.spec.settings or {}
    subtitle_raw = (
        settings.get("hero_tagline")
        or settings.get("site_description")
        or settings.get("hero_description")
        or ""
    )
    subtitle = _strip_accent_brace_tokens(subtitle_raw)[:140].strip()

    # Wrap title across up to 2 lines for long brand names.
    def _wrap(text, font, max_w):
        if not text or not font:
            return [text or ""]
        words = text.split()
        if not words:
            return [""]
        lines = []
        cur = words[0]
        for w in words[1:]:
            trial = cur + " " + w
            if _text_size(draw, trial, font)[0] <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        return lines

    if title_font:
        title_lines = _wrap(title_text, title_font, right_max_w)[:2]
        line_h = _text_size(draw, "Hg", title_font)[1] + 12
        title_block_h = line_h * len(title_lines)
    else:
        title_lines = [title_text]
        line_h = 92
        title_block_h = line_h

    if sub_font and subtitle:
        sub_lines = _wrap(subtitle, sub_font, right_max_w)[:3]
        sub_line_h = _text_size(draw, "Hg", sub_font)[1] + 8
        sub_block_h = sub_line_h * len(sub_lines)
    else:
        sub_lines = []
        sub_line_h = 0
        sub_block_h = 0

    total_h = title_block_h + (24 if sub_block_h else 0) + sub_block_h
    cur_y = (H - total_h) // 2
    if title_font:
        for line in title_lines:
            draw.text((right_x, cur_y), line, fill=fg, font=title_font)
            cur_y += line_h
    if sub_lines and sub_font:
        cur_y += 24
        for line in sub_lines:
            draw.text((right_x, cur_y), line, fill=muted, font=sub_font)
            cur_y += sub_line_h

    # Hairline footer: 1px border line and meta line with site URL or
    # business name. Footer kept narrow so screenshots in tweet/iMessage
    # crop nicely without losing the brand block.
    draw.line([(80, H - 100), (W - 80, H - 100)], fill=border, width=1)
    meta_line = (
        (settings.get("site_url") or "")
        .replace("https://", "")
        .replace("http://", "")
        .rstrip("/")
    )
    if not meta_line:
        meta_line = settings.get("business_name") or rs.site_name
    # One line of the card: Pillow draws the whole string, so an unbounded
    # value is an image of unbounded width.
    meta_line = meta_line[:_OG_META_LINE_MAX]
    if meta_font and meta_line:
        draw.text((80, H - 70), meta_line, fill=muted, font=meta_font)

    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88, optimize=True, progressive=True)
    return ("public/brand/og-default.jpg", buf.getvalue())


def emit_env_example(rs: ResolvedSpec) -> tuple:
    # SESSION_SECRET is left EMPTY on purpose. A placeholder value here gets
    # copied into real deployments and becomes a known signing key; an empty
    # one makes server.js refuse to start in production and fall back to a
    # random per-process secret in development.
    contents = f'''DATABASE_URL=postgresql://user@localhost:5432/{rs.spec.app_name.replace("-","_")}
# Required in production (16+ characters). Generate one with:
#   node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
SESSION_SECRET=
PORT=4000
NODE_ENV=development
ADMIN_EMAIL={rs.spec.admin_email}
# Optional. Without RESEND_API_KEY no mail is sent: form notifications are
# skipped and a requested login link is written to the server log instead.
# RESEND_API_KEY=
# RESEND_FROM_DOMAIN=
# SITE_NAME=
'''
    return (".env.example", contents)


def emit_railwayignore(rs: ResolvedSpec) -> tuple:
    return (".railwayignore", "node_modules\n.env\n.DS_Store\n")


def emit_spec_json(rs: ResolvedSpec) -> tuple:
    """Emit the spec as-built into the output dir for audit / re-build."""
    out = {
        "app_name": rs.spec.app_name,
        "archetype": rs.spec.archetype,
        "palette": rs.spec.palette,
        "font_pair": rs.spec.font_pair,
        "admin_email": rs.spec.admin_email,
        "settings": rs.spec.settings,
        "data_models": rs.data_models,
        "pages": rs.spec.pages,
        "seed_content": rs.spec.seed_content,
        "_resolved": {
            "archetype_id": rs.archetype["id"],
            "palette_id": rs.palette["id"],
            "font_pair_id": rs.font_pair["id"],
        }
    }
    return ("spec.json", json.dumps(out, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Top-level build
# ---------------------------------------------------------------------------

def compose_files(rs: ResolvedSpec) -> List[tuple]:
    """Return list of (path, contents) for the full generated app."""
    files: List[tuple] = []

    files.append(emit_package_json(rs))
    files.append(emit_schema_sql(rs))
    files.append(emit_seed_sql(rs))
    files.extend(emit_ops_scripts(rs))
    files.append(emit_server_js(rs))
    files.append(emit_db_js(rs))
    files.append(emit_db_migrate_js(rs))
    files.append(emit_middleware_logging_js(rs))
    files.append(emit_middleware_auth_js(rs))
    files.append(emit_middleware_csrf_js(rs))
    files.append(emit_middleware_rate_limit_js(rs))
    files.append(emit_middleware_fields_js(rs))
    files.append(emit_middleware_settings_js(rs))
    files.append(emit_middleware_slug_aliases_js(rs))
    files.append(emit_services_notify_js(rs))
    files.append(emit_services_health_js(rs))
    files.append(emit_routes_auth_js(rs))
    files.append(emit_routes_admin_dashboard_js(rs))
    files.append(emit_routes_admin_settings_js(rs))
    files.append(emit_routes_upload_js(rs))
    files.append(emit_routes_legal_js(rs))
    files.append(emit_routes_seo_js(rs))
    files.append(emit_routes_pages_js(rs))

    for model in rs.data_models:
        files.append(emit_routes_resource_js(rs, model))

    # Views
    files.extend(emit_layout_files(rs))
    files.extend(emit_widget_files(rs))
    files.extend(emit_error_views(rs))
    files.extend(_emit_legal_views(rs))
    files.extend(emit_custom_page_views(rs))  # views of the custom pages
    files.append(emit_login_view(rs))
    files.extend(emit_auth_views(rs))
    files.append(emit_dashboard_view(rs))
    files.append(emit_settings_view(rs))

    for page_id, layout in rs.page_layouts.items():
        files.append(emit_page_view(rs, page_id, layout))
        # Per-item detail view, when the page's listing widget warrants
        # click-through (resource-list-card-grid). Skips reviews-grid +
        # service-area-list + photo-gallery-grid pages where each card
        # is the full content.
        detail = emit_page_detail_view(rs, page_id, layout)
        if detail[0]:
            files.append(detail)

    for model in rs.data_models:
        files.extend(emit_admin_crud_views(rs, model))

    # Public assets
    files.extend(emit_public_assets(rs))

    # Self-hosted font WOFF2s. Only emits
    # when `_harbor_meta.self_host_fonts: true` AND the WOFF2 cache at
    # compose/data/fonts/ has every (family, weight) the font_pair needs.
    # When the cache is incomplete, _self_host_fonts_plan returns None
    # and falls back to the Google Fonts URL path (it records the
    # `self_host_fonts_fallback` engine warning).
    _font_plan = _self_host_fonts_plan(rs)
    if _font_plan is not None:
        files.extend(emit_font_files(_font_plan))
    # Favicon family (16/32/48/64/96/128/180/192/256/512 PNGs +
    # apple-touch-icon + multi-resolution favicon.ico). Skipped silently
    # when PIL is unavailable; layout-public.ejs already references
    # /favicon.svg as the canonical icon source so browsers degrade
    # gracefully. Worker image carries Pillow >= 10 so this always emits
    # in production.
    from compose.utils.brand_assets import compose_favicon_assets
    _is_serif = bool(rs.font_pair.get("display", {}).get("is_serif", False))
    files.extend(compose_favicon_assets(
        rs.site_name, rs.palette_vars, is_serif=_is_serif
    ))
    # Social-link preview card (1200x630). Skipped silently when PIL is
    # unavailable; the layout's og:image then 404s gracefully and crawlers
    # fall back to the favicon. Worker image carries Pillow>=10.0 so this
    # always emits in production.
    og_default = emit_og_default_image(rs)
    if og_default is not None:
        files.append(og_default)
    files.append(emit_env_example(rs))
    files.append(emit_railwayignore(rs))
    files.append(emit_spec_json(rs))

    # Copy curated stock photos referenced by the build into public/images/
    # stock/. This replaces picsum.photos (an external dependency that serves
    # random photos, one of which put corporate logos on a coach's hero and
    # about image) with the self-hosted cli/stock_photos/ library. The worker
    # Docker image carries these files (COPY . .). We resolve refs two ways:
    # (1) the authoritative settings.hero_image_url / about_photo_url, and
    # (2) a scan of every emitted text file for /images/stock/<file> (covers
    # seed.sql + page-data literals), so one robust pass copies them all.
    # A photo of a specific person or place is never copied (the gate refuses
    # a spec that names one, `_personal_stock_photo_errors`).
    from compose.stock_photos import is_personal_photo_name, stock_photo_source_path
    _stock_refs = set()
    _settings = rs.spec.settings or {}
    for _k in ("hero_image_url", "about_photo_url"):
        _v = _settings.get(_k, "")
        if isinstance(_v, str) and _v.startswith("/images/stock/"):
            _stock_refs.add(_v.rsplit("/", 1)[-1].split("?", 1)[0])
    for _p, _c in list(files):
        if isinstance(_c, str) and "/images/stock/" in _c:
            for _m in re.findall(r"/images/stock/([A-Za-z0-9._-]+)", _c):
                _stock_refs.add(_m.split("?", 1)[0])
    for _fn in sorted(_stock_refs):
        if not _fn:
            continue
        if is_personal_photo_name(_fn):
            # The gate refuses a spec that names one; this is for a
            # ResolvedSpec that did not go through it.
            _engine_warning(
                rs.spec, "stock_photo_not_copied",
                f"{_fn}: shows a specific person or place and is never used for a customer's site",
            )
            continue
        _src = stock_photo_source_path(_fn)
        if _src is not None:
            try:
                files.append((f"public/images/stock/{_fn}", _src.read_bytes()))
            except OSError as _stock_exc:
                # The build still ships (the <img> will 404); say so instead
                # of dropping the photo silently.
                _engine_warning(
                    rs.spec, "stock_photo_copy_failed",
                    f"{_fn}: {type(_stock_exc).__name__}",
                )

    # Cache-bust static assets that change between builds. The browser caches
    # /style.css, /js/*.js, /favicon.svg by URL alone, and `localhost:4600`
    # is shared across many local-preview generated apps (a
    # known local-preview convention), so a previous app's cached
    # /style.css gets served when a new app boots even though `Cache-Control:
    # max-age=0` is set. Symptom (observed on a wren-rye-cafe
    # local rebuild): first page load renders broken (mobile + desktop nav
    # both visible because the new build's md: utility classes aren't in the
    # cached old style.css); Cmd+Shift+R fetches fresh and looks correct;
    # next click reverts to broken because the cache hits old bytes.
    #
    # Fix: hash each cache-sensitive asset's content and rewrite every link
    # to it in the emitted views to `/<path>?v=<hash8>`. Same content =>
    # same URL => browser caches; different content => different URL =>
    # browser fetches fresh. Matches the standard webpack/vite contenthash
    # pattern.
    files = _cache_bust_static_assets(files)

    # Coverage audit for the curated Tailwind-like utility CSS subset. When
    # a widget
    # references a utility class the emitted CSS doesn't define, the
    # rendered page silently collapses that property (e.g. `<a class="
    # px-7 py-3">` with no `.px-7` rule loses horizontal padding).
    # Surface each gap as an engine warning so SE reviewers see the
    # drift in compose-manifest.txt + translation-warnings.txt; the
    # audit itself is non-blocking so a single missing utility never
    # bricks a build.
    try:
        css_gaps = audit_emitted_css_utility_coverage(files)
    except Exception as _audit_exc:  # pragma: no cover - defensive
        css_gaps = []
        _engine_warning(rs.spec, "css_coverage_audit_error", str(_audit_exc))
    for gap in css_gaps:
        _engine_warning(
            rs.spec, "css_utility_missing",
            f"{gap['class']} used in {len(gap['used_in'])} file(s) "
            f"but not defined in emitted CSS: {gap['used_in'][:3]}",
        )

    # Responsive lint: never pair
    # a fixed min-height with aspect-ratio on a fluid box.
    for v in audit_aspect_ratio_min_height_conflicts(files):
        _engine_warning(
            rs.spec, "aspect_ratio_min_height_conflict",
            f"{v['path']}: style pairs aspect-ratio with a fixed "
            f"min-height (forces width overflow on narrow "
            f"viewports): {v['style']}",
        )

    return files


def _cache_bust_static_assets(files: List[tuple]) -> List[tuple]:
    """Append `?v=<hash8>` to references of cache-sensitive static assets.

    Hashes each asset's content (sha256, first 8 chars), then rewrites every
    occurrence of the bare path in any .ejs / .js view file to the hashed
    URL. Pass-through for everything else.
    """
    import hashlib
    # Assets we cache-bust. Keep the list small + explicit so we don't
    # accidentally rewrite arbitrary path-like substrings.
    # Images / icons are included because nav_logo_url defaults to
    # /logo.svg (seeded into site_settings), favicon.svg + manifest.json
    # are referenced from layout-public.ejs, and all of these collide
    # with prior local-preview apps on localhost:4600. Symptom (2026-04-25
    # follow-up to the css/js cache-bust): logo on Wren build rendered as
    # broken-image placeholder because browser had a cached /logo.svg
    # from reading-tracker that didn't match the new monogram.
    cache_busted_paths = (
        "/style.css",
        "/js/cas-form-validate.js",
        "/js/cas-reveal.js",
        "/js/cas-fmt.js",
        "/js/upload-zone.js",
        "/logo.svg",
        "/favicon.svg",
        "/manifest.json",
    )
    # 1) Compute hash for each cache-busted asset.
    hashes = {}
    for path, contents in files:
        public_path = "/" + path[len("public/"):] if path.startswith("public/") else None
        if public_path in cache_busted_paths:
            data = contents.encode("utf-8") if isinstance(contents, str) else contents
            hashes[public_path] = hashlib.sha256(data).hexdigest()[:8]
    # 2) Rewrite references in view + script files.
    if not hashes:
        return files
    rewritten = []
    # .sql is included so seed.sql's INSERT INTO site_settings (..., '/logo.svg')
    # gets cache-busted at the data layer too. The runtime template
    # `<%= settings.nav_logo_url %>` reads the seeded value verbatim.
    rewrite_extensions = (".ejs", ".js", ".html", ".sql")
    for path, contents in files:
        if not isinstance(contents, str):
            rewritten.append((path, contents))
            continue
        if not path.endswith(rewrite_extensions):
            rewritten.append((path, contents))
            continue
        # Don't rewrite the assets themselves (they reference each other),
        # and don't rewrite operator scripts: rehash-assets.js carries the
        # BARE asset paths as data (it appends the hashes itself at run
        # time), and a cache-busted path literal would break its lookups.
        if path.startswith("public/") or path.startswith("scripts/"):
            rewritten.append((path, contents))
            continue
        new_contents = contents
        for asset_path, h in hashes.items():
            # Match the bare path in href= / src= attributes. We avoid
            # rewriting paths that already carry a query string so this
            # function is idempotent.
            for quote in ('"', "'"):
                bare = f"{quote}{asset_path}{quote}"
                hashed = f"{quote}{asset_path}?v={h}{quote}"
                new_contents = new_contents.replace(bare, hashed)
        rewritten.append((path, new_contents))
    return rewritten


def _safe_output_path(output_dir: Path, rel_path: Any) -> Path:
    """Resolve an emitted file's relative path to a location inside
    `output_dir`, or raise ValueError.

    Emitters build file names from spec-derived values (a model slug in
    routes/<slug>.js, a page id in views/<page>.ejs). Those values are
    validated upstream, but a write primitive that trusts its callers is
    one missed check away from an arbitrary file write on the build
    worker, so containment is enforced here, where the write happens.

    Rejected: non-strings, empty paths, NUL bytes, backslashes (a path
    separator on Windows, never legitimate in an emitted name), absolute
    paths, drive-qualified paths, any `.` or `..` component, and, as the
    last word, anything whose resolved location is not under the resolved
    output directory (which also covers a symlink planted inside it).
    """
    if not isinstance(rel_path, str) or not rel_path:
        raise ValueError(f"refusing to write: output path is not a non-empty string: {rel_path!r}")
    if "\x00" in rel_path or "\\" in rel_path:
        raise ValueError(f"refusing to write: illegal character in output path {rel_path!r}")
    pure = PurePosixPath(rel_path)
    if pure.is_absolute() or rel_path.startswith("/") or (len(rel_path) > 1 and rel_path[1] == ":"):
        raise ValueError(f"refusing to write: absolute output path {rel_path!r}")
    # Split on "/" by hand: PurePosixPath silently drops "." components
    # and would hide them from this check.
    parts = rel_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"refusing to write: output path {rel_path!r} is not a plain relative path")
    root = Path(output_dir).resolve()
    full = (root / pure).resolve()
    if root not in full.parents:
        raise ValueError(f"refusing to write: output path {rel_path!r} escapes the build directory")
    return full


# The two files build_app writes itself after the emitted ones (their names
# are constants and their bodies are assembled there). An emitter may not
# produce a file under either name: it would be silently replaced.
_BUILD_REPORT_FILES = ("compose-manifest.txt", "translation-warnings.txt")


def _write_build_files(output_dir: Path, files: List[tuple]) -> None:
    """Write every emitted `(relative path, content)` pair under output_dir.

    The ONE place emitted files touch the disk. Before anything is written,
    every path is confined to output_dir by `_safe_output_path`, and the
    whole list is checked for collisions: two entries with the same path
    (compared without regard to letter case, since the build may run on a
    case-insensitive filesystem), or an entry named like one of the build's
    own report files. Any of those raises ValueError and nothing is
    written.

    The collision check is what makes "a spec value chose a file name the
    engine already uses" a failed build instead of a silently replaced
    file. Reserved names are refused earlier, by the gate, with a better
    message; this is the backstop for whatever the gate did not anticipate.
    """
    targets = []
    seen: Dict[str, str] = {}
    for rel_path, content in files:
        full = _safe_output_path(output_dir, rel_path)
        key = rel_path.casefold()
        if key in seen:
            raise ValueError(
                "refusing to write: two emitted files have the output path "
                f"{rel_path!r}" + ("" if seen[key] == rel_path else f" (also emitted as {seen[key]!r})")
            )
        if key in _BUILD_REPORT_FILES:
            raise ValueError(
                f"refusing to write: output path {rel_path!r} is reserved for the build report"
            )
        seen[key] = rel_path
        targets.append((full, content))
    for full, content in targets:
        full.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            full.write_bytes(content)
        else:
            # Explicit encoding + newline: without them the bytes on disk
            # depend on the build machine's locale and platform.
            with open(full, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)


# Builds live in a directory of this name: the default output is
# builds/<app_name> below the working directory, and the worker builds into
# builds/<app_name>-<build id> below the repository that holds this engine.
_BUILDS_DIR_NAME = "builds"


def _builds_roots() -> List[Path]:
    """The directories whose direct children build_app may replace without
    further proof that they are builds: `builds/` under the working
    directory and `builds/` beside the engine."""
    return [
        (Path.cwd() / _BUILDS_DIR_NAME).resolve(),
        (COMPOSE_DIR.parent / _BUILDS_DIR_NAME).resolve(),
    ]


def _remove_previous_build(output_dir: Path) -> None:
    """Delete what an earlier build left at `output_dir`, or refuse.

    build_app writes a complete app and starts from an empty directory, so
    an existing `output_dir` is removed first. This is the only deletion
    the engine performs, and it removes exactly that one directory: never
    its parent, never a sibling. The caller chooses the path, so the
    directory is deleted only when one of these holds:

      - it is empty;
      - it contains `compose-manifest.txt`, the report build_app writes as
        the last step of every build (it is a finished build);
      - it is a direct child of a builds root (`_builds_roots`), where a
        build interrupted before its manifest was written must still be
        replaceable (the worker retries into the same directory).

    Everything else raises ValueError and nothing is deleted: a non-empty
    directory that is none of the above, a symlink (its target is somebody
    else's directory), a path that is not a directory, and a directory that
    contains the working directory or the engine's own source.
    """
    if not output_dir.exists() and not output_dir.is_symlink():
        return
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError(
            f"refusing to build into {str(output_dir)!r}: it exists and is not a plain directory"
        )
    target = output_dir.resolve()
    for precious in (Path.cwd().resolve(), COMPOSE_DIR):
        if target == precious or target in precious.parents:
            raise ValueError(
                f"refusing to delete {str(target)!r}: it contains {str(precious)!r}"
            )
    replaceable = (
        not any(target.iterdir())
        or (target / "compose-manifest.txt").is_file()
        or target.parent in _builds_roots()
    )
    if not replaceable:
        raise ValueError(
            f"refusing to delete {str(target)!r}: it is not empty, it has no "
            "compose-manifest.txt (so this engine did not finish a build there) and it is "
            f"not directly under a {_BUILDS_DIR_NAME}/ directory of the working directory or "
            "of the engine's repository. Remove it yourself or choose another output_dir."
        )
    shutil.rmtree(target)


def build_app(spec_path_or_dict, output_dir: Optional[Path] = None, tier: Optional[str] = None) -> Path:
    """Main entry: takes a spec path or dict, emits the full generated app.

    Output goes to `output_dir`, by default `builds/<app_name>` under the
    working directory. If that directory already exists it is DELETED first
    (that directory only), and only when `_remove_previous_build` recognizes
    it as an earlier build; otherwise the build is refused with ValueError
    before anything is written.

    `tier` changes nothing in the emitted app. The worker passes the
    build's `builds.tier` value through (the same call shape as v1's
    `cli.build.build_app`) and it is written to the `tier:` line of
    compose-manifest.txt, falling back to the spec's own `tier` field and
    then to "build".

    Two report files are written beside the app after it: the manifest
    (archetype, palette, font pair, file count, a sha256 over the emitted
    files, the warning counts, what happened to the external-URL lint, and
    one line per build warning: the engine's own first, then a bounded copy
    of the spec's) and, when there are warnings, translation-warnings.txt.
    The external-URL lint runs here and not in `compose_files`; see the
    module docstring for what that means for determinism.

    A spec given as a dict or a file is bounded as a whole (nesting, number
    of values, length of a string, all text) before anything reads it, keys
    the engine does not know included: ValueError "Spec validation failed:
    ...".
    """
    if isinstance(spec_path_or_dict, (str, Path)):
        if os.path.getsize(spec_path_or_dict) > _MAX_SPEC_FILE_BYTES:
            raise ValueError(
                "Spec validation failed: the spec file is larger than "
                f"{_MAX_SPEC_FILE_BYTES} bytes"
            )
        with open(spec_path_or_dict, encoding="utf-8") as f:
            try:
                d = json.load(f)
            except RecursionError:
                # The JSON reader recurses once per level of nesting and
                # runs out of stack long before the gate's own (iterative)
                # depth check could see the value.
                raise ValueError(
                    "Spec validation failed: the spec file is nested too deeply to read"
                ) from None
    else:
        d = spec_path_or_dict

    # The whole document is bounded before anything reads it, not only the
    # containers BuildSpec keeps: the lint below walks every key.
    if isinstance(d, dict):
        too_big = _document_size_errors(d)
        if too_big:
            raise ValueError("Spec validation failed: " + too_big[0])

    spec = BuildSpec.from_dict(d)
    errors = validate_spec(spec)
    if errors:
        raise ValueError("Spec validation failed: " + "; ".join(errors))
    rs = resolve_spec(spec)

    # HEAD-ping every external URL referenced in the spec and record a
    # build warning for any 404 / dead link. Default mode is "warn": the
    # build still ships and the warning is in translation-warnings.txt.
    # CAS_LINT_EXTERNAL_URLS=skip turns the lint off (tests, machines with
    # no network egress); =error refuses the build on any dead link. This
    # is the one step of a build that depends on the network, and it only
    # reaches the two report files (module docstring, exception 4). The
    # lint walks `d` (the raw spec dict, not the resolved-spec dataclass)
    # so it also covers _harbor_meta.*, seed_content[*], and any
    # keeper-supplied custom URL.
    #
    # The report says which of three things happened, in its
    # `external_url_lint:` header: "skipped" (the mode asked for no lint),
    # "ran" (every URL it was allowed to check was checked, or counted in
    # an `external_url_lint_truncated` warning) or "failed" (the lint
    # itself raised, so no URL was judged; the warning below says why). A
    # report with no link warning and "ran" is a lint that found nothing.
    lint_state = "skipped" if lint_mode() == "skip" else "ran"
    try:
        link_warnings = lint_external_urls(d)
    except ExternalUrlLintError:
        # CAS_LINT_EXTERNAL_URLS=error mode: caller asked us to refuse
        # the build on any dead link. Re-raise so build_app surfaces it.
        raise
    except Exception as e:
        # Fail-open: if the linter itself blows up (e.g. egress blocked),
        # record that it could not run and keep going. The build is more
        # important than the lint result.
        lint_state = "failed"
        link_warnings = [{
            "reason": "external_url_lint_error",
            "detail": f"linter raised {type(e).__name__}: {e}"[:200],
        }]
    for _link_warning in link_warnings:
        _engine_warning(rs.spec, _link_warning.get("reason"), _link_warning.get("detail"))

    if output_dir is None:
        output_dir = Path(_BUILDS_DIR_NAME) / spec.app_name
    output_dir = Path(output_dir)

    # Start from an empty directory: remove what an earlier build left at
    # this exact path (and only there), or refuse.
    _remove_previous_build(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    files = compose_files(rs)
    _write_build_files(output_dir, files)

    # Write the build manifest. The tier is recorded there and nowhere
    # else; the spec's `tier` field is the fallback when none was passed.
    _effective_tier = tier or d.get("tier") or "build"
    manifest_hash = hashlib.sha256()
    # Sort on the path only: sorting the (path, content) tuples would
    # compare a str body with a bytes body if two entries shared a path.
    for rel_path, content in sorted(files, key=lambda entry: entry[0]):
        manifest_hash.update(rel_path.encode("utf-8"))
        manifest_hash.update((content if isinstance(content, bytes) else content.encode("utf-8")))
    # The build report. Both files carry the same warning lines in the same
    # order: every engine warning first (`warning: <reason>: <detail>`),
    # then the bounded copy of what the spec carried under
    # `_translation_warnings` (`spec-warning: ...`). See the "Build
    # warnings" section above for why the two never share a prefix. The
    # worker's lint of a v2 build reads the counts and the warning lines of
    # the manifest into the build log.
    engine_lines = _engine_warning_lines(rs.spec)
    spec_lines, spec_omitted = _spec_warning_lines(rs.spec)
    warnings_lines = "".join(line + "\n" for line in engine_lines + spec_lines)
    # translation-warnings.txt is written only when there is at least one
    # warning line.
    if warnings_lines:
        (output_dir / "translation-warnings.txt").write_text(
            "# Build warnings (one per line).\n"
            "# Lines that begin with \"warning: \" come first and were written by\n"
            "# the compose engine itself (dropped pages, what the build shows to\n"
            "# visitors, seed rows, CSS coverage, the external-URL lint).\n"
            "# Lines that begin with \"spec-warning: \" are text the spec carried\n"
            "# (the notes of the translator that produced it), at most\n"
            f"# {_MAX_SPEC_WARNING_LINES} of them. Each line documents a lossy or suspicious\n"
            "# decision; review these before sending the build to the customer.\n\n"
            + warnings_lines,
            encoding="utf-8",
        )
    # The palette's primary hex is written beside its id so the manifest
    # is self-explanatory (the warm-earth primary is a burgundy, for
    # instance).
    _palette_primary = rs.palette.get("vars", {}).get("--color-primary", "")
    _palette_line = (
        f"palette: {rs.palette['id']} (primary={_palette_primary})\n"
        if _palette_primary else f"palette: {rs.palette['id']}\n"
    )
    # Manifest shape (queue/worker_pg.py reads it; keep the two in step).
    # Header lines, each exactly once and in this order:
    #   archetype, palette, font_pair, tier, files, sha256,
    #   translation_warnings_count  (warning lines below: engine + spec)
    #   engine_warnings_count       (the `warning: ` lines)
    #   spec_warnings_count         (the `spec-warning: ` lines)
    #   spec_warnings_omitted       (spec entries left out by the cap)
    #   external_url_lint           (skipped, ran or failed)
    # then the engine lines, then the spec lines. Nothing follows them.
    (output_dir / "compose-manifest.txt").write_text(
        f"archetype: {rs.archetype['id']}\n"
        + _palette_line
        + f"font_pair: {rs.font_pair['id']}\n"
        f"tier: {_report_text(_effective_tier, _MAX_TIER_TEXT)}\n"
        f"files: {len(files)}\n"
        f"sha256: {manifest_hash.hexdigest()}\n"
        f"translation_warnings_count: {len(engine_lines) + len(spec_lines)}\n"
        f"engine_warnings_count: {len(engine_lines)}\n"
        f"spec_warnings_count: {len(spec_lines)}\n"
        f"spec_warnings_omitted: {spec_omitted}\n"
        f"external_url_lint: {lint_state}\n"
        + warnings_lines,
        encoding="utf-8",
    )
    return output_dir


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m compose.build <spec.json>")
        sys.exit(1)
    out = build_app(sys.argv[1])
    print(f"Composed to {out}")


if __name__ == "__main__":
    main()
