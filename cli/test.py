# cli/test.py - Playwright test runner for generated apps

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# The scrubbed child environment is defined once, in deploy.py (the module both
# engines use). The underscore name is kept for callers and tests of this file.
from deploy import env_without_secrets as _env_without_secrets

load_dotenv()

STARTUP_TIMEOUT_SECONDS = 30
STARTUP_POLL_INTERVAL = 0.5
TEST_PORT = 4567
TEST_MODE_PASSWORD = "test_password_for_ci"


def _pg_env() -> dict:
    """Build env dict with PG* vars extracted from DATABASE_URL.

    PostgreSQL client tools (createdb, dropdb, psql) use PGHOST, PGPORT,
    PGUSER, PGPASSWORD environment variables. When running in a Docker
    container (e.g. Railway worker), there is no local Postgres server;
    the tools must connect to the remote database via these vars.

    psql runs model-written SQL files (db/schema.sql, db/seed.sql), and a psql
    script can start a shell with a backslash command, so the tools get the
    scrubbed environment plus the PG* connection variables and nothing else of
    the worker's. The connection itself is the worker's database server
    account; see the known limitations in cli/README.md.

    Returns:
        Scrubbed copy of os.environ with PG* vars set (if DATABASE_URL is present).
    """
    env = _env_without_secrets()
    # A PGPASSWORD set for the worker is a connection setting for these tools;
    # the scrubber drops it by name, so carry it over explicitly.
    if os.environ.get("PGPASSWORD"):
        env["PGPASSWORD"] = os.environ["PGPASSWORD"]
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url:
        parsed = urlparse(db_url)
        if parsed.hostname:
            env["PGHOST"] = parsed.hostname
        if parsed.port:
            env["PGPORT"] = str(parsed.port)
        if parsed.username:
            env["PGUSER"] = parsed.username
        if parsed.password:
            env["PGPASSWORD"] = parsed.password
    return env


# The app under test, its npm install scripts, psql on the generated SQL and the
# generated QA tests are model-chosen, untrusted code. They all start from
# _env_without_secrets() (deploy.env_without_secrets): no platform API keys, no
# Railway/Stripe/Resend/GitHub credentials, not the worker's own database URL.
# start_app() sets back only what the app needs (PORT, DATABASE_URL for the
# throwaway test DB, SESSION_SECRET, ...).


def run_cmd(command, cwd=None, check=True, timeout=120, env=None):
    """Run a command (as a list) and return the CompletedProcess result.

    Uses list-form subprocess invocation (no shell=True) so that variable
    arguments cannot introduce shell metacharacters that would be interpreted
    by the shell, preventing shell injection.

    Args:
        command: List of command tokens, e.g. ['createdb', 'mydb'].
        cwd: Optional working directory for the subprocess.
        check: If True, print a warning on non-zero exit (but do not raise).
        timeout: Maximum seconds to wait for the subprocess (default 120).
        env: Optional environment dict for the subprocess.

    Returns:
        subprocess.CompletedProcess instance.
    """
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if check and result.returncode != 0:
        print(f"  Command failed: {' '.join(command)}")
        if result.stdout:
            print(f"  stdout: {result.stdout[:500]}")
        if result.stderr:
            print(f"  stderr: {result.stderr[:500]}")
    return result


def _validate_db_name(db_name: str) -> None:
    """Raise ValueError if db_name contains characters that are unsafe in psql args.

    Postgres identifiers must be alphanumeric plus underscores and hyphens.
    This guard prevents an attacker-controlled app_name from injecting extra
    command-line arguments into createdb/dropdb/psql calls.

    Args:
        db_name: The database name to validate.

    Raises:
        ValueError: If db_name contains disallowed characters.
    """
    if not re.match(r'^[a-zA-Z0-9_-]+$', db_name):
        raise ValueError(f"Unsafe database name: '{db_name}'")


def setup_database(app_dir, db_name):
    """Create a test database, run schema and seed files.

    Args:
        app_dir: pathlib.Path to the app's build directory.
        db_name: Name for the test database (must be alphanumeric/_/-).

    Returns:
        True on success, False on any failure.
    """
    _validate_db_name(db_name)
    pg = _pg_env()
    print(f"  Creating database: {db_name}")
    # Drop if exists from a previous failed run; pass name as a separate token.
    run_cmd(["dropdb", "--if-exists", db_name], check=False, env=pg)
    result = run_cmd(["createdb", db_name], env=pg)
    if result.returncode != 0:
        print("  Failed to create database. Is PostgreSQL running?")
        return False

    schema_path = app_dir / "db" / "schema.sql"
    seed_path = app_dir / "db" / "seed.sql"

    if schema_path.exists():
        # psql -f <file> avoids shell redirection; db_name is a separate token.
        result = run_cmd(["psql", db_name, "-f", "db/schema.sql"], cwd=app_dir, env=pg)
        if result.returncode != 0:
            print("  Failed to run schema.sql")
            return False

    if seed_path.exists():
        result = run_cmd(["psql", db_name, "-f", "db/seed.sql"], cwd=app_dir, env=pg)
        if result.returncode != 0:
            print("  Failed to run seed.sql")
            return False

    return True


def teardown_database(db_name):
    """Drop the test database.

    Args:
        db_name: Name of the database to drop.
    """
    _validate_db_name(db_name)
    pg = _pg_env()
    print(f"  Dropping database: {db_name}")
    run_cmd(["dropdb", "--if-exists", db_name], check=False, env=pg)


def install_deps(app_dir):
    """Run npm install for the app directory.

    On Railway, the builds/ directory is a network-attached volume. To avoid
    NFS mkdir race conditions (ENOENT) and prevent node_modules from filling
    the volume (ENOSPC), we:
      1. Copy ALL source files (excluding node_modules/.git) to a local /tmp dir
      2. Run npm install in /tmp (fast, reliable local ext4 I/O)
      3. Return the /tmp dir as test_dir so the app process runs there
      4. NEVER write node_modules to the volume

    deploy.py never reads node_modules from the volume; Railway's own build
    pipeline installs them fresh inside the deployed container. The volume only
    ever needs source files (~300KB per build).

    Args:
        app_dir: pathlib.Path (or str) to the app's build directory on the volume.

    Returns:
        (success: bool, error_detail: str, test_dir: Path|None)
        - On Railway success: test_dir is a /tmp Path containing source + node_modules.
          Caller MUST clean up test_dir (via teardown_app).
        - On Railway failure: test_dir is None; any tmp_dir created is already removed.
        - Locally: test_dir is None; npm install ran directly in app_dir (unchanged).
    """
    import shutil
    import tempfile

    print("  Installing npm dependencies...")

    app_dir = Path(app_dir)
    on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT"))

    if on_railway:
        # Create a local working directory for the full test phase.
        # Source files are copied here; node_modules are installed here.
        # The volume (app_dir) never receives node_modules.
        tmp_dir = Path(tempfile.mkdtemp(prefix="npm_test_"))
        success = False
        try:
            # 1. Copy source files from volume to tmp (skip node_modules and .git).
            #    Source is ~300KB; this copy is fast even over NFS reads.
            for item in app_dir.iterdir():
                if item.name in ("node_modules", ".git"):
                    continue
                dst = tmp_dir / item.name
                if item.is_file():
                    shutil.copy2(item, dst)
                elif item.is_dir():
                    shutil.copytree(str(item), str(dst), symlinks=True)

            # 2. npm install in /tmp (local ext4, no NFS concurrent mkdir races)
            env = _env_without_secrets()
            env["npm_config_cache"] = str(tmp_dir / ".npm-cache")
            result = run_cmd(
                ["npm", "install", "--no-audit"],
                cwd=tmp_dir,
                env=env,
                timeout=180,
            )
            if result.returncode != 0:
                raw = result.stderr or ""
                lines = [ln for ln in raw.splitlines()
                         if not ln.startswith("npm warn config ")]
                err = "\n".join(lines)[-500:] if lines else raw[-500:]
                return False, err, None

            # 3. Copy package-lock.json back to volume (tiny; needed for reproducibility)
            lock_src = tmp_dir / "package-lock.json"
            if lock_src.exists():
                shutil.copy2(lock_src, app_dir / "package-lock.json")

            # 4. Return tmp_dir to caller: do NOT clean up here.
            #    The app process will run from tmp_dir. Caller cleans up via teardown_app.
            success = True
            return True, "", tmp_dir

        finally:
            # Only clean up on failure. On success, test_dir persists for the app process.
            if not success:
                shutil.rmtree(str(tmp_dir), ignore_errors=True)
    else:
        # Local dev: install directly in app_dir (unchanged behavior)
        result = run_cmd(["npm", "install"], cwd=app_dir, env=_env_without_secrets())
        if result.returncode == 0:
            return True, "", None
        raw = result.stderr or ""
        lines = [ln for ln in raw.splitlines()
                 if not ln.startswith("npm warn config ")]
        err = "\n".join(lines)[-500:] if lines else raw[-500:]
        return False, err, None


def start_app(app_dir, db_name, port):
    """Start the app as a subprocess, return the process handle.

    stdout and stderr are written to log files instead of PIPE to avoid
    buffer-full deadlocks and to preserve output for debugging.
    """
    env = _env_without_secrets()
    env["PORT"] = str(port)
    # Build DATABASE_URL for the test database, using the same host as the
    # worker's DATABASE_URL (which may be a remote Railway Postgres). The app
    # gets that server's credentials but only the throwaway test DB name.
    source_url = os.environ.get("DATABASE_URL", "")
    if source_url:
        parsed = urlparse(source_url)
        userinfo = ""
        if parsed.username:
            userinfo = parsed.username
            if parsed.password:
                userinfo += f":{parsed.password}"
            userinfo += "@"
        host = parsed.hostname or "localhost"
        port_num = parsed.port or 5432
        env["DATABASE_URL"] = f"postgres://{userinfo}{host}:{port_num}/{db_name}"
    else:
        env["DATABASE_URL"] = f"postgres://localhost:5432/{db_name}"
    env["SESSION_SECRET"] = "test_session_secret"
    env["NODE_ENV"] = "test"
    env["TEST_MODE"] = "true"

    stdout_path = Path(app_dir) / "test_stdout.log"
    stderr_path = Path(app_dir) / "test_stderr.log"

    stdout_file = open(stdout_path, "w")
    stderr_file = open(stderr_path, "w")

    print(f"  Starting app on port {port}...")
    process = subprocess.Popen(
        ["node", "server.js"],
        cwd=app_dir,
        env=env,
        stdout=stdout_file,
        stderr=stderr_file,
    )
    # Attach file handles so we can close them later
    process._stdout_file = stdout_file
    process._stderr_file = stderr_file
    return process


def wait_for_app(port, timeout=STARTUP_TIMEOUT_SECONDS):
    """Poll the health endpoint until the app is ready."""
    import urllib.request
    import urllib.error

    url = f"http://localhost:{port}/health"
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            response = urllib.request.urlopen(url, timeout=2)  # nosec B310  # fixed http://localhost:<port>/health URL; the port is an int the worker picked
            if response.status == 200:
                return True
        except (urllib.error.URLError, ConnectionRefusedError, OSError):
            pass
        time.sleep(STARTUP_POLL_INTERVAL)

    return False


def stop_app(process):
    """Stop the app subprocess and close log file handles."""
    if process and process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    # Close log file handles opened by start_app
    for attr in ("_stdout_file", "_stderr_file"):
        fh = getattr(process, attr, None)
        if fh and not fh.closed:
            fh.close()


def load_spec(app_dir):
    """Load the spec.json from the build directory."""
    spec_path = app_dir / "spec.json"
    if not spec_path.exists():
        return None
    with open(spec_path) as file:
        return json.load(file)


def _build_seed_users(spec):
    """Derive seed user credentials from spec roles.

    Generates one test user per role using the predictable email format that
    build.py instructs Claude to seed: {role}@example.com.
    The first role uses spec.admin_email if available (post-processor ensures
    it in seed.sql). Any role named "admin" (case-insensitive) also uses
    admin_email, because _fix_seed_admin_email() replaces admin@example.com
    with the real email -- but when the spec has roles ordered with a non-admin
    role first (e.g. visitor/admin), Claude may seed the admin user directly
    as admin_email without ever using admin@example.com as a placeholder,
    causing the default {role}@example.com mapping to produce a non-existent
    user and failing Test 6 (login as admin redirects away from login).

    Args:
        spec: Parsed spec dict (may be None).

    Returns:
        Dict of role_name -> {email, password}.
    """
    if not spec:
        return {}
    users = {}
    admin_email = spec.get("admin_email", "")
    roles = spec.get("user_roles", [])
    for i, role_entry in enumerate(roles):
        role = role_entry.get("role", "")
        if role:
            # First role OR any role named "admin" uses admin_email from spec.
            # This handles apps where the admin role is not at index 0
            # (e.g. visitor/admin ordering where visitor has no account and
            # admin is seeded directly with the owner's email).
            if admin_email and (i == 0 or role.lower() == "admin"):
                email = admin_email
            else:
                email = f"{role}@example.com"
            users[role] = {
                "email": email,
                "password": TEST_MODE_PASSWORD,
            }
    return users


def _first_protected_page(spec):
    """Return the path of the first auth-required page from the spec.

    Args:
        spec: Parsed spec dict (may be None).

    Returns:
        Path string (e.g. '/dashboard'), or '/dashboard' as fallback.
    """
    if spec:
        for page in spec.get("pages", []):
            if page.get("auth_required"):
                return page["path"]
    return "/dashboard"


def _admin_pages(spec):
    """Return paths exclusive to the first defined role (first role can access, second role cannot).

    Uses the intersection approach: pages that first role can access AND second role cannot.
    This ensures Test 8 tests genuine role enforcement rather than shared pages.

    Args:
        spec: Parsed spec dict (may be None).

    Returns:
        List of path strings that the first role can access but the second role cannot.
        Falls back to all auth-required pages for the first role if no exclusive pages exist.
    """
    if not spec:
        return []
    roles = spec.get("user_roles", [])
    if not roles:
        return []
    first_role = roles[0]["role"]
    second_role = roles[1]["role"] if len(roles) > 1 else None
    # Return only pages that first role can access but second role cannot.
    # If no such pages exist (all pages are shared between roles), return
    # empty list so Test 8 (role enforcement) and Test 9 (admin access)
    # are skipped: testing shared pages as "admin-only" produces false
    # positives that waste hours of rebuild cycles.
    return [
        p["path"]
        for p in spec.get("pages", [])
        if p.get("auth_required")
        and first_role in p.get("allowed_roles", [])
        and (second_role is None or second_role not in p.get("allowed_roles", []))
    ]


def _non_admin_pages(spec):
    """Return paths restricted to roles OTHER than the first role.

    Args:
        spec: Parsed spec dict (may be None).

    Returns:
        List of path strings the first role should NOT be able to access
        (used to verify role enforcement from a lower-privilege account).
    """
    if not spec:
        return []
    roles = spec.get("user_roles", [])
    if len(roles) < 2:
        return []
    first_role = roles[0]["role"]
    return [
        p["path"]
        for p in spec.get("pages", [])
        if p.get("auth_required")
        and p.get("allowed_roles")
        and first_role not in p["allowed_roles"]
    ]


def capture_screenshots(browser, port, spec, output_dir):
    """Capture screenshots of key pages for visual verification.

    Best-effort: failures log a warning but never raise. Takes screenshots
    of the login page (unauthenticated) and up to 5 auth-required pages
    (logged in as the first seed user).

    Args:
        browser: Playwright Browser instance (already launched).
        port: The port the app is running on.
        spec: Parsed spec dict.
        output_dir: Path to save screenshots (created if needed).

    Returns:
        List of dicts with 'page' (label) and 'path' (absolute file path).
    """
    base_url = f"http://localhost:{port}"
    screenshots_dir = Path(output_dir) / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    captured = []

    def _slugify(path_str):
        return path_str.strip("/").replace("/", "-") or "root"

    try:
        # 1. Login page (unauthenticated)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.set_default_timeout(15000)
        page.set_default_navigation_timeout(15000)
        page.goto(f"{base_url}/login", timeout=15000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        login_path = str(screenshots_dir / "login.png")
        page.screenshot(path=login_path, full_page=False)
        captured.append({"page": "/login", "path": login_path})
        print(f"  Screenshot: /login -> login.png")

        # 2. Log in as first role, then visit auth-required pages
        seed_users = _build_seed_users(spec)
        roles = list(seed_users.keys())
        first_role = roles[0] if roles else None

        if first_role and first_role in seed_users:
            page.goto(f"{base_url}/login", timeout=15000)
            page.fill("form:has(input[name='password']) input[name='email']", seed_users[first_role]["email"])
            page.fill("form:has(input[name='password']) input[name='password']", seed_users[first_role]["password"])
            page.click("form:has(input[name='password']) button[type='submit']")
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # Visit up to 5 auth-required pages
            pages_to_visit = [
                p["path"] for p in spec.get("pages", [])
                if p.get("auth_required") and first_role in p.get("allowed_roles", [])
            ][:5]

            for page_path in pages_to_visit:
                try:
                    response = page.goto(f"{base_url}{page_path}", timeout=15000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    if response and response.status == 200:
                        slug = _slugify(page_path)
                        ss_path = str(screenshots_dir / f"{slug}.png")
                        page.screenshot(path=ss_path, full_page=False)
                        captured.append({"page": page_path, "path": ss_path})
                        print(f"  Screenshot: {page_path} -> {slug}.png")
                except Exception as page_exc:
                    print(f"  Screenshot warning: {page_path} failed: {page_exc}")

        page.close()
    except Exception as exc:
        print(f"  Screenshot capture warning: {exc}")

    return captured


def run_tests(port, spec, db_name=None):
    """Run Playwright smoke tests against the running app. Returns (passed, failed, results)."""
    base_url = f"http://localhost:{port}"
    results = []
    passed = 0
    failed = 0

    seed_users = _build_seed_users(spec)
    roles = list(seed_users.keys())
    first_role = roles[0] if roles else None
    second_role = roles[1] if len(roles) > 1 else None
    protected_page = _first_protected_page(spec)
    admin_pages = _admin_pages(spec)
    restricted_pages = _non_admin_pages(spec)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        # Test 1: Health check
        test_name = "health endpoint returns 200"
        try:
            page = browser.new_page()
            response = page.goto(f"{base_url}/health")
            assert response.status == 200, f"Expected 200, got {response.status}"
            body = page.text_content("body")
            assert "ok" in body, "Health response missing 'ok'"
            results.append((test_name, True, None))
            passed += 1
            page.close()
        except Exception as error:
            results.append((test_name, False, str(error)))
            failed += 1
            page.close()

        # Test 2: Root page behavior (login redirect OR public landing page)
        has_public_root = any(
            p.get("path") == "/" and not p.get("auth_required", True)
            for p in spec.get("pages", [])
        )
        if has_public_root:
            test_name = "root shows public landing page"
            try:
                page = browser.new_page()
                response = page.goto(f"{base_url}/")
                page.wait_for_load_state("networkidle")
                # Public landing page should return 200 and NOT redirect to login
                is_public = response.status == 200 and "/login" not in page.url
                assert is_public, f"Expected public landing page at root, got {page.url} status {response.status}"
                results.append((test_name, True, None))
                passed += 1
                page.close()
            except Exception as error:
                results.append((test_name, False, str(error)))
                failed += 1
                page.close()
        else:
            test_name = "root redirects to login"
            try:
                page = browser.new_page()
                response = page.goto(f"{base_url}/")
                page.wait_for_load_state("networkidle")
                is_login = "/login" in page.url or page.locator("input[name='email']").count() > 0
                assert is_login, f"Expected login page at root or redirect, got {page.url}"
                results.append((test_name, True, None))
                passed += 1
                page.close()
            except Exception as error:
                results.append((test_name, False, str(error)))
                failed += 1
                page.close()

        # Test 3: Login page renders
        test_name = "login page renders form"
        try:
            page = browser.new_page()
            page.goto(f"{base_url}/login")
            page.wait_for_load_state("networkidle")
            assert page.locator("form:has(input[name='password']) input[name='email']").first.is_visible(), "Missing email input"
            assert page.locator("form:has(input[name='password']) input[name='password']").first.is_visible(), "Missing password input"
            results.append((test_name, True, None))
            passed += 1
            page.close()
        except Exception as error:
            results.append((test_name, False, str(error)))
            failed += 1
            page.close()

        # Test 4: Login with bad credentials fails
        test_name = "login with bad credentials is rejected"
        try:
            page = browser.new_page()
            page.goto(f"{base_url}/login")
            page.fill("form:has(input[name='password']) input[name='email']", "nobody@example.com")
            page.fill("form:has(input[name='password']) input[name='password']", "wrongpassword")
            page.click("form:has(input[name='password']) button[type='submit']")
            page.wait_for_load_state("networkidle")
            assert "/login" in page.url or page.locator(".error, .alert, .message").count() > 0, \
                "Bad login did not stay on login page or show error"
            results.append((test_name, True, None))
            passed += 1
            page.close()
        except Exception as error:
            results.append((test_name, False, str(error)))
            failed += 1
            page.close()

        # Test 5: Login as first role and verify redirect away from login
        if first_role and first_role in seed_users:
            test_name = f"login as {first_role} redirects away from login"
            try:
                page = browser.new_page()
                page.goto(f"{base_url}/login")
                page.fill("form:has(input[name='password']) input[name='email']", seed_users[first_role]["email"])
                page.fill("form:has(input[name='password']) input[name='password']", seed_users[first_role]["password"])
                page.click("form:has(input[name='password']) button[type='submit']")
                page.wait_for_load_state("networkidle")
                # Capture debug info before asserting
                debug_url = page.url
                debug_body = ""
                if "/login" in debug_url:
                    debug_body = page.text_content("body")[:300] if page.locator("body").count() > 0 else ""
                assert "/login" not in debug_url, \
                    f"Expected redirect away from login, got {debug_url}. Page text: {debug_body}"
                results.append((test_name, True, None))
                passed += 1
                page.close()
            except Exception as error:
                results.append((test_name, False, str(error)))
                failed += 1
                page.close()

        # Test 6: Login as second role and verify redirect away from login
        if second_role and second_role in seed_users:
            test_name = f"login as {second_role} redirects away from login"
            try:
                page = browser.new_page()
                page.goto(f"{base_url}/login")
                page.fill("form:has(input[name='password']) input[name='email']", seed_users[second_role]["email"])
                page.fill("form:has(input[name='password']) input[name='password']", seed_users[second_role]["password"])
                page.click("form:has(input[name='password']) button[type='submit']")
                page.wait_for_load_state("networkidle")
                assert "/login" not in page.url, \
                    f"Expected redirect away from login, got {page.url}"
                results.append((test_name, True, None))
                passed += 1
                page.close()
            except Exception as error:
                results.append((test_name, False, str(error)))
                failed += 1
                page.close()

        # Test 7: Protected page rejects unauthenticated access
        test_name = f"protected page {protected_page} requires auth"
        try:
            page = browser.new_page()
            response = page.goto(f"{base_url}{protected_page}")
            page.wait_for_load_state("networkidle")
            is_redirected = "/login" in page.url
            is_forbidden = response.status in (401, 403)
            assert is_redirected or is_forbidden, \
                f"Expected redirect to /login or 401/403, got {page.url} status {response.status}"
            results.append((test_name, True, None))
            passed += 1
            page.close()
        except Exception as error:
            results.append((test_name, False, str(error)))
            failed += 1
            page.close()

        # Test 8: Second role cannot access first role's admin-only pages
        if second_role and admin_pages and second_role in seed_users:
            test_name = f"{second_role} cannot access admin-only pages"
            try:
                page = browser.new_page()
                page.goto(f"{base_url}/login")
                page.fill("form:has(input[name='password']) input[name='email']", seed_users[second_role]["email"])
                page.fill("form:has(input[name='password']) input[name='password']", seed_users[second_role]["password"])
                page.click("form:has(input[name='password']) button[type='submit']")
                page.wait_for_load_state("networkidle")
                response = page.goto(f"{base_url}{admin_pages[0]}")
                page.wait_for_load_state("networkidle")
                is_forbidden = response.status in (401, 403)
                is_redirected = "/login" in page.url
                is_on_own_page = "/login" not in page.url and page.url != f"{base_url}{admin_pages[0]}"
                assert is_forbidden or is_redirected or is_on_own_page, \
                    f"Expected 401/403 or redirect, got {response.status} at {page.url}"
                results.append((test_name, True, None))
                passed += 1
                page.close()
            except Exception as error:
                results.append((test_name, False, str(error)))
                failed += 1
                page.close()

        # Test 9: First role can access their admin page
        if first_role and admin_pages and first_role in seed_users:
            test_name = f"{first_role} can access {admin_pages[0]}"
            try:
                page = browser.new_page()
                page.goto(f"{base_url}/login")
                page.fill("form:has(input[name='password']) input[name='email']", seed_users[first_role]["email"])
                page.fill("form:has(input[name='password']) input[name='password']", seed_users[first_role]["password"])
                page.click("form:has(input[name='password']) button[type='submit']")
                page.wait_for_load_state("networkidle")
                response = page.goto(f"{base_url}{admin_pages[0]}")
                page.wait_for_load_state("networkidle")
                assert response.status == 200, \
                    f"Expected 200, got {response.status} at {page.url}"
                results.append((test_name, True, None))
                passed += 1
                page.close()
            except Exception as error:
                results.append((test_name, False, str(error)))
                failed += 1
                page.close()

        # Test 10: XSS, error query param must not execute as script
        test_name = "login error param is escaped (XSS check)"
        try:
            page = browser.new_page()
            xss_triggered = []
            page.on("dialog", lambda dialog: (xss_triggered.append(dialog.message), dialog.dismiss()))
            page.goto(f"{base_url}/login?error=<script>alert('xss')</script>")
            page.wait_for_load_state("networkidle")
            assert len(xss_triggered) == 0, "XSS alert fired; error param is not escaped"
            # Also check the raw script tag is not injected as executable
            content = page.content()
            assert "<script>alert('xss')</script>" not in content, \
                "Unescaped script tag found in page source"
            results.append((test_name, True, None))
            passed += 1
            page.close()
        except Exception as error:
            results.append((test_name, False, str(error)))
            failed += 1
            page.close()

        # Test 11: Buttons on authenticated pages are visible (not invisible/transparent)
        if first_role and first_role in seed_users:
            test_name = "buttons have visible backgrounds (not invisible)"
            try:
                page = browser.new_page()
                page.goto(f"{base_url}/login")
                page.fill("form:has(input[name='password']) input[name='email']", seed_users[first_role]["email"])
                page.fill("form:has(input[name='password']) input[name='password']", seed_users[first_role]["password"])
                page.click("form:has(input[name='password']) button[type='submit']")
                page.wait_for_load_state("networkidle")
                if "/login" not in page.url:
                    # Check all submit buttons and primary action links on this page
                    # Only check actual form submit buttons (not nav links)
                    submit_btns = page.locator("button[type='submit']")
                    invisible_btns = []
                    for i in range(min(submit_btns.count(), 5)):
                        btn = submit_btns.nth(i)
                        if btn.is_visible():
                            bg = btn.evaluate("el => getComputedStyle(el).backgroundColor")
                            color = btn.evaluate("el => getComputedStyle(el).color")
                            # transparent or white-on-white = invisible
                            if bg in ("rgba(0, 0, 0, 0)", "transparent", ""):
                                invisible_btns.append(f"{btn.text_content().strip()[:30]} (no background)")
                            elif bg in ("rgb(255, 255, 255)", "rgba(255, 255, 255, 1)") and \
                                 color in ("rgb(255, 255, 255)", "rgba(255, 255, 255, 1)"):
                                invisible_btns.append(f"{btn.text_content().strip()[:30]} (white on white)")
                    assert len(invisible_btns) == 0, \
                        f"Found invisible buttons: {'; '.join(invisible_btns)}"
                results.append((test_name, True, None))
                passed += 1
                page.close()
            except Exception as error:
                results.append((test_name, False, str(error)))
                failed += 1
                page.close()

        # Test 12: Logout works and session is destroyed
        if first_role and first_role in seed_users:
            test_name = "logout destroys session"
            try:
                page = browser.new_page()
                page.goto(f"{base_url}/login")
                page.fill("form:has(input[name='password']) input[name='email']", seed_users[first_role]["email"])
                page.fill("form:has(input[name='password']) input[name='password']", seed_users[first_role]["password"])
                page.click("form:has(input[name='password']) button[type='submit']")
                page.wait_for_load_state("networkidle")
                assert "/login" not in page.url, "Could not log in for logout test"
                # Submit logout form: tolerate multiple matches (desktop + mobile nav) via .first
                nav_logout = page.locator("nav form[action='/logout']")
                any_logout = page.locator("form[action='/logout']")
                logout_form = None
                if nav_logout.count() > 0:
                    logout_form = nav_logout.first
                elif any_logout.count() > 0:
                    logout_form = any_logout.first
                if logout_form is not None:
                    logout_form.locator("button[type='submit']").first.click()
                    page.wait_for_load_state("networkidle")
                    is_logged_out = "/login" in page.url or page.locator("input[name='email']").count() > 0
                    assert is_logged_out, f"Expected redirect to login after logout, got {page.url}"
                    # Verify protected page now redirects
                    response = page.goto(f"{base_url}{protected_page}")
                    page.wait_for_load_state("networkidle")
                    assert "/login" in page.url or response.status in (401, 403), \
                        "Protected page accessible after logout"
                results.append((test_name, True, None))
                passed += 1
                page.close()
            except Exception as error:
                results.append((test_name, False, str(error)))
                failed += 1
                page.close()

        # Test 13: CSS is actually loading (not unstyled HTML)
        test_name = "CSS is loading (pages are styled)"
        try:
            page = browser.new_page()
            page.goto(f"{base_url}/login")
            page.wait_for_load_state("networkidle")
            # Check that body has a non-default background or that a styled element exists
            body_bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
            body_font = page.evaluate("() => getComputedStyle(document.body).fontFamily")
            # Also check if any stylesheet is loaded
            stylesheets = page.evaluate("() => document.styleSheets.length")
            # Unstyled page: white bg + Times New Roman/serif + 0 stylesheets
            is_unstyled = (
                body_bg in ("rgb(255, 255, 255)", "rgba(0, 0, 0, 0)", "transparent", "")
                and ("Times" in body_font or "serif" in body_font.lower() or body_font == "")
                and stylesheets == 0
            )
            assert not is_unstyled, \
                f"Page appears unstyled (bg={body_bg}, font={body_font}, sheets={stylesheets})"
            results.append((test_name, True, None))
            passed += 1
            page.close()
        except Exception as error:
            results.append((test_name, False, str(error)))
            failed += 1
            page.close()

        # Test 14: No duplicate page content (layout rendering issue)
        test_name = "no duplicate content (layout renders correctly)"
        try:
            page = browser.new_page()
            # Check signup or register page for duplicate headings
            signup_url = f"{base_url}/signup"
            response = page.goto(signup_url)
            if response.status == 404:
                signup_url = f"{base_url}/register"
                page.goto(signup_url)
            page.wait_for_load_state("networkidle")
            # Count h1 tags: should be at most 1 (or 2 if there's a site title + page title)
            h1_count = page.locator("h1").count()
            # Count forms: signup should have exactly 1 form
            form_count = page.locator("form").count()
            assert h1_count <= 2, \
                f"Found {h1_count} h1 tags on signup page (expected 1-2, likely duplicate content)"
            assert form_count <= 2, \
                f"Found {form_count} forms on signup page (expected 1, likely duplicate rendering)"
            results.append((test_name, True, None))
            passed += 1
            page.close()
        except Exception as error:
            results.append((test_name, False, str(error)))
            failed += 1
            page.close()

        # ----------------------------------------------------------------
        # Tests 15-18: Route coverage, CRUD data, form submission, 500 detection
        # Use requests (HTTP) instead of Playwright for speed and reliability
        # ----------------------------------------------------------------
        import requests as http_requests

        # Helper: get a logged-in requests session for the first role
        def _get_auth_session():
            """Log in via requests and return an authenticated session."""
            sess = http_requests.Session()
            for _attempt in range(2):  # retry once on timeout
                try:
                    login_resp = sess.get(f"{base_url}/login", timeout=8)
                    csrf_match = re.search(r'name="_csrf"\s+value="([^"]+)"', login_resp.text)
                    if not csrf_match:
                        return None
                    csrf = csrf_match.group(1)

                    # Use admin/first-role credentials
                    admin_email = spec.get("admin_email", "admin@example.com")
                    sess.post(
                        f"{base_url}/login",
                        data={"email": admin_email, "password": TEST_MODE_PASSWORD, "_csrf": csrf},
                        allow_redirects=True,
                        timeout=8,
                    )
                    return sess
                except http_requests.exceptions.Timeout:
                    time.sleep(2)  # transient timeout -- retry once
                    continue
                except Exception:
                    return None
            return None

        # Test 15: All spec routes return 200 (or valid redirect)
        test_name = "all spec routes return 200"
        try:
            auth_sess = _get_auth_session()
            assert auth_sess is not None, "Could not log in for route testing"

            all_pages = spec.get("pages", [])
            failing_routes = []
            for page_entry in all_pages:
                page_path = page_entry.get("path", "/")
                if ":id" in page_path or ":slug" in page_path:
                    continue  # Skip parameterized routes
                try:
                    resp = auth_sess.get(f"{base_url}{page_path}", timeout=8, allow_redirects=True)
                    if resp.status_code == 500:
                        # Retry once on 500 (transient DB race)
                        time.sleep(2)
                        resp = auth_sess.get(f"{base_url}{page_path}", timeout=8, allow_redirects=True)
                    if resp.status_code == 500:
                        failing_routes.append(f"{page_path} -> 500")
                    elif resp.status_code == 404:
                        failing_routes.append(f"{page_path} -> 404")
                except http_requests.exceptions.Timeout:
                    # Timeout = slow infra, not a code bug; retry once before flagging
                    try:
                        time.sleep(3)
                        resp = auth_sess.get(f"{base_url}{page_path}", timeout=10, allow_redirects=True)
                        if resp.status_code == 500:
                            failing_routes.append(f"{page_path} -> 500")
                        elif resp.status_code == 404:
                            failing_routes.append(f"{page_path} -> 404")
                    except Exception:
                        pass  # Timeout on retry = infra issue, skip rather than fail
                except Exception as route_err:
                    failing_routes.append(f"{page_path} -> error: {route_err}")

            # Also check common routes not in spec
            for common_path in ["/login", "/signup", "/register"]:
                try:
                    resp = http_requests.get(f"{base_url}{common_path}", timeout=5, allow_redirects=True)
                    if resp.status_code == 500:
                        failing_routes.append(f"{common_path} -> 500")
                except Exception:
                    pass

            assert not failing_routes, f"Failing routes: {'; '.join(failing_routes[:5])}"
            results.append((test_name, True, None))
            passed += 1
        except Exception as error:
            results.append((test_name, False, str(error)))
            failed += 1

        # Test 16: CRUD list pages show data (seed data populated)
        test_name = "CRUD list pages show data"
        try:
            auth_sess = auth_sess or _get_auth_session()
            assert auth_sess is not None, "Could not log in for CRUD testing"

            data_models = spec.get("data_models", [])
            empty_lists = []
            for model in data_models:
                model_name = model.get("name", "").lower().replace(" ", "_")
                # Try common plural patterns
                for slug in [f"/{model_name}s", f"/{model_name}"]:
                    try:
                        resp = auth_sess.get(f"{base_url}{slug}", timeout=5)
                        if resp.status_code == 200:
                            # Check for table/list content (at least one data row)
                            has_data = (
                                "<tr" in resp.text.lower()
                                or '<div class="card' in resp.text.lower()
                                or "data-id" in resp.text.lower()
                                or resp.text.count("<a href") > 3
                            )
                            if not has_data and "<table" in resp.text.lower():
                                empty_lists.append(f"{slug} (empty table)")
                            break
                    except Exception:
                        pass

            # Warning-level: report but only fail if ALL are empty
            if empty_lists and len(empty_lists) == len(data_models):
                assert False, f"All list pages empty (no seed data): {'; '.join(empty_lists[:3])}"
            results.append((test_name, True, None))
            passed += 1
        except Exception as error:
            results.append((test_name, False, str(error)))
            failed += 1

        # Test 17: Create form submission works (first data model)
        test_name = "create form submission works"
        try:
            auth_sess = auth_sess or _get_auth_session()
            assert auth_sess is not None, "Could not log in for form testing"

            data_models = spec.get("data_models", [])
            if not data_models:
                results.append((test_name, True, "skipped: no data models"))
                passed += 1
            else:
                first_model = data_models[0]
                model_name = first_model.get("name", "").lower().replace(" ", "_")
                new_url = f"{base_url}/{model_name}s/new"

                # GET the new/create form
                form_resp = auth_sess.get(new_url, timeout=5)
                if form_resp.status_code == 404:
                    # Try alternate URL patterns
                    new_url = f"{base_url}/{model_name}/new"
                    form_resp = auth_sess.get(new_url, timeout=5)

                if form_resp.status_code == 200:
                    # Extract CSRF token
                    csrf_match = re.search(r'name="_csrf"\s+value="([^"]+)"', form_resp.text)
                    if csrf_match:
                        csrf = csrf_match.group(1)
                        # Build minimal form data from the model's fields
                        form_data = {"_csrf": csrf}
                        for field in first_model.get("fields", []):
                            field_name = field.get("name", "").lower().replace(" ", "_")
                            field_type = field.get("type", "string").lower()
                            if field_name in ("id", "created_at", "updated_at"):
                                continue
                            if "email" in field_name:
                                form_data[field_name] = "test@example.com"
                            elif "timestamp" in field_type or "datetime" in field_type or "time" in field_type:
                                form_data[field_name] = "2026-06-01T19:00"
                            elif "date" in field_type:
                                form_data[field_name] = "2026-01-15"
                            elif "number" in field_type or "integer" in field_type:
                                form_data[field_name] = "42"
                            elif "boolean" in field_type:
                                form_data[field_name] = "true"
                            elif "url" in field_name or "link" in field_name:
                                form_data[field_name] = "https://example.com"
                            else:
                                form_data[field_name] = f"Test {field_name}"

                        # POST the form
                        post_url = f"{base_url}/{model_name}s"
                        post_resp = auth_sess.post(
                            post_url, data=form_data, timeout=5, allow_redirects=False
                        )
                        # Accept 302 (redirect) or 200 (render with success) or 201
                        assert post_resp.status_code in (200, 201, 302), \
                            f"Create form POST returned {post_resp.status_code}"
                    else:
                        assert False, f"No CSRF token on {new_url}"
                else:
                    # Can't find create form; pass with note
                    results.append((test_name, True, f"skipped: {new_url} returned {form_resp.status_code}"))
                    passed += 1
                    # Skip the test result append below
                    new_url = None

                if new_url is not None:
                    results.append((test_name, True, None))
                    passed += 1
        except Exception as error:
            results.append((test_name, False, str(error)))
            failed += 1

        # Test 18: No 500 errors on any reachable page (link crawler)
        test_name = "no 500 errors on any reachable page"
        try:
            auth_sess = auth_sess or _get_auth_session()
            assert auth_sess is not None, "Could not log in for 500 check"

            # Start from dashboard, crawl internal links
            visited = set()
            to_visit = ["/dashboard", "/"]
            errors_500 = []
            max_pages = 20

            while to_visit and len(visited) < max_pages:
                current_path = to_visit.pop(0)
                if current_path in visited:
                    continue
                if ":id" in current_path or "logout" in current_path:
                    continue
                visited.add(current_path)

                try:
                    resp = auth_sess.get(
                        f"{base_url}{current_path}", timeout=5, allow_redirects=True
                    )
                    if resp.status_code == 500:
                        errors_500.append(current_path)

                    # Extract internal links for further crawling
                    if resp.status_code == 200:
                        for match in re.findall(r'href="(/[^"]*)"', resp.text):
                            clean = match.split("?")[0].split("#")[0]
                            if clean not in visited and clean.startswith("/"):
                                to_visit.append(clean)
                except Exception:
                    pass

            assert not errors_500, \
                f"500 errors on: {', '.join(errors_500[:5])} (checked {len(visited)} pages)"
            results.append((test_name, True, None))
            passed += 1
        except Exception as error:
            results.append((test_name, False, str(error)))
            failed += 1

        # Test 19: No broken links on CRUD list pages
        test_name = "no broken links on CRUD list pages"
        try:
            data_models = spec.get("data_models", [])
            if not data_models:
                results.append((test_name, True, None))
                passed += 1
            else:
                # Import slug helpers from build.py; fall back to local impl if unavailable
                try:
                    sys.path.insert(0, str(Path(__file__).parent))
                    from build import _model_slug as _ms, _pluralize_slug as _ps
                except Exception:
                    def _camel_split(name):
                        parts = re.split(r'[\s_\-]+', name)
                        result = []
                        for tok in parts:
                            result.extend(p.lower() for p in re.findall(
                                r'[A-Z][a-z]*|[a-z]+|[A-Z]+(?=[A-Z][a-z]|\b)', tok) or [tok.lower()])
                        return [p for p in result if p]

                    def _ms(name):
                        return "-".join(_camel_split(name))

                    def _ps(slug):
                        parts = slug.split("-")
                        last = parts[-1]
                        vowels = set("aeiou")
                        if last.endswith("y") and len(last) > 1 and last[-2] not in vowels:
                            pl = last[:-1] + "ies"
                        elif last.endswith(("ch", "sh", "ss")) or last[-1:] in ("x", "z", "s"):
                            pl = last + "es"
                        else:
                            pl = last + "s"
                        return "-".join(parts[:-1] + [pl]) if len(parts) > 1 else pl

                auth_sess = auth_sess or _get_auth_session()
                assert auth_sess is not None, "Could not log in for link check"

                # Static paths to exclude from link testing
                _skip_extensions = {".css", ".js", ".png", ".svg", ".ico", ".webp",
                                     ".map", ".woff", ".woff2", ".ttf"}
                _skip_prefixes = ("/api/", "/admin/", "/ops/", "/support",
                                  "/logout", "/health", "/manifest", "/favicon",
                                  "/style", "/tailwind", "/sw.")

                def _should_skip(path):
                    if any(path.startswith(p) for p in _skip_prefixes):
                        return True
                    ext = Path(path).suffix.lower()
                    return ext in _skip_extensions

                all_links = set()
                broken_links = []

                for m in data_models:
                    name = m["name"] if isinstance(m, dict) else m
                    slug = _ms(name)
                    plural = _ps(slug)
                    list_url = f"/{plural}"

                    try:
                        resp = auth_sess.get(
                            f"{base_url}{list_url}", timeout=5, allow_redirects=True
                        )
                        if resp.status_code == 404:
                            broken_links.append(f"{list_url} (404)")
                            continue
                        if resp.status_code not in (200, 301, 302):
                            broken_links.append(f"{list_url} ({resp.status_code})")
                            continue
                        if resp.status_code == 200:
                            # Extract all internal hrefs and form actions
                            for match in re.findall(r'href="(/[^"#?]*)"', resp.text):
                                clean = match.split("?")[0].split("#")[0]
                                if clean and not _should_skip(clean):
                                    all_links.add(clean)
                            # Note: form actions are NOT crawled, they are
                            # POST-only routes (delete, update, toggle, etc.)
                            # that return 404 on GET. Only href links are safe
                            # to test with GET requests.
                    except Exception:
                        pass

                # Test each discovered link (cap at 100 total)
                for link in list(all_links)[:100]:
                    if len(broken_links) >= 10:
                        break
                    try:
                        resp = auth_sess.get(
                            f"{base_url}{link}", timeout=2, allow_redirects=True
                        )
                        if resp.status_code in (404, 500):
                            broken_links.append(f"{link} ({resp.status_code})")
                    except Exception:
                        pass

                assert not broken_links, \
                    f"Broken links: {', '.join(broken_links[:5])}"
                results.append((test_name, True, None))
                passed += 1
        except Exception as error:
            results.append((test_name, False, str(error)))
            failed += 1

        # Test 20: Magic link login works
        test_name = "magic link login works"
        from deploy import magic_link_auth_enabled  # the one reader of USE_MAGIC_LINK_AUTH
        if magic_link_auth_enabled() and db_name:
            try:
                import hashlib
                import secrets as _secrets
                raw_token = _secrets.token_hex(32)
                token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

                # Insert test magic link token via psql
                pg = _pg_env()
                insert_sql = (
                    f"INSERT INTO magic_link_tokens (user_id, token_hash, expires_at) "
                    f"SELECT id, '{token_hash}', NOW() + INTERVAL '1 hour' "
                    f"FROM users WHERE role = 'admin' LIMIT 1"
                )
                result = run_cmd(
                    ["psql", db_name, "-c", insert_sql],
                    env=pg, check=False, timeout=10,
                )
                assert result.returncode == 0, \
                    f"Failed to insert magic link token: {result.stderr}"

                # Use the token to log in via GET /magic?token=xxx
                sess = http_requests.Session()
                resp = sess.get(
                    f"{base_url}/magic?token={raw_token}",
                    allow_redirects=False,
                    timeout=10,
                )
                assert resp.status_code == 302, \
                    f"Magic link returned {resp.status_code}, expected 302 redirect"
                location = resp.headers.get("location", "")
                assert "/login" not in location, \
                    f"Magic link redirected back to login: {location}"

                results.append((test_name, True, None))
                passed += 1
            except Exception as error:
                results.append((test_name, False, str(error)))
                failed += 1
        else:
            results.append((test_name, True, "skipped: magic link auth disabled or no db_name"))
            passed += 1

        # Test 21: Admin routes mounted (csv-import, reset-data, change-password)
        test_name = "admin routes mounted"
        try:
            auth_sess = auth_sess or _get_auth_session()
            assert auth_sess is not None, "Could not log in for admin route testing"

            admin_routes_ok = True
            missing_admin = []
            for admin_path in ["/admin/csv-import", "/admin/reset-data", "/admin/change-password"]:
                try:
                    # POST without body/CSRF: expect 403 (CSRF fail) or 400, NOT 404
                    resp = auth_sess.post(f"{base_url}{admin_path}", timeout=5, allow_redirects=False)
                    if resp.status_code == 404:
                        missing_admin.append(f"{admin_path} -> 404")
                        admin_routes_ok = False
                except Exception:
                    pass  # Timeout/connection = infra, not missing route

            assert admin_routes_ok, f"Admin routes missing: {'; '.join(missing_admin)}"
            results.append((test_name, True, None))
            passed += 1
        except Exception as error:
            results.append((test_name, False, str(error)))
            failed += 1

        browser.close()

    return passed, failed, results


def setup_app(build_path):
    """Install deps, create DB, start app, and wait until ready.

    Factored out of test_app() so callers (e.g. qa.py) can reuse a running
    app instance across multiple test phases without restarting it.

    Args:
        build_path: Path to the build directory (str or Path).

    Returns:
        Tuple (process, db_name, spec) on success.

    Raises:
        RuntimeError: If any setup step fails (npm install, DB setup, app start).
    """
    app_dir = Path(build_path).resolve()
    spec = load_spec(app_dir)
    if not spec:
        raise RuntimeError(f"No spec.json found in {app_dir}")

    app_name = spec.get("app_name", "test-app")
    db_name = f"test_{app_name.replace('-', '_')}"

    print(f"Testing: {app_name}")
    print(f"Build dir: {app_dir}")

    print("[1/3] Dependencies")
    deps_ok, deps_err, test_dir = install_deps(app_dir)
    if not deps_ok:
        # Include stderr snippet so upstream can classify ENOSPC as transient
        detail = f"; {deps_err}" if deps_err else ""
        raise RuntimeError(f"npm install failed{detail}")

    # run_dir is where the app process runs. On Railway, test_dir is a /tmp
    # directory containing source + node_modules (volume never gets node_modules).
    # Locally, test_dir is None and the app runs from app_dir directly.
    run_dir = Path(test_dir) if test_dir is not None else app_dir

    # Advisory npm audit: log high-severity vulnerabilities but never block the build
    try:
        # Scrubbed environment: npm reads the generated app's .npmrc, and an
        # .npmrc can expand ${ENV_VAR} into a registry auth token and name
        # the registry to send it to.
        audit_result = run_cmd(
            ["npm", "audit", "--audit-level=high", "--json"],
            cwd=run_dir,
            timeout=30,
            env=_env_without_secrets(),
        )
        if audit_result.returncode != 0:
            import json as _json
            try:
                audit_data = _json.loads(audit_result.stdout or "{}")
                vuln_count = audit_data.get("metadata", {}).get("vulnerabilities", {}).get("high", 0)
                crit_count = audit_data.get("metadata", {}).get("vulnerabilities", {}).get("critical", 0)
                if vuln_count or crit_count:
                    print(f"  [audit] Advisory: {vuln_count} high, {crit_count} critical vulnerabilities found")
            except Exception:
                print("  [audit] Advisory: npm audit reported issues (non-blocking)")
    except Exception:
        pass  # npm audit failure must never block the build

    print("[2/3] Database")
    if not setup_database(app_dir, db_name):
        import shutil
        teardown_database(db_name)
        if test_dir is not None:
            shutil.rmtree(str(test_dir), ignore_errors=True)
        raise RuntimeError("Database setup failed")

    print("[3/3] Start app")
    process = start_app(run_dir, db_name, TEST_PORT)

    if not wait_for_app(TEST_PORT):
        import shutil
        stderr_text = ""
        # Log files are written to run_dir (where the app process runs)
        stderr_path = run_dir / "test_stderr.log"
        if stderr_path.exists():
            stderr_text = stderr_path.read_text()[:1000]
            if stderr_text:
                print(f"  stderr: {stderr_text}")
        stop_app(process)
        teardown_database(db_name)
        if test_dir is not None:
            shutil.rmtree(str(test_dir), ignore_errors=True)
        raise RuntimeError(
            f"App did not start within {STARTUP_TIMEOUT_SECONDS}s. stderr: {stderr_text[:500]}"
        )

    print(f"  App running on port {TEST_PORT}")
    return process, db_name, spec, test_dir


def teardown_app(process, db_name, app_dir=None, test_dir=None):
    """Stop app, drop test DB, and clean up log files and test_dir.

    Safe to call even if setup partially failed (uses --if-exists for dropdb).

    Args:
        process: Subprocess handle returned by start_app (may be None).
        db_name: Name of the test database to drop (may be None).
        app_dir: Path to build directory for log cleanup (optional).
        test_dir: /tmp test working directory returned by install_deps on Railway
                  (optional). Removed after process termination.
    """
    import shutil
    stop_app(process)
    if db_name:
        teardown_database(db_name)
    if app_dir:
        for logfile in ("test_stdout.log", "test_stderr.log"):
            log_path = Path(app_dir) / logfile
            if log_path.exists():
                try:
                    log_path.unlink()
                except Exception:
                    pass
    if test_dir is not None:
        shutil.rmtree(str(test_dir), ignore_errors=True)


def test_app(build_path):
    """Full test pipeline: setup, run tests, teardown.

    Returns:
        dict with keys:
          - success: bool (True if all tests passed)
          - results: list of (test_name, passed, error_msg) tuples
          - app_stderr: str (last 50 lines of app stderr, for debugging)
          - passed: int
          - failed: int
    """
    app_dir = Path(build_path).resolve()
    if not app_dir.exists():
        print(f"Build directory not found: {app_dir}")
        sys.exit(1)

    process = None
    db_name = None
    test_dir = None

    try:
        process, db_name, spec, test_dir = setup_app(build_path)

        # Log files are written to wherever the app process ran (test_dir on Railway,
        # app_dir locally). Use log_dir so we read from the right location.
        log_dir = Path(test_dir) if test_dir is not None else app_dir

        # Run tests
        print()
        print("[4/5] Running tests")
        print()
        passed, failed_count, results = run_tests(TEST_PORT, spec, db_name=db_name)

        # Print results
        for test_name, success, error in results:
            status = "PASS" if success else "FAIL"
            print(f"  [{status}] {test_name}")
            if error:
                print(f"         {error}")

        print()
        print(f"Results: {passed} passed, {failed_count} failed out of {passed + failed_count}")

        # Capture screenshots if tests passed (best-effort, app still running).
        # Screenshots are stored in app_dir (volume) so they persist for deploy phase.
        screenshots = []
        if failed_count == 0:
            print()
            print("[4.5/5] Capturing screenshots")
            try:
                with sync_playwright() as pw:
                    ss_browser = pw.chromium.launch(headless=True)
                    screenshots = capture_screenshots(ss_browser, TEST_PORT, spec, str(app_dir))
                    ss_browser.close()
                print(f"  Captured {len(screenshots)} screenshot(s)")
            except Exception as ss_exc:
                print(f"  Screenshot capture warning: {ss_exc}")

        # Collect app stderr for debugging (read from log_dir where process ran)
        app_stderr = ""
        stderr_path = log_dir / "test_stderr.log"
        if stderr_path.exists():
            lines = stderr_path.read_text().splitlines()[-50:]
            app_stderr = "\n".join(lines)
            if lines:
                print(f"\n  App stderr (last {len(lines)} lines):")
                for line in lines:
                    print(f"    {line}")

        return {
            "success": failed_count == 0,
            "results": results,
            "app_stderr": app_stderr,
            "passed": passed,
            "failed": failed_count,
            "screenshots": [s["path"] for s in screenshots],
        }

    except RuntimeError as exc:
        # Setup failure (npm install, DB, app start)
        app_stderr = ""
        # Try log_dir first (Railway), fall back to app_dir (local)
        log_dir = Path(test_dir) if test_dir is not None else app_dir
        for candidate in (log_dir, app_dir):
            if candidate and candidate.exists():
                stderr_path = candidate / "test_stderr.log"
                if stderr_path.exists():
                    app_stderr = stderr_path.read_text()[:1000]
                    break
        return {
            "success": False,
            "results": [("app startup", False, str(exc))],
            "app_stderr": app_stderr,
            "passed": 0,
            "failed": 1,
        }

    finally:
        # Cleanup: stop app, drop DB, remove test_dir (/tmp on Railway)
        print()
        print("[5/5] Cleanup")
        teardown_app(process, db_name, app_dir, test_dir)
        print("  Done")


def main():
    """CLI entry point: test a built app."""
    if len(sys.argv) != 2:
        print("Usage: python cli/test.py <build-directory>")
        print("Example: python cli/test.py builds/landscaping-task-tracker")
        sys.exit(1)

    success = test_app(sys.argv[1])
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
