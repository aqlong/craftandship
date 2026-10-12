"""Design quality review agent for generated apps (Stage 2.75).

Runs after code review (review.py, Stage 2.5), before smoke tests.
Uses Claude Opus to evaluate source code against VISUAL_DESIGN_CHECKLIST,
STYLE_VISUAL_HOOKS, and UX_RULES_COMPACT from design_data.py.

Non-blocking: design review failures never fail the build.
"""
import json
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from api_client import call_api as _shared_call_api, make_client
from build_trace import emit_build_trace
from design_data import VISUAL_DESIGN_CHECKLIST, STYLE_VISUAL_HOOKS, UX_RULES_COMPACT

load_dotenv()

MODEL = "claude-opus-4-6"
_MAX_CONTEXT_CHARS = 80_000

# Files that may contain design-relevant middleware or font setup
# Priority files loaded first to ensure they fit within the context budget
# (alphabetical rglob loads admin/* before layout/login, exhausting the budget)
_KEY_FILES = [
    "server.js",
    "tailwind.config.js",
    "views/layout.ejs",
    "views/login.ejs",
    "views/register.ejs",
    "views/index.ejs",
    "views/contact.ejs",
    "views/events.ejs",
]

# Directories containing design-relevant files (views for layout, public for CSS)
_DESIGN_DIRS = ["views"]

DESIGN_REVIEW_SYSTEM_PROMPT = """You are a design quality reviewer for Craft and Ship, a platform that generates Express.js + PostgreSQL web apps.

Your job: review the generated source code for visual design quality against the provided design checklist, style hooks, and UX rules. Return a structured JSON report.

Return ONLY valid JSON, no commentary, no markdown fences, no text outside the JSON.

Schema:
{
  "passed": true|false,
  "score": <1-10>,
  "confidence": <1-5>,
  "critical_count": <int>,
  "warning_count": <int>,
  "issues": [
    {
      "severity": "critical"|"warning",
      "category": "<layout|colors|typography|spacing|navigation|responsive|animations|accessibility>",
      "file": "<filename or 'general'>",
      "issue": "<description>",
      "suggestion": "<concrete fix>"
    }
  ]
}

Confidence guide (how sure you are about your overall assessment):
  5 = Very confident: all checklist items clearly verifiable from source
  4 = Confident: most items verifiable, a few ambiguous
  3 = Moderate: several items ambiguous or context-dependent
  2 = Low: much depends on runtime rendering you cannot see
  1 = Very low, not enough source context to judge

Critical = ONLY these specific issues (nothing else is critical):
  - Login/register page uses a centered card on plain white instead of split-panel layout
  - Nav bar is white or transparent instead of solid dark/primary surface
  - Page backgrounds are flat white with no gradient or tint
  - No CSS custom properties or Tailwind config for the spec colors at all
  - Hero or section text is invisible/illegible against its background because the text color and the section background are both light (e.g. text-white / text-cream / text-ivory / text-amber-50 placed on bg-amber-50 / bg-stone-100 / bg-orange-50 / bg-cream / bg-ivory or any pale background; i.e. light-on-light), or both dark (text-slate-900 on bg-slate-900). This includes hero <h1>/<h2> tags whose text color brightness is within ~20 percentage points of the section background brightness
Warning = everything else, including: color contrast concerns, missing animations, inconsistent spacing, typography mismatches, breakpoint differences, missing hover states, suboptimal card layouts.

Score guide:
  9-10: Exceeds all checklist items, polished and cohesive
  7-8: Meets most checklist items with minor gaps
  5-6: Several checklist items missing
  3-4: Significant design quality issues
  1-2: Minimal design effort, looks like unstyled template

Be concise. Each issue should be one sentence."""


def _create_client():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return make_client(api_key=api_key)


def _collect_design_files(build_dir: Path) -> list:
    """Collect design-relevant source files, respecting the context limit.

    Collects:
      - Key files (server.js) for middleware/font checks
      - All .ejs files from views/
      - All .css files from public/
    Skips:
      - db/, routes/, node_modules/ and other non-design dirs
    """
    files = []
    total_chars = 0

    # Key files first
    for rel_path in _KEY_FILES:
        full_path = build_dir / rel_path
        if full_path.exists() and full_path.is_file():
            content = full_path.read_text(errors="replace")
            if total_chars + len(content) > _MAX_CONTEXT_CHARS:
                break
            files.append((rel_path, content))
            total_chars += len(content)

    # Views directory (all .ejs files)
    for dir_name in _DESIGN_DIRS:
        dir_path = build_dir / dir_name
        if not dir_path.is_dir():
            continue
        for file_path in sorted(dir_path.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.suffix not in (".ejs",):
                continue
            rel = str(file_path.relative_to(build_dir))
            if any(rel == f[0] for f in files):
                continue
            content = file_path.read_text(errors="replace")
            if total_chars + len(content) > _MAX_CONTEXT_CHARS:
                break
            files.append((rel, content))
            total_chars += len(content)

    # Public CSS files
    public_dir = build_dir / "public"
    if public_dir.is_dir():
        for file_path in sorted(public_dir.rglob("*.css")):
            if not file_path.is_file():
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


def _build_design_review_prompt(spec: dict, source_files: list) -> str:
    """Build the design review prompt with checklist, style hooks, and source files."""
    design = spec.get("design", {}) or {}
    style = design.get("style", "modern light")

    # Use get_design_context() for UUPM-aware effective palette; the post-processor
    # (_ensure_color_consistency) also uses get_design_context(), so the reviewer must
    # check against the same effective colors, not the raw spec values which may have
    # been overridden by UUPM for generic/dark placeholder colors.
    try:
        from design_data import get_design_context
        ctx = get_design_context(spec)
        palette = ctx.get("palette", {})
        effective_primary = palette.get("primary") or design.get("primary_color", "")
        effective_secondary = palette.get("secondary") or design.get("secondary_color", "")
        effective_accent = palette.get("accent") or design.get("accent_color", "")
        effective_font_display = ctx.get("font_display") or design.get("font_display", "")
        effective_font_body = ctx.get("font_body") or design.get("font_body", "")
    except Exception:
        effective_primary = design.get("primary_color", "")
        effective_secondary = design.get("secondary_color", "")
        effective_accent = design.get("accent_color", "")
        effective_font_display = design.get("font_display", "")
        effective_font_body = design.get("font_body", "")

    # Color summary (effective palette after UUPM overrides)
    colors_section = "## Effective Colors (after UUPM palette resolution)\n"
    if effective_primary:
        colors_section += f"  - primary_color: {effective_primary}\n"
    if effective_secondary:
        colors_section += f"  - secondary_color: {effective_secondary}\n"
    if effective_accent:
        colors_section += f"  - accent_color: {effective_accent}\n"

    # Font summary (effective fonts after UUPM overrides)
    fonts_section = "## Effective Fonts (after UUPM font resolution)\n"
    if effective_font_display:
        fonts_section += f"  - font_display: {effective_font_display}\n"
    if effective_font_body:
        fonts_section += f"  - font_body: {effective_font_body}\n"

    # Style-specific hooks
    style_hooks_section = ""
    hooks = STYLE_VISUAL_HOOKS.get(style)
    if hooks:
        style_hooks_section = f"## Style Visual Hooks (for '{style}' style)\n{hooks}\n"

    # Logo checks (conditional)
    logo_section = ""
    if spec.get("logo_data"):
        logo_section = (
            "## Logo Checks\n"
            "- Verify logo placement in nav bar (appropriate size, not stretched)\n"
            "- Check logo appears on login/register pages\n"
            "- Ensure logo colors complement the primary palette\n"
        )

    # Source files section
    source_section = "\n".join(
        f"--- {rel_path} ---\n{content}\n" for rel_path, content in source_files
    )

    return f"""Review this generated web app for visual design quality.

{colors_section}
{fonts_section}
{style_hooks_section}
## UX Rules
{UX_RULES_COMPACT}

## Design Checklist
{VISUAL_DESIGN_CHECKLIST}

{logo_section}
## What to check
1. Does the login/register page use a split-panel layout (not a centered card on white)?
2. Do page backgrounds use gradients or tinted colors (not flat white)?
3. Is the nav bar a solid dark or primary-colored surface (not white/transparent)?
3a. CRITICAL CONTRAST: For every hero/section, is the text color readable against the section background? Pay special attention to cream/beige/amber backgrounds (bg-amber-50, bg-stone-100, bg-orange-50, bg-cream, bg-ivory, bg-yellow-50, bg-rose-50, bg-pink-50, etc.); text-white / text-cream / text-ivory / text-amber-50 on these backgrounds is INVISIBLE and is a critical issue. Same for navy on navy or slate-900 on slate-900.
4. Do cards have hover animations and staggered fade-in?
5. Do dashboards use varied card sizes for visual hierarchy?
6. Are the spec colors actually used in the CSS/templates?
7. Are the spec fonts loaded and applied?
8. Is the layout responsive with proper mobile breakpoints?
9. Are status badges using appropriate color coding?
10. Do forms follow the UX rules (validate on blur, inline errors)?

## Source Files
{source_section}

Return the JSON design review report now."""


def review_design(build_dir: str, spec: dict, tier: str = "build") -> dict:
    """Review generated app design quality against the visual checklist.

    Args:
        build_dir: Absolute path to the built app directory.
        spec: The parsed spec dict.
        tier: Build tier for batch/realtime selection.

    Returns:
        Dict with keys: passed, score, critical_count, warning_count, issues.
        On any error, returns a safe skipped result so the build can continue.
    """
    build_path = Path(build_dir).resolve()
    if not build_path.is_dir():
        emit_build_trace(
            None,
            "design_review.skipped",
            {"reason": "build_dir_not_found", "build_dir": str(build_dir)},
        )
        return _skipped(f"Build dir not found: {build_dir}")

    emit_build_trace(
        build_path,
        "design_review.start",
        {"tier": tier, "model": MODEL},
    )

    source_files = _collect_design_files(build_path)
    if not source_files:
        emit_build_trace(
            build_path,
            "design_review.skipped",
            {"reason": "no_design_files"},
        )
        return _skipped("No design files found in build dir")

    prompt = _build_design_review_prompt(spec, source_files)
    client = _create_client()
    system = [{
        "type": "text",
        "text": DESIGN_REVIEW_SYSTEM_PROMPT,
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
            module="design_review",
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
        result.setdefault("score", 0)
        result.setdefault("confidence", 3)
        result.setdefault("critical_count", 0)
        result.setdefault("warning_count", 0)
        result.setdefault("issues", [])
        # Attach model attribution for downstream observability
        result["model"] = MODEL
        # Attach cost metadata
        if _metrics:
            result["api_cost_usd"] = getattr(_metrics, "estimated_cost_usd", 0.0)
        emit_build_trace(
            build_path,
            "design_review.completed",
            {
                "passed": result.get("passed"),
                "score": result.get("score"),
                "confidence": result.get("confidence"),
                "critical_count": result.get("critical_count"),
                "warning_count": result.get("warning_count"),
                "model": MODEL,
                "api_cost_usd": result.get("api_cost_usd", 0.0),
            },
        )
        return result
    except json.JSONDecodeError as exc:
        emit_build_trace(
            build_path,
            "design_review.skipped",
            {"reason": "json_parse_error", "error": str(exc)[:200]},
        )
        return _skipped(f"Could not parse design review JSON: {exc}")
    except Exception as exc:
        emit_build_trace(
            build_path,
            "design_review.skipped",
            {"reason": "unexpected_error", "error": str(exc)[:200]},
        )
        return _skipped(f"Unexpected error: {exc}")


def _skipped(reason: str) -> dict:
    return {
        "passed": True,  # Don't block on design review errors
        "score": 0,
        "critical_count": 0,
        "warning_count": 0,
        "summary": f"Design review skipped: {reason}",
        "issues": [],
        "skipped": True,
    }
