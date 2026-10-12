# cli/fix.py - Self-correction for generated apps when tests fail.
#
# Reads test failure details + relevant source files from the build directory,
# asks Claude to diagnose and fix the code, then writes the corrected files.
# Called by worker_pg.py between test failure and re-test.

"""LEGACY v1 build engine (frozen): smoke-test fix loop. Superseded by the v2
engine in compose/, where no model repairs build output; kept for reference
only and run only when CAS_BUILD_ENGINE=v1. Do not extend it.
See cli/README.md."""

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

from api_client import call_api as _shared_call_api, make_client, BuildCancelled
from cli import cancellation

load_dotenv()


def _raise_if_cancelled(build_id, *, stage):
    """Raise BuildCancelled if the supervisor has fired this attempt's cancel event.

    `build_id` is the attempt's cancel key (see the caller). With no key the
    check is a no-op: a run outside the worker has no supervisor.

    Module-local helper (intentionally duplicated across fix loops) so that
    a refactor or rename of one fix module cannot silently remove the
    cooperative cancel check from another.
    """
    if not build_id:
        return
    event = cancellation.get(build_id)
    if event.is_set():
        raise BuildCancelled(build_id)

MODEL = "claude-opus-4-6"
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2

ADVISOR_GUIDANCE = """You have access to an `advisor` tool backed by a stronger model. Call it:
- Before writing any file fixes, to diagnose the root cause
- If your first approach produces files that still contain the error pattern from the test results
The advisor should respond in under 100 words and use enumerated steps, not explanations.

"""


def _get_advisor_config():
    """Return advisor parameters if USE_ADVISOR_FIX is enabled, else empty dict."""
    val = os.environ.get("USE_ADVISOR_FIX", "").lower()
    if val and val not in ("false", "0", "no"):
        return {"advisor_model": "claude-opus-4-6", "advisor_max_uses": 2}
    return {}


def _build_advisor_prompt(base_prompt):
    """Prepend advisor guidance to a system prompt."""
    return ADVISOR_GUIDANCE + base_prompt

# Files to always include in the fix context (if they exist in the build dir)
_KEY_FILES = [
    "server.js",
    "package.json",
    "middleware/auth.js",
    "db/schema.sql",
    "db/seed.sql",
    "db/migrate.js",
]

# Directories to scan for additional source files
_SOURCE_DIRS = ["routes", "views", "public"]

# Max total chars of source context to send to Claude (to stay within token limits)
_MAX_CONTEXT_CHARS = 80000

FIX_SYSTEM_PROMPT = """You are a code fixer for CraftAndShip, a platform that generates Express.js + PostgreSQL web apps.

A generated app's Playwright tests have failed. You are given:
1. The test results showing which tests passed and which failed with error details
2. The app's stderr output (if any)
3. The relevant source files from the build directory

Your job is to diagnose the root cause and output ONLY the fixed files.

## Output rules

For each file you need to fix, use this exact format:

--BEGINFILE-- path/to/file.ext
complete file contents here
--ENDFILE--

CRITICAL: --BEGINFILE-- and --ENDFILE-- must appear on their own lines. Never write them inside file content.

IMPORTANT:
- Output ONLY files that need changes. Do not output unchanged files.
- Output the COMPLETE file content, not just the diff.
- The path must match the original file's path exactly (e.g. server.js, routes/auth.js).
- Focus on fixing the test failures; do not refactor or add features.
- Common issues: missing routes, wrong redirect paths, missing middleware, auth logic errors,
  missing CSRF handling, incorrect SQL, missing seed data, port binding issues.
- If server.js does not have a /health endpoint, add one.
- Ensure login/logout flows work: POST /login should validate credentials against the database,
  set session, and redirect. GET /logout or POST /logout should destroy session and redirect to /login.
- All forms must include a CSRF token if the app uses CSRF protection.
- If the app crashes on startup (stderr shows errors), fix the crash first.
- When a route returns 404 or 500, check if server.js mounts the router for that path.
  If the spec page path differs from the model CRUD slug (e.g., /time vs /time-entry),
  the router must be mounted on BOTH paths in server.js.
- When fixing missing routes, ensure the route file EXISTS and EXPORTS a router.
"""


def _build_route_manifest(spec: dict) -> str:
    """Build an explicit route manifest from the spec for fix context.

    Lists every page path that MUST return 200, plus CRUD routes for each model.
    This gives Claude a clear target when fixing missing/broken routes.
    """
    if not spec:
        return ""

    lines = ["\n## ROUTE MANIFEST (every path below MUST return 200 when authenticated)\n"]

    # Page routes from spec
    pages = spec.get("pages", [])
    if pages:
        lines.append("### Spec pages:")
        for page in pages:
            path = page.get("path", "/")
            auth = "auth required" if page.get("auth_required", False) else "public"
            lines.append(f"  GET {path} ({auth})")

    # CRUD routes from data models
    data_models = spec.get("data_models", [])
    if data_models:
        lines.append("\n### CRUD model routes:")
        for model in data_models:
            name = model.get("name", "?") if isinstance(model, dict) else model
            # Use build.py's canonical slug helper for consistency
            try:
                from build import _model_slug
                slug = _model_slug(name)
            except Exception:
                slug = name.lower().replace(" ", "-").replace("_", "-")
            lines.append(
                f"  {name}: GET /{slug}s, GET /{slug}s/new, "
                f"GET /{slug}s/:id, GET /{slug}s/:id/edit"
            )

    # Detect page-to-model mismatches
    try:
        from build import _build_page_model_mapping
        mapping = _build_page_model_mapping(spec)
        if mapping.get("route_aliases"):
            lines.append("\n### CRITICAL route aliases (page path != CRUD slug):")
            for alias in mapping["route_aliases"]:
                lines.append(
                    f"  Page '{alias['page_path']}' -> Model '{alias['model_name']}' "
                    f"(CRUD at /{alias['crud_slug']}s)")
                lines.append(
                    f"  FIX: server.js must mount router on BOTH paths: "
                    f"app.use('{alias['page_path']}', require('./routes/{alias['crud_slug']}'));")
    except Exception:
        pass

    # Add plural slug reminder so fix attempts use correct plural paths in views
    try:
        from build import _build_plural_slug_rules
        plural_rules = _build_plural_slug_rules(spec)
        if plural_rules:
            lines.append(plural_rules)
    except Exception:
        pass

    lines.append("")
    return "\n".join(lines)


def _create_client():
    """Create an Anthropic client."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return make_client(api_key=api_key)


def _call_api(client, messages, system_prompt=FIX_SYSTEM_PROMPT,
              model=None, advisor_model=None, advisor_max_uses=2,
              build_id=None, wall_clock_budget_s=None):
    """Thin wrapper delegating to api_client.call_api. Always realtime.

    Forwards ``build_id`` so api_client can poll the cancellation event
    between retries on its side. ``wall_clock_budget_s`` is forwarded
    when the caller wants a tighter per-call cap than the shared
    _DEFAULT_WALL_CLOCK_BUDGET_S (used by deploy_fix.py to keep an
    iteration from burning the whole fix-loop budget on a throttle).
    """
    effective_model = model or MODEL
    system = [{
        "type": "text",
        "text": system_prompt,
        "cache_control": {"type": "ephemeral"},
    }]
    extra_kwargs = {}
    if advisor_model:
        extra_kwargs["advisor_model"] = advisor_model
        extra_kwargs["advisor_max_uses"] = advisor_max_uses
    if build_id is not None:
        extra_kwargs["build_id"] = build_id
    if wall_clock_budget_s is not None:
        extra_kwargs["wall_clock_budget_s"] = wall_clock_budget_s
    response, _metrics = _shared_call_api(
        client=client,
        messages=messages,
        model=effective_model,
        system=system,
        max_tokens=16384,
        use_batch=False,
        module="fix",
        handle_continuation=False,
        **extra_kwargs,
    )
    return response


def _parse_fix_files(text: str) -> list:
    """Extract file blocks from response text.

    Supports two delimiter formats Claude may use:
      1. Primary:  --BEGINFILE-- path\\n...content...\\n--ENDFILE--
      2. Fallback: ===FILE: path\\n...content...\\n===END===  (Claude sometimes uses this)
    Validates parsed filenames to skip malformed matches where the delimiter
    leaked into the path (e.g. path > 80 chars or contains '===').
    """
    import re
    files = []

    # Primary format
    primary = re.findall(
        r"^--BEGINFILE--[ \t]+(.+?)\s*\n(.*?)^--ENDFILE--",
        text, re.DOTALL | re.MULTILINE
    )
    files.extend(primary)

    # Fallback: ===FILE: path / ===END=== or similar triple-equals variants
    if not files:
        fallback = re.findall(
            r"^===+FILE:[ \t]+(.+?)\s*\n(.*?)^===+END===",
            text, re.DOTALL | re.MULTILINE
        )
        files.extend(fallback)

    result = []
    for filepath, content in files:
        filepath = filepath.strip()
        # Security: prevent path traversal
        if ".." in filepath or filepath.startswith("/"):
            continue
        # Sanity check: reject malformed filenames where delimiter leaked into path
        if len(filepath) > 80 or "===" in filepath or "--BEGINFILE" in filepath:
            continue
        # Guard against embedded --BEGINFILE-- markers inside content. Claude
        # occasionally emits a duplicated begin marker on long continuations
        # without a matching --ENDFILE-- between copies, which the non-greedy
        # outer regex captures as one big block. Split on any embedded marker
        # and keep the last copy (the most recently-written version).
        if re.search(r"^--BEGINFILE--[ \t]+\S", content, re.MULTILINE):
            parts = re.split(r"^--BEGINFILE--[ \t]+\S.*\n", content, flags=re.MULTILINE)
            content = parts[-1]
            print(f"  [fix parser] Stripped embedded --BEGINFILE-- marker from {filepath}")
        result.append((filepath, content))
    return result


def _collect_source_files(build_dir: Path) -> list:
    """Collect relevant source files from the build directory.

    Returns list of (relative_path, content) tuples.
    Respects _MAX_CONTEXT_CHARS limit.
    """
    files = []
    total_chars = 0

    # Key files first
    for rel_path in _KEY_FILES:
        full_path = build_dir / rel_path
        if full_path.exists() and full_path.is_file():
            content = full_path.read_text(errors="replace")
            files.append((rel_path, content))
            total_chars += len(content)

    # Then scan source directories
    for dir_name in _SOURCE_DIRS:
        dir_path = build_dir / dir_name
        if not dir_path.is_dir():
            continue
        for file_path in sorted(dir_path.rglob("*")):
            if not file_path.is_file():
                continue
            # Skip non-source files
            if file_path.suffix not in (".js", ".ejs", ".css", ".sql", ".json", ".html"):
                continue
            rel = str(file_path.relative_to(build_dir))
            if any(rel == existing[0] for existing in files):
                continue
            content = file_path.read_text(errors="replace")
            if total_chars + len(content) > _MAX_CONTEXT_CHARS:
                break
            files.append((rel, content))
            total_chars += len(content)

    return files


def _format_test_results(test_results: list) -> str:
    """Format test results for the prompt."""
    lines = []
    for test_name, success, error in test_results:
        status = "PASS" if success else "FAIL"
        lines.append(f"[{status}] {test_name}")
        if error:
            lines.append(f"  Error: {error}")
    return "\n".join(lines)


def _format_source_files(source_files: list) -> str:
    """Format source files for the prompt."""
    parts = []
    for rel_path, content in source_files:
        parts.append(f"--- {rel_path} ---\n{content}\n")
    return "\n".join(parts)


def attempt_fix(
    build_dir: str,
    test_results: list,
    app_stderr: str = "",
    spec: Optional[dict] = None,
    *,
    cancel_key: Optional[str] = None,
) -> dict:
    """Attempt to fix a generated app based on test failures.

    Args:
        build_dir: Path to the build directory.
        test_results: List of (test_name, passed, error_msg) tuples from test.py.
        app_stderr: The app's stderr output (last 50 lines).
        spec: The original app spec (optional, for context).
        cancel_key: Key of the attempt's cancel event. When the worker's
            supervisor fires it, this call raises BuildCancelled at its next
            checkpoint (before the model call, during it, after it, and
            before any file is written).

    Returns:
        Dict with:
          - fixed: bool (True if files were written)
          - files_changed: list of file paths that were updated
          - description: str summary of what was fixed
    """
    # Key of the attempt's cancel event, handed over by the worker
    # (queue/worker_pg.py, _new_cancel_key; it begins with the build's id).
    # The model client is given the same key as its `build_id`: it uses that
    # argument to find the event and as the label on its log lines.
    build_id = cancel_key

    # Cancel guard: iteration_top. Bail before any expensive work if the
    # watchdog supervisor has already fired for this build.
    _raise_if_cancelled(build_id, stage="fix:iteration_top")

    build_path = Path(build_dir).resolve()

    # Collect source files
    source_files = _collect_source_files(build_path)
    if not source_files:
        return {"fixed": False, "files_changed": [], "description": "No source files found"}

    # Build the prompt
    failed_tests = [(name, passed, err) for name, passed, err in test_results if not passed]
    if not failed_tests:
        return {"fixed": False, "files_changed": [], "description": "No failed tests to fix"}

    prompt_parts = []
    prompt_parts.append("## Test Results\n")
    prompt_parts.append(_format_test_results(test_results))

    if app_stderr:
        # The app under test runs with a database URL for the test database;
        # keep that (and anything else secret-shaped it printed) out of the
        # prompt, which leaves for a third party.
        from failure_kb import redact_text
        prompt_parts.append("\n\n## App stderr (last 50 lines)\n")
        prompt_parts.append(redact_text(app_stderr[:3000]))

    if spec:
        prompt_parts.append("\n\n## App Spec\n")
        prompt_parts.append(json.dumps(spec, indent=2)[:5000])

        # Add explicit route manifest so Claude knows exactly what routes must exist
        route_manifest = _build_route_manifest(spec)
        if route_manifest:
            prompt_parts.append(route_manifest)

    prompt_parts.append("\n\n## Source Files\n")
    prompt_parts.append(_format_source_files(source_files))

    # Check failure KB for hints from past fixes
    error_summary = "; ".join(err for _, _, err in failed_tests if err)
    try:
        from failure_kb import get_prompt_hints
        kb_hints = get_prompt_hints(error_summary)
    except Exception:
        kb_hints = ""

    if kb_hints:
        prompt_parts.append(kb_hints)

    prompt_parts.append("\n\nFix the failing tests. Output only the files that need changes.")

    prompt = "\n".join(prompt_parts)

    # Call Claude
    client = _create_client()
    advisor_config = _get_advisor_config()
    effective_model = "claude-sonnet-4-6" if advisor_config else None
    effective_prompt = _build_advisor_prompt(FIX_SYSTEM_PROMPT) if advisor_config else FIX_SYSTEM_PROMPT
    response = _call_api(
        client, [{"role": "user", "content": prompt}],
        system_prompt=effective_prompt,
        model=effective_model,
        build_id=build_id,
        **advisor_config,
    )

    # Cancel guard: post_api. The API call is the longest blocking step in
    # this loop, so re-check immediately after it returns.
    _raise_if_cancelled(build_id, stage="fix:post_api")

    response_text = response.content[0].text if response.content else ""

    # Parse and write fixed files
    fixed_files = _parse_fix_files(response_text)

    # Cancel guard: pre_subprocess. Last chance to bail before we touch the
    # filesystem with Claude's suggested fixes.
    _raise_if_cancelled(build_id, stage="fix:pre_subprocess")
    if not fixed_files:
        # Record failed attempt so KB tracks errors that resist fixing
        try:
            from failure_kb import record_fix
            record_fix(error_summary, "Claude returned no file fixes", [],
                        fix_category="unknown", succeeded=False)
        except Exception:
            pass
        return {"fixed": False, "files_changed": [], "description": "Claude returned no file fixes"}

    files_changed = []
    for rel_path, content in fixed_files:
        target = build_path / rel_path
        # Create parent dirs if needed
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        files_changed.append(rel_path)

    description = f"Fixed {len(files_changed)} file(s): {', '.join(files_changed)}"
    category = _classify_fix(files_changed)

    # NOTE: We do NOT record succeeded=True here, the fix may still fail tests.
    # The worker records the actual outcome after tests re-run (see worker_pg.py).
    return {
        "fixed": True,
        "files_changed": files_changed,
        "description": description,
        "error_summary": error_summary,  # pass back so worker can record true outcome
        "fix_category": category,
    }


def _classify_fix(files_changed: list) -> str:
    """Classify a fix into a category based on files that were changed."""
    files_str = " ".join(files_changed).lower()
    if "auth" in files_str or "login" in files_str:
        return "auth"
    if "seed" in files_str or "migrate" in files_str or "schema" in files_str:
        return "seed_data"
    if "session" in files_str:
        return "session"
    if "layout" in files_str or "style" in files_str:
        return "styling"
    if "route" in files_str:
        return "routing"
    if "view" in files_str or ".ejs" in files_str:
        return "views"
    return "unknown"
