"""The headline security properties of a generated site, on a booted site.

Two sites are composed and started with `node server.js` against throwaway
PostgreSQL databases, and every assertion is made over HTTP on loopback or
by reading the database and the server's own log:

  site A (development mode, no mail key): a build with a contact model that
    is not the default shape, a dated model that is not called Event, an
    Event model without an `event_date` column, pages for one role, for two
    roles and for "user", and a form page on a private model's route.
  site B (production mode, a mail key, outgoing fetch replaced by a
    recorder so nothing leaves the machine): the stock healer sample.

What is exercised: no login token reaches the log (and the one case where
a link is logged on purpose), role guards with real sessions, admin routes
refusing a signed-in non-admin, CSRF on public and admin writes, a login
link being single use and expiring, the session cookie flags, session
fixation, unpublished rows staying private, uploads restricted to the
admin and to raster images, the contact form and the listings following
their models, the per-address login limit, and the CSP nonce.

Budget to keep in mind before adding requests: the generated app allows
10 POSTs per 15 minutes per address across /login, /magic/consume and
/set-password (site A spends 7 of them; the last test of site B spends all
of that site's), 5 login-link requests per hour, and 20 public form posts
per 15 minutes.

Same prerequisites as test_compose_end_to_end.py (node, a local
PostgreSQL, builds/sample-healer/node_modules); the module skips without
them.
"""

from __future__ import annotations

import datetime
import hashlib
import http.client
import json
import os
import re
import secrets
import urllib.parse
from pathlib import Path

import pytest

from compose.build import build_app
from compose.tests.test_compose_end_to_end import boot_generated_app

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
ADMIN = "owner@site-a.example"
MEMBER = "member@site-a.example"
EDITOR = "editor@site-a.example"
PLAIN_USER = "user@site-a.example"
HEALER_ADMIN = "admin@sample-healer.example"
TOKEN_RE = re.compile(r"[0-9a-f]{64}")


# ---------------------------------------------------------------------------
# A small HTTP client: explicit cookies, no redirects followed
# ---------------------------------------------------------------------------

class Reply:
    def __init__(self, status, text, headers, set_cookies):
        self.status = status
        self.text = text
        self.headers = headers
        self.location = headers.get("Location")
        self.set_cookies = set_cookies


class Client:
    """One browser. Cookies are kept by name and sent back on every request
    whatever their flags, so the flags themselves can be asserted on.
    `https_proxy=True` adds the header the one trusted proxy would add for a
    request that reached it over TLS."""

    def __init__(self, port: int, https_proxy: bool = False):
        self.port = port
        self.https_proxy = https_proxy
        self.cookies: dict = {}

    def request(self, method: str, path: str, body: bytes = None, headers: dict = None) -> Reply:
        sent = dict(headers or {})
        if self.cookies:
            sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if self.https_proxy:
            sent["X-Forwarded-Proto"] = "https"
        conn = http.client.HTTPConnection("localhost", self.port, timeout=15)
        try:
            conn.request(method, path, body=body, headers=sent)
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", "replace")
            set_cookies = resp.headers.get_all("Set-Cookie") or []
            for line in set_cookies:
                name, _, rest = line.partition("=")
                self.cookies[name.strip()] = rest.split(";", 1)[0]
            return Reply(resp.status, text, resp.headers, set_cookies)
        finally:
            conn.close()

    def get(self, path: str, headers: dict = None) -> Reply:
        return self.request("GET", path, headers=headers)

    def head(self, path: str) -> Reply:
        return self.request("HEAD", path)

    def post(self, path: str, form: dict, headers: dict = None) -> Reply:
        body = urllib.parse.urlencode(form).encode("utf-8")
        sent = {"Content-Type": "application/x-www-form-urlencoded"}
        sent.update(headers or {})
        return self.request("POST", path, body=body, headers=sent)

    def upload(self, path: str, filename: str, content: bytes, mimetype: str, headers: dict = None) -> Reply:
        boundary = "----casTestBoundary" + secrets.token_hex(8)
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mimetype}\r\n\r\n"
        ).encode("ascii") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        sent = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        sent.update(headers or {})
        return self.request("POST", path, body=body, headers=sent)

    def csrf(self, path: str = "/login") -> str:
        """The CSRF token of this browser's session, read from a form."""
        reply = self.get(path)
        assert reply.status == 200, f"{path} answered {reply.status}"
        return csrf_in(reply.text)


def csrf_in(page: str) -> str:
    found = re.search(r'name="_csrf" value="([0-9a-f]{16,})"', page)
    assert found, "no CSRF input in the page"
    return found.group(1)


def issue_token(site, email: str, lifetime: str = "1 hour") -> str:
    """Store a login token the way the generated app stores one (the
    SHA-256 of a random value) and return the raw value."""
    raw = secrets.token_hex(32)
    site["query"](
        "INSERT INTO magic_link_tokens (user_id, token_hash, expires_at) "
        "SELECT id, %s, NOW() + %s::interval FROM users WHERE email = %s",
        (hashlib.sha256(raw.encode("ascii")).hexdigest(), lifetime, email),
    )
    return raw


def sign_in(site, email: str, raw: str = None):
    """Walk the two-step login-link flow in a new browser. Returns
    (client, the reply to the consuming POST, the raw token)."""
    client = Client(site["port"])
    raw = raw or issue_token(site, email)
    confirm = client.get("/magic?token=" + raw)
    assert confirm.status == 200, f"/magic answered {confirm.status}"
    consumed = client.post("/magic/consume", {"_csrf": csrf_in(confirm.text), "token": raw})
    return client, consumed, raw


def token_state(site, raw: str):
    rows = site["query"](
        "SELECT used_at IS NOT NULL, expires_at > NOW() FROM magic_link_tokens WHERE token_hash = %s",
        (hashlib.sha256(raw.encode("ascii")).hexdigest(),),
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Site A
# ---------------------------------------------------------------------------

def _site_a_spec() -> dict:
    spec = json.loads((FIXTURES / "sample-coach-spec.json").read_text(encoding="utf-8"))
    soon = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    spec["admin_email"] = ADMIN
    spec["data_models"] = [
        {"name": "ContactInquiry", "fields": [
            {"name": "full_name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "email", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "company", "sql_type": "VARCHAR(255)"},
            {"name": "message", "field_type": "long_text", "required": True},
        ]},
        {"name": "Workshop", "public_listing_path": "/workshops", "fields": [
            {"name": "title", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "starts_on", "sql_type": "DATE"},
            {"name": "published", "sql_type": "BOOLEAN", "default": "TRUE"},
        ]},
        {"name": "Event", "plural": "happenings", "fields": [
            {"name": "title", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "held_on", "sql_type": "DATE"},
        ]},
        {"name": "Rsvp", "fields": [
            {"name": "name", "sql_type": "VARCHAR(255)", "required": True},
            {"name": "email", "sql_type": "VARCHAR(255)", "required": True},
        ]},
        # Mounted at /services, a path the coach site's own page has.
        {"name": "Service", "fields": [{"name": "title", "sql_type": "VARCHAR(255)", "required": True}]},
        {"name": "Resource", "fields": [{"name": "title", "sql_type": "VARCHAR(255)", "required": True}]},
    ]
    spec["seed_content"] = dict(spec.get("seed_content") or {})
    spec["seed_content"]["workshops"] = [
        {"title": "Late workshop", "starts_on": "2031-03-01", "published": True},
        {"title": "Early workshop", "starts_on": "2031-01-01", "published": True},
        {"title": "Middle workshop", "starts_on": "2031-02-01", "published": True},
        {"title": "Draft workshop", "starts_on": "2030-12-01", "published": False},
    ]
    spec["seed_content"]["happenings"] = [{"title": "Open day", "held_on": soon}]
    spec["custom_pages"] = [
        {"path": "/members", "title": "Members area", "auth_required": True, "allowed_roles": ["member"]},
        {"path": "/staff", "title": "Staff room", "auth_required": True, "allowed_roles": ["admin", "editor"]},
        {"path": "/anyone", "title": "Signed in", "auth_required": True, "allowed_roles": ["user"]},
        {"path": "/rsvps", "title": "RSVP", "form_target": "Rsvp", "allowed_fields": ["name", "email"]},
        # Protected pages at paths that sit next to a model's or a page's
        # own routes without being one of them.
        {"path": "/resources", "title": "Resource room", "auth_required": True, "allowed_roles": ["admin"]},
        {"path": "/resources/a/b", "title": "Deep room", "auth_required": True, "allowed_roles": ["admin"]},
        {"path": "/services/pricing", "title": "Price room", "auth_required": True, "allowed_roles": ["admin"]},
        {"path": "/Admin/Reports/", "title": "Report room", "auth_required": True, "allowed_roles": ["admin"]},
    ]
    return spec


@pytest.fixture(scope="module")
def site_a(tmp_path_factory):
    os.environ.setdefault("CAS_LINT_EXTERNAL_URLS", "skip")
    out = tmp_path_factory.mktemp("security-a") / "app"
    build_app(_site_a_spec(), output_dir=out)
    with boot_generated_app(out, admin_email=ADMIN) as running:
        running["query"](
            "INSERT INTO users (email, password, password_set, role, email_verified) VALUES "
            "(%s, NULL, TRUE, 'member', TRUE), (%s, NULL, TRUE, 'editor', TRUE), (%s, NULL, TRUE, 'user', TRUE)",
            (MEMBER, EDITOR, PLAIN_USER),
        )
        yield running


@pytest.fixture(scope="module")
def admin_session(site_a):
    """The admin, signed in once by login link. Keeps the raw token and the
    session cookie values from before and after the sign-in."""
    client = Client(site_a["port"])
    raw = issue_token(site_a, ADMIN)
    confirm = client.get("/magic?token=" + raw)
    assert confirm.status == 200
    cookie_before = client.cookies["connect.sid"]
    consumed = client.post("/magic/consume", {"_csrf": csrf_in(confirm.text), "token": raw})
    assert (consumed.status, consumed.location) == (302, "/admin")
    return {"client": client, "token": raw, "cookie_before": cookie_before,
            "cookie_after": client.cookies["connect.sid"]}


@pytest.fixture(scope="module")
def role_sessions(site_a, admin_session):
    sessions = {"admin": admin_session["client"], "anonymous": Client(site_a["port"])}
    landings = {}
    for label, email in (("member", MEMBER), ("editor", EDITOR), ("user", PLAIN_USER)):
        sessions[label], consumed, _raw = sign_in(site_a, email)
        landings[label] = (consumed.status, consumed.location)
    return {"sessions": sessions, "landings": landings}


# ---- the log ----

def test_no_login_token_reaches_the_log_and_opening_a_link_does_not_spend_it(site_a):
    """`GET /magic?token=<raw>` renders the confirmation page without
    consuming the token, so a logged request line would be a working login
    for whoever reads the log. Every way the token can be put in front of
    the server is tried, then the whole log is searched for it."""
    client = Client(site_a["port"])
    raw = issue_token(site_a, ADMIN)
    link = "/magic?token=" + raw

    assert client.get(link).status == 200
    assert client.head(link).status == 200
    assert client.get("/Magic/?token=" + raw).status == 200          # same route to Express
    assert client.get("/magic?%74oken=" + raw).status == 200         # same parameter, encoded
    # A later request that names the link as its referrer.
    assert client.get("/style.css", headers={"Referer": f"http://localhost:{site_a['port']}{link}"}).status == 200
    # A consuming POST whose body the parser rejects (it attaches the raw
    # body to the error it raises).
    rejected = client.request("POST", "/magic/consume", body=('{"token":"' + raw + '", oops').encode("ascii"),
                              headers={"Content-Type": "application/json"})
    assert rejected.status == 400
    too_many = ("&".join(f"p{i}=1" for i in range(1100)) + "&token=" + raw).encode("ascii")
    rejected = client.request("POST", "/magic/consume", body=too_many,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert rejected.status == 413
    # A CSRF failure on a URL that carries the token.
    assert client.post("/magic/consume?token=" + raw, {"_csrf": "0" * 64, "token": raw}).status == 403

    log = site_a["log"]()
    assert raw not in log, [line for line in log.splitlines() if raw in line][:3]
    # The requests are in the log, without their query strings.
    assert '"GET /magic HTTP/1.1" 200' in log
    assert '"HEAD /magic HTTP/1.1" 200' in log
    assert '"GET /Magic/ HTTP/1.1" 200' in log
    assert f'"http://localhost:{site_a["port"]}/magic" ' in log
    assert "[request] body rejected (entity.parse.failed, 400): POST /magic/consume" in log
    assert "[request] body rejected (parameters.too.many, 413): POST /magic/consume" in log
    assert re.search(r'\[csrf-mismatch\] \{"method":"POST","url":"/magic/consume",', log)
    # No query string at all is logged, whatever the route.
    assert not re.search(r'"(?:GET|HEAD|POST) [^" ]*\?', log)

    # And none of that spent the token.
    assert token_state(site_a, raw) == (False, True)
    assert client.head(link).status == 200


def test_a_requested_login_link_is_logged_only_because_no_mail_key_is_set(site_a):
    """The one place the app writes a login link to its log, on purpose:
    with no RESEND_API_KEY nothing can be emailed and the log is the
    owner's only way in. (Site B has a mail key; see the test there.)"""
    visitor = Client(site_a["port"])
    before = site_a["log"]()
    reply = visitor.post("/request-magic-link",
                         {"_csrf": visitor.csrf("/request-magic-link"), "email": ADMIN.upper()})
    assert (reply.status, reply.location) == (302, "/magic-link-sent")
    added = site_a["log"]()[len(before):]
    lines = [line for line in added.splitlines() if "token=" in line]
    assert len(lines) == 1, lines
    assert re.fullmatch(
        r"\[magic-link\] issued for user \d+ \(no RESEND_API_KEY, link logged for operator recovery\): "
        r"/magic\?token=[0-9a-f]{64}", lines[0])
    logged = TOKEN_RE.search(lines[0]).group(0)
    assert token_state(site_a, logged) == (False, True), "the logged link is the real, unused one"
    # An address that has no account gets the same answer and no link.
    before = site_a["log"]()
    reply = visitor.post("/request-magic-link",
                         {"_csrf": visitor.csrf("/request-magic-link"), "email": "nobody@site-a.example"})
    assert (reply.status, reply.location) == (302, "/magic-link-sent")
    assert "token=" not in site_a["log"]()[len(before):]


# ---- login links ----

def test_a_login_link_signs_in_once(site_a, admin_session):
    raw = admin_session["token"]
    assert token_state(site_a, raw) == (True, True)
    replay = Client(site_a["port"])
    opened = replay.get("/magic?token=" + raw)
    assert (opened.status, opened.location) == (302, "/login?error=token-already-used")
    assert replay.head("/magic?token=" + raw).status == 410
    consumed = replay.post("/magic/consume", {"_csrf": replay.csrf(), "token": raw})
    assert (consumed.status, consumed.location) == (302, "/login?error=token-already-used")
    refused = replay.get("/admin")
    assert (refused.status, refused.location) == (302, "/login")
    shown = replay.get("/login?error=token-already-used")
    assert "already been used" in shown.text


def test_a_login_link_expires(site_a):
    raw = issue_token(site_a, ADMIN, lifetime="-1 minute")
    assert token_state(site_a, raw) == (False, False)
    client = Client(site_a["port"])
    opened = client.get("/magic?token=" + raw)
    assert (opened.status, opened.location) == (302, "/login?error=token-expired")
    assert client.head("/magic?token=" + raw).status == 410
    consumed = client.post("/magic/consume", {"_csrf": client.csrf(), "token": raw})
    assert (consumed.status, consumed.location) == (302, "/login?error=token-expired")
    assert client.get("/admin").location == "/login"
    assert token_state(site_a, raw) == (False, False), "an expired token is not marked used either"

    unknown = client.get("/magic?token=" + secrets.token_hex(32))
    assert (unknown.status, unknown.location) == (302, "/login?error=invalid-token")


def test_signing_in_replaces_the_session_id(admin_session):
    """Session fixation: the id a browser held before the sign-in is not
    the id of the signed-in session."""
    assert admin_session["cookie_before"] and admin_session["cookie_after"]
    assert admin_session["cookie_before"] != admin_session["cookie_after"]


def test_session_cookie_is_http_only_and_same_site_lax(site_a):
    client = Client(site_a["port"])
    reply = client.get("/login")
    cookie = next(line for line in reply.set_cookies if line.startswith("connect.sid="))
    flags = [part.strip().lower() for part in cookie.split(";")[1:]]
    assert "httponly" in flags and "samesite=lax" in flags
    assert "secure" not in flags, "development mode serves plain http"
    assert any(flag.startswith("expires=") for flag in flags)


# ---- roles ----

def test_a_page_opens_for_any_one_of_its_roles_and_for_nobody_else(role_sessions):
    """Real sessions, real session store. /staff lists admin and editor; a
    page that chained one guard per role could be opened by neither."""
    sessions = role_sessions["sessions"]
    opened = {}
    for path in ("/members", "/staff", "/anyone"):
        opened[path] = set()
        for who, client in sessions.items():
            reply = client.get(path)
            if reply.status == 200:
                opened[path].add(who)
            else:
                assert (reply.status, reply.location) == (302, "/login"), (path, who)
    assert opened["/staff"] == {"admin", "editor"}
    assert opened["/members"] == {"member"}
    assert opened["/anyone"] == {"admin", "editor", "member", "user"}


def test_every_protected_page_the_build_kept_is_reachable_and_asks_for_its_login(site_a, role_sessions):
    """The build classifies a custom page as kept only if its own route is
    the first to answer its path. Checked here on the running site, for
    pages that sit beside a model's routes (/resources is the Resource
    model's mount, /services belongs to a page of the site and to the
    Service model) and under /admin."""
    sessions = role_sessions["sessions"]
    for path, title in (("/resources", "Resource room"), ("/resources/a/b", "Deep room"),
                        ("/services/pricing", "Price room"), ("/admin/reports", "Report room"),
                        ("/Resources/", "Resource room")):
        for who in ("anonymous", "member"):
            reply = sessions[who].get(path)
            assert (reply.status, reply.location) == (302, "/login"), (path, who)
        opened = sessions["admin"].get(path)
        assert opened.status == 200 and title in opened.text, path
    # None of them is advertised to visitors.
    home = sessions["anonymous"].get("/").text
    sitemap = sessions["anonymous"].get("/sitemap.xml").text
    for path in ("/resources", "/services/pricing", "/admin/reports", "/members", "/staff"):
        assert f'href="{path}"' not in home and path not in sitemap, path
    # And what the model routers keep for themselves stays a 404 for a visitor.
    for path in ("/resources/1", "/resources/new", "/resources/1/edit", "/services/new"):
        assert sessions["anonymous"].get(path).status == 404, path


def test_an_account_that_is_not_the_admin_lands_on_a_page_it_can_open(role_sessions):
    assert role_sessions["landings"] == {"member": (302, "/"), "editor": (302, "/"), "user": (302, "/")}
    member = role_sessions["sessions"]["member"]
    assert member.get("/").status == 200
    # Its protected page does not offer it the admin's navigation.
    page = member.get("/members").text
    assert "Members area" in page and 'href="/admin/settings"' not in page
    assert 'action="/logout"' in page
    assert 'href="/admin/settings"' in role_sessions["sessions"]["admin"].get("/staff").text


def test_admin_routes_refuse_a_signed_in_account_that_is_not_the_admin(site_a, role_sessions):
    member = role_sessions["sessions"]["member"]
    token = member.csrf("/login")        # the member's own, valid CSRF token
    for path in ("/admin", "/admin/settings", "/admin/workshops", "/admin/workshops/new",
                 "/admin/workshops/1", "/admin/workshops/1/edit", "/admin/happenings",
                 "/admin/contact-inquiries", "/admin/rsvps"):
        reply = member.get(path)
        assert (reply.status, reply.location) == (302, "/login"), path

    name_before = site_a["query"]("SELECT value FROM site_settings WHERE key = 'site_name'")[0][0]
    workshops_before = site_a["query"]("SELECT COUNT(*) FROM workshops")[0][0]
    writes = [
        ("/admin/settings", {"site_name": "Taken over"}),
        ("/admin/workshops", {"title": "Planted"}),
        ("/admin/workshops/1/update", {"title": "Changed"}),
        ("/admin/workshops/1/delete", {}),
        ("/admin/clear-all", {}),
        ("/admin/change-password", {"new_password": "long enough to pass"}),
    ]
    for path, form in writes:
        reply = member.post(path, dict(form, _csrf=token))
        assert (reply.status, reply.location) == (302, "/login"), path
    upload = member.upload("/admin/upload", "x.png", b"\x89PNG\r\n\x1a\n" + bytes(16), "image/png",
                           headers={"x-csrf-token": token})
    assert (upload.status, upload.location) == (302, "/login")

    assert site_a["query"]("SELECT value FROM site_settings WHERE key = 'site_name'")[0][0] == name_before
    assert site_a["query"]("SELECT COUNT(*) FROM workshops")[0][0] == workshops_before
    assert site_a["query"]("SELECT title FROM workshops WHERE id = 1")[0][0] != "Changed"
    assert site_a["query"]("SELECT password FROM users WHERE email = %s", (MEMBER,))[0][0] is None


# ---- CSRF ----

def test_a_public_write_needs_the_sessions_own_csrf_token(site_a):
    count = lambda: site_a["query"]("SELECT COUNT(*) FROM contact_inquiries")[0][0]  # noqa: E731
    before = count()
    message = {"full_name": "Pat Doe", "email": "pat@example.org", "message": "Hello there"}
    visitor = Client(site_a["port"])
    other = Client(site_a["port"])
    own_token = visitor.csrf("/contact")

    assert visitor.post("/contact", message).status == 403
    assert visitor.post("/contact", dict(message, _csrf="0" * 64)).status == 403
    # A valid token, but of another visitor's session.
    assert visitor.post("/contact", dict(message, _csrf=other.csrf("/contact"))).status == 403
    # A scripted client sends the token in a header and gets JSON back.
    api = visitor.post("/contact", message, headers={"x-csrf-token": "0" * 64})
    assert api.status == 403 and json.loads(api.text) == {"error": "Invalid CSRF token"}
    assert count() == before

    accepted = visitor.post("/contact", dict(message, _csrf=own_token))
    assert accepted.status == 200 and "Thank you for reaching out" in accepted.text
    assert count() == before + 1


def test_an_admin_write_needs_the_csrf_token_too(site_a, admin_session):
    admin = admin_session["client"]
    read = lambda: site_a["query"]("SELECT value FROM site_settings WHERE key = 'site_name'")[0][0]  # noqa: E731
    original = read()
    assert admin.post("/admin/settings", {"site_name": "Forged"}).status == 403
    assert admin.post("/admin/settings", {"site_name": "Forged", "_csrf": "f" * 64}).status == 403
    assert read() == original
    # Signing out is a write as well.
    assert admin.post("/logout", {}).status == 403
    assert admin.get("/admin").status == 200

    token = csrf_in(admin.get("/admin/settings").text)
    saved = admin.post("/admin/settings", {"site_name": original + " (edited)", "_csrf": token})
    assert (saved.status, saved.location) == (302, "/admin/settings")
    assert read() == original + " (edited)"
    admin.post("/admin/settings", {"site_name": original, "_csrf": token})
    assert read() == original


# ---- drafts ----

def test_an_unpublished_row_is_not_listed_and_cannot_be_read_by_its_address(site_a, admin_session):
    # A model's own public listing (the resource router).
    draft_id = site_a["query"]("SELECT id FROM workshops WHERE title = 'Draft workshop'")[0][0]
    live_id = site_a["query"]("SELECT id FROM workshops WHERE title = 'Early workshop'")[0][0]
    visitor = Client(site_a["port"])
    listing = visitor.get("/workshops")
    assert listing.status == 200 and "Early workshop" in listing.text
    assert "Draft workshop" not in listing.text
    assert visitor.get(f"/workshops/{draft_id}").status == 404
    assert visitor.get(f"/workshops/{live_id}").status == 200

    # A composed listing page and its detail route (routes/pages.js).
    site_a["query"](
        "INSERT INTO blog_posts (title, slug, body, published) VALUES "
        "('Hidden draft post', 'hidden-draft-post', 'not for readers', FALSE), "
        "('Visible post', 'visible-post', 'for readers', TRUE)"
    )
    hidden_id = site_a["query"]("SELECT id FROM blog_posts WHERE slug = 'hidden-draft-post'")[0][0]
    blog = visitor.get("/blog")
    assert blog.status == 200 and "Visible post" in blog.text and "Hidden draft post" not in blog.text
    assert visitor.get("/blog/visible-post").status == 200
    assert visitor.get("/blog/hidden-draft-post").status == 404
    assert visitor.get(f"/blog/{hidden_id}").status == 404
    assert visitor.get(f"/blog-posts/{hidden_id}").status == 404
    home = visitor.get("/")
    assert "Hidden draft post" not in home.text and "Draft workshop" not in home.text
    sitemap = visitor.get("/sitemap.xml")
    assert "hidden-draft-post" not in sitemap.text

    # The admin sees both.
    admin = admin_session["client"]
    assert "Draft workshop" in admin.get("/admin/workshops").text
    assert "Hidden draft post" in admin.get("/admin/blog-posts").text
    assert admin.get(f"/admin/workshops/{draft_id}").status == 200


# ---- uploads ----

def test_only_the_admin_can_upload_and_only_raster_images(site_a, admin_session):
    png = b"\x89PNG\r\n\x1a\n" + secrets.token_bytes(64)
    uploads = site_a["app_dir"] / "public" / "uploads"
    stored = lambda: sorted(  # noqa: E731
        p.name for p in uploads.rglob("*") if p.is_file() and p.name != ".gitkeep")

    visitor = Client(site_a["port"])
    refused = visitor.upload("/admin/upload", "photo.png", png, "image/png",
                             headers={"x-csrf-token": visitor.csrf()})
    assert (refused.status, refused.location) == (302, "/login")

    admin = admin_session["client"]
    token = csrf_in(admin.get("/admin/settings").text)
    assert admin.upload("/admin/upload", "photo.png", png, "image/png").status == 403        # no CSRF token
    for name, mimetype in (("page.html", "image/png"), ("script.js", "image/png"),
                           ("drawing.svg", "image/svg+xml"), ("photo.png", "text/html"),
                           ("noextension", "image/png")):
        rejected = admin.upload("/admin/upload", name, png, mimetype, headers={"x-csrf-token": token})
        assert rejected.status == 400, (name, mimetype, rejected.status)
        assert "Only JPG, PNG, GIF, WebP or AVIF" in json.loads(rejected.text)["error"]
    assert stored() == []

    accepted = admin.upload("/admin/upload", "../../photo.PNG", png, "image/png", headers={"x-csrf-token": token})
    assert accepted.status == 200
    url = json.loads(accepted.text)["url"]
    assert re.fullmatch(r"/uploads/\d{4}/\d{2}/\d+-[0-9a-f]{16}\.png", url), url
    assert len(stored()) == 1 and stored()[0] == url.rsplit("/", 1)[1]
    served = visitor.request("GET", url)
    assert served.status == 200 and served.headers.get("Content-Type") == "image/png"


# ---- model-derived forms, listings and dashboard ----

def test_contact_form_shows_and_stores_the_fields_of_the_contact_model(site_a, admin_session):
    visitor = Client(site_a["port"])
    page = visitor.get("/contact")
    assert page.status == 200
    form = page.text[page.text.index('<form action="/contact"'):]
    form = form[:form.index("</form>")]
    assert re.findall(r'<(?:input|textarea|select)[^>]* name="([a-z_]+)"', form) == [
        "_csrf", "full_name", "email", "company", "message"]
    token = csrf_in(form)

    incomplete = visitor.post("/contact", {"_csrf": token, "full_name": "", "email": "Sam@Example.org",
                                           "company": "Kept Co", "message": "typed text"})
    assert incomplete.status == 200 and "Full Name is required" in incomplete.text
    assert 'value="Kept Co"' in incomplete.text and "typed text</textarea>" in incomplete.text

    sent = visitor.post("/contact", {"_csrf": token, "full_name": " Sam Roe ", "email": "Sam@Example.org",
                                     "company": "", "message": "Please write back", "phone": "ignored"})
    assert sent.status == 200 and "Thank you for reaching out" in sent.text
    row = site_a["query"](
        "SELECT full_name, email, company, message FROM contact_inquiries WHERE full_name = 'Sam Roe'")
    assert row == [("Sam Roe", "sam@example.org", None, "Please write back")]

    # The dashboard's "recent inquiries" card reads the same model.
    dashboard = admin_session["client"].get("/admin")
    assert dashboard.status == 200 and "Recent Inquiries" in dashboard.text
    assert "Sam Roe" in dashboard.text and "sam@example.org" in dashboard.text
    inquiry_id = site_a["query"]("SELECT id FROM contact_inquiries WHERE full_name = 'Sam Roe'")[0][0]
    assert f'href="/admin/contact-inquiries/{inquiry_id}"' in dashboard.text


def test_a_dated_model_is_listed_in_date_order_whatever_it_is_called(site_a, admin_session):
    visitor = Client(site_a["port"])
    listing = visitor.get("/workshops")
    assert listing.status == 200
    # Order of first appearance on the page: by starts_on, not by row id
    # (the rows were seeded Late, Early, Middle).
    order = list(dict.fromkeys(re.findall(r"(Late|Early|Middle) workshop", listing.text)))
    assert order == ["Early", "Middle", "Late"]

    # A model named Event that has no event_date column: its admin list
    # works, and the dashboard's "upcoming" card reads the date it does have.
    admin = admin_session["client"]
    manage = admin.get("/admin/happenings")
    assert manage.status == 200 and "Open day" in manage.text
    dashboard = admin.get("/admin")
    assert dashboard.status == 200 and "Upcoming Events" in dashboard.text and "Open day" in dashboard.text
    event_id = site_a["query"]("SELECT id FROM happenings WHERE title = 'Open day'")[0][0]
    assert f'href="/admin/happenings/{event_id}/edit"' in dashboard.text
    assert 'href="/admin/happenings/new"' in dashboard.text
    log = site_a["log"]()
    assert "does not exist" not in log and "[dashboard]" not in log


def test_a_form_page_on_a_private_models_route_works_and_the_rows_stay_private(site_a, admin_session):
    visitor = Client(site_a["port"])
    page = visitor.get("/rsvps")
    assert page.status == 200 and 'name="email"' in page.text and "RSVP" in page.text
    sent = visitor.post("/rsvps", {"_csrf": csrf_in(page.text), "name": "Kim", "email": "kim@example.org"})
    assert (sent.status, sent.location) == (302, "/rsvps")
    assert site_a["query"]("SELECT name, email FROM rsvps") == [("Kim", "kim@example.org")]
    # The rows themselves are not readable by visitors.
    rsvp_id = site_a["query"]("SELECT id FROM rsvps")[0][0]
    assert visitor.get(f"/rsvps/{rsvp_id}").status == 404
    assert visitor.get("/rsvps/new").status == 404
    assert visitor.get("/admin/rsvps").location == "/login"
    assert "Kim" in admin_session["client"].get("/admin/rsvps").text


# ---- headers ----

def test_inline_scripts_carry_the_response_nonce_and_nothing_else_may_run(site_a):
    reply = Client(site_a["port"]).get("/")
    policy = reply.headers.get("Content-Security-Policy")
    script_src = next(part for part in policy.split(";") if part.strip().startswith("script-src"))
    nonce = re.search(r"'nonce-([A-Za-z0-9+/=]+)'", script_src).group(1)
    assert "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src
    inline = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", reply.text)
    assert inline, "the home page has inline scripts"
    assert all(f'nonce="{nonce}"' in attrs for attrs in inline)
    again = Client(site_a["port"]).get("/").headers.get("Content-Security-Policy")
    assert nonce not in again, "a new nonce per response"
    assert reply.headers.get("X-Powered-By") is None
    assert reply.headers.get("X-Content-Type-Options") == "nosniff"


# ---------------------------------------------------------------------------
# Site B: production mode, a mail key, outgoing mail recorded
# ---------------------------------------------------------------------------

_FETCH_RECORDER = """
// Test stand-in for the mail provider: every outgoing fetch is appended to
// a file and answered 200. Nothing leaves the machine.
const fs = require('fs');
global.fetch = async function (url, options) {
  fs.appendFileSync(process.env.CAS_TEST_FETCH_LOG,
    JSON.stringify({ url: String(url), body: (options && options.body) || null }) + '\\n');
  return { ok: true, status: 200, text: async () => '{}', json: async () => ({}) };
};
"""


@pytest.fixture(scope="module")
def site_b(tmp_path_factory):
    os.environ.setdefault("CAS_LINT_EXTERNAL_URLS", "skip")
    root = tmp_path_factory.mktemp("security-b")
    out = root / "app"
    build_app(str(FIXTURES / "sample-healer-spec.json"), output_dir=out)
    recorder = root / "record-fetch.js"
    recorder.write_text(_FETCH_RECORDER, encoding="utf-8")
    fetch_log = root / "fetch.log"
    fetch_log.write_text("", encoding="utf-8")
    env = {
        "NODE_OPTIONS": f"--require={recorder}",
        "CAS_TEST_FETCH_LOG": str(fetch_log),
        # Not a credential: a random value that only has to be non-empty.
        "RESEND_API_KEY": "test-" + secrets.token_hex(12),
    }
    with boot_generated_app(out, admin_email=HEALER_ADMIN, node_env="production", extra_env=env) as running:
        running["fetch_log"] = fetch_log
        yield running


def test_with_a_mail_key_the_login_link_goes_to_the_mailbox_and_not_to_the_log(site_b):
    visitor = Client(site_b["port"], https_proxy=True)
    reply = visitor.post("/request-magic-link",
                         {"_csrf": visitor.csrf("/request-magic-link"), "email": HEALER_ADMIN})
    assert (reply.status, reply.location) == (302, "/magic-link-sent")

    calls = [json.loads(line) for line in site_b["fetch_log"].read_text(encoding="utf-8").splitlines()]
    mail = [c for c in calls if c["url"] == "https://api.resend.com/emails"]
    assert len(mail) == 1
    body = json.loads(mail[0]["body"])
    assert body["to"] == [HEALER_ADMIN]
    link = re.search(r"https://localhost:\d+/magic\?token=([0-9a-f]{64})", body["html"])
    assert link, body["html"][:200]
    raw = link.group(1)
    assert token_state(site_b, raw) == (False, True), "the mailed link is the real, unused one"

    # Opening the link is a request the server logs; the token is not in it.
    assert visitor.get("/magic?token=" + raw).status == 200
    log = site_b["log"]()
    assert raw not in log and "token=" not in log
    assert re.search(r"^\[magic-link\] issued for user \d+$", log, re.M)
    assert '"GET /magic HTTP/1.1" 200' in log


def test_in_production_the_session_cookie_is_secure_and_never_set_over_plain_http(site_b):
    behind_tls = Client(site_b["port"], https_proxy=True)
    cookie = next(line for line in behind_tls.get("/login").set_cookies if line.startswith("connect.sid="))
    flags = [part.strip().lower() for part in cookie.split(";")[1:]]
    assert "secure" in flags and "httponly" in flags and "samesite=lax" in flags

    plain = Client(site_b["port"])
    reply = plain.get("/login")
    assert reply.status == 200
    assert not [line for line in reply.set_cookies if line.startswith("connect.sid=")]


def test_newsletter_signup_stores_the_address_in_the_subscriber_model(site_b):
    visitor = Client(site_b["port"], https_proxy=True)
    address = f"reader-{secrets.token_hex(4)}@example.org"
    reply = visitor.post("/newsletter-signup", {"_csrf": visitor.csrf("/"), "email": address.upper()})
    assert (reply.status, reply.location) == (302, "/#newsletter-success")
    assert site_b["query"]("SELECT COUNT(*) FROM newsletter_subscribers WHERE email = %s", (address,))[0][0] == 1
    # Not an address: accepted silently, nothing stored.
    total = site_b["query"]("SELECT COUNT(*) FROM newsletter_subscribers")[0][0]
    reply = visitor.post("/newsletter-signup", {"_csrf": visitor.csrf("/"), "email": "not an address"})
    assert reply.status == 302
    assert site_b["query"]("SELECT COUNT(*) FROM newsletter_subscribers")[0][0] == total


def test_password_guessing_from_one_address_is_cut_off(site_b):
    """Ten attempts per fifteen minutes per address, then 429. Runs last in
    this module: it uses up the address's allowance on site B."""
    guesser = Client(site_b["port"], https_proxy=True)
    token = guesser.csrf()
    statuses = []
    for attempt in range(12):
        reply = guesser.post("/login", {"_csrf": token, "email": HEALER_ADMIN, "password": f"guess-{attempt}"})
        statuses.append(reply.status)
        if reply.status == 429:
            break
        assert "Invalid email or password" in reply.text
    assert statuses[-1] == 429 and statuses.count(200) == 10, statuses
    assert "Too many requests" in reply.text
    # The limit is on the address, not on the session: a new browser from
    # the same address is refused as well.
    fresh = Client(site_b["port"], https_proxy=True)
    assert fresh.post("/login", {"_csrf": fresh.csrf(), "email": HEALER_ADMIN, "password": "x"}).status == 429
