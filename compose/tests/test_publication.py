"""What a visitor can write to a generated site, and when it becomes public.

The rule under test: a row created by a visitor who is not signed in is
never shown on the site until the owner publishes it, and every value a
visitor submits is checked against the model before it is stored.

  - every unauthenticated INSERT into a model table is enumerated
    (`_public_insert_targets`) and stores `published = FALSE` when the model
    has that column (`_public_insert_sql`);
  - a build whose public form stores into a model that is shown to visitors
    and has no boolean `published` column is refused (`_public_form_errors`);
  - only a BOOLEAN column named `published` is that flag: a DATE or TEXT
    column of the same name is an ordinary column (`_publication_column`);
  - the POST handlers validate each field against its field type, a length
    limit and the type of its column (`_public_form_checks`, and
    `processPublicForm` in the emitted middleware/fields.js).

The emitted middleware is executed with node (no packages needed) and the
emitted routers are executed in Express with a stand-in database (the
harness of test_generated_app_runtime.py) when web/node_modules is
installed. test_generated_site_publication_e2e.py does the same on a booted
site with Postgres. What a build shows to visitors in the first place is
covered next to the hostile-spec matrix in test_spec_hardening.py.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path

import pytest

from compose import build as B
from compose.build import BuildSpec, build_app, compose_files, resolve_spec
from compose.tests import _site_check as SC
from compose.tests import test_generated_app_runtime as runtime

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

needs_node = pytest.mark.skipif(runtime.NODE is None, reason="node is not on PATH")
needs_express = pytest.mark.skipif(
    runtime.NODE is None
    or not all((runtime.WEB_MODULES / name).exists() for name in ("express", "express-rate-limit", "bcrypt")),
    reason="needs node and web/node_modules with express, express-rate-limit and bcrypt (run `cd web && npm ci`)",
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "skip")


def _spec(fixture: str = "coach") -> dict:
    return json.loads((FIXTURES / f"sample-{fixture}-spec.json").read_text(encoding="utf-8"))


def _resolve(spec: dict):
    return resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))


def _files(spec: dict) -> dict:
    return dict(compose_files(_resolve(spec)))


def _post_handler(pages_js: str, path: str) -> str:
    start = pages_js.index(f"router.post('{path}'")
    return pages_js[start:pages_js.index("\n});", start)]


def _fields_of(handler: str) -> list:
    return json.loads(handler.split("const FIELDS = ", 1)[1].split(";\n", 1)[0])


QUOTE_FIELDS = [
    {"name": "author_name", "sql_type": "VARCHAR(80)", "required": True},
    {"name": "quote", "sql_type": "TEXT", "required": True},
]
PUBLISHED_TRUE = {"name": "published", "sql_type": "BOOLEAN", "default": "TRUE"}


def _testimonial(*extra) -> dict:
    return {"name": "Testimonial", "fields": copy.deepcopy(QUOTE_FIELDS) + [dict(f) for f in extra]}


def _story_form(**overrides) -> dict:
    page = {"path": "/share-your-story", "title": "Share your story", "form_target": "Testimonial",
            "allowed_fields": ["author_name", "quote"]}
    page.update(overrides)
    return page


# ---------------------------------------------------------------------------
# A visitor's row is stored unpublished
# ---------------------------------------------------------------------------

def test_a_public_form_stores_its_row_unpublished_whatever_the_column_default():
    """The coach home page shows testimonials. A public form that stores
    into Testimonial used to leave `published` to the column default, which
    this spec sets to TRUE: whatever a visitor typed was on the home page
    at once."""
    spec = _spec()
    spec["data_models"] = [_testimonial(PUBLISHED_TRUE)]
    spec["custom_pages"] = [_story_form()]
    files = _files(spec)
    assert "published BOOLEAN DEFAULT TRUE" in files["db/schema.sql"]
    handler = _post_handler(files["routes/pages.js"], "/share-your-story")
    assert ("'INSERT INTO testimonials (author_name, quote, published) VALUES ($1, $2, FALSE) RETURNING id'"
            in handler)
    # The home page leaves out exactly those rows.
    assert "FROM testimonials WHERE published = TRUE" in files["routes/pages.js"]
    # `published` is not a field of the form, so a visitor cannot send it.
    assert [f["name"] for f in _fields_of(handler)] == ["author_name", "quote"]
    assert "published" not in files["views/custom/share-your-story.ejs"]


@pytest.mark.parametrize("spelling", ["published", "Published", "PUBLISHED"])
def test_the_published_column_is_recognized_in_any_letter_case(spelling):
    """Postgres folds the unquoted column name, so "Published" IS the
    published column. It must not become a checkbox on the public form, the
    INSERT must set it, and the model's own listing must filter on it."""
    spec = _spec()
    spec["data_models"] = [{"name": "Story", "public_listing_path": "/stories", "fields":
                            copy.deepcopy(QUOTE_FIELDS) + [{"name": spelling, "sql_type": "BOOLEAN", "default": "TRUE"}]}]
    spec["custom_pages"] = [{"path": "/tell", "title": "Tell", "form_target": "Story",
                             "allowed_fields": ["author_name", "quote", spelling]}]
    files = _files(spec)
    handler = _post_handler(files["routes/pages.js"], "/tell")
    assert [f["name"] for f in _fields_of(handler)] == ["author_name", "quote"]
    assert f"'INSERT INTO stories (author_name, quote, {spelling}) VALUES ($1, $2, FALSE) RETURNING id'" in handler
    router = files["routes/stories.js"]
    assert f"const where = isAdmin ? '' : 'WHERE {spelling} = TRUE';" in router
    assert f"'SELECT * FROM stories WHERE id = $1' + (isAdmin ? '' : ' AND {spelling} = TRUE')" in router


@pytest.mark.parametrize("system_field", sorted(B._PUBLIC_FORM_SYSTEM_FIELDS))
def test_no_system_column_is_a_form_field_in_any_letter_case(system_field):
    for spelling in (system_field, system_field.upper(), system_field.title()):
        if spelling.lower() in ("id", "created_at"):
            # The gate refuses a field that differs from an engine column
            # only by case; the exact spelling is the engine's own column.
            spelling = system_field
        model = {"name": "Thing", "fields": [
            {"name": "title", "sql_type": "TEXT"}, {"name": spelling, "sql_type": "TEXT"}]}
        assert [f["name"] for f in B._public_form_fields({}, model)] == ["title"], spelling
        # An allowlist on the page cannot bring it back.
        assert [f["name"] for f in B._public_form_fields({"allowed_fields": ["title", spelling]}, model)] == ["title"]


def test_every_unauthenticated_insert_is_enumerated_and_stored_unpublished():
    """The three public write paths, on one build whose models all have a
    `published` column: custom form, contact form, newsletter signup."""
    spec = _spec("healer")
    spec["data_models"] = [
        _testimonial(PUBLISHED_TRUE),
        {"name": "ContactInquiry", "fields": [
            {"name": "email", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "message", "sql_type": "TEXT", "required": True}, dict(PUBLISHED_TRUE)]},
        {"name": "NewsletterSubscriber", "fields": [
            {"name": "email", "sql_type": "VARCHAR(255)", "required": True}, dict(PUBLISHED_TRUE)]},
    ]
    spec["custom_pages"] = [
        _story_form(),
        {"path": "/owner-notes", "title": "Notes", "form_target": "Testimonial", "auth_required": True},
    ]
    rs = _resolve(spec)
    targets = [(what, model["name"]) for what, model in B._public_insert_targets(rs)]
    assert targets == [
        ("the form on custom page '/share-your-story'", "Testimonial"),
        ("the contact form", "ContactInquiry"),
        ("the newsletter signup form", "NewsletterSubscriber"),
    ]
    pages = dict(compose_files(rs))["routes/pages.js"]
    inserts = [line.strip() for line in pages.splitlines() if "INSERT INTO" in line]
    assert inserts == [
        "'INSERT INTO contact_inquiries (email, message, published) VALUES ($1, $2, FALSE)',",
        "await db.query('INSERT INTO newsletter_subscribers (email, published) SELECT $1::text, FALSE WHERE NOT EXISTS "
        "(SELECT 1 FROM newsletter_subscribers WHERE LOWER(email::text) = LOWER($1::text))', [email]);",
        # the public form and the form behind the login (same rule for both)
        "'INSERT INTO testimonials (author_name, quote, published) VALUES ($1, $2, FALSE) RETURNING id',",
        "'INSERT INTO testimonials (author_name, quote, published) VALUES ($1, $2, FALSE) RETURNING id',",
    ]
    # Every route file a visitor can reach was just read in full: nothing
    # else in the build inserts into a model table without the admin login.
    files = dict(compose_files(rs))
    others = [path for path, body in files.items()
              if path.startswith("routes/") and isinstance(body, str) and "INSERT INTO" in body
              and path != "routes/pages.js"]
    model_routers = {f"routes/{B._slug_for(m)}.js" for m in rs.data_models}
    assert set(others) - model_routers == {"routes/auth.js", "routes/admin-settings.js"}
    for path in model_routers:
        create = files[path][files[path].index("router.post('/'"):]
        assert create.index("if (!req.baseUrl.startsWith('/admin/'))") < create.index("INSERT INTO")


# ---------------------------------------------------------------------------
# A model that cannot hold a row back
# ---------------------------------------------------------------------------

def _refused_specs():
    form_into_curated = _spec()
    form_into_curated["data_models"] = [_testimonial()]
    form_into_curated["custom_pages"] = [_story_form()]

    form_into_listed = _spec()
    form_into_listed["data_models"] = [dict(_testimonial(), name="Story", public_listing_path="/stories")]
    form_into_listed["custom_pages"] = [_story_form(form_target="Story")]

    contact_into_listed = _spec()
    contact_into_listed["data_models"] = [{"name": "ContactInquiry", "public_listing_path": "/guestbook", "fields": [
        {"name": "email", "sql_type": "VARCHAR(255)", "required": True},
        {"name": "message", "sql_type": "TEXT", "required": True}]}]

    newsletter_into_listed = _spec("healer")
    newsletter_into_listed["data_models"] = [{"name": "NewsletterSubscriber", "public_listing_path": "/readers",
                                              "fields": [{"name": "email", "sql_type": "VARCHAR(255)"}]}]
    return [
        ("custom form into a model the archetype shows", form_into_curated,
         "the form on custom page '/share-your-story'", "Testimonial", "the home page (testimonials-stack)"),
        ("custom form into a model the spec lists", form_into_listed,
         "the form on custom page '/share-your-story'", "Story", "its public listing at /stories"),
        ("contact form into a model the spec lists", contact_into_listed,
         "the contact form", "ContactInquiry", "its public listing at /guestbook"),
        ("newsletter signup into a model the spec lists", newsletter_into_listed,
         "the newsletter signup form", "NewsletterSubscriber", "its public listing at /readers"),
    ]


REFUSED_FORMS = _refused_specs()


@pytest.mark.parametrize("label,spec,what,model,where", REFUSED_FORMS, ids=[r[0] for r in REFUSED_FORMS])
def test_build_is_refused_when_a_visitors_row_would_be_public_at_once(label, spec, what, model, where, tmp_path):
    with pytest.raises(ValueError) as refused:
        _resolve(spec)
    message = str(refused.value)
    assert message.startswith(f"Spec validation failed: {what} lets a visitor add a row to the model {model!r}")
    assert where in message and "has no boolean `published` column" in message
    out = tmp_path / "app"
    with pytest.raises(ValueError, match="Spec validation failed"):
        build_app(copy.deepcopy(spec), output_dir=out)
    assert not out.exists()

    # The refusal is about that one missing column: with it the same spec
    # builds, and the form's INSERT stores the row unpublished.
    fixed = copy.deepcopy(spec)
    next(m for m in fixed["data_models"] if m["name"] == model)["fields"].append(dict(PUBLISHED_TRUE))
    pages = _files(fixed)["routes/pages.js"]
    table = B._table_name_for_model(next(m for m in fixed["data_models"] if m["name"] == model))
    inserts = [line for line in pages.splitlines() if f"INSERT INTO {table} " in line]
    assert inserts and all("FALSE" in line for line in inserts), inserts


def test_a_form_into_a_private_model_or_behind_the_login_is_not_refused():
    # A model nobody is shown needs no moderation column.
    spec = _spec()
    spec["data_models"] = [{"name": "Rsvp", "fields": copy.deepcopy(QUOTE_FIELDS)}]
    spec["custom_pages"] = [{"path": "/rsvp", "title": "RSVP", "form_target": "Rsvp",
                             "allowed_fields": ["author_name", "quote"]}]
    assert B._public_form_errors(_resolve(spec)) == []
    # A form only signed-in accounts can submit is not a visitor's form.
    spec = _spec()
    spec["data_models"] = [_testimonial()]
    spec["custom_pages"] = [_story_form(auth_required=True)]
    rs = _resolve(spec)
    assert B._public_form_errors(rs) == []
    assert B._public_insert_targets(rs)[0][0] == "the contact form"
    # Every shipped fixture builds: none pairs a public form with a public
    # model that lacks the column.
    for fixture in sorted(FIXTURES.glob("sample-*-spec.json")):
        spec = json.loads(fixture.read_text(encoding="utf-8"))
        assert B._public_form_errors(_resolve(spec)) == [], fixture.name


# ---------------------------------------------------------------------------
# Only a BOOLEAN column named `published` is the publication flag
# ---------------------------------------------------------------------------

NOT_A_FLAG = [
    ("a date", {"name": "published", "sql_type": "DATE"}),
    ("text", {"name": "published", "sql_type": "TEXT"}),
    ("no type at all, which is TEXT", {"name": "published"}),
    ("an array of booleans", {"name": "published", "sql_type": "BOOLEAN[]"}),
    ("a date, spelled Published", {"name": "Published", "sql_type": "DATE"}),
]


@pytest.mark.parametrize("label,column", NOT_A_FLAG, ids=[c[0] for c in NOT_A_FLAG])
def test_a_published_column_of_another_type_is_an_ordinary_column(label, column):
    """A model may call the date a book came out `published`. Every public
    read used to compare any column of that name with TRUE, which Postgres
    rejects for a DATE or TEXT column: the listing answered 500 on every
    request."""
    model = {"name": "Volume", "public_listing_path": "/volumes",
             "fields": copy.deepcopy(QUOTE_FIELDS) + [dict(column)]}
    spec = _spec()
    spec["data_models"] = [model]
    assert B._publication_column(model) is None
    files = _files(spec)
    assert "const where = isAdmin ? '' : '';" in files["routes/volumes.js"]
    compares_with_a_boolean = re.compile(r"\bpublished\s*(?:=|IS NOT)\s*(?:TRUE|FALSE)", re.I)
    assert not compares_with_a_boolean.search(files["routes/volumes.js"])
    for path, body in files.items():
        if path.startswith("routes/") and isinstance(body, str):
            for line in body.splitlines():
                assert not ("volumes" in line and compares_with_a_boolean.search(line)), (path, line)

    # Such a model cannot hold a visitor's row back, so a public form into
    # it is refused while its rows are shown ...
    spec["custom_pages"] = [{"path": "/tell", "title": "Tell", "form_target": "Volume",
                             "allowed_fields": ["author_name", "quote"]}]
    with pytest.raises(ValueError, match="Volume has no boolean `published` column"):
        _resolve(spec)
    # ... and stores the row without touching the column when they are not.
    del model["public_listing_path"]
    handler = _post_handler(_files(spec)["routes/pages.js"], "/tell")
    assert "'INSERT INTO volumes (author_name, quote) VALUES ($1, $2) RETURNING id'" in handler


def test_an_archetype_page_does_not_filter_on_a_published_column_that_is_not_boolean():
    spec = _spec()
    spec["data_models"] = [_testimonial({"name": "published", "sql_type": "DATE"})]
    pages = _files(spec)["routes/pages.js"]
    reads = [line for line in pages.splitlines() if "FROM testimonials" in line]
    assert reads and not any("published" in line for line in reads), reads
    spec["data_models"] = [_testimonial(PUBLISHED_TRUE)]
    assert "FROM testimonials WHERE published = TRUE" in _files(spec)["routes/pages.js"]


@pytest.mark.parametrize("column", [
    {"name": "published", "sql_type": "boolean"},
    {"name": "published", "sql_type": " Bool "},
    {"name": "published", "field_type": "boolean"},
])
def test_a_boolean_published_column_is_the_flag_however_its_type_is_written(column):
    model = {"name": "Volume", "public_listing_path": "/volumes",
             "fields": copy.deepcopy(QUOTE_FIELDS) + [dict(column)]}
    assert B._publication_column(model) == "published"
    spec = _spec()
    spec["data_models"] = [model]
    spec["custom_pages"] = [{"path": "/tell", "title": "Tell", "form_target": "Volume",
                             "allowed_fields": ["author_name", "quote"]}]
    files = _files(spec)
    assert "const where = isAdmin ? '' : 'WHERE published = TRUE';" in files["routes/volumes.js"]
    assert ("'INSERT INTO volumes (author_name, quote, published) VALUES ($1, $2, FALSE) RETURNING id'"
            in _post_handler(files["routes/pages.js"], "/tell"))


# ---------------------------------------------------------------------------
# What a submitted value is checked against
# ---------------------------------------------------------------------------

# What a visitor may fill in on the booking form: every column but the
# engine's own (`status` is one of them).
BOOKING_FIELDS = ["name", "email", "notes", "guests", "children", "budget", "ratio", "day", "start",
                  "arrives_at", "vegetarian", "room", "tags"]
BOOKING = {"name": "Booking", "fields": [
    {"name": "name", "sql_type": "VARCHAR(40)", "required": True},
    {"name": "email", "sql_type": "VARCHAR(255)", "required": True},
    {"name": "notes", "sql_type": "TEXT"},
    {"name": "guests", "sql_type": "INTEGER"},
    {"name": "children", "sql_type": "SMALLINT"},
    {"name": "budget", "sql_type": "NUMERIC(10,2)"},
    {"name": "ratio", "sql_type": "double precision"},
    {"name": "day", "sql_type": "DATE"},
    {"name": "start", "sql_type": "TIME"},
    {"name": "arrives_at", "sql_type": "TIMESTAMPTZ"},
    {"name": "vegetarian", "sql_type": "BOOLEAN"},
    {"name": "room", "field_type": "select", "options": ["Garden", {"value": "Hall", "label": "Great hall"}]},
    {"name": "tags", "sql_type": "TEXT[]"},
    {"name": "status", "sql_type": "VARCHAR(20)", "default": "pending"},
]}


def _booking_spec() -> dict:
    spec = _spec()
    spec["data_models"] = [copy.deepcopy(BOOKING)]
    spec["custom_pages"] = [{"path": "/book", "title": "Book", "form_target": "Booking",
                             "allowed_fields": BOOKING_FIELDS}]
    return spec


def test_checks_are_derived_from_the_models_columns():
    handler = _post_handler(_files(_booking_spec())["routes/pages.js"], "/book")
    checks = {f["name"]: f for f in _fields_of(handler)}
    assert list(checks) == ["name", "email", "notes", "guests", "children", "budget", "ratio", "day",
                            "start", "arrives_at", "vegetarian", "room", "tags"]      # no `status`
    summary = {name: (c["field_type"], c["column"], c["max_length"], c.get("int_bits")) for name, c in checks.items()}
    assert summary == {
        "name": ("text", "text", 40, None),                 # the column is narrower than the type
        "email": ("email", "text", 255, None),              # inferred from the name
        "notes": ("long_text", "text", 10000, None),
        "guests": ("integer", "integer", 10000, 32),
        "children": ("integer", "integer", 10000, 16),
        "budget": ("decimal", "number", 10000, None),
        "ratio": ("text", "number", 255, None),
        "day": ("date", "date", 10000, None),
        "start": ("time", "time", 10000, None),
        "arrives_at": ("datetime", "timestamp", 10000, None),
        "vegetarian": ("boolean", "boolean", 10000, None),
        "room": ("select", "text", 100, None),
        "tags": ("text", "text", 255, None),
    }
    assert checks["room"]["options"] == ["Garden", "Hall"]
    assert [name for name, c in checks.items() if c["required"]] == ["name", "email"]
    # The form the visitor sees is rendered from the same field list.
    view = _files(_booking_spec())["views/custom/book.ejs"]
    for name in checks:
        assert f'"name": "{name}"' in view or f"name: '{name}'" in view or f'"{name}"' in view
    assert '"status"' not in view


_NODE_DRIVER = r"""
const fields = require(process.argv[1]);
const FIELDS = JSON.parse(process.argv[2]);
const cases = JSON.parse(process.argv[3]);
const out = cases.map((body) => {
  const r = fields.processPublicForm(FIELDS, body);
  return { values: r.values, errors: r.errors, submitted: r.submitted };
});
process.stdout.write(JSON.stringify(out));
"""


def _run_checks(tmp_path, bodies: list) -> list:
    files = _files(_booking_spec())
    script = tmp_path / "fields.js"
    script.write_text(files["middleware/fields.js"], encoding="utf-8")
    checks = _fields_of(_post_handler(files["routes/pages.js"], "/book"))
    proc = subprocess.run(
        [runtime.NODE, "-e", _NODE_DRIVER, str(script), json.dumps(checks), json.dumps(bodies)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    return json.loads(proc.stdout)


GOOD = {"name": " Pat Doe ", "email": "Pat@Example.ORG", "notes": "See you there", "guests": "4",
        "children": "0", "budget": "120.50", "ratio": "0.3333", "day": "2031-02-28", "start": "18:30",
        "arrives_at": "2031-02-28T18:00", "vegetarian": "1", "room": "Hall"}

# (label, the one field changed, the value sent, the message expected)
BAD_VALUES = [
    ("longer than the column", "name", "x" * 41, "Name is too long (at most 40 characters)"),
    ("longer than any field may be", "notes", "x" * 10001, "Notes is too long (at most 10000 characters)"),
    ("required and missing", "name", "", "Name is required"),
    ("required and only spaces", "name", "   ", "Name is required"),
    ("not an email address", "email", "pat at example", "Email format is invalid"),
    ("integer with letters", "guests", "four", "Guests format is invalid"),
    ("integer past the column's range", "guests", "2147483648", "Guests is out of range"),
    ("smallint past the column's range", "children", "40000", "Children is out of range"),
    ("integer with far too many digits", "guests", "9" * 30, "Guests must be a whole number"),
    ("decimal with three places", "budget", "1.234", "Budget format is invalid"),
    ("number with an exponent", "ratio", "1e9999", "Ratio must be a number"),
    ("date that is not on the calendar", "day", "2031-02-30", "Day must be a date (YYYY-MM-DD)"),
    ("date in another format", "day", "28/02/2031", "Day format is invalid"),
    ("time past midnight", "start", "25:00", "Start must be a time (HH:MM)"),
    ("timestamp that is not one", "arrives_at", "soon", "Arrives At must be a date and time"),
    ("option that was not offered", "room", "Vault", "Room is not one of the choices"),
]


@needs_node
def test_a_valid_submission_passes_and_is_normalized(tmp_path):
    result = _run_checks(tmp_path, [GOOD, dict(GOOD, vegetarian=None, notes="")])[0:2]
    accepted, sparse = result
    assert accepted["errors"] == {}
    assert accepted["values"]["name"] == "Pat Doe" and accepted["values"]["email"] == "pat@example.org"
    assert accepted["values"]["vegetarian"] is True and accepted["values"]["guests"] == "4"
    assert sparse["errors"] == {} and sparse["values"]["notes"] is None


@needs_node
@pytest.mark.parametrize("label,field,value,message", BAD_VALUES, ids=[b[0] for b in BAD_VALUES])
def test_a_bad_value_is_rejected_with_a_message_for_that_field(label, field, value, message, tmp_path):
    result = _run_checks(tmp_path, [dict(GOOD, **{field: value})])[0]
    assert result["errors"] == {field: message}
    # What goes back to the form is what was typed, cut to the limit.
    limit = 40 if field == "name" else 10000
    assert result["submitted"][field] == value[:limit]
    assert {k: v for k, v in result["submitted"].items() if k not in (field, "tags")} == \
        {k: v for k, v in GOOD.items() if k != field}


@needs_node
def test_a_value_that_is_not_a_string_is_treated_as_missing(tmp_path):
    """With the extended urlencoded parser `name[]=x` arrives as an array
    and `name[a]=x` as an object."""
    array_value, object_value = _run_checks(tmp_path, [
        dict(GOOD, name=["a", "b"]), dict(GOOD, notes={"a": "b"}, name={"toString": "x"})])
    assert array_value["errors"] == {"name": "Name is required"} and array_value["submitted"]["name"] == ""
    assert object_value["errors"] == {"name": "Name is required"}
    assert object_value["values"].get("notes") is None and object_value["submitted"]["notes"] == ""


# ---------------------------------------------------------------------------
# Which columns a visitor may write
# ---------------------------------------------------------------------------

STORY_COLUMNS = [
    {"name": "title", "sql_type": "VARCHAR(160)", "required": True},
    {"name": "body", "sql_type": "TEXT"},
    {"name": "featured_on_home", "sql_type": "BOOLEAN", "default": False},
    {"name": "sort_order", "sql_type": "INTEGER"},
    {"name": "external_url", "sql_type": "VARCHAR(500)"},
    PUBLISHED_TRUE,
]


def _story_spec(**page) -> dict:
    spec = _spec()
    spec["data_models"] = [{"name": "Story", "fields": copy.deepcopy(STORY_COLUMNS)}]
    spec["custom_pages"] = [dict({"path": "/tell", "title": "Tell", "form_target": "Story"}, **page)]
    return spec


@pytest.mark.parametrize("page, fragment", [
    ({}, "the page names no allowed_fields"),
    ({"allowed_fields": []}, "the page names no allowed_fields"),
    ({"allowed_fields": ["id", "published", "created_at"]}, "lists allowed_fields that name no column of 'Story' a visitor may fill in"),
    ({"allowed_fields": ["no_such_column"]}, "lists allowed_fields that name no column of 'Story' a visitor may fill in"),
])
def test_a_public_form_page_that_does_not_name_its_columns_is_refused(page, fragment):
    """A model is declared for what the owner enters. Without a list the
    form took every column that is not the engine's own: the flag that puts
    a row on the home page, the sort order, a link the owner would publish
    without reading. The spec has to say which columns a stranger may set."""
    spec = _story_spec(**page)
    errors = B.validate_spec(BuildSpec.from_dict(copy.deepcopy(spec)))
    assert len(errors) == 1 and "the form on custom page '/tell'" in errors[0] and fragment in errors[0], errors
    with pytest.raises(ValueError, match="Spec validation failed: the form on custom page '/tell'"):
        _resolve(spec)


def test_a_public_form_page_takes_the_columns_it_names_and_no_others():
    files = _files(_story_spec(allowed_fields=["title", "body", "featured_on_home", "sort_order", "published"]))
    handler = _post_handler(files["routes/pages.js"], "/tell")
    # Named, but the engine's own `published` is never offered; the owner's
    # own columns are offered because the owner's spec named them.
    assert [f["name"] for f in _fields_of(handler)] == ["title", "body", "featured_on_home", "sort_order"]
    files = _files(_story_spec(allowed_fields=["title", "body"]))
    handler = _post_handler(files["routes/pages.js"], "/tell")
    assert [f["name"] for f in _fields_of(handler)] == ["title", "body"]
    assert "'INSERT INTO stories (title, body, published) VALUES ($1, $2, FALSE) RETURNING id'" in handler
    for hidden in ("featured_on_home", "sort_order", "external_url"):
        assert hidden not in files["views/custom/tell.ejs"], hidden


def test_a_form_page_behind_a_login_keeps_every_column_by_default_and_the_contact_form_is_unchanged():
    spec = _story_spec(auth_required=True)
    assert B._public_form_errors(_resolve(spec)) == []
    handler = _post_handler(_files(spec)["routes/pages.js"], "/tell")
    assert [f["name"] for f in _fields_of(handler)] == ["title", "body", "featured_on_home", "sort_order", "external_url"]
    # The contact form's model holds what visitors send: its columns are the form.
    contact = _post_handler(_files(_spec())["routes/pages.js"], "/contact")
    assert _fields_of(contact), "the built-in contact form still has its fields"


# ---------------------------------------------------------------------------
# One definition of "published"
# ---------------------------------------------------------------------------

def test_every_public_read_of_a_row_applies_the_same_condition():
    """A row is published when its flag is TRUE: FALSE and NULL are not. The
    model's own routes used to say `= TRUE` and the composed pages `IS NOT
    FALSE`, so a row with no value was on the home page and not on its own
    listing."""
    spec = _spec()
    spec["data_models"] = [
        {"name": "BlogPost", "plural": "blog-posts", "fields": [
            {"name": "title", "sql_type": "VARCHAR(255)"}, {"name": "slug", "sql_type": "VARCHAR(255)"},
            {"name": "published", "sql_type": "BOOLEAN"}]},
        {"name": "Testimonial", "belongs_to": "BlogPost", "public_listing_path": "/kind-words", "fields": [
            {"name": "author_name", "sql_type": "VARCHAR(255)"}, {"name": "published", "sql_type": "BOOLEAN"}]},
    ]
    files = _files(spec)
    reads = []
    for path, body in files.items():
        if path.startswith("routes/") and isinstance(body, str):
            assert "IS NOT FALSE" not in body, path
            reads += [line for line in body.splitlines() if "published" in line and "SELECT" in line]
    joined = "\n".join(reads)
    # the composed listing, the composed detail page, the rows related to a record,
    assert "FROM blog_posts WHERE published = TRUE ORDER BY id ASC" in joined
    assert "WHERE (slug::text = $1 OR id::text = $1) AND published = TRUE LIMIT 1" in joined
    assert "FROM testimonials WHERE blog_post_id = $1 AND published = TRUE ORDER BY id ASC" in joined
    # and the model's own listing and detail.
    router = files["routes/testimonials.js"]
    assert "const where = isAdmin ? '' : 'WHERE published = TRUE';" in router
    assert "'SELECT * FROM testimonials WHERE id = $1' + (isAdmin ? '' : ' AND published = TRUE')" in router


@pytest.mark.parametrize("column", [
    {"name": "published", "sql_type": "bool"}, {"name": "published", "sql_type": "BOOL", "default": "TRUE"},
    {"name": "Published", "sql_type": " Boolean "}, {"name": "published", "field_type": "boolean"},
])
def test_a_boolean_published_column_is_a_checkbox_in_the_admin_however_its_type_is_written(column):
    """The flag for the public reads was a text box in the admin form when it
    was typed `bool`: the box was never ticked, and the form saved FALSE."""
    spec = _spec()
    spec["data_models"] = [{"name": "Story", "public_listing_path": "/stories",
                            "fields": copy.deepcopy(QUOTE_FIELDS) + [dict(column)]}]
    files = _files(spec)
    name = column["name"]
    assert json.loads(files["routes/stories.js"].split("const BOOLEAN_COLS = new Set(", 1)[1].split(");", 1)[0]) == [name]
    form = files["views/stories/new.ejs"]
    field = next(f for f in json.loads(form.split("var fieldsTemplate = ", 1)[1].split(";", 1)[0]) if f["name"] == name)
    assert field["type"] == "checkbox", field


def test_a_seed_row_that_does_not_say_is_published_and_a_serial_column_fills_itself_in():
    spec = _spec()
    spec["data_models"] = [{"name": "Story", "public_listing_path": "/stories", "fields": [
        {"name": "title", "sql_type": "VARCHAR(120)"}, {"name": "ordinal", "sql_type": "SERIAL"},
        {"name": "published", "sql_type": "BOOLEAN"}]}]
    spec["seed_content"] = dict(spec.get("seed_content") or {}, stories=[
        {"title": "Said nothing"}, {"title": "Said null", "published": None},
        {"title": "Said no", "published": False}, {"title": "Said yes", "published": True}])
    seed = _files(spec)["db/seed.sql"]
    rows = [line for line in seed.splitlines() if line.startswith("INSERT INTO stories ")]
    assert [line.split("VALUES ", 1)[1] for line in rows] == [
        "('Said nothing', DEFAULT, TRUE);", "('Said null', DEFAULT, TRUE);",
        "('Said no', DEFAULT, FALSE);", "('Said yes', DEFAULT, TRUE);"]


@pytest.mark.skipif(not SC.AVAILABLE, reason=SC.UNAVAILABLE_REASON)
def test_a_row_whose_flag_has_no_value_is_not_public_on_postgres(tmp_path):
    """The two kinds of public read, on a real database, against three rows
    each: published, unpublished and NULL. The composed detail page
    (/blog/<slug>) and the model's own (/stories/<id>) show the first and
    not the other two, for a visitor; the admin's own route shows all three."""
    pytest.importorskip("psycopg2")
    import psycopg2
    from compose.tests._scratch_pg import scratch_database_url

    spec = _spec()
    spec["data_models"] = [
        {"name": "BlogPost", "plural": "blog-posts", "fields": [
            {"name": "title", "sql_type": "VARCHAR(255)"}, {"name": "slug", "sql_type": "VARCHAR(255)"},
            {"name": "published", "sql_type": "BOOLEAN"}]},
        {"name": "Story", "public_listing_path": "/stories", "fields": [
            {"name": "title", "sql_type": "VARCHAR(255)"}, {"name": "published", "sql_type": "BOOLEAN"}]},
    ]
    spec["seed_content"] = dict(spec.get("seed_content") or {}, blog_posts=[
        {"title": "Placeholder", "slug": "placeholder", "published": True}], stories=[
        {"title": "Placeholder", "published": True}])
    site = build_app(copy.deepcopy(spec), output_dir=tmp_path / "site")
    with scratch_database_url() as url:
        SC.apply_site_sql(site, url)
        conn = psycopg2.connect(url)
        conn.autocommit = True
        story_ids = {}
        with conn.cursor() as cur:
            for label, flag in (("yes", True), ("no", False), ("null", None)):
                cur.execute("INSERT INTO blog_posts (title, slug, published) VALUES (%s, %s, %s)",
                            (label, label + "-post", flag))
                cur.execute("INSERT INTO stories (title, published) VALUES (%s, %s) RETURNING id", (label, flag))
                story_ids[label] = cur.fetchone()[0]
        conn.close()
        paths = [f"/blog/{label}-post" for label in ("yes", "no", "null")]
        paths += [f"/stories/{story_ids[label]}" for label in ("yes", "no", "null")]
        paths += [f"/admin/stories/{story_ids[label]}" for label in ("yes", "no", "null")]
        result = SC.check_sites([{"id": "site", "dir": site, "database_url": url, "extra_paths": paths}],
                                tmp_path / "run")["site"]
    assert SC.problems_of(result) == []
    seen = result["extra"]
    for who in ("visitor", "admin"):
        assert seen[f"{who} /blog/yes-post"] == 200
        assert seen[f"{who} /blog/no-post"] == 404 and seen[f"{who} /blog/null-post"] == 404
    assert seen[f"visitor /stories/{story_ids['yes']}"] == 200
    assert seen[f"visitor /stories/{story_ids['no']}"] == 404 and seen[f"visitor /stories/{story_ids['null']}"] == 404
    for label in ("yes", "no", "null"):
        assert seen[f"admin /admin/stories/{story_ids[label]}"] == 200


# ---------------------------------------------------------------------------
# The emitted POST handler, executed
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def story_app(tmp_path_factory):
    import os
    os.environ["CAS_LINT_EXTERNAL_URLS"] = "skip"
    spec = _spec()
    spec["data_models"] = [_testimonial(PUBLISHED_TRUE), copy.deepcopy(BOOKING)]
    spec["custom_pages"] = [_story_form(), {"path": "/book", "title": "Book", "form_target": "Booking",
                                            "allowed_fields": BOOKING_FIELDS}]
    out = tmp_path_factory.mktemp("publication") / "app"
    build_app(spec, output_dir=out)
    (out / "db.js").write_text(runtime._DB_STUB, encoding="utf-8")
    return out


@needs_express
def test_form_post_validates_then_inserts_unpublished(story_app):
    good = {"_csrf": "t0ken", "author_name": "Pat", "quote": "Wonderful."}
    stored, too_long, missing, honeypot, bad_booking, no_csrf = runtime._run(story_app, "pages", [
        {"method": "POST", "path": "/share-your-story", "session": runtime.ANON, "form": good},
        {"method": "POST", "path": "/share-your-story", "session": runtime.ANON,
         "form": dict(good, author_name="x" * 81)},
        {"method": "POST", "path": "/share-your-story", "session": runtime.ANON, "form": dict(good, quote="")},
        {"method": "POST", "path": "/share-your-story", "session": runtime.ANON,
         "form": dict(good, website_url_hp="http://spam.example")},
        {"method": "POST", "path": "/book", "session": runtime.ANON,
         "form": {"_csrf": "t0ken", "name": "Pat", "email": "pat@example.org", "guests": "lots", "day": "2031-13-01"}},
        {"method": "POST", "path": "/share-your-story", "session": runtime.ANON, "form": dict(good, _csrf="wrong")},
    ])
    assert (stored["status"], stored["location"]) == (302, "/share-your-story")
    assert runtime._inserts(stored) == [
        "INSERT INTO testimonials (author_name, quote, published) VALUES ($1, $2, FALSE) RETURNING id"]
    assert stored["session"].get("flash_error") is None and stored["session"]["flash_success"]

    assert (too_long["status"], too_long["location"]) == (302, "/share-your-story")
    assert runtime._inserts(too_long) == []
    assert too_long["session"]["flash_error"] == "Author Name is too long (at most 80 characters)"
    # The form comes back with what was typed, cut to the limit.
    assert too_long["session"]["_form_submitted_share_your_story"] == {"author_name": "x" * 80, "quote": "Wonderful."}

    assert runtime._inserts(missing) == [] and missing["session"]["flash_error"] == "Quote is required"
    assert runtime._inserts(honeypot) == [] and honeypot["session"].get("flash_error") is None

    assert runtime._inserts(bad_booking) == []
    assert bad_booking["session"]["flash_error"].split(" " + chr(0x00B7) + " ") == [
        "Guests format is invalid", "Day must be a date (YYYY-MM-DD)"]

    assert no_csrf["status"] == 403 and no_csrf["sql"] == []
