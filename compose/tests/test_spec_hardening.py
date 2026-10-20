"""Hostile-spec and generated-app hardening tests for the v2 compose engine.

A spec is written by a model from a customer conversation, so every string
in it is treated as attacker-controlled. Two surfaces are covered:

  A. The generator on the build worker: the spec safety gate (identifiers,
     column types, route paths, page ids), the per-context escapers (SQL
     literal, SQL comment, JS string, JSON inside an EJS tag), the central
     file writer, the external-URL linter (server-side request forgery),
     and build determinism.
  B. The generated app: session secret handling, session fixation, rate
     limits, CSRF coverage, upload filtering, unpublished-row visibility,
     output escaping in the widgets, and the absence of any default
     credential.

Where node and web/node_modules/ejs are available the emitted JavaScript is
syntax-checked with `node --check` and the emitted views are compiled and
rendered with the real EJS, so these are not only string assertions. No
network, no database.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from compose import build as B
from compose import lint_external_urls as L
from compose.build import (
    BuildSpec,
    build_app,
    compose_files,
    resolve_spec,
    validate_spec,
)
from compose.intake import (
    _build_copy_hero_block,
    _copy_path_to_page_id,
    translate_v1_spec,
)
from compose.markdown_safe import render_markdown_blocks, sanitize_html_fragment
from compose.stock_photos import stock_photo_source_path

COMPOSE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = COMPOSE_DIR.parent
FIXTURES = COMPOSE_DIR / "fixtures"
EJS_DIR = REPO_ROOT / "web" / "node_modules" / "ejs"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")
needs_ejs = pytest.mark.skipif(
    NODE is None or not EJS_DIR.exists(),
    reason="needs node and web/node_modules/ejs (run `cd web && npm ci`)",
)

LINE_SEP = chr(0x2028)
PARA_SEP = chr(0x2029)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "skip")


def _base() -> dict:
    return json.loads((FIXTURES / "sample-coach-spec.json").read_text(encoding="utf-8"))


def _files(spec: dict) -> dict:
    return dict(compose_files(resolve_spec(BuildSpec.from_dict(spec))))


def _model(**overrides) -> dict:
    model = {"name": "Thing", "fields": [{"name": "title", "sql_type": "TEXT"}]}
    model.update(overrides)
    return model


# ---------------------------------------------------------------------------
# A1. The spec safety gate
# ---------------------------------------------------------------------------

def _set(key, value):
    def apply(spec):
        spec[key] = value
    return apply


def _models(*models):
    return _set("data_models", list(models))


def _page(page_id):
    def apply(spec):
        spec["pages"][page_id] = {"_copy_blocks": ["hello"], "hero": {"heading": "x"}}
    return apply


def _setting(key, value):
    def apply(spec):
        spec["settings"][key] = value
    return apply


HOSTILE_SPECS = [
    ("app_name traversal", _set("app_name", "../../etc")),
    ("app_name absolute", _set("app_name", "/tmp/x")),
    ("app_name trailing newline", _set("app_name", "coach\n")),
    ("app_name not a string", _set("app_name", ["coach"])),
    # The platform's own Railway projects are named with this prefix, and
    # destroy_project refuses to delete any project that is.
    ("app_name reserved prefix", _set("app_name", "craftandship-x")),
    ("app_name is the reserved prefix", _set("app_name", "craftandship")),
    ("model name with SQL", _models(_model(name="Thing; DROP TABLE users"))),
    ("model name trailing newline", _models(_model(name="Thing\n"))),
    ("model name not a string", _models(_model(name={"a": 1}))),
    ("plural with JS", _models(_model(plural="x'); require('child_process'); ('"))),
    ("plural with path", _models(_model(plural="../../x"))),
    ("field name with SQL", _models(_model(fields=[{"name": "title TEXT); DROP TABLE users; --"}]))),
    ("field name trailing newline", _models(_model(fields=[{"name": "title\n", "sql_type": "TEXT"}]))),
    ("sql_type with statement", _models(_model(fields=[{"name": "title", "sql_type": "TEXT); DROP TABLE users; --"}]))),
    ("sql_type with function call", _models(_model(fields=[{"name": "title", "sql_type": "TEXT DEFAULT pg_sleep(10)"}]))),
    ("sql_type with comment", _models(_model(fields=[{"name": "title", "sql_type": "TEXT /* x */"}]))),
    ("sql_type not a string", _models(_model(fields=[{"name": "title", "sql_type": ["TEXT"]}]))),
    ("public_listing_path with JS", _models(_model(public_listing_path="/t'); require('child_process'); app.use('/z"))),
    ("public_listing_path under /admin", _models(_model(public_listing_path="/admin/things"))),
    ("public_listing_path /Admin (Express is case-insensitive)", _models(_model(public_listing_path="/Admin/things"))),
    ("public_listing_path is /admin itself", _models(_model(public_listing_path="/admin"))),
    ("public_listing_path is the site root", _models(_model(public_listing_path="/"))),
    ("public_listing_path shadows /login", _models(_model(public_listing_path="/login"))),
    ("public_listing_path shadows /contact", _models(_model(public_listing_path="/Contact"))),
    ("public_listing_path with dot segments", _models(_model(public_listing_path="/../x"))),
    ("public_listing_path with Express pattern", _models(_model(public_listing_path="/things/:id(.*)"))),
    ("public_listing_path with whitespace", _models(_model(public_listing_path="/a b"))),
    ("belongs_to_field with SQL", _models(
        _model(name="Parent"),
        _model(name="Kid", belongs_to="Parent", belongs_to_field="p_id INTEGER); DROP TABLE users; --"),
    )),
    ("model maps to users table", _models(_model(name="User"))),
    ("model maps to user_sessions table", _models(_model(name="UserSession"))),
    ("model maps to site_settings table", _models(_model(name="SiteSetting"))),
    ("model maps to magic_link_tokens table", _models(_model(name="MagicLinkToken"))),
    ("model maps to slug_aliases table", _models(_model(name="Alias", plural="slug-aliases"))),
    ("model overwrites routes/pages.js", _models(_model(name="Page"))),
    ("model overwrites routes/auth.js", _models(_model(name="Credential", plural="auth"))),
    ("model mounted over /admin", _models(_model(name="Administrator", plural="admin"))),
    ("model shadows /admin/settings", _models(_model(name="Setting"))),
    ("two models, one table", _models(_model(name="Gadget", plural="things"), _model(name="Thing"))),
    ("page id traversal", _page("../../escaped")),
    ("page id with quote", _page("a'b")),
    ("page id with dot", _page("about.html")),
    ("page id with uppercase", _page("About")),
    ("admin_email with newline", _set("admin_email", "a@b.example\nSESSION_SECRET=known")),
    ("admin_email with quote", _set("admin_email", "a'b@c.example")),
    ("admin_email with space", _set("admin_email", "a b@c.example")),
    ("settings is not an object", _set("settings", "abc")),
    ("pages is not an object", _set("pages", ["home"])),
    ("data_models is not a list", _set("data_models", {"name": "Thing"})),
    ("site_name is not a string", _setting("site_name", ["x"])),
    ("hero_tagline is not a string", _setting("hero_tagline", {"a": 1})),
    ("custom page title is not a string", _set("custom_pages", [{"path": "/x", "title": ["a"]}])),
    ("custom page path is not a string", _set("custom_pages", [{"path": ["/x"], "title": "a"}])),
    # A page id that names a built-in view or route. Before the reservation,
    # "login" built cleanly and views/login.ejs was the copied page.
    ("page id login (replaces the login form)", _page("login")),
    ("page id layout-public (replaces the site layout)", _page("layout-public")),
    ("page id error-404", _page("error-404")),
    ("page id privacy", _page("privacy")),
    ("page id set-password", _page("set-password")),
    ("page id admin (a built-in route)", _page("admin")),
    ("page id blog-detail (the blog page's detail view)", _page("blog-detail")),
    ("reserved page id without copy blocks", lambda spec: spec["pages"].update({"login": {"title": "x"}})),
    # A custom page "path" that names another origin. These used to be
    # dropped from the routes and kept in the navigation.
    ("custom page path protocol-relative", _set("custom_pages", [{"path": "//evil.example/signin", "title": "Sign in"}])),
    ("custom page path backslash protocol-relative", _set("custom_pages", [{"path": "/\\evil.example", "title": "Account"}])),
    ("custom page path tab between the slashes", _set("custom_pages", [{"path": "/\t/evil.example", "title": "x"}])),
    ("custom page path space then protocol-relative", _set("custom_pages", [{"path": " //evil.example", "title": "x"}])),
    ("custom page path absolute URL", _set("custom_pages", [{"path": "https://evil.example/x", "title": "x"}])),
    ("custom page path javascript URL", _set("custom_pages", [{"path": "javascript:alert(1)", "title": "x"}])),
]


@pytest.mark.parametrize("label,mutate", HOSTILE_SPECS, ids=[c[0] for c in HOSTILE_SPECS])
def test_gate_refuses_hostile_spec(label, mutate, tmp_path):
    """Each hostile shape is refused three times over: validate_spec reports
    it, resolve_spec (the only way to obtain what the emitters accept)
    raises, and build_app writes nothing."""
    spec = _base()
    mutate(spec)
    bs = BuildSpec.from_dict(copy.deepcopy(spec))
    assert validate_spec(bs), f"validate_spec accepted: {label}"
    with pytest.raises(ValueError, match="Spec validation failed"):
        resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))
    out = tmp_path / "build" / "app"
    with pytest.raises(ValueError, match="Spec validation failed"):
        build_app(copy.deepcopy(spec), output_dir=out)
    assert not out.exists(), "build_app created the output directory for a refused spec"
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_gate_accepts_every_shipped_fixture():
    """The gate must not reject legitimate specs: every v2 sample fixture
    validates clean and resolves."""
    fixtures = sorted(FIXTURES.glob("sample-*-spec.json"))
    assert len(fixtures) >= 8
    for fx in fixtures:
        spec = json.loads(fx.read_text(encoding="utf-8"))
        assert validate_spec(BuildSpec.from_dict(spec)) == [], fx.name
        resolve_spec(BuildSpec.from_dict(spec))


def test_gate_accepts_ordinary_customer_models():
    spec = _base()
    spec["data_models"] = [
        _model(name="Widget", plural="widgets", public_listing_path="/our-widgets/all",
               fields=[
                   {"name": "title", "sql_type": "VARCHAR(255)", "required": True},
                   {"name": "price", "sql_type": "numeric(10, 2)"},
                   {"name": "tags", "sql_type": "TEXT[]"},
                   {"name": "meta", "sql_type": "JSONB", "default": "'{}'::jsonb"},
                   {"name": "status", "sql_type": "VARCHAR(20)", "default": "pending"},
                   {"name": "starts", "sql_type": "double precision"},
               ]),
        _model(name="Part", belongs_to="Widget", belongs_to_field="widget_ref", on_delete="cascade"),
    ]
    assert validate_spec(BuildSpec.from_dict(spec)) == []
    files = _files(spec)
    schema = files["db/schema.sql"]
    assert "meta JSONB DEFAULT '{}'::jsonb" in schema
    assert "status VARCHAR(20) DEFAULT 'pending'" in schema
    assert "widget_ref INTEGER REFERENCES widgets(id) ON DELETE CASCADE" in schema
    assert "app.use('/our-widgets/all', require('./routes/widgets'));" in files["server.js"]


def test_customer_model_cannot_take_over_an_archetype_table():
    """Shadowing an archetype model by name is supported; reusing its table
    under a different name (explicit plural) is refused at resolve time,
    after the archetype defaults have been merged in."""
    spec = _base()
    archetype_tables = {B._table_name_for_model(m) for m in resolve_spec(BuildSpec.from_dict(_base())).data_models}
    assert "blog_posts" in archetype_tables
    spec["data_models"] = [_model(name="Memo", plural="blog-posts")]
    with pytest.raises(ValueError, match="map to the same table 'blog_posts'"):
        resolve_spec(BuildSpec.from_dict(spec))
    # Same name (any case) is a shadow, not a collision.
    spec["data_models"] = [_model(name="blogpost", plural="blog-posts")]
    resolved = resolve_spec(BuildSpec.from_dict(spec))
    assert [m["name"] for m in resolved.data_models if B._table_name_for_model(m) == "blog_posts"] == ["blogpost"]


def _spec_with_trailing_newline(position: str):
    """`(dirty, clean)`: the coach sample with one identifier written with a
    trailing newline at the named position, and the same spec with the value
    as it should be. A trailing newline is the character a comment or string
    breakout needs, and the one `re.match(r"^x$")` accepts after "x"."""
    clean = _base()
    clean["data_models"] = [{"name": "Thing", "plural": "things", "public_listing_path": "/things",
                             "fields": [{"name": "title", "sql_type": "TEXT"}]}]
    clean["pages"]["notes"] = {}
    dirty = copy.deepcopy(clean)
    model, field = dirty["data_models"][0], dirty["data_models"][0]["fields"][0]
    if position == "app_name":
        dirty["app_name"] = clean["app_name"] + "\n"
    elif position == "admin_email":
        dirty["admin_email"] = clean["admin_email"] + "\n"
    elif position == "model name":
        model["name"] = "Thing\n"
    elif position == "model plural":
        model["plural"] = "things\n"
    elif position == "model slug":
        del model["plural"]
        del clean["data_models"][0]["plural"]
        model["slug"] = "things\n"
        clean["data_models"][0]["slug"] = "things"
    elif position == "field name":
        field["name"] = "title\n"
    elif position == "column type":
        # Whitespace around a type is trimmed before it is matched (and the
        # emitted type is the trimmed one), so a newline at the END is not a
        # breakout; one that is followed by anything is.
        field["sql_type"] = "text\n); DROP TABLE users; --"
    elif position == "page id":
        del dirty["pages"]["notes"]
        dirty["pages"]["notes\n"] = {}
    elif position == "listing path":
        model["public_listing_path"] = "/things\n"
    else:
        raise AssertionError(position)
    return dirty, clean


@pytest.mark.parametrize("position, fragment", [
    ("app_name", "app_name must be lowercase slug"),
    ("admin_email", "admin_email is not a plain email address"),
    ("model name", "data_model name is not a safe identifier"),
    ("model plural", "data_model plural is not a safe slug"),
    ("model slug", "data_model slug is not a safe slug"),
    ("field name", "data_model field name is not a safe identifier"),
    ("column type", "sql_type is not a plain column type"),
    ("page id", "page id is not a safe slug"),
    ("listing path", "public_listing_path '/things\\n' is not a plain absolute path"),
])
def test_the_gate_refuses_an_identifier_with_a_trailing_newline(position, fragment):
    """Every pattern the gate applies to text that becomes code has to
    match the WHOLE value. Driven through the gate, not through the
    pattern: the test fails for a gate that calls `.match` or `.search`,
    or matches a prefix, whichever pattern it is handed."""
    dirty, clean = _spec_with_trailing_newline(position)
    errors = validate_spec(BuildSpec.from_dict(dirty))
    assert any(fragment in error for error in errors), (position, errors)
    with pytest.raises(ValueError, match="Spec validation failed"):
        resolve_spec(BuildSpec.from_dict(dirty))
    # The same spec with the value as it should be is accepted (the control).
    assert validate_spec(BuildSpec.from_dict(clean)) == [], position


def test_emitters_recheck_types_and_paths_without_the_gate():
    """Defense in depth: the DDL and route emitters refuse an unsafe column
    type or path even when handed a ResolvedSpec that never went through
    the gate (constructed by hand here)."""
    rs = resolve_spec(BuildSpec.from_dict(_base()))
    rs.data_models = list(rs.data_models) + [
        {"name": "Thing", "fields": [{"name": "title", "sql_type": "TEXT); DROP TABLE users; --"}]}
    ]
    with pytest.raises(ValueError, match="plain column type"):
        B.emit_schema_sql(rs)

    rs = resolve_spec(BuildSpec.from_dict(_base()))
    rs.data_models = list(rs.data_models) + [
        {"name": "Thing", "public_listing_path": "/t'); process.exit(); ('",
         "fields": [{"name": "title", "sql_type": "TEXT"}]}
    ]
    with pytest.raises(ValueError, match="plain route path"):
        B.emit_server_js(rs)


# ---------------------------------------------------------------------------
# A1b. Reserved names, route guards and navigation: what is EMITTED
#
# The gate tests above only look at the error list. These read the emitted
# routes, views and navigation, because that is where each of these holes
# was: the gate reported nothing and the output was wrong.
# ---------------------------------------------------------------------------

_ROUTE_LINE_RE = re.compile(
    r"^(?:router|app)\.(get|post|put|patch|delete|head|all)\('([^']+)'(.*)$", re.M
)
_HANDLER_START_RE = re.compile(r",\s*(?:async\s*)?\(req, res(?:, next)?\)\s*=>")
_GUARD_RE = re.compile(
    r"requireAuth|requireAdmin|requireRole\('(?:[^'\\]|\\.)*'\)"
    r"|requireAnyRole\(\[(?:'(?:[^'\\]|\\.)*'(?:, )?)*\]\)"
)


def _routes(source: str) -> list:
    """(verb, path, middleware source text) for every route an emitted
    router file registers."""
    found = []
    for verb, path, rest in _ROUTE_LINE_RE.findall(source):
        handler = _HANDLER_START_RE.search(rest)
        found.append((verb, path, rest[:handler.start()] if handler else rest))
    return found


def _js_const(source: str, name: str):
    """The JSON value of `const <name> = <json>;` in emitted JavaScript."""
    return json.loads(source.split(f"const {name} = ", 1)[1].split(";\n", 1)[0])


def _protected_form_spec() -> dict:
    spec = _base()
    spec["custom_pages"] = [{
        "path": "/members-form", "title": "Members only", "auth_required": True,
        "allowed_roles": ["admin"], "form_target": "Testimonial",
    }]
    return spec


def _dropped_custom_pages_spec() -> dict:
    """Custom pages the engine declines to emit without refusing the spec,
    next to three it keeps."""
    spec = _base()
    spec["custom_pages"] = [
        {"path": "/book", "title": "Book a call"},                       # kept
        {"path": "/a b", "title": "Space"},                              # not a plain path
        {"path": "/dot.html", "title": "Dot"},
        {"path": "/search?q=1", "title": "Query"},
        {"path": "/x', (q,r)=>r.end()); router.get('/y", "title": "Injected"},
        {"path": "/events/:slug", "title": "Parametric"},
        {"path": "/Login", "title": "Sign in here"},                     # built-in route, other case
        {"path": "/login/", "title": "Sign in"},                         # built-in route, trailing slash
        {"path": "/health", "title": "Status"},
        {"path": "/uploads", "title": "Files"},
        {"path": "/admin", "title": "Admin"},
        {"path": "/admin/settings", "title": "Fake settings"},
        {"path": "/admin/blog-posts", "title": "Fake posts"},            # /admin/<model slug>
        {"path": "/ADMIN/billing", "title": "Billing"},                  # public page under /admin
        {"path": "/admin/reports", "title": "Reports",
         "auth_required": True, "allowed_roles": ["admin"]},             # kept, behind the login
        {"path": "/team/leads", "title": "Leads"},                       # kept
        {"path": "/team-leads", "title": "Leads again"},                 # same view file as /team/leads
        {"path": "/book", "title": "Book again"},                        # same path
        {"path": "/Book/", "title": "Book, other spelling"},             # same route to Express
    ]
    return spec


def _node_check_all(build_dir: Path) -> None:
    scripts = sorted(p for p in build_dir.rglob("*.js") if "node_modules" not in p.parts)
    assert len(scripts) > 20
    for script in scripts:
        proc = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{script.relative_to(build_dir)}: {proc.stderr[:400]}"


def test_every_route_of_a_protected_form_page_carries_its_guard():
    """An auth_required form page used to get a guarded GET and an open
    POST: anyone with a CSRF token from /login could insert rows."""
    pages = _files(_protected_form_spec())["routes/pages.js"]
    routes = {verb: mw for verb, path, mw in _routes(pages) if path == "/members-form"}
    assert sorted(routes) == ["get", "post"]
    assert routes["get"] == ", requireAuth, requireRole('admin')"
    assert routes["post"] == ", formLimiter, requireAuth, requireRole('admin'), validateCsrfToken"


@pytest.mark.parametrize("make_spec", [
    lambda: _base(),
    lambda: _protected_form_spec(),
    lambda: _dropped_custom_pages_spec(),
    lambda: _hostile_text_spec(),
], ids=["sample", "protected-form", "dropped-pages", "hostile-text"])
def test_no_path_has_one_verb_guarded_and_another_open(make_spec):
    """For every path in every emitted router, every verb carries the same
    guard middleware (possibly none). This is the general form of the
    protected-form hole, over all routers and several specs."""
    files = _files(make_spec())
    checked = 0
    for name, source in files.items():
        if not (name.startswith("routes/") and isinstance(source, str)):
            continue
        guards_by_path = {}
        for verb, path, middleware in _routes(source):
            guards_by_path.setdefault(path, {})[verb] = tuple(_GUARD_RE.findall(middleware))
        for path, by_verb in guards_by_path.items():
            checked += 1
            assert len(set(by_verb.values())) == 1, f"{name} {path}: {by_verb}"
    assert checked > 30


def test_dropped_custom_pages_leave_no_route_view_or_link_behind():
    """Navigation is built from the pages that survived, not from the raw
    custom_pages list: every nav link points at a route the build serves."""
    spec = _dropped_custom_pages_spec()
    rs = resolve_spec(BuildSpec.from_dict(spec))
    files = dict(compose_files(rs))

    custom_views = sorted(p for p in files if p.startswith("views/custom/"))
    assert custom_views == ["views/custom/admin-reports.ejs", "views/custom/book.ejs",
                            "views/custom/team-leads.ejs"]

    page_routes = _routes(files["routes/pages.js"])
    archetype_paths = {"/" if p == "home" else f"/{p}" for p in rs.page_layouts}
    custom_gets = sorted(path for verb, path, _ in page_routes
                         if verb == "get" and path.split("/:")[0] not in archetype_paths)
    assert custom_gets == ["/admin/reports", "/book", "/team/leads"]
    guards = {path: mw for verb, path, mw in page_routes if verb == "get"}
    assert guards["/admin/reports"] == ", requireAuth, requireRole('admin')"
    assert guards["/book"] == ""

    served = {path for name in ("routes/pages.js", "routes/legal.js")
              for verb, path, _ in _routes(files[name]) if verb == "get"}
    served |= set(re.findall(r"^app\.use\('([^']+)', require\(", files["server.js"], re.M))
    nav = _js_const(files["middleware/settings.js"], "_NAV_ITEMS")
    hrefs = [item["href"] for item in nav]
    assert "/book" in hrefs and "/team/leads" in hrefs
    assert set(hrefs) <= served, sorted(set(hrefs) - served)
    assert len(hrefs) == len(set(hrefs))
    labels = {item["label"] for item in nav}
    assert labels.isdisjoint({"Sign in here", "Sign in", "Status", "Files", "Admin", "Fake settings",
                              "Fake posts", "Billing", "Reports", "Space", "Dot", "Query",
                              "Injected", "Parametric", "Leads again", "Book again"})

    sitemap = _js_const(files["routes/seo.js"], "PUBLIC_ROUTES")
    assert "/book" in sitemap and "/team/leads" in sitemap
    assert "/admin/reports" not in sitemap and not [p for p in sitemap if p.lower().startswith("/admin")]

    reasons = [w["reason"] for w in rs.spec.engine_warnings if isinstance(w, dict)]
    assert reasons.count("custom_page_path_dropped") == 4
    assert reasons.count("custom_page_duplicate_dropped") == 3
    assert reasons.count("custom_page_under_admin_dropped") == 1


def test_nav_override_only_keeps_links_that_stay_on_the_site():
    spec = _base()
    spec["nav_items"] = [
        {"label": "Home", "href": "/"},
        {"label": "About", "href": " /about "},
        {"label": "Anchor", "href": "/#contact"},
        {"label": "Query", "href": "/contact?service=coaching"},
        {"label": "File", "href": "/uploads/2026/01/menu.pdf"},
        {"label": "Proto", "href": "//evil.example/x"},
        {"label": "Backslash", "href": "/\\evil.example/x"},
        {"label": "Tab", "href": "/\t/evil.example"},
        {"label": "Newline", "href": "/\n/evil.example"},
        {"label": "Scheme", "href": "https://evil.example/"},
        {"label": "Script", "href": "javascript:alert(1)"},
        {"label": "Parametric", "href": "/events/:slug"},
        {"label": "Encoded", "href": "/%2f%2fevil.example"},
        {"label": "Not a string", "href": ["/about"]},
    ]
    nav = _js_const(_files(spec)["middleware/settings.js"], "_NAV_ITEMS")
    assert [item["href"] for item in nav] == [
        "/", "/about", "/#contact", "/contact?service=coaching", "/uploads/2026/01/menu.pdf",
    ]


@pytest.mark.parametrize("value,offsite", [
    ("//evil.example/x", True),
    ("/\\evil.example", True),
    ("\\\\evil.example\\x", True),
    ("/\t/evil.example", True),
    ("\n//evil.example", True),
    ("https://evil.example", True),
    ("HTTPS://evil.example", True),
    ("javascript:alert(1)", True),
    ("mailto:a@b.example", True),
    ("/about", False),
    ("/events/:slug", False),
    ("/a b", False),
    ("book", False),
    ("/", False),
])
def test_offsite_reference_detection(value, offsite):
    assert B._is_offsite_reference(value) is offsite


def test_a_page_never_gives_a_model_a_public_listing_whether_or_not_it_asks_for_a_login():
    """A page at /testimonials, protected or not, is a page. It never sets
    public_listing_path on the Testimonial model (nothing in the engine
    sets that key), and the protected one keeps its guard."""
    spec = _base()
    spec["custom_pages"] = [{"path": "/testimonials", "title": "Praise", "auth_required": True}]
    for auth_required in (True, False):
        spec["custom_pages"][0]["auth_required"] = auth_required
        rs = resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))
        testimonial = next(m for m in rs.data_models if m["name"] == "Testimonial")
        assert testimonial.get("public_listing_path") is None
        assert B._public_model_mount(testimonial) == ("/testimonials", False)
        files = dict(compose_files(rs))
        assert "views/testimonials/index.ejs" not in files
        routes = {(verb, path): mw for verb, path, mw in _routes(files["routes/pages.js"])}
        assert routes[("get", "/testimonials")] == (", requireAuth" if auth_required else "")
        in_sitemap = "/testimonials" in _js_const(files["routes/seo.js"], "PUBLIC_ROUTES")
        assert in_sitemap is (not auth_required)
    # The archetype's decision file is cached at module scope and shared by
    # every build in the process: resolving must not have written to it.
    cached = next(m for m in B.get_archetype(spec["archetype"])["default_data_models"] if m["name"] == "Testimonial")
    assert cached.get("public_listing_path") is None


def test_a_page_named_like_a_model_never_moves_the_models_mount():
    spec = _base()
    spec["data_models"] = [_model(name="Admin")]
    spec["custom_pages"] = [{"path": "/admin", "title": "Team"}]
    server = _files(spec)["server.js"]
    assert "app.use('/admins', require('./routes/admins'));" in server
    assert "app.use('/admin', require('./routes/admins'));" not in server


# ---------------------------------------------------------------------------
# What a build publishes. A model's rows are served without a login only
# where the spec says so (public_listing_path) or where a page layout of the
# archetype reads the table; never because something else in the spec has
# the model's name.
# ---------------------------------------------------------------------------

CLIENT_FIELDS = [
    {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
    {"name": "email", "sql_type": "VARCHAR(255)"},
    {"name": "phone", "sql_type": "VARCHAR(50)"},
    {"name": "description", "sql_type": "TEXT"},
]


def _client(**overrides) -> dict:
    model = {"name": "Client", "fields": copy.deepcopy(CLIENT_FIELDS)}
    model.update(overrides)
    return model


# (label, fixture, models, custom pages, the model that must stay private,
#  the reason code the build report must carry or None)
NAME_COLLISIONS = [
    ("page at the model's slug", "coach", [_client()], [{"path": "/clients", "title": "Our clients"}],
     "Client", "page_named_like_model"),
    ("page at the singular", "coach", [_client()], [{"path": "/client"}], "Client", "page_named_like_model"),
    ("page at the first word of the slug", "coach", [_client(name="ClientNote")], [{"path": "/client"}],
     "ClientNote", "page_named_like_model"),
    ("other letter case and a trailing slash", "coach", [_client()], [{"path": "/Clients/"}],
     "Client", "page_named_like_model"),
    ("page at an explicit plural", "coach", [_client(plural="roster")], [{"path": "/roster"}],
     "Client", "page_named_like_model"),
    ("public_listing_path null", "coach", [_client(public_listing_path=None)], [{"path": "/clients"}],
     "Client", "page_named_like_model"),
    ("public_listing_path empty", "coach", [_client(public_listing_path="")], [{"path": "/clients"}],
     "Client", "page_named_like_model"),
    ("page below the model's mount", "coach", [_client()], [{"path": "/clients/all"}],
     "Client", "custom_page_route_taken_dropped"),
    ("no page at all", "coach", [_client()], [], "Client", None),
    ("submission-style model name", "coach", [_client(name="Lead")], [{"path": "/leads"}],
     "Lead", "custom_page_route_taken_dropped"),
    ("model the archetype keeps private", "event-producer", [], [{"path": "/photos", "title": "Gallery"}],
     "Photo", "page_named_like_model"),
    ("a page of the same kind as the archetype's", "healer", [_client()], [{"path": "/blog"}, {"path": "/clients"}],
     "Client", "page_named_like_model"),
    ("child of a model the archetype shows", "event-producer",
     [_client(name="Registration", belongs_to="Event")], [], "Registration", None),
    ("child of a model the spec lists", "coach",
     [_model(name="Workshop", public_listing_path="/workshops"), _client(name="Attendee", belongs_to="Workshop")],
     [{"path": "/attendees"}], "Attendee", "page_named_like_model"),
]


@pytest.mark.parametrize("label,fixture,models,pages,private,reason", NAME_COLLISIONS,
                         ids=[c[0] for c in NAME_COLLISIONS])
def test_nothing_named_like_a_model_publishes_its_rows(label, fixture, models, pages, private, reason):
    spec = json.loads((FIXTURES / f"sample-{fixture}-spec.json").read_text(encoding="utf-8"))
    spec["data_models"] = copy.deepcopy(models)
    spec["custom_pages"] = copy.deepcopy(pages)
    rs = resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))
    model = next(m for m in rs.data_models if m["name"] == private)
    slug, table = B._slug_for(model), B._table_name_for_model(model)
    files = dict(compose_files(rs))

    # 1. Nothing gave the model a public listing path.
    assert not model.get("public_listing_path")

    # 2. No public view of the model exists.
    assert f"views/{slug}/index.ejs" not in files and f"views/{slug}/detail.ejs" not in files

    # 3. Its router is mounted once for visitors, and there a GET of the
    #    mount path and of anything below it is turned away before a query.
    router = files[f"routes/{slug}.js"]
    assert f"'{slug}/index'" not in router and f"'{slug}/detail'" not in router, "the router names a public view"
    listing = router[router.index("router.get('/'"):router.index("router.get('/new'")]
    gate = re.search(r"if \(!req\.baseUrl\.startsWith\('/admin/'\)\) \{\n\s+return (next\(\)|res\.status\(404\))", listing)
    assert gate and gate.start() < listing.index("db.query")
    detail = router[router.index("router.get('/:id'"):router.index("router.get('/:id/edit'")]
    assert detail.index("if (!isAdmin) {\n      return res.status(404)") < detail.index("db.query")
    for verb_and_path in ("router.get('/new'", "router.post('/'", "router.get('/:id/edit'",
                          "router.post('/:id/update'", "router.post('/:id/delete'"):
        handler = router[router.index(verb_and_path):]
        assert handler.index("if (!req.baseUrl.startsWith('/admin/')) {\n      return res.status(404)") \
            < handler.index("db.query")

    # 4. No other emitted route file reads the table: routes/pages.js (the
    #    only router visitors reach besides the model's own) never names it.
    for path, body in files.items():
        if path.startswith("routes/") and path not in (f"routes/{slug}.js", "routes/admin-dashboard.js") \
                and isinstance(body, str):
            assert not re.search(rf"\b(FROM|INTO|UPDATE)\s+{table}\b", body), path

    # 5. The sitemap lists the page (when one is emitted), never a listing.
    kept = {B._path_key(cp["path"]) for cp in B._custom_pages_filter(rs)}
    for public_path in _js_const(files["routes/seo.js"], "PUBLIC_ROUTES"):
        if B._path_key(public_path) == B._path_key(f"/{slug}"):
            assert B._path_key(public_path) in kept

    # 6. The engine's one definition of "public" agrees with the files.
    assert table not in B._public_row_sources(rs)
    assert B._public_model_mount(model)[1] is False

    # 7. The build report tells the reviewer.
    reasons = [w["reason"] for w in rs.spec.engine_warnings if isinstance(w, dict)]
    if reason:
        assert reason in reasons, rs.spec.engine_warnings
    assert "spec_model_shown_by_archetype_page" not in [
        w["reason"] for w in rs.spec.engine_warnings
        if isinstance(w, dict) and repr(private) in w.get("detail", "")]


# The tables each archetype's own page layouts read, per archetype. This is
# the complete list of rows a build shows without the spec asking for it. A
# decision-file edit that publishes another table has to be made here too.
CURATED_PUBLIC_TABLES = {
    "healer-wellness": {"blog_posts", "events", "photos", "testimonials"},
    "coach-consultant": {"blog_posts", "case_studies", "testimonials"},
    "local-service": {"reviews"},
    "therapist-counselor": {"testimonials"},
    "fitness-studio": {"instructors", "schedule_slots", "testimonials"},
    "salon-spa": {"photos", "reviews", "staff"},
    "restaurant-cafe": {"menu_items", "photos"},
    "tradesperson-contractor": {"reviews", "service_areas"},
    "event-producer": {"events", "testimonials"},
    "author": {"blog_posts", "books", "events", "testimonials"},
}
# Never shown by any archetype, whatever its layouts say.
ALWAYS_PRIVATE_TABLES = {"contact_inquiries", "newsletter_subscribers", "users", "user_sessions",
                         "magic_link_tokens", "site_settings", "slug_aliases"}


def _bare_spec(archetype_id: str) -> dict:
    spec = _base()
    spec.update(archetype=archetype_id, palette="", font_pair="", pages={}, seed_content={})
    spec.pop("data_models", None)
    spec.pop("custom_pages", None)
    return spec


def test_the_tables_each_archetype_shows_are_exactly_the_enumerated_ones():
    archetype_ids = [a["id"] for a in B.load_archetypes()["archetypes"]]
    assert set(archetype_ids) == set(CURATED_PUBLIC_TABLES)
    for archetype_id in archetype_ids:
        rs = resolve_spec(BuildSpec.from_dict(_bare_spec(archetype_id)))
        sources = B._public_row_sources(rs)
        assert set(sources) == CURATED_PUBLIC_TABLES[archetype_id], archetype_id
        assert not set(sources) & ALWAYS_PRIVATE_TABLES, archetype_id
        # None of it comes from a listing: no archetype default carries a
        # public_listing_path, so every source is a page of the archetype.
        assert all("public_listing_path" not in where for found in sources.values() for where in found)
        assert [m["name"] for m in rs.data_models if m.get("public_listing_path")] == []
        # Every row routes/pages.js reads comes from one of those tables.
        # The one other FROM is the newsletter signup asking whether an
        # address is already stored; its answer is never sent to anyone.
        pages_js = dict(compose_files(rs))["routes/pages.js"]
        read = set(re.findall(r"SELECT \* FROM ([a-z_]+)", pages_js))
        assert read <= CURATED_PUBLIC_TABLES[archetype_id], (archetype_id, read)
        assert set(re.findall(r"\bFROM ([a-z_]+)", pages_js)) - read <= {"newsletter_subscribers"}
        assert "SELECT 1 FROM newsletter_subscribers WHERE" in pages_js or "newsletter_subscribers" not in pages_js


def test_every_layout_variant_reads_only_tables_its_archetype_is_known_to_show():
    """A layout variant (a sibling in the same variant_group) can be picked
    by a hint in the spec. No variant may read a table the archetype's
    enumerated set does not have."""
    layouts = B.load_layouts()["layouts"]
    # The layout a page gets when the spec supplies its text reads no table.
    assert B._layout_db_tables("copy-faithful-default") == set()
    for archetype in B.load_archetypes()["archetypes"]:
        archetype_id = archetype["id"]
        known = {B._table_name_for_model(m) for m in archetype["default_data_models"]}
        for page_id, layout_id in archetype["page_layouts"].items():
            group = layouts[layout_id].get("variant_group")
            siblings = [other for other, layout in layouts.items()
                        if group and layout.get("variant_group") == group] or [layout_id]
            assert layout_id in siblings
            for sibling in siblings:
                tables = B._layout_db_tables(sibling)
                assert tables & known <= CURATED_PUBLIC_TABLES[archetype_id], (archetype_id, page_id, sibling)
                assert not tables & ALWAYS_PRIVATE_TABLES, sibling


def test_a_spec_model_on_a_table_the_archetype_shows_is_reported():
    """The one place a name still matters: a layout reads a TABLE, so a
    spec model that redefines the archetype's model of that name is what
    the page shows. The build report says so for each such model."""
    spec = _base()
    spec["data_models"] = [_model(name="Testimonial", fields=[
        {"name": "author_name", "sql_type": "VARCHAR(255)"}, {"name": "quote", "sql_type": "TEXT"}])]
    rs = resolve_spec(BuildSpec.from_dict(spec))
    shown = [w["detail"] for w in rs.spec.engine_warnings
             if isinstance(w, dict) and w["reason"] == "spec_model_shown_by_archetype_page"]
    assert len(shown) == 1 and "'Testimonial'" in shown[0] and "the home page (testimonials-stack)" in shown[0]
    # Untouched archetype models are not reported.
    rs = resolve_spec(BuildSpec.from_dict(_base()))
    assert [w for w in rs.spec.engine_warnings
            if isinstance(w, dict) and w["reason"] == "spec_model_shown_by_archetype_page"] == []


def test_related_rows_are_read_only_for_a_child_that_is_public_itself():
    """Registration belongs_to Event. The event-producer's /events page has
    public detail pages; they must not read the registrations."""
    spec = json.loads((FIXTURES / "sample-event-producer-spec.json").read_text(encoding="utf-8"))
    spec["data_models"] = [_client(name="Registration", belongs_to="Event")]
    pages = _files(spec)["routes/pages.js"]
    assert "router.get('/events/:identifier'" in pages
    assert "FROM registrations" not in pages and "related_rows" not in pages
    # The author archetype's Testimonial belongs_to Book and is shown on the
    # home page, so the book page still lists a book's testimonials.
    author = _files(_bare_spec("author"))["routes/pages.js"]
    assert "SELECT * FROM testimonials WHERE book_id = $1" in author


def test_a_listing_page_survives_a_custom_page_of_the_same_kind():
    """The healer's /journal lists blog_posts. A custom /news page (same
    alias group) does not list them, so the journal is kept."""
    spec = json.loads((FIXTURES / "sample-healer-spec.json").read_text(encoding="utf-8"))
    spec["custom_pages"] = [{"path": "/news", "title": "News"}, {"path": "/photos", "title": "Photos"}]
    rs = resolve_spec(BuildSpec.from_dict(spec))
    assert {"journal", "gallery"} <= set(rs.page_layouts)
    assert {"blog_posts", "photos"} <= set(B._public_row_sources(rs))
    assert {cp["path"] for cp in B._custom_pages_filter(rs)} == {"/news", "/photos"}


def test_an_auth_required_page_that_loses_its_path_is_reported():
    """An auth_required page whose path a public page of the site answers
    used to be dropped with a warning while the path stayed public. The
    build is refused now (the site is deployed publicly before anyone reads
    a warning); test_model_derived_emission.py covers every other way a
    protected page can lose its path."""
    spec = _base()
    spec["custom_pages"] = [{"path": "/about", "title": "Members", "auth_required": True}]
    with pytest.raises(ValueError) as refused:
        resolve_spec(BuildSpec.from_dict(spec))
    message = str(refused.value)
    assert "'/about'" in message and "auth_required" in message
    assert "answered without a login by a page of the site" in message
    # The same page without the flag is the ordinary collision: it builds,
    # the site's own /about page is served, and nothing is reported.
    spec["custom_pages"][0]["auth_required"] = False
    rs = resolve_spec(BuildSpec.from_dict(spec))
    assert not [w for w in rs.spec.engine_warnings
                if isinstance(w, dict) and w["reason"].startswith("auth_required")]


def test_a_public_page_never_points_visitors_at_a_protected_form():
    """A content-less custom page gets a "Get in touch" button that leads to
    the first form page. A form behind auth_required is not that page."""
    spec = _protected_form_spec()
    spec["custom_pages"].append({"path": "/studio", "title": "Studio"})
    studio = _files(spec)["views/custom/studio.ejs"]
    assert '"href": "/contact"' in studio
    assert "/members-form" not in studio


def test_reserved_page_ids_cover_every_built_in_view_route_and_static_dir(tmp_path):
    """The reserved list is compared with what a build emits, so a built-in
    view or route added later cannot be forgotten in it."""
    # The healer sample: its home page carries the newsletter form, so this
    # build emits every built-in route, POST /newsletter-signup included
    # (a build whose pages do not show that form does not get the route).
    base = json.loads((FIXTURES / "sample-healer-spec.json").read_text(encoding="utf-8"))
    out = tmp_path / "app"
    build_app(copy.deepcopy(base), output_dir=out)
    page_ids = set(resolve_spec(BuildSpec.from_dict(copy.deepcopy(base))).page_layouts)

    view_names = {p.stem for p in (out / "views").glob("*.ejs")}
    built_in_views = {v for v in view_names
                      if v not in page_ids and not (v.endswith("-detail") and v[:-len("-detail")] in page_ids)}
    assert {"login", "layout-public", "error-404", "set-password"} <= built_in_views
    assert built_in_views <= B._RESERVED_PAGE_IDS, sorted(built_in_views - B._RESERVED_PAGE_IDS)

    static_dirs = {p.name for p in (out / "public").iterdir() if p.is_dir()}
    assert static_dirs and static_dirs <= B._RESERVED_PAGE_IDS, sorted(static_dirs - B._RESERVED_PAGE_IDS)

    route_names = {"admin"}  # mounted with app.use('/admin', ...)
    for name in ("routes/auth.js", "routes/legal.js", "server.js"):
        for _verb, path, _mw in _routes((out / name).read_text(encoding="utf-8")):
            first = path.strip("/").split("/")[0]
            if B._PAGE_ID_RE.fullmatch(first):
                route_names.add(first)
    for verb, path, _mw in _routes((out / "routes" / "pages.js").read_text(encoding="utf-8")):
        if verb == "post" and path.strip("/") not in page_ids:
            route_names.add(path.strip("/"))
    assert {"login", "logout", "magic", "health", "privacy", "newsletter-signup"} <= route_names
    assert route_names <= B._RESERVED_PAGE_IDS, sorted(route_names - B._RESERVED_PAGE_IDS)

    # And the list never names a page an archetype ships.
    for archetype in B.load_archetypes()["archetypes"]:
        assert B._RESERVED_PAGE_IDS.isdisjoint(archetype["default_pages"]), archetype["id"]


def test_every_reserved_page_id_is_refused_by_the_gate():
    for page_id in sorted(B._RESERVED_PAGE_IDS):
        spec = _base()
        _page(page_id)(spec)
        errors = validate_spec(BuildSpec.from_dict(spec))
        assert any("is reserved for a built-in view or route" in e for e in errors), page_id


@pytest.fixture(scope="module")
def guarded_builds(tmp_path_factory):
    """The two crafted specs of this section, built to disk."""
    os.environ["CAS_LINT_EXTERNAL_URLS"] = "skip"
    root = tmp_path_factory.mktemp("guards")
    builds = {}
    for name, make in (("protected-form", _protected_form_spec), ("dropped-pages", _dropped_custom_pages_spec)):
        builds[name] = build_app(make(), output_dir=root / name)
    return builds


@needs_node
def test_guarded_builds_emit_javascript_that_parses(guarded_builds):
    """The assertions above read source text; this checks the same builds
    are still valid JavaScript."""
    for out in guarded_builds.values():
        _node_check_all(out)


@needs_ejs
def test_guarded_builds_emit_views_that_compile(guarded_builds):
    for out in guarded_builds.values():
        proc = subprocess.run([NODE, "-e", _EJS_COMPILE_ALL, str(EJS_DIR), str(out / "views")],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr[:400]
        assert json.loads(proc.stdout)["bad"] == []
    login = (guarded_builds["dropped-pages"] / "views" / "login.ejs").read_text(encoding="utf-8")
    assert 'type="password"' in login and 'name="_csrf"' in login


# ---------------------------------------------------------------------------
# A2. Context escapers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("pending", "'pending'"),
    ("it's", "'it''s'"),
    ("'[]'::jsonb", "'[]'::jsonb"),
    ("'{}'::text[]", "'{}'::text[]"),
    ("'quoted'", "'quoted'"),
    ("NOW()", "NOW()"),
    ("now()", "NOW()"),
    ("0", "0"),
    (True, "TRUE"),
    (None, "NULL"),
    # The two pass-through shapes used to be prefix tests. Each of these was
    # emitted verbatim into CREATE TABLE.
    ("'x'::text); DROP TABLE users; --", "'''x''::text); DROP TABLE users; --'"),
    ("'a'; DROP TABLE users; --'", "'''a''; DROP TABLE users; --'''"),
    ("'x'::jsonb || (SELECT password FROM users LIMIT 1)::jsonb",
     "'''x''::jsonb || (SELECT password FROM users LIMIT 1)::jsonb'"),
    ("pg_sleep(10)", "'pg_sleep(10)'"),
])
def test_format_sql_default_only_passes_through_complete_literals(value, expected):
    assert B._format_sql_default(value) == expected


def test_sql_literal_escaper():
    assert B._sql_esc("O'Brien") == "O''Brien"
    assert B._sql_esc("a\\'; DROP") == "a\\''; DROP"
    assert "\x00" not in B._sql_esc("a\x00b")
    assert B._sql_esc(None) == ""


def test_sql_comment_escaper_keeps_the_value_on_one_line():
    hostile = "editor\nDELETE FROM users; --\r\nx" + LINE_SEP + "y\x0bz"
    out = B._sql_comment(hostile)
    assert "\n" not in out and "\r" not in out and LINE_SEP not in out and "\x0b" not in out
    assert out.startswith("editor DELETE FROM users;")


def test_js_string_escapers():
    hostile = "a'b\\c\nd\re" + LINE_SEP + PARA_SEP + "`${x}`</script>"
    body = B._js_escape(hostile)
    assert "\n" not in body and "\r" not in body and LINE_SEP not in body and PARA_SEP not in body
    assert "\\'" in body and "\\\\" in body
    assert json.loads(B._js_str(hostile)) == hostile


def test_ejs_json_never_contains_an_ejs_delimiter():
    hostile = {"a %> b": ["<% x %>", "-%>", "_%>", "</script>", "<!--", LINE_SEP, 'q"q', "é"]}
    out = B._ejs_json(hostile)
    for needle in ("<", ">", LINE_SEP):
        assert needle not in out
    # Still the same data once a JS engine (or json) decodes it.
    assert json.loads(out) == hostile


def test_to_ejs_data_literal_escapes_keys_and_nested_values():
    literal = B._to_ejs_data_literal({
        "k %>": "v",
        "nested": {"x": "a %> b"},
        "list": ["<%- evil %>"],
        "ref": B._EJSRef("settings.site_name || " + B._ejs_json("a %> b")),
    })
    assert "%>" not in literal and "<%" not in literal


@pytest.mark.parametrize("value,expected", [
    ("https://example.com/a", "https://example.com/a"),
    ("http://example.com", "http://example.com"),
    ("/contact?service=x", "/contact?service=x"),
    ("#top", "#top"),
    ("about.html", "about.html"),
    ("mailto:a@b.example", "mailto:a@b.example"),
    ("tel:+15550100", "tel:+15550100"),
    ("sms:5550100", "sms:5550100"),
    ("javascript:alert(1)", "#"),
    ("JaVaScRiPt:alert(1)", "#"),
    (" javascript:alert(1)", "#"),
    ("java\tscript:alert(1)", "#"),
    ("java\nscript:alert(1)", "#"),
    ("\x01javascript:alert(1)", "#"),
    ("data:text/html,<script>alert(1)</script>", "#"),
    ("vbscript:msgbox(1)", "#"),
    ("file:///etc/passwd", "#"),
    (None, None),
    (5, 5),
])
def test_neutralize_url(value, expected):
    assert B._neutralize_url(value) == expected


def test_sanitize_spec_content_is_key_aware_and_does_not_mutate():
    original = {
        "heading": "javascript:not-a-url-field",
        "cta_primary": {"text": "Go", "href": "javascript:alert(1)"},
        "items": [{"title": "t", "cta_href": "vbscript:x", "image_url": "https://img.example/a.jpg"}],
        "body_html": "<p onclick=\"x()\">hi</p><script>alert(1)</script>",
        "ref": B._EJSRef("settings.x"),
    }
    snapshot = copy.deepcopy({k: v for k, v in original.items() if k != "ref"})
    out = B._sanitize_spec_content(original)
    assert out["heading"] == "javascript:not-a-url-field"  # not a URL key: left for the HTML escaper
    assert out["cta_primary"]["href"] == "#"
    assert out["items"][0]["cta_href"] == "#"
    assert out["items"][0]["image_url"] == "https://img.example/a.jpg"
    assert "<script" not in out["body_html"] and "onclick=\"" not in out["body_html"]
    assert out["ref"] is original["ref"]
    assert {k: v for k, v in original.items() if k != "ref"} == snapshot


# ---------------------------------------------------------------------------
# A3. Hostile free text through the whole pipeline
# ---------------------------------------------------------------------------

PAYLOAD = (
    "x'\"`${7*7}</script><script>alert(1)</script> %> <%- global.process.exit(7) %> "
    "*/ --> \\ \n; DROP TABLE users; -- "
)


def _hostile_text_spec() -> dict:
    spec = _base()
    s = spec["settings"]
    for key in ("site_name", "site_description", "hero_tagline", "hero_description",
                "about_title", "about_body", "business_name", "contact_address"):
        s[key] = PAYLOAD + key
    s["instagram_url"] = "javascript:alert(1)"
    s["service_options"] = [PAYLOAD + "opt"]
    home = spec["pages"].setdefault("home", {})
    hero = home.setdefault("hero", {})
    hero["eyebrow"] = PAYLOAD + "eyebrow"
    hero["cta_primary"] = {"text": PAYLOAD + "cta", "href": "javascript:alert(document.cookie)"}
    spec["pages"]["faithful"] = {
        "title": PAYLOAD + "ptitle",
        "description": PAYLOAD + "pdesc",
        "hero": {"heading": PAYLOAD + "h", "body_html": "<p>ok</p><img src=x onerror=alert(1)><base href=//evil.example>"},
        "_copy_blocks": ["# " + PAYLOAD, "[x](javascript:alert(1)) " + PAYLOAD, "- item %> <% x"],
    }
    spec["custom_pages"] = [
        {"path": "/book", "title": PAYLOAD + "title", "purpose": PAYLOAD + "purpose",
         "description": PAYLOAD + "desc", "form_target": "BookingRequest",
         "allowed_fields": ["name", "email", "message"],
         "success_message": PAYLOAD + "ok", "submit_text": PAYLOAD + "send"},
        {"path": "/members", "title": "Members", "auth_required": True,
         "allowed_roles": ["x'), (q,r)=>r.send('pwn'), requireRole('y", "admin"]},
        {"path": "/x', (q,r)=>r.send(process.env.SESSION_SECRET)); router.get('/y", "title": "Injected"},
        {"path": "/a b", "title": "Space"},
        {"path": "/search?q=1", "title": "Query"},
        {"path": "/dot.html", "title": "Dot"},
        {"path": "/events/:slug", "title": "Parametric"},
    ]
    spec["data_models"] = [{
        "name": "BookingRequest",
        "fields": [
            {"name": "name", "sql_type": "VARCHAR(255)", "required": True, "placeholder": PAYLOAD + "ph",
             "label": PAYLOAD + "label", "help": PAYLOAD + "help"},
            {"name": "kind", "field_type": "select", "options": [PAYLOAD + "o1", "plain"]},
            {"name": "link_url", "sql_type": "TEXT"},
        ],
    }]
    spec["seed_content"] = {
        "booking_requests": [
            {"name": PAYLOAD + "seed", "kind": "plain", "link_url": "javascript:alert(1)"},
            {"name": "nan", "kind": float("nan")},
            "not-a-row",
        ],
    }
    spec["user_roles"] = [
        {"role": "editor\nDELETE FROM users; --", "description": "d\r\nDROP TABLE users;"},
        "not-a-role",
    ]
    spec["nav_items"] = [{"label": PAYLOAD + "nav", "href": "/about"}]
    spec["_translation_warnings"] = [{"reason": "r\nforged: line", "detail": "d\nsha256: forged"},
                                     "a plain string\nwarning", 7]
    spec["tier"] = "build\nsha256: forged"
    return spec


@pytest.fixture(scope="module")
def hostile_build(tmp_path_factory):
    out = tmp_path_factory.mktemp("hostile") / "app"
    os.environ["CAS_LINT_EXTERNAL_URLS"] = "skip"
    build_app(_hostile_text_spec(), output_dir=out)
    return out


def test_hostile_text_builds_and_stays_inside_the_build_dir(hostile_build):
    assert (hostile_build / "server.js").exists()
    siblings = sorted(p.name for p in hostile_build.parent.iterdir())
    assert siblings == ["app"], siblings


def test_hostile_custom_page_paths_are_dropped_with_a_warning(hostile_build):
    pages = (hostile_build / "routes" / "pages.js").read_text(encoding="utf-8")
    assert "process.env.SESSION_SECRET" not in pages
    assert "router.get('/book'" in pages and "router.get('/members'" in pages
    for dropped in ("/a b", "/search", "/dot.html", "/events/:slug"):
        assert f"router.get('{dropped}" not in pages
    custom_views = sorted(p.name for p in (hostile_build / "views" / "custom").iterdir())
    assert custom_views == ["book.ejs", "members.ejs"]
    warnings = (hostile_build / "translation-warnings.txt").read_text(encoding="utf-8")
    assert warnings.count("custom_page_path_dropped") == 4  # the parametric one is dropped silently, as before


def test_hostile_role_name_is_a_string_literal_not_code(hostile_build):
    pages = (hostile_build / "routes" / "pages.js").read_text(encoding="utf-8")
    assert "r.send('pwn')" not in pages
    # The page lists two roles, so it gets one requireAnyRole guard whose
    # array holds two string literals: the hostile name, escaped, and admin.
    assert ("requireAnyRole(['x\\'), (q,r)=>r.send(\\'pwn\\'), requireRole(\\'y', 'admin'])") in pages


def test_seed_sql_has_no_statement_outside_its_own_inserts(hostile_build):
    """Every line of seed.sql is blank, a comment, or the start of an INSERT
    the engine wrote. A newline smuggled into a comment or a quote smuggled
    out of a literal would produce a line that is none of those."""
    seed = (hostile_build / "db" / "seed.sql").read_text(encoding="utf-8")
    in_literal = False
    for line in seed.split("\n"):
        if not in_literal:
            assert line == "" or line.startswith("-- ") or line.startswith("INSERT INTO "), line[:120]
        # Track whether this line ends inside a single-quoted literal (the
        # payload contains newlines, which are legal inside one).
        if line.startswith("-- ") and not in_literal:
            continue
        if line.count("'") % 2 == 1:
            in_literal = not in_literal
    assert not in_literal
    assert "--   - editor DELETE FROM users; --: d DROP TABLE users;" in seed
    assert "'javascript:alert(1)'" not in seed, "a javascript: URL was seeded into a *_url column"
    assert "VALUES ('nan', NULL, NULL)" in seed, "NaN must be stored as NULL, not as a bare word"
    assert "not-a-row" not in seed


def test_manifest_lines_cannot_be_forged(hostile_build):
    manifest = (hostile_build / "compose-manifest.txt").read_text(encoding="utf-8")
    assert sum(1 for line in manifest.split("\n") if line.startswith("sha256: ")) == 1
    lines = manifest.split("\n")
    assert "tier: build sha256: forged" in lines
    # What the spec carried is written under its own prefix, one line each.
    assert "spec-warning: r forged: line: d sha256: forged" in lines
    assert "spec-warning: a plain string warning" in lines, "string warnings are kept, on one line"
    # No line the spec supplied starts the way an engine warning or a
    # header line does.
    assert not [line for line in lines if line.startswith("warning: r") or line.startswith("warning: a plain")]
    assert sum(1 for line in lines if line.startswith("translation_warnings_count: ")) == 1


@needs_node
def test_every_emitted_js_file_parses(hostile_build):
    scripts = sorted(hostile_build.rglob("*.js"))
    assert len(scripts) > 20
    for script in scripts:
        proc = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{script.relative_to(hostile_build)}: {proc.stderr[:400]}"


@needs_node
def test_hostile_title_round_trips_as_data_in_route_source(hostile_build):
    """Evaluate the emitted `page_title: '...'` literal with a real JS engine
    and compare it with the spec value: it must decode to the same text
    (line breaks become spaces), which it can only do if nothing in it was
    interpreted as code."""
    import re as _re
    pages = (hostile_build / "routes" / "pages.js").read_text(encoding="utf-8")
    start = pages.index("res.render('custom/book'")
    literal = _re.search(r"page_title: ('(?:[^'\\\n]|\\.)*')", pages[start:]).group(1)
    proc = subprocess.run(
        [NODE, "-e", "process.stdout.write(JSON.stringify(" + literal + "))"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr[:300]
    assert json.loads(proc.stdout) == (PAYLOAD + "title").replace("\n", " ")


_EJS_COMPILE_ALL = r"""
const fs = require('fs'); const path = require('path');
const ejs = require(process.argv[1]);
const bad = [];
let n = 0;
(function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full);
    else if (full.endsWith('.ejs')) {
      n += 1;
      try { ejs.compile(fs.readFileSync(full, 'utf8'), { filename: full }); }
      catch (err) { bad.push(path.relative(process.argv[2], full) + ': ' + String(err.message).split('\n')[0]); }
    }
  }
})(process.argv[2]);
process.stdout.write(JSON.stringify({ n, bad }));
"""

_EJS_RENDER = r"""
const ejs = require(process.argv[1]);
const data = JSON.parse(process.argv[3]);
ejs.renderFile(process.argv[2], data, {}, function (err, html) {
  if (err) { process.stderr.write(String(err.message)); process.exit(1); }
  process.stdout.write(html);
});
"""

_VIEW_LOCALS = {
    "settings": {
        "site_name": "Test Site", "site_description": "desc", "contact_email": "a@b.example",
        "_palette_vars": {"--color-primary": "#111111", "--color-on-primary": "#ffffff"},
        "_fonts": {"display": {"family": "Georgia", "is_serif": True}, "body": {"family": "system-ui"}},
        "_fonts_url": "", "_field_types": {}, "_us_states": [],
    },
    "csrfToken": "00ff",
    "cspNonce": "nonce",
    "nav_items": [],
    "user": None,
    "currentPath": "/",
}


def _render(view: Path, extra: dict = None) -> str:
    data = dict(_VIEW_LOCALS)
    data.update(extra or {})
    proc = subprocess.run(
        [NODE, "-e", _EJS_RENDER, str(EJS_DIR), str(view), json.dumps(data)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"{view.name}: {proc.stderr[:600]}"
    return proc.stdout


@needs_ejs
def test_every_widget_and_template_compiles():
    """Compile every EJS source the engine ships. Two widgets (photo
    slideshow, video embed) did not compile at all: a comment inside their
    script block contained a literal EJS tag, which closed the block."""
    for root in (COMPOSE_DIR / "widgets", COMPOSE_DIR / "templates"):
        proc = subprocess.run([NODE, "-e", _EJS_COMPILE_ALL, str(EJS_DIR), str(root)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr[:400]
        result = json.loads(proc.stdout)
        assert result["n"] > 0
        assert result["bad"] == [], result["bad"]


@needs_ejs
def test_every_emitted_view_compiles_with_hostile_text(hostile_build):
    """Spec text containing `%>` used to end the EJS tag it was embedded in,
    so the view failed to compile and the page returned 500 forever."""
    proc = subprocess.run([NODE, "-e", _EJS_COMPILE_ALL, str(EJS_DIR), str(hostile_build / "views")],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[:400]
    result = json.loads(proc.stdout)
    assert result["n"] > 80
    assert result["bad"] == [], result["bad"]


@needs_ejs
def test_hostile_page_renders_escaped(hostile_build):
    html = _render(hostile_build / "views" / "faithful.ejs")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "global.process.exit" in html, "the EJS payload should be visible as inert text"
    # body_html: the allowed tag survives, everything else is text.
    assert "<p>ok</p>" in html
    assert "<img src=x" not in html and "<base" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    # markdown link with a javascript: target keeps its label, loses the link.
    assert 'href="javascript:' not in html and "href='javascript:" not in html

    home = _render(hostile_build / "views" / "home.ejs", {"items": [], "items_testimonials": []})
    assert "<script>alert(1)</script>" not in home
    assert "javascript:alert" not in home, "a spec-supplied javascript: href reached the page"
    assert 'href="#"' in home

    form = _render(hostile_build / "views" / "custom" / "book.ejs", {"submitted": {}})
    assert "<script>alert(1)</script>" not in form
    assert 'placeholder="x&#39;&quot;`${7*7}&lt;/script&gt;' in form or "placeholder=\"x'&quot;`${7*7}&lt;/script&gt;" in form


@needs_ejs
def test_admin_form_view_renders_hostile_placeholder_as_text(hostile_build):
    html = _render(hostile_build / "views" / "booking-requests" / "new.ejs",
                   {"mode": "new", "record": None, "error": None, "parent_rows": []})
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ---------------------------------------------------------------------------
# A4. The central file writer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "../escaped.txt",
    "views/../../escaped.ejs",
    "views/../../../../tmp/x",
    "/etc/cron.d/x",
    "C:/Windows/x",
    "views\\..\\..\\x",
    "views/./x.ejs",
    "views//x.ejs",
    "views/x.ejs/",
    "",
    ".",
    "..",
    "views/a\x00b.ejs",
    None,
    7,
    b"views/x.ejs",
])
def test_safe_output_path_refuses(rel, tmp_path):
    with pytest.raises(ValueError, match="refusing to write"):
        B._safe_output_path(tmp_path, rel)


def test_safe_output_path_accepts_plain_relative_paths(tmp_path):
    for rel in ("server.js", "routes/blog-posts.js", "views/widgets/hero/centered.ejs",
                "public/brand/logo-16.png", ".env.example", "views/about..ejs"):
        full = B._safe_output_path(tmp_path, rel)
        assert tmp_path.resolve() in full.parents


def test_safe_output_path_refuses_a_symlink_that_leaves_the_build_dir(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    build = tmp_path / "build"
    build.mkdir()
    (build / "views").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes the build directory"):
        B._safe_output_path(build, "views/home.ejs")


def test_build_app_writes_nothing_when_any_path_is_unsafe(tmp_path, monkeypatch):
    """The writer checks EVERY path before writing ANY file, so one bad
    entry cannot leave a half-written build behind or a file outside it."""
    real = B.compose_files

    def poisoned(rs):
        return real(rs) + [("views/../../escaped.ejs", "pwned")]

    monkeypatch.setattr(B, "compose_files", poisoned)
    out = tmp_path / "build" / "app"
    with pytest.raises(ValueError, match="refusing to write"):
        build_app(_base(), output_dir=out)
    assert not (tmp_path / "build" / "escaped.ejs").exists()
    assert not (tmp_path / "escaped.ejs").exists()
    assert list(out.rglob("*")) == []


@pytest.mark.parametrize("extra,message", [
    (("views/login.ejs", "a second login view"), "two emitted files have the output path 'views/login.ejs'"),
    (("Views/LOGIN.ejs", "the same file on a case-insensitive disk"), "two emitted files have the output path"),
    (("compose-manifest.txt", "forged"), "is reserved for the build report"),
    (("translation-warnings.txt", "forged"), "is reserved for the build report"),
])
def test_writer_refuses_a_second_file_at_an_emitted_path(extra, message, tmp_path, monkeypatch):
    """Two emitters producing one path used to mean the later one silently
    won (that is how a page id of "login" replaced the login form). The
    writer now fails the build, before writing anything."""
    real = B.compose_files
    monkeypatch.setattr(B, "compose_files", lambda rs: real(rs) + [extra])
    out = tmp_path / "build" / "app"
    with pytest.raises(ValueError, match="refusing to write") as raised:
        build_app(_base(), output_dir=out)
    assert message in str(raised.value)
    assert list(out.rglob("*")) == []


def test_no_shipped_fixture_emits_one_path_twice():
    """The writer's collision check must not fire on a legitimate build."""
    for fx in sorted(FIXTURES.glob("sample-*-spec.json")):
        spec = json.loads(fx.read_text(encoding="utf-8"))
        paths = [path.casefold() for path, _ in compose_files(resolve_spec(BuildSpec.from_dict(spec)))]
        assert len(paths) == len(set(paths)), fx.name
        assert not set(paths) & set(B._BUILD_REPORT_FILES), fx.name


# ---------------------------------------------------------------------------
# A5. External-URL linter: server-side request forgery
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ip,public", [
    ("93.184.216.34", True),
    ("2606:2800:220:1:248:1893:25c8:1946", True),
    ("127.0.0.1", False),
    ("10.0.0.5", False),
    ("172.16.0.1", False),
    ("192.168.1.1", False),
    ("169.254.169.254", False),   # cloud metadata
    ("100.64.0.1", False),        # carrier-grade NAT
    ("0.0.0.0", False),
    ("224.0.0.1", False),
    ("::1", False),
    ("fe80::1", False),
    ("fd00::1", False),
    ("::ffff:127.0.0.1", False),  # v4-mapped loopback
    ("::ffff:10.0.0.1", False),
    ("2002:7f00:1::", False),     # 6to4 wrapping 127.0.0.1
    ("64:ff9b::7f00:1", False),   # NAT64 wrapping 127.0.0.1
    ("not-an-ip", False),
])
def test_is_public_address(ip, public):
    assert L._is_public_address(ip) is public


@pytest.mark.parametrize("url,reason", [
    ("ftp://example.com/x", "not an http(s) URL"),
    ("file:///etc/passwd", "not an http(s) URL"),
    ("http://user:pw@example.com/", "credentials"),
    ("http://example.com:22/", "non-default port"),
    ("http://example.com:6379/", "non-default port"),
    ("http://exa mple.com/", "whitespace"),
    ("http://example.com/\r\nHost: x", "whitespace"),
    ("http:///nohost", "no host"),
    ("http://" + "a" * 3000 + ".example/", "too long"),
])
def test_url_precheck_refuses(url, reason):
    assert reason in (L._url_precheck(url) or "")


def test_url_precheck_accepts_ordinary_urls():
    for url in ("https://example.com/", "http://example.com:80/a?b=c", "https://example.com:443/x#y"):
        assert L._url_precheck(url) is None


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://[::1]/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::ffff:10.0.0.1]/",
])
def test_checker_refuses_internal_destinations_without_connecting(url, monkeypatch):
    """The real check_url path, with only socket.connect instrumented: the
    guard must refuse BEFORE any connection attempt. (These are IP literals,
    so no DNS traffic is generated either. Names, including numeric forms
    such as "2130706433", are judged by the address they RESOLVE to; that
    path is covered with a stubbed resolver below.) This also proves the
    guard is actually installed on the connections http.client makes."""
    import socket as _socket
    attempts = []

    def refuse_and_record(self, addr):
        attempts.append(addr)
        raise OSError("no connection is allowed in this test")

    monkeypatch.setattr(_socket.socket, "connect", refuse_and_record)
    result = L.check_url(url, timeout=2.0)
    assert result["ok"] is False and result["status"] is None
    assert "non-public address" in (result["error"] or ""), result
    assert attempts == []


def test_guarded_connection_refuses_a_name_with_any_private_answer(monkeypatch):
    """A hostname that resolves to a public AND a private address is
    refused outright, and the public one is never connected to."""
    import socket as _socket
    answers = [
        (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
        (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("10.0.0.7", 80)),
    ]
    monkeypatch.setattr(_socket, "getaddrinfo", lambda *a, **k: answers)
    attempts = []
    monkeypatch.setattr(_socket.socket, "connect", lambda self, addr: attempts.append(addr))
    with pytest.raises(L.BlockedDestinationError):
        L._guarded_create_connection(("rebind.example", 80), 2.0)
    assert attempts == []


def test_guarded_connection_judges_a_name_by_what_it_resolves_to(monkeypatch):
    """"2130706433" is not an IP literal to a URL parser, but the resolver
    turns it into 127.0.0.1. The guard looks at the resolved address."""
    import socket as _socket
    monkeypatch.setattr(_socket, "getaddrinfo", lambda *a, **k: [
        (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80)),
    ])
    with pytest.raises(L.BlockedDestinationError) as refused:
        L._guarded_create_connection(("2130706433", 80), 2.0)
    # The address that decided it is on the exception for a caller that
    # needs it, and is not in the message (the message reaches the build
    # log; see the next test).
    assert refused.value.address == "127.0.0.1"
    assert str(refused.value) == "2130706433 resolves to a non-public address"


def test_a_refused_destination_is_reported_without_the_address_it_resolved_to(monkeypatch, tmp_path):
    """A URL in a spec whose host resolves to an internal address is refused,
    and the warning says so without the address. The warning goes into the
    build report, and the worker copies report lines into the build log the
    site's owner reads: what a name resolves to inside the worker's network
    must not be in it.

    The whole path is real (extract, precheck, the guarded opener, urllib's
    wrapping of the error, the warning, the two report files); only the
    resolver's answer and socket.connect are replaced.
    """
    import socket as _socket
    internal = "10.20.30.40"
    monkeypatch.setattr(_socket, "getaddrinfo", lambda *a, **k: [
        (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", (internal, 443)),
    ])
    attempts = []
    monkeypatch.setattr(_socket.socket, "connect", lambda self, addr: attempts.append(addr))

    spec = _base()
    spec["settings"]["instagram_url"] = "https://intranet-wiki.example/x"
    warnings = L.lint_external_urls(spec, mode="warn")
    assert attempts == []
    # Every external URL of the spec resolves to the internal address here,
    # so every one of them is refused; none of the warnings names it.
    details = [w["detail"] for w in warnings]
    assert [d for d in details if d.startswith("settings.instagram_url=https://intranet-wiki.example/x ")], details
    assert details and all("resolves to a non-public address" in d for d in details)
    assert not [d for d in details if internal in d or "10.20" in d]

    # The text of the refusal is fixed. It is not built from the exception,
    # so an error that did carry an address (in its own wording, or wrapped
    # by urllib) could not put one into the warning either.
    class Chatty(L.BlockedDestinationError):
        def __str__(self):
            return f"{self.host} resolves to {self.address}"

    import urllib.error as _urlerror
    for raised in (Chatty("intranet-wiki.example", internal), _urlerror.URLError(Chatty("h.example", internal))):
        def opener(_req, timeout=None, _raised=raised):
            raise _raised
        result = L._check_url_via("https://intranet-wiki.example/x", 2.0, opener=opener)
        assert result == {
            "url": "https://intranet-wiki.example/x", "ok": False, "status": None,
            "error": L.BLOCKED_DESTINATION_TEXT,
        }

    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "warn")
    out = build_app(spec, output_dir=tmp_path / "app")
    for name in ("compose-manifest.txt", "translation-warnings.txt"):
        report = (out / name).read_text(encoding="utf-8")
        assert "warning: external_url_unreachable: settings.instagram_url=" in report, name
        assert internal not in report, name


def test_guarded_connection_connects_to_the_address_it_checked(monkeypatch):
    """No second resolution: the socket connects to the sockaddr that passed
    the check, so a DNS answer cannot change between check and use."""
    import socket as _socket
    calls = []

    def fake_getaddrinfo(*a, **k):
        calls.append(a)
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

    connected = []
    monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(_socket.socket, "connect", lambda self, addr: connected.append(addr))
    sock = L._guarded_create_connection(("public.example", 80), 2.0)
    sock.close()
    assert len(calls) == 1
    assert connected == [("93.184.216.34", 80)]


def test_redirect_handler_refuses_non_http_targets():
    import urllib.error
    import urllib.request
    handler = L._LimitedRedirectHandler()
    req = urllib.request.Request("http://example.com/")
    for target in ("ftp://example.com/x", "file:///etc/passwd", "gopher://example.com/"):
        with pytest.raises(urllib.error.HTTPError):
            handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, target)
    assert handler.max_redirections == L.MAX_REDIRECTS <= 5


def test_checker_opener_has_no_proxy_file_or_ftp_handler():
    L._opener = None
    import urllib.request
    captured = {}
    real_open = urllib.request.OpenerDirector.open

    def spy(self, *a, **k):
        captured["handlers"] = [type(h).__name__ for h in self.handlers]
        raise OSError("stop before any I/O")

    try:
        urllib.request.OpenerDirector.open = spy
        with pytest.raises(OSError):
            L._guarded_open(urllib.request.Request("http://example.com/"), timeout=1)
    finally:
        urllib.request.OpenerDirector.open = real_open
        L._opener = None
    names = captured["handlers"]
    assert "_GuardedHTTPHandler" in names and "_GuardedHTTPSHandler" in names
    for banned in ("ProxyHandler", "FileHandler", "FTPHandler", "DataHandler", "HTTPHandler", "HTTPSHandler"):
        assert banned not in names, names


def test_lint_caps_the_number_of_urls_it_will_check():
    spec = {"settings": {}, "seed_content": {"links": [{"url": f"https://example.com/{i}"} for i in range(L.MAX_URLS + 25)]}}
    seen = []

    def check(url):
        seen.append(url)
        return {"url": url, "ok": True, "status": 200, "error": None}

    warnings = L.lint_external_urls(spec, mode="error", check_fn=check)
    assert len(seen) == L.MAX_URLS
    assert [w["reason"] for w in warnings] == ["external_url_lint_truncated"]
    assert "25 external URL(s) were not checked" in warnings[0]["detail"]


def test_lint_stops_at_the_time_budget(monkeypatch):
    spec = {"seed_content": {"links": [{"url": f"https://example.com/{i}"} for i in range(10)]}}
    import types
    clock = {"now": 1000.0}
    # Replace the linter's reference to the time module, not the clock
    # itself, so nothing else in the process sees the fake.
    monkeypatch.setattr(L, "time", types.SimpleNamespace(monotonic=lambda: clock["now"]))
    seen = []

    def slow_check(url):
        seen.append(url)
        clock["now"] += L.TOTAL_BUDGET_SECONDS / 2 + 1
        return {"url": url, "ok": True, "status": 200, "error": None}

    warnings = L.lint_external_urls(spec, mode="warn", check_fn=slow_check)
    assert len(seen) == 2
    assert warnings[-1]["reason"] == "external_url_lint_truncated"
    assert "8 external URL(s) were not checked" in warnings[-1]["detail"]


# The next tests drive the real checker (real sockets, real http.client and
# urllib handlers) against throwaway servers on loopback. Two things have to
# be relaxed for that, and each test says which: the address policy (loopback
# is not a public address) and, where a request must reach a server on an
# ephemeral port, the URL precheck (which only allows ports 80 and 443).

@contextlib.contextmanager
def _loopback_server(respond):
    """A TCP server on 127.0.0.1. For each connection it reads the request
    head and calls `respond(conn, request_line)`. Yields (port, request
    lines seen so far)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.1)
    seen = []
    stop = threading.Event()

    def serve(conn):
        with conn:
            conn.settimeout(5)
            head = b""
            try:
                while b"\r\n\r\n" not in head:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    head += chunk
                line = head.split(b"\r\n", 1)[0].decode("latin-1")
                seen.append(line)
                respond(conn, line)
            except OSError:
                pass

    def accept_loop():
        while not stop.is_set():
            try:
                conn, _addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=serve, args=(conn,), daemon=True).start()

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], seen
    finally:
        stop.set()
        thread.join(2)
        listener.close()


def _reply(conn, status_line: str, *headers: str) -> None:
    conn.sendall(("\r\n".join([status_line, *headers, "Content-Length: 0", "", ""])).encode("latin-1"))


@pytest.mark.parametrize("target,refused", [
    ("http://example.com:22/", "non-default port"),
    ("http://example.com:6379/", "non-default port"),
    ("https://user:pw@example.com/", "credentials"),
    ("ftp://example.com/x", "not an http(s) URL"),
    ("http://example.com/" + "a" * 3000, "too long"),
], ids=["port-22", "port-6379", "credentials", "ftp", "too-long"])
def test_redirect_targets_get_the_same_precheck_as_the_first_url(target, refused):
    import urllib.error
    import urllib.request
    handler = L._LimitedRedirectHandler()
    req = urllib.request.Request("http://example.com/")
    with pytest.raises(urllib.error.HTTPError) as raised:
        handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, target)
    assert refused in str(raised.value.reason)


def test_an_allowed_redirect_is_followed_without_reading_its_body():
    import urllib.request
    handler = L._LimitedRedirectHandler()
    req = urllib.request.Request("http://example.com/", method="HEAD")
    body = io.BytesIO(b"x" * 1000)
    new = handler.redirect_request(req, body, 302, "Found", {}, "https://example.com/next")
    assert new.full_url == "https://example.com/next"
    assert body.closed, "the redirect response must be closed unread"


def test_checker_does_not_follow_a_redirect_to_another_port(monkeypatch):
    """Real path: a public-looking host answers 302 to a different port (a
    stand-in for an internal service). The first URL's precheck never saw
    that port; the hop used to be made."""
    monkeypatch.setattr(L, "_is_public_address", lambda ip: True)  # reach loopback
    with _loopback_server(lambda conn, line: _reply(conn, "HTTP/1.1 200 OK")) as (inner_port, inner_seen):
        def redirect(conn, line):
            _reply(conn, "HTTP/1.1 302 Found", f"Location: http://127.0.0.1:{inner_port}/internal")
        with _loopback_server(redirect) as (port, seen):
            # _check_url_via, not check_url: the first URL is on an
            # ephemeral port, which check_url's own precheck would refuse.
            result = L._check_url_via(f"http://127.0.0.1:{port}/start", 3.0, L._guarded_open)
    assert seen == ["HEAD /start HTTP/1.1"]
    assert inner_seen == [], "the checker connected to the redirect target"
    assert result["ok"] is False and result["status"] == 302


def test_checker_does_not_read_the_body_of_a_redirect(monkeypatch):
    """Real path: HEAD is refused (405), so the checker falls back to GET;
    the GET answers 302 with a 64 MiB body. The stock urllib handler reads
    such a body to the end before following."""
    monkeypatch.setattr(L, "_is_public_address", lambda ip: True)  # reach loopback
    monkeypatch.setattr(L, "_url_precheck", lambda url: None)      # the hop stays on the ephemeral port
    mebibyte = b"\0" * (1024 * 1024)
    sent = {"mib": 0}

    def respond(conn, line):
        if line.startswith("HEAD "):
            _reply(conn, "HTTP/1.1 405 Method Not Allowed")
        elif line.startswith("GET /start"):
            conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: /next\r\nConnection: close\r\n\r\n")
            for _ in range(64):
                conn.sendall(mebibyte)
                sent["mib"] += 1
        else:
            _reply(conn, "HTTP/1.1 200 OK")

    with _loopback_server(respond) as (port, seen):
        result = L._check_url_via(f"http://127.0.0.1:{port}/start", 3.0, L._guarded_open)
    assert result["ok"] is True and result["status"] == 200
    assert seen == ["HEAD /start HTTP/1.1", "GET /start HTTP/1.1", "GET /next HTTP/1.1"]
    assert sent["mib"] < 32, f"the checker took {sent['mib']} MiB of a redirect body"


def test_time_budget_stops_a_check_that_is_already_running(monkeypatch):
    """Real path: the budget used to be looked at between URLs only. A host
    that sends one header byte every 200 ms never trips the per-read
    timeout, so one URL could hold the build for as long as the host liked
    (6 s here). With the deadline enforced inside the check, the lint
    returns at the budget and reports the URLs as not checked."""
    monkeypatch.setattr(L, "_is_public_address", lambda ip: True)  # reach loopback
    monkeypatch.setattr(L, "_url_precheck", lambda url: None)      # ephemeral port
    monkeypatch.setattr(L, "TOTAL_BUDGET_SECONDS", 1.0)

    def trickle(conn, line):
        conn.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
        for _ in range(30):
            conn.sendall(b"z")
            time.sleep(0.2)

    with _loopback_server(trickle) as (port, seen):
        spec = {"settings": {"site_url": f"http://127.0.0.1:{port}/one",
                             "instagram_url": f"http://127.0.0.1:{port}/two"}}
        started = time.monotonic()
        warnings = L.lint_external_urls(spec, mode="error")
        elapsed = time.monotonic() - started
    assert elapsed < 3.0, f"the lint ran {elapsed:.1f}s against a 1s budget"
    assert seen == ["HEAD /one HTTP/1.1"]
    # Not judged is not the same as dead: no "unreachable" warning, and
    # "error" mode did not refuse the build.
    assert [w["reason"] for w in warnings] == ["external_url_lint_truncated"]
    assert "2 external URL(s) were not checked" in warnings[0]["detail"]
    assert L._budget_deadline() is None, "the deadline must not outlive the lint run"


def test_no_connection_is_attempted_once_the_budget_is_spent(monkeypatch):
    import socket as _socket
    lookups = []
    monkeypatch.setattr(_socket, "getaddrinfo", lambda *a, **k: lookups.append(a) or [])
    monkeypatch.setattr(L._budget, "deadline", time.monotonic() - 1, raising=False)
    with pytest.raises(L.LinkCheckBudgetExceeded):
        L._guarded_create_connection(("public.example", 80), 5.0)
    assert lookups == []
    result = L._check_url_via("https://public.example/", 5.0, L._guarded_open)
    assert result == {"url": "https://public.example/", "ok": False, "status": None,
                      "error": "not checked: time budget exhausted", "unchecked": True}


def test_guarded_urlopen_without_a_budget_still_reads_a_normal_response(monkeypatch):
    """The health probes outside the linter use the same opener with no
    deadline set; the budget plumbing must leave them alone."""
    import urllib.request
    monkeypatch.setattr(L, "_is_public_address", lambda ip: True)  # reach loopback
    assert L._budget_deadline() is None
    with _loopback_server(lambda conn, line: _reply(conn, "HTTP/1.1 204 No Content", "X-Probe: yes")) as (port, seen):
        with L.guarded_urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/health"), timeout=3) as resp:
            assert resp.status == 204
            assert resp.headers["X-Probe"] == "yes"
    assert seen == ["GET /health HTTP/1.1"]


# ---------------------------------------------------------------------------
# A6. Determinism
# ---------------------------------------------------------------------------

_BUILD_IN_SUBPROCESS = """
import hashlib, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from compose.build import build_app
out = Path(sys.argv[3])
build_app(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")), output_dir=out)
digest = {}
for p in sorted(out.rglob("*")):
    if p.is_file():
        digest[p.relative_to(out).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
sys.stdout.write(json.dumps(digest))
"""


def _build_digest(tmp_path, tag, hash_seed, source_date_epoch):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHASHSEED", "SOURCE_DATE_EPOCH")}
    env["PYTHONHASHSEED"] = hash_seed
    env["CAS_LINT_EXTERNAL_URLS"] = "skip"
    if source_date_epoch is not None:
        env["SOURCE_DATE_EPOCH"] = source_date_epoch
    proc = subprocess.run(
        [sys.executable, "-c", _BUILD_IN_SUBPROCESS, str(REPO_ROOT),
         str(FIXTURES / "sample-healer-spec.json"), str(tmp_path / tag)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr[-600:]
    return json.loads(proc.stdout)


def test_two_processes_emit_identical_text_files(tmp_path):
    """Same spec, two fresh interpreters with different hash seeds, build
    time pinned with SOURCE_DATE_EPOCH: every emitted file is
    byte-identical. (Set and dict ordering, the clock and the locale are
    the usual ways this breaks.)"""
    first = _build_digest(tmp_path, "a", "1", "1700000000")
    second = _build_digest(tmp_path, "b", "4242", "1700000000")
    assert sorted(first) == sorted(second)
    differing = sorted(p for p in first if first[p] != second[p])
    assert differing == [], differing
    assert "BUILD_TIME = '2023-11-14T22:13:20Z'" in (tmp_path / "a" / "routes" / "seo.js").read_text(encoding="utf-8")


def test_only_the_sitemap_build_time_depends_on_the_clock(tmp_path):
    """Without SOURCE_DATE_EPOCH the build time is the one thing that may
    differ between two builds; the module docstring says so, and this
    pins it: a different epoch changes routes/seo.js (and therefore the
    manifest hash) and nothing else."""
    first = _build_digest(tmp_path, "a", "7", "1700000000")
    second = _build_digest(tmp_path, "b", "7", "1800000000")
    differing = sorted(p for p in first if first[p] != second[p])
    assert differing == ["compose-manifest.txt", "routes/seo.js"], differing


def test_source_date_epoch_ignores_garbage(monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-a-number; rm -rf /")
    stamp = B._build_time_iso(None)
    assert len(stamp) == 20 and stamp.endswith("Z")


# ---------------------------------------------------------------------------
# B. The generated app
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app_files():
    return _files(_base())


def test_no_default_or_guessable_credential_is_emitted(app_files):
    seed = app_files["db/seed.sql"]
    admin = [line for line in seed.split("\n") if line.startswith("INSERT INTO users ")]
    assert len(admin) == 1
    assert "VALUES ('" in admin[0] and ", NULL, TRUE, 'admin', TRUE)" in admin[0]
    bcrypt_prefixes = ("$2" + "a$", "$2" + "b$", "$2" + "y$")
    for path, content in app_files.items():
        if not isinstance(content, str):
            continue
        for prefix in bcrypt_prefixes:
            assert prefix not in content, f"{path} contains a password hash literal"
    assert "INSERT INTO magic_link_tokens" not in seed, "compose itself must not seed a login token"
    server = app_files["server.js"]
    assert "change-me" not in server
    assert "|| '" not in server.split("app.use(session(")[0].split("let sessionSecret")[1], \
        "session secret must have no string fallback"


def test_session_secret_has_no_fallback_and_production_refuses_to_start(app_files):
    server = app_files["server.js"]
    block = server[server.index("let sessionSecret"):server.index("app.use(session(")]
    assert "process.env.NODE_ENV === 'production'" in block
    assert "process.exit(1)" in block
    assert "crypto.randomBytes(32)" in block
    assert block.index("process.exit(1)") < block.index("crypto.randomBytes(32)")
    assert "secret: sessionSecret," in server
    env_example = app_files[".env.example"]
    assert "\nSESSION_SECRET=\n" in env_example, "the example file must not ship a usable secret"


def test_session_cookie_flags_and_csp(app_files):
    server = app_files["server.js"]
    for flag in ("httpOnly: true", "sameSite: 'lax'", "secure: process.env.NODE_ENV === 'production'"):
        assert flag in server
    for directive in ("base-uri 'self'", "form-action 'self'", "object-src 'none'", "frame-ancestors 'self'"):
        assert directive in server
    script_src_line = "\"script-src 'self' 'nonce-\" + nonce + \"'; \""
    assert script_src_line in server
    assert "unsafe-inline" not in script_src_line
    # Exactly the hosts the widgets frame (two video players, a maps embed);
    # frame-src 'self' alone blocked the engine's own embeds.
    assert ("\"frame-src 'self' https://www.youtube-nocookie.com https://player.vimeo.com "
            "https://www.google.com; \"") in server


def test_database_rows_cannot_replace_engine_constants_in_settings(app_files):
    """The layouts write settings._palette_vars and settings._fonts into a
    <style> block. Those are build-time constants; a site_settings row with
    an underscore-prefixed key must not override them."""
    settings_mw = app_files["middleware/settings.js"]
    assert "if (typeof r.key === 'string' && r.key.charAt(0) !== '_') out[r.key] = r.value;" in settings_mw
    assert "out[r.key] = r.value;\n    // Don't cache" not in settings_mw
    # And the admin settings route only accepts its own allowlist of keys.
    admin_settings = app_files["routes/admin-settings.js"]
    assert "for (const key of ALLOWED_KEYS)" in admin_settings
    allowed = json.loads(admin_settings.split("const ALLOWED_KEYS = ", 1)[1].split(";\n", 1)[0])
    assert allowed and all(not key.startswith("_") for key in allowed)


def test_health_does_not_echo_database_errors(app_files):
    server = app_files["server.js"]
    health = server[server.index("app.get('/health'"):server.index("// Auth routes")]
    assert "res.status(503).json({status: 'db_error'});" in health
    assert "error:" not in health.split("console.error")[1]


def test_login_regenerates_the_session_and_does_not_enumerate_accounts(app_files):
    auth = app_files["routes/auth.js"]
    login = auth[auth.index("router.post('/login'"):auth.index("router.head('/magic'")]
    assert "establishSession(req, user," in login
    assert "req.session.userId =" not in login, "identity must only be written after regenerate()"
    assert login.count("_LOGIN_FAILED") == 1
    assert "uses magic-link login" not in auth
    assert "_TIMING_EQUALIZER_HASH" in login
    assert "typeof req.body.email === 'string'" in login and "typeof req.body.password === 'string'" in login


def test_every_unauthenticated_post_is_rate_limited_and_csrf_checked(app_files):
    import re as _re
    limiter = app_files["middleware/rate-limit.js"]
    for name in ("authLimiter", "magicLinkRequestLimiter", "formLimiter"):
        assert f"const {name} = limiter(" in limiter
    auth = app_files["routes/auth.js"]
    assert "router.post('/login', authLimiter, validateCsrfToken," in auth
    assert "router.post('/magic/consume', authLimiter, validateCsrfToken," in auth
    assert "router.post('/request-magic-link', magicLinkRequestLimiter, validateCsrfToken," in auth

    spec = _base()
    spec["data_models"] = [{"name": "BookingRequest", "fields": [{"name": "name", "sql_type": "TEXT"}]}]
    spec["custom_pages"] = [{"path": "/book", "title": "Book", "form_target": "BookingRequest",
                             "allowed_fields": ["name"]}]
    pages = _files(spec)["routes/pages.js"]
    posts = _re.findall(r"router\.post\('([^']+)'([^\n]*)", pages)
    # The coach sample shows the contact form and no newsletter form, so it
    # gets POST /contact and no POST /newsletter-signup.
    assert sorted(p for p, _ in posts) == ["/book", "/contact"]
    for path, rest in posts:
        assert rest.startswith(", formLimiter, validateCsrfToken, async"), (path, rest)
    # A build whose home page carries the newsletter form gets that route,
    # limited and CSRF-checked the same way.
    healer = json.loads((FIXTURES / "sample-healer-spec.json").read_text(encoding="utf-8"))
    healer_posts = dict(_re.findall(r"router\.post\('([^']+)'([^\n]*)", _files(healer)["routes/pages.js"]))
    assert sorted(healer_posts) == ["/contact", "/newsletter-signup"]
    assert healer_posts["/newsletter-signup"].startswith(", formLimiter, validateCsrfToken, async")

    # Every POST route anywhere in the app carries validateCsrfToken.
    for path, content in app_files.items():
        if path.startswith("routes/") and isinstance(content, str):
            for line in content.split("\n"):
                if line.startswith("router.post("):
                    assert "validateCsrfToken" in line, f"{path}: {line[:90]}"


def test_csrf_comparison_is_constant_time_and_rejects_missing_tokens(app_files):
    auth_mw = app_files["middleware/auth.js"]
    assert "crypto.timingSafeEqual(a, b)" in auth_mw
    assert "typeof submitted !== 'string' || typeof expected !== 'string' || !expected" in auth_mw
    assert "token !== req.session.csrfToken" not in auth_mw
    assert "req.get('referer')" not in auth_mw


@needs_node
def test_csrf_token_comparison_behaves(app_files, tmp_path):
    """Run the emitted tokensMatch() itself."""
    src = app_files["middleware/auth.js"]
    helper = src[src.index("function tokensMatch"):src.index("function validateCsrfToken")]
    script = "const crypto = require('crypto');\n" + helper + """
const t = 'ab'.repeat(32);
const out = [
  tokensMatch(t, t), tokensMatch(t, 'cd'.repeat(32)), tokensMatch(undefined, undefined),
  tokensMatch('', ''), tokensMatch(t, undefined), tokensMatch(['x'], 'x'),
  tokensMatch(t.slice(1), t), tokensMatch('é', 'e'),
];
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[:300]
    assert json.loads(proc.stdout) == [True, False, False, False, False, False, False, False]


def test_magic_link_issue_reads_no_request_header_for_the_link_and_does_not_log_tokens_when_mail_works(app_files):
    """The text of the handler; test_generated_app_runtime.py runs it with a
    spoofed Host header."""
    auth = app_files["routes/auth.js"]
    issue = auth[auth.index("router.post('/request-magic-link'"):auth.index("router.get('/magic-link-sent'")]
    assert "x-forwarded-host" not in issue.lower()
    assert "req.get(" not in issue and "req.headers" not in issue and "req.protocol" not in issue
    assert "const origin = await linkOrigin();" in issue
    mail, no_mail = issue.split("if (process.env.RESEND_API_KEY) {", 1)[1].split("} else {", 1)
    assert "rawToken" not in mail
    assert "rawToken" in no_mail.split("}", 1)[0]
    assert "24 * 60 * 60 * 1000" in issue, "token lifetime"
    assert "crypto.createHash('sha256').update(rawToken).digest('hex')" in issue, "stored hashed"
    assert "crypto.randomBytes(32)" in issue


def test_upload_route_allowlists_extension_and_generates_the_filename(app_files):
    upload = app_files["routes/upload.js"]
    assert "new Set(['.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif'])" in upload
    assert ".svg" not in upload.split("const ALLOWED_EXTENSIONS")[1].split(";")[0]
    assert "ALLOWED_EXTENSIONS.has(ext) || !ALLOWED_MIMETYPES.has(file.mimetype)" in upload
    assert "crypto.randomBytes(8).toString('hex') + ext" in upload
    assert "file.originalname || 'upload').replace(" not in upload, "client filename must not be reused"
    assert "/image\\//.test(file.mimetype)" not in upload
    assert "requireAuth, requireRole('admin'), validateCsrfToken," in upload
    assert "fileSize: 10 * 1024 * 1024" in upload


def test_resource_router_rechecks_admin_session_and_hides_unpublished_rows():
    spec = _base()
    spec["data_models"] = [{
        "name": "Article", "public_listing_path": "/articles",
        "fields": [{"name": "title", "sql_type": "TEXT"}, {"name": "published", "sql_type": "BOOLEAN", "default": "FALSE"}],
    }]
    router = _files(spec)["routes/articles.js"]
    guard = router[router.index("router.use("):router.index("// GET /: list.")]
    assert "req.baseUrl.startsWith('/admin/')" in guard
    assert "req.session.role === 'admin'" in guard and "res.redirect('/login')" in guard
    detail = router[router.index("router.get('/:id'"):router.index("router.get('/:id/edit'")]
    assert "'SELECT * FROM articles WHERE id = $1' + (isAdmin ? '' : ' AND published = TRUE')" in detail
    # The id is checked once, for every :id route, before any handler runs
    # (test_generated_app_runtime.py sends the requests).
    param = router[router.index("router.param('id'"):router.index("// GET /: list.")]
    assert "/^[0-9]{1,9}$/.test(String(id))" in param


def test_composed_pages_hide_unpublished_rows():
    files = _files(_base())
    pages = files["routes/pages.js"]
    assert "SELECT * FROM blog_posts WHERE published = TRUE ORDER BY id ASC" in pages
    assert "WHERE (slug::text = $1 OR id::text = $1) AND published = TRUE LIMIT 1" in pages
    # A table without a published column is read unfiltered.
    assert "SELECT * FROM case_studies ORDER BY id ASC" in pages


def test_sitemap_escapes_the_origin(app_files):
    seo = app_files["routes/seo.js"]
    assert "const loc = xmlEscape(origin + p);" in seo
    assert ".replace(/&/g, '&amp;')" in seo


def test_every_emitted_dependency_is_actually_required(app_files):
    """package.json lists exactly the third-party modules the emitted code
    requires: nothing unused (it would be install time and supply-chain
    surface for nothing) and nothing missing (the app would not boot)."""
    import re as _re
    declared = set(json.loads(app_files["package.json"])["dependencies"])
    required = set()
    for path, content in app_files.items():
        if isinstance(content, str) and path.endswith(".js") and not path.startswith("public/"):
            for name in _re.findall(r"require\('([^'./][^']*)'\)", content):
                required.add(name)
    # node's own modules (http: the home page check in services/health.js).
    builtin = {"crypto", "path", "fs", "http"}
    assert required - builtin - declared == set(), "required but not declared"
    # ejs is loaded by Express through `view engine`, not by require().
    assert declared - required == {"ejs"}, sorted(declared - required)


def test_no_test_or_debug_bypass_in_emitted_code(app_files):
    """No environment switch may relax authentication, CSRF or rate limits."""
    import re as _re
    allowed_env = {
        "SESSION_SECRET", "NODE_ENV", "PORT", "DATABASE_URL", "RESEND_API_KEY",
        "ADMIN_EMAIL", "SITE_NAME", "RESEND_FROM_DOMAIN",
        # The origin of emailed links (linkOrigin); it relaxes no check.
        "SITE_ORIGIN",
    }
    seen = set()
    for path, content in app_files.items():
        if isinstance(content, str) and path.endswith((".js", ".ejs")):
            seen.update(_re.findall(r"process\.env\.([A-Z0-9_]+)", content))
            lowered = content.lower()
            for marker in ("test_mode", "skip_auth", "disable_auth", "bypass_csrf", "disable_csrf", "backdoor"):
                assert marker not in lowered, f"{path} mentions {marker}"
    assert seen <= allowed_env, sorted(seen - allowed_env)


# ---------------------------------------------------------------------------
# B2. Output escaping in widgets and helpers
# ---------------------------------------------------------------------------

def test_sanitize_html_fragment_allowlist():
    hostile = (
        '<p>Tom &amp; Jerry &lt;3</p>\n<P onclick="x()">hi</P><script>alert(1)</script>'
        '<a href="javascript:x">l</a><base href="//evil.example"><img src=x onerror=alert(1)>'
        '<style>*{display:none}</style><form action="https://evil.example"><EM>ok</EM><br/>'
        '<iframe src="//evil.example"></iframe><p\nonmouseover=x>'
    )
    out = sanitize_html_fragment(hostile)
    assert out.startswith("<p>Tom &amp; Jerry &lt;3</p>")
    assert "<em>ok</em><br/>" in out
    import re as _re
    tags = set(_re.findall(r"<(/?[a-z0-9]+)[^>]*>", out))
    assert tags <= {"p", "/p", "em", "/em", "br"}, tags
    assert _re.findall(r"<[a-z]+\s[^>]*>", out) == [], "no tag may carry an attribute"
    assert sanitize_html_fragment(out) == out, "idempotent"
    assert sanitize_html_fragment(None) == "" and sanitize_html_fragment(["<p>x</p>"]) == ""
    assert len(sanitize_html_fragment("<p>" + "a" * 20000 + "</p>")) < 8100


def test_markdown_links_reject_protocol_relative_and_script_targets():
    html = render_markdown_blocks([
        "[a](//evil.example/x) [b](/ok) [c](about.html) [d](/\\evil.example) "
        "[e](javascript:alert(1)) [f](https://ok.example) [g](data:text/html,x)"
    ])
    import re as _re
    assert _re.findall(r'href="([^"]*)"', html) == ["/ok", "about.html", "https://ok.example"]
    assert "javascript:" not in html and "data:" not in html and "evil.example" not in html


def test_copy_hero_block_sanitizes_body_html():
    block = _build_copy_hero_block(
        {"heading": "H", "body_html": "<p>fine</p><script>alert(1)</script><a href='javascript:x'>l</a>"},
        "Site", "home",
    )
    assert block["body_html"].startswith("<p>fine</p>")
    assert "<script" not in block["body_html"] and "<a " not in block["body_html"]


def test_raw_output_tags_in_widgets_are_the_reviewed_set():
    """Every `<%- ... %>` in the widget set that is not an include. A new
    raw output is a decision someone has to make on purpose: adding one
    fails this test until it is listed here with its justification."""
    import re as _re
    allowed = {
        # escaped-then-wrapped heading helpers
        "_accent", "_npAccent", "_bsAccent", "_booksGridAccent", "_rendered", "_renderedSafe",
        # constant strings chosen by comparison, or built from escaped parts
        "_ctaIconSvg", "_iconSvg", "_linkAttrs", "_imgStyle", "_bcRetailers.join", "_attrs", "_extra",
        # attribute-free tag allowlist applied in the widget
        "_bcSafeHtml",
        # sanitized at build time (compose.markdown_safe)
        "body_html", "_html",
        # builder output assembled by the auth views from escaped parts
        "_body", "_footer",
        # constant table lookup guarded with hasOwnProperty
        "(Object.prototype.hasOwnProperty.call",
    }
    found = set()
    for path in sorted((COMPOSE_DIR / "widgets").rglob("*.ejs")):
        for expr in _re.findall(r"<%-\s*([^%]+?)\s*%>", path.read_text(encoding="utf-8")):
            if expr.startswith("include("):
                continue
            head = _re.match(r"[A-Za-z_(.][A-Za-z0-9_.]*(?:\.call)?", expr).group(0)
            found.add(head)
    assert found <= allowed, sorted(found - allowed)


def test_no_javascript_urls_or_referer_links_in_widgets():
    for path in sorted((COMPOSE_DIR / "widgets").rglob("*.ejs")):
        text = path.read_text(encoding="utf-8")
        assert 'href="javascript:' not in text, path.name
        assert "<%= referer %>" not in text, path.name


@needs_ejs
def test_book_card_description_keeps_formatting_and_drops_everything_else():
    html = _render(COMPOSE_DIR / "widgets" / "offerings" / "book-card.ejs", {
        "book": {
            "title": "T",
            "description": '<p>A <em>fine</em> &amp; good book</p><script>alert(1)</script>'
                           '<a href="https://evil.example">x</a><img src=x onerror=alert(1)><style>p{}</style>',
            "local_bookstore_url": 'https://shop.example/"><script>alert(2)</script>',
        },
    })
    assert "<p>A <em>fine</em> &amp; good book</p>" in html
    assert "<script" not in html and "<img" not in html and "<style" not in html
    assert '<a href="https://evil.example"' not in html
    assert ">Local bookstore</a>" in html
    assert 'href="https://shop.example/&quot;&gt;&lt;script&gt;' in html


@needs_ejs
def test_slideshow_and_video_widgets_render_and_constrain_style_values():
    slides = _render(COMPOSE_DIR / "widgets" / "content" / "photo-slideshow.ejs", {
        "photos": ["https://img.example/a.jpg", "/uploads/2026/01/b.jpg", "//evil.example/c.jpg",
                   "javascript:alert(1)", "data:image/svg+xml,<svg onload=alert(1)>"],
        "aspect_ratio": "1/1;background:url(//evil.example/x)",
        "heading_alt": "Shots",
    })
    assert slides.count("<img ") == 2
    assert "evil.example" not in slides and "javascript:" not in slides
    assert "aspect-ratio: 16/10;" in slides

    one = _render(COMPOSE_DIR / "widgets" / "content" / "photo-slideshow.ejs", {
        "photos": ["/uploads/a.jpg"], "max_height": "9px;position:fixed", "aspect_ratio": "4 / 3",
    })
    assert "max-height: 640px;" in one

    video = _render(COMPOSE_DIR / "widgets" / "content" / "video-embed-nocookie.ejs", {
        "video_url": "https://www.youtube.com/watch?v=abcdefghijk", "caption": "Tom & Jerry",
    })
    assert 'src="https://www.youtube-nocookie.com/embed/abcdefghijk"' in video
    assert 'title="Tom &amp; Jerry"' in video, "the title used to be escaped twice"

    link = _render(COMPOSE_DIR / "widgets" / "content" / "video-embed-nocookie.ejs", {
        "video_url": "javascript:alert(1)", "caption": "",
    })
    assert "<iframe" not in link and "javascript:" not in link


@needs_ejs
def test_map_embed_only_frames_a_google_maps_embed_url():
    widget = COMPOSE_DIR / "widgets" / "offerings" / "hours-location-block.ejs"
    good = _render(widget, {"map_embed_url": "https://www.google.com/maps/embed?pb=abc"})
    assert '<iframe src="https://www.google.com/maps/embed?pb=abc" title="Map"' in good
    for bad in ("https://evil.example/maps/embed?x", "javascript:alert(1)",
                "https://www.google.com.evil.example/maps/embed?x", "//www.google.com/maps/embed?x",
                "https://www.google.com/maps/embedded-phish"):
        assert "<iframe" not in _render(widget, {"map_embed_url": bad}), bad


@needs_ejs
def test_grouping_widgets_survive_prototype_named_groups():
    """An admin who names a menu section "constructor" must not turn the
    public menu page into a 500."""
    menu = _render(COMPOSE_DIR / "widgets" / "offerings" / "menu-grouped-list.ejs", {
        "items": [{"name": "Soup", "section_name": "constructor"}, {"name": "Tea", "section_name": "__proto__"}],
    })
    assert "Soup" in menu and "Tea" in menu
    services = _render(COMPOSE_DIR / "widgets" / "offerings" / "service-menu-priced.ejs", {
        "items": [{"title": "Cut", "category": "toString"}, {"title": "Color", "category": "constructor"}],
    })
    assert "Cut" in services and "Color" in services
    books = _render(COMPOSE_DIR / "widgets" / "offerings" / "books-grid.ejs", {
        "items": [{"title": "One", "series_label": "constructor"}, {"title": "Two", "series_label": "valueOf"}],
    })
    assert "One" in books and "Two" in books


@needs_ejs
def test_auth_views_render_from_a_build_with_hostile_locals(tmp_path):
    # Rendered from a real build: the auth views include their shell by a
    # path that only resolves inside a generated app's views/ directory.
    out = tmp_path / "app"
    build_app(_base(), output_dir=out)
    for view in ("login.ejs", "request-magic-link.ejs", "set-password.ejs", "magic-link-confirm.ejs"):
        html = _render(out / "views" / view, {
            "csrfToken": '"><script>alert(1)</script>',
            "error": "<b>bad</b>",
            "token": '"><script>alert(2)</script>',
            "magic_link_available": False,
        })
        assert "<script>alert" not in html, view
        assert 'name="_csrf" value="scriptalert1script"' in html, view
    csrf_page = _render(out / "views" / "error-csrf.ejs", {"referer": "https://evil.example/"})
    assert "evil.example" not in csrf_page and "javascript:" not in csrf_page
    assert 'href="/" data-cas-back' in csrf_page
    assert "data-cas-back" in (out / "public" / "js" / "cas-form-validate.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Intake, stock photos, brand assets, engine neutrality
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,page_id", [
    ("/", "home"),
    ("", "home"),
    ("/about", "about"),
    ("/Work-With-Me/", "work-with-me"),
    ("/services/coaching", "services-coaching"),
    ("/about.html", "about-html"),
    ("/../../etc/passwd", "etc-passwd"),
    ("/a'b\"c", "a-b-c"),
    ("/%2e%2e/x", "2e-2e-x"),
    ("/" + "x" * 200, "x" * 64),
    ("/...", "page"),
    (None, "home"),
])
def test_copy_path_to_page_id_is_always_a_safe_slug(path, page_id):
    result = _copy_path_to_page_id(path)
    assert result == page_id
    assert B._PAGE_ID_RE.fullmatch(result)


def test_translator_drops_hostile_page_paths_and_still_builds(tmp_path):
    v1 = {
        "app_name": "translator-hostile",
        "description": "A yoga studio offering classes.",
        "pages": [
            {"path": "/classes-info", "title": "Info"},
            {"path": "/x', (q,r)=>r.send(1)); router.get('/y", "title": "Injected"},
            {"path": "/a b", "title": "Space"},
            {"path": ["/list"], "title": "Not a string"},
        ],
        "_harbor_meta": {"copy_pages": [
            {"path": "/../../escape.html", "markdown_blocks": ["hello %> world"],
             "hero": {"heading": "H", "body_html": "<p>x</p><script>alert(1)</script>"}},
        ]},
    }
    spec = translate_v1_spec(v1, admin_email="owner@example.com")
    assert [p["path"] for p in spec["custom_pages"]] == ["/classes-info"]
    reasons = [w["detail"] for w in spec["_translation_warnings"] if w["reason"] == "invalid_page_path"]
    assert len(reasons) == 3
    assert "escape-html" in spec["pages"]
    assert "<script" not in spec["pages"]["escape-html"]["hero"]["body_html"]
    assert validate_spec(BuildSpec.from_dict(spec)) == []
    out = tmp_path / "work" / "app"
    build_app(spec, output_dir=out)
    assert (out / "views" / "escape-html.ejs").exists()
    assert sorted(p.name for p in (tmp_path / "work").iterdir()) == ["app"]


def test_translator_leaves_out_copied_pages_that_would_replace_a_built_in(tmp_path):
    """A crawled site has its own /login, /privacy and /admin. The engine
    refuses those page ids, so the translator (which may not refuse a
    customer's build over somebody else's URL layout) leaves the copies out
    and says so; the generated app keeps its own login form."""
    v1 = {
        "app_name": "copy-reserved",
        "description": "A yoga studio offering classes.",
        "pages": [],
        "_harbor_meta": {"copy_pages": [
            {"path": "/login", "markdown_blocks": ["Old client login"]},
            {"path": "/privacy", "markdown_blocks": ["Old privacy policy"]},
            {"path": "/Admin/", "markdown_blocks": ["Old admin"]},
            {"path": "/our-story", "markdown_blocks": ["Kept"]},
        ]},
    }
    spec = translate_v1_spec(v1, admin_email="owner@example.com")
    assert "our-story" in spec["pages"]
    assert not {"login", "privacy", "admin"} & set(spec["pages"])
    reasons = [w["reason"] for w in spec["_translation_warnings"]]
    assert reasons.count("copy_pages_reserved_page_id") == 3
    assert validate_spec(BuildSpec.from_dict(spec)) == []
    out = tmp_path / "app"
    build_app(spec, output_dir=out)
    assert (out / "views" / "our-story.ejs").exists()
    login = (out / "views" / "login.ejs").read_text(encoding="utf-8")
    assert 'type="password"' in login and 'name="_csrf"' in login
    assert "Old client login" not in login
    assert "Old privacy policy" not in (out / "views" / "privacy.ejs").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["*", "*.jpg", "..", "../manifest.json", "a/b.jpg", "[a-z]*.jpg", "", "x\x00.jpg", ".hidden"])
def test_stock_photo_lookup_refuses_patterns_and_paths(name):
    stock_photo_source_path.cache_clear()
    assert stock_photo_source_path(name) is None


def test_monogram_text_is_drawn_verbatim_not_xml_escaped(monkeypatch):
    pil = pytest.importorskip("PIL.ImageDraw")
    from compose.utils import brand_assets
    drawn = []
    real_text = pil.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return real_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(pil.ImageDraw, "text", spy)
    png = brand_assets.render_brand_mark_png(64, "A&B Studio", {"--color-primary": "#111111"})
    assert png and png[:4] == b"\x89PNG"
    assert drawn == ["AS"]
    drawn.clear()
    brand_assets.render_brand_mark_png(64, "A&B", {"--color-primary": "#111111"})
    assert drawn == ["A&"], "the ampersand must reach Pillow as-is"


def test_favicon_svg_escapes_the_initial():
    spec = _base()
    spec["settings"]["site_name"] = "<Studio & Co>"
    files = _files(spec)
    assert ">&lt;</text></svg>" in files["public/favicon.svg"]


def test_engine_carries_no_customer_specific_field_or_palette_names():
    """Item: a general engine should not name one real bookstore in a
    column, nor a palette after a customer."""
    store = "book" + "people"
    for path in sorted(COMPOSE_DIR.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".png", ".jpg", ".woff2"):
            continue
        assert store not in path.read_text(encoding="utf-8", errors="replace").lower(), path
    names = [f["name"] for f in B._default_fields("Book")]
    assert "local_bookstore_url" in names
    assert "local_bookstore_url" in L._URL_FIELD_NAMES
    palette_ids = [p["id"] for p in B.load_palettes()["palettes"]]
    assert "wine-rose" in palette_ids
    assert B.get_palette("wine-rose")["vars"]["--color-primary"] == "#760008"
    for pid in palette_ids:
        assert pid == pid.lower() and " " not in pid

