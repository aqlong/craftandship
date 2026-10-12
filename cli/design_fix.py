"""LEGACY v1 build engine (frozen): design fix loop. Superseded by the v2 engine in
compose/, where no model repairs build output; kept for reference only and run
only when CAS_BUILD_ENGINE=v1. Do not extend it. See cli/README.md.

Design fix agent for generated apps.

When design review finds critical issues (flat white backgrounds, transparent
navs, missing card shadows), this module asks Claude Opus to fix the relevant
CSS/layout files. Strict guardrails prevent modification of auth, DB, routing,
or server startup logic. Maximum one fix attempt per build.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

from api_client import call_api as _shared_call_api, make_client, BuildCancelled
from cli import cancellation

load_dotenv()

logger = logging.getLogger(__name__)


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
_MAX_CONTEXT_CHARS = 80_000

_PROTECTED_FILES = {
    "server.js",
    "middleware/auth.js",
    "routes/auth.js",
    "db/schema.sql",
    "db/seed.sql",
    "db/migrate.js",
    "db/index.js",
    "package.json",
}

_ALLOWED_EXTENSIONS = {".ejs", ".css"}

DESIGN_FIX_SYSTEM_PROMPT = """You are a design fix agent for CraftAndShip, a platform that generates Express.js + PostgreSQL web apps.

A generated app has passed functional tests but has visual/design issues found by the design review agent. You are given:
1. The design issues with severity, category, file path, problem description, and suggested fix
2. The relevant source files (EJS templates and CSS)
3. The design context: colors, fonts, and style from the app spec

Your job is to fix ONLY the CSS/Tailwind classes and HTML layout structure to resolve the design issues.

## What you CAN modify
- Tailwind CSS utility classes (bg-*, text-*, shadow-*, rounded-*, p-*, m-*, flex, grid, etc.)
- Inline styles for CSS custom properties (--color-primary, --color-secondary, etc.)
- HTML structure within EJS templates (adding wrapper divs, reorganizing layout elements)
- CSS custom properties and font-family declarations
- Background colors, gradients, shadows, and spacing
- Responsive classes (sm:, md:, lg:, xl:)

## What you MUST NEVER modify
- Authentication logic, CSRF tokens, session handling, or bcrypt
- Database queries, SQL, or migration files
- Route handlers (req, res logic), form action URLs, or redirect paths
- server.js, middleware/auth.js, routes/auth.js
- Package.json dependencies
- EJS logic (if/else conditions for auth, roles, permissions)
- Form hidden inputs (especially _csrf)

## Output format

For each file you fix, use this exact format:

===FILE: path/to/file.ext===
complete file contents here
===END FILE===

IMPORTANT:
- Output ONLY files that need changes. Do not output unchanged files.
- Output the COMPLETE file content, not just the diff.
- The path must match the original file's path exactly (e.g. views/layout.ejs, public/style.css).
- Only output .ejs and .css files. Never output .js, .sql, or .json files.
"""


def _collect_relevant_files(build_dir: Path, issues: list) -> list:
    """Collect EJS/CSS files relevant to the design issues.

    Always includes views/layout.ejs. Also collects files referenced in issues.
    Only collects .ejs and .css files. Respects _MAX_CONTEXT_CHARS limit.

    Returns list of (relative_path, content) tuples.
    """
    files = []
    seen = set()
    total_chars = 0

    # Always include layout.ejs
    layout_path = build_dir / "views" / "layout.ejs"
    if layout_path.exists() and layout_path.is_file():
        content = layout_path.read_text(errors="replace")
        files.append(("views/layout.ejs", content))
        seen.add("views/layout.ejs")
        total_chars += len(content)

    # Collect files referenced in issues
    for issue in issues:
        file_ref = issue.get("file", "")
        if not file_ref or file_ref in seen:
            continue
        # Only collect allowed extensions
        ext = Path(file_ref).suffix.lower()
        if ext not in _ALLOWED_EXTENSIONS:
            continue
        full_path = build_dir / file_ref
        if full_path.exists() and full_path.is_file():
            content = full_path.read_text(errors="replace")
            if total_chars + len(content) > _MAX_CONTEXT_CHARS:
                break
            files.append((file_ref, content))
            seen.add(file_ref)
            total_chars += len(content)

    return files


def _build_design_context_summary(spec: dict) -> str:
    """Extract design fields from spec and format as readable text.

    Returns a text block with colors, fonts, and style information.
    """
    design = spec.get("design", {})
    if not design:
        return "No design context available."

    lines = ["## Design Context\n"]

    primary = design.get("primary_color", "not set")
    secondary = design.get("secondary_color", "not set")
    accent = design.get("accent_color", "not set")
    font_display = design.get("font_display", "not set")
    font_body = design.get("font_body", "not set")
    style = design.get("style", "not set")

    lines.append(f"Primary color: {primary}")
    lines.append(f"Secondary color: {secondary}")
    lines.append(f"Accent color: {accent}")
    lines.append(f"Display font: {font_display}")
    lines.append(f"Body font: {font_body}")
    lines.append(f"Style: {style}")

    return "\n".join(lines)


def _build_fix_prompt(issues: list, source_files: list, design_context: str) -> str:
    """Combine issues, source files, and design context into a fix prompt.

    Formats issues as a numbered list with severity, file, problem, and suggestion.
    """
    parts = []

    # Design context
    parts.append(design_context)

    # Issues
    parts.append("\n\n## Design Issues to Fix\n")
    for i, issue in enumerate(issues, 1):
        severity = issue.get("severity", "unknown")
        category = issue.get("category", "unknown")
        file_ref = issue.get("file", "unknown")
        problem = issue.get("issue", "unknown")
        suggestion = issue.get("suggestion", "")
        parts.append(f"{i}. [{severity}] ({category}) {file_ref}")
        parts.append(f"   Problem: {problem}")
        if suggestion:
            parts.append(f"   Suggestion: {suggestion}")

    # Source files
    parts.append("\n\n## Source Files\n")
    for rel_path, content in source_files:
        parts.append(f"--- {rel_path} ---\n{content}\n")

    parts.append(
        "\n\nFix the design issues listed above. Output only the files that need changes. "
        "Do NOT modify auth logic, CSRF tokens, route handlers, or database queries."
    )

    return "\n".join(parts)


def _parse_fix_files(text: str) -> list:
    """Extract ===FILE: path=== ... ===END FILE=== blocks from response.

    Applies path traversal protection, protected file filtering, and extension
    allowlist filtering. Returns list of (path, content) tuples.
    """
    pattern = r"===FILE:\s*(.+?)\s*===\n(.*?)===END FILE==="
    matches = re.findall(pattern, text, re.DOTALL)
    files = []
    for filepath, content in matches:
        filepath = filepath.strip()
        # Security: prevent path traversal
        if ".." in filepath or filepath.startswith("/"):
            continue
        # Filter out protected files
        if filepath in _PROTECTED_FILES:
            continue
        # Filter by allowed extensions
        ext = Path(filepath).suffix.lower()
        if ext not in _ALLOWED_EXTENSIONS:
            continue
        files.append((filepath, content))
    return files


def fix_design(build_dir: str, spec: dict, issues: list, *,
               cancel_key: Optional[str] = None) -> dict:
    """Fix design issues in a generated app.

    Single attempt, Sonnet model, strict guardrails. Only modifies .ejs and .css
    files. Never touches auth, DB, routing, or server startup logic.

    Args:
        build_dir: Path to the build directory.
        spec: The app spec dict (must contain spec.design).
        issues: List of issue dicts from design review, each with:
            severity, category, file, issue, suggestion.
        cancel_key: Key of the attempt's cancel event. When the worker's
            supervisor fires it, this call raises BuildCancelled at its next
            checkpoint.

    Returns:
        Dict with success (bool), files_modified (list[str]), error (str|None).
    """
    try:
        # Key of the attempt's cancel event, handed over by the worker
        # (queue/worker_pg.py, _new_cancel_key; it begins with the build's
        # id). The model client is given the same key as its `build_id`: it
        # uses that argument to find the event and as the label on its log
        # lines.
        build_id = cancel_key

        # Cancel guard: iteration_top. Bail before any expensive work if the
        # watchdog supervisor has already fired for this build.
        _raise_if_cancelled(build_id, stage="design_fix:iteration_top")

        build_path = Path(build_dir).resolve()

        # Filter to critical issues only
        critical_issues = [
            issue for issue in issues
            if issue.get("severity") == "critical"
        ]
        if not critical_issues:
            return {"success": True, "files_modified": [], "error": None}

        # Collect relevant source files
        source_files = _collect_relevant_files(build_path, critical_issues)
        if not source_files:
            return {"success": True, "files_modified": [], "error": None}

        # Build prompt
        design_context = _build_design_context_summary(spec)
        prompt = _build_fix_prompt(critical_issues, source_files, design_context)

        # Check advisor toggle
        use_advisor = os.environ.get("USE_ADVISOR_DESIGN_FIX", "").lower()
        use_advisor = bool(use_advisor and use_advisor not in ("false", "0", "no"))

        prompt_text = DESIGN_FIX_SYSTEM_PROMPT
        effective_model = MODEL
        advisor_kwargs = {}
        if use_advisor:
            from fix import ADVISOR_GUIDANCE
            prompt_text = ADVISOR_GUIDANCE + DESIGN_FIX_SYSTEM_PROMPT
            effective_model = "claude-sonnet-4-6"
            advisor_kwargs = {"advisor_model": "claude-opus-4-6", "advisor_max_uses": 2}

        # Call API (single attempt, no retry)
        system = [{
            "type": "text",
            "text": prompt_text,
            "cache_control": {"type": "ephemeral"},
        }]
        client = make_client()
        response, _metrics = _shared_call_api(
            client=client,
            messages=[{"role": "user", "content": prompt}],
            model=effective_model,
            system=system,
            max_tokens=16384,
            use_batch=False,
            module="design_fix",
            handle_continuation=False,
            build_id=build_id,
            **advisor_kwargs,
        )

        # Cancel guard: post_api. The API call is the longest blocking step in
        # this loop, so re-check immediately after it returns.
        _raise_if_cancelled(build_id, stage="design_fix:post_api")

        response_text = response.content[0].text if response.content else ""

        # Parse fix files (with guardrails)
        fixed_files = _parse_fix_files(response_text)
        if not fixed_files:
            return {"success": True, "files_modified": [], "error": None}

        # Cancel guard: pre_subprocess. Last chance to bail before we touch the
        # filesystem with Claude's suggested fixes.
        _raise_if_cancelled(build_id, stage="design_fix:pre_subprocess")

        # Write fixed files to disk
        files_modified = []
        for rel_path, content in fixed_files:
            target = build_path / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            files_modified.append(rel_path)

        logger.info("Design fix applied %d file(s): %s",
                     len(files_modified), ", ".join(files_modified))

        return {"success": True, "files_modified": files_modified, "error": None}

    except BuildCancelled:
        # CRITICAL: re-raise cancellation so the supervisor can reap the
        # build thread. Must appear BEFORE the broad `except Exception`
        # below, otherwise a hung fix loop will swallow the supervisor
        # signal and keep burning budget. This is the 2026-04-11 incident.
        raise
    except Exception as exc:
        logger.warning("Design fix failed: %s", exc)
        return {"success": False, "files_modified": [], "error": str(exc)}
