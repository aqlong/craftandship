"""The worker's login probe against the login routes a generated site really has.

tests/test_probe_guard.py runs cli/deploy.py's verify_magic_link_auth against
a small site written for the test. This file closes the other half of the
seam: the routes come from compose/build.py. A v2 site is built, and its
emitted routes/auth.js (with the emitted middleware/auth.js and
middleware/rate-limit.js) is mounted in an Express app on loopback. The probe
is the real one.

Real: the emitted router and middleware (the POST /magic/consume handler, its
CSRF check, its rate limiter, establishSession), Express, express-session,
and the token pair made by the worker's own _generate_magic_link_token.

Stand-ins, and therefore not covered: the database (a few statements the
login routes use, answered from memory), the views (GET /login answers with
the hidden `_csrf` input only), the middleware in the emitted server.js that
puts a CSRF value into a new session (three lines, repeated in the harness),
and the site's HTTP logger (the harness records each request line itself).

Skipped when node or web/node_modules (`cd web && npm ci`) is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "queue"), str(ROOT / "cli"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

WEB_MODULES = ROOT / "web" / "node_modules"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not all((WEB_MODULES / name).exists()
                            for name in ("express", "express-session", "express-rate-limit", "bcrypt")),
    reason="needs node and web/node_modules with express, express-session, express-rate-limit "
           "and bcrypt (run `cd web && npm ci`)",
)

# Replaces the emitted db.js: the login routes' statements, answered from memory.
_DB_STUB = r"""
const tokens = JSON.parse(process.env.TEST_TOKEN_HASHES).map(function (hash, i) {
  return { id: i + 1, user_id: 1, token_hash: hash, used_at: null,
           expires_at: new Date(Date.now() + 3600000) };
});
const db = {
  tokens: tokens,
  pool: {},
  query: async function (text, params) {
    const sql = String(text).replace(/\s+/g, ' ').trim();
    if (sql.indexOf('FROM magic_link_tokens t') !== -1 && sql.indexOf('WHERE t.token_hash = $1') !== -1) {
      const found = tokens.filter(function (t) { return t.token_hash === params[0]; });
      return { rows: found.map(function (t) {
        return { id: t.id, user_id: t.user_id, used_at: t.used_at, expires_at: t.expires_at,
                 email: 'owner@example.com', role: 'admin', password_set: false };
      }), rowCount: found.length };
    }
    if (sql.indexOf('UPDATE magic_link_tokens SET used_at = NOW()') === 0) {
      const row = tokens.filter(function (t) {
        return t.id === params[0] && t.used_at === null && t.expires_at > new Date();
      })[0];
      if (!row) return { rows: [], rowCount: 0 };
      row.used_at = new Date();
      return { rows: [{ id: row.id }], rowCount: 1 };
    }
    return { rows: [], rowCount: 0 };
  },
};
module.exports = db;
"""

_HARNESS = r"""
const path = require('path');
const crypto = require('crypto');
const express = require('express');
const session = require('express-session');
const appDir = process.argv[1];
console.log = console.info = console.debug = console.error;
const db = require(path.join(appDir, 'db.js'));

const accessLog = [];
const app = express();
app.use(function (req, res, next) {
  if (req.path.indexOf('/__') !== 0) accessLog.push(req.method + ' ' + req.originalUrl);
  next();
});
app.use(express.urlencoded({ extended: true }));
app.use(session({ secret: crypto.randomBytes(16).toString('hex'), resave: false, saveUninitialized: true }));
// What the emitted server.js does for every request: a CSRF value per session.
app.use(function (req, res, next) {
  if (!req.session.csrfToken) req.session.csrfToken = crypto.randomBytes(32).toString('hex');
  res.locals.csrfToken = req.session.csrfToken;
  res.locals.settings = { site_name: 'Test' };
  res.render = function (view) {
    res.type('html').send('<form method="post"><input type="hidden" name="_csrf" value="' +
                          res.locals.csrfToken + '"><!-- view: ' + view + ' --></form>');
  };
  next();
});
app.get('/__state', function (req, res) {
  res.json({ accessLog: accessLog,
             used: db.tokens.map(function (t) { return t.used_at !== null; }) });
});
app.use('/', require(path.join(appDir, 'routes', 'auth.js')));
app.use(function (req, res) { res.status(404).send('no route'); });
const server = app.listen(0, '127.0.0.1', function () {
  process.stdout.write(String(server.address().port) + '\n');
});
process.stdin.on('end', function () { server.close(); process.exit(0); });
process.stdin.resume();
"""


@pytest.fixture(scope="module")
def built_site(tmp_path_factory):
    from compose.build import build_app

    os.environ["CAS_LINT_EXTERNAL_URLS"] = "skip"
    spec = json.loads((ROOT / "compose" / "fixtures" / "sample-coach-spec.json").read_text(encoding="utf-8"))
    out = tmp_path_factory.mktemp("login-probe") / "app"
    build_app(spec, output_dir=out)
    (out / "db.js").write_text(_DB_STUB, encoding="utf-8")
    return out


@pytest.fixture()
def site(built_site, monkeypatch):
    import monitor.ping as ping_mod
    import worker_pg

    verify = worker_pg._generate_magic_link_token()
    customer = worker_pg._generate_magic_link_token()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "NODE_PATH": str(WEB_MODULES),
        "NODE_ENV": "test",
        "TEST_TOKEN_HASHES": json.dumps([verify["token_hash"], customer["token_hash"]]),
    }
    proc = subprocess.Popen(
        [NODE, "-e", _HARNESS, str(built_site)], cwd=str(built_site), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        port_line = proc.stdout.readline().strip()
        assert port_line.isdigit(), proc.stderr.read()[-1500:]
        # The probe refuses loopback; let the address policy accept it for
        # this listener. Everything else about the probe is as in production.
        real = ping_mod.is_public_address
        monkeypatch.setattr(ping_mod, "is_public_address",
                            lambda ip: ip in ("127.0.0.1", "::1") or real(ip))
        base = f"http://127.0.0.1:{port_line}"

        def state():
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(base + "/__state", timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))

        yield {"base": base, "verify": verify, "customer": customer, "state": state}
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def test_the_probe_signs_in_through_the_emitted_consume_route(site):
    """The emitted POST /magic/consume accepts what the probe sends (form
    fields `token` and `_csrf`, the session cookie from GET /login), uses the
    token up and redirects away from /login; the probe reads that as "ok". The
    token is in no request line, and the customer's token is untouched."""
    import deploy

    raw = site["verify"]["raw_token"]
    assert deploy.verify_magic_link_auth(site["base"], raw, timeout=10) == "ok"

    after = site["state"]()
    assert after["accessLog"] == ["GET /login", "POST /magic/consume"], after["accessLog"]
    assert not any(raw in line for line in after["accessLog"])
    assert after["used"] == [True, False], "verification token used up, customer token untouched"


def test_a_used_token_and_an_unknown_token_fail_against_the_emitted_route(site):
    import secrets

    import deploy

    raw = site["verify"]["raw_token"]
    assert deploy.verify_magic_link_auth(site["base"], raw, timeout=10) == "ok"
    assert deploy.verify_magic_link_auth(site["base"], raw, timeout=10) == "failed"
    assert deploy.verify_magic_link_auth(site["base"], secrets.token_hex(32), timeout=10) == "failed"
    assert site["state"]()["used"] == [True, False]
