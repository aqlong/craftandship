"""What the web app's spec translator emits, this engine builds.

A Harbor build's spec is written by a model. The web app passes it through
its translator (`translateExtractedSpecToV1` in
web/src/services/harbor_to_compose.ts) and stores the result; the worker
sets `admin_email`, runs `compose.intake.translate_v1_spec` and hands the
spec to this engine. The engine refuses a spec that contradicts itself (a
page marked auth_required at a path something else serves publicly, a
public form that adds rows to a model visitors can read and that cannot hold
a row back, two models on one table, a role name it cannot check). A
refusal is a failed build for a customer, and the same spec is refused again
on every retry. The translator therefore resolves each of those
contradictions before the spec is stored, always toward less exposure.

This module holds the two sides together. It runs the REAL translator (the
TypeScript source, compiled in memory by the compiler in web/node_modules;
see _translate_extracted_spec.js), does to its output what the worker does,
and requires a build: for the specs named in the review (a /book page beside
a publicly listed Session model), for every other contradiction a model can
write, and for each of them against every archetype's own models and pages.

Skipped without node or without web/node_modules/typescript
(`cd web && npm ci`).
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from compose.build import (
    BuildSpec,
    _public_row_sources,
    build_app,
    compose_files,
    resolve_spec,
    validate_spec,
)
from compose.intake import translate_v1_spec

COMPOSE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = COMPOSE_DIR.parent
WEB_DIR = REPO_ROOT / "web"
DRIVER = Path(__file__).with_name("_translate_extracted_spec.js")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not (WEB_DIR / "node_modules" / "typescript").exists(),
    reason="needs node and web/node_modules/typescript (run `cd web && npm ci`)",
)

OWNER_EMAIL = "owner@example.com"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "skip")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")


# ---------------------------------------------------------------------------
# Extracted specs a model could write
# ---------------------------------------------------------------------------

def extracted(**overrides) -> dict:
    """An extracted spec, as the spec extraction returns it."""
    spec = {
        "spec_version": "1.0", "app_name": "sample-keeper", "business_type": "practitioner",
        "archetype": "healer-wellness", "tier": "build", "admin_email": "sample@example.com",
        "pages": [], "data_models": [], "copy": {}, "design": {}, "_extraction_warnings": [],
    }
    spec.update(overrides)
    return spec


TITLE = {"name": "title", "type": "string", "required": True}
FIELDS = [TITLE, {"name": "description", "type": "long_text"}]
QUOTE = [{"name": "author_name", "type": "string", "required": True}, {"name": "quote", "type": "long_text"}]
COPIED = "A paragraph copied from the owner's existing site, long enough to be kept."
LISTED_SESSION = {"name": "Session", "public_listing_path": "/sessions", "fields": FIELDS}
LISTED_STORY = {"name": "Story", "public_listing_path": "/stories", "fields": FIELDS}

BOOK_BESIDE_LISTED_SESSION = extracted(pages=[{"slug": "book", "title": "Book a session"}], data_models=[LISTED_SESSION])
FORM_INTO_LISTED_STORY = extracted(pages=[{"slug": "tell", "form_target": "Story"}], data_models=[LISTED_STORY])

# Each of these was refused by the engine when the translator passed the
# contradiction through unchanged.
CONTRADICTIONS = [
    ("a /book page beside a publicly listed Session model", BOOK_BESIDE_LISTED_SESSION),
    ("an /apply page beside a publicly listed Application model", extracted(
        pages=[{"slug": "apply"}],
        data_models=[{"name": "Application", "public_listing_path": "/applications", "fields": FIELDS}])),
    ("a form into a model the spec lists", FORM_INTO_LISTED_STORY),
    ("a form into a model the archetype shows on its home page", extracted(
        pages=[{"slug": "share-your-story", "form_target": "Testimonial"}],
        data_models=[{"name": "Testimonial", "fields": QUOTE}])),
    ("a form into a model another archetype shows", extracted(
        archetype="salon-spa", pages=[{"slug": "leave-a-review", "form_target": "Review"}],
        data_models=[{"name": "Review", "fields": QUOTE}])),
    ("a form into a listed model without a field list", extracted(
        pages=[{"slug": "tell", "form_target": "Tale"}], data_models=[{"name": "Tale", "public_listing_path": "/tales"}])),
    ("a form into a listed model whose `published` field is a date", extracted(
        pages=[{"slug": "tell", "form_target": "Tale"}],
        data_models=[{"name": "Tale", "public_listing_path": "/tales",
                      "fields": FIELDS + [{"name": "published", "type": "date"}]}])),
    ("a form into a listed model whose `published` field has no type", extracted(
        pages=[{"slug": "tell", "form_target": "Tale"}],
        data_models=[{"name": "Tale", "public_listing_path": "/tales", "fields": FIELDS + [{"name": "published"}]}])),
    ("the contact form's model listed publicly", extracted(
        data_models=[{"name": "ContactInquiry", "public_listing_path": "/guestbook",
                      "fields": [{"name": "email", "type": "email"}, {"name": "message", "type": "long_text"}]}])),
    ("the newsletter's model listed publicly", extracted(
        data_models=[{"name": "NewsletterSubscriber", "public_listing_path": "/readers",
                      "fields": [{"name": "email", "type": "email"}]}])),
    ("a model that takes an archetype model's table through plural", extracted(
        data_models=[{"name": "Post", "plural": "blog-posts", "fields": FIELDS}])),
    ("the same, on a table the archetype shows, with a form into it", extracted(
        archetype="local-service", pages=[{"slug": "praise", "form_target": "Praise"}],
        data_models=[{"name": "Praise", "plural": "testimonials", "fields": QUOTE}])),
    ("allowed_roles with an empty entry", extracted(
        pages=[{"slug": "members", "auth_required": True, "allowed_roles": ["member", ""]}])),
    ("allowed_roles with an entry too long to be stored", extracted(
        pages=[{"slug": "members", "auth_required": True, "allowed_roles": ["m" * 60]}])),
    ("allowed_roles with a blank entry on a public page", extracted(
        pages=[{"slug": "team-notes", "allowed_roles": ["  "]}])),
    ("a protected page at an archetype page", extracted(archetype="author", pages=[{"slug": "books", "auth_required": True}])),
    ("a protected page at a built-in page", extracted(pages=[{"slug": "privacy", "auth_required": True}])),
    ("a protected page at the login route", extracted(pages=[{"slug": "login", "auth_required": True}])),
    ("a protected page at the newsletter route", extracted(pages=[{"slug": "newsletter-signup", "auth_required": True}])),
    ("a protected page below the journal", extracted(pages=[{"slug": "journal/archive", "auth_required": True}])),
    ("a protected page below the events page", extracted(pages=[{"slug": "events/vip", "auth_required": True}])),
    ("a protected page at a model's public listing", extracted(
        pages=[{"slug": "stories", "auth_required": True}], data_models=[LISTED_STORY])),
    ("a protected page below a model's public listing", extracted(
        pages=[{"slug": "stories/members", "auth_required": True}], data_models=[LISTED_STORY])),
    ("a path declared public, then protected", extracted(
        pages=[{"slug": "vault", "title": "Vault"}, {"slug": "vault", "auth_required": True}])),
    ("the same in another letter case", extracted(
        pages=[{"slug": "vault"}, {"path": "/Vault/", "auth_required": True}])),
    ("a protected page at the route of a copied page", extracted(
        copy_pages=[{"path": "/vault", "markdown_blocks": [COPIED]}], pages=[{"slug": "vault", "auth_required": True}])),
    ("a protected page at the route of a copied page two levels deep", extracted(
        copy_pages=[{"path": "/members/area", "markdown_blocks": [COPIED]}],
        pages=[{"slug": "members-area", "auth_required": True}])),
    # Columns the engine adds or reads with a fixed meaning.
    ("a created_at field that is text", extracted(
        data_models=[{"name": "Note", "fields": FIELDS + [{"name": "created_at", "type": "string"}]}])),
    ("a created_at field that is a number", extracted(
        data_models=[{"name": "Note", "fields": FIELDS + [{"name": "created_at", "sql_type": "BIGINT"}]}])),
    ("an id field of any type", extracted(
        data_models=[{"name": "Note", "fields": [{"name": "id", "type": "string"}] + FIELDS}])),
    ("a published_at field that is json", extracted(
        data_models=[{"name": "Note", "public_listing_path": "/notes",
                      "fields": FIELDS + [{"name": "published_at", "sql_type": "jsonb"}]}])),
    ("an email field that is a number", extracted(
        data_models=[{"name": "Lead", "fields": [TITLE, {"name": "email", "type": "integer"}]}])),
    ("a link column to the parent that is text", extracted(
        data_models=[{"name": "Shelf", "fields": FIELDS},
                     {"name": "Volume", "belongs_to": "Shelf",
                      "fields": FIELDS + [{"name": "shelf_id", "type": "string"}]}])),
    ("a link column to an archetype model that is text", extracted(
        data_models=[{"name": "Comment", "belongs_to": "BlogPost",
                      "fields": FIELDS + [{"name": "blog_post_id", "type": "string"}]}])),
    ("models that belong to each other", extracted(
        data_models=[{"name": "Alpha", "belongs_to": "Beta", "fields": FIELDS},
                     {"name": "Beta", "belongs_to": "Alpha", "fields": FIELDS}])),
    ("a ring of three models", extracted(
        data_models=[{"name": "Alpha", "belongs_to": "Gamma", "fields": FIELDS},
                     {"name": "Beta", "belongs_to": "Alpha", "fields": FIELDS},
                     {"name": "Gamma", "belongs_to": "Beta", "fields": FIELDS}])),
    ("the author's Book that belongs to the archetype's Testimonial", extracted(
        archetype="author", data_models=[{"name": "Book", "belongs_to": "Testimonial", "fields": FIELDS}])),
    # Who may fill in a form, and who may open a page.
    ("a form into a private model with no field list", extracted(
        pages=[{"slug": "tell", "form_target": "Rsvp"}], data_models=[{"name": "Rsvp"}])),
    ("a form into a private model whose fields are all the engine's", extracted(
        pages=[{"slug": "tell", "form_target": "Rsvp"}],
        data_models=[{"name": "Rsvp", "fields": [{"name": "status", "type": "string"}, {"name": "approved", "type": "boolean"}]}])),
    ("a page with roles and no login flag", extracted(
        pages=[{"slug": "staff-notes", "allowed_roles": ["admin"]}])),
    ("a page with roles and no login flag below an archetype's listing", extracted(
        pages=[{"slug": "events/vip", "allowed_roles": ["admin"]}])),
    ("a page with roles and no login flag, and a form", extracted(
        pages=[{"slug": "staff-intake", "allowed_roles": ["admin"], "form_target": "Lead"}],
        data_models=[{"name": "Lead", "fields": QUOTE}])),
    # A stock photo of a specific person.
    ("a copied page that names a stock photo of a person", extracted(
        copy_pages=[{"path": "/about", "markdown_blocks": [COPIED + " ![me](/images/stock/practitioner-portrait-01.jpg)"]}])),
]


def _archetype_cases() -> list:
    """The same contradictions against what each archetype owns: a public
    form into each of its models, another model on each of its tables, and
    a protected page at and one level below each of its pages."""
    cases = []
    archetypes = json.loads((COMPOSE_DIR / "data" / "archetypes.json").read_text(encoding="utf-8"))["archetypes"]
    for archetype in archetypes:
        aid = archetype["id"]
        for model in archetype.get("default_data_models") or []:
            cases.append((f"{aid}: a public form into {model['name']}", extracted(
                archetype=aid, pages=[{"slug": "send-it", "form_target": model["name"]}],
                data_models=[{"name": model["name"], "fields": QUOTE}])))
            cases.append((f"{aid}: another model on the table of {model['name']}", extracted(
                archetype=aid, pages=[{"slug": "send-it", "form_target": "Zed"}],
                data_models=[{"name": "Zed", "plural": model.get("plural") or "", "fields": QUOTE}])))
        for page in archetype.get("default_pages") or []:
            cases.append((f"{aid}: protected pages at and below /{page}", extracted(
                archetype=aid,
                pages=[{"slug": page, "auth_required": True}, {"slug": page + "/inner", "auth_required": True}])))
    return cases


ARCHETYPE_CASES = _archetype_cases()

# Paths at, below and beside the routes a build already has: a model's public
# listing (one and two segments deep), a private model's mount, the admin
# surface, the static mounts, the built-in pages and the archetype's listings.
NEAR_ROUTE_SLUGS = [
    "stories/a/b", "stories/5/edit", "stories/new", "stories/5", "our", "our/tales", "our/tales/x", "our/tales/x/y",
    "rsvps", "rsvps/new", "rsvps/5", "clients", "clients/new", "clients/7/edit",
    "uploads/x", "js/x", "images/x", "brand/x", "fonts/x", "admin/reports", "admin/stories", "admin/settings",
    "magic/x", "login/x", "health/x", "contact/thanks", "home", "index", "events/5", "journal/a/b", "gallery/x",
    "testimonials", "testimonials/x", "book-a-session", "contact-us", "photos", "posts",
]
NEAR_ROUTE_MODELS = [
    LISTED_STORY,
    {"name": "Tale", "public_listing_path": "/our/tales", "fields": FIELDS},
    {"name": "Rsvp", "fields": FIELDS},
    {"name": "Client", "fields": FIELDS},
]
NEAR_ROUTE_CASES = []
for _slug in NEAR_ROUTE_SLUGS:
    NEAR_ROUTE_CASES += [
        (f"near a route: a protected page at /{_slug}", extracted(
            archetype="event-producer", pages=[{"slug": _slug, "auth_required": True}], data_models=NEAR_ROUTE_MODELS)),
        (f"near a route: a protected form page at /{_slug}", extracted(
            archetype="event-producer",
            pages=[{"slug": _slug, "auth_required": True, "form_target": "Rsvp"}], data_models=NEAR_ROUTE_MODELS)),
        (f"near a route: a public form into a listed model at /{_slug}", extracted(
            archetype="event-producer", pages=[{"slug": _slug, "form_target": "Story"}], data_models=NEAR_ROUTE_MODELS)),
    ]

ALL_CASES = CONTRADICTIONS + ARCHETYPE_CASES + NEAR_ROUTE_CASES


# ---------------------------------------------------------------------------
# The translator, and what the worker does with its output
# ---------------------------------------------------------------------------

def run_translator(inputs: list, tmp_dir: Path) -> list:
    payload = tmp_dir / "extracted-specs.json"
    payload.write_text(json.dumps(inputs), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(DRIVER), str(WEB_DIR), str(payload)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    results = json.loads(proc.stdout)
    assert len(results) == len(inputs)
    return results


@pytest.fixture(scope="module")
def translated(tmp_path_factory) -> dict:
    """label -> what the translator returned for that case ({spec, warnings})."""
    results = run_translator([spec for _label, spec in ALL_CASES], tmp_path_factory.mktemp("translator"))
    by_label = {}
    for (label, _spec), result in zip(ALL_CASES, results):
        assert "error" not in result, f"{label}: the translator threw: {result.get('error')}"
        by_label[label] = result
    assert len(by_label) == len(ALL_CASES), "case labels must be unique"
    return by_label


def worker_spec(translator_spec: dict) -> dict:
    """The engine's input for a translator output, as the worker's validate
    stage produces it (queue/worker_pg.py): the customer's address becomes
    admin_email, then translate_v1_spec."""
    v1 = copy.deepcopy(translator_spec)
    v1["admin_email"] = OWNER_EMAIL
    assert "archetype" not in v1, "the translator emits the v1 shape; the worker translates it"
    return translate_v1_spec(v1, admin_email=v1["admin_email"], app_name=v1.get("app_name"))


def emitted_files(v2_spec: dict) -> dict:
    """Validate and compose, as build_app does; {path: content}."""
    errors = validate_spec(BuildSpec.from_dict(copy.deepcopy(v2_spec)))
    assert errors == [], errors
    return dict(compose_files(resolve_spec(BuildSpec.from_dict(copy.deepcopy(v2_spec)))))


def page_of(translator_spec: dict, path: str) -> dict:
    return next(p for p in translator_spec.get("pages") or [] if p["path"] == path)


def model_of(translator_spec: dict, name: str) -> dict:
    return next(m for m in translator_spec.get("data_models") or [] if m["name"] == name)


# ---------------------------------------------------------------------------
# The two specs from the review, built to disk and read back
# ---------------------------------------------------------------------------

def test_book_page_beside_a_publicly_listed_session_model_builds(translated, tmp_path):
    result = translated["a /book page beside a publicly listed Session model"]
    spec = result["spec"]
    # The translator no longer picks the listed model as the page's form target ...
    assert "form_target" not in page_of(spec, "/book")
    assert model_of(spec, "Session")["public_listing_path"] == "/sessions"

    # ... so the engine builds the site: the listing is public, /book is a page,
    # and nothing a visitor posts is stored in the listed table.
    out = build_app(worker_spec(spec), output_dir=tmp_path / "site")
    server = (out / "server.js").read_text(encoding="utf-8")
    pages = (out / "routes" / "pages.js").read_text(encoding="utf-8")
    assert re.search(r"app\.use\('/sessions',\s*[^)]*sessions", server), "the public listing is mounted"
    assert "router.get('/book'" in pages
    assert "router.post('/book'" not in pages
    assert "INSERT INTO sessions" not in pages
    assert (out / "views" / "custom" / "book.ejs").is_file()
    assert "Spec validation failed" not in (out / "compose-manifest.txt").read_text(encoding="utf-8")


def test_a_form_into_a_listed_model_builds_and_its_rows_wait_for_the_owner(translated, tmp_path):
    result = translated["a form into a model the spec lists"]
    spec = result["spec"]
    assert page_of(spec, "/tell")["form_target"] == "Story"
    assert model_of(spec, "Story")["fields"][-1] == {"name": "published", "type": "boolean", "default": True}
    assert any(w.startswith('data_model "Story": a "published" field was added') for w in result["warnings"])

    out = build_app(worker_spec(spec), output_dir=tmp_path / "site")
    schema = (out / "db" / "schema.sql").read_text(encoding="utf-8")
    pages = (out / "routes" / "pages.js").read_text(encoding="utf-8")
    stories = (out / "routes" / "stories.js").read_text(encoding="utf-8")
    table = re.search(r"CREATE TABLE IF NOT EXISTS stories \((.*?)\n\);", schema, re.S).group(1)
    # Rows the owner enters are visible by default; a row a visitor submits is
    # stored unpublished, and the public listing and detail page require TRUE.
    assert re.search(r"^\s*published BOOLEAN DEFAULT TRUE,?$", table, re.M), table
    assert "INSERT INTO stories (title, description, published) VALUES ($1, $2, FALSE)" in pages
    assert "const where = isAdmin ? '' : 'WHERE published = TRUE';" in stories
    assert "' AND published = TRUE'" in stories


# ---------------------------------------------------------------------------
# Every contradiction a model can write
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", [label for label, _spec in CONTRADICTIONS])
def test_the_translator_resolves_the_contradiction_and_the_engine_builds(translated, label):
    result = translated[label]
    emitted_files(worker_spec(result["spec"]))
    # Nothing is resolved silently: each of these leaves a warning in the spec.
    assert result["warnings"], "the translator changed the spec without a warning"
    assert result["spec"].get("_translation_warnings") == result["warnings"]


@pytest.mark.parametrize("label", [label for label, _spec in ARCHETYPE_CASES])
def test_the_same_contradictions_against_every_archetype(translated, label):
    emitted_files(worker_spec(translated[label]["spec"]))


def test_protected_pages_and_forms_near_other_routes_build(translated):
    refused = []
    for label, _spec in NEAR_ROUTE_CASES:
        try:
            emitted_files(worker_spec(translated[label]["spec"]))
        except (AssertionError, ValueError) as exc:
            refused.append(f"{label}: {str(exc)[:200]}")
    assert refused == []


def test_resolutions_never_widen_what_a_visitor_can_reach(translated):
    """Spot checks of the direction: a path that was asked to be protected
    is protected or absent, never public."""
    def files_for(label):
        return emitted_files(worker_spec(translated[label]["spec"]))

    # Protected page at a model listing: the page is kept behind the login
    # and the listing is withdrawn (the model's router answers visitors 404).
    spec = translated["a protected page at a model's public listing"]["spec"]
    assert page_of(spec, "/stories")["auth_required"] is True
    assert model_of(spec, "Story")["public_listing_path"] is None
    files = files_for("a protected page at a model's public listing")
    assert re.search(r"router\.get\('/stories', requireAuth", files["routes/pages.js"])
    assert "const CUSTOM_PAGE_AT_PUBLIC_MOUNT = true;" in files["routes/stories.js"]
    resolved = resolve_spec(BuildSpec.from_dict(worker_spec(spec)))
    assert "stories" not in _public_row_sources(resolved), "no public read of the Story table is left"

    # Public then protected: one page, behind the login.
    spec = translated["a path declared public, then protected"]["spec"]
    assert [p["path"] for p in spec["pages"]] == ["/vault"]
    assert spec["pages"][0]["auth_required"] is True
    files = files_for("a path declared public, then protected")
    assert re.search(r"router\.get\('/vault', requireAuth", files["routes/pages.js"])

    # A role list nobody could satisfy becomes admin-only, not "any account".
    spec = translated["allowed_roles with an entry too long to be stored"]["spec"]
    assert page_of(spec, "/members")["allowed_roles"] == ["admin"]
    files = files_for("allowed_roles with an entry too long to be stored")
    assert re.search(r"router\.get\('/members', requireAuth, requireRole\(\[?['\"]admin['\"]", files["routes/pages.js"])

    # Protected page at a copied page's route: the copy is left out.
    spec = translated["a protected page at the route of a copied page"]["spec"]
    assert "copy_pages" not in (spec.get("_harbor_meta") or {})
    files = files_for("a protected page at the route of a copied page")
    assert re.search(r"router\.get\('/vault', requireAuth", files["routes/pages.js"])
    assert COPIED not in "".join(c for c in files.values() if isinstance(c, str))

    # Protected page below the journal: dropped, so no page claims the path.
    spec = translated["a protected page below the journal"]["spec"]
    assert not spec.get("pages")


def test_a_crawl_merged_in_after_translation_does_not_make_a_protected_page_public(translated):
    """The web tier stores an operator's crawl of the owner's existing site
    on the trial and merges it into `_harbor_meta` AFTER its translator ran
    (web/src/services/harbor_dispatch.ts, web/src/routes/admin.ts), so the
    translator never sees those pages. A copied page at the route of a page
    the spec marks auth_required is then left out by translate_v1_spec:
    the build is not refused and the path stays behind the login."""
    spec = copy.deepcopy(translated["allowed_roles with an empty entry"]["spec"])
    assert page_of(spec, "/members")["auth_required"] is True
    meta = dict(spec.get("_harbor_meta") or {})
    meta["copy_source_url"] = "https://old-site.example/"
    meta["copy_pages"] = [
        {"path": "/Members/", "markdown_blocks": [COPIED]},
        {"path": "/our-story", "markdown_blocks": ["The story of the practice, copied from the existing site."]},
    ]
    spec["_harbor_meta"] = meta

    v2 = worker_spec(spec)
    assert "members" not in v2["pages"] and "our-story" in v2["pages"]
    assert [w for w in v2["_translation_warnings"] if isinstance(w, dict) and w["reason"] == "copy_pages_protected_path"] == [{
        "reason": "copy_pages_protected_path",
        "detail": "copy_pages entry '/Members/' would be served without a login at '/members', where the spec "
                  "has a page marked auth_required; the copied page was not imported",
    }]
    files = emitted_files(v2)
    assert re.search(r"router\.get\('/members', requireAuth", files["routes/pages.js"])
    everything = "".join(content for content in files.values() if isinstance(content, str))
    assert COPIED not in everything
    assert "The story of the practice, copied from the existing site." in everything


def test_the_engine_still_refuses_each_contradiction_when_it_is_put_back(translated):
    """The cases above build because the translator resolved them, not
    because the engine stopped checking: put the contradiction back into the
    translator's output and the engine refuses it."""
    def refusal(v2_spec):
        with pytest.raises(ValueError, match="Spec validation failed") as raised:
            resolve_spec(BuildSpec.from_dict(copy.deepcopy(v2_spec)))
        return str(raised.value)

    book = worker_spec(translated["a /book page beside a publicly listed Session model"]["spec"])
    next(p for p in book["custom_pages"] if p["path"] == "/book")["form_target"] = "Session"
    assert "lets a visitor add a row to the model 'Session'" in refusal(book)

    listing = worker_spec(translated["a protected page at a model's public listing"]["spec"])
    next(m for m in listing["data_models"] if m["name"] == "Story")["public_listing_path"] = "/stories"
    assert "custom page '/stories' is marked auth_required" in refusal(listing)

    roles = worker_spec(translated["allowed_roles with an empty entry"]["spec"])
    next(p for p in roles["custom_pages"] if p["path"] == "/members")["allowed_roles"] = ["member", ""]
    assert "allowed_roles entries must be role names" in refusal(roles)

    table = worker_spec(translated["a model that takes an archetype model's table through plural"]["spec"])
    next(m for m in table["data_models"] if m["name"] == "Post")["plural"] = "blog-posts"
    assert "map to the same table 'blog_posts'" in refusal(table)


# ---------------------------------------------------------------------------
# What each resolution looks like in the translator's output and in the build
# ---------------------------------------------------------------------------

def _engine_columns(files: dict, table: str) -> str:
    schema = files["db/schema.sql"]
    start = schema.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    return schema[start:schema.index(");", start)]


def test_columns_the_engine_owns_are_left_to_the_engine(translated):
    for label, table in (("a created_at field that is text", "notes"), ("a created_at field that is a number", "notes"),
                         ("an id field of any type", "notes")):
        result = translated[label]
        assert [f["name"] for f in model_of(result["spec"], "Note")["fields"]] == ["title", "description"], label
        assert any(w.startswith("Note.") and "field dropped" in w for w in result["warnings"]), label
        columns = _engine_columns(emitted_files(worker_spec(result["spec"])), table)
        assert columns.count("created_at TIMESTAMPTZ DEFAULT NOW()") == 1 and columns.count("id SERIAL PRIMARY KEY") == 1, label
    note = model_of(translated["a published_at field that is json"]["spec"], "Note")
    assert [f["name"] for f in note["fields"]] == ["title", "description"]
    lead = model_of(translated["an email field that is a number"]["spec"], "Lead")
    assert {"name": "email", "type": "email"} in lead["fields"]
    assert "email VARCHAR(255)" in _engine_columns(emitted_files(worker_spec(translated["an email field that is a number"]["spec"])), "leads")


def test_a_date_or_timestamp_created_at_is_kept(tmp_path):
    results = run_translator([extracted(data_models=[{"name": "Note", "fields": FIELDS + [
        {"name": "created_at", "type": "timestamp"}]}])], tmp_path)
    (result,) = results
    assert model_of(result["spec"], "Note")["fields"][-1] == {"name": "created_at", "type": "timestamp"}
    emitted_files(worker_spec(result["spec"]))


def test_the_column_that_links_a_child_to_its_parent_is_the_engines(translated):
    for label, child, table in (("a link column to the parent that is text", "Volume", "volumes"),
                                ("a link column to an archetype model that is text", "Comment", "comments")):
        result = translated[label]
        assert [f["name"] for f in model_of(result["spec"], child)["fields"]] == ["title", "description"], label
        assert any("the engine adds the column that links it to" in w for w in result["warnings"]), label
        columns = _engine_columns(emitted_files(worker_spec(result["spec"])), table)
        assert "INTEGER REFERENCES" in columns and columns.count("_id INTEGER") == 1, columns


def test_models_that_belong_to_each_other_keep_one_link(translated):
    for label in ("models that belong to each other", "a ring of three models"):
        result = translated[label]
        links = [m for m in result["spec"]["data_models"] if m.get("belongs_to")]
        names = len(result["spec"]["data_models"])
        assert len(links) == names - 1, label
        assert any("belong to each other in a ring" in w for w in result["warnings"]), label
    book = model_of(translated["the author's Book that belongs to the archetype's Testimonial"]["spec"], "Book")
    assert "belongs_to" not in book


def test_a_child_declared_before_its_parent_builds_with_the_parent_first(tmp_path):
    """Nothing for the translator to change: the engine orders the tables."""
    (result,) = run_translator([extracted(
        data_models=[{"name": "Chapter", "belongs_to": "Volume", "fields": FIELDS},
                     {"name": "Volume", "fields": FIELDS}])], tmp_path)
    assert not result["warnings"]
    files = emitted_files(worker_spec(result["spec"]))
    schema = files["db/schema.sql"]
    assert schema.index("CREATE TABLE IF NOT EXISTS volumes (") < schema.index("CREATE TABLE IF NOT EXISTS chapters (")


def test_every_public_form_page_names_its_fields(translated, tmp_path):
    for label in ("a form into a model the spec lists", "a form into a model the archetype shows on its home page"):
        spec = translated[label]["spec"]
        page = next(p for p in spec["pages"] if p.get("form_target"))
        assert page["allowed_fields"], label
        model = model_of(spec, page["form_target"])
        assert set(page["allowed_fields"]) <= {f["name"] for f in model["fields"]}
        assert not set(page["allowed_fields"]) & {"published", "id", "created_at", "status"}
    # The booking form the translator adds for a conversation about booking.
    result = run_translator([extracted(pages=[{"slug": "home", "sections": [
        {"type": "booking", "content": {"paths": [{"label": "session"}]}}]}])], tmp_path)[0]
    booking = page_of(result["spec"], "/book-a-session")
    assert booking["allowed_fields"] == ["requester_name", "requester_email", "requester_phone", "preferred_window", "notes"]
    emitted_files(worker_spec(result["spec"]))
    # A form whose model has nothing a visitor may fill in is not a form.
    for label in ("a form into a private model with no field list",
                  "a form into a private model whose fields are all the engine's"):
        spec = translated[label]["spec"]
        assert not [p for p in spec.get("pages") or [] if p.get("form_target")], label
        assert any("has no field of its own that a visitor may fill in" in w for w in translated[label]["warnings"]), label


def test_a_page_with_roles_is_a_protected_page_in_the_translator_and_in_the_build(translated):
    spec = translated["a page with roles and no login flag"]["spec"]
    assert page_of(spec, "/staff-notes")["auth_required"] is True
    files = emitted_files(worker_spec(spec))
    assert re.search(r"router\.get\('/staff-notes', requireAuth, requireRole\(\[?['\"]admin['\"]", files["routes/pages.js"])
    # Below an archetype's listing the page is dropped, like any protected page there.
    below = translated["a page with roles and no login flag below an archetype's listing"]
    assert not below["spec"].get("pages")
    assert any("lists allowed_roles without auth_required" in w for w in below["warnings"])
    # A form on such a page is a form behind a login: no list of fields is required of it.
    form = translated["a page with roles and no login flag, and a form"]["spec"]
    assert page_of(form, "/staff-intake")["auth_required"] is True and "allowed_fields" not in page_of(form, "/staff-intake")
    emitted_files(worker_spec(form))


def test_a_stock_photo_of_a_person_is_taken_out_of_everything_the_translator_returns(translated):
    result = translated["a copied page that names a stock photo of a person"]
    blocks = result["spec"]["_harbor_meta"]["copy_pages"][0]["markdown_blocks"]
    assert blocks == [COPIED + " ![me]()"]
    assert any(w.startswith('a path to the stock photo "practitioner-portrait-01.jpg" was removed') for w in result["warnings"])
    assert "/images/stock/" not in json.dumps(result["spec"]), "no path to the library is left, warning included"
    emitted_files(worker_spec(result["spec"]))


def test_the_engine_refuses_each_column_rule_when_it_is_put_back(translated):
    def refusal(v2_spec):
        with pytest.raises(ValueError, match="Spec validation failed") as raised:
            resolve_spec(BuildSpec.from_dict(copy.deepcopy(v2_spec)))
        return str(raised.value)

    note = worker_spec(translated["a created_at field that is text"]["spec"])
    next(m for m in note["data_models"] if m["name"] == "Note")["fields"].append({"name": "created_at", "sql_type": "VARCHAR(255)"})
    assert "field 'created_at' has the type 'VARCHAR(255)'" in refusal(note)

    link = worker_spec(translated["a link column to the parent that is text"]["spec"])
    next(m for m in link["data_models"] if m["name"] == "Volume")["fields"].append({"name": "shelf_id", "sql_type": "TEXT"})
    assert "declares 'shelf_id', the column that holds the id of a 'Shelf' row" in refusal(link)

    ring = worker_spec(translated["models that belong to each other"]["spec"])
    for model in ring["data_models"]:
        model["belongs_to"] = "Beta" if model["name"] == "Alpha" else "Alpha"
    assert "refer to each other in a ring through belongs_to" in refusal(ring)

    form = worker_spec(translated["a form into a model the spec lists"]["spec"])
    del next(p for p in form["custom_pages"] if p.get("form_target"))["allowed_fields"]
    assert "the page names no allowed_fields" in refusal(form)

    roles = worker_spec(translated["a page with roles and no login flag"]["spec"])
    page = next(p for p in roles["custom_pages"] if p["path"] == "/staff-notes")
    # Put a page that lists roles at a path a page of the site answers without a login.
    assert refusal({**roles, "custom_pages": [dict(page, path="/about", auth_required=False)]}).count("is marked auth_required") == 1

    stock = worker_spec(translated["a copied page that names a stock photo of a person"]["spec"])
    stock["pages"] = {"about": {"hero": {"image_url": "/images/stock/practitioner-portrait-01.jpg"}}}
    assert "names the stock photo 'practitioner-portrait-01.jpg'" in refusal(stock)
