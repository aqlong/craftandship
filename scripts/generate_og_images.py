"""Generate per-page OG images at 1200x630 for the Craft & Ship marketing site.

Each PNG is a warm-paper composite with the page title, a one-line subtitle,
the brand wordmark, and a small lighthouse mark. The list of pages mirrors
`web/src/services/og-images.ts`; keep the two in sync when adding pages.

Usage:
    python3 scripts/generate_og_images.py

Re-run after editing OG_PAGES below or after a brand-voice copy pass.
Outputs land in web/public/og-*.png; commit the regenerated PNGs alongside
any change to the route map.

Dependencies: Pillow only (already in the dev deps via PIL imports across
scripts/). No cairo / rsvg / canvas / @vercel/og; keeps the toolchain
identical to scripts/generate_favicon_ico.py.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC = os.path.join(ROOT, "web", "public")

# Canonical OG card size for Twitter summary_large_image + Facebook + LinkedIn
WIDTH = 1200
HEIGHT = 630

# Warm-paper Harbor palette. Mirrors brand-harbor/tokens.css.
COLOR_PAPER = (241, 228, 201)       # #F1E4C9; primary background
COLOR_PAPER_DEEP = (234, 212, 166)  # #EAD4A6; accent panel
COLOR_INK = (43, 36, 28)            # #2B241C; primary text
COLOR_INK_SOFT = (74, 58, 44)       # #4A3A2C; secondary text
COLOR_GOLD = (201, 160, 79)         # #C9A04F; Harbor gold accent
COLOR_LIGHTHOUSE_BODY = (234, 212, 166)
COLOR_LIGHTHOUSE_DARK = (58, 42, 31)


# Font fallback chain: serif display + sans body. We prefer Cormorant /
# Source Sans (the live brand fonts) when present, fall back to Garamond /
# Georgia / DejaVu so the script runs on Mac, Linux CI, and stripped images.
SERIF_CANDIDATES = [
    "Cormorant Garamond",
    "EB Garamond",
    "Garamond",
    "Baskerville",
    "Georgia",
    "Times New Roman",
    "DejaVu Serif",
]
SANS_CANDIDATES = [
    "Source Sans 3",
    "Source Sans Pro",
    "Helvetica Neue",
    "Helvetica",
    "Arial",
    "DejaVu Sans",
]


def _try_font(family: str, size: int, italic: bool = False) -> Optional[ImageFont.FreeTypeFont]:
    """Try to load a font by family name. Returns None when unavailable."""
    paths_to_try = []
    suffix = " Italic" if italic else ""
    paths_to_try.append(f"/Library/Fonts/{family}{suffix}.ttf")
    paths_to_try.append(f"/Library/Fonts/{family}{suffix}.ttc")
    paths_to_try.append(f"/System/Library/Fonts/{family}{suffix}.ttf")
    paths_to_try.append(f"/System/Library/Fonts/{family}{suffix}.ttc")
    paths_to_try.append(f"/System/Library/Fonts/Supplemental/{family}{suffix}.ttf")
    paths_to_try.append(f"/System/Library/Fonts/Supplemental/{family}{suffix}.ttc")
    paths_to_try.append(f"/usr/share/fonts/truetype/{family.lower().replace(' ', '-')}/{family}-Regular.ttf")
    paths_to_try.append(os.path.expanduser(f"~/Library/Fonts/{family}{suffix}.ttf"))
    paths_to_try.append(os.path.expanduser(f"~/Library/Fonts/{family}{suffix}.otf"))

    for p in paths_to_try:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return None


def _load_font(candidates: List[str], size: int, italic: bool = False) -> ImageFont.FreeTypeFont:
    """Load the first available font from a candidate list. Falls back to
    PIL's default bitmap font as a last resort (legible at large sizes with
    a noticeable downgrade: fine for build-time generation)."""
    for fam in candidates:
        font = _try_font(fam, size, italic=italic)
        if font is not None:
            return font
    # Final fallback. PIL default font ignores size, but the surrounding
    # script will still produce a readable card.
    return ImageFont.load_default()


def _wrap_to_width(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
) -> List[str]:
    """Greedy word-wrap to keep each rendered line within max_width pixels."""
    words = text.split()
    if not words:
        return [""]
    lines: List[str] = []
    current = words[0]
    for w in words[1:]:
        candidate = f"{current} {w}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = w
    lines.append(current)
    return lines


def _draw_lighthouse_mark(
    img: Image.Image,
    cx: int,
    cy: int,
    scale: float = 1.0,
) -> None:
    """Draw a small lighthouse mark at (cx, cy). Coordinates from the SVG
    viewBox 64x64 of web/public/brand-harbor/favicon.svg, scaled."""
    d = ImageDraw.Draw(img)

    def s(x: float) -> int:
        return int(round(x * scale))

    # Anchor: shift so cx, cy is the center of the 64x64 viewBox
    ox = cx - s(32)
    oy = cy - s(32)

    # Base
    d.rectangle((ox + s(19), oy + s(50), ox + s(19 + 22), oy + s(50 + 6)),
                fill=COLOR_LIGHTHOUSE_DARK)
    # Trapezoid tower
    d.polygon(
        [
            (ox + s(19), oy + s(50)),
            (ox + s(23), oy + s(24)),
            (ox + s(37), oy + s(24)),
            (ox + s(41), oy + s(50)),
        ],
        fill=COLOR_LIGHTHOUSE_BODY,
    )
    # Tower bands
    d.rectangle((ox + s(20.5), oy + s(38), ox + s(39.5), oy + s(40)),
                fill=COLOR_LIGHTHOUSE_DARK)
    d.rectangle((ox + s(21.5), oy + s(31), ox + s(38.5), oy + s(33)),
                fill=COLOR_LIGHTHOUSE_DARK)
    # Top deck
    d.rectangle((ox + s(23), oy + s(20), ox + s(23 + 14), oy + s(20 + 4)),
                fill=COLOR_LIGHTHOUSE_DARK)
    # Lantern housing
    d.rounded_rectangle(
        (ox + s(24), oy + s(6.5), ox + s(36), oy + s(20)),
        radius=s(6),
        fill=COLOR_LIGHTHOUSE_DARK,
    )
    # Lantern glass (gold glow)
    d.rounded_rectangle(
        (ox + s(25), oy + s(12.5), ox + s(35), oy + s(19)),
        radius=s(2),
        fill=COLOR_GOLD,
    )
    # Finial
    d.ellipse((ox + s(28.5), oy + s(3), ox + s(31.5), oy + s(6)),
              fill=COLOR_LIGHTHOUSE_DARK)


def _compose_card(filename: str, title: str, subtitle: str) -> None:
    """Compose a single 1200x630 OG card and write it to web/public/."""
    img = Image.new("RGBA", (WIDTH, HEIGHT), COLOR_PAPER + (255,))
    d = ImageDraw.Draw(img)

    # Subtle vignette / paper edge: a 12px deeper-paper border, then the
    # canvas. Keeps the card from feeling flat in social previews.
    d.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=COLOR_PAPER_DEEP, width=4)

    # Gold rule above the wordmark, anchoring the composition.
    rule_y = 540
    d.rectangle((80, rule_y, 200, rule_y + 3), fill=COLOR_GOLD)

    # Brand wordmark (lighthouse + "Craft & Ship") at the bottom-left.
    _draw_lighthouse_mark(img, cx=110, cy=rule_y + 50, scale=1.1)

    wordmark_serif = _load_font(SERIF_CANDIDATES, 36)
    wordmark_italic = _load_font(SERIF_CANDIDATES, 36, italic=True)
    d.text((160, rule_y + 24), "Craft & ", font=wordmark_serif, fill=COLOR_INK)
    # Compute "Ship" italic placement from the width of "Craft & "
    width_craft = d.textbbox((0, 0), "Craft & ", font=wordmark_serif)[2]
    d.text((160 + width_craft, rule_y + 24), "Ship", font=wordmark_italic, fill=COLOR_GOLD)

    # Page title: the dominant element. Cormorant Garamond display feel.
    title_size = 96 if len(title) <= 24 else 80 if len(title) <= 36 else 68
    title_font = _load_font(SERIF_CANDIDATES, title_size)
    title_lines = _wrap_to_width(title, title_font, max_width=WIDTH - 160, draw=d)

    # Vertically center title block in the upper region.
    title_line_height = int(title_size * 1.1)
    title_block_h = title_line_height * len(title_lines)
    title_top = max(110, (rule_y - 40 - title_block_h) // 2 + 40)
    for i, line in enumerate(title_lines):
        d.text((80, title_top + i * title_line_height), line,
               font=title_font, fill=COLOR_INK)

    # Subtitle: smaller serif under the title.
    if subtitle:
        sub_font = _load_font(SERIF_CANDIDATES, 32, italic=True)
        sub_lines = _wrap_to_width(subtitle, sub_font, max_width=WIDTH - 160, draw=d)
        sub_top = title_top + title_block_h + 24
        sub_line_height = 42
        if len(sub_lines) > 2:
            # The card holds two lines of subtitle. Dropping the rest would
            # cut a sentence off mid-claim, so refuse instead.
            raise ValueError(
                f"{filename}: subtitle wraps to {len(sub_lines)} lines, the card "
                f"holds 2: {subtitle!r}"
            )
        for i, line in enumerate(sub_lines):
            d.text((80, sub_top + i * sub_line_height), line,
                   font=sub_font, fill=COLOR_INK_SOFT)

    out_path = os.path.join(PUBLIC, filename)
    img.convert("RGB").save(out_path, format="PNG", optimize=True)
    print(f"wrote {out_path}")


# (filename, title, subtitle). Title + subtitle copy follow Harbor brand
# voice: affirmative, no em dashes, no "we are not" framing, no calendar
# timetables. They say what the page's own copy says (views/layout-harbor.ejs
# and the page descriptions in web/src/app.ts): websites are built from a
# library of tested building blocks and every website needs a recorded review
# approval before it goes live. They do not say who builds or reviews a site.
OG_PAGES: List[Tuple[str, str, str]] = [
    (
        "og-default.png",
        "A website made with you",
        "For healers, therapists, and event producers. Yours, kept across the years.",
    ),
    (
        "og-pricing.png",
        "One subscription. Whatever fits your practice.",
        "Choose any amount from $39 down to $19 a month. Hosting included.",
    ),
    (
        "og-companion.png",
        "Meet Haven, your companion",
        "A thoughtful companion who listens, remembers what matters, and is honest about being AI.",
    ),
    (
        "og-how-it-works.png",
        "How a Craft & Ship website is made",
        "You begin with Haven. Your website is built from tested building blocks. Every website needs a recorded review approval before it goes live.",
    ),
    (
        "og-gallery.png",
        "Recent websites from the studio",
        "Real practitioner websites, built from a library of tested building blocks and each needs a recorded review approval before it goes live.",
    ),
    (
        "og-about.png",
        "A studio in Austin",
        "For healers, therapists, and event producers.",
    ),
    (
        "og-about-ocean.png",
        "Ocean Longnion, Senior Web Craftsman",
        "Founder of Craft & Ship, which makes custom websites for healers, therapists, and event producers.",
    ),
    (
        "og-contact.png",
        "Reach the studio",
        "Questions about your website, pricing, or the platform.",
    ),
    (
        "og-terms.png",
        "Terms of Service",
        "The plain-English terms behind every Craft & Ship subscription.",
    ),
    (
        "og-privacy.png",
        "Privacy Policy",
        "How Craft & Ship handles your information. Plain English, no surprises.",
    ),
]


def main() -> None:
    os.makedirs(PUBLIC, exist_ok=True)
    for filename, title, subtitle in OG_PAGES:
        _compose_card(filename, title, subtitle)


if __name__ == "__main__":
    main()
