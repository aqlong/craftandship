# cli/review.py - Code review agent for generated apps (Stage 2.5).
#
# Runs after build writes files, before test.py. Uses Claude Sonnet to verify
# the generated code against the spec: pages have routes/views, data models
# have CRUD, auth/roles are enforced, seed data is realistic, forms have CSRF.
#
# Non-blocking: review errors are logged but never fail the build.
# Called by worker_pg.py after _store_build_artifact().

import json
import os
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from api_client import call_api as _shared_call_api, make_client

load_dotenv()

MODEL = "claude-sonnet-4-6"
MAX_RETRIES = 3
_MAX_CONTEXT_CHARS = 80_000
_KEY_FILES = ["server.js", "package.json", "db/schema.sql", "db/seed.sql", "middleware/auth.js"]
_SOURCE_DIRS = ["routes", "views", "public"]

REVIEW_SYSTEM_PROMPT = """You are a code reviewer for Craft and Ship, a platform that generates Express.js + PostgreSQL web apps from specs.

Your job: review the generated source code against the app spec and return a structured JSON report.

Return ONLY valid JSON: no commentary, no markdown fences, no text outside the JSON.

Schema:
{
  "passed": true|false,
  "critical_count": <int>,
  "warning_count": <int>,
  "summary": "<one sentence>",
  "issues": [
    {"severity": "critical"|"warning", "file": "<filename or 'general'>", "issue": "<description>"}
  ]
}

Critical = the app will not work (missing route, broken auth, no CRUD for a model, app crashes).
Warning = degraded quality (missing seed data, weak UX, minor security gap).

Be concise. Each issue should be one sentence."""


def _create_client():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return make_client(api_key=api_key)


def _collect_source_files(build_dir: Path) -> list:
    """Collect source files from the build directory, respecting the context limit."""
    files = []
    total_chars = 0

    for rel_path in _KEY_FILES:
        full_path = build_dir / rel_path
        if full_path.exists() and full_path.is_file():
            content = full_path.read_text(errors="replace")
            files.append((rel_path, content))
            total_chars += len(content)

    for dir_name in _SOURCE_DIRS:
        dir_path = build_dir / dir_name
        if not dir_path.is_dir():
            continue
        for file_path in sorted(dir_path.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.suffix not in (".js", ".ejs", ".css", ".sql", ".json"):
                continue
            rel = str(file_path.relative_to(build_dir))
            if any(rel == f[0] for f in files):
                continue
            content = file_path.read_text(errors="replace")
            if total_chars + len(content) > _MAX_CONTEXT_CHARS:
                break
            files.append((rel, content))
            total_chars += len(content)

    return files


def _build_review_prompt(spec: dict, source_files: list) -> str:
    """Build the review prompt with spec-derived checklist and source files."""
    pages = spec.get("pages", [])
    data_models = spec.get("data_models", [])
    roles = spec.get("roles", [])

    # Derive expected routes from spec
    route_checklist = []
    for page in pages:
        name = page.get("name", "?")
        path = page.get("path", "?")
        auth = page.get("auth_required", False)
        page_roles = page.get("roles", roles)
        auth_note = f" (auth required, roles: {page_roles})" if auth else ""
        route_checklist.append(f"  - {name}: GET {path}{auth_note}")

    # Derive expected CRUD per data model. Respect read-only flags so the
    # reviewer does not false-positive on models that intentionally lack
    # create/update/delete routes (e.g. ContactMessage populated only by
    # the public contact form). See
    # docs/internals/WIDGETS_AND_CONTRACTS.md and the v20 review
    # findings.
    crud_checklist = []
    for model in data_models:
        model_name = model.get("name", "?")
        read_only = (
            model.get("read_only") is True
            or str(model.get("access", "")).lower() in ("read-only", "readonly", "ro")
            or str(model.get("permissions", "")).lower() in ("read", "view", "read-only")
        )
        if read_only:
            crud_checklist.append(
                f"  - {model_name}: list + read only (spec marks this model "
                f"read-only; do NOT expect create/update/delete routes or "
                f"views for {model_name})"
            )
        else:
            crud_checklist.append(
                f"  - {model_name}: list, create, read, update, delete routes + views"
            )

    # Build plural slug reference for check #9
    plural_slug_lines = []
    try:
        import sys
        from pathlib import Path as _Path
        sys.path.insert(0, str(_Path(__file__).parent))
        from build import _model_slug, _pluralize_slug
        for model in data_models:
            model_name = model.get("name", "?") if isinstance(model, dict) else model
            slug = _model_slug(model_name)
            plural = _pluralize_slug(slug)
            plural_slug_lines.append(
                f"  - {model_name}: use /{plural}/ everywhere. "
                f"NEVER use /{slug}/ in href or action.")
    except Exception:
        pass

    plural_slug_section = (
        "\n".join(plural_slug_lines)
        if plural_slug_lines else "  (derive from model names)"
    )

    spec_summary = "\n".join([
        f"App: {spec.get('app_name', '?')}",
        f"Description: {spec.get('description', '')}",
        f"Roles: {', '.join(roles) if roles else 'none'}",
        "",
        "Expected pages (verify each has a route handler AND a view):",
        "\n".join(route_checklist) if route_checklist else "  (none specified)",
        "",
        "Expected data models (verify full CRUD):",
        "\n".join(crud_checklist) if crud_checklist else "  (none specified)",
    ])

    source_section = "\n".join(
        f"--- {rel_path} ---\n{content}\n" for rel_path, content in source_files
    )

    return f"""Review this generated web app against its spec.

## Spec
{spec_summary}

## What to check
1. Every spec page has a route handler (GET at minimum) and an EJS view
2. Every data model has full CRUD (list, create, read, update, delete)
3. Auth-required pages call requireAuth middleware before the handler
4. Role-restricted pages check the user's role
5. POST /register and POST /login routes exist and work
6. db/seed.sql has 10+ realistic records per table (no placeholder data)
7. All POST forms include a CSRF token hidden field
8. User-supplied data in EJS uses <%= %> not <%- %>
9. CRITICAL: Plural slug consistency: ALL href and action attributes in EJS views must
   use the PLURAL slug path, never the singular. Using a singular slug causes every nav
   link and form to return 404.
{plural_slug_section}
   Check every .ejs file in views/. If ANY file contains href="/{'{singular_slug}'}/..."
   or action="/{'{singular_slug}'}/..." where it should be the plural, report it as a
   CRITICAL issue with the filename and the incorrect pattern found.
   Example: WRONG: href="/recipe/1"  CORRECT: href="/recipes/1"

## Source files
{source_section}

Return the JSON review report now."""


def review_build(build_dir: str, spec: dict, tier: str = "build") -> dict:
    """Review generated app code against spec before testing.

    Args:
        build_dir: Absolute path to the built app directory.
        spec: The parsed spec dict.
        tier: Build tier for batch/realtime selection.

    Returns:
        Dict with keys: passed, critical_count, warning_count, summary, issues.
        On any error, returns a safe skipped result so the build can continue.
    """
    build_path = Path(build_dir).resolve()
    if not build_path.is_dir():
        return _skipped(f"Build dir not found: {build_dir}")

    source_files = _collect_source_files(build_path)
    if not source_files:
        return _skipped("No source files found in build dir")

    prompt = _build_review_prompt(spec, source_files)
    client = _create_client()
    system = [{
        "type": "text",
        "text": REVIEW_SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]

    try:
        from api_client import should_use_batch
        response, _metrics = _shared_call_api(
            client=client,
            messages=[{"role": "user", "content": prompt}],
            model=MODEL,
            system=system,
            max_tokens=2048,
            use_batch=should_use_batch(tier),
            module="review",
            handle_continuation=False,
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        # Ensure required keys
        result.setdefault("passed", True)
        result.setdefault("critical_count", 0)
        result.setdefault("warning_count", 0)
        result.setdefault("summary", "Review complete")
        result.setdefault("issues", [])
        return result
    except json.JSONDecodeError as exc:
        return _skipped(f"Could not parse review JSON: {exc}")
    except Exception as exc:
        return _skipped(f"Unexpected error: {exc}")

    return _skipped("Max retries exceeded")


def _skipped(reason: str) -> dict:
    return {
        "passed": True,  # Don't block on review errors
        "critical_count": 0,
        "warning_count": 0,
        "summary": f"Review skipped: {reason}",
        "issues": [],
        "skipped": True,
    }
