"""
Local v2 end-to-end test harness.

For each `compose/fixtures/sample-*.json` this test:
  1. Composes the app into a temp directory via `compose.build.build_app`.
  2. Provisions an isolated local Postgres database (`cas_v2_e2e_<rand>`).
  3. Symlinks a session-cached `node_modules` into the build dir so we
     only run `npm install` ONCE per test session (saves 30-45s per fixture).
  4. Runs `node db_migrate.js` against the isolated DB.
  5. Boots `node server.js` on a free port with `NODE_ENV=development`.
  6. Polls `/health` (30s budget), asserts the server is live.
  7. Asserts HTTP status expectations for:
        - /health -> 200 {"status":"ok"}
        - / (home) -> 200
        - /login -> 200
        - /admin (unauthed) -> 302 to /login
        - /magic (no token) -> 302 to /login?error=invalid-token
        - /magic?token=bogus -> 302 to /login?error=invalid-token
  8. Puts a fresh login token into `magic_link_tokens` and walks the two
     steps a person takes from the emailed link: GET /magic?token=<raw>
     shows a confirmation page and does not spend the token (opening the
     link twice, or a mail scanner fetching it, logs nobody in); the POST
     from that page to /magic/consume, with its CSRF token, spends the
     token and starts the session, and /admin then answers that session.
  9. Replays the spent token, by GET and by POST: both end at
     /login?error=token-already-used.
  10. Cleanup: SIGTERM the server, drop the DB.

A compose change is checked here against a real Node process and a real
Postgres, without a deploy.

RUN MODE
--------
Skipped by default. Run via:

    pytest -m v2_integration tests/test_v2_local_e2e.py

Requires on PATH: `psql`, `createdb`, `dropdb`, `node`, `npm`. If any
is missing the whole module is skipped (keeps CI clean). `npm install`
runs once per session and reaches the npm registry; it is a child
process, so the network guard in the root conftest.py does not stop it
and CAS_TESTS_ALLOW_NETWORK is not needed.

Requires a local Postgres accepting trusted connections for the shell
`$USER` (same assumption as `scripts/cas-local-preview`). The tests
do NOT write any shared/production data; every run uses a uniquely-
named isolated database dropped at teardown.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest
import requests


ROOT = Path(__file__).parent.parent


# --- Module-level skip if local infra is missing ---------------------------

_REQUIRED_CMDS = ("psql", "createdb", "dropdb", "node", "npm")
_MISSING_CMDS = [c for c in _REQUIRED_CMDS if shutil.which(c) is None]
if _MISSING_CMDS:
    pytest.skip(
        f"v2 local e2e requires {_MISSING_CMDS} on PATH; skipping module",
        allow_module_level=True,
    )

# Make compose importable when the test runs.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


FIXTURES_DIR = ROOT / "compose" / "fixtures"
FIXTURES: list[Path] = sorted(FIXTURES_DIR.glob("sample-*.json"))
# v1-shape specs that exercise the translator, custom_pages, form_target
# and belongs_to. These go through
# translate_v1_spec before compose, unlike the v2-shape sample specs
# above which feed compose directly.
V1_FIXTURES: list[Path] = sorted(FIXTURES_DIR.glob("v1-sample-*.json"))


# --- Helpers ---------------------------------------------------------------

def _free_port() -> int:
    """Return a currently-free TCP port on 127.0.0.1 by binding + closing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pg_user() -> str:
    """Local Postgres user; matches the dev shell's $USER by convention
    (same convention as scripts/cas-local-preview)."""
    return os.environ.get("USER") or "postgres"


def _create_isolated_db() -> str:
    """Create a uniquely-named Postgres DB and return its name."""
    name = f"cas_v2_e2e_{secrets.token_hex(4)}"
    subprocess.run(
        ["createdb", name],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return name


def _drop_db(name: str) -> None:
    """Best-effort teardown; never raises."""
    subprocess.run(
        ["dropdb", "--if-exists", name],
        check=False,
        capture_output=True,
        timeout=15,
    )


def _psql_exec(db_name: str, sql: str) -> None:
    """Run a SQL statement against the isolated DB. Raises on failure."""
    subprocess.run(
        ["psql", "-d", db_name, "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _psql_value(db_name: str, sql: str) -> str:
    """Run a query that returns one value against the isolated DB and
    return it as text."""
    done = subprocess.run(
        ["psql", "-d", db_name, "-v", "ON_ERROR_STOP=1", "-At", "-c", sql],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return done.stdout.strip()


def _csrf_token(page_html: str) -> str:
    """The CSRF token of the first form on a page of the generated site."""
    import re
    found = re.search(r'name="_csrf" value="([^"]+)"', page_html)
    assert found, f"no CSRF token in the page:\n{page_html[:800]}"
    return found.group(1)


def _wait_for_health(base_url: str, timeout: float = 30.0) -> tuple[bool, str]:
    """Poll /health until 200 + "ok" or timeout. Returns (ok, last_error)."""
    deadline = time.monotonic() + timeout
    last_err = "no attempts made"
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{base_url}/health", timeout=2)
            if r.status_code == 200 and "ok" in r.text.lower():
                return True, ""
            last_err = f"status={r.status_code} body={r.text[:100]!r}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    return False, last_err


def _kill(proc: subprocess.Popen) -> None:
    """SIGTERM, then SIGKILL if still alive. Never raises."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


# --- Session-scoped npm cache ---------------------------------------------

@pytest.fixture(scope="session")
def v2_e2e_node_modules(tmp_path_factory) -> Path:
    """Install node_modules ONCE per test session and reuse across fixtures.

    Per-fixture builds symlink to this cached tree instead of running
    `npm install` fresh (saves ~30-45s per fixture). Node's CommonJS
    resolver follows symlinks, and `compose` emits a fully self-contained
    app whose dependencies are identical across all fixtures (same
    package.json emitter).
    """
    from compose.build import build_app

    cache_dir = tmp_path_factory.mktemp("v2-e2e-node-modules")
    probe_dir = cache_dir / "probe-build"
    build_app(str(FIXTURES[0]), output_dir=str(probe_dir))

    r = subprocess.run(
        ["npm", "install", "--silent", "--no-audit", "--no-fund"],
        cwd=probe_dir,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert r.returncode == 0, f"npm install failed: {r.stderr[:2000]}"

    node_modules = probe_dir / "node_modules"
    assert node_modules.is_dir(), "npm install did not create node_modules"
    return node_modules


# --- The test -------------------------------------------------------------

@pytest.mark.v2_integration
@pytest.mark.parametrize("fixture_path", FIXTURES, ids=lambda p: p.stem)
def test_v2_fixture_boots_and_serves_routes(
    fixture_path: Path,
    v2_e2e_node_modules: Path,
    tmp_path: Path,
) -> None:
    """End-to-end: compose + migrate + boot + probe routes + magic-link flow.

    Any failure surfaces a specific assertion (not a vague 500) so the
    diagnosis path is short. Attachments on failure: server stdout +
    stderr are included in the pytest output via the assertion message.
    """
    from compose.build import build_app

    # 1. Compose
    build_dir = tmp_path / "build"
    build_app(str(fixture_path), output_dir=str(build_dir))

    # Compose must produce the core files we then exercise.
    for required in ("server.js", "db_migrate.js", "package.json", "spec.json"):
        assert (build_dir / required).is_file(), (
            f"compose output for {fixture_path.name} is missing {required}"
        )

    # 2. Provision an isolated DB
    db_name = _create_isolated_db()

    # 3. Symlink cached node_modules
    (build_dir / "node_modules").symlink_to(v2_e2e_node_modules)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "DATABASE_URL": f"postgresql://{_pg_user()}@localhost:5432/{db_name}",
        "SESSION_SECRET": "v2-e2e-test-secret",
        "PORT": str(port),
        # NODE_ENV=development keeps express-session cookies non-Secure
        # so HTTP localhost can persist them (prod runs over HTTPS and
        # correctly sets Secure).
        "NODE_ENV": "development",
    }

    server: subprocess.Popen | None = None
    try:
        # 4. Migrate
        mr = subprocess.run(
            ["node", "db_migrate.js"],
            cwd=build_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert mr.returncode == 0, (
            f"db_migrate failed for {fixture_path.name}:\n"
            f"stdout: {mr.stdout}\nstderr: {mr.stderr}"
        )
        assert "Schema migration complete" in mr.stdout, (
            f"db_migrate completed but did not log success marker:\n{mr.stdout}"
        )

        # 5. Boot server
        server = subprocess.Popen(
            ["node", "server.js"],
            cwd=build_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # 6. Health
        ok, last_err = _wait_for_health(base_url, timeout=30.0)
        if not ok:
            # Pull whatever the server already printed to aid debugging.
            try:
                out = server.stdout.read() if server.stdout else ""
                err = server.stderr.read() if server.stderr else ""
            except Exception:
                out, err = "", ""
            pytest.fail(
                f"{fixture_path.name}: /health never came up within 30s "
                f"(last probe error: {last_err})\n"
                f"server stdout: {out[:2000]}\n"
                f"server stderr: {err[:2000]}"
            )

        # /health body content
        r_health = requests.get(f"{base_url}/health", timeout=5)
        assert r_health.status_code == 200
        assert "ok" in r_health.text.lower()

        # 7. Route assertions
        r_home = requests.get(f"{base_url}/", timeout=5, allow_redirects=False)
        assert r_home.status_code in (200, 302), (
            f"{fixture_path.name}: / returned {r_home.status_code}"
        )

        r_login = requests.get(f"{base_url}/login", timeout=5)
        assert r_login.status_code == 200, (
            f"{fixture_path.name}: /login returned {r_login.status_code}"
        )

        r_admin = requests.get(f"{base_url}/admin", timeout=5, allow_redirects=False)
        assert r_admin.status_code == 302, (
            f"{fixture_path.name}: /admin unauthed should 302, got {r_admin.status_code}"
        )
        assert "/login" in r_admin.headers.get("Location", ""), (
            f"{fixture_path.name}: /admin should redirect to /login, got "
            f"{r_admin.headers.get('Location')!r}"
        )

        # /magic with no token → 302 to /login?error=invalid-token
        r_magic_empty = requests.get(
            f"{base_url}/magic", timeout=5, allow_redirects=False
        )
        assert r_magic_empty.status_code == 302, (
            f"{fixture_path.name}: /magic (no token) should 302, got "
            f"{r_magic_empty.status_code}"
        )
        assert "invalid-token" in r_magic_empty.headers.get("Location", ""), (
            f"{fixture_path.name}: /magic (no token) should redirect with "
            f"invalid-token, got {r_magic_empty.headers.get('Location')!r}"
        )

        # /magic with bogus token → same
        r_magic_bogus = requests.get(
            f"{base_url}/magic?token=bogus", timeout=5, allow_redirects=False
        )
        assert r_magic_bogus.status_code == 302
        assert "invalid-token" in r_magic_bogus.headers.get("Location", "")

        # 8. Put a valid login token in the database and walk the two steps
        #    a person takes from the emailed link.
        raw = secrets.token_hex(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        _psql_exec(
            db_name,
            "INSERT INTO magic_link_tokens (user_id, token_hash, expires_at, created_at) "
            f"SELECT id, '{token_hash}', NOW() + INTERVAL '1 hour', NOW() "
            f"FROM users ORDER BY id LIMIT 1;",
        )
        token_is_spent = (
            f"SELECT used_at IS NOT NULL FROM magic_link_tokens WHERE token_hash = '{token_hash}';"
        )

        # Opening the link shows a confirmation page. It does not log
        # anyone in and does not spend the token, however often it is
        # opened (a mail scanner that fetches the link changes nothing).
        session = requests.Session()
        for attempt in ("first", "second"):
            r_confirm = session.get(
                f"{base_url}/magic?token={raw}", timeout=5, allow_redirects=False
            )
            assert r_confirm.status_code == 200, (
                f"{fixture_path.name}: {attempt} GET /magic?token=<valid> should show the "
                f"confirmation page, got {r_confirm.status_code} "
                f"(Location: {r_confirm.headers.get('Location')!r})"
            )
            assert 'action="/magic/consume"' in r_confirm.text, (
                f"{fixture_path.name}: the confirmation page has no form that posts to "
                f"/magic/consume:\n{r_confirm.text[:800]}"
            )
        assert _psql_value(db_name, token_is_spent) == "f", (
            f"{fixture_path.name}: opening the link spent the token"
        )
        r_admin_before = session.get(f"{base_url}/admin", timeout=5, allow_redirects=False)
        assert r_admin_before.status_code == 302 and "/login" in r_admin_before.headers.get("Location", ""), (
            f"{fixture_path.name}: opening the link must not start a session"
        )

        # The POST from that page needs its CSRF token ...
        r_no_csrf = session.post(
            f"{base_url}/magic/consume", data={"token": raw}, timeout=5, allow_redirects=False
        )
        assert r_no_csrf.status_code == 403, (
            f"{fixture_path.name}: POST /magic/consume without a CSRF token should be "
            f"refused, got {r_no_csrf.status_code}"
        )
        assert _psql_value(db_name, token_is_spent) == "f"

        # ... and with it, spends the token and starts the session. The
        # seeded admin has password_set = TRUE, so the landing is /admin
        # (it would be /set-password for an account that still has to
        # choose one).
        r_consume = session.post(
            f"{base_url}/magic/consume",
            data={"_csrf": _csrf_token(r_confirm.text), "token": raw},
            timeout=5,
            allow_redirects=False,
        )
        assert r_consume.status_code == 302, (
            f"{fixture_path.name}: POST /magic/consume should redirect, got "
            f"{r_consume.status_code}: {r_consume.text[:300]}"
        )
        assert r_consume.headers.get("Location") == "/admin", (
            f"{fixture_path.name}: the login should land on /admin, got "
            f"{r_consume.headers.get('Location')!r}"
        )
        assert _psql_value(db_name, token_is_spent) == "t"

        # The session is real: /admin answers it with the dashboard.
        r_admin_authed = session.get(
            f"{base_url}/admin", timeout=5, allow_redirects=False
        )
        assert r_admin_authed.status_code == 200, (
            f"{fixture_path.name}: /admin with the new session returned "
            f"{r_admin_authed.status_code} (Location: "
            f"{r_admin_authed.headers.get('Location')!r}): the session was not persisted"
        )

        # 9. The token is single use: the link, and a second POST of it from
        #    another browser, both end at the login page.
        r_replay = session.get(
            f"{base_url}/magic?token={raw}", timeout=5, allow_redirects=False
        )
        assert r_replay.status_code == 302
        assert "token-already-used" in r_replay.headers.get("Location", ""), (
            f"{fixture_path.name}: consumed token replay should redirect "
            f"to ?error=token-already-used, got "
            f"{r_replay.headers.get('Location')!r}"
        )
        other = requests.Session()
        r_login_page = other.get(f"{base_url}/login", timeout=5)
        r_replay_post = other.post(
            f"{base_url}/magic/consume",
            data={"_csrf": _csrf_token(r_login_page.text), "token": raw},
            timeout=5,
            allow_redirects=False,
        )
        assert r_replay_post.status_code == 302
        assert "token-already-used" in r_replay_post.headers.get("Location", ""), (
            f"{fixture_path.name}: a second POST of a spent token should redirect to "
            f"?error=token-already-used, got {r_replay_post.headers.get('Location')!r}"
        )
        r_other_admin = other.get(f"{base_url}/admin", timeout=5, allow_redirects=False)
        assert "/login" in r_other_admin.headers.get("Location", ""), (
            f"{fixture_path.name}: replaying a spent token must not start a session"
        )

    finally:
        # 10. Teardown: always runs.
        if server is not None:
            _kill(server)
        _drop_db(db_name)
        # tmp_path is pytest-managed; don't remove the build dir here; 
        # keeping it lets a failing run inspect the artifact.


# --- A tiny smoke test so `pytest -m v2_integration` doesn't fail with ---
# --- "no tests collected" if all fixtures get skipped for some reason. ---

@pytest.mark.v2_integration
def test_v2_e2e_module_loaded_and_fixtures_exist() -> None:
    """Sanity: the fixtures directory has the expected sample specs, and
    the harness module itself imported cleanly."""
    assert FIXTURES, (
        f"no compose/fixtures/sample-*.json found under {FIXTURES_DIR}: "
        "the harness has nothing to exercise"
    )
    assert len(FIXTURES) >= 6, (
        f"expected >= 6 sample fixtures, got {len(FIXTURES)}: "
        f"{[f.name for f in FIXTURES]}"
    )


# --- The archetype's own listing page shows the model's rows ------------

@pytest.mark.v2_integration
def test_v2_archetype_events_page_serves_seeded_rows(
    v2_e2e_node_modules: Path,
    tmp_path: Path,
) -> None:
    """The spec describes an event producer, declares an Event model and
    lists a page at /events. The event-producer archetype has an events
    page of its own whose layout reads the events table, so that page
    answers at /events and shows the model's rows; the spec's page of the
    same path is not emitted beside it. (Rows are public here because the
    archetype's layout reads the table, not because the page has the
    model's name: see `_public_row_sources` in compose/build.py.)
    Seeds a row into the DB and asserts its title is in the response.
    """
    import json as _json
    from compose.build import build_app
    from compose.intake import translate_v1_spec

    v1 = {
        "spec_version": "v1",
        "app_name": "events-e2e",
        "description": "Event producer running seasonal art shows",
        "admin_email": "admin@example.com",
        "data_models": [
            {
                "name": "Event",
                "fields": [
                    {"name": "title", "type": "string", "required": True},
                    {"name": "event_date", "type": "date", "required": True},
                    {"name": "location", "type": "string"},
                ],
            }
        ],
        "pages": [
            {"path": "/events", "title": "All Events", "purpose": "Browse shows"}
        ],
    }
    v2 = translate_v1_spec(v1)
    build_dir = tmp_path / "build"
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_app(str(spec_path), output_dir=str(build_dir))

    db_name = _create_isolated_db()
    (build_dir / "node_modules").symlink_to(v2_e2e_node_modules)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "DATABASE_URL": f"postgresql://{_pg_user()}@localhost:5432/{db_name}",
        "SESSION_SECRET": "v2-events-test-secret",
        "PORT": str(port),
        "NODE_ENV": "development",
    }

    server: subprocess.Popen | None = None
    try:
        mr = subprocess.run(
            ["node", "db_migrate.js"],
            cwd=build_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert mr.returncode == 0, f"db_migrate failed:\n{mr.stderr}"

        # Seed an event row
        _psql_exec(
            db_name,
            "INSERT INTO events (title, event_date, location) VALUES "
            "('Summer Art Fair', '2026-07-15', 'The Old Mill');",
        )

        server = subprocess.Popen(
            ["node", "server.js"],
            cwd=build_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ok, last_err = _wait_for_health(base_url, timeout=30.0)
        assert ok, f"/health never came up (last: {last_err})"

        r = requests.get(f"{base_url}/events", timeout=5)
        assert r.status_code == 200, (
            f"GET /events should return the archetype's events page, got {r.status_code}. "
            f"Body: {r.text[:400]}"
        )
        assert "Summer Art Fair" in r.text, (
            f"seeded event title not rendered on /events:\n{r.text[:800]}"
        )
    finally:
        if server is not None:
            _kill(server)
        _drop_db(db_name)


# --- v1-shape fixture coverage ------------------------------------------

@pytest.mark.v2_integration
@pytest.mark.parametrize("v1_fixture_path", V1_FIXTURES, ids=lambda p: p.stem)
def test_v2_v1_fixture_translates_builds_and_boots(
    v1_fixture_path: Path,
    v2_e2e_node_modules: Path,
    tmp_path: Path,
) -> None:
    """End-to-end for each v1-shape Option A fixture:

    1. translate_v1_spec produces a valid v2 spec
    2. compose + migrate + boot succeeds (no crash)
    3. / returns 200 (home)
    4. every customer-declared non-auth custom_page path returns 200
    5. if the fixture has a form_target page, that path's form posts
       with CSRF create a row in the target table
    6. if the fixture has a data_model whose slug matches a custom_page
       path, the public list mount returns 200

    One parametric test per fixture, because adding specs as v1 JSON is
    the Option A validation surface: if the translator + compose can
    cleanly serve a plausible customer spec, Option A works.
    """
    import json as _json
    from compose.build import build_app
    from compose.intake import translate_v1_spec

    v1 = _json.loads(v1_fixture_path.read_text())
    v2 = translate_v1_spec(v1)

    build_dir = tmp_path / "build"
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_app(str(spec_path), output_dir=str(build_dir))

    db_name = _create_isolated_db()
    (build_dir / "node_modules").symlink_to(v2_e2e_node_modules)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "DATABASE_URL": f"postgresql://{_pg_user()}@localhost:5432/{db_name}",
        "SESSION_SECRET": "v2-v1fx-test-secret",
        "PORT": str(port),
        "NODE_ENV": "development",
    }

    server: subprocess.Popen | None = None
    try:
        mr = subprocess.run(
            ["node", "db_migrate.js"],
            cwd=build_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert mr.returncode == 0, (
            f"db_migrate failed for {v1_fixture_path.name}:\n{mr.stderr}"
        )

        server = subprocess.Popen(
            ["node", "server.js"],
            cwd=build_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ok, last_err = _wait_for_health(base_url, timeout=30.0)
        if not ok:
            try:
                err = server.stderr.read() if server.stderr else ""
            except Exception:
                err = ""
            pytest.fail(
                f"{v1_fixture_path.name}: /health timed out (last: {last_err})\n"
                f"server stderr: {err[:2000]}"
            )

        # Home must render
        r_home = requests.get(f"{base_url}/", timeout=5)
        assert r_home.status_code == 200, (
            f"{v1_fixture_path.name}: / returned {r_home.status_code}"
        )

        # Every non-auth custom_page path returns 200 or 302 (login redirect
        # only for auth_required=true pages, which we filtered out).
        for cp in (v2.get("custom_pages") or []):
            if cp.get("auth_required"):
                continue
            path = cp.get("path") or ""
            if not path.startswith("/"):
                continue
            # A page whose path is the name of a model is an ordinary
            # page (or, where the spec set public_listing_path on the model,
            # that model's listing); either way it answers.
            r = requests.get(f"{base_url}{path}", timeout=5, allow_redirects=False)
            assert r.status_code in (200, 302), (
                f"{v1_fixture_path.name}: GET {path} returned "
                f"{r.status_code}, expected 200 or 302"
            )

        # If any page has form_target resolving to a real model, POST-
        # test the form end-to-end.
        models_by_name = {
            (m.get("name") or "").lower(): m
            for m in (v2.get("data_models") or [])
            if isinstance(m, dict)
        }
        form_page = next(
            (
                cp for cp in (v2.get("custom_pages") or [])
                if isinstance(cp, dict)
                and cp.get("form_target")
                and (cp.get("form_target") or "").lower() in models_by_name
            ),
            None,
        )
        if form_page is not None:
            session = requests.Session()
            form_path = form_page["path"]
            r_form = session.get(f"{base_url}{form_path}", timeout=5)
            assert r_form.status_code == 200
            import re as _re
            m = _re.search(r'name="_csrf" value="([^"]+)"', r_form.text)
            assert m, (
                f"{v1_fixture_path.name}: form at {form_path} missing CSRF"
            )
            csrf = m.group(1)
            target_model = models_by_name[form_page["form_target"].lower()]
            required = [
                f["name"] for f in (target_model.get("fields") or [])
                if isinstance(f, dict) and f.get("required")
            ]
            body = {"_csrf": csrf}
            for rn in required:
                if rn == "email":
                    body[rn] = "e2e@example.com"
                elif rn == "phone":
                    body[rn] = "555-1234"
                elif "date" in rn:
                    body[rn] = "2026-07-15"
                elif "time" in rn:
                    body[rn] = "18:00"
                else:
                    body[rn] = f"e2e-{rn}"
            r_post = session.post(
                f"{base_url}{form_path}",
                data=body,
                timeout=5,
                allow_redirects=False,
            )
            assert r_post.status_code == 302, (
                f"{v1_fixture_path.name}: POST {form_path} returned "
                f"{r_post.status_code}, expected 302"
            )
            assert r_post.headers.get("Location") == form_path

        # A custom page whose path is the name of a model. Nothing is
        # published because of a name: the page answers, and it shows the
        # model's rows only where the build serves that table publicly
        # anyway (the spec set public_listing_path, or a layout of the
        # archetype reads the table). Seed a row and check both cases.
        from compose.build import (
            BuildSpec,
            _public_row_sources,
            _singular_slug,
            _slug_for,
            _table_name_for_model,
            resolve_spec,
        )
        public_tables = set(_public_row_sources(resolve_spec(BuildSpec.from_dict(json.loads(json.dumps(v2))))))
        for cp in (v2.get("custom_pages") or []):
            if not isinstance(cp, dict) or cp.get("form_target") or cp.get("auth_required"):
                continue
            path = cp.get("path") or ""
            if not path.startswith("/"):
                continue
            slug_candidate = path[1:]
            target = next(
                (
                    m for m in (v2.get("data_models") or [])
                    if isinstance(m, dict)
                    and slug_candidate in (_slug_for(m), _singular_slug(m["name"]))
                ),
                None,
            )
            if target is None:
                continue
            table = _table_name_for_model(target)
            # Required fields, with a marker in every text column.
            req_fields = [
                f for f in (target.get("fields") or [])
                if isinstance(f, dict) and f.get("required")
            ]
            if not req_fields:
                continue
            cols = ", ".join(f["name"] for f in req_fields)
            vals_list = []
            markers = []
            for f in req_fields:
                sql = (f.get("sql_type") or "").upper()
                if "DATE" in sql:
                    vals_list.append("'2026-07-15'")
                elif "TIME" in sql:
                    vals_list.append("'18:00'")
                elif "INT" in sql:
                    vals_list.append("1")
                else:
                    markers.append(f"e2e-{table}-{f['name']}")
                    vals_list.append(f"'{markers[-1]}'")
            _psql_exec(
                db_name,
                f"INSERT INTO {table} ({cols}) VALUES ({', '.join(vals_list)});",
            )
            r_list = requests.get(f"{base_url}{path}", timeout=5)
            assert r_list.status_code == 200, (
                f"{v1_fixture_path.name}: GET {path} returned {r_list.status_code}"
            )
            if table not in public_tables:
                shown = [marker for marker in markers if marker in r_list.text]
                assert not shown, (
                    f"{v1_fixture_path.name}: the page at {path} shows rows of the "
                    f"private model {target['name']}: {shown}"
                )
    finally:
        if server is not None:
            _kill(server)
        _drop_db(db_name)


# --- A public form: POST, validation, INSERT ------------------------------

@pytest.mark.v2_integration
def test_v2_public_form_post_inserts_row(
    v2_e2e_node_modules: Path,
    tmp_path: Path,
) -> None:
    """Compose a v1 spec with a form_target
    custom page, boot the app, GET the form, POST a valid body with CSRF,
    assert the INSERT landed in the model's table + flash_success appears
    on the follow-up GET.

    Also asserts CSRF enforcement (POST without a token → 403) and
    honeypot handling (filled honeypot → redirect, no row).
    """
    import json as _json
    from compose.build import build_app
    from compose.intake import translate_v1_spec
    import psycopg2  # type: ignore[import-not-found]

    v1 = {
        "spec_version": "v1",
        "app_name": "form-e2e",
        "description": "Sound bath facilitator",
        "admin_email": "admin@example.com",
        "data_models": [
            {
                "name": "ContactMessage",
                "fields": [
                    {"name": "name", "type": "string", "required": True},
                    {"name": "email", "type": "email", "required": True},
                    {"name": "phone", "type": "phone"},
                    {"name": "message", "type": "text", "required": True},
                ],
            }
        ],
        "pages": [
            {
                "path": "/reach-out",
                "title": "Reach Out",
                "purpose": "Send us a message",
                "form_target": "ContactMessage",
                "allowed_fields": ["name", "email", "phone", "message"],
                "submit_text": "Send",
                "success_message": "Received; we will reply shortly.",
            }
        ],
    }
    v2 = translate_v1_spec(v1)

    build_dir = tmp_path / "build"
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_app(str(spec_path), output_dir=str(build_dir))

    db_name = _create_isolated_db()
    (build_dir / "node_modules").symlink_to(v2_e2e_node_modules)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "DATABASE_URL": f"postgresql://{_pg_user()}@localhost:5432/{db_name}",
        "SESSION_SECRET": "v2-form-test-secret",
        "PORT": str(port),
        "NODE_ENV": "development",
    }

    server: subprocess.Popen | None = None
    try:
        mr = subprocess.run(
            ["node", "db_migrate.js"],
            cwd=build_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert mr.returncode == 0, f"db_migrate failed:\n{mr.stderr}"

        server = subprocess.Popen(
            ["node", "server.js"],
            cwd=build_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ok, last_err = _wait_for_health(base_url, timeout=30.0)
        assert ok, f"/health never came up (last: {last_err})"

        # --- GET the form to acquire session cookie + CSRF token ---
        session = requests.Session()
        r_get = session.get(f"{base_url}/reach-out", timeout=5)
        assert r_get.status_code == 200, f"GET /reach-out: {r_get.status_code}"
        # Widget rendered
        assert "Reach Out" in r_get.text
        assert 'name="_csrf"' in r_get.text
        # Extract CSRF token from the hidden input
        import re as _re
        m = _re.search(r'name="_csrf" value="([^"]+)"', r_get.text)
        assert m, f"no CSRF token in form HTML:\n{r_get.text[:800]}"
        csrf = m.group(1)

        # --- POST without CSRF token → 403 (handler + error-csrf view) ---
        r_no_csrf = session.post(
            f"{base_url}/reach-out",
            data={"name": "x", "email": "x@example.com", "message": "hi"},
            timeout=5,
            allow_redirects=False,
        )
        assert r_no_csrf.status_code == 403, (
            f"POST without _csrf should 403, got {r_no_csrf.status_code}"
        )

        # --- POST with honeypot filled → redirect, NO row ---
        r_bot = session.post(
            f"{base_url}/reach-out",
            data={
                "_csrf": csrf,
                "name": "Spam Bot",
                "email": "bot@example.com",
                "message": "buy crypto",
                "website_url_hp": "https://spam.example",
            },
            timeout=5,
            allow_redirects=False,
        )
        assert r_bot.status_code == 302, (
            f"honeypot POST should redirect, got {r_bot.status_code}"
        )

        # --- POST with valid body → redirect + INSERT ---
        r_post = session.post(
            f"{base_url}/reach-out",
            data={
                "_csrf": csrf,
                "name": "Real User",
                "email": "user@example.com",
                "phone": "555-1234",
                "message": "Hello there",
            },
            timeout=5,
            allow_redirects=False,
        )
        assert r_post.status_code == 302, (
            f"valid POST should 302 to /reach-out, got {r_post.status_code}"
        )
        assert r_post.headers.get("Location") == "/reach-out"

        # --- Follow-up GET renders flash_success with customer message ---
        r_after = session.get(f"{base_url}/reach-out", timeout=5)
        assert r_after.status_code == 200
        assert "Received; we will reply shortly." in r_after.text, (
            f"flash_success not rendered on GET after POST:\n{r_after.text[:1200]}"
        )

        # --- Confirm exactly ONE row landed in contact_messages and it is
        #     the Real-User one (honeypot + no-csrf attempts did not create).
        conn = psycopg2.connect(
            f"postgresql://{_pg_user()}@localhost:5432/{db_name}"
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name, email, phone, message FROM contact_messages ORDER BY id"
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        assert len(rows) == 1, (
            f"expected exactly 1 INSERTed row, got {len(rows)}: {rows}"
        )
        assert rows[0] == ("Real User", "user@example.com", "555-1234", "Hello there"), (
            f"row shape wrong: {rows[0]}"
        )
    finally:
        if server is not None:
            _kill(server)
        _drop_db(db_name)
