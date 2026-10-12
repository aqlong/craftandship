"""Content-integrity guards on compose default seeds + widgets.

Two systemic content-integrity issues were discovered on generated
builds:

  - Every
    tradesperson-contractor + event-producer build seeded
    ``home.trust.items`` with 4 fabricated stat badges
    ("5★ Rating / Happy customers", "24/7 Emergency service",
    "Family-Run / Your neighbors", "Licensed / Fully credentialed").
    These were customer-attributable claims with no relationship to the
    real business.

  - scaffold_spec seeded ``reviews``,
    ``staff``, ``instructors``, and ``schedule_slots`` with fabricated
    rows attributed to invented people ("D. Marlow / Highly recommend",
    "Alex Carter / Lead", "Jordan Blake / Lead Instructor"). These
    rendered on public widgets as if they were real reviews / employees.

The rule: nothing attributed
to an invented person or invented customer experience is acceptable on a
customer-facing surface. Default seeds must either ship empty (and let
the widget hide its chrome) or ship neutrally-named placeholders that
read as templates, not as real claims.

This module tests two contracts:

  1. ``compose/intake.py`` does NOT contain banned fabricated-content
     phrases anywhere in its default seeds. Adding a new fabricated
     name, customer claim, or business-attribute statistic will fail
     this test and force the author to use the empty-seed pattern.

  2. The home-page-facing widgets (``reviews-grid``, ``staff-card-grid``,
     ``instructor-grid``, ``trust-badges-row``, ``service-area-list``,
     ``impact-stats-row``, ``credentials-block``) hide their entire
     section chrome when they receive an empty items list. No empty
     bands on home pages.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INTAKE_PY = REPO_ROOT / "compose" / "intake.py"
WIDGETS_DIR = REPO_ROOT / "compose" / "widgets"


# ---------------------------------------------------------------------------
# (1) Banned phrases in compose/intake.py
# ---------------------------------------------------------------------------

# Fabricated marketing claims and customer-attributable phrases that
# appeared in the original seeds and must NEVER reappear. Each entry is
# a substring; any match in intake.py source fails the test.
#
# Group A: trust-badge stats from the 2026-05-06 fix.
# Group B: review-row text and authors from the 2026-05-07 fix.
# Group C: staff/instructor names from the 2026-05-07 fix.
# Group D: business-attribute claims (24/7, emergency service, etc.)
#          when shipped as default-seeded customer claims.
BANNED_INTAKE_PHRASES = [
    # Group A: fabricated trust-badge stats
    "Family-Run",
    "Your neighbors",
    "Happy customers",
    "5★ Rating",
    "5 Star Rating",
    "Fully credentialed",
    # Group B: fabricated review authors + bodies
    "D. Marlow",
    "Priya S.",
    "Omar J.",
    "Lena K.",
    "Rob C.",
    "Highly recommend.",
    "Best in town.",
    "Saved us from a flood",
    "Third time using them",
    "remember my house, my name",
    # Group C: fabricated team-member names
    "Alex Carter",
    "Morgan Rivers",
    "Jordan Blake",
    "Sam Taylor",
    # Group D: fabricated business-attribute claims
    "24/7 Emergency Service",
    "20 years of C-suite practice",
]


def _intake_source() -> str:
    return INTAKE_PY.read_text(encoding="utf-8")


@pytest.mark.parametrize("banned", BANNED_INTAKE_PHRASES)
def test_intake_seeds_have_no_fabricated_phrases(banned: str) -> None:
    """Each banned phrase must NOT appear as content in compose/intake.py.

    The module-level docstring + comments may legitimately reference some
    of these phrases (to document why they were removed). Only the
    fabricated rendering-into-output occurrences are the problem. To
    distinguish: the test checks for the phrase OUTSIDE of comments and
    docstrings by stripping them first.
    """
    src = _intake_source()
    # Strip line comments (everything after '#' that isn't inside a
    # quoted string). This is approximate but good enough for the
    # purpose of this lint: the seeded data lives inside dict-literal
    # values, not inside comments.
    cleaned_lines = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)

    # Strip triple-quoted strings (docstrings + multi-line documentation
    # blocks that explain *why* the phrase was removed). Use a simple
    # state machine: when we hit a triple-quote, skip until the matching
    # one. This handles both ``"""..."""`` and ``'''...'''``.
    out: list[str] = []
    i = 0
    while i < len(cleaned):
        if cleaned[i:i + 3] in ('"""', "'''"):
            quote = cleaned[i:i + 3]
            j = cleaned.find(quote, i + 3)
            if j == -1:
                # Unterminated docstring: should not happen in valid
                # Python, but bail safely.
                break
            i = j + 3
            continue
        out.append(cleaned[i])
        i += 1
    final = "".join(out)

    assert banned not in final, (
        f"Banned fabricated phrase {banned!r} found in compose/intake.py "
        f"outside comments/docstrings. Default seeds must use empty "
        f"arrays + widget-hide-on-empty pattern."
    )


# ---------------------------------------------------------------------------
# (2) Widgets must hide section chrome on empty inputs
# ---------------------------------------------------------------------------

# Widgets that render on home-page layouts. Each MUST wrap its outer
# <section> element in a guard that suppresses output when items is
# empty, so a customer with no real data does not ship a blank colored
# band on the home page. Pattern matched after this audit:
# `<% if (_xxx.length > 0) { %>` before the <section> tag.
HOME_FACING_WIDGETS_HIDE_ON_EMPTY = [
    "offerings/reviews-grid.ejs",
    "offerings/staff-card-grid.ejs",
    "offerings/instructor-grid.ejs",
    "offerings/trust-badges-row.ejs",
    "offerings/service-area-list.ejs",
    "offerings/impact-stats-row.ejs",
    "offerings/credentials-block.ejs",
    "testimonials/stack.ejs",
]


@pytest.mark.parametrize("widget_rel", HOME_FACING_WIDGETS_HIDE_ON_EMPTY)
def test_widget_hides_section_chrome_when_empty(widget_rel: str) -> None:
    """Widget source must guard its outer <section> with a length check.

    The contract: looking at the source from top to bottom, an
    ``<% if (... .length > 0) { %>`` (or equivalent length check) must
    appear before the first ``<section`` opener. Without that, the outer
    chrome (background color + padding) renders even when there is no
    data to show, leaving an awkward empty band on the page.
    """
    p = WIDGETS_DIR / widget_rel
    assert p.exists(), f"Expected widget file at {p}"
    src = p.read_text(encoding="utf-8")
    # Strip out EJS code blocks `<% ... %>` (which can contain comments
    # or human-readable docstrings mentioning "<section>") so we only
    # find a real opening <section ...> tag in the rendered HTML stream.
    import re as _re
    src_no_ejs = _re.sub(r"<%[\s\S]*?%>", "", src)
    # Match an actual <section opening tag (followed by space, >, or
    # newline); not "<section" inside the word "subsection" or similar.
    m = _re.search(r"<section[\s>]", src_no_ejs)
    assert m is not None, f"{widget_rel} has no rendered <section> element"
    # Now find the same offset in the original source by searching for
    # the same `<section` substring that comes AFTER the last EJS block.
    # Easier approach: in the original source, find the LAST `<% if (`
    # length-check guard before a `<section` token in the HTML stream.
    # Equivalent contract: any of the seven widgets either guards
    # everything with a length check or renders nothing.
    assert ".length > 0" in src or ".length>0" in src, (
        f"{widget_rel}: must contain a `.length > 0` guard somewhere "
        f"so the widget renders nothing when items is empty. See "
        f"compose/widgets/offerings/service-area-list.ejs for the "
        f"reference pattern."
    )
    # Stronger contract: the LAST `<section` opener (in the rendered
    # HTML stream) must be preceded by a `<% if (` block in the same
    # file that references `.length` (the most recent open guard
    # applies). We approximate by checking that a `<% if (` containing
    # `length` appears earlier in the file than the FIRST real
    # `<section` opener.
    # Find first `<section ` in original source that is NOT inside a
    # comment line. EJS files don't have JS comments around <section>;
    # the only false-positive source is HTML text like <section> inside
    # an EJS comment block, which we already stripped above. Locate the
    # first real `<section` after stripping EJS:
    real_section_idx = src.find("<section ")
    if real_section_idx == -1:
        real_section_idx = src.find("<section>")
    # Walk backwards: if we previously saw an EJS comment block that
    # mentions the literal string "<section", skip those and find the
    # first occurrence outside `<% ... %>`.
    # Build a list of (start, end) for each EJS code block; then pick
    # the first `<section` whose offset is NOT inside any EJS block.
    ejs_ranges = [(m.start(), m.end()) for m in _re.finditer(r"<%[\s\S]*?%>", src)]

    def _in_ejs(idx: int) -> bool:
        return any(s <= idx < e for s, e in ejs_ranges)

    # Iterate every `<section` occurrence and pick the first one that
    # is OUTSIDE EJS code blocks.
    real_idx = -1
    for sm in _re.finditer(r"<section[\s>]", src):
        if not _in_ejs(sm.start()):
            real_idx = sm.start()
            break
    assert real_idx != -1, (
        f"{widget_rel}: no rendered <section> tag found outside EJS "
        f"comment blocks. (The widget must either render a section "
        f"or be a non-section widget; this list only contains "
        f"section-rendering widgets.)"
    )
    prefix = src[:real_idx]
    assert ".length > 0" in prefix or ".length>0" in prefix, (
        f"{widget_rel}: outer <section> must be wrapped in an "
        f"`<% if (... .length > 0) { '{' } %>` guard so the widget "
        f"renders nothing when items is empty. See "
        f"compose/widgets/offerings/service-area-list.ejs for the "
        f"reference pattern."
    )


# ---------------------------------------------------------------------------
# (3) scaffold_spec seeds for fabrication-prone tables ship empty
# ---------------------------------------------------------------------------

# Models whose seeded rows would be rendered as identity claims (named
# people, named reviewers, named team members) on a public surface.
# scaffold_spec must seed these with [] so the widget renders nothing
# until the customer adds real data via /admin.
EMPTY_SEEDED_TABLES = ["reviews", "staff", "instructors", "schedule_slots"]


@pytest.mark.parametrize("table", EMPTY_SEEDED_TABLES)
def test_scaffold_spec_seeds_empty_for_fabrication_prone_tables(table: str) -> None:
    """Run scaffold_spec for a representative archetype and verify the
    fabrication-prone table seeds to []."""
    from compose import intake

    archetype_for_table = {
        "reviews": "tradesperson-contractor",
        "staff": "salon-spa",
        "instructors": "fitness-studio",
        "schedule_slots": "fitness-studio",
    }
    archetype = archetype_for_table[table]
    answers = {
        "app_name": "audit-test",
        "archetype": archetype,
        "site_name": "Audit Test",
        "admin_email": "admin@example.com",
    }
    spec = intake.scaffold_spec(answers)
    seed = spec.get("seed_content", {})
    # The scaffold filters seed by the archetype's declared models, so
    # the table is present only when the archetype actually declares it
    # (otherwise it is correctly absent). When present, it must be the
    # empty list.
    if table in seed:
        assert seed[table] == [], (
            f"{archetype} seed for {table} must be [] (got "
            f"{seed[table]!r}). Fabricated rows ship as if they were "
            f"real customer data; the scaffold must not introduce "
            f"invented people or fake reviews."
        )
