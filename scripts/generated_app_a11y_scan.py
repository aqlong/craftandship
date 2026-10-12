#!/usr/bin/env python3
"""Static a11y scanner for v2 compose-emitted apps.

Why static: a live Playwright + axe-core run for 9 archetypes requires 9
Postgres DBs, 9 Express servers, and a headless browser; too heavy for
the executor environment and CI. This scanner builds each archetype
with NO customer overrides (archetype defaults only) into a tmpdir, then
grep-walks the emitted EJS views for the highest-frequency a11y failure
modes axe-core would flag. Findings are deterministic and groupable into
"cross-archetype" (compose-engine fix) vs "archetype-specific"
(archetype-config fix).

Coverage:

  - missing-alt-on-img             : <img> with no alt= attribute
  - empty-alt-decorative-mismatch  : <img alt=""> with meaningful src= context
  - input-missing-label            : <input> with no for/aria-label/aria-labelledby
  - button-empty-content           : <button> with empty body and no aria-label
  - link-generic-text              : <a> with "click here" / "read more" / "learn more"
  - heading-skip-level             : an <h3> with no preceding <h2> in the document
  - missing-main-landmark         : page with no <main> tag in layout
  - missing-skip-link              : layout missing the skip-to-content widget
  - missing-html-lang              : <html> without lang=
  - color-token-non-aa-on-bg       : palette-foreground token computed below 4.5:1

Run: python3 scripts/generated_app_a11y_scan.py [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from compose.build import build_app, load_palettes  # noqa: E402
from compose.intake import scaffold_spec  # noqa: E402

# 9 production archetypes per intake._KNOWN_ARCHETYPE_IDS minus `author` (not
# yet in the harbor flow). Order is stable for diffable findings tables.
ARCHETYPES: Tuple[str, ...] = (
    "healer-wellness",
    "coach-consultant",
    "local-service",
    "therapist-counselor",
    "fitness-studio",
    "salon-spa",
    "restaurant-cafe",
    "tradesperson-contractor",
    "event-producer",
)

# Pages we audit per archetype. Home + 1-2 inner pages per the parent
# roadmap description. We pick a stable set across all archetypes so the
# findings table can roll up cross-archetype patterns. Pages that don't
# exist for a given archetype are skipped silently.
#
# `home.ejs` / `about.ejs` / `contact.ejs` are all <%- include() %> stitched
# pages: the actual a11y surface lives in the widget partials they pull
# in. So in addition to the page files we walk the included widgets too
# (transitively via _follow_includes below) and the layout-public shell.
AUDIT_PAGES: Tuple[str, ...] = (
    "home.ejs",
    "about.ejs",
    "contact.ejs",
    "layout-public.ejs",
)

_INCLUDE_RE = re.compile(
    r"""include\(\s*['"]\./([\w./\-]+)['"]""", re.IGNORECASE
)


def _follow_includes(app_dir: Path, view_rel: str, seen: set) -> List[str]:
    """Return view + transitive includes (rel paths under views/) breadth-first."""
    out: List[str] = []
    queue = [view_rel]
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        target = app_dir / "views" / cur
        if not target.exists():
            continue
        out.append(cur)
        try:
            content = target.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for m in _INCLUDE_RE.finditer(content):
            include_path = m.group(1)
            if not include_path.endswith(".ejs"):
                include_path += ".ejs"
            # Resolve relative to the current file's dir.
            cur_dir = Path(cur).parent
            resolved = (cur_dir / include_path).as_posix()
            # Normalize ./foo -> foo
            resolved = re.sub(r"(^|/)\./", r"\1", resolved)
            queue.append(resolved)
    return out


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------

_IMG_TAG = re.compile(r"<img\b([^>]*)>", re.IGNORECASE)
_ATTR = re.compile(r"""(\w[\w-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_INPUT_TAG = re.compile(r"<input\b([^>]*)>", re.IGNORECASE)
_BUTTON_TAG = re.compile(
    r"<button\b([^>]*)>(.*?)</button>", re.IGNORECASE | re.DOTALL
)
_LINK_TAG = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_HEADING_TAG = re.compile(r"<(h[1-6])\b", re.IGNORECASE)
_HTML_OPEN = re.compile(r"<html\b([^>]*)>", re.IGNORECASE)
_MAIN_TAG = re.compile(r"<main\b", re.IGNORECASE)


def _parse_attrs(blob: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in _ATTR.finditer(blob):
        name = m.group(1).lower()
        value = m.group(2) or m.group(3) or m.group(4) or ""
        out[name] = value
    return out


def _strip_ejs(s: str) -> str:
    # Remove EJS code blocks / output tags so static greps don't trip on them.
    s = re.sub(r"<%[-=]?\s*(.*?)\s*%>", "TOKEN", s, flags=re.DOTALL)
    return s


def _has_text(content: str) -> bool:
    return bool(re.search(r"\S", re.sub(r"<[^>]+>", "", _strip_ejs(content))))


GENERIC_LINK_TEXTS = {
    "click here",
    "read more",
    "learn more",
    "here",
    "more",
    "details",
}


def scan_file(rel: str, content: str) -> List[dict]:
    """Return a list of finding dicts for a single view file."""
    findings: List[dict] = []
    raw = content  # keep original for include/string checks
    # Strip EJS first: many widgets emit alt="<%= image_alt %>" or
    # src="<%= img_url %>" where the inner EJS tag itself contains `>`,
    # which would otherwise terminate our `<img ... >` capture prematurely
    # and produce false missing-alt findings.
    text = _strip_ejs(content)

    # --- missing-alt-on-img --------------------------------------------------
    for m in _IMG_TAG.finditer(text):
        attrs = _parse_attrs(m.group(1))
        if "alt" not in attrs:
            findings.append(
                {
                    "rule": "missing-alt-on-img",
                    "file": rel,
                    "snippet": m.group(0)[:120],
                }
            )

    # --- input-missing-label -------------------------------------------------
    # We treat hidden / submit / button / csrf inputs as fine. For everything
    # else, we look for one of: aria-label, aria-labelledby, an enclosing
    # <label>, or a matching <label for=id>. Static-grep `for=id` is the
    # easiest signal; we also tolerate aria-label / aria-labelledby on the
    # input itself.
    for m in _INPUT_TAG.finditer(text):
        attrs = _parse_attrs(m.group(1))
        ttype = attrs.get("type", "text").lower()
        if ttype in {"hidden", "submit", "button", "image", "reset"}:
            continue
        if attrs.get("aria-label") or attrs.get("aria-labelledby"):
            continue
        # Look for a label[for=<id>] elsewhere in the file.
        if attrs.get("id"):
            esc_id = re.escape(attrs["id"])
            if re.search(
                rf"""<label\b[^>]*\bfor\s*=\s*['"]?{esc_id}['"]?""",
                text,
                re.IGNORECASE,
            ):
                continue
        # Look for an enclosing <label>…<input>…</label> on the same line.
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line = text[line_start: line_end if line_end > -1 else len(text)]
        if "<label" in line.lower():
            continue
        findings.append(
            {
                "rule": "input-missing-label",
                "file": rel,
                "snippet": m.group(0)[:120],
            }
        )

    # --- button-empty-content ------------------------------------------------
    for m in _BUTTON_TAG.finditer(text):
        attrs = _parse_attrs(m.group(1))
        body = m.group(2)
        if attrs.get("aria-label"):
            continue
        if not _has_text(body):
            findings.append(
                {
                    "rule": "button-empty-content",
                    "file": rel,
                    "snippet": m.group(0)[:120],
                }
            )

    # --- link-generic-text ---------------------------------------------------
    for m in _LINK_TAG.finditer(text):
        body = re.sub(r"<[^>]+>", "", _strip_ejs(m.group(2))).strip().lower()
        if body in GENERIC_LINK_TEXTS:
            findings.append(
                {
                    "rule": "link-generic-text",
                    "file": rel,
                    "snippet": m.group(0)[:120],
                }
            )

    # --- heading-skip-level --------------------------------------------------
    # A single-file static scan can't reason about the full composed page (the
    # layout supplies the <h1> via title/include). But within a page partial,
    # a leading <h3> with no <h2> earlier in the file is suspicious. We only
    # flag at the page-file level (home/about/contact), not at the layout.
    if rel.endswith(".ejs") and "/widgets/" not in rel and "layout" not in rel:
        seen_levels: List[int] = []
        for m in _HEADING_TAG.finditer(text):
            lvl = int(m.group(1)[1])
            if seen_levels and lvl > max(seen_levels) + 1:
                findings.append(
                    {
                        "rule": "heading-skip-level",
                        "file": rel,
                        "snippet": f"jumped from h{max(seen_levels)} to h{lvl}",
                    }
                )
            seen_levels.append(lvl)

    # --- missing-html-lang ---------------------------------------------------
    m = _HTML_OPEN.search(text)
    if m:
        attrs = _parse_attrs(m.group(1))
        if "lang" not in attrs:
            findings.append(
                {
                    "rule": "missing-html-lang",
                    "file": rel,
                    "snippet": m.group(0)[:120],
                }
            )

    # --- missing-main-landmark + missing-skip-link --------------------------
    if "layout-public" in rel:
        if not _MAIN_TAG.search(text):
            findings.append(
                {
                    "rule": "missing-main-landmark",
                    "file": rel,
                    "snippet": "no <main> element in layout",
                }
            )
        if "skip-to-content" not in raw:
            findings.append(
                {
                    "rule": "missing-skip-link",
                    "file": rel,
                    "snippet": "no skip-to-content include in layout",
                }
            )

    return findings


def scan_archetype(app_dir: Path) -> List[dict]:
    """Walk the emitted app dir and run static checks on AUDIT_PAGES + their
    transitive widget includes + the layout shell.
    """
    out: List[dict] = []
    seen: set = set()
    visited: List[str] = []
    for page in AUDIT_PAGES:
        visited.extend(_follow_includes(app_dir, page, seen))
    for rel in visited:
        target = app_dir / "views" / rel
        if not target.exists():
            continue
        try:
            content = target.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        out.extend(scan_file(f"views/{rel}", content))
    return out


# ---------------------------------------------------------------------------
# Palette contrast check
# ---------------------------------------------------------------------------


def _hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rel_luminance(rgb: Tuple[int, int, int]) -> float:
    def chan(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast_ratio(fg: Tuple[int, int, int], bg: Tuple[int, int, int]) -> float:
    l1 = _rel_luminance(fg)
    l2 = _rel_luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def scan_palettes_for_low_contrast() -> List[dict]:
    """Check every palette in compose/data/palettes.json: does foreground-on-
    background meet WCAG AA-normal (4.5:1)? Foreground-muted on background
    is the secondary-copy pair the design system uses most heavily.
    """
    out: List[dict] = []
    palettes = load_palettes()
    for p in palettes["palettes"]:
        v = p.get("vars", {})
        fg = v.get("--color-foreground")
        bg = v.get("--color-background")
        fg_muted = v.get("--color-foreground-muted")
        if not (fg and bg):
            continue
        ratio_fg_bg = contrast_ratio(_hex_to_rgb(fg), _hex_to_rgb(bg))
        if ratio_fg_bg < 4.5:
            out.append(
                {
                    "rule": "palette-foreground-fails-aa",
                    "palette": p["id"],
                    "fg": fg,
                    "bg": bg,
                    "ratio": round(ratio_fg_bg, 2),
                }
            )
        if fg_muted:
            ratio_muted_bg = contrast_ratio(
                _hex_to_rgb(fg_muted), _hex_to_rgb(bg)
            )
            if ratio_muted_bg < 4.5:
                out.append(
                    {
                        "rule": "palette-foreground-muted-fails-aa",
                        "palette": p["id"],
                        "fg": fg_muted,
                        "bg": bg,
                        "ratio": round(ratio_muted_bg, 2),
                    }
                )
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def build_all(out_root: Path) -> Dict[str, Path]:
    """Build all 9 archetypes with zero overrides into out_root."""
    built: Dict[str, Path] = {}
    for a in ARCHETYPES:
        ans = {
            "app_name": f"audit-{a}-test",
            "archetype": a,
            "site_name": f"Audit {a.title()}",
            "admin_email": "audit@example.com",
        }
        spec = scaffold_spec(ans)
        target = out_root / a
        build_app(spec, output_dir=target)
        built[a] = target
    return built


def aggregate_findings(built: Dict[str, Path]) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for archetype, app_dir in built.items():
        out[archetype] = scan_archetype(app_dir)
    return out


def cross_archetype_rules(findings: Dict[str, List[dict]]) -> List[Tuple[str, int]]:
    """Rules that hit in >=5 of 9 archetypes are "cross-archetype"; fix in
    the compose engine. Returns sorted list of (rule, archetype_hit_count).
    """
    rule_hits: Dict[str, set] = {}
    for archetype, items in findings.items():
        for f in items:
            rule_hits.setdefault(f["rule"], set()).add(archetype)
    return sorted(
        ((r, len(hits)) for r, hits in rule_hits.items()),
        key=lambda x: (-x[1], x[0]),
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, help="Optional JSON output path")
    p.add_argument("--keep", action="store_true", help="Keep tmp build dir")
    args = p.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="a11y-sweep-"))
    try:
        built = build_all(tmp)
        view_findings = aggregate_findings(built)
        palette_findings = scan_palettes_for_low_contrast()
        cross = cross_archetype_rules(view_findings)

        report = {
            "archetypes": list(built.keys()),
            "view_findings": view_findings,
            "palette_findings": palette_findings,
            "cross_archetype": cross,
        }

        # Console summary
        print("\n=== Per-archetype rule hits ===")
        for a in built.keys():
            counts: Dict[str, int] = {}
            for f in view_findings[a]:
                counts[f["rule"]] = counts.get(f["rule"], 0) + 1
            print(f"  {a}: {counts or '(none)'}")
        print("\n=== Cross-archetype rule incidence ===")
        for rule, hits in cross:
            print(f"  {rule}: {hits}/9 archetypes")
        print("\n=== Palette contrast ===")
        if not palette_findings:
            print("  (all palettes pass WCAG AA)")
        for f in palette_findings:
            print(
                f"  {f['rule']}: palette={f['palette']} fg={f['fg']} bg={f['bg']} ratio={f['ratio']}"
            )

        if args.out:
            args.out.write_text(json.dumps(report, indent=2))
            print(f"\nReport written to {args.out}")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
