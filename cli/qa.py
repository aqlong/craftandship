# cli/qa.py - Spec-aware QA agent for generated apps.
#
# Reads spec.json to understand what the app should do, asks Claude Haiku to
# generate targeted Playwright tests for that specific app, executes them
# against a running instance, and returns structured results.
#
# Called by worker_pg.py as Stage 3.5, after smoke tests pass and before deploy.
# QA failures are non-blocking warnings; the build proceeds regardless.
#
# The generated test code is never executed in this process: it runs in a child
# process (cli/qa_runner.py) with a scrubbed environment. See _run_tests_in_child.

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

from api_client import make_client

# Import test.py helpers at module level so they are patchable in tests.
# qa.py and test.py both live in cli/; no circular dependency.
from test import (
    _env_without_secrets,
    install_deps,
    load_spec,
    setup_database,
    start_app,
    stop_app,
    teardown_database,
    wait_for_app,
    STARTUP_TIMEOUT_SECONDS,
)

load_dotenv()

# Use Haiku for cost savings (spec is ~2K tokens, test output ~3K tokens)
# QA is non-blocking; Haiku is sufficient for structured test generation.
MODEL = "claude-haiku-4-5"
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2

# Port for the QA-managed app instance (different from test.py's TEST_PORT=4567)
QA_TEST_PORT = 4568

# The child process that runs the generated tests (see cli/qa_runner.py), and
# the wall-clock limit after which its whole process group is killed.
_QA_RUNNER = Path(__file__).resolve().parent / "qa_runner.py"
QA_CHILD_TIMEOUT_SECONDS = 600

# Caps on what is read back from the child. Its result file is written by the
# same untrusted code, so it is data to bound, not a report to trust.
_MAX_CHILD_RESULT_BYTES = 1_000_000
_MAX_RESULT_ITEMS = 200
_MAX_RESULT_NAME_CHARS = 300
_MAX_RESULT_ERROR_CHARS = 2000

QA_SYSTEM_PROMPT = """You are a QA test generator for a CraftAndShip generated Express.js web app.
You receive the app's spec (JSON) which defines every page, data model, user role, and action.

Generate a Python function that tests this specific app using Playwright:

def run_qa_tests(page, base_url):
    results = {"passed": [], "failed": []}
    # Auth helper _cas_login_as_admin(page, base_url) IS ALREADY DEFINED above
    # your function. Call it once at the top before accessing protected pages.
    _cas_login_as_admin(page, base_url)
    # ... tests ...
    return results

Rules:
- Use ONLY sync Playwright page methods (goto, fill, click, locator, wait_for_load_state, etc.)
- The page argument is an already-launched sync Playwright page instance.
- AUTH: CAS apps use magic-link authentication. The seeded admin user has NO
  password set - attempting `page.fill("input[name='password']", ...)` will
  succeed but `page.click("button[type='submit']")` will return to /login
  with an auth error. DO NOT write your own login code.
  The pipeline has already defined `_cas_login_as_admin(page, base_url)` in
  the file above your function. It mints a magic-link token via psql, visits
  `/magic?token=X`, and establishes the admin session. CALL IT ONCE at the
  top of run_qa_tests before any protected-page test:
      _cas_login_as_admin(page, base_url)
  Do NOT use `admin@example.com` or any hardcoded email - the seeded admin
  email is spec-specific and the helper reads it from the DB.
- After each test, append {"name": "descriptive test name"} to results["passed"]
  or {"name": "descriptive test name", "error": "error message"} to results["failed"]
- Wrap EVERY test in try/except so one failure never aborts the rest
- Test every page for: HTTP 200 when accessed by an authorized role, key UI elements present, no error text visible
- Test at least one CRUD flow if the spec has create/update actions: navigate to list, fill create form, submit, verify record appears
- Test seed data visibility: list pages should show rows, not just empty states
- Test navigation: clicking main nav links should load the expected pages
- Use page.wait_for_load_state("load", timeout=15000) before assertions, inside try/except.
  Do NOT use "networkidle" as the wait state for form submissions or page navigation; it can
  timeout indefinitely if the app makes background requests (polling, analytics, etc.).
  Use "load" or "domcontentloaded" instead.
- Use resilient locators: prefer get_by_role(), get_by_label(), get_by_placeholder() over CSS selectors.
  NEVER use locator("text=X") or get_by_text("X") for navigation or page-title checks, see STRICT MODE rules below.
- STRICT MODE RULE: Every CAS app has a desktop nav + mobile nav (#mobileMenu) + page heading + footer.
  The SAME text appears 3-5 times per page. locator("text=Events") matches the desktop nav link, the mobile
  nav link, any heading containing "Events" (e.g. "Upcoming Events"), and the footer link simultaneously; 
  this raises StrictViolationError. To check you are on the right page, navigate with goto() and then assert
  on a unique page element: page.get_by_role("heading").first or a data element only on that page. To click
  a nav link, use page.get_by_role("navigation").get_by_role("link", name="Events").click() to scope to nav.
- STRICT MODE RULE: When a button/element appears in EVERY list row (e.g. "Done", "Delete", "Edit"),
  NEVER use get_by_role(name="Done") alone: it matches ALL rows and raises StrictViolationError.
  Instead use: page.locator("tr").nth(0).get_by_role("button", name="Done") or
  page.locator("[data-id]").first.get_by_role("button") to scope to one row.
- STRICT MODE RULE: When a form has multiple fields with the same label (e.g. two "Name" inputs),
  use page.get_by_label("Name").nth(0) to target the first one specifically.
- STRICT MODE RULE: Do NOT assume admin sub-pages exist at /admin/{model} paths. Check spec.pages
  for the actual admin route(s) defined. If spec only has /admin (not /admin/events, /admin/videos,
  etc.), all management is from that dashboard; navigate to /admin and discover actual sub-paths from
  links/buttons on that page. To test admin CRUD, try the model slug directly (e.g., GET /events may
  be the admin list when logged in as admin). NEVER hardcode /admin/{model} paths unless they appear
  explicitly in spec.pages.
- Set explicit timeouts on all waits: timeout=15000 for navigation/goto, timeout=10000 for clicks/fills
- For click/fill/goto that might timeout, wrap in try/except and mark as failed (don't abort all remaining tests)
- IMPORTANT: After filling a create/edit form and clicking submit, wait for the redirect using:
    page.wait_for_load_state("load", timeout=20000)
  Do NOT use "networkidle" here: create routes often redirect to a list page that continues loading.
  If the form does not redirect (AJAX), wait for a success message element instead.
- NEVER call string methods (.replace(), .split(), .strip(), etc.) on Playwright objects.
  page.goto() returns a Response object, not a string. page.locator() returns a Locator object, not a string.
  If you need the current URL as a string, use page.url (always a plain string).
- ROLE ACCESS TESTING: To test that a lower-privilege role CANNOT access an admin page,
  you MUST clear the session cookie first. CAS apps use POST-only logout forms
  (no GET /logout route), so page.goto("/logout") will 404. The seeded non-admin
  users also use magic-link auth (no password). Use:
    page.context.clear_cookies()
    page.goto(base_url + "/admin")       # expect 302 to /login without session
    # Or navigate to whichever admin URL you are checking.
  NEVER test role restrictions while still logged in as admin (the initial call
  to _cas_login_as_admin at the top of run_qa_tests uses the admin session).
- Return ONLY the Python function body starting with "def run_qa_tests(page, base_url):"
- No imports, no surrounding code, no markdown fences
- Keep tests under 200 lines total; focus on the most critical flows
- Do NOT test raw login form mechanics (filling username/password) -- smoke tests cover that.
  DO log in at the top of run_qa_tests so subsequent tests can access protected pages.
- Do NOT use async/await -- use sync Playwright only
"""


def _create_client():
    """Create an Anthropic client."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return make_client(api_key=api_key)


def _call_api(client, messages, use_batch=False):
    """Thin wrapper delegating to api_client.call_api."""
    from api_client import call_api as _shared_call_api
    system = [{
        "type": "text",
        "text": QA_SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]
    response, _metrics = _shared_call_api(
        client=client,
        messages=messages,
        model=MODEL,
        system=system,
        max_tokens=8192,
        use_batch=use_batch,
        module="qa",
        handle_continuation=False,
    )
    return response


def _extract_function_code(response_text: str) -> str:
    """Extract the Python function code from the Claude response.

    Handles code fence blocks (```python ... ```) and bare function definitions.
    """
    import re

    # Strip code fences if present
    code_block = re.search(r"```(?:python)?\n(.*?)```", response_text, re.DOTALL)
    if code_block:
        return code_block.group(1).strip()

    # Find the def block (greedy from first def to end)
    def_match = re.search(r"(def run_qa_tests\(.*)", response_text, re.DOTALL)
    if def_match:
        return def_match.group(1).strip()

    return response_text.strip()


# Standard auth prelude injected at the top of every generated qa_tests.py
# file. Makes CAS-app auth a fixed primitive (not a thing Claude has to
# reinvent correctly per build). Reads DATABASE_URL from env, mints a
# magic-link token via psql for the seeded admin user (WHERE role='admin'),
# visits /magic?token=X, waits for the session to establish. Playwright
# test bodies that Claude generates just call `_cas_login_as_admin(page,
# base_url)` once and everything else assumes an authenticated session.
#
# Detected class of bug 2026-04-18 on v21+v22 QA loops: Claude's generated
# test used `admin@example.com` + `test_password_for_ci` which does not
# match MW's seed (e2e@craftandship.example, NULL password, magic-link
# only). QA loop triggered self-correction iterations that sometimes
# WORSENED the artifact before reverting to pre-qa-fix snapshot. This
# standard prelude eliminates the first-pass auth failure entirely.
_CAS_LOGIN_AS_ADMIN_PRELUDE = '''\
# --- Pipeline-injected auth helper (see cli/qa.py) ---
# Call _cas_login_as_admin(page, base_url) at the top of run_qa_tests
# before any protected page. Uses magic-link auth; the generated app has
# no password-auth path when the admin user is seeded password=NULL.
import hashlib as _cas_hashlib
import os as _cas_os
import secrets as _cas_secrets
import subprocess as _cas_subprocess


def _cas_login_as_admin(page, base_url):
    """Establish an authenticated admin session on the Playwright page.

    Mints a fresh magic-link token, INSERT INTO magic_link_tokens via psql,
    then visits /magic?token=X which redirects to a session-established
    destination. Any caller-side tests after this see an authenticated
    admin cookie on the shared page context.

    Raises RuntimeError if DATABASE_URL is unset or the psql insert fails.
    """
    database_url = _cas_os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "_cas_login_as_admin: DATABASE_URL env var is required"
        )
    raw_token = _cas_secrets.token_hex(32)
    token_hash = _cas_hashlib.sha256(raw_token.encode()).hexdigest()
    insert_sql = (
        "INSERT INTO magic_link_tokens (user_id, token_hash, expires_at) "
        f"SELECT id, '{token_hash}', NOW() + INTERVAL '1 hour' "
        "FROM users WHERE role = 'admin' LIMIT 1"
    )
    result = _cas_subprocess.run(
        ["psql", database_url, "-c", insert_sql],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"_cas_login_as_admin: psql insert failed: {result.stderr[:200]}"
        )
    page.goto(base_url + "/magic?token=" + raw_token, timeout=15000)
    page.wait_for_load_state("load", timeout=15000)
# --- End pipeline-injected auth helper ---


'''


def generate_qa_tests(spec: dict, use_batch: bool = False) -> str:
    """Call Claude to generate spec-aware QA test code.

    Validates the generated code with compile() before returning.  If the first
    attempt has a syntax error, one retry is made with the error appended to the
    prompt so Claude can self-correct (qa-test-generation-syntax-error fix).

    Args:
        spec: The app spec dict.
        use_batch: Whether to use batch API mode.

    Returns:
        Python source code string for the run_qa_tests function.
    """
    spec_json = json.dumps(spec, indent=2)
    # Truncate large specs to stay within token budget
    if len(spec_json) > 4000:
        spec_json = spec_json[:4000] + "\n... (truncated)"

    base_prompt = (
        f"Generate Playwright QA tests for this app spec:\n\n{spec_json}\n\n"
        "Return only the run_qa_tests Python function."
    )

    client = _create_client()
    response = _call_api(client, [{"role": "user", "content": base_prompt}], use_batch=use_batch)
    raw = response.content[0].text if response.content else ""
    code = _extract_function_code(raw)

    # Validate syntax; retry once with the error hint if broken.
    try:
        compile(code, "<qa_tests>", "exec")
    except SyntaxError as exc:
        print(f"  [QA] Syntax error in generated test code ({exc}), retrying with fix hint...")
        retry_prompt = (
            base_prompt
            + f"\n\nIMPORTANT: Your previous response had a Python syntax error: {exc}. "
            "Please fix it and return only syntactically valid Python."
        )
        retry_response = _call_api(client, [{"role": "user", "content": retry_prompt}], use_batch=False)
        retry_raw = retry_response.content[0].text if retry_response.content else ""
        code = _extract_function_code(retry_raw)

    # Prepend the pipeline-injected _cas_login_as_admin auth helper so the
    # generated run_qa_tests body can call it. This is the "standard
    # widget" for build-time auth in qa tests - Claude does not reinvent
    # it per build, so auth never drifts against the seeded user shape.
    # Guard on the DEFINITION (not just a CALL site: Claude's body
    # typically calls `_cas_login_as_admin(page, base_url)` as instructed
    # in the prompt, which would false-trip a substring check).
    if "def _cas_login_as_admin" not in code:
        code = _CAS_LOGIN_AS_ADMIN_PRELUDE + code

    return code


class QaChildError(RuntimeError):
    """The QA child process could not produce a result (crash, timeout, bad output)."""


def _kill_process_group(process) -> None:
    """SIGKILL the child's process group.

    The child is started with start_new_session=True, so its pid is also its
    process group id and the group holds the Python child and whatever it
    started without detaching. The browser is launched by the Playwright
    driver; whether it is in this group, and whether it goes away when the
    driver is killed, is up to Playwright and has not been verified here.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _clip(value, limit: int) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def _last_line(path: Path, max_chars: int = 300) -> str:
    """Last non-empty line of a (possibly large) text file, clipped."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4096))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return "no error output"
    lines = [line for line in tail.strip().splitlines() if line.strip()]
    return _clip(lines[-1], max_chars) if lines else "no error output"


def _bounded_results(raw) -> dict:
    """Shape-check and bound the child's result (untrusted data)."""
    if not isinstance(raw, dict):
        return {
            "passed": [],
            "failed": [{"name": "QA test runner", "error": f"run_qa_tests returned {type(raw).__name__}, expected dict"}],
        }
    bounded = {"passed": [], "failed": []}
    for key in ("passed", "failed"):
        items = raw.get(key, [])
        if not isinstance(items, list):
            items = [items]
        for item in items[:_MAX_RESULT_ITEMS]:
            if isinstance(item, dict):
                entry = {"name": _clip(item.get("name", "unnamed test"), _MAX_RESULT_NAME_CHARS)}
                if key == "failed":
                    entry["error"] = _clip(item.get("error", "unknown error"), _MAX_RESULT_ERROR_CHARS)
            else:
                entry = {"name": _clip(item, _MAX_RESULT_NAME_CHARS)}
                if key == "failed":
                    entry["error"] = "unknown error"
            bounded[key].append(entry)
    return bounded


def _run_tests_in_child(test_code: str, base_url: str, build_path: Path,
                        database_url: Optional[str] = None,
                        timeout: Optional[float] = None) -> dict:
    """Run generated test code in a child Python process and return its raw results.

    The code is model-written and steered by the customer's spec, so it does
    not run in the worker. The child (cli/qa_runner.py) gets:
      * the scrubbed environment (no API keys, no Railway/Resend/GitHub
        credentials, not the worker's DATABASE_URL), plus DATABASE_URL for the
        throwaway test database when `database_url` is given (the injected
        _cas_login_as_admin helper needs it);
      * the build directory as its working directory;
      * its own session, so that on timeout its process group can be killed
        (see _kill_process_group for what that does and does not cover).
    The test file and the result file live in a private temporary directory.

    This keeps the worker's secrets out of the generated code's environment
    and memory. It is not a sandbox: same user, same container, same network.

    Returns a dict with "passed" and "failed" lists, bounded in size.
    Raises QaChildError if the child times out, crashes or writes no usable result.
    """
    limit = QA_CHILD_TIMEOUT_SECONDS if timeout is None else timeout
    env = _env_without_secrets()
    if database_url:
        env["DATABASE_URL"] = database_url

    with tempfile.TemporaryDirectory(prefix="cas_qa_") as tmp:
        test_path = Path(tmp) / "qa_tests.py"
        result_path = Path(tmp) / "result.json"
        stderr_path = Path(tmp) / "stderr.log"
        test_path.write_text(test_code, encoding="utf-8")

        # stderr goes to a file, not a pipe: the generated code can write an
        # unbounded amount and only the last line is ever reported.
        with open(stderr_path, "w", encoding="utf-8") as stderr_file:
            process = subprocess.Popen(
                [sys.executable, str(_QA_RUNNER), str(test_path), base_url, str(result_path)],
                cwd=str(build_path),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                start_new_session=True,
            )
            try:
                returncode = process.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                _kill_process_group(process)
                process.wait()
                raise QaChildError(f"generated tests exceeded the {int(limit)}s limit and were stopped") from None

        if returncode != 0:
            # A child that died can leave its Playwright driver behind.
            _kill_process_group(process)
            raise QaChildError(f"QA child exited with status {returncode}: {_last_line(stderr_path)}")
        try:
            if result_path.stat().st_size > _MAX_CHILD_RESULT_BYTES:
                raise QaChildError("QA child wrote an oversized result")
            raw = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise QaChildError(f"QA child wrote no usable result ({type(exc).__name__})") from None
    return _bounded_results(raw)


def _normalize_results(raw: dict) -> list:
    """Convert raw {passed: [...], failed: [...]} to a flat list of result dicts.

    Each result dict: {name: str, passed: bool, error: str|None}
    """
    results = []
    for item in raw.get("passed", []):
        name = item.get("name", str(item)) if isinstance(item, dict) else str(item)
        results.append({"name": name, "passed": True, "error": None})
    for item in raw.get("failed", []):
        if isinstance(item, dict):
            name = item.get("name", str(item))
            error = item.get("error", "unknown error")
        else:
            name = str(item)
            error = "unknown error"
        results.append({"name": name, "passed": False, "error": error})
    return results


def _skipped_result(reason: str) -> dict:
    """Return a 'QA skipped' result dict (success=True so it never blocks the build)."""
    return {
        "success": True,
        "passed": 0,
        "failed": 0,
        "results": [],
        "qa_report": f"QA skipped: {reason}",
        "skipped": True,
    }


def run_qa(build_dir: str, spec: dict, port: Optional[int] = None, tier: str = "build") -> dict:
    """Generate spec-aware QA tests with Claude and run them against the app.

    If port is provided, the caller is responsible for the app already running there.
    If port is None, run_qa starts and stops its own app instance on QA_TEST_PORT.

    QA failures are non-blocking: success=False means "tests found issues" but the
    caller (worker) still proceeds to deploy; it just logs the warnings.

    Args:
        build_dir: Path to the build directory.
        spec: App spec dict.
        port: Port of an already-running app instance, or None to manage our own.

    Returns:
        dict with:
          - success: bool (False if QA found failures; True if all passed or skipped)
          - passed: int
          - failed: int
          - results: list of {name, passed, error} dicts
          - qa_report: str summary
          - skipped: bool (True if QA was skipped due to setup/generation failure)
    """
    build_path = Path(build_dir).resolve()

    managed_app = port is None
    process = None
    db_name = None
    test_dir = None
    qa_port = port if port is not None else QA_TEST_PORT
    base_url = f"http://localhost:{qa_port}"

    try:
        if managed_app:
            # Start our own app instance on QA_TEST_PORT
            # Load spec from disk if available (may have been updated by fix.py)
            loaded_spec = load_spec(build_path)
            if loaded_spec:
                spec = loaded_spec

            app_name = spec.get("app_name", "test-app")
            db_name = f"test_qa_{app_name.replace('-', '_')}"

            print("  [QA] Installing npm dependencies...")
            install_ok, install_err, test_dir = install_deps(build_path)
            if not install_ok:
                return _skipped_result(f"npm install failed: {install_err[:200]}")

            # run_dir: /tmp test dir on Railway (source + node_modules), or build_path locally
            run_dir = Path(test_dir) if test_dir is not None else build_path

            print(f"  [QA] Setting up database: {db_name}")
            if not setup_database(build_path, db_name):
                teardown_database(db_name)
                db_name = None
                if test_dir is not None:
                    import shutil
                    shutil.rmtree(str(test_dir), ignore_errors=True)
                return _skipped_result("database setup failed")

            print(f"  [QA] Starting app on port {qa_port}...")
            process = start_app(run_dir, db_name, qa_port)

            if not wait_for_app(qa_port, timeout=STARTUP_TIMEOUT_SECONDS):
                stop_app(process)
                teardown_database(db_name)
                process = None
                db_name = None
                return _skipped_result("app failed to start for QA")

            print(f"  [QA] App ready at {base_url}")

        # Generate QA tests via Claude Sonnet
        print("  [QA] Generating spec-aware tests...")
        try:
            from api_client import should_use_batch
            test_code = generate_qa_tests(spec, use_batch=should_use_batch(tier))
        except Exception as gen_exc:
            print(f"  [QA] Test generation failed: {gen_exc}")
            return _skipped_result(f"test generation error: {gen_exc}")

        # Write to disk for debugging / qa_fix.py context
        test_file = build_path / "qa_tests.py"
        try:
            test_file.write_text(test_code)
            print(f"  [QA] Test file written: {test_file}")
        except Exception:
            pass  # Non-fatal; we still run from memory

        # Execute the generated tests in a child process (never in the worker).
        #
        # The pipeline-injected _cas_login_as_admin helper reads DATABASE_URL to
        # mint a magic-link token against the running app's DB. The child is
        # given the URL of the throwaway TEST database (where the generated
        # app's tables exist), built from the worker's database server account
        # the same way start_app() builds it for the app under test. The
        # worker's own os.environ is not touched, and the child never sees the
        # worker's DATABASE_URL. Without a managed test database (caller passed
        # `port`) the child gets no DATABASE_URL at all.
        test_db_url = None
        if db_name is not None:
            source_url = os.environ.get("DATABASE_URL") or ""
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
                test_db_url = f"postgres://{userinfo}{host}:{port_num}/{db_name}"
            else:
                test_db_url = f"postgres://localhost:5432/{db_name}"

        print("  [QA] Running tests in a child process (scrubbed environment)...")
        try:
            raw_results = _run_tests_in_child(test_code, base_url, build_path, database_url=test_db_url)
        except Exception as run_exc:
            print(f"  [QA] Test run error: {run_exc}")
            return _skipped_result(f"test run error: {run_exc}")

        results = _normalize_results(raw_results)
        passed_count = sum(1 for r in results if r["passed"])
        failed_count = sum(1 for r in results if not r["passed"])

        print(f"  [QA] Results: {passed_count} passed, {failed_count} failed")

        # Build human-readable report
        report_lines = [f"QA: {passed_count} passed, {failed_count} failed"]
        for r in results:
            if not r["passed"]:
                report_lines.append(f"  FAIL: {r['name']}: {r['error']}")
        qa_report = "\n".join(report_lines)

        return {
            "success": failed_count == 0,
            "passed": passed_count,
            "failed": failed_count,
            "results": results,
            "qa_report": qa_report,
            "skipped": False,
        }

    finally:
        if managed_app:
            if process is not None:
                stop_app(process)
            if db_name is not None:
                teardown_database(db_name)
            # Clean up log files left by start_app
            run_dir = Path(test_dir) if test_dir is not None else build_path
            for logfile in ("test_stdout.log", "test_stderr.log"):
                for log_path in (run_dir / logfile, build_path / logfile):
                    if log_path.exists():
                        try:
                            log_path.unlink()
                        except Exception:
                            pass
            # Clean up /tmp test working directory (Railway only)
            if test_dir is not None:
                import shutil
                shutil.rmtree(str(test_dir), ignore_errors=True)
