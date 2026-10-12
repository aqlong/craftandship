"""What the engine derives from a spec's model and page definitions, and
what it refuses.

These cover rules that used to be keyed on a fixed name or a fixed column
list instead of the model definition, and the checks that go with them:

  - a page that lists several roles is opened by any one of them;
  - the contact form, its POST handler and the admin dashboard read their
    table and columns from the contact model;
  - a listing is ordered by the model's date column, whatever the model is
    called;
  - a page marked auth_required whose path something else would answer
    without a login refuses the build;
  - column defaults, duplicate column names, the markdown link pattern and
    the directory build_app deletes.

No database here. The emitted routers are executed in Express with a
stand-in database (the harness of test_generated_app_runtime.py) when
web/node_modules is installed, and the contact widget is rendered with the
real EJS. test_generated_site_security_e2e.py runs the same behavior on a
booted site with Postgres.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from pathlib import Path

import pytest

from compose import build as B
from compose.build import BuildSpec, build_app, compose_files, resolve_spec, validate_spec
from compose.intake import translate_v1_spec
from compose.markdown_safe import render_markdown_blocks
from compose.tests import test_generated_app_runtime as runtime
from compose.tests import test_widgets as widgets

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

needs_express = pytest.mark.skipif(
    runtime.NODE is None
    or not all((runtime.WEB_MODULES / name).exists() for name in ("express", "express-rate-limit", "bcrypt")),
    reason="needs node and web/node_modules with express, express-rate-limit and bcrypt (run `cd web && npm ci`)",
)
needs_ejs = pytest.mark.skipif(
    runtime.NODE is None or not (runtime.WEB_MODULES / "ejs").exists(),
    reason="needs node and web/node_modules/ejs (run `cd web && npm ci`)",
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "skip")


def _coach() -> dict:
    return json.loads((FIXTURES / "sample-coach-spec.json").read_text(encoding="utf-8"))


def _healer() -> dict:
    return json.loads((FIXTURES / "sample-healer-spec.json").read_text(encoding="utf-8"))


def _resolve(spec: dict):
    return resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))


def _files(spec: dict) -> dict:
    return dict(compose_files(_resolve(spec)))


def _warnings(rs, reason: str) -> list:
    return [w["detail"] for w in rs.spec.engine_warnings
            if isinstance(w, dict) and w.get("reason") == reason]


# A contact model that shares only `email` and `message` with the default.
CUSTOM_CONTACT = {"name": "ContactInquiry", "fields": [
    {"name": "full_name", "sql_type": "VARCHAR(255)", "required": True},
    {"name": "email", "sql_type": "VARCHAR(255)", "required": True},
    {"name": "company", "sql_type": "VARCHAR(255)"},
    {"name": "message", "field_type": "long_text", "required": True},
    {"name": "admin_notes", "sql_type": "TEXT"},
]}


# ---------------------------------------------------------------------------
# Roles on a protected page
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("roles,expected", [
    (None, ["requireAuth"]),
    ([], ["requireAuth"]),
    (["admin"], ["requireAuth", "requireRole('admin')"]),
    (["Admin"], ["requireAuth", "requireRole('admin')"]),
    (["member"], ["requireAuth", "requireRole('member')"]),
    (["admin", "editor"], ["requireAuth", "requireAnyRole(['admin', 'editor'])"]),
    (["editor", " editor ", "admin"], ["requireAuth", "requireAnyRole(['editor', 'admin'])"]),
    # "user" is the role every account has by default: any signed-in account.
    (["user"], ["requireAuth"]),
    (["admin", "user"], ["requireAuth"]),
    (["o'brien", "admin"], ["requireAuth", "requireAnyRole(['o\\'brien', 'admin'])"]),
])
def test_a_protected_page_gets_one_guard_that_any_listed_role_passes(roles, expected):
    page = {"path": "/x", "auth_required": True}
    if roles is not None:
        page["allowed_roles"] = roles
    assert B._custom_page_guards(page) == expected
    # Never one requireRole per role: an account has a single role, so a
    # chain of them would let nobody through.
    assert sum(g.startswith("requireRole(") for g in B._custom_page_guards(page)) <= 1


def test_roles_on_a_page_that_is_not_marked_auth_required_make_it_a_protected_page():
    """A list of roles is a restriction. The page used to be public (GET and
    POST) with the roles ignored; it now asks for a login and one of the
    roles, and `resolve_spec` says so in a warning."""
    assert B._custom_page_guards({"path": "/x", "allowed_roles": ["admin", "editor"]}) == [
        "requireAuth", "requireAnyRole(['admin', 'editor'])"]
    assert B._custom_page_guards({"path": "/x", "allowed_roles": ["admin"]}) == ["requireAuth", "requireRole('admin')"]
    # No roles, or none that name a role: nothing restricts the page.
    assert B._custom_page_guards({"path": "/x", "allowed_roles": []}) == []
    assert B._custom_page_guards({"path": "/x"}) == []

    spec = _coach()
    spec["data_models"] = [_model("Lead", TITLE)]
    spec["custom_pages"] = [{"path": "/staff-intake", "title": "Staff intake", "allowed_roles": ["admin"],
                             "form_target": "Lead"}]
    rs = _resolve(spec)
    assert rs.spec.custom_pages[0]["auth_required"] is True
    assert spec["custom_pages"][0].get("auth_required") is None, "the caller's spec is left as it was"
    warned = _warnings(rs, "allowed_roles_imply_login")
    assert len(warned) == 1 and "'/staff-intake'" in warned[0] and "requires a login" in warned[0]
    pages = dict(compose_files(rs))["routes/pages.js"]
    for verb in ("get", "post"):
        start = pages.index(f"router.{verb}('/staff-intake'")
        line = pages[start:pages.index("\n", start)]
        assert "requireAuth" in line and "requireRole('admin')" in line, line
    # Resolving the same spec again does not say it twice.
    assert len(_warnings(resolve_spec(rs.spec), "allowed_roles_imply_login")) == 1


def test_a_page_with_roles_at_a_path_something_else_serves_publicly_is_refused_like_any_protected_page():
    spec = _coach()
    spec["custom_pages"] = [{"path": "/about", "title": "About", "allowed_roles": ["admin"]}]
    errors = validate_spec(BuildSpec.from_dict(spec))
    assert errors and "custom page '/about' is marked auth_required" in errors[0], errors


@pytest.mark.parametrize("roles,fragment", [
    ([7], "must be role names"),
    ([""], "must be role names"),
    (["  "], "must be role names"),
    ([["admin"]], "must be role names"),
    (["r" * 51], "longer than the 50 characters"),
])
def test_gate_refuses_a_role_entry_the_guard_could_not_check(roles, fragment):
    spec = _coach()
    spec["custom_pages"] = [{"path": "/members", "auth_required": True, "allowed_roles": roles}]
    errors = validate_spec(BuildSpec.from_dict(spec))
    assert any(fragment in e for e in errors), errors
    with pytest.raises(ValueError, match="allowed_roles"):
        _resolve(spec)


def test_the_v1_translator_reads_a_single_role_written_as_a_string():
    """`"allowed_roles": "admin"` used to become the five roles a, d, m, i
    and n, which no account has."""
    v1 = {
        "app_name": "roles-v1", "description": "A studio with a members area", "admin_email": "admin@example.com",
        "pages": [
            {"path": "/members", "title": "Members", "auth_required": True, "allowed_roles": "admin"},
            {"path": "/staff", "title": "Staff", "auth_required": True, "allowed_roles": ["admin", "editor"]},
            {"path": "/lounge", "title": "Lounge", "auth_required": True, "allowed_roles": ""},
        ],
    }
    v2 = translate_v1_spec(copy.deepcopy(v1))
    roles = {p["path"]: p["allowed_roles"] for p in v2["custom_pages"]}
    assert roles == {"/members": ["admin"], "/staff": ["admin", "editor"], "/lounge": []}
    pages = _files(v2)["routes/pages.js"]
    assert "router.get('/members', requireAuth, requireRole('admin'), async" in pages
    assert "router.get('/staff', requireAuth, requireAnyRole(['admin', 'editor']), async" in pages
    assert "router.get('/lounge', requireAuth, async" in pages

    # A shape that is not a list of names is not guessed at: the gate names it.
    v1["pages"][0]["allowed_roles"] = {"admin": True}
    errors = validate_spec(BuildSpec.from_dict(translate_v1_spec(copy.deepcopy(v1))))
    assert any("allowed_roles must be a list" in e for e in errors), errors


def test_the_v1_translator_says_so_when_it_drops_a_page_that_was_meant_to_be_protected():
    v1 = {
        "app_name": "dropped-v1", "description": "A studio with a members area", "admin_email": "admin@example.com",
        "pages": [
            {"path": "members", "title": "Members", "auth_required": True},        # not absolute
            {"path": "/members area", "title": "Members", "auth_required": True},  # not a plain path
            {"path": "/open house", "title": "Open house"},                        # dropped, but public
        ],
    }
    v2 = translate_v1_spec(v1)
    assert not v2.get("custom_pages")
    reasons = [w["reason"] for w in v2["_translation_warnings"]]
    assert reasons.count("invalid_page_path") == 3
    reported = [w["detail"] for w in v2["_translation_warnings"] if w["reason"] == "auth_required_page_not_emitted"]
    assert len(reported) == 2
    assert "'members'" in reported[0] and "'/members area'" in reported[1]
    assert all("no protected page exists at that path" in detail for detail in reported)


@pytest.fixture(scope="module")
def roles_app(tmp_path_factory):
    """A build with pages for one role, two roles and "user", with the
    database replaced by the recorder of test_generated_app_runtime.py."""
    os.environ["CAS_LINT_EXTERNAL_URLS"] = "skip"
    spec = _coach()
    spec["data_models"] = [copy.deepcopy(CUSTOM_CONTACT)]
    spec["custom_pages"] = [
        {"path": "/members", "title": "Members", "auth_required": True, "allowed_roles": ["member"]},
        {"path": "/staff", "title": "Staff", "auth_required": True, "allowed_roles": ["admin", "editor"]},
        {"path": "/anyone", "title": "Anyone", "auth_required": True, "allowed_roles": ["user"]},
    ]
    out = tmp_path_factory.mktemp("roles") / "app"
    build_app(spec, output_dir=out)
    (out / "db.js").write_text(runtime._DB_STUB, encoding="utf-8")
    return out


def _session(role: str) -> dict:
    return {"csrfToken": "t0ken", "userId": 9, "role": role}


@needs_express
def test_a_page_with_two_roles_opens_for_either_and_for_nobody_else(roles_app):
    """The emitted guard, executed: /staff lists admin and editor."""
    paths = ("/members", "/staff", "/anyone")
    sessions = {"anonymous": runtime.ANON, "admin": _session("admin"), "editor": _session("editor"),
                "member": _session("member"), "user": _session("user")}
    plan = [{"method": "GET", "path": path, "session": session}
            for path in paths for session in sessions.values()]
    results = iter(runtime._run(roles_app, "pages", plan))
    opened = {path: {who for who in sessions if next(results)["status"] == 200} for path in paths}
    assert opened["/staff"] == {"admin", "editor"}
    assert opened["/members"] == {"member"}
    assert opened["/anyone"] == {"admin", "editor", "member", "user"}

    # A session that carries a role but no user id is not a signed-in
    # account, whatever the role says.
    (no_user,) = runtime._run(roles_app, "pages", [
        {"method": "GET", "path": "/staff", "session": {"csrfToken": "t0ken", "role": "admin"}},
    ])
    assert (no_user["status"], no_user["location"]) == (302, "/login")


@needs_express
def test_a_sign_in_lands_where_the_account_can_go(roles_app):
    """/admin turns every role but admin away, so only the admin is sent
    there after signing in; any other account lands on the home page."""
    (consumed,) = runtime._run(roles_app, "auth", [
        {"method": "POST", "path": "/magic/consume", "session": runtime.ANON,
         "form": {"_csrf": "t0ken", "token": "f" * 64}},
    ])
    assert (consumed["status"], consumed["location"]) == (302, "/admin")  # the stub's account is the admin
    auth = (roles_app / "routes" / "auth.js").read_text(encoding="utf-8")
    assert "return role === 'admin' ? '/admin' : '/';" in auth
    assert auth.count("res.redirect('/admin')") == 0, "every post-login redirect goes through landingFor"
    assert auth.count("landingFor(") == 4  # the definition, login, link consume, set-password


# ---------------------------------------------------------------------------
# The contact form and the newsletter form follow their models
# ---------------------------------------------------------------------------

def _contact_post(pages: str) -> str:
    start = pages.index("router.post('/contact'")
    return pages[start:pages.index("\n});", start)]


def test_contact_handler_inserts_the_columns_of_the_contact_model():
    default = _contact_post(_files(_coach())["routes/pages.js"])
    assert ("'INSERT INTO contact_inquiries (name, email, phone, service_interest, message) "
            "VALUES ($1, $2, $3, $4, $5)'") in default

    spec = _coach()
    spec["data_models"] = [copy.deepcopy(CUSTOM_CONTACT)]
    custom = _contact_post(_files(spec)["routes/pages.js"])
    assert ("'INSERT INTO contact_inquiries (full_name, email, company, message) "
            "VALUES ($1, $2, $3, $4)'") in custom
    for absent in ("phone", "service_interest", "admin_notes"):
        assert absent not in custom.split("const FIELDS = ", 1)[1].split(";\n", 1)[0], absent
    fields = json.loads(custom.split("const FIELDS = ", 1)[1].split(";\n", 1)[0])
    assert [(f["name"], f["field_type"], f["required"]) for f in fields] == [
        ("full_name", "text", True),
        ("email", "email", True),      # typed from its name, as in the admin CRUD
        ("company", "text", False),
        ("message", "long_text", True),
    ]


def test_contact_form_follows_a_renamed_contact_model():
    """Without ContactInquiry, the form stores into the model that plays
    its part, under that model's own table name."""
    spec = _coach()
    spec["_harbor_meta"] = {"exclude_data_models": ["ContactInquiry"]}
    spec["data_models"] = [{"name": "ContactMessage", "plural": "enquiries", "fields": [
        {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
        {"name": "message", "sql_type": "TEXT", "required": True},
    ]}]
    rs = _resolve(spec)
    files = dict(compose_files(rs))
    assert "'INSERT INTO enquiries (name, message) VALUES ($1, $2)'" in _contact_post(files["routes/pages.js"])
    assert "contact_inquiries" not in files["routes/pages.js"]
    assert "contact_inquiries" not in files["db/schema.sql"]
    assert "inquiries_admin_path: '/admin/enquiries'" in files["routes/admin-dashboard.js"]


def test_contact_form_without_any_contact_model_says_so_instead_of_failing():
    spec = _coach()
    spec["_harbor_meta"] = {"exclude_data_models": ["ContactInquiry"]}
    rs = _resolve(spec)
    post = _contact_post(dict(compose_files(rs))["routes/pages.js"])
    assert "INSERT INTO" not in post
    assert "res.status(503).render('contact'" in post
    assert len(_warnings(rs, "contact_form_without_model")) == 1


def test_built_in_form_routes_exist_only_when_a_page_shows_the_form():
    coach = _files(_coach())["routes/pages.js"]       # contact form, no newsletter form
    healer = _files(_healer())["routes/pages.js"]     # both
    assert "router.post('/contact'" in coach and "router.post('/newsletter-signup'" not in coach
    assert "router.post('/contact'" in healer and "router.post('/newsletter-signup'" in healer
    # One row per address: the statement looks for the address itself (the
    # column has no unique constraint for ON CONFLICT to act on).
    assert ("'INSERT INTO newsletter_subscribers (email) SELECT $1::text WHERE NOT EXISTS "
            "(SELECT 1 FROM newsletter_subscribers WHERE LOWER(email::text) = LOWER($1::text))'") in healer
    assert "ON CONFLICT" not in healer

    # No contact page: no public write endpoint for a form nobody can see.
    spec = _coach()
    spec["_harbor_meta"] = {"exclude_pages": ["contact"]}
    assert "router.post('/contact'" not in _files(spec)["routes/pages.js"]

    # The newsletter form follows its model too.
    spec = _healer()
    spec["_harbor_meta"] = {"exclude_data_models": ["NewsletterSubscriber"]}
    spec["data_models"] = [{"name": "ListSubscriber", "plural": "readers", "fields": [
        {"name": "Email", "sql_type": "VARCHAR(255)", "required": True}]}]
    assert ("'INSERT INTO readers (Email) SELECT $1::text WHERE NOT EXISTS "
            "(SELECT 1 FROM readers WHERE LOWER(Email::text) = LOWER($1::text))'") in _files(spec)["routes/pages.js"]
    spec["data_models"] = []
    rs = _resolve(spec)
    pages = dict(compose_files(rs))["routes/pages.js"]
    assert "INSERT INTO newsletter_subscribers" not in pages
    assert len(_warnings(rs, "newsletter_form_without_model")) == 1


@needs_express
def test_contact_post_validates_and_inserts_the_models_own_columns(roles_app):
    """The emitted POST /contact, executed, for the model in CUSTOM_CONTACT."""
    good = {"_csrf": "t0ken", "full_name": " Pat Doe ", "email": "Pat@Example.ORG",
            "company": "", "message": "Hello", "admin_notes": "x", "phone": "555"}
    stored, missing_name, bad_email, array_value, no_csrf = runtime._run(roles_app, "pages", [
        {"method": "POST", "path": "/contact", "session": runtime.ANON, "form": good},
        {"method": "POST", "path": "/contact", "session": runtime.ANON, "form": dict(good, full_name="")},
        {"method": "POST", "path": "/contact", "session": runtime.ANON, "form": dict(good, email="nope")},
        {"method": "POST", "path": "/contact", "session": runtime.ANON,
         "form": {"_csrf": "t0ken", "full_name[]": "x", "email": "a@b.example", "message": "m"}},
        {"method": "POST", "path": "/contact", "session": runtime.ANON, "form": dict(good, _csrf="wrong")},
    ])
    assert stored["status"] == 200 and stored["body"]["view"] == "contact"
    assert runtime._inserts(stored) == [
        "INSERT INTO contact_inquiries (full_name, email, company, message) VALUES ($1, $2, $3, $4)"]
    for rejected in (missing_name, bad_email, array_value):
        assert rejected["status"] == 200 and rejected["body"]["view"] == "contact"
        assert runtime._inserts(rejected) == []
    assert no_csrf["status"] == 403 and no_csrf["sql"] == []


@needs_ejs
def test_contact_widget_renders_the_fields_it_is_given():
    fields = B._contact_form_fields(copy.deepcopy(CUSTOM_CONTACT))
    out = widgets._render_widget("form-contact", {
        "heading": "Write to us", "service_options": ["A", "B"], "form_fields": fields,
        "submitted": {"full_name": "Pat <Doe>", "company": "Example Co"},
    })
    names = re.findall(r'<(?:input|textarea|select)[^>]* name="([a-z_]+)"', out)
    assert names == ["_csrf", "full_name", "email", "company", "message"]
    assert 'value="Pat &lt;Doe&gt;"' in out and 'value="Example Co"' in out
    assert 'type="email"' in out and "<textarea" in out
    assert "admin_notes" not in out and 'name="phone"' not in out

    # Without form_fields the widget renders the default contact shape,
    # service select included, and echoes what was typed.
    default = widgets._render_widget("form-contact", {
        "heading": "Contact", "service_options": ["A", "B"],
        "submitted": {"name": "Pat", "message": "Hello there", "service_interest": "B"},
    })
    names = re.findall(r'<(?:input|textarea|select)[^>]* name="([a-z_]+)"', default)
    assert names == ["_csrf", "name", "email", "phone", "service_interest", "message"]
    assert 'value="Pat"' in default and ">Hello there</textarea>" in default
    assert '<option value="B" selected>B</option>' in default
    assert "Optional." in default and 'placeholder="How can I support you?"' in default
    # No service options: no service select.
    assert 'name="service_interest"' not in widgets._render_widget("form-contact", {"service_options": []})


def test_contact_view_passes_the_model_fields_and_page_copy_cannot_replace_them():
    spec = _coach()
    spec["data_models"] = [copy.deepcopy(CUSTOM_CONTACT)]
    spec["pages"].setdefault("contact", {})["form"] = {
        "heading": "Say hello", "form_fields": [{"name": "injected_column", "field_type": "text"}]}
    view = _files(spec)["views/contact.ejs"]
    assert "injected_column" not in view
    assert view.count('"form_fields": [{"name": "full_name"') == 1
    assert "Say hello" in view


# ---------------------------------------------------------------------------
# Listings and the admin dashboard follow the model's columns
# ---------------------------------------------------------------------------

def _model(name: str, *fields, **extra) -> dict:
    return dict({"name": name, "fields": [dict(f) for f in fields]}, **extra)


TITLE = {"name": "title", "sql_type": "VARCHAR(255)", "required": True}


@pytest.mark.parametrize("model,expected", [
    (_model("Event"), "ORDER BY event_date ASC NULLS LAST, id ASC"),          # the engine's default fields
    (_model("Workshop", TITLE, {"name": "starts_on", "sql_type": "DATE"}),
     "ORDER BY starts_on ASC NULLS LAST, id ASC"),
    (_model("Class", TITLE, {"name": "class_date", "field_type": "date"}),
     "ORDER BY class_date ASC NULLS LAST, id ASC"),
    (_model("Talk", TITLE, {"name": "starts_at", "sql_type": "TIMESTAMPTZ"}),
     "ORDER BY starts_at ASC NULLS LAST, id ASC"),
    # A model named Event without a date column is not ordered by one.
    (_model("Event", TITLE, {"name": "venue", "sql_type": "TEXT"}), "ORDER BY id DESC"),
    # Bookkeeping timestamps and a clock time are not "the date" of a row.
    (_model("Note", TITLE, {"name": "updated_at", "sql_type": "TIMESTAMPTZ"},
            {"name": "start_time", "sql_type": "TIME"}), "ORDER BY id DESC"),
    (_model("BlogPost"), "ORDER BY published_at DESC NULLS LAST, id DESC"),
    (_model("Memo", TITLE, {"name": "sent_on", "sql_type": "DATE"},
            {"name": "published_at", "sql_type": "TIMESTAMPTZ"}),
     "ORDER BY published_at DESC NULLS LAST, id DESC"),
])
def test_listing_order_comes_from_the_models_columns(model, expected):
    assert B._listing_order_by(model) == expected


def test_resource_router_never_orders_by_a_column_the_model_lacks():
    spec = _coach()
    spec["data_models"] = [
        _model("Event", TITLE, {"name": "held_on", "sql_type": "DATE"}, public_listing_path="/happenings"),
        _model("Workshop", TITLE, {"name": "starts_on", "sql_type": "DATE"}, public_listing_path="/workshops"),
    ]
    files = _files(spec)
    events = files["routes/events.js"]
    assert "event_date" not in events
    assert "'ORDER BY held_on ASC NULLS LAST, id ASC'" in events
    assert "'ORDER BY starts_on ASC NULLS LAST, id ASC'" in files["routes/workshops.js"]


def test_dashboard_queries_name_only_columns_the_models_have():
    spec = _coach()
    spec["data_models"] = [
        copy.deepcopy(CUSTOM_CONTACT),
        _model("Event", TITLE, {"name": "held_on", "sql_type": "DATE"}, plural="happenings"),
    ]
    dashboard = _files(spec)["routes/admin-dashboard.js"]
    assert ("'SELECT id, full_name AS name, email AS email, NULL AS service, created_at "
            "FROM contact_inquiries ORDER BY created_at DESC LIMIT 5'") in dashboard
    assert ("'SELECT id, title AS title, held_on AS event_date, NULL AS event_time, NULL AS end_date, "
            "NULL AS end_time, NULL AS location FROM happenings WHERE held_on >= CURRENT_DATE "
            "ORDER BY held_on ASC, id ASC LIMIT 5'") in dashboard
    assert "events_enabled: true" in dashboard
    assert "events_admin_path: '/admin/happenings'" in dashboard
    assert "inquiries_admin_path: '/admin/contact-inquiries'" in dashboard
    # A failed dashboard query is logged, not swallowed.
    assert "catch (e) {}" not in dashboard

    # An Event model with no date column has nothing to list as "upcoming".
    spec["data_models"][1] = _model("Event", TITLE)
    dashboard = _files(spec)["routes/admin-dashboard.js"]
    assert "events_enabled: false" in dashboard and "CURRENT_DATE" not in dashboard

    # The default models keep the query they always had the columns for.
    healer = _files(_healer())["routes/admin-dashboard.js"]
    assert ("FROM events WHERE event_date >= CURRENT_DATE "
            "ORDER BY event_date ASC, event_time ASC NULLS LAST, id ASC LIMIT 5") in healer
    assert "service_interest AS service" in healer


def test_a_parent_select_is_labelled_by_the_parents_title_not_its_id():
    """The author archetype's Testimonial belongs to Book, and Book relies
    on the engine's default fields."""
    spec = _coach()
    spec["archetype"] = "author"
    spec["pages"] = {}
    testimonials = _files(spec)["routes/testimonials.js"]
    assert "'SELECT id, title AS display FROM books ORDER BY title'" in testimonials
    assert "id AS display" not in testimonials


# ---------------------------------------------------------------------------
# An auth_required page is enforced, refused, or reported
# ---------------------------------------------------------------------------

def _auth_spec(page: dict, *public_pages: dict, models=()) -> dict:
    spec = _coach()
    spec["data_models"] = [copy.deepcopy(m) for m in models]
    spec["custom_pages"] = [dict(p) for p in public_pages] + [dict(page, auth_required=True)]
    return spec


WORKSHOP = _model("Workshop", TITLE, public_listing_path="/workshops")
BOOKING = _model("Booking", {"name": "email", "sql_type": "VARCHAR(255)"})
RESOURCE = _model("Resource", TITLE)

REFUSED = [
    ("a page of the site", {"path": "/about"}, (), ()),
    ("a page of the site, other spelling", {"path": "/About/"}, (), ()),
    ("an earlier public custom page", {"path": "/pricing"}, ({"path": "/pricing", "title": "Pricing"},), ()),
    ("an earlier public custom page, other case", {"path": "/Pricing"}, ({"path": "/pricing"},), ()),
    ("a model's public listing", {"path": "/workshops"}, (), (WORKSHOP,)),
    ("a model's public listing, as a form page",
     {"path": "/workshops", "form_target": "Booking", "allowed_fields": ["name"]},
     (), (WORKSHOP, BOOKING)),
    ("an earlier public page named like a model", {"path": "/resources"}, ({"path": "/resources"},), (RESOURCE,)),
    ("a built-in public route", {"path": "/privacy"}, (), ()),
    ("the login form", {"path": "/login"}, (), ()),
    ("a public detail page, parametric", {"path": "/workshops/:id"}, (), (WORKSHOP,)),
    ("a public detail page, by id", {"path": "/workshops/7"}, (), (WORKSHOP,)),
    ("the detail route of a listing page", {"path": "/blog/archive"}, (), ()),
    ("the detail route of a listing page, parametric", {"path": "/blog/:slug"}, (), ()),
]


@pytest.mark.parametrize("label,page,public_pages,models", REFUSED, ids=[r[0] for r in REFUSED])
def test_build_is_refused_when_a_protected_path_would_be_public(label, page, public_pages, models):
    spec = _auth_spec(page, *public_pages, models=models)
    with pytest.raises(ValueError) as refused:
        _resolve(spec)
    message = str(refused.value)
    assert message.startswith("Spec validation failed: custom page ")
    assert repr(page["path"]) in message and "is marked auth_required" in message
    assert "answered without a login by" in message

    # The refusal is about the flag: the same spec without it builds.
    spec["custom_pages"][-1]["auth_required"] = False
    _resolve(spec)


def test_refusal_names_every_conflicting_page_and_build_app_writes_nothing(tmp_path):
    spec = _auth_spec({"path": "/about"}, models=(WORKSHOP,))
    spec["custom_pages"].append({"path": "/workshops", "auth_required": True})
    out = tmp_path / "app"
    with pytest.raises(ValueError) as refused:
        build_app(spec, output_dir=out)
    assert "'/about'" in str(refused.value) and "'/workshops'" in str(refused.value)
    assert not out.exists()


NOT_EMITTED = [
    ("not a plain path", {"path": "/members area"}, (), "not a plain route path"),
    ("parametric, nothing public there", {"path": "/members/:id"}, (), "route parameter"),
    ("parametric under a private model", {"path": "/bookings/:id"}, (BOOKING,), "route parameter"),
    ("the route of a private model", {"path": "/bookings"}, (BOOKING,), "rows are private"),
    ("below a private model's routes", {"path": "/resources/handbook"}, (RESOURCE,), "routes of the Resource model"),
    ("a model router's own /new", {"path": "/resources/new"}, (RESOURCE,), "routes of the Resource model"),
    ("a model router's own /<x>/edit", {"path": "/resources/handbook/edit"}, (RESOURCE,),
     "routes of the Resource model"),
    # /services is a page of the coach site; a Service model's router is
    # mounted there too and still answers /services/new itself.
    ("/new below a page that a model shares", {"path": "/services/new"}, (_model("Service", TITLE),),
     "routes of the Service model"),
    ("same view file as an earlier page", {"path": "/team-leads"}, (), "same view file"),
]


@pytest.mark.parametrize("label,page,models,why", NOT_EMITTED, ids=[r[0] for r in NOT_EMITTED])
def test_a_protected_page_that_is_not_emitted_is_reported_and_its_path_is_not_served(label, page, models, why):
    spec = _auth_spec(page, {"path": "/team/leads", "title": "Leads"}, models=models)
    rs = _resolve(spec)
    reported = _warnings(rs, "auth_required_page_not_emitted")
    assert len(reported) == 1, rs.spec.engine_warnings
    assert repr(page["path"]) in reported[0] and why in reported[0]
    assert "no protected page exists at that path" in reported[0]
    # Nothing was emitted for it: no route, no view, no nav link, no sitemap entry.
    files = dict(compose_files(rs))
    assert f"router.get('{page['path']}'" not in files["routes/pages.js"]
    assert page["path"] not in json.loads(
        files["routes/seo.js"].split("const PUBLIC_ROUTES = ", 1)[1].split(";\n", 1)[0])
    # Resolving again does not report it twice.
    resolve_spec(rs.spec)
    assert len(_warnings(rs, "auth_required_page_not_emitted")) == 1


GUARDED_ELSEWHERE = [
    ("the admin dashboard", {"path": "/admin"}, ()),
    ("a built-in admin route", {"path": "/admin/settings"}, ()),
    ("the set-password form", {"path": "/set-password"}, ()),
    ("an earlier protected page", {"path": "/vault"}, ({"path": "/vault", "auth_required": True},)),
]


@pytest.mark.parametrize("label,page,earlier", GUARDED_ELSEWHERE, ids=[r[0] for r in GUARDED_ELSEWHERE])
def test_a_protected_page_on_a_path_that_asks_for_a_login_anyway_is_left_alone(label, page, earlier):
    rs = _resolve(_auth_spec(page, *earlier))
    assert _warnings(rs, "auth_required_page_not_emitted") == []


def test_a_protected_page_that_nothing_else_answers_is_kept_with_its_guard():
    """The other side of the classification: paths that look close to a
    model's or a page's routes but are the custom page's own."""
    spec = _coach()
    spec["data_models"] = [copy.deepcopy(RESOURCE), _model("Service", TITLE)]
    kept = ["/vault", "/resources", "/resources/a/b", "/services/pricing", "/about/team", "/admin/reports"]
    spec["custom_pages"] = [{"path": path, "auth_required": True, "allowed_roles": ["admin"]} for path in kept]
    rs = _resolve(spec)
    assert [w for w in rs.spec.engine_warnings
            if isinstance(w, dict) and "custom page" in str(w.get("detail"))] == []
    pages = dict(compose_files(rs))["routes/pages.js"]
    for path in kept:
        assert f"router.get('{path}', requireAuth, requireRole('admin'), async" in pages, path


def test_every_dropped_reason_has_an_outcome_for_a_protected_page():
    """The trace, as a test: one page for each reason _classify_custom_pages
    can give, each marked auth_required. None may come out both unprotected
    and unmentioned."""
    spec = _coach()
    spec["data_models"] = [copy.deepcopy(m) for m in (WORKSHOP, BOOKING, RESOURCE)]
    pages = {
        "parametric": "/members/:id", "not_plain": "/members area", "page_exists": "/about",
        "engine_route": "/terms", "model_listing": "/workshops", "model_route": "/bookings",
        "detail_route": "/resources/x", "duplicate": "/dup", "duplicate_view": "/d-up",
    }
    spec["custom_pages"] = [{"path": "/dup", "title": "first"}, {"path": "/d/up", "title": "second"}] + [
        {"path": path} for path in pages.values()]
    # Resolved without the flag (with it, resolve_spec refuses this spec),
    # then every page under test is marked auth_required and classified.
    rs = _resolve(spec)
    for cp in rs.spec.custom_pages[2:]:
        cp["auth_required"] = True
    dropped = {reason: cp for cp, reason in B._classify_custom_pages(rs)[1]}
    assert sorted(dropped) == sorted(pages), "one page per reason"
    outcomes = {reason: B._dropped_auth_page_outcome(rs, cp, reason)[0] for reason, cp in dropped.items()}
    assert outcomes == {
        "parametric": "absent", "not_plain": "absent", "model_route": "absent",
        "detail_route": "absent", "duplicate_view": "absent",
        "page_exists": "public", "engine_route": "public", "model_listing": "public", "duplicate": "public",
    }
    errors = B._auth_required_page_errors(rs)
    warned = [w for w in B._custom_page_warnings(rs) if w["reason"] == "auth_required_page_not_emitted"]
    assert len(errors) == 4 and len(warned) == 5
    assert len(errors) + len(warned) == len(pages)


def test_a_form_page_at_a_private_models_route_is_kept_and_the_router_yields_to_it():
    """A form at /rsvps that stores Rsvp rows: the model's router answers
    404 at its mount for visitors, except that it hands GET and POST of the
    mount path on to this page."""
    spec = _coach()
    spec["data_models"] = [_model("Rsvp", {"name": "email", "sql_type": "VARCHAR(255)", "required": True})]
    spec["custom_pages"] = [{"path": "/Rsvps/", "title": "RSVP", "form_target": "Rsvp",
                             "allowed_fields": ["email"]}]
    rs = _resolve(spec)
    files = dict(compose_files(rs))
    assert "router.get('/Rsvps/'" in files["routes/pages.js"] and "router.post('/Rsvps/'" in files["routes/pages.js"]
    router = files["routes/rsvps.js"]
    assert "const CUSTOM_PAGE_AT_PUBLIC_MOUNT = true;" in router
    assert "router.post('/', yieldPublicPost, validateCsrfToken," in router

    def public_listing_gate(source: str) -> str:
        """The statement that decides a visitor's GET of the mount path."""
        handler = source[source.index("router.get('/', "):source.index("const isAdmin")]
        return handler.split("if (!req.baseUrl.startsWith('/admin/')) {", 1)[1].split("}", 1)[0].strip()

    assert public_listing_gate(router) == "return next();"

    # A page that is not a form does not get the private model's path.
    spec["custom_pages"] = [{"path": "/rsvps", "title": "RSVP list"}]
    rs = _resolve(spec)
    files = dict(compose_files(rs))
    assert "router.get('/rsvps'" not in files["routes/pages.js"]
    assert "const CUSTOM_PAGE_AT_PUBLIC_MOUNT = false;" in files["routes/rsvps.js"]
    assert public_listing_gate(files["routes/rsvps.js"]).startswith("return res.status(404)")
    assert len(_warnings(rs, "custom_page_route_taken_dropped")) == 1
    nav = json.loads(files["middleware/settings.js"].split("const _NAV_ITEMS = ", 1)[1].split(";\n", 1)[0])
    assert "/rsvps" not in [item["href"] for item in nav]


# ---------------------------------------------------------------------------
# Column defaults and column names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    # One literal cast to one plain type passes through.
    ("'[]'::jsonb", "'[]'::jsonb"),
    ("'{}'::text[]", "'{}'::text[]"),
    ("'2020-01-01'::date", "'2020-01-01'::date"),
    ("'0'::numeric(10,2)", "'0'::numeric(10,2)"),
    ("'1'::double precision", "'1'::double precision"),
    ("'it''s'::text", "'it''s'::text"),
    # Anything after the type is not part of a default: the whole value is
    # quoted as a string instead of riding into the DDL.
    ("'x'::text UNIQUE", "'''x''::text UNIQUE'"),
    ("'1'::int REFERENCES users", "'''1''::int REFERENCES users'"),
    ("'1'::int PRIMARY KEY", "'''1''::int PRIMARY KEY'"),
    ("'a'::text NOT NULL", "'''a''::text NOT NULL'"),
    ("'a'::text COLLATE \"C\"", "'''a''::text COLLATE \"C\"'"),
    ("'a'::users", "'''a''::users'"),
    ("'x'::text); DROP TABLE users; --", "'''x''::text); DROP TABLE users; --'"),
    # Numbers: plain decimals only.
    ("0", "0"), ("-3", "-3"), ("1.5", "1.5"), ("1e3", "1e3"),
    ("nan", "'nan'"), ("inf", "'inf'"), ("Infinity", "'Infinity'"), ("1_000", "'1_000'"),
    (float("nan"), "NULL"), (float("inf"), "NULL"),
    (False, "FALSE"), (0, "0"), ("pending", "'pending'"), ("now()", "NOW()"),
])
def test_a_column_default_is_exactly_one_value(value, expected):
    assert B._format_sql_default(value) == expected


def test_false_and_zero_are_defaults_and_empty_is_not():
    spec = _coach()
    spec["data_models"] = [_model(
        "Thing",
        {"name": "active", "sql_type": "BOOLEAN", "default": False},
        {"name": "rank", "sql_type": "INTEGER", "default": 0},
        {"name": "label", "sql_type": "TEXT", "default": ""},
        {"name": "note", "sql_type": "TEXT", "default": None},
        {"name": "tag", "sql_type": "TEXT", "default": "'x'::text UNIQUE"},
    )]
    schema = _files(spec)["db/schema.sql"]
    table = schema[schema.index("CREATE TABLE IF NOT EXISTS things"):]
    table = table[:table.index(");")]
    assert "  active BOOLEAN DEFAULT FALSE," in table
    assert "  rank INTEGER DEFAULT 0," in table
    assert "  label TEXT," in table and "  note TEXT," in table
    assert "UNIQUE" not in table.replace("'''x''::text UNIQUE'", "")


@pytest.mark.parametrize("fields,fragment", [
    (["Email", "email"], "declares the column 'email' more than once"),
    (["title", "title"], "declares the column 'title' more than once"),
    (["Title", "body", "TITLE"], "declares the column 'title' more than once"),
    (["ID", "title"], "differs only by letter case from the column 'id'"),
    (["Created_At", "title"], "differs only by letter case from the column 'created_at'"),
])
def test_gate_refuses_column_names_that_are_one_column_to_postgres(fields, fragment):
    spec = _coach()
    spec["data_models"] = [_model("Thing", *[{"name": name, "sql_type": "TEXT"} for name in fields])]
    errors = validate_spec(BuildSpec.from_dict(spec))
    assert any(fragment in e for e in errors), errors
    with pytest.raises(ValueError, match="Spec validation failed"):
        _resolve(spec)


def test_declaring_id_or_created_at_in_their_own_spelling_is_still_accepted():
    spec = _coach()
    spec["data_models"] = [_model("Thing", {"name": "id", "sql_type": "INTEGER"}, TITLE,
                                  {"name": "created_at", "sql_type": "TIMESTAMPTZ"})]
    schema = _files(spec)["db/schema.sql"]
    table = schema[schema.index("CREATE TABLE IF NOT EXISTS things"):]
    table = table[:table.index(");")]
    assert table.count("id SERIAL PRIMARY KEY") == 1 and table.count("created_at") == 1


def test_a_declared_foreign_key_column_is_not_added_a_second_time_in_another_case():
    spec = _coach()
    spec["data_models"] = [
        _model("Shelf", TITLE),
        _model("Volume", TITLE, {"name": "Shelf_Id", "sql_type": "INTEGER"}, belongs_to="Shelf"),
    ]
    files = _files(spec)
    table = files["db/schema.sql"][files["db/schema.sql"].index("CREATE TABLE IF NOT EXISTS volumes"):]
    table = table[:table.index(");")]
    assert len(re.findall(r"(?i)\bshelf_id\b", table)) == 1
    allowed = json.loads(files["routes/volumes.js"].split("const ALLOWED_COLS = ", 1)[1].split(";\n", 1)[0])
    assert [c.lower() for c in allowed].count("shelf_id") == 1


# ---------------------------------------------------------------------------
# Markdown links
# ---------------------------------------------------------------------------

def test_markdown_link_matching_is_linear_in_the_block_length():
    """A run of "[" used to cost a scan to the end of the block for every
    bracket in it: 40,000 of them took four seconds, and four times as long
    for every doubling. The engine does not cap a block's length, so this
    is the build worker's time. With the label unable to contain "[", the
    60,000 brackets below (about nine seconds before) and the larger
    blocks after them render in milliseconds."""
    for hostile in ("[" * 60_000, "[" * 200_000, "[a" * 100_000, "[a](" * 50_000, "[a](x(y)" * 25_000,
                    "[" * 100_000 + "](x)", "[a]" * 60_000 + "(", "](" * 100_000):
        started = time.perf_counter()
        out = render_markdown_blocks([hostile])
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, f"{hostile[:8]!r} x {len(hostile)}: {elapsed:.2f}s"
        assert out.startswith("<p>")


@pytest.mark.parametrize("source,expected", [
    ("[site](https://example.org/a)", '<p><a href="https://example.org/a">site</a></p>'),
    ("see [the docs](/docs) now", '<p>see <a href="/docs">the docs</a> now</p>'),
    ("[w](https://en.example.org/wiki/A_(b))", '<p><a href="https://en.example.org/wiki/A_(b)">w</a></p>'),
    ("[x](javascript:alert(1))", "<p>x</p>"),
    ("[a] and [b](/b)", '<p>[a] and <a href="/b">b</a></p>'),
    # The innermost bracket pair is the link label.
    ("[a [b](/b)", '<p>[a <a href="/b">b</a></p>'),
    ("[[b](/b)]", '<p>[<a href="/b">b</a>]</p>'),
])
def test_markdown_links_still_render(source, expected):
    assert render_markdown_blocks([source]) == expected


# ---------------------------------------------------------------------------
# The one directory build_app deletes
# ---------------------------------------------------------------------------

def test_build_app_replaces_an_earlier_build_and_an_empty_directory(tmp_path):
    out = tmp_path / "site"
    out.mkdir()                                   # empty: fine
    build_app(_coach(), output_dir=out)
    (out / "stale-from-last-build.txt").write_text("x", encoding="utf-8")
    build_app(_coach(), output_dir=out)           # holds compose-manifest.txt: replaced
    assert (out / "server.js").exists() and not (out / "stale-from-last-build.txt").exists()


def test_build_app_refuses_to_delete_a_directory_it_cannot_recognize_as_a_build(tmp_path):
    victim = tmp_path / "documents"
    victim.mkdir()
    (victim / "keep.txt").write_text("not a build", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to delete"):
        build_app(_coach(), output_dir=victim)
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "not a build"
    assert sorted(p.name for p in victim.iterdir()) == ["keep.txt"]

    # A symlink is refused as well: its target is somebody else's directory.
    link = tmp_path / "link"
    link.symlink_to(victim, target_is_directory=True)
    with pytest.raises(ValueError, match="not a plain directory"):
        build_app(_coach(), output_dir=link)
    # So is a file.
    with pytest.raises(ValueError, match="not a plain directory"):
        build_app(_coach(), output_dir=victim / "keep.txt")
    assert (victim / "keep.txt").exists()


def test_the_working_directory_and_the_engine_are_never_deleted(tmp_path, monkeypatch):
    """Even a directory that looks like a build (it holds a manifest) is
    kept when the working directory or the engine's source lives in it.
    Everything here is under tmp_path, the stand-in engine directory
    included, so a regression deletes nothing that matters."""
    work = tmp_path / "work"
    (work / "inner").mkdir(parents=True)
    engine = tmp_path / "repo" / "compose"
    engine.mkdir(parents=True)
    for looks_like_a_build in (work, work / "inner", tmp_path / "repo", engine):
        (looks_like_a_build / "compose-manifest.txt").write_text("files: 1\n", encoding="utf-8")
    monkeypatch.chdir(work / "inner")
    monkeypatch.setattr(B, "COMPOSE_DIR", engine)
    for target in (work / "inner", work, Path("."), Path(".."), tmp_path / "repo", engine):
        with pytest.raises(ValueError, match="refusing to delete"):
            B._remove_previous_build(target)
        assert (target / "compose-manifest.txt").exists(), target
    # The same call does remove a finished build that holds neither.
    finished = tmp_path / "finished"
    finished.mkdir()
    (finished / "compose-manifest.txt").write_text("files: 1\n", encoding="utf-8")
    B._remove_previous_build(finished)
    assert not finished.exists()


def test_a_leftover_directly_under_builds_is_replaced_without_a_manifest(tmp_path, monkeypatch):
    """The worker retries a build into the same builds/<name> directory; an
    attempt that died before writing its manifest must not block the next."""
    monkeypatch.chdir(tmp_path)
    leftover = tmp_path / "builds" / "sample-coach"
    leftover.mkdir(parents=True)
    (leftover / "half-written.js").write_text("x", encoding="utf-8")
    out = build_app(_coach())                      # default output: builds/<app_name>
    assert out == Path("builds") / "sample-coach"
    assert (leftover / "server.js").exists() and not (leftover / "half-written.js").exists()
    # One level further down is not a build directory any more.
    nested = tmp_path / "builds" / "group" / "site"
    nested.mkdir(parents=True)
    (nested / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to delete"):
        build_app(_coach(), output_dir=nested)
    assert (nested / "keep.txt").exists()
