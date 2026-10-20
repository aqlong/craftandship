"""Emitted routers, executed.

Most generated-app tests in this directory read the emitted source. These
load the emitted routes/pages.js and routes/auth.js into a real Express app
and send HTTP requests to it on a loopback port, so a guard that is present
in the text but does not guard is caught.

Real: Express, express-rate-limit and bcrypt (from web/node_modules, the same
majors the generated package.json asks for), and the emitted routers,
middleware/auth.js, middleware/rate-limit.js, middleware/fields.js,
middleware/settings.js and services/notify.js.

Stand-ins, and therefore NOT covered here: the database (db.js is replaced by
a recorder that returns canned rows, or raises an error the test names), the
session store (each request's session is supplied by the test as JSON),
res.render (it answers with the view name and a few locals as JSON, so no
template runs), the mail provider (global fetch records what notify.js would
send and answers 200) and server.js.
compose/tests/test_compose_end_to_end.py boots the whole generated site
against Postgres when its dependencies are installed.

Skipped when node or web/node_modules (`cd web && npm ci`) is missing.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from compose.build import build_app

COMPOSE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = COMPOSE_DIR.parent
FIXTURES = COMPOSE_DIR / "fixtures"
WEB_MODULES = REPO_ROOT / "web" / "node_modules"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not all((WEB_MODULES / name).exists() for name in ("express", "express-rate-limit", "bcrypt")),
    reason="needs node and web/node_modules with express, express-rate-limit and bcrypt (run `cd web && npm ci`)",
)

# Replaces the emitted db.js. Records every statement; answers the few
# SELECTs the routers under test make from a fixture the harness sets per
# request. Password hashes are made here, at run time, with the real bcrypt.
_DB_STUB = r"""
const bcrypt = require('bcrypt');
const hashes = {};
function hashFor(password) {
  if (!hashes[password]) hashes[password] = bcrypt.hashSync(password, 4);
  return hashes[password];
}
const db = {
  calls: [],
  params: [],
  fixture: {},
  pool: {},
  query: async function (text, params) {
    const sql = String(text).replace(/\s+/g, ' ').trim();
    db.calls.push(sql);
    db.params.push(params === undefined ? null : params);
    const f = db.fixture || {};
    // A statement the test wants to fail, the way node-postgres fails one:
    // an Error whose message is the server's text, with its SQLSTATE code.
    if (f.failOn && sql.indexOf(f.failOn) === 0) {
      const error = new Error(f.failWith || 'stand-in database error');
      if (f.failCode) error.code = f.failCode;
      throw error;
    }
    if (sql.indexOf('SELECT key, value FROM site_settings') === 0) {
      const rows = f.siteSettings || [];
      return { rows: rows, rowCount: rows.length };
    }
    if (sql.indexOf('SELECT id, email FROM users WHERE LOWER(email)') === 0) {
      return { rows: [{ id: 1, email: 'owner@example.com' }], rowCount: 1 };
    }
    if (sql.indexOf('SELECT COUNT(*)') === 0) return { rows: [{ total: 0, count: '0' }], rowCount: 1 };
    if (sql.indexOf('SELECT password FROM users') === 0) {
      if (f.user === 'missing') return { rows: [], rowCount: 0 };
      return { rows: [{ password: f.userPassword ? hashFor(f.userPassword) : null }], rowCount: 1 };
    }
    if (sql.indexOf('SELECT id, email, password, role FROM users') === 0) {
      return { rows: [{ id: 1, email: 'owner@example.com', role: 'admin',
                        password: f.userPassword ? hashFor(f.userPassword) : null }], rowCount: 1 };
    }
    if (sql.indexOf('FROM magic_link_tokens t JOIN users u') !== -1) {
      return { rows: [{ id: 3, user_id: 1, used_at: null, expires_at: new Date(Date.now() + 3600000),
                        email: 'owner@example.com', role: 'admin', password_set: true }], rowCount: 1 };
    }
    if (sql.indexOf('RETURNING id') !== -1) return { rows: [{ id: 7 }], rowCount: 1 };
    return { rows: [], rowCount: 1 };
  },
};
module.exports = db;
"""

# argv[1]: the built app directory. argv[2]: JSON plan
# {router, mount?, requests: [{method, path, session, form, db, headers?, sid?,
# expectMail?}]}. `mount` is where the router is attached ("/" unless given; a
# model's router is also mounted at /admin/<slug>, where it serves the admin).
# `headers` are sent as they are (a test's own Host header, for one); `sid` is
# the request's session id (req.sessionID); with `expectMail` the harness
# waits up to two seconds for the fire-and-forget mail the request starts.
_HARNESS = r"""
const path = require('path');
const http = require('http');
const express = require('express');
const appDir = process.argv[1];
const plan = JSON.parse(process.argv[2]);
// stdout carries the JSON result only; whatever the generated code logs
// goes to stderr.
console.log = console.info = console.debug = console.error;
const db = require(path.join(appDir, 'db.js'));

// The mail provider: services/notify.js posts to it with fetch. Each call is
// recorded with its JSON body and answered 200; nothing leaves the machine.
const mails = [];
global.fetch = async function (url, options) {
  mails.push({ url: String(url), body: JSON.parse((options && options.body) || 'null') });
  return { ok: true, status: 200, text: async function () { return '{}'; }, json: async function () { return {}; } };
};

let lastSession = null;
const app = express();
app.use(express.urlencoded({ extended: true }));
app.use(function (req, res, next) {
  const session = JSON.parse(req.headers['x-test-session'] || '{}');
  session.save = function (cb) { if (cb) cb(); };
  session.regenerate = function (cb) {
    Object.keys(session).forEach(function (k) { if (typeof session[k] !== 'function') delete session[k]; });
    cb();
  };
  session.destroy = function (cb) { if (cb) cb(); };
  req.session = session;
  req.sessionID = req.headers['x-test-sid'] || 'sid-under-test';
  lastSession = session;
  res.locals.settings = { site_name: 'Test' };
  res.render = function (view, locals) {
    const l = locals || {};
    const shown = { view: view, error: l.error || null,
                    needs_current: l.needs_current === undefined ? null : l.needs_current };
    if (l.flashSuccess) shown.flashSuccess = l.flashSuccess;
    if (l.flashError) shown.flashError = l.flashError;
    res.json(shown);
  };
  next();
});
app.use(plan.mount || '/', require(path.join(appDir, 'routes', plan.router + '.js')));
app.use(function (req, res) { res.status(404).json({ view: 'no-route' }); });

function send(port, r) {
  return new Promise(function (resolve, reject) {
    const body = r.form ? new URLSearchParams(r.form).toString() : '';
    const headers = Object.assign({ 'x-test-session': JSON.stringify(r.session || {}) }, r.headers || {});
    if (r.sid) headers['x-test-sid'] = r.sid;
    if (r.method === 'POST') {
      headers['content-type'] = 'application/x-www-form-urlencoded';
      headers['content-length'] = Buffer.byteLength(body);
    }
    const req = http.request({ host: '127.0.0.1', port: port, path: r.path, method: r.method, headers: headers },
      function (res) {
        let data = '';
        res.setEncoding('utf8');
        res.on('data', function (chunk) { data += chunk; });
        res.on('end', function () {
          let parsed = null;
          try { parsed = JSON.parse(data); } catch (e) { parsed = null; }
          resolve({ status: res.statusCode, location: res.headers.location || null, body: parsed });
        });
      });
    req.on('error', reject);
    req.end(body);
  });
}

const server = app.listen(0, '127.0.0.1', async function () {
  const port = server.address().port;
  const results = [];
  try {
    for (const r of plan.requests) {
      db.fixture = r.db || {};
      const before = db.calls.length;
      const mailsBefore = mails.length;
      const out = await send(port, r);
      for (let waited = 0; r.expectMail && mails.length === mailsBefore && waited < 2000; waited += 20) {
        await new Promise(function (resolve) { setTimeout(resolve, 20); });
      }
      out.sql = db.calls.slice(before);
      out.params = db.params.slice(before);
      out.mails = mails.slice(mailsBefore);
      out.session = JSON.parse(JSON.stringify(lastSession || {}));
      results.push(out);
    }
    process.stdout.write(JSON.stringify(results));
  } catch (err) {
    process.stderr.write(String(err && err.stack || err));
    process.exitCode = 1;
  } finally {
    server.close();
  }
});
"""


def _run_with_log(app_dir: Path, router: str, requests: list, mount: str = "/",
                  env: dict = None) -> tuple:
    """The results and what the generated code wrote to its log (stderr).
    `env` adds environment variables of the node process."""
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "NODE_PATH": str(WEB_MODULES),
        "NODE_ENV": "test",
    }
    child_env.update(env or {})
    proc = subprocess.run(
        [NODE, "-e", _HARNESS, str(app_dir),
         json.dumps({"router": router, "mount": mount, "requests": requests})],
        capture_output=True, text=True, cwd=str(app_dir), env=child_env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    return json.loads(proc.stdout), proc.stderr


def _run(app_dir: Path, router: str, requests: list, mount: str = "/", env: dict = None) -> list:
    return _run_with_log(app_dir, router, requests, mount=mount, env=env)[0]


def _inserts(result: dict) -> list:
    return [sql for sql in result["sql"] if sql.startswith("INSERT INTO ")]


def _password_updates(result: dict) -> list:
    return [sql for sql in result["sql"] if sql.startswith("UPDATE users SET password")]


@pytest.fixture(scope="module")
def app_dir(tmp_path_factory):
    """One build with a protected form page and a public form page, its
    db.js replaced by the recorder."""
    os.environ["CAS_LINT_EXTERNAL_URLS"] = "skip"
    spec = json.loads((FIXTURES / "sample-coach-spec.json").read_text(encoding="utf-8"))
    spec["data_models"] = [
        {"name": "MemberNote", "fields": [{"name": "body", "sql_type": "TEXT", "required": True}]},
        {"name": "BookingRequest", "fields": [{"name": "name", "sql_type": "TEXT", "required": True}]},
        # A model whose columns a visitor must not all be able to write.
        {"name": "Story", "plural": "stories", "public_listing_path": "/stories", "fields": [
            {"name": "title", "sql_type": "VARCHAR(160)", "required": True},
            {"name": "body", "sql_type": "TEXT"},
            {"name": "featured_on_home", "sql_type": "BOOLEAN", "default": False},
            {"name": "sort_order", "sql_type": "INTEGER"},
            {"name": "published", "sql_type": "BOOLEAN", "default": True},
        ]},
        # A public list with a column the page does not print (`secret_note`),
        # one it prints (`title`) and one nothing can order by (`details`).
        {"name": "Resource", "plural": "resources", "public_listing_path": "/resources", "fields": [
            {"name": "title", "sql_type": "VARCHAR(160)", "required": True},
            {"name": "summary", "sql_type": "TEXT"},
            {"name": "secret_note", "sql_type": "TEXT"},
            {"name": "price", "sql_type": "INTEGER"},
            {"name": "details", "sql_type": "JSON"},
        ]},
    ]
    spec["custom_pages"] = [
        {"path": "/members-form", "title": "Members only", "auth_required": True,
         "allowed_roles": ["admin"], "form_target": "MemberNote"},
        # Roles and no auth_required: still a protected page.
        {"path": "/staff-note", "title": "Staff note", "allowed_roles": ["admin"], "form_target": "MemberNote"},
        {"path": "/book", "title": "Book", "form_target": "BookingRequest", "allowed_fields": ["name"]},
        {"path": "/tell", "title": "Tell us", "form_target": "Story", "allowed_fields": ["title", "body"]},
    ]
    out = tmp_path_factory.mktemp("runtime") / "app"
    build_app(spec, output_dir=out)
    (out / "db.js").write_text(_DB_STUB, encoding="utf-8")
    return out


ANON = {"csrfToken": "t0ken"}
EDITOR = {"csrfToken": "t0ken", "userId": 5, "role": "editor"}
ADMIN = {"csrfToken": "t0ken", "userId": 1, "role": "admin"}


def test_protected_form_refuses_a_post_without_the_page_guard(app_dir):
    """The POST of an auth_required form page: a visitor holding a valid
    CSRF token (anyone can get one from /login) is sent to /login and
    nothing is inserted. Only the admin's POST reaches the INSERT."""
    note = {"_csrf": "t0ken", "body": "hello"}
    anon_get, anon_post, editor_post, admin_post, public_post = _run(app_dir, "pages", [
        {"method": "GET", "path": "/members-form", "session": ANON},
        {"method": "POST", "path": "/members-form", "session": ANON, "form": note},
        {"method": "POST", "path": "/members-form", "session": EDITOR, "form": note},
        {"method": "POST", "path": "/members-form", "session": ADMIN, "form": note},
        {"method": "POST", "path": "/book", "session": ANON, "form": {"_csrf": "t0ken", "name": "Visitor"}},
    ])
    assert (anon_get["status"], anon_get["location"]) == (302, "/login")
    assert (anon_post["status"], anon_post["location"]) == (302, "/login")
    assert anon_post["sql"] == []
    assert (editor_post["status"], editor_post["location"]) == (302, "/login")
    assert editor_post["sql"] == []
    assert (admin_post["status"], admin_post["location"]) == (302, "/members-form")
    assert _inserts(admin_post) == ["INSERT INTO member_notes (body) VALUES ($1) RETURNING id"]
    # Control: the recorder does see an INSERT when a route is public, so
    # the empty lists above mean "refused", not "nothing is ever recorded".
    assert (public_post["status"], public_post["location"]) == (302, "/book")
    assert _inserts(public_post) == ["INSERT INTO booking_requests (name) VALUES ($1) RETURNING id"]


def test_a_page_that_lists_roles_without_auth_required_is_protected_on_every_verb(app_dir):
    """`allowed_roles: ["admin"]` and no `auth_required` used to be a public
    page: a visitor could open it and post to it. A list of roles is a
    restriction, so the page asks for a login and for one of those roles on
    GET and on POST alike."""
    note = {"_csrf": "t0ken", "body": "hello"}
    anon_get, anon_post, editor_post, admin_get, admin_post = _run(app_dir, "pages", [
        {"method": "GET", "path": "/staff-note", "session": ANON},
        {"method": "POST", "path": "/staff-note", "session": ANON, "form": note},
        {"method": "POST", "path": "/staff-note", "session": EDITOR, "form": note},
        {"method": "GET", "path": "/staff-note", "session": ADMIN},
        {"method": "POST", "path": "/staff-note", "session": ADMIN, "form": note},
    ])
    assert (anon_get["status"], anon_get["location"]) == (302, "/login")
    assert (anon_post["status"], anon_post["location"]) == (302, "/login") and anon_post["sql"] == []
    assert (editor_post["status"], editor_post["location"]) == (302, "/login") and editor_post["sql"] == []
    assert admin_get["status"] == 200
    assert (admin_post["status"], admin_post["location"]) == (302, "/staff-note")
    assert _inserts(admin_post) == ["INSERT INTO member_notes (body) VALUES ($1) RETURNING id"]


def test_a_public_form_stores_only_the_columns_its_page_lists(app_dir):
    """Whatever else a visitor puts in the request body (a flag that puts the
    row on the home page, a sort order, the engine's own columns) is not a
    column of the INSERT."""
    (post,) = _run(app_dir, "pages", [
        {"method": "POST", "path": "/tell", "session": ANON,
         "form": {"_csrf": "t0ken", "title": "My story", "body": "Once.", "featured_on_home": "on",
                  "sort_order": "1", "published": "on", "id": "9", "created_at": "2001-01-01"}},
    ])
    assert (post["status"], post["location"]) == (302, "/tell")
    assert _inserts(post) == ["INSERT INTO stories (title, body, published) VALUES ($1, $2, FALSE) RETURNING id"]


def _order_by(result: dict) -> str:
    """The ORDER BY clause of the statement that lists the rows."""
    listing = [sql for sql in result["sql"] if sql.startswith("SELECT * FROM")]
    assert len(listing) == 1, result["sql"]
    return listing[0].split(" ORDER BY ", 1)[1].split(" LIMIT ", 1)[0]


def test_a_visitor_can_only_sort_a_public_list_by_a_column_it_prints(app_dir):
    """`?sort=` took any column of the model: an ordering oracle on a column
    the page never shows. A visitor's list takes the columns the list prints
    (the title, a date), the admin's takes every column that can be ordered."""
    def listing(query, session, mount):
        return _run(app_dir, "resources", [{"method": "GET", "path": mount + "/" + query, "session": session}],
                    mount=mount)[0]

    public = {sort: _order_by(listing(f"?sort={sort}&order=desc", ANON, "/resources"))
              for sort in ("secret_note", "price", "summary", "details", "no_such_column", "title", "id", "created_at")}
    assert public["title"] == "title DESC"
    assert public["id"] == "id DESC" and public["created_at"] == "created_at DESC"
    # Anything else sorts like no request at all.
    default = _order_by(listing("", ANON, "/resources"))
    for hidden in ("secret_note", "price", "summary", "details", "no_such_column"):
        assert public[hidden] == default, hidden
    # The admin's list takes every column that can be ordered.
    admin = {sort: _order_by(listing(f"?sort={sort}", ADMIN, "/admin/resources"))
             for sort in ("secret_note", "price", "summary", "title", "created_at", "details")}
    assert admin["secret_note"] == "secret_note ASC" and admin["price"] == "price ASC"
    assert admin["summary"] == "summary ASC" and admin["created_at"] == "created_at ASC"
    # json has no ordering operator: not a column to sort by, for anyone.
    assert admin["details"] == default


def test_protected_form_still_checks_csrf_for_the_admin(app_dir):
    (wrong_token,) = _run(app_dir, "pages", [
        {"method": "POST", "path": "/members-form", "session": ADMIN,
         "form": {"_csrf": "not-the-token", "body": "hello"}},
    ])
    assert wrong_token["status"] == 403
    assert wrong_token["sql"] == []


# Test passwords are assembled at run time; nothing here is a credential.
CURRENT = " ".join(["first", "phrase", "for", "the", "account"])
_REPLACEMENT = " ".join(["second", "phrase", "for", "the", "account"])
NEW = {"_csrf": "t0ken", "new_password": _REPLACEMENT, "confirm_password": _REPLACEMENT}


def _now_ms() -> int:
    return int(time.time() * 1000)


def test_set_password_needs_the_current_password_unless_a_login_link_just_signed_in(app_dir):
    """POST /set-password replaced the password for ANY signed-in session.
    It now does so without the current password only for an account that
    has none, or within 15 minutes of a login-link sign-in."""
    has_password = {"userPassword": CURRENT}
    fresh_link = dict(ADMIN, loginLinkAt=_now_ms() - 60 * 1000)
    stale_link = dict(ADMIN, loginLinkAt=_now_ms() - 16 * 60 * 1000)
    results = _run(app_dir, "auth", [
        {"method": "POST", "path": "/set-password", "session": ANON, "form": NEW, "db": has_password},
        {"method": "POST", "path": "/set-password", "session": ADMIN, "form": NEW, "db": has_password},
        {"method": "POST", "path": "/set-password", "session": ADMIN,
         "form": dict(NEW, current_password="a wrong guess"), "db": has_password},
        {"method": "POST", "path": "/set-password", "session": stale_link, "form": NEW, "db": has_password},
        {"method": "POST", "path": "/set-password", "session": ADMIN,
         "form": dict(NEW, current_password=CURRENT), "db": has_password},
        {"method": "POST", "path": "/set-password", "session": fresh_link, "form": NEW, "db": has_password},
        {"method": "POST", "path": "/set-password", "session": ADMIN, "form": NEW, "db": {}},
        {"method": "POST", "path": "/set-password", "session": ADMIN, "form": NEW, "db": {"user": "missing"}},
    ])
    anon, no_current, wrong_current, stale, right_current, fresh, first_time, no_user = results

    assert (anon["status"], anon["location"]) == (302, "/login")
    assert anon["sql"] == []

    for refused in (no_current, wrong_current, stale):
        assert refused["status"] == 200
        assert refused["body"] == {"view": "set-password", "error": "Current password is incorrect.",
                                   "needs_current": True}
        assert _password_updates(refused) == []

    for accepted in (right_current, fresh, first_time):
        assert (accepted["status"], accepted["location"]) == (302, "/admin")
        assert len(_password_updates(accepted)) == 1
    # The login-link allowance is single use.
    assert "loginLinkAt" not in fresh["session"]

    assert (no_user["status"], no_user["location"]) == (302, "/login")
    assert _password_updates(no_user) == []


def test_set_password_form_shows_the_current_password_field_only_when_required(app_dir):
    fresh_link = dict(ADMIN, loginLinkAt=_now_ms())
    with_password, after_link, first_time, anon = _run(app_dir, "auth", [
        {"method": "GET", "path": "/set-password", "session": ADMIN, "db": {"userPassword": CURRENT}},
        {"method": "GET", "path": "/set-password", "session": fresh_link, "db": {"userPassword": CURRENT}},
        {"method": "GET", "path": "/set-password", "session": ADMIN, "db": {}},
        {"method": "GET", "path": "/set-password", "session": ANON},
    ])
    assert with_password["body"] == {"view": "set-password", "error": None, "needs_current": True}
    assert after_link["body"]["needs_current"] is False
    assert first_time["body"]["needs_current"] is False
    assert (anon["status"], anon["location"]) == (302, "/login")


def test_only_a_login_link_sign_in_stamps_the_session(app_dir):
    """The stamp /set-password trusts is written by POST /magic/consume and
    by nothing else: a password login does not carry it, and a value planted
    in the pre-login session does not survive the sign-in."""
    planted = dict(ANON, loginLinkAt=_now_ms())
    by_link, by_password = _run(app_dir, "auth", [
        {"method": "POST", "path": "/magic/consume", "session": ANON,
         "form": {"_csrf": "t0ken", "token": "f" * 64}},
        {"method": "POST", "path": "/login", "session": planted, "db": {"userPassword": CURRENT},
         "form": {"_csrf": "t0ken", "email": "owner@example.com", "password": CURRENT}},
    ])
    assert (by_link["status"], by_link["location"]) == (302, "/admin")
    assert by_link["session"]["userId"] == 1
    assert abs(by_link["session"]["loginLinkAt"] - _now_ms()) < 60 * 1000

    assert (by_password["status"], by_password["location"]) == (302, "/admin")
    assert by_password["session"]["userId"] == 1
    assert "loginLinkAt" not in by_password["session"]


def test_set_password_is_rate_limited_like_login(app_dir):
    """With the current password required, the route answers "right or
    wrong" for a guess, so it sits behind the same limiter as /login."""
    guess = {"method": "POST", "path": "/set-password", "session": ADMIN,
             "form": dict(NEW, current_password="guess"), "db": {"userPassword": CURRENT}}
    results = _run(app_dir, "auth", [guess] * 11)
    assert [r["status"] for r in results[:10]] == [200] * 10
    assert results[10]["status"] == 429
    assert results[10]["sql"] == []


# ---------------------------------------------------------------------------
# Links the site sends by email: built on its own address, not on the Host
# header of the request that caused the email
# ---------------------------------------------------------------------------

# Not a credential: a random value that only has to be non-empty.
MAIL_ENV = {"RESEND_API_KEY": "test-" + secrets.token_hex(12), "ADMIN_EMAIL": "owner@example.com"}
SPOOFED = {"Host": "evil.example", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example"}
STORED_SITE_URL = {"siteSettings": [{"key": "site_url", "value": "https://stored-site.example/"}]}


def _one_mail(result: dict) -> dict:
    assert [m["url"] for m in result["mails"]] == ["https://api.resend.com/emails"], result["mails"]
    return result["mails"][0]["body"]


@pytest.mark.parametrize("label, env, db, expected", [
    ("SITE_ORIGIN", {"SITE_ORIGIN": "https://owner-site.example"}, {}, "https://owner-site.example"),
    ("the stored site_url", {}, STORED_SITE_URL, "https://stored-site.example"),
    ("no address", {}, {}, None),
    ("a SITE_ORIGIN that is not http(s)", {"SITE_ORIGIN": "javascript:alert(1)//x"}, {}, None),
])
def test_a_form_notification_links_to_the_sites_own_address_and_never_to_the_request_host(
        app_dir, label, env, db, expected):
    """A visitor chooses the Host and X-Forwarded-* headers of the form post
    that triggers the owner's notification. The "View in admin" link uses
    SITE_ORIGIN (which deploy() sets), else the stored site_url, else no link."""
    (post,) = _run(app_dir, "pages", [
        {"method": "POST", "path": "/book", "session": ANON, "headers": SPOOFED, "expectMail": True, "db": db,
         "form": {"_csrf": "t0ken", "name": "Visitor"}},
    ], env=dict(MAIL_ENV, **env))
    assert (post["status"], post["location"]) == (302, "/book")
    mail = _one_mail(post)
    assert mail["to"] == ["owner@example.com"]
    assert "evil.example" not in json.dumps(mail), label
    if expected:
        assert f'href="{expected}/admin/booking-requests/7"' in mail["html"], label
    else:
        assert "View in admin" not in mail["html"] and "href=" not in mail["html"], label


@pytest.mark.parametrize("label, env, db, expected", [
    ("SITE_ORIGIN", {"SITE_ORIGIN": "https://owner-site.example"}, {}, "https://owner-site.example"),
    ("the stored site_url", {}, STORED_SITE_URL, "https://stored-site.example"),
])
def test_a_requested_login_link_points_at_the_sites_own_address_whatever_the_host_header(
        app_dir, label, env, db, expected):
    (post,) = _run(app_dir, "auth", [
        {"method": "POST", "path": "/request-magic-link", "session": ANON, "headers": SPOOFED,
         "expectMail": True, "db": db, "form": {"_csrf": "t0ken", "email": "OWNER@example.com"}},
    ], env=dict(MAIL_ENV, **env))
    assert (post["status"], post["location"]) == (302, "/magic-link-sent")
    mail = _one_mail(post)
    assert mail["to"] == ["owner@example.com"]
    assert "evil.example" not in json.dumps(mail), label
    assert re.search(re.escape(expected) + r"/magic\?token=[0-9a-f]{64}\"", mail["html"]), label


def test_with_no_known_address_a_login_link_is_not_emailed_and_not_logged(app_dir):
    """A mail key but neither SITE_ORIGIN nor a stored site_url: the app
    cannot build a link it trusts, so it sends none (and still does not
    write the token to its log, because a mail key is set)."""
    (post,), log = _run_with_log(app_dir, "auth", [
        {"method": "POST", "path": "/request-magic-link", "session": ANON, "headers": SPOOFED,
         "expectMail": True, "form": {"_csrf": "t0ken", "email": "owner@example.com"}},
    ], env=MAIL_ENV)
    assert (post["status"], post["location"]) == (302, "/magic-link-sent")
    assert post["mails"] == []
    assert "[magic-link] not emailed for user 1" in log
    assert not re.search(r"[0-9a-f]{64}", log)


# ---------------------------------------------------------------------------
# Admin record routes: the id, and what a database error shows
# ---------------------------------------------------------------------------

ADMIN_RECORDS = "/admin/booking-requests"


def test_a_record_route_answers_404_for_an_id_that_is_not_a_number_before_any_query(app_dir):
    """GET /:id checked its id; edit, update and delete passed any value to
    Postgres (a 500 page). One router.param check now answers 404 for all of
    them before a statement is sent."""
    token = {"_csrf": "t0ken"}
    junk_ids = ("abc", "1e3", "12345678901", "-1", "7;DROP")
    plan = []
    for junk in junk_ids:
        plan += [
            {"method": "GET", "path": f"{ADMIN_RECORDS}/{junk}", "session": ADMIN},
            {"method": "GET", "path": f"{ADMIN_RECORDS}/{junk}/edit", "session": ADMIN},
            {"method": "POST", "path": f"{ADMIN_RECORDS}/{junk}/update", "session": ADMIN,
             "form": dict(token, name="x")},
            {"method": "POST", "path": f"{ADMIN_RECORDS}/{junk}/delete", "session": ADMIN, "form": token},
        ]
    plan.append({"method": "GET", "path": f"{ADMIN_RECORDS}/7/edit", "session": ADMIN})
    results = _run(app_dir, "booking-requests", plan, mount=ADMIN_RECORDS)
    *refused, control = results
    for request, reply in zip(plan, refused):
        assert (reply["status"], reply["body"]["view"]) == (404, "error-404"), request["path"]
        assert reply["sql"] == [], request["path"]
    # Control: a numeric id reaches the query.
    assert control["sql"] == ["SELECT * FROM booking_requests WHERE id = $1"]


# A server error text that names a constraint, a column and a value.
DB_ERROR = 'duplicate key value violates unique constraint "booking_requests_secret_idx" (name)=(Kept Private)'


def test_a_database_error_goes_to_the_log_and_the_admin_sees_a_fixed_message(app_dir):
    form = {"_csrf": "t0ken", "name": "Changed"}
    update, delete, delete_referenced, create = _run(app_dir, "booking-requests", [
        {"method": "POST", "path": f"{ADMIN_RECORDS}/7/update", "session": ADMIN, "form": form,
         "db": {"failOn": "UPDATE booking_requests", "failWith": DB_ERROR}},
        {"method": "POST", "path": f"{ADMIN_RECORDS}/7/delete", "session": ADMIN, "form": {"_csrf": "t0ken"},
         "db": {"failOn": "DELETE FROM booking_requests", "failWith": DB_ERROR}},
        {"method": "POST", "path": f"{ADMIN_RECORDS}/7/delete", "session": ADMIN, "form": {"_csrf": "t0ken"},
         "db": {"failOn": "DELETE FROM booking_requests", "failWith": DB_ERROR, "failCode": "23503"}},
        {"method": "POST", "path": ADMIN_RECORDS, "session": ADMIN, "form": form,
         "db": {"failOn": "INSERT INTO booking_requests", "failWith": DB_ERROR}},
    ], mount=ADMIN_RECORDS)
    assert (update["status"], update["location"]) == (302, f"{ADMIN_RECORDS}/7/edit")
    assert update["session"]["flash_error"] == "Could not save. Please check the fields and try again."
    assert (delete["status"], delete["location"]) == (302, ADMIN_RECORDS)
    assert delete["session"]["flash_error"] == "Could not delete. Please try again."
    assert delete_referenced["session"]["flash_error"] == "Could not delete: other entries still refer to this one."
    assert create["body"]["view"] == "booking-requests/new"
    assert create["body"]["error"] == "Could not save. Please check the fields and try again."

    (settings,), settings_log = _run_with_log(app_dir, "admin-settings", [
        {"method": "POST", "path": "/admin/settings", "session": ADMIN,
         "form": {"_csrf": "t0ken", "site_name": "Renamed"},
         "db": {"failOn": "INSERT INTO site_settings", "failWith": DB_ERROR}},
    ], mount="/admin")
    assert settings["session"]["flash_error"] == "Could not save the settings. Please try again."
    (cleared,), cleared_log = _run_with_log(app_dir, "admin-dashboard", [
        {"method": "POST", "path": "/admin/clear-all", "session": ADMIN, "form": {"_csrf": "t0ken"},
         "db": {"failOn": "DELETE FROM", "failWith": DB_ERROR}},
    ], mount="/admin")
    assert cleared["session"]["flash_error"] == "Could not clear the data. Please try again."
    # The detail is in the server's log, for the operator.
    assert DB_ERROR in settings_log and DB_ERROR in cleared_log


# ---------------------------------------------------------------------------
# Opening a login link
# ---------------------------------------------------------------------------

def test_opening_a_login_link_is_rate_limited_per_address(app_dir):
    """HEAD and GET /magic say whether a token is valid without spending it.
    They share one limit of 60 requests per 15 minutes per address."""
    link = "/magic?token=" + "e" * 64
    plan = ([{"method": "HEAD", "path": link, "session": ANON}] * 30
            + [{"method": "GET", "path": link, "session": ANON}] * 30
            + [{"method": "HEAD", "path": link, "session": ANON},
               {"method": "GET", "path": link, "session": ANON}])
    results = _run(app_dir, "auth", plan)
    assert [r["status"] for r in results[:30]] == [410] * 30
    assert all((r["status"], r["location"]) == (302, "/login?error=invalid-token") for r in results[30:60])
    assert [r["status"] for r in results[60:]] == [429, 429]
    assert results[60]["sql"] == [] and results[61]["sql"] == []


# ---------------------------------------------------------------------------
# Honeypot on the contact form and the newsletter signup
# ---------------------------------------------------------------------------

HONEYPOT = {"website_url_hp": "http://spam.example"}


def test_a_filled_honeypot_on_the_contact_form_is_answered_like_a_message_and_not_stored(app_dir):
    message = {"_csrf": "t0ken", "name": "Pat Doe", "email": "pat@example.org", "message": "Hello there"}
    bot, person = _run(app_dir, "pages", [
        {"method": "POST", "path": "/contact", "session": ANON, "form": dict(message, **HONEYPOT)},
        {"method": "POST", "path": "/contact", "session": ANON, "form": message},
    ])
    assert bot["status"] == 200 and bot["body"]["flashSuccess"] == person["body"]["flashSuccess"]
    assert _inserts(bot) == []
    assert [sql.split(" (")[0] for sql in _inserts(person)] == ["INSERT INTO contact_inquiries"]


@pytest.fixture(scope="module")
def healer_dir(tmp_path_factory):
    """The healer sample, which shows the newsletter signup; db.js replaced
    by the recorder."""
    os.environ["CAS_LINT_EXTERNAL_URLS"] = "skip"
    out = tmp_path_factory.mktemp("runtime-healer") / "app"
    build_app(json.loads((FIXTURES / "sample-healer-spec.json").read_text(encoding="utf-8")), output_dir=out)
    (out / "db.js").write_text(_DB_STUB, encoding="utf-8")
    return out


def test_a_filled_honeypot_on_the_newsletter_signup_is_answered_like_a_signup_and_not_stored(healer_dir):
    signup = {"_csrf": "t0ken", "email": "reader@example.org"}
    bot, person = _run(healer_dir, "pages", [
        {"method": "POST", "path": "/newsletter-signup", "session": ANON, "form": dict(signup, **HONEYPOT)},
        {"method": "POST", "path": "/newsletter-signup", "session": ANON, "form": signup},
    ])
    assert (bot["status"], bot["location"]) == (302, "/#newsletter-success")
    assert (person["status"], person["location"]) == (302, "/#newsletter-success")
    assert bot["sql"] == []
    assert [sql.split(" (")[0] for sql in _inserts(person)] == ["INSERT INTO newsletter_subscribers"]


def test_the_contact_form_and_the_newsletter_signup_carry_the_honeypot_field(app_dir, healer_dir):
    """The field the handlers check is in the forms the widgets render (the
    booted-site tests read it from the rendered pages)."""
    for view in (app_dir / "views" / "widgets" / "form" / "contact.ejs",
                 healer_dir / "views" / "widgets" / "cta" / "newsletter-inline.ejs"):
        text = view.read_text(encoding="utf-8")
        assert 'name="website_url_hp" value="" tabindex="-1" autocomplete="off"' in text, view.name
        assert text.index('name="website_url_hp"') < text.index("</form>"), view.name


# ---------------------------------------------------------------------------
# A changed password signs the account's other sessions out
# ---------------------------------------------------------------------------

END_OTHER_SESSIONS = "DELETE FROM user_sessions WHERE sid <> $1 AND sess->>'userId' = $2"


def test_a_password_change_ends_the_accounts_other_sessions(app_dir):
    """Both places a password is set send the statement, keeping the session
    that made the change. (test_generated_site_security_e2e.py runs it
    against the real session store.)"""
    (first_time,) = _run(app_dir, "auth", [
        {"method": "POST", "path": "/set-password", "session": ADMIN, "sid": "sid-of-the-changer",
         "form": NEW, "db": {}},
    ])
    assert (first_time["status"], first_time["location"]) == (302, "/admin")
    ended = [i for i, sql in enumerate(first_time["sql"]) if sql == END_OTHER_SESSIONS]
    assert len(ended) == 1 and first_time["params"][ended[0]] == ["sid-of-the-changer", "1"]
    updated = first_time["sql"].index(_password_updates(first_time)[0])
    assert updated < ended[0], "the sessions end after the new hash is stored"

    (admin_change,) = _run(app_dir, "admin-dashboard", [
        {"method": "POST", "path": "/admin/change-password", "session": ADMIN, "sid": "sid-of-the-changer",
         "form": {"_csrf": "t0ken", "new_password": _REPLACEMENT}, "db": {}},
    ], mount="/admin")
    assert (admin_change["status"], admin_change["location"]) == (302, "/admin")
    ended = [i for i, sql in enumerate(admin_change["sql"]) if sql == END_OTHER_SESSIONS]
    assert len(ended) == 1 and admin_change["params"][ended[0]] == ["sid-of-the-changer", "1"]
    assert admin_change["session"]["flash_success"] == "Password updated."

    # A refused change ends nothing.
    (refused,) = _run(app_dir, "auth", [
        {"method": "POST", "path": "/set-password", "session": ADMIN, "form": NEW,
         "db": {"userPassword": CURRENT}},
    ])
    assert END_OTHER_SESSIONS not in refused["sql"]
