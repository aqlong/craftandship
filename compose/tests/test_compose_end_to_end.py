"""End-to-end integration tests; run compose on a fixture spec,
boot a throwaway DB, boot the generated app, curl-check all routes.

These are the acceptance tests for a v2 build. They are expensive
(createdb + npm install reuse + server boot + requests) so they skip unless
node, a local PostgreSQL and builds/sample-healer/node_modules are all present.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import pytest

from compose.build import build_app


REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SPEC = Path(__file__).resolve().parent.parent / "fixtures" / "sample-healer-spec.json"


def _node_available() -> bool:
    try:
        r = subprocess.run(["node", "--version"], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


@pytest.fixture(scope="module")
def composed_app(tmp_path_factory):
    """Compose the sample healer spec to a temp dir."""
    out = tmp_path_factory.mktemp("v2-e2e") / "app"
    build_app(str(SAMPLE_SPEC), output_dir=out)
    yield out


def _free_port() -> int:
    """A currently unused localhost TCP port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_LOCAL_PG_HOSTS = ("localhost", "127.0.0.1", "::1")


def _postgres_admin():
    """An autocommit connection for CREATE / DROP DATABASE and the
    parameters it was opened with, or a skip.

    The server is chosen the way tests/conftest.py's scratch_pg_url chooses
    it: DATABASE_URL when it is set (in CI that is the Postgres service
    container, with its user and password), otherwise the PostgreSQL on
    localhost that the current OS user can reach. Either way the host must
    be this machine, and only a database this module creates is touched.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2 import extensions

    url = os.environ.get("DATABASE_URL", "").strip()
    try:
        params = extensions.parse_dsn(url) if url else {"dbname": "postgres", "host": "localhost"}
    except psycopg2.ProgrammingError:
        pytest.skip("DATABASE_URL is not a valid PostgreSQL connection string")
    if params.get("host", "localhost") not in _LOCAL_PG_HOSTS:
        pytest.skip("DATABASE_URL does not name a PostgreSQL on this machine over TCP; "
                    "refusing to create a database elsewhere")
    try:
        admin = psycopg2.connect(extensions.make_dsn(**{**params, "connect_timeout": "3"}))
    except psycopg2.OperationalError as exc:
        pytest.skip("no local PostgreSQL reachable (start one, or set DATABASE_URL to a local server): "
                    + type(exc).__name__)
    admin.autocommit = True
    return admin, params


def _node_database_url(params: dict, db_name: str) -> str:
    """postgresql:// URL for the generated app. With no user in `params` the
    URL carries none either, and node-postgres falls back to the current OS
    user, which is the role that created the database."""
    from urllib.parse import quote
    auth = ""
    if params.get("user"):
        auth = quote(params["user"], safe="")
        if params.get("password"):
            auth += ":" + quote(params["password"], safe="")
        auth += "@"
    host = params.get("host", "localhost")
    if ":" in host:
        host = f"[{host}]"
    return f"postgresql://{auth}{host}:{params.get('port', '5432')}/{db_name}"


# Names a database the tests may use instead of creating one of their own.
# Each boot then gets a SCHEMA of its own inside it (created and dropped
# here), so two boots at once do not share tables. Without it every boot
# creates, and drops, a database of its own.
FIXED_DATABASE_ENV = "CAS_COMPOSE_TEST_DATABASE"


@contextlib.contextmanager
def boot_generated_app(app_dir: Path, admin_email: str, node_env: str = "development",
                       extra_env: Optional[dict] = None, expect_healthy: bool = True):
    """Boot a composed app against a throwaway database.

    Creates a database with a per-run name, links the cached node_modules
    into `app_dir` (fast: no npm install), starts `node server.js` on a free
    port and waits for /health. Yields a dict with the port, the database
    name, the process, the app directory, `query(sql, args)` for reading
    or preparing rows in that database, and `log()` for everything the
    server has written to stdout and stderr so far. Stops the server and
    drops the database afterwards.

    `node_env` is the server's NODE_ENV (the generated app sets the Secure
    cookie flag and insists on SESSION_SECRET only under "production");
    `extra_env` adds or overrides environment variables of the server.
    With `expect_healthy=False` the boot is over when the server says it is
    listening, whatever /health answers (for a site that is meant to be
    unhealthy).

    Skips unless node, PostgreSQL (see _postgres_admin) and
    builds/sample-healer/node_modules are all present. Every generated app
    has the same package.json dependencies, so that one install serves any
    spec.
    """
    if not _node_available():
        pytest.skip("node is not on PATH")

    cached_nm = REPO_ROOT / "builds" / "sample-healer" / "node_modules"
    if not cached_nm.exists():
        pytest.skip(
            "needs builds/sample-healer/node_modules (the generated app's dependencies): from the "
            "repository root run `CAS_LINT_EXTERNAL_URLS=skip python3 -m compose.build "
            "compose/fixtures/sample-healer-spec.json`, then `npm install` inside builds/sample-healer"
        )

    admin, params = _postgres_admin()
    import psycopg2
    from psycopg2 import extensions

    fixed_database = os.environ.get(FIXED_DATABASE_ENV, "").strip()
    schema = None
    if fixed_database:
        db_name = fixed_database
        schema = "cas_e2e_" + uuid.uuid4().hex[:10]
        try:
            owner = psycopg2.connect(extensions.make_dsn(**{**params, "dbname": db_name, "connect_timeout": "3"}))
            owner.autocommit = True
            with owner.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{schema}"')
            owner.close()
        except psycopg2.Error as exc:
            admin.close()
            pytest.skip(f"cannot use the database named by {FIXED_DATABASE_ENV}: {type(exc).__name__}")
    else:
        db_name = "cas_test_e2e_" + uuid.uuid4().hex[:10]
        try:
            with admin.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{db_name}"')
        except psycopg2.Error as exc:
            admin.close()
            pytest.skip(f"cannot create a scratch database on the local server: {type(exc).__name__}")

    port = None
    proc = None
    log_f = None
    conn = None
    try:
        # Link the cached node_modules symlink (fast: no npm install)
        nm_target = app_dir / "node_modules"
        if nm_target.exists():
            if nm_target.is_symlink():
                nm_target.unlink()
            else:
                shutil.rmtree(nm_target)
        nm_target.symlink_to(cached_nm.resolve())

        db_url = _node_database_url(params, db_name)
        if schema:
            # The server's connections (and its session store's) find their
            # tables in this schema only.
            db_url += "?options=-c%20search_path%3D" + schema
        log_path = app_dir / "server.log"
        # The port is free when it is picked and can be taken a moment
        # later by any other process on the machine (another test run, for
        # one). The server is therefore "up" only when OUR process says it
        # is listening on that port; a /health answer alone could come from
        # whoever got the port first. When our process lost that race it
        # exits with EADDRINUSE, and the boot is tried again on a new port.
        ok = False
        for _attempt in range(3):
            port = _free_port()
            secret = f"test_secret_v2_e2e_{port}"
            (app_dir / ".env").write_text(
                f"DATABASE_URL={db_url}\n"
                f"SESSION_SECRET={secret}\n"
                f"PORT={port}\n"
                f"NODE_ENV={node_env}\n"
                f"ADMIN_EMAIL={admin_email}\n"
            )

            # Boot the server in the background
            env = os.environ.copy()
            env["DATABASE_URL"] = db_url
            env["SESSION_SECRET"] = secret
            env["PORT"] = str(port)
            env["NODE_ENV"] = node_env
            env["ADMIN_EMAIL"] = admin_email
            # A mail key in the developer's shell must never reach a test
            # server: with one, the app would call the mail provider.
            for name in ("RESEND_API_KEY", "RESEND_FROM_DOMAIN"):
                env.pop(name, None)
            env.update(extra_env or {})

            if log_f is not None:
                log_f.close()
            log_f = open(log_path, "w")
            proc = subprocess.Popen(
                ["node", "server.js"],
                cwd=app_dir, env=env, stdout=log_f, stderr=subprocess.STDOUT
            )

            # Wait for our process to announce the port and for /health to
            # answer (the first boot applies the schema and the seed; allow
            # a slow CI runner 30 s).
            listening = f"Server running on port {port}"
            for _ in range(150):
                time.sleep(0.2)
                if proc.poll() is not None:
                    break
                if listening not in log_path.read_text(encoding="utf-8", errors="replace"):
                    continue
                if not expect_healthy:
                    ok = True
                    break
                try:
                    import urllib.request
                    with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=1) as r:
                        if r.status == 200:
                            ok = True
                            break
                except Exception:
                    continue
            if ok:
                break
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            log_f.flush()
            if "EADDRINUSE" not in log_path.read_text(encoding="utf-8", errors="replace"):
                break

        if not ok:
            log_f.flush()
            log = log_path.read_text() if log_path.exists() else "<no log>"
            pytest.fail(f"server failed to boot on port {port}; log:\n{log}")

        conn_options = {"options": f"-c search_path={schema}"} if schema else {}
        conn = psycopg2.connect(
            extensions.make_dsn(**{**params, "dbname": db_name, "connect_timeout": "3", **conn_options})
        )
        conn.autocommit = True

        def query(sql, args=None):
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchall() if cur.description else []

        def log() -> str:
            # The access log line of a request is written when its response
            # finishes, which can be a moment after the client has read it.
            log_f.flush()
            time.sleep(0.15)
            return log_path.read_text(encoding="utf-8", errors="replace")

        yield {"port": port, "db": db_name, "proc": proc, "app_dir": app_dir, "query": query, "log": log}
    finally:
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        if log_f is not None:
            log_f.close()
        if conn is not None:
            conn.close()
        try:
            if schema:
                owner = psycopg2.connect(
                    extensions.make_dsn(**{**params, "dbname": db_name, "connect_timeout": "3"})
                )
                owner.autocommit = True
                with owner.cursor() as cur:
                    cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                owner.close()
            else:
                with admin.cursor() as cur:
                    try:
                        cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
                    except psycopg2.Error:
                        cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            admin.close()


@pytest.fixture(scope="module")
def db_and_server(composed_app):
    """The composed sample site, booted once for every test in this module
    and in test_smoke_suite.py. See boot_generated_app."""
    with boot_generated_app(composed_app, admin_email="admin@sample-healer.example") as running:
        yield running


@pytest.fixture
def http(db_and_server):
    import urllib.request
    import urllib.error
    port = db_and_server["port"]

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)

    def get(path, cookies=None):
        req = urllib.request.Request(f"http://localhost:{port}{path}")
        if cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()))
        try:
            with opener.open(req, timeout=5) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    return get


# ---- happy path ----

def test_health_returns_200(http):
    status, body, _ = http("/health")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}


def _get(port: int, path: str):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=8) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_health_fails_while_the_home_page_cannot_render(tmp_path):
    """/health is what a deploy is judged by. A site whose home page throws
    for every visitor must not pass it: the database answers, the server is
    up, every other page works, and /health still says 503.

    The home view is broken on disk after the build (a view that throws
    stands in for anything a view can meet at request time). The answer is
    then put to the worker's own definition of a healthy /health.
    """
    spec = json.loads(SAMPLE_SPEC.read_text(encoding="utf-8"))
    out = build_app(spec, output_dir=tmp_path / "broken-home")
    (out / "views" / "home.ejs").write_text(
        "<% throw new Error('the home view cannot render') %>\n", encoding="utf-8",
    )
    with boot_generated_app(out, spec["admin_email"], expect_healthy=False) as app:
        port = app["port"]
        deadline = time.time() + 10
        status, body = _get(port, "/health")
        while status != 503 and time.time() < deadline:
            time.sleep(0.2)
            status, body = _get(port, "/health")
        assert (status, json.loads(body)) == (503, {"status": "home_page_error"})
        # The site is otherwise up: the database answered, another page renders.
        assert _get(port, "/")[0] == 500
        assert _get(port, "/about")[0] == 200
        assert "[health] home page check failed: GET / answered 500" in app["log"]()
        # What the worker makes of that answer (cli/deploy.py): not healthy,
        # and not the database's fault.
        cli_dir = str(REPO_ROOT / "cli")
        for extra in (cli_dir, str(REPO_ROOT)):
            if extra not in sys.path:
                sys.path.insert(0, extra)
        try:
            import deploy as worker_deploy
        except ImportError:
            worker_deploy = None
        if worker_deploy is not None:
            assert worker_deploy.health_response_ok(status, body) is False
            assert worker_deploy.health_status_word(status, body) == "home_page_error"

        # The view is repaired in place (Express caches compiled views only
        # under NODE_ENV=production, and this boot is a development one).
        # A failure is not remembered for good: a later probe asks again
        # and the site becomes healthy.
        (out / "views" / "home.ejs").write_text("<p>home</p>\n", encoding="utf-8")
        deadline = time.time() + 25
        while time.time() < deadline:
            status, body = _get(port, "/health")
            if status == 200:
                break
            time.sleep(0.5)
        assert (status, json.loads(body)) == (200, {"status": "ok"})


def test_home_returns_200(http):
    status, body, _ = http("/")
    assert status == 200
    # spec has site_name "Sample Healing Studio"
    assert "Sample Healing Studio" in body or "Site" in body


def test_every_spec_page_is_reachable(http):
    for page in ("/", "/about", "/work-with-me", "/journal", "/events", "/contact"):
        status, body, _ = http(page)
        assert status == 200, f"{page} returned {status}"
        assert "Sample Healing Studio" in body, f"{page} missing brand name"


def test_login_page_has_csrf_token(http):
    status, body, _ = http("/login")
    assert status == 200
    assert '_csrf' in body


def test_admin_paths_redirect_to_login_when_anon(http):
    """MW v25 pattern: all /admin/* paths redirect to login when anon."""
    for path in ("/admin", "/admin/settings", "/admin/events", "/admin/blog-posts",
                 "/admin/testimonials", "/admin/contact-inquiries",
                 "/admin/newsletter-subscribers", "/admin/events/new",
                 "/admin/events/1/edit"):
        status, _, headers = http(path)
        assert status == 302, f"{path} should redirect (got {status})"
        assert "login" in headers.get("Location", "").lower()


def test_admin_only_bare_slugs_404_for_nonpublic_models(http):
    """MW v25: bare /<slug> for models WITHOUT public_listing_path should
    404 (not render admin view). Testimonial, ContactInquiry, and
    NewsletterSubscriber are admin-only; public visitors get 404."""
    for slug in ("/testimonials", "/contact-inquiries", "/newsletter-subscribers"):
        status, body, _ = http(slug)
        assert status == 404, f"{slug} (admin-only bare mount) should 404, got {status}"


def test_public_listing_paths_show_content_when_anon(http):
    """Events + BlogPost + Photo have public_listing_path set → anon
    visitors see the public list at that path (not login redirect, not 404).
    BlogPost mounts at /journal (its public_listing_path), not /blog-posts.
    """
    for path in ("/events", "/journal", "/gallery"):
        status, body, _ = http(path)
        assert status == 200, f"{path} should be public; got {status}"


def test_public_resource_detail_renders(http):
    """Anon visitors can view individual events + blog posts via the
    public_listing_path mount."""
    for path in ("/events/1", "/journal/1"):
        status, body, _ = http(path)
        assert status == 200, f"{path} should be public; got {status}"
        # Detail pages include a "Back to" link
        assert "Back to" in body, f"{path} missing back link"
    # A gallery photo has no page of its own: the engine emits per-item
    # detail pages only for the listing widgets in _DETAIL_PAGE_WIDGETS
    # (card grids and book grids), and a photo-gallery card is the whole
    # content.
    status, _, _ = http("/gallery/1")
    assert status == 404, f"/gallery/1 should not exist; got {status}"


def test_admin_bare_slug_new_and_edit_404_for_anon(http):
    """Admin-only action paths (new, :id/edit) should be unreachable via
    the bare public mount even for models with public_listing_path."""
    for path in ("/events/new", "/events/1/edit", "/journal/new", "/gallery/new"):
        status, _, _ = http(path)
        assert status == 404, f"{path} admin action via bare mount should 404, got {status}"


# ---- negative paths ----

def test_404_renders_error_page(http):
    status, body, _ = http("/does-not-exist-at-all")
    assert status == 404
    assert "404" in body


def test_health_never_redirects(http):
    status, _, headers = http("/health")
    assert status == 200
    assert "Location" not in headers
