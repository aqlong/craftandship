#!/usr/bin/env python3
"""Static tab-order scanner for /lighthouse/* keeper-flow views.

Why static: a live Playwright walk requires Postgres, the web server, a
trial-keeper cookie, a build id, and a headless browser. This script is
the deps-free first pass that enumerates the natural tab order of each
EJS view by walking the file in document order and surfacing tabbable
elements plus a small set of known anti-patterns.

The Playwright spec at `web/tests/ui/keyboard-tab-order.spec.ts` is the
live second pass that catches DOM-mutation surprises the static pass
cannot see (dynamically-injected forms, conditional CSS focus styles,
JS-forced focus moves, native control intricacies).

Run:  python3 scripts/keyboard_tab_order_static_scan.py

Emits a markdown report to stdout. The audit doc at
`docs/audits/keyboard-tab-order-2026-05-27.md` bundles that output plus
hand-curated grouped findings.

Anti-patterns flagged:

  AP1: duplicate accessible name across sibling controls
        (e.g. five "Choose this" buttons on /lighthouse/preview)
  AP2: JS autofocus competing with the skip-to-content link
        (compose textarea steals focus on first paint, defeating
         the skip-link's "first focusable element" contract)
  AP3: disclosure widget that opens without moving focus into the
        revealed region AND lacks a keyboard close (Escape) handler
        (memory side-panel drawer)
  AP4: `<meta http-equiv="refresh">` resets focus on a state page
        that should use an aria-live region instead

These are the failures the parent roadmap item asked the live walk to
catch. The static script surfaces them deterministically so the regression
test in `tests/test_keyboard_tab_order_static_scan.py` can pin them.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VIEWS_DIR = _REPO_ROOT / "web" / "views" / "lighthouse"
_SKIP_LINK_PARTIAL = _REPO_ROOT / "web" / "views" / "partials" / "_harbor-skip-link.ejs"

# Map view file -> the public route(s) it serves.
_VIEW_ROUTES = {
    "table.ejs": ("/lighthouse/table",),
    "recall.ejs": ("/lighthouse/table/recall",),
    "preview.ejs": ("/lighthouse/preview",),
    "lamp.ejs": ("/lighthouse/lamp/<id>",),
    "make-public.ejs": ("/lighthouse/lamp/<id>/make-public",),
    "published.ejs": ("/lighthouse/lamp/<id>/published",),
    "cancelled.ejs": ("/lighthouse/lamp/<id>/cancelled",),
    "changes-first.ejs": ("/lighthouse/lamp/<id>/make-public (review_status=changes_requested)",),
}

# Tags that are natively focusable in tab order when present without
# tabindex=-1 / disabled / hidden / type=hidden.
_FOCUSABLE_TAGS = {"a", "button", "input", "select", "textarea"}


@dataclass
class Focusable:
    tag: str
    snippet: str  # short identifier used in reports (id / text / placeholder)
    line: int
    # State branch this control lives in; None means it shows on every render.
    # Free-form short label; matched verbatim by the regression test.
    branch: str | None = None
    # True when the element only renders after a JS DOM mutation (no SSR).
    dynamic: bool = False

    def label(self) -> str:
        b = f" [{self.branch}]" if self.branch else ""
        d = " (js-injected)" if self.dynamic else ""
        return f"{self.tag}#{self.snippet}{b}{d}"


@dataclass
class Finding:
    code: str  # AP1 / AP2 / AP3 / AP4
    view: str
    description: str
    severity: str  # "blocker" | "issue" | "advisory"


@dataclass
class ViewReport:
    view: str
    routes: tuple[str, ...]
    skip_link_present: bool
    main_landmark_id: str | None
    focusables: list[Focusable] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


# --- EJS stripper ----------------------------------------------------------


def _blank_keep_newlines(s: str) -> str:
    """Replace every non-newline character with a space.

    Used by the EJS / script strippers so the file's line numbering is
    preserved for downstream regex matches.
    """

    return "".join(c if c == "\n" else " " for c in s)


def _strip_scripts(src: str) -> str:
    """Blank `<script>...</script>` bodies so the HTML tag scanner ignores
    JS string literals like `'<input id="email-gate-input">'` that look
    like real HTML to a regex."""

    return re.sub(
        r"<script\b[^>]*>.*?</script\s*>",
        lambda m: _blank_keep_newlines(m.group(0)),
        src,
        flags=re.S | re.I,
    )


def _strip_ejs(src: str) -> str:
    """Remove EJS tags but preserve line numbering."""

    # `<%# comment %>` -> blank (newlines preserved)
    src = re.sub(
        r"<%#.*?%>",
        lambda m: _blank_keep_newlines(m.group(0)),
        src,
        flags=re.S,
    )
    # `<%- expr %>` and `<%= expr %>` -> blank inline (we don't expand includes
    # since the only include used inside the keeper flow is the skip-link
    # partial, handled separately).
    src = re.sub(
        r"<%[-=]?\s*.*?%>",
        lambda m: _blank_keep_newlines(m.group(0)),
        src,
        flags=re.S,
    )
    # `<% js %>` -> blank (newlines preserved)
    src = re.sub(
        r"<%[^%]*%>",
        lambda m: _blank_keep_newlines(m.group(0)),
        src,
        flags=re.S,
    )
    return src


# --- Snippet extraction ----------------------------------------------------


_ATTR_RE = re.compile(r"""(\w[\w-]*)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")


def _parse_attrs(opening_tag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(opening_tag):
        k = m.group(1).lower()
        v = m.group(2)
        if v.startswith(("'", '"')):
            v = v[1:-1]
        out[k] = v
    return out


def _short_snippet(tag: str, attrs: dict[str, str], inner: str) -> str:
    # Accessible-name preference: id > visible-text > name/placeholder/href.
    if attrs.get("id"):
        return attrs["id"]
    if tag == "input":
        t = attrs.get("type", "text")
        n = attrs.get("name", "")
        ph = attrs.get("placeholder", "")
        return f"{t}:{n or ph or '<no-name>'}"
    if tag == "a":
        href = attrs.get("href", "")
        text = inner[:48]
        return text or href[:48] or "<no-text>"
    if tag == "button":
        return attrs.get("name") or inner[:48] or "<no-text>"
    return attrs.get("name") or inner[:40] or "<no-name>"


# --- Focusable enumerator --------------------------------------------------


def _is_focusable(tag: str, attrs: dict[str, str]) -> bool:
    if tag not in _FOCUSABLE_TAGS:
        return False
    if "disabled" in attrs:
        return False
    if "hidden" in attrs:
        return False
    if attrs.get("tabindex") == "-1":
        return False
    if tag == "input" and attrs.get("type", "text").lower() in {"hidden"}:
        return False
    # type=submit / image / reset / button / range / email / text / checkbox
    # etc. are all tabbable.
    return True


def _line_of(src: str, idx: int) -> int:
    return src.count("\n", 0, idx) + 1


# Branch detection: walk `<% if (...) %>` blocks and label each focusable
# with the conditional surrounding it. Static heuristic; the real engine is
# EJS at runtime, but for tab-order reporting this is enough.

# `<% if (cond) { %>` opens a new scope.  The leading `}` (when it appears as
# `<% } else if ...`) is handled by _ELSE_IF_RE, NOT by this regex, so the
# stack depth stays correct.
_IF_OPEN_RE = re.compile(r"<%\s*if\s*\((.*?)\)\s*\{\s*%>", re.S)
_ELSE_IF_RE = re.compile(r"<%\s*\}\s*else\s+if\s*\((.*?)\)\s*\{\s*%>", re.S)
_ELSE_RE = re.compile(r"<%\s*\}\s*else\s*\{\s*%>")
_CLOSE_RE = re.compile(r"<%\s*\}\s*%>")


def _build_branch_map(src: str):
    """Return a closure: offset -> branch label.

    Branch label is the most recent active condition (`if` / `else if` /
    `else of: <cond>`), trimmed to ~60 chars, OR `None` for unconditional
    content. Nested branches get the innermost label.
    """

    events: list[tuple[int, str, str | None]] = []
    for m in _IF_OPEN_RE.finditer(src):
        cond = re.sub(r"\s+", " ", m.group(1)).strip()
        if len(cond) > 60:
            cond = cond[:57] + "..."
        events.append((m.end(), "push", cond))
    for m in _ELSE_IF_RE.finditer(src):
        cond = re.sub(r"\s+", " ", m.group(1)).strip()
        if len(cond) > 60:
            cond = cond[:57] + "..."
        events.append((m.end(), "swap", f"else-if: {cond}"))
    for m in _ELSE_RE.finditer(src):
        events.append((m.end(), "swap-else", None))
    for m in _CLOSE_RE.finditer(src):
        events.append((m.end(), "pop", None))
    events.sort()

    boundary_marks: list[tuple[int, str | None]] = [(0, None)]
    stack: list[str] = []
    for offset, kind, payload in events:
        if kind == "push":
            assert payload is not None
            stack.append(payload)
        elif kind == "swap":
            # `} else if (...)`: replace the top frame without changing depth.
            assert payload is not None
            if stack:
                stack[-1] = payload
            else:
                stack.append(payload)
        elif kind == "swap-else":
            # `} else { %>`: flip the top frame to `else of: <prior>`.
            if stack:
                prior = stack[-1]
                stack[-1] = f"else of: {prior}"
        elif kind == "pop":
            if stack:
                stack.pop()
        boundary_marks.append((offset, stack[-1] if stack else None))

    # Lookup helper: caller passes an offset, we return the branch label
    # active at that offset.
    def lookup(idx: int) -> str | None:
        # binary-search-ish; views are short, linear scan is fine
        active: str | None = None
        for boundary, label in boundary_marks:
            if boundary <= idx:
                active = label
            else:
                break
        return active

    # Pre-materialize as dict for the offsets we'll actually query (focusable
    # tag starts): simpler to expose as a callable, so just return a closure.
    return lookup  # type: ignore[return-value]


_TAG_RE = re.compile(
    r"<(?P<tag>a|button|input|select|textarea)\b(?P<attrs>[^>]*)>",
    re.IGNORECASE,
)


def _clean_inner(inner: str) -> str:
    # Drop any nested HTML tags + collapse whitespace so the snippet for
    # `<a><img/><span>Foo</span></a>` reads "Foo" not the raw markup.
    text = re.sub(r"<[^>]+>", " ", inner)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)  # entities
    return re.sub(r"\s+", " ", text).strip()


def enumerate_focusables(view_path: Path) -> list[Focusable]:
    raw = view_path.read_text()
    # Strip script bodies first so JS-string literals like
    # '<input id="email-gate-input">' don't get parsed as live HTML.
    no_script = _strip_scripts(raw)
    src = _strip_ejs(no_script)
    branch_lookup = _build_branch_map(no_script)

    out: list[Focusable] = []
    for m in _TAG_RE.finditer(src):
        tag = m.group("tag").lower()
        attrs_blob = m.group("attrs")
        attrs = _parse_attrs(attrs_blob)
        if not _is_focusable(tag, attrs):
            continue
        # Inner text best-effort: read until the closing tag in the stripped src.
        close = re.search(rf"</{tag}\s*>", src[m.end() :], re.IGNORECASE)
        inner_raw = src[m.end() : m.end() + close.start()] if close else ""
        snippet = _short_snippet(tag, attrs, _clean_inner(inner_raw))
        line = _line_of(src, m.start())
        branch = branch_lookup(m.start())
        out.append(Focusable(tag=tag, snippet=snippet, line=line, branch=branch))
    return out


# --- Skip-link presence ---------------------------------------------------


def has_skip_link(view_path: Path) -> bool:
    raw = view_path.read_text()
    return "_harbor-skip-link" in raw


def main_landmark_id(view_path: Path) -> str | None:
    raw = view_path.read_text()
    m = re.search(r"<main\b[^>]*\bid\s*=\s*['\"]([^'\"]+)['\"]", raw)
    return m.group(1) if m else None


# --- JS-injected controls (table.ejs email gate) --------------------------


def detect_dynamic_focusables(view_path: Path) -> list[Focusable]:
    """The keeper-table view injects an email-gate form via JS. Surface its
    focusables so the live walk has a known answer to check against."""

    if view_path.name != "table.ejs":
        return []
    raw = view_path.read_text()
    out: list[Focusable] = []
    if "email-gate-input" in raw:
        out.append(
            Focusable(
                tag="input",
                snippet="email:email-gate-input",
                line=_line_of(raw, raw.index("email-gate-input")),
                branch="JS: 402 requiresEmail (turn count >= gate)",
                dynamic=True,
            )
        )
        out.append(
            Focusable(
                tag="button",
                snippet="submit:Send + continue",
                line=_line_of(raw, raw.index("email-gate-input")),
                branch="JS: 402 requiresEmail (turn count >= gate)",
                dynamic=True,
            )
        )
    return out


# --- Anti-pattern detectors -----------------------------------------------


def detect_findings(view_path: Path, focusables: list[Focusable]) -> list[Finding]:
    raw = view_path.read_text()
    name = view_path.name
    out: list[Finding] = []

    # AP1: duplicate accessible name across sibling buttons (preview.ejs)
    # The preview surface emits the same "Choose this" label once per family
    # via the EJS `families.forEach` loop. Static scan sees it once but the
    # template generates N copies.
    if name == "preview.ejs":
        if re.search(r"<button\b[^>]*>\s*Choose this\s*</button>", _strip_ejs(raw)):
            # Confirm there's no per-family aria-label
            if "aria-label=" not in re.findall(r"<button[^>]*>", _strip_ejs(raw))[0]:
                out.append(
                    Finding(
                        code="AP1",
                        view=name,
                        description=(
                            "Every template-family card emits a `<button>Choose this</button>` "
                            "with no per-family aria-label. SR users tabbing through 5 cards hear "
                            "'Choose this, Choose this, ...' with no way to distinguish which "
                            "family they are selecting. Fix: add "
                            "`aria-label=\"Choose <%= f.name %>\"` to each button (preferred) or "
                            "swap to visually-hidden text per button. Reference: WCAG 2.4.6 "
                            "Headings and Labels."
                        ),
                        severity="issue",
                    )
                )

    # AP2: JS autofocus on table.ejs steals focus from the skip-link.
    # The cold-load anti-pattern is `textarea.focus()` called unconditionally
    # at the bottom of the IIFE, right after `scrollToBottom()`, with no
    # `document.referrer` guard between them. The post-turn `textarea.focus()`
    # inside `sendTurn()`'s finally block is fine (it runs after the keeper
    # submits a turn, not on cold load) and is intentionally not flagged.
    # After the fix, the IIFE-end focus call lives inside an
    # `if (document.referrer ...)` guard so cold-load visits leave the
    # skip-to-content link as the first focus stop.
    if name == "table.ejs":
        cold_load_autofocus = re.search(
            r"scrollToBottom\(\s*\)\s*;\s*textarea\.focus\(\s*\)\s*;",
            raw,
        )
        if cold_load_autofocus:
            out.append(
                Finding(
                    code="AP2",
                    view=name,
                    description=(
                        "Inline script calls `textarea.focus()` on page load, which moves the "
                        "browser's focus ring straight into the compose textarea before the "
                        "skip-to-main-content link can ever be the first focusable element. "
                        "Keyboard / screen-reader users landing on the page have to Shift-Tab "
                        "back through the document order to reach the skip-link, defeating its "
                        "purpose. Fix: only auto-focus the textarea after the keeper interacts "
                        "(e.g. on first keydown / scroll), OR drop autofocus on first paint "
                        "and rely on the skip-link as the first focus stop. WCAG 2.4.3 Focus "
                        "Order + 3.2.1 On Focus."
                    ),
                    severity="issue",
                )
            )

    # AP3: memory drawer opens without moving focus in + no Escape handler.
    if name == "table.ejs":
        has_drawer = re.search(r"id=['\"]memory-tab['\"]", raw) is not None
        has_escape = re.search(r"key\s*===?\s*['\"]Escape['\"]", raw) is not None
        moves_focus = re.search(
            r"memory-panel.*\.focus\(",
            raw,
            re.S,
        ) is not None
        if has_drawer and (not has_escape or not moves_focus):
            out.append(
                Finding(
                    code="AP3",
                    view=name,
                    description=(
                        "The memory side-panel drawer is opened by a `<button id=\"memory-tab\">` "
                        "with `aria-controls=\"memory-panel\"` and an `aria-expanded` toggle, "
                        "but: (1) on open, focus stays on the toggle button instead of moving "
                        "into the drawer's first focusable element, and (2) there is no Escape "
                        "keydown handler to close the drawer with the keyboard (only mouse-out "
                        "via the document click listener). Keyboard-only keepers can open the "
                        "drawer but cannot navigate into it or close it without Tab-cycling out. "
                        "Fix: on open, call `memoryPanel.querySelector('a, button').focus()` (or "
                        "focus the first `memory-item` if any) and add a `document.addEventListener"
                        "('keydown', e => e.key === 'Escape' && close())` handler. WCAG 2.1.2 No "
                        "Keyboard Trap + 2.4.3 Focus Order."
                    ),
                    severity="issue",
                )
            )

    # AP4: meta refresh on /published while the webhook hasn't settled.
    if name == "published.ejs":
        if 'http-equiv="refresh"' in raw or "http-equiv='refresh'" in raw:
            out.append(
                Finding(
                    code="AP4",
                    view=name,
                    description=(
                        "The `<meta http-equiv=\"refresh\" content=\"3\">` element reloads the "
                        "page every 3 seconds while waiting for the Stripe webhook to flip the "
                        "build to live. Every reload resets focus to document start, which is "
                        "disorienting for keyboard / SR users on the waiting screen and conflicts "
                        "with WCAG 2.2.1 (Timing Adjustable) and 2.2.4 (Interruptions). Fix: "
                        "swap the polling to a `<div aria-live=\"polite\" role=\"status\">` "
                        "block that fetches state without reloading, or extend the refresh to "
                        ">=20s and respect `prefers-reduced-motion`. The 'Skip the wait' link "
                        "below the spinner gives the user an out, but the focus reset is still "
                        "an issue every 3s."
                    ),
                    severity="advisory",
                )
            )

    return out


# --- Top-level scan -------------------------------------------------------


def scan_view(view_path: Path) -> ViewReport:
    name = view_path.name
    focusables = enumerate_focusables(view_path)
    focusables.extend(detect_dynamic_focusables(view_path))
    findings = detect_findings(view_path, focusables)
    return ViewReport(
        view=name,
        routes=_VIEW_ROUTES.get(name, ()),
        skip_link_present=has_skip_link(view_path),
        main_landmark_id=main_landmark_id(view_path),
        focusables=focusables,
        findings=findings,
    )


def scan_all() -> list[ViewReport]:
    views = sorted(p for p in _VIEWS_DIR.glob("*.ejs"))
    return [scan_view(v) for v in views]


# --- Markdown renderer ----------------------------------------------------


def render_markdown(reports: list[ViewReport]) -> str:
    lines: list[str] = []
    total_focusables = sum(len(r.focusables) for r in reports)
    total_findings = sum(len(r.findings) for r in reports)
    lines.append(f"**Views scanned:** {len(reports)}")
    lines.append(f"**Focusable controls enumerated:** {total_focusables}")
    lines.append(f"**Anti-patterns surfaced:** {total_findings}")
    lines.append("")
    lines.append("| View | Routes | Skip-link | `<main id>` | Focusables | Findings |")
    lines.append("|---|---|---|---|---|---|")
    for r in reports:
        routes = "<br>".join(r.routes) if r.routes else "-"
        lines.append(
            f"| `{r.view}` | {routes} | "
            f"{'YES' if r.skip_link_present else 'NO'} | "
            f"`{r.main_landmark_id or '-'}` | "
            f"{len(r.focusables)} | "
            f"{len(r.findings)} |"
        )
    lines.append("")
    for r in reports:
        lines.append(f"### `{r.view}`: tab order")
        lines.append("")
        if not r.focusables:
            lines.append("_No tabbable controls._")
            lines.append("")
            continue
        lines.append("| # | Tag | Identifier | Branch | Line |")
        lines.append("|---|---|---|---|---|")
        for idx, f in enumerate(r.focusables, start=1):
            branch = f.branch or "always-rendered"
            if f.dynamic:
                branch += " (DOM-injected)"
            lines.append(f"| {idx} | `<{f.tag}>` | `{f.snippet}` | {branch} | {f.line} |")
        if r.findings:
            lines.append("")
            lines.append(f"**Findings for `{r.view}`:**")
            for f in r.findings:
                lines.append("")
                lines.append(f"- **{f.code} ({f.severity})**: {f.description}")
        lines.append("")
    return "\n".join(lines)


# --- CLI -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    reports = scan_all()
    print(render_markdown(reports))
    return 0 if all(f.severity != "blocker" for r in reports for f in r.findings) else 1


if __name__ == "__main__":
    raise SystemExit(main())
