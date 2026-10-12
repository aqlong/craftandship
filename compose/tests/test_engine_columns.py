"""The columns the engine itself adds to a table or reads with a fixed meaning.

The engine adds `id` and `created_at` to every model table. It orders a
listing by `published_at`, compares `created_at` with `NOW() - INTERVAL '7
days'` on the admin dashboard, looks a row up by `slug`, fills a
`belongs_to` link into `<parent>_id`, and reads `published` as the flag that
hides a row. A spec is JSON that a model wrote, so it can declare any of
those names with any column type, and the old engine built such a site and
let Postgres refuse the statement when the page was requested (`created_at`
typed VARCHAR: every statement of the admin dashboard failed, `GET /admin`
answered 500).

The rule (`_ENGINE_COLUMN_KINDS`, `_engine_column_errors`, and the cast in the
slug lookup in compose/build.py): a declaration the engine cannot work with is
refused, with a message that names the column and the types it takes; a
declaration the engine can work with is handled (a declared `id` is the
engine's own and is left out of the model; a `slug` of any type is compared
as text).

Two layers. The first needs no database and pins the gate cell by cell
(every column name x every column type the gate accepts). The second builds
sites, applies their `db/schema.sql` and `db/seed.sql` to a real PostgreSQL
and requests every GET route as a visitor and as the admin with the emitted
routers (see `_site_harness.js`, `database_url`): it is what proves that what
the gate lets through works, and, with the gate taken out of the way, that
what it refuses does not. They skip without node, `web/node_modules` or a
local PostgreSQL (`_scratch_pg.py` says which one and how to name a database).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from compose import build as B
from compose.build import BuildSpec, build_app, compose_files, resolve_spec, validate_spec
from compose.tests import _site_check as SC

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# One representative of every kind of column type the gate accepts.
COLUMN_TYPES = [
    "TEXT", "VARCHAR(40)", "CHAR(4)", "INTEGER", "BIGINT", "SMALLINT", "SERIAL", "NUMERIC(10,2)", "REAL",
    "BOOLEAN", "DATE", "TIME", "TIMESTAMP", "TIMESTAMPTZ", "JSON", "JSONB", "UUID", "TEXT[]",
]
# What each column accepts, written out here and not read from the engine.
ID_TYPES = {"INTEGER", "BIGINT", "SMALLINT", "SERIAL"}
CREATED_AT_TYPES = {"DATE", "TIMESTAMP", "TIMESTAMPTZ"}
PUBLISHED_AT_REFUSED = {"JSON", "JSONB"}
FK_TYPES = {"INTEGER", "BIGINT", "SMALLINT"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "skip")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")


def _coach() -> dict:
    return json.loads((FIXTURES / "sample-coach-spec.json").read_text(encoding="utf-8"))


def _errors(spec: dict) -> list:
    return validate_spec(BuildSpec.from_dict(copy.deepcopy(spec)))


def _resolve(spec: dict):
    return resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))


def _widget_spec(column: str, sql_type: str) -> dict:
    """The coach site with a public Widget model whose fields are a title,
    a flag and one column named `column`."""
    spec = _coach()
    fields = [{"name": "title", "sql_type": "VARCHAR(160)", "required": True},
              {"name": "published", "sql_type": "BOOLEAN", "default": True}]
    fields = [f for f in fields if f["name"] != column] + [{"name": column, "sql_type": sql_type}]
    spec["data_models"] = [{"name": "Widget", "plural": "widgets", "public_listing_path": "/widgets",
                            "fields": fields}]
    return spec


# ---------------------------------------------------------------------------
# The gate, cell by cell
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql_type", COLUMN_TYPES)
def test_created_at_is_a_date_or_a_timestamp_and_nothing_else(sql_type):
    errors = _errors(_widget_spec("created_at", sql_type))
    if sql_type in CREATED_AT_TYPES:
        assert errors == []
    else:
        assert len(errors) == 1, errors
        assert errors[0].startswith("data_model 'Widget' field 'created_at' has the type ")
        assert "date, timestamp or timestamptz" in errors[0]
        with pytest.raises(ValueError, match="field 'created_at' has the type"):
            _resolve(_widget_spec("created_at", sql_type))


@pytest.mark.parametrize("sql_type", COLUMN_TYPES)
def test_a_declared_id_must_agree_with_the_primary_key_the_engine_adds(sql_type):
    errors = _errors(_widget_spec("id", sql_type))
    if sql_type in ID_TYPES:
        assert errors == []
    else:
        assert len(errors) == 1 and "field 'id' has the type" in errors[0], errors


@pytest.mark.parametrize("sql_type", COLUMN_TYPES)
def test_published_at_orders_a_listing_so_it_may_not_be_json(sql_type):
    errors = _errors(_widget_spec("published_at", sql_type))
    if sql_type in PUBLISHED_AT_REFUSED:
        assert len(errors) == 1 and "field 'published_at' has the type" in errors[0], errors
        assert "any type but json" in errors[0]
    else:
        assert errors == []


def test_a_field_with_no_type_of_its_own_is_checked_by_the_type_its_field_type_gives_it():
    spec = _coach()
    spec["data_models"] = [{"name": "Widget", "fields": [
        {"name": "title", "sql_type": "TEXT"}, {"name": "created_at", "field_type": "text"}]}]
    assert "field 'created_at' has the type 'VARCHAR(255)'" in _errors(spec)[0]
    spec["data_models"][0]["fields"][1] = {"name": "created_at", "field_type": "datetime"}
    assert _errors(spec) == []
    spec["data_models"][0]["fields"][1] = {"name": "created_at"}
    assert "field 'created_at' has the type 'TEXT'" in _errors(spec)[0]


def test_a_declared_id_is_the_engines_own_and_is_not_a_column_of_the_model():
    spec = _widget_spec("id", "INTEGER")
    spec["seed_content"] = {"widgets": [{"title": "First widget"}, {"title": "Second widget"}]}
    rs = _resolve(spec)
    widget = next(m for m in rs.data_models if m["name"] == "Widget")
    assert [f["name"] for f in widget["fields"]] == ["title", "published"]
    files = dict(compose_files(rs))
    table = files["db/schema.sql"].split("CREATE TABLE IF NOT EXISTS widgets (", 1)[1].split(");", 1)[0]
    assert table.count("id SERIAL PRIMARY KEY") == 1 and "id INTEGER" not in table
    # Not a column an admin form offers, an INSERT names or a seed row writes.
    assert json.loads(files["routes/widgets.js"].split("const ALLOWED_COLS = ", 1)[1].split(";\n", 1)[0]) == [
        "title", "published"]
    assert "INSERT INTO widgets (title, published) VALUES" in files["db/seed.sql"]
    assert "(id," not in files["db/seed.sql"]
    # The caller's spec still has the field it wrote.
    assert any(f["name"] == "id" for f in spec["data_models"][0]["fields"])


# ---------------------------------------------------------------------------
# Links between models
# ---------------------------------------------------------------------------

def _linked_spec(column: str, sql_type: str, belongs_to_field=None) -> dict:
    """A Testimonial that belongs to the coach site's BlogPost through the
    column `column`, declared with `sql_type`."""
    spec = _coach()
    spec["data_models"] = [
        {"name": "BlogPost", "plural": "blog-posts", "fields": [
            {"name": "title", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "slug", "sql_type": "VARCHAR(255)"},
            {"name": "published", "sql_type": "BOOLEAN", "default": True}]},
        {"name": "Testimonial", "plural": "testimonials", "belongs_to": "BlogPost", "fields": [
            {"name": "author_name", "sql_type": "VARCHAR(255)"}, {"name": "quote", "sql_type": "TEXT"},
            {"name": "published", "sql_type": "BOOLEAN", "default": True},
            {"name": column, "sql_type": sql_type}]},
    ]
    if belongs_to_field:
        spec["data_models"][1]["belongs_to_field"] = belongs_to_field
    return spec


@pytest.mark.parametrize("sql_type", COLUMN_TYPES)
def test_the_column_that_links_a_child_to_its_parent_holds_the_parents_id(sql_type):
    errors = _errors(_linked_spec("blog_post_id", sql_type))
    if sql_type in FK_TYPES:
        assert errors == []
    else:
        assert len(errors) == 1, errors
        assert "data_model 'Testimonial' declares 'blog_post_id', the column that holds the id of a 'BlogPost' row" in errors[0]
        assert "whole-number type" in errors[0]


@pytest.mark.parametrize("column", ["id", "created_at", "ID", "Created_At"])
def test_a_link_cannot_be_named_like_a_column_the_engine_adds_itself(column):
    spec = _linked_spec("note", "TEXT", belongs_to_field=column)
    spec["data_models"][1]["fields"] = [f for f in spec["data_models"][1]["fields"] if f["name"] != "note"]
    errors = _errors(spec)
    assert errors and "which the engine adds to every table itself" in errors[0], errors


def test_a_ring_of_models_that_belong_to_each_other_is_refused():
    spec = _coach()
    spec["data_models"] = [
        {"name": "Alpha", "belongs_to": "Gamma", "fields": [{"name": "title", "sql_type": "TEXT"}]},
        {"name": "Beta", "belongs_to": "Alpha", "fields": [{"name": "title", "sql_type": "TEXT"}]},
        {"name": "Gamma", "belongs_to": "Beta", "fields": [{"name": "title", "sql_type": "TEXT"}]},
    ]
    errors = _errors(spec)
    assert errors == ["data_models refer to each other in a ring through belongs_to (Alpha -> Gamma -> Beta -> Alpha); "
                      "a table cannot point to one that is created after it"], errors
    with pytest.raises(ValueError, match="in a ring through belongs_to"):
        _resolve(spec)
    # A model that belongs to itself is not a ring: the table exists in its own definition.
    spec["data_models"] = [{"name": "Category", "belongs_to": "Category", "fields": [{"name": "title", "sql_type": "TEXT"}]}]
    assert _errors(spec) == []


def test_a_child_declared_before_its_parent_is_created_after_it():
    """A foreign key REFERENCES a table that has to exist already: the
    schema (and the seed, and the dashboard's delete-all) follow the links,
    not the order the spec lists the models in."""
    spec = _coach()
    spec["data_models"] = [
        {"name": "Chapter", "belongs_to": "Volume", "fields": [{"name": "title", "sql_type": "TEXT"}]},
        {"name": "Volume", "belongs_to": "Shelf", "fields": [{"name": "title", "sql_type": "TEXT"}]},
        {"name": "Shelf", "plural": "shelves", "fields": [{"name": "title", "sql_type": "TEXT"}]},
    ]
    spec["seed_content"] = dict(spec.get("seed_content") or {}, **{
        "shelves": [{"title": "Top shelf"}],
        "volumes": [{"title": "Volume one", "shelf_id": 1}],
        "chapters": [{"title": "Chapter one", "volume_id": 1}],
    })
    files = dict(compose_files(_resolve(spec)))
    schema, seed = files["db/schema.sql"], files["db/seed.sql"]
    created = [schema.index(f"CREATE TABLE IF NOT EXISTS {t} (") for t in ("shelves", "volumes", "chapters")]
    assert created == sorted(created), "a parent is created before the table that references it"
    seeded = [seed.index(f"INSERT INTO {t} ") for t in ("shelves", "volumes", "chapters")]
    assert seeded == sorted(seeded), "a parent's rows are inserted before its children's"
    cleared = files["routes/admin-dashboard.js"].split("const CLEARABLE_TABLES = ", 1)[1].split(";\n", 1)[0]
    order = [t for t in json.loads(cleared) if t in ("shelves", "volumes", "chapters")]
    assert order == ["chapters", "volumes", "shelves"], "children are deleted before their parents"
    # The models themselves keep the order the spec gave them (it is the order of the admin's menu).
    assert [m["name"] for m in _resolve(spec).data_models if m["name"] in ("Chapter", "Volume", "Shelf")] == [
        "Chapter", "Volume", "Shelf"]


def test_the_newsletter_email_column_holds_text():
    spec = _coach()
    spec["data_models"] = [{"name": "NewsletterSubscriber", "fields": [
        {"name": "email", "sql_type": "INTEGER"}, {"name": "name", "sql_type": "TEXT"}]}]
    healer = json.loads((FIXTURES / "sample-healer-spec.json").read_text(encoding="utf-8"))
    healer["data_models"] = spec["data_models"]
    errors = _errors(healer)
    assert errors and "stores the address a visitor signs up with, as text" in errors[0], errors
    healer["data_models"][0]["fields"][0]["sql_type"] = "VARCHAR(255)"
    assert _errors(healer) == []


# ---------------------------------------------------------------------------
# On a real PostgreSQL
# ---------------------------------------------------------------------------

pytestmark_db = pytest.mark.skipif(not SC.AVAILABLE, reason=SC.UNAVAILABLE_REASON)


@pytest.fixture(scope="module")
def database_url():
    pytest.importorskip("psycopg2")
    from compose.tests._scratch_pg import scratch_database_url
    with scratch_database_url() as url:
        yield url


def _build(spec: dict, root: Path, name: str) -> Path:
    return build_app(copy.deepcopy(spec), output_dir=root / name)


def _requests(site: Path, url: str, extra: list, root: Path, name: str) -> dict:
    """Apply the site's schema and seed to the database and request every GET
    route (and `extra`) as a visitor and as the admin."""
    SC.apply_site_sql(site, url)
    return SC.check_sites([{"id": name, "dir": site, "database_url": url, "extra_paths": extra}], root / ("run-" + name))[name]


@pytestmark_db
@pytest.mark.parametrize("sql_type", sorted(CREATED_AT_TYPES))
def test_a_dashboard_over_a_date_or_timestamp_created_at_answers(sql_type, database_url, tmp_path):
    spec = _widget_spec("created_at", sql_type)
    spec["seed_content"] = {"widgets": [{"title": "First widget"}]}
    site = _build(spec, tmp_path, "site")
    result = _requests(site, database_url, ["/admin", "/widgets", "/admin/widgets"], tmp_path, "site")
    assert SC.problems_of(result) == []
    assert result["extra"]["admin /admin"] == 200
    assert result["extra"]["visitor /widgets"] == 200 and result["extra"]["admin /admin/widgets"] == 200


@pytestmark_db
def test_the_dashboard_really_fails_on_postgres_when_created_at_is_text(database_url, tmp_path, monkeypatch):
    """Why the rule exists, on the real thing: with the gate taken out of the
    way (`created_at` allowed to be text), the site builds, its schema
    applies, and the admin dashboard's count of the last seven days is a
    statement Postgres rejects. The test above is the same site with a
    timestamp, which answers."""
    allowed = dict(B._ENGINE_COLUMN_KINDS)
    allowed["created_at"] = (("text", "date", "timestamp"),) + allowed["created_at"][1:]
    monkeypatch.setattr(B, "_ENGINE_COLUMN_KINDS", allowed)
    spec = _widget_spec("created_at", "VARCHAR(40)")
    assert _errors(spec) == []
    site = _build(spec, tmp_path, "text-site")
    result = _requests(site, database_url, ["/admin"], tmp_path, "text-site")
    assert result["extra"]["admin /admin"] == 500
    problems = SC.problems_of(result)
    assert any("operator does not exist: character varying > timestamp with time zone" in p for p in problems), problems


@pytestmark_db
@pytest.mark.parametrize("sql_type", sorted(ID_TYPES))
def test_a_declared_id_does_not_break_the_seed_on_postgres(sql_type, database_url, tmp_path):
    """The seed wrote an explicit NULL into every declared column that a
    row did not give, `id` included: "null value in column id violates
    not-null constraint", and the site never booted."""
    spec = _widget_spec("id", sql_type)
    spec["seed_content"] = {"widgets": [{"title": "First widget"}, {"title": "Second widget"}]}
    site = _build(spec, tmp_path, "site")
    result = _requests(site, database_url, ["/widgets/1", "/widgets/2", "/admin/widgets/1"], tmp_path, "site")
    assert SC.problems_of(result) == []
    assert result["extra"]["visitor /widgets/2"] == 200


@pytestmark_db
@pytest.mark.parametrize("sql_type", ["TEXT", "VARCHAR(40)", "INTEGER", "BIGINT", "NUMERIC(10,2)", "BOOLEAN", "DATE",
                                      "TIMESTAMPTZ", "UUID", "JSONB"])
def test_a_page_detail_lookup_by_slug_works_whatever_type_the_slug_has(sql_type, database_url, tmp_path):
    """The lookup compared the column with a text parameter: for an integer,
    a date or a uuid slug, "operator does not exist: text = integer"."""
    spec = _coach()
    values = {"TEXT": "first-post", "VARCHAR(40)": "first-post", "INTEGER": 7, "BIGINT": 7, "NUMERIC(10,2)": 7.5,
              "BOOLEAN": True, "DATE": "2030-01-15", "TIMESTAMPTZ": "2030-01-15T17:30:00Z",
              "UUID": "00000000-0000-4000-8000-000000000001", "JSONB": ["a"]}
    spec["data_models"] = [{"name": "BlogPost", "plural": "blog-posts", "fields": [
        {"name": "title", "sql_type": "VARCHAR(255)", "required": True}, {"name": "slug", "sql_type": sql_type},
        {"name": "body", "sql_type": "TEXT"}, {"name": "published", "sql_type": "BOOLEAN", "default": True}]}]
    spec["seed_content"] = dict(spec["seed_content"], blog_posts=[
        {"title": "First post", "slug": values[sql_type], "body": "Body.", "published": True}])
    site = _build(spec, tmp_path, "site")
    path = "/blog/1"
    result = _requests(site, database_url, [path], tmp_path, "site")
    assert SC.problems_of(result) == []
    assert result["extra"]["visitor " + path] == 200, result["extra"]


@pytestmark_db
@pytest.mark.parametrize("sql_type", ["INTEGER", "BIGINT", "SMALLINT"])
def test_rows_related_to_a_record_are_read_on_postgres(sql_type, database_url, tmp_path):
    spec = _linked_spec("blog_post_id", sql_type)
    spec["seed_content"] = dict(spec["seed_content"], blog_posts=[
        {"title": "First post", "slug": "first-post", "published": True}], testimonials=[
        {"author_name": "A. Client", "quote": "Good.", "published": True, "blog_post_id": 1},
        {"author_name": "B. Client", "quote": "Hidden.", "published": False, "blog_post_id": 1}])
    site = _build(spec, tmp_path, "site")
    result = _requests(site, database_url, ["/blog/first-post", "/blog/1"], tmp_path, "site")
    assert SC.problems_of(result) == []
    assert result["extra"]["visitor /blog/first-post"] == 200


@pytestmark_db
def test_a_site_whose_child_model_is_declared_before_its_parent_applies_on_postgres(database_url, tmp_path):
    """The foreign key of the child REFERENCES a table that was created
    after it ("relation volumes does not exist") and the site never booted;
    its seed rows then had the same order problem."""
    spec = _coach()
    spec["data_models"] = [
        {"name": "Chapter", "belongs_to": "Volume", "public_listing_path": "/chapters", "fields": [
            {"name": "title", "sql_type": "VARCHAR(255)"}, {"name": "published", "sql_type": "BOOLEAN"}]},
        {"name": "Volume", "belongs_to": "Shelf", "public_listing_path": "/volumes", "fields": [
            {"name": "title", "sql_type": "VARCHAR(255)"}, {"name": "published", "sql_type": "BOOLEAN"}]},
        {"name": "Shelf", "plural": "shelves", "public_listing_path": "/shelves", "fields": [
            {"name": "title", "sql_type": "VARCHAR(255)"}, {"name": "published", "sql_type": "BOOLEAN"}]},
    ]
    spec["seed_content"] = dict(spec["seed_content"], shelves=[{"title": "Top shelf"}],
                                volumes=[{"title": "Volume one", "shelf_id": 1}],
                                chapters=[{"title": "Chapter one", "volume_id": 1}])
    site = _build(spec, tmp_path, "site")
    result = _requests(site, database_url, ["/chapters/1", "/admin/chapters/1/edit"], tmp_path, "site")
    assert SC.problems_of(result) == []
    assert result["extra"]["visitor /chapters/1"] == 200 and result["extra"]["admin /admin/chapters/1/edit"] == 200
