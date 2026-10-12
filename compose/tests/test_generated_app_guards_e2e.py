"""A generated site with protected, public and dropped custom pages, booted
for real: `node server.js` from the emitted files, a real session store, real
views and a throwaway PostgreSQL database. Requests go over HTTP on loopback
with a cookie jar, the way a browser would send them.

This is the end-to-end check for the guarantees that used to be asserted on
emitted source text only:

  - a form on an auth_required page accepts a POST from the admin and from
    nobody else;
  - /set-password asks for the current password unless the account has none
    or a login link has just signed the session in;
  - a custom page that was dropped (not a plain path, a duplicate, a built-in
    route, a public page under /admin) has no route and no navigation link,
    and the built-in login form is the one served at /login.

Same prerequisites as test_compose_end_to_end.py (node, a local PostgreSQL,
builds/sample-healer/node_modules); the whole module skips without them.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from compose.build import build_app
from compose.tests.test_compose_end_to_end import boot_generated_app

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
ADMIN_EMAIL = "owner@guards.example"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    os.environ.setdefault("CAS_LINT_EXTERNAL_URLS", "skip")
    spec = json.loads((FIXTURES / "sample-coach-spec.json").read_text(encoding="utf-8"))
    spec["admin_email"] = ADMIN_EMAIL
    spec["data_models"] = [
        {"name": "MemberNote", "fields": [{"name": "body", "sql_type": "TEXT", "required": True}]},
    ]
    spec["custom_pages"] = [
        {"path": "/members-form", "title": "Members only", "auth_required": True,
         "allowed_roles": ["admin"], "form_target": "MemberNote"},
        {"path": "/book", "title": "Book a call"},
        {"path": "/team/leads", "title": "Team leads"},
        {"path": "/team-leads", "title": "Leads again"},          # same view file: dropped
        {"path": "/a b", "title": "Space"},                       # not a plain path: dropped
        {"path": "/Login", "title": "Sign in here"},              # built-in route: dropped
        {"path": "/ADMIN/billing", "title": "Billing"},           # public page under /admin: dropped
    ]
    out = tmp_path_factory.mktemp("guards-e2e") / "app"
    build_app(spec, output_dir=out)
    with boot_generated_app(out, admin_email=ADMIN_EMAIL) as running:
        yield running


class Browser:
    """A cookie-keeping HTTP client that does not follow redirects."""

    def __init__(self, port: int):
        self.base = f"http://localhost:{port}"

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect,
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def _send(self, req):
        try:
            with self.opener.open(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace"), resp.headers.get("Location")
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode("utf-8", "replace"), err.headers.get("Location")

    def get(self, path: str):
        return self._send(urllib.request.Request(self.base + path))

    def post(self, path: str, form: dict):
        body = urllib.parse.urlencode(form).encode("ascii")
        return self._send(urllib.request.Request(self.base + path, data=body, method="POST"))

    def csrf(self, path: str = "/login") -> str:
        """The CSRF token of this browser's session, read from a form."""
        status, body, _ = self.get(path)
        assert status == 200, f"{path} answered {status}"
        return _csrf_in(body)


def _csrf_in(body: str) -> str:
    found = re.search(r'name="_csrf" value="([0-9a-f]{16,})"', body)
    assert found, "no CSRF input in the page"
    return found.group(1)


def _sign_in_by_login_link(site, browser: Browser) -> None:
    """Issue a login token the way the generated app stores one (the SHA-256
    of a random value) and walk the two-step confirm flow with it."""
    raw = secrets.token_hex(32)
    site["query"](
        "INSERT INTO magic_link_tokens (user_id, token_hash, expires_at) "
        "SELECT id, %s, NOW() + INTERVAL '1 hour' FROM users WHERE email = %s",
        (hashlib.sha256(raw.encode("ascii")).hexdigest(), ADMIN_EMAIL),
    )
    status, body, _ = browser.get("/magic?token=" + raw)
    assert status == 200, f"/magic answered {status}"
    status, _, location = browser.post("/magic/consume", {"_csrf": _csrf_in(body), "token": raw})
    assert (status, location) == (302, "/admin")


def _stored_password(site):
    return site["query"]("SELECT password FROM users WHERE email = %s", (ADMIN_EMAIL,))[0][0]


def test_built_in_login_form_is_served_whatever_the_spec_called_its_pages(site):
    browser = Browser(site["port"])
    for path in ("/login", "/Login"):
        status, body, _ = browser.get(path)
        assert status == 200
        assert 'type="password"' in body and 'name="_csrf"' in body
        assert "Sign in here" not in body


def test_dropped_pages_have_no_route_and_kept_pages_do(site):
    browser = Browser(site["port"])
    for path in ("/book", "/team/leads"):
        assert browser.get(path)[0] == 200, path
    for path in ("/team-leads", "/a%20b", "/ADMIN/billing", "/admin/billing"):
        assert browser.get(path)[0] == 404, path


def test_every_link_on_the_home_page_stays_on_the_site_and_resolves(site):
    browser = Browser(site["port"])
    status, body, _ = browser.get("/")
    assert status == 200
    assert "Book a call" in body and "Team leads" in body
    for dropped in ("Leads again", "Sign in here", "Billing", ">Space<"):
        assert dropped not in body, dropped
    hrefs = set(re.findall(r'href="([^"]*)"', body))
    assert not [h for h in hrefs if h.startswith("//") or h.startswith("/\\")], hrefs
    local = sorted(h.split("#")[0].split("?")[0] for h in hrefs if h.startswith("/"))
    assert "/book" in local and "/team/leads" in local
    for path in sorted(set(local)):
        assert browser.get(path)[0] == 200, f"the home page links to {path}, which does not resolve"


def test_protected_form_takes_a_post_from_the_admin_and_nobody_else(site):
    """The finding, on a running site: a visitor with a valid CSRF token of
    their own used to be able to insert rows through an auth_required form."""
    count = lambda: site["query"]("SELECT COUNT(*) FROM member_notes")[0][0]  # noqa: E731
    assert count() == 0

    visitor = Browser(site["port"])
    assert visitor.get("/members-form")[::2] == (302, "/login")
    status, _, location = visitor.post(
        "/members-form", {"_csrf": visitor.csrf("/login"), "body": "from a stranger"})
    assert (status, location) == (302, "/login")
    assert count() == 0

    admin = Browser(site["port"])
    _sign_in_by_login_link(site, admin)
    status, form_page, _ = admin.get("/members-form")
    assert status == 200 and 'name="body"' in form_page
    status, _, location = admin.post(
        "/members-form", {"_csrf": _csrf_in(form_page), "body": "from the owner"})
    assert (status, location) == (302, "/members-form")
    assert site["query"]("SELECT body FROM member_notes") == [("from the owner",)]


def test_set_password_asks_for_the_current_password_once_there_is_one(site):
    """Walks the whole rule on a running site. (Six requests here and one in
    the test above go through the login rate limiter, which allows ten per
    fifteen minutes per address; keep that in mind before adding more.)"""
    first = secrets.token_urlsafe(12)
    second = secrets.token_urlsafe(12)
    third = secrets.token_urlsafe(12)
    assert _stored_password(site) is None, "the seeded admin starts without a password"

    # 1. First sign-in is by login link; no password exists, none is asked for.
    owner = Browser(site["port"])
    _sign_in_by_login_link(site, owner)
    status, page, _ = owner.get("/set-password")
    assert status == 200 and 'name="current_password"' not in page
    status, _, location = owner.post(
        "/set-password", {"_csrf": _csrf_in(page), "new_password": first, "confirm_password": first})
    assert (status, location) == (302, "/admin")
    hash_after_first = _stored_password(site)
    assert hash_after_first

    # 2. Same session, a moment later: the login-link allowance was spent by
    #    step 1, so the current password is now required.
    status, page, _ = owner.get("/set-password")
    assert 'name="current_password"' in page
    token = _csrf_in(page)
    status, page, _ = owner.post(
        "/set-password", {"_csrf": token, "new_password": second, "confirm_password": second})
    assert status == 200 and "Current password is incorrect." in page
    assert _stored_password(site) == hash_after_first
    status, _, location = owner.post(
        "/set-password", {"_csrf": token, "current_password": first,
                          "new_password": second, "confirm_password": second})
    assert (status, location) == (302, "/admin")
    hash_after_second = _stored_password(site)
    assert hash_after_second != hash_after_first

    # 3. A session started with the password (not a login link) cannot
    #    replace it without typing it.
    other = Browser(site["port"])
    status, _, location = other.post(
        "/login", {"_csrf": other.csrf("/login"), "email": ADMIN_EMAIL, "password": second})
    assert (status, location) == (302, "/admin")
    status, page, _ = other.get("/set-password")
    assert 'name="current_password"' in page
    status, page, _ = other.post(
        "/set-password", {"_csrf": _csrf_in(page), "new_password": third, "confirm_password": third})
    assert status == 200 and "Current password is incorrect." in page
    assert _stored_password(site) == hash_after_second
