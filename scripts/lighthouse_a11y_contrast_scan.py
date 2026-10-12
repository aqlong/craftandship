#!/usr/bin/env python3
"""Static WCAG contrast scanner for /lighthouse/* views.

Why static: a live Playwright + axe-core run requires Postgres, the web server,
and a headless browser. This script is the deps-free, infra-free first pass
that catches the most common contrast failure mode (token-pair mistakes)
without any of that. The Playwright spec at
`web/tests/ui/lighthouse-a11y.spec.ts` is the second pass that catches
DOM-dependent failures (overlapping text, dynamic state, hover, focus).

Run:  python3 scripts/lighthouse_a11y_contrast_scan.py

Emits a markdown table to stdout. The audit doc bundles that table plus a
hand-curated pairing list derived from grep over the lighthouse views.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Iterable


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _composite(fg_rgba: tuple[int, int, int, float], bg_rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Composite a translucent foreground over an opaque background."""
    fr, fg, fb, a = fg_rgba
    br, bg_, bb = bg_rgb
    return (
        round(fr * a + br * (1 - a)),
        round(fg * a + bg_ * (1 - a)),
        round(fb * a + bb * (1 - a)),
    )


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def chan(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1 = _relative_luminance(fg)
    l2 = _relative_luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


@dataclass
class Pairing:
    fg_name: str
    fg_value: str  # display string
    bg_name: str
    bg_value: str
    fg_rgb: tuple[int, int, int]  # composited if translucent
    bg_rgb: tuple[int, int, int]
    use_case: str
    wcag_target: str  # "AA-normal", "AA-large", "AAA-normal"
    routes: tuple[str, ...]

    def ratio(self) -> float:
        return contrast_ratio(self.fg_rgb, self.bg_rgb)

    def passes(self) -> bool:
        r = self.ratio()
        if self.wcag_target == "AA-large":
            return r >= 3.0
        if self.wcag_target == "AAA-normal":
            return r >= 7.0
        return r >= 4.5  # AA-normal default


# Harbor palette (from web/views/lighthouse/table.ejs :root)
OAT = _hex_to_rgb("#e8e0cd")
OAT_DEEP = _hex_to_rgb("#ddd1b6")
CLAY = _hex_to_rgb("#b89683")
CLAY_DEEP = _hex_to_rgb("#a17e6a")
SAGE = _hex_to_rgb("#7d8b6e")
DUSK_ROSE = _hex_to_rgb("#c89595")
INK_BROWN = _hex_to_rgb("#3a2f24")
LANTERN_AMBER = _hex_to_rgb("#f4d29a")
LANTERN_DEEP = _hex_to_rgb("#ecbf76")
SLATE_BLUE = _hex_to_rgb("#5e7187")

# Lamp/make-public palette extensions (lamp.ejs, make-public.ejs, etc.)
CREAM_F6 = _hex_to_rgb("#f6f1e8")
CREAM_FFFAF2 = _hex_to_rgb("#fffaf2")
CREAM_FBF6E8 = _hex_to_rgb("#fbf6e8")
CREAM_FDF2E8 = _hex_to_rgb("#fdf2e8")
WHITE = _hex_to_rgb("#fff")
INK_5A = _hex_to_rgb("#5a534a")
INK_5E = _hex_to_rgb("#5e4f3a")
INK_6E = _hex_to_rgb("#6e5e48")
INK_1A = _hex_to_rgb("#1a1714")
COPPER_A06 = _hex_to_rgb("#9a5a42")


def _ink_alpha(alpha: float, bg: tuple[int, int, int]) -> tuple[int, int, int]:
    return _composite((58, 47, 36, alpha), bg)


def _build_pairings() -> list[Pairing]:
    p: list[Pairing] = []

    # ── Table / Recall / Preview surface (oat background) ──
    routes_table = ("/lighthouse/table", "/lighthouse/recall", "/lighthouse/preview")

    p.append(Pairing(
        "ink-brown", "#3a2f24", "oat", "#e8e0cd",
        INK_BROWN, OAT,
        "Primary body text (Haven turns, keeper turns, headings)",
        "AA-normal", routes_table,
    ))
    # Secondary + faint ink tokens were raised to alpha 0.85 (commit
    # auto/harbor-a11y-ink-faint-on-oat) so all text usages composite to
    # 6.59:1 on oat, clearing WCAG AA. Alphas below 0.85 are reserved for
    # non-text decoration (borders, box-shadows, hairlines) and are NOT
    # enumerated here because the scanner is the text-contrast surface.
    p.append(Pairing(
        "ink-soft", "rgba(58,47,36,0.85)", "oat", "#e8e0cd",
        _ink_alpha(0.85, OAT), OAT,
        "Secondary body text (subtitles, helper copy)",
        "AA-normal", routes_table,
    ))
    p.append(Pairing(
        "ink-faint", "rgba(58,47,36,0.85)", "oat", "#e8e0cd",
        _ink_alpha(0.85, OAT), OAT,
        "Faint meta text (timestamps, byline-style labels)",
        "AA-normal", routes_table,
    ))
    p.append(Pairing(
        "ink-0.85", "rgba(58,47,36,0.85)", "oat", "#e8e0cd",
        _ink_alpha(0.85, OAT), OAT,
        "High-contrast body text",
        "AA-normal", routes_table,
    ))

    # Clay / sage / slate-blue used for borders + small accent labels
    p.append(Pairing(
        "clay", "#b89683", "oat", "#e8e0cd",
        CLAY, OAT,
        "Decorative border / muted label",
        "AA-large", routes_table,
    ))
    p.append(Pairing(
        "clay-deep", "#a17e6a", "oat", "#e8e0cd",
        CLAY_DEEP, OAT,
        "Decorative border (hover) / muted label",
        "AA-large", routes_table,
    ))
    p.append(Pairing(
        "sage", "#7d8b6e", "oat", "#e8e0cd",
        SAGE, OAT,
        "Accent label / icon-text",
        "AA-large", routes_table,
    ))
    p.append(Pairing(
        "slate-blue", "#5e7187", "oat", "#e8e0cd",
        SLATE_BLUE, OAT,
        "Accent label / icon-text",
        "AA-large", routes_table,
    ))
    p.append(Pairing(
        "dusk-rose", "#c89595", "oat", "#e8e0cd",
        DUSK_ROSE, OAT,
        "Accent label / icon-text",
        "AA-large", routes_table,
    ))

    # Lantern amber used for CTAs (white-on-amber per inline-button styling)
    p.append(Pairing(
        "ink-brown", "#3a2f24", "lantern-amber", "#f4d29a",
        INK_BROWN, LANTERN_AMBER,
        "Primary CTA label (Send / Make it public)",
        "AA-normal", routes_table,
    ))

    # ── Lamp / make-public / published / cancelled / changes-first ──
    routes_lamp = (
        "/lighthouse/lamp/:id",
        "/lighthouse/lamp/:id/make-public",
        "/lighthouse/lamp/:id/published",
        "/lighthouse/lamp/:id/cancelled",
        "/lighthouse/lamp/:id/changes-first",
    )

    p.append(Pairing(
        "ink-1a", "#1a1714", "cream-f6", "#f6f1e8",
        INK_1A, CREAM_F6,
        "Lamp body heading + primary text",
        "AA-normal", routes_lamp,
    ))
    p.append(Pairing(
        "ink-5a", "#5a534a", "cream-f6", "#f6f1e8",
        INK_5A, CREAM_F6,
        "Lamp secondary copy",
        "AA-normal", routes_lamp,
    ))
    p.append(Pairing(
        "ink-5e", "#5e4f3a", "cream-fffaf2", "#fffaf2",
        INK_5E, CREAM_FFFAF2,
        "Make-public form helper copy",
        "AA-normal", routes_lamp,
    ))
    p.append(Pairing(
        "ink-6e", "#6e5e48", "cream-fffaf2", "#fffaf2",
        INK_6E, CREAM_FFFAF2,
        "Make-public field label",
        "AA-normal", routes_lamp,
    ))
    p.append(Pairing(
        "copper-9a5a42", "#9a5a42", "cream-fdf2e8", "#fdf2e8",
        COPPER_A06, CREAM_FDF2E8,
        "Past-due eyebrow on cream card",
        "AA-normal", routes_lamp,
    ))
    p.append(Pairing(
        "white", "#fff", "lantern-deep", "#ecbf76",
        WHITE, LANTERN_DEEP,
        "CTA button text on amber",
        "AA-normal", routes_lamp,
    ))
    p.append(Pairing(
        "ink-brown", "#3a2f24", "lantern-deep", "#ecbf76",
        INK_BROWN, LANTERN_DEEP,
        "CTA button alt text on amber",
        "AA-normal", routes_lamp,
    ))

    return p


def render_markdown(pairings: Iterable[Pairing]) -> str:
    pairings = list(pairings)
    failing = [p for p in pairings if not p.passes()]
    passing = [p for p in pairings if p.passes()]

    lines: list[str] = []
    lines.append("| FG | BG | Use case | Ratio | Target | Status | Routes |")
    lines.append("|---|---|---|---|---|---|---|")
    for p in pairings:
        status = "PASS" if p.passes() else "FAIL"
        ratio = f"{p.ratio():.2f}:1"
        routes = ", ".join(p.routes)
        lines.append(
            f"| {p.fg_name} `{p.fg_value}` | {p.bg_name} `{p.bg_value}` | "
            f"{p.use_case} | {ratio} | {p.wcag_target} | {status} | {routes} |"
        )
    lines.append("")
    lines.append(f"**Pairings scanned:** {len(pairings)}  ")
    lines.append(f"**Failing:** {len(failing)}  ")
    lines.append(f"**Passing:** {len(passing)}  ")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    pairings = _build_pairings()
    print(render_markdown(pairings))
    failing = [p for p in pairings if not p.passes()]
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
