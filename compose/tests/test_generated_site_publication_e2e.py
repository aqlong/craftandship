"""What a booted generated site shows and stores for a visitor who is not
signed in.

One site is composed from the healer sample plus models and pages chosen to
sit on every seam between "a model exists" and "its rows are public", then
started with `node server.js` against a throwaway PostgreSQL database.
Every assertion is made over HTTP on loopback or by reading that database.

  - A model the spec did not publish cannot be read by a visitor: not at a
    page that has the model's name, not below it, not through the sitemap,
    and not on any other public page of the site. The rows are there (the
    signed-in admin reads them), so the test is not vacuous.
  - A row a visitor creates through a public form is not shown anywhere
    until the admin publishes it, on a page of the archetype and on a
    listing the spec asked for alike.
  - A public form rejects values that do not fit the model, says why on the
    form, keeps what was typed, and stores nothing.
  - The newsletter signup stores an address once.

Budget: the generated app allows 20 public form posts per 15 minutes per
address; this module makes 15.

Same prerequisites as test_compose_end_to_end.py (node, a local PostgreSQL,
builds/sample-healer/node_modules); the module skips without them.
"""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path

import pytest

from compose.build import build_app
from compose.tests.test_compose_end_to_end import boot_generated_app
from compose.tests.test_generated_site_security_e2e import Client, csrf_in, sign_in

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
OWNER = "owner@site-p.example"

# Values that exist only in rows no visitor may read. None of them may
# appear in any response to a visitor.
PRIVATE_VALUES = (
    "Quillon Marsh", "quillon@clients.example", "+1 555 0100", "Prefers evening calls about the Hartwell account",
    "Ostara Venn", "ostara@clients.example",
    "Lead from the trade fair", "lead@leads.example",
    "Unreleased draft story",
    "Row of a child model", "child@registrations.example",
)


def _site_spec() -> dict:
    spec = json.loads((FIXTURES / "sample-healer-spec.json").read_text(encoding="utf-8"))
    spec["admin_email"] = OWNER
    spec["data_models"] = [
        # The reviewer's case: a private model and an ordinary page that
        # happens to have its name.
        {"name": "Client", "fields": [
            {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "email", "sql_type": "VARCHAR(255)"},
            {"name": "phone", "sql_type": "VARCHAR(50)"},
            {"name": "description", "sql_type": "TEXT"},
        ]},
        {"name": "Lead", "fields": [
            {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "email", "sql_type": "VARCHAR(255)"},
        ]},
        # Shown by the archetype's home page (testimonials-stack).
        {"name": "Testimonial", "fields": [
            {"name": "author_name", "sql_type": "VARCHAR(80)", "required": True},
            {"name": "quote", "sql_type": "TEXT", "required": True},
            {"name": "published", "sql_type": "BOOLEAN", "default": "TRUE"},
        ]},
        # Listed because the spec says so.
        {"name": "Story", "public_listing_path": "/stories", "fields": [
            {"name": "title", "sql_type": "VARCHAR(120)", "required": True},
            {"name": "description", "sql_type": "TEXT"},
            {"name": "published", "sql_type": "BOOLEAN", "default": "TRUE"},
        ]},
        # Listed, with a column that is called `published` and is a date:
        # an ordinary column, not the flag that hides a row.
        {"name": "Volume", "public_listing_path": "/volumes", "fields": [
            {"name": "title", "sql_type": "VARCHAR(120)", "required": True},
            {"name": "published", "sql_type": "DATE"},
        ]},
        # A private model with one column of each kind a form can carry.
        {"name": "Booking", "fields": [
            {"name": "name", "sql_type": "VARCHAR(40)", "required": True},
            {"name": "email", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "guests", "sql_type": "INTEGER"},
            {"name": "day", "sql_type": "DATE"},
            {"name": "notes", "sql_type": "TEXT"},
        ]},
        # Belongs to a model the archetype shows (the events page has
        # public detail pages); private itself.
        {"name": "Registration", "belongs_to": "Event", "fields": [
            {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "email", "sql_type": "VARCHAR(255)"},
        ]},
    ]
    seed = dict(spec.get("seed_content") or {})
    seed["clients"] = [
        {"name": "Quillon Marsh", "email": "quillon@clients.example", "phone": "+1 555 0100",
         "description": "Prefers evening calls about the Hartwell account"},
        {"name": "Ostara Venn", "email": "ostara@clients.example", "phone": None, "description": None},
    ]
    seed["leads"] = [{"name": "Lead from the trade fair", "email": "lead@leads.example"}]
    seed["testimonials"] = [{"author_name": "Published Author", "quote": "A quote the owner published.",
                             "published": True}]
    seed["stories"] = [
        {"title": "Published story", "description": "Out in the open.", "published": True},
        {"title": "Unreleased draft story", "description": "Not yet.", "published": False},
    ]
    seed["volumes"] = [
        {"title": "Volume with a date", "published": "2031-02-03"},
        {"title": "Volume without one", "published": None},
    ]
    seed["registrations"] = [{"name": "Row of a child model", "email": "child@registrations.example"}]
    spec["seed_content"] = seed
    spec["custom_pages"] = [
        {"path": "/clients", "title": "Who we work with", "purpose": "A few words about the people we serve."},
        {"path": "/leads", "title": "Leads"},
        {"path": "/share-your-story", "title": "Share your story", "form_target": "Testimonial",
         "allowed_fields": ["author_name", "quote"]},
        {"path": "/tell", "title": "Tell us", "form_target": "Story",
         "allowed_fields": ["title", "description"]},
        {"path": "/book", "title": "Book", "form_target": "Booking",
         "allowed_fields": ["name", "email", "guests", "day", "notes"]},
    ]
    return spec


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    os.environ.setdefault("CAS_LINT_EXTERNAL_URLS", "skip")
    out = tmp_path_factory.mktemp("publication-e2e") / "app"
    build_app(_site_spec(), output_dir=out)
    with boot_generated_app(out, admin_email=OWNER) as running:
        yield running


@pytest.fixture(scope="module")
def admin(site):
    client, consumed, _raw = sign_in(site, OWNER)
    assert (consumed.status, consumed.location) == (302, "/admin")
    return client


def _sitemap_paths(client) -> list:
    reply = client.get("/sitemap.xml")
    assert reply.status == 200
    return [re.sub(r"^https?://[^/]+", "", loc) or "/" for loc in re.findall(r"<loc>([^<]+)</loc>", reply.text)]


def _flash(page: str) -> str:
    found = re.search(r'role="alert">([^<]*)<', page)
    return html.unescape(found.group(1)) if found else ""


# ---------------------------------------------------------------------------
# A model the spec did not publish
# ---------------------------------------------------------------------------

def test_the_private_rows_exist_and_the_admin_can_read_them(site, admin):
    assert site["query"]("SELECT count(*) FROM clients")[0][0] == 2
    assert site["query"]("SELECT count(*) FROM leads")[0][0] == 1
    assert site["query"]("SELECT count(*) FROM registrations")[0][0] == 1
    listing = admin.get("/admin/clients")
    assert listing.status == 200 and "Quillon Marsh" in listing.text and "Ostara Venn" in listing.text
    detail = admin.get("/admin/clients/1")
    assert detail.status == 200 and "quillon@clients.example" in html.unescape(detail.text)


def test_a_page_named_like_a_model_is_the_page_and_shows_no_rows(site):
    visitor = Client(site["port"])
    page = visitor.get("/clients")
    assert page.status == 200
    assert "Who we work with" in page.text and "A few words about the people we serve." in page.text
    for value in PRIVATE_VALUES:
        assert value not in html.unescape(page.text), value
    # Other spellings of the same path, and the listing's query parameters.
    for path in ("/Clients/", "/clients?page=1&per_page=100", "/clients?sort=email&order=desc"):
        reply = visitor.get(path)
        assert reply.status == 200, path
        assert "Quillon Marsh" not in reply.text and "Ostara Venn" not in reply.text, path


@pytest.mark.parametrize("path", [
    "/clients/1", "/clients/2", "/Clients/1/", "/clients/1/edit", "/clients/new", "/client", "/client/1",
    "/leads", "/leads/1", "/registrations", "/registrations/1", "/bookings", "/bookings/1",
    "/stories/2",            # the unpublished story, by id
    "/testimonials/1",       # shown on the home page; no detail route of its own
])
def test_no_route_below_or_beside_a_private_model_serves_a_row(site, path):
    visitor = Client(site["port"])
    reply = visitor.get(path)
    assert reply.status == 404, f"{path} answered {reply.status}"
    for value in PRIVATE_VALUES:
        assert value not in html.unescape(reply.text), (path, value)


def test_a_visitor_cannot_write_through_a_private_models_router(site):
    visitor = Client(site["port"])
    token = visitor.csrf()
    before = site["query"]("SELECT count(*) FROM clients")[0][0]
    for path in ("/clients", "/clients/1/update", "/clients/1/delete", "/leads", "/admin/clients"):
        reply = visitor.post(path, {"_csrf": token, "name": "Planted", "email": "planted@clients.example"})
        assert reply.status in (302, 404), (path, reply.status)
        if reply.status == 302:
            assert reply.location == "/login", (path, reply.location)
    assert site["query"]("SELECT count(*) FROM clients")[0][0] == before
    assert site["query"]("SELECT count(*) FROM clients WHERE name = 'Planted'")[0][0] == 0


def test_no_public_page_of_the_site_contains_a_private_value(site):
    """Every path the sitemap lists, the home page and every link in its
    navigation, fetched as a visitor."""
    visitor = Client(site["port"])
    paths = set(_sitemap_paths(visitor)) | {"/"}
    home = visitor.get("/")
    assert home.status == 200
    paths |= {href for href in re.findall(r'href="(/[^"#?]*)"', home.text) if not href.startswith("/admin")}
    assert {"/clients", "/stories", "/journal", "/events", "/share-your-story"} <= paths
    assert not [p for p in paths if p.startswith(("/leads", "/registrations", "/bookings", "/clients/"))]
    fetched = 0
    for path in sorted(paths):
        reply = visitor.get(path)
        if reply.status != 200:
            continue
        fetched += 1
        text = html.unescape(reply.text)
        for value in PRIVATE_VALUES:
            assert value not in text, (path, value)
    assert fetched >= 10
    # An event's public detail page does not show who registered for it.
    # (That the page does not even read those rows is pinned on the emitted
    # source: test_related_rows_are_read_only_for_a_child_that_is_public_itself
    # in test_spec_hardening.py.)
    event_links = re.findall(r'href="(/events/[^"]+)"', visitor.get("/events").text)
    assert event_links
    site["query"]("UPDATE registrations SET event_id = (SELECT min(id) FROM events)")
    for link in sorted(set(event_links)):
        detail = visitor.get(link)
        assert detail.status == 200 and "Row of a child model" not in detail.text, link


def test_what_the_spec_and_the_archetype_publish_is_still_served(site):
    visitor = Client(site["port"])
    stories = visitor.get("/stories")
    assert stories.status == 200 and "Published story" in stories.text
    assert "Unreleased draft story" not in stories.text
    assert visitor.get("/stories/1").status == 200
    home = visitor.get("/")
    assert "A quote the owner published." in html.unescape(home.text)
    assert visitor.get("/journal").status == 200 and visitor.get("/events").status == 200


def test_a_date_column_named_published_does_not_break_or_filter_the_listing(site):
    """Only a BOOLEAN `published` column is the publication flag. Compared
    with TRUE, a DATE column is a type error in Postgres and the listing
    would answer 500 on every request."""
    assert site["query"]("SELECT data_type FROM information_schema.columns "
                         "WHERE table_name = 'volumes' AND column_name = 'published'")[0][0] == "date"
    visitor = Client(site["port"])
    listing = visitor.get("/volumes")
    assert listing.status == 200
    assert "Volume with a date" in listing.text and "Volume without one" in listing.text
    assert visitor.get("/volumes/1").status == 200
    assert visitor.get("/volumes/2").status == 200


# ---------------------------------------------------------------------------
# A row a visitor creates
# ---------------------------------------------------------------------------

def test_a_visitors_testimonial_is_not_on_the_site_until_the_admin_publishes_it(site, admin):
    visitor = Client(site["port"])
    quote = "Submitted by a visitor a moment ago"
    sent = visitor.post("/share-your-story", {
        "_csrf": visitor.csrf("/share-your-story"), "author_name": "Passing Visitor", "quote": quote})
    assert (sent.status, sent.location) == (302, "/share-your-story")
    rows = site["query"]("SELECT id, published FROM testimonials WHERE quote = %s", (quote,))
    assert len(rows) == 1 and rows[0][1] is False, "a visitor's row must be stored unpublished"
    row_id = rows[0][0]

    other = Client(site["port"])
    assert quote not in html.unescape(other.get("/").text)

    # The owner sees it in the admin and publishes it there.
    assert quote in html.unescape(admin.get(f"/admin/testimonials/{row_id}").text)
    token = csrf_in(admin.get(f"/admin/testimonials/{row_id}/edit").text)
    saved = admin.post(f"/admin/testimonials/{row_id}/update", {
        "_csrf": token, "author_name": "Passing Visitor", "quote": quote, "published": "on"})
    assert (saved.status, saved.location) == (302, "/admin/testimonials")
    assert site["query"]("SELECT published FROM testimonials WHERE id = %s", (row_id,))[0][0] is True
    assert quote in html.unescape(other.get("/").text)


def test_a_visitors_row_is_not_on_a_listing_the_spec_asked_for_either(site, admin):
    visitor = Client(site["port"])
    title = "Story sent in through the form"
    sent = visitor.post("/tell", {"_csrf": visitor.csrf("/tell"), "title": title, "description": "Mine."})
    assert (sent.status, sent.location) == (302, "/tell")
    rows = site["query"]("SELECT id, published FROM stories WHERE title = %s", (title,))
    assert len(rows) == 1 and rows[0][1] is False
    row_id = rows[0][0]
    other = Client(site["port"])
    assert title not in other.get("/stories").text
    assert other.get(f"/stories/{row_id}").status == 404
    # A visitor cannot publish it by sending the column along.
    forced = visitor.post("/tell", {"_csrf": visitor.csrf("/tell"), "title": "Forced story",
                                    "published": "on", "Published": "true", "id": "1"})
    assert forced.status == 302
    assert site["query"]("SELECT published FROM stories WHERE title = 'Forced story'") == [(False,)]
    assert "Forced story" not in other.get("/stories").text

    token = csrf_in(admin.get(f"/admin/stories/{row_id}/edit").text)
    saved = admin.post(f"/admin/stories/{row_id}/update", {
        "_csrf": token, "title": title, "description": "Mine.", "published": "on"})
    assert saved.status == 302
    assert title in other.get("/stories").text
    assert other.get(f"/stories/{row_id}").status == 200


# ---------------------------------------------------------------------------
# What a public form accepts
# ---------------------------------------------------------------------------

GOOD_BOOKING = {"name": "Pat Doe", "email": "pat@visitors.example", "guests": "3", "day": "2031-05-17",
                "notes": "Window seat, please."}


@pytest.mark.parametrize("label,changes,message", [
    ("longer than its column", {"name": "N" * 41}, "Name is too long (at most 40 characters)"),
    ("not an email address", {"email": "pat at visitors"}, "Email format is invalid"),
    ("integer past the column's range", {"guests": "99999999999"}, "Guests is out of range"),
    ("a date that is not on the calendar", {"day": "2031-02-31"}, "Day must be a date (YYYY-MM-DD)"),
    ("two bad fields and a missing required one", {"name": "", "guests": "many"},
     "Name is required · Guests format is invalid"),
])
def test_a_public_form_rejects_a_value_that_does_not_fit_the_model(site, label, changes, message):
    visitor = Client(site["port"])
    before = site["query"]("SELECT count(*) FROM bookings")[0][0]
    form = dict(GOOD_BOOKING, **changes)
    sent = visitor.post("/book", dict(form, _csrf=visitor.csrf("/book")))
    assert (sent.status, sent.location) == (302, "/book")
    assert site["query"]("SELECT count(*) FROM bookings")[0][0] == before, "nothing may be stored"
    back = visitor.get("/book")
    assert _flash(back.text) == message
    # The form comes back with what was typed (an oversized value cut to
    # the limit), so the visitor corrects it instead of starting over.
    assert 'value="pat@visitors.example"' in back.text or "email" in changes
    assert ">Window seat, please.</textarea>" in back.text
    if "name" in changes and changes["name"]:
        assert f'value="{"N" * 40}"' in back.text and "N" * 41 not in back.text


def test_a_public_form_stores_a_valid_submission_normalized(site):
    visitor = Client(site["port"])
    sent = visitor.post("/book", dict(GOOD_BOOKING, name="  Pat Doe  ", email="Pat@Visitors.Example",
                                      _csrf=visitor.csrf("/book")))
    assert (sent.status, sent.location) == (302, "/book")
    assert "Thanks for reaching out" in _flash(visitor.get("/book").text)
    rows = site["query"]("SELECT name, email, guests, day::text, notes FROM bookings WHERE email = %s",
                         ("pat@visitors.example",))
    assert rows == [("Pat Doe", "pat@visitors.example", 3, "2031-05-17", "Window seat, please.")]


def test_the_contact_form_still_stores_a_message_and_rejects_an_oversized_one(site):
    visitor = Client(site["port"])
    token = visitor.csrf("/contact")
    before = site["query"]("SELECT count(*) FROM contact_inquiries")[0][0]
    rejected = visitor.post("/contact", {"_csrf": token, "name": "Pat", "email": "pat@visitors.example",
                                         "message": "m" * 10001})
    assert rejected.status == 200 and "Message is too long (at most 10000 characters)" in html.unescape(rejected.text)
    assert site["query"]("SELECT count(*) FROM contact_inquiries")[0][0] == before
    stored = visitor.post("/contact", {"_csrf": token, "name": "Pat", "email": "pat@visitors.example",
                                       "message": "Hello from the contact form"})
    assert stored.status == 200
    assert site["query"]("SELECT count(*) FROM contact_inquiries WHERE message = 'Hello from the contact form'") == [(1,)]


def test_the_newsletter_signup_stores_an_address_once(site):
    """The subscriber table has no unique constraint on the address, so the
    handler's `ON CONFLICT DO NOTHING` used to conflict with nothing and
    every repeat added a row."""
    visitor = Client(site["port"])
    token = visitor.csrf()
    for address in ("reader@visitors.example", "reader@visitors.example", "Reader@Visitors.Example "):
        reply = visitor.post("/newsletter-signup", {"_csrf": token, "email": address})
        assert (reply.status, reply.location) == (302, "/#newsletter-success")
    assert site["query"]("SELECT email FROM newsletter_subscribers WHERE lower(email) = 'reader@visitors.example'") \
        == [("reader@visitors.example",)]
    other = visitor.post("/newsletter-signup", {"_csrf": token, "email": "second@visitors.example"})
    assert other.status == 302
    assert site["query"]("SELECT count(*) FROM newsletter_subscribers WHERE email LIKE '%%@visitors.example'") == [(2,)]
