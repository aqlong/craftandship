"""Two UX regressions surfaced in a browser review:

1. Outline (ghost) submit buttons rendered as maroon-on-maroon.
   Claude emits `style="border-color: var(--color-primary); color:
   var(--color-primary);"` for Publish/Unpublish-style toggle buttons
  , intending a transparent-bg pill with primary-colored text +
   border. The existing blanket `button[type="submit"] { background:
   primary; color: #fff }` safety-net CSS fills the bg with primary
   too, while the inline `color:` beats `#fff` on specificity. Result:
   primary text on primary bg → invisible.

2. `views/admin/settings.ejs` "Back to Dashboard" link pointed at
   `/admin`, which isn't a real route. Fix: re-target to
   `/dashboard` in the shipped canonical template.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


# ---------------------------------------------------------------------------
# OUTLINE_BUTTON_CONTRAST
# ---------------------------------------------------------------------------


def test_outline_button_contrast_override_injected_into_style_css():
    """After _fix_delete_button_styling runs, style.css must carry an
    OUTLINE_BUTTON_CONTRAST block that undoes the safety-net bg fill
    for buttons whose inline style declares both `border-color` and
    `color:`. Without it, Publish/Unpublish-style ghost buttons
    render maroon-on-maroon (invisible)."""
    claude_style = (
        "/* Button safety net */\n"
        "button[type=\"submit\"] {\n"
        "  background-color: var(--color-primary, #6b1519);\n"
        "  color: #fff;\n"
        "}\n"
    )
    files = [("public/style.css", claude_style)]
    out = dict(build._fix_delete_button_styling(files))
    got = out["public/style.css"]
    assert "OUTLINE_BUTTON_CONTRAST" in got
    # The targeting selector matches the outline-button pattern exactly.
    assert 'button[type="submit"][style*="border-color"][style*="color:"]' in got
    assert "background: transparent !important" in got
    # Hover state restores solid primary + white text so the button
    # reads as interactive.
    assert "background: var(--color-primary" in got


def test_outline_button_rule_is_idempotent():
    """Running _fix_delete_button_styling twice must NOT duplicate the
    OUTLINE_BUTTON_CONTRAST block."""
    claude_style = (
        "button[type=\"submit\"] {\n"
        "  background-color: var(--color-primary);\n"
        "  color: #fff;\n"
        "}\n"
    )
    files = [("public/style.css", claude_style)]
    once = dict(build._fix_delete_button_styling(files))
    twice = dict(build._fix_delete_button_styling(list(once.items())))
    assert once["public/style.css"].count("OUTLINE_BUTTON_CONTRAST") == 1
    assert twice["public/style.css"].count("OUTLINE_BUTTON_CONTRAST") == 1


def test_outline_button_selector_is_order_agnostic():
    """The CSS `[style*=...]` attribute selectors match substring
    presence regardless of declaration order. Verify the injected
    rule will still target buttons where the inline style lists
    `color:` BEFORE `border-color:` (Claude sometimes does this)."""
    claude_style = 'button[type="submit"] { background-color: var(--color-primary); color: #fff; }\n'
    out = dict(build._fix_delete_button_styling(
        [("public/style.css", claude_style)]
    ))
    got = out["public/style.css"]
    # The selector lists BOTH attribute conditions. CSS guarantees the
    # match is independent of the order in which the HTML attribute
    # contains them.
    assert 'button[type="submit"][style*="border-color"][style*="color:"]' in got
    # Sanity: both attribute conditions present as an AND (not an
    # alternation, which would be over-broad).
    import re as _re
    pattern = r'\[style\*="border-color"\]\[style\*="color:"\]'
    assert _re.search(pattern, got), (
        "selector must be an attribute AND, not separate rules"
    )


def test_outline_button_rule_independent_of_color_var_used():
    """The injected rule targets the STRUCTURAL outline pattern
    (inline `border-color` + `color`), not a specific CSS variable.
    So a button with `--color-accent` instead of `--color-primary`
    is ALSO targeted, no var-specific hardcoding."""
    claude_style = 'button[type="submit"] { background-color: var(--color-primary); color: #fff; }\n'
    out = dict(build._fix_delete_button_styling(
        [("public/style.css", claude_style)]
    ))
    got = out["public/style.css"]
    # No var-specific `accent` / `primary` mentions in the SELECTOR
    # (the hover bg is fine since we want it to resolve to primary).
    idx = got.find("OUTLINE_BUTTON_CONTRAST")
    block = got[idx:idx + 800]
    selector_line = block.split("{")[0]
    assert "--color-primary" not in selector_line
    assert "--color-accent" not in selector_line


def test_no_outline_override_when_no_submit_rule_present():
    """If Claude's style.css has no `button[type="submit"]` rule at
    all, the OUTLINE_BUTTON_CONTRAST block is NOT appended, there's
    no safety-net to undo. (Other processors in the same function may
    still touch the file, so we assert the scoped block's absence
    rather than file-byte equality.)"""
    claude_style = "/* just some other styles */\n.foo { color: red; }\n"
    files = [("public/style.css", claude_style)]
    out = dict(build._fix_delete_button_styling(files))
    assert "OUTLINE_BUTTON_CONTRAST" not in out["public/style.css"]
    # The original CSS content is still present (not clobbered).
    assert ".foo { color: red; }" in out["public/style.css"]


# ---------------------------------------------------------------------------
# Settings back-link
# ---------------------------------------------------------------------------


def test_settings_template_back_link_targets_dashboard_not_admin():
    """cli/templates/views_admin_settings.ejs must point "Back to
    Dashboard" at `/dashboard`, `/admin` is not a real route in
    generated apps (the admin router is mounted at `/admin/<slug>` but
    there's no `/admin` index handler). This surfaced when
    a reviewer hit "Back to Dashboard" from /admin/settings and
    landed on a 404."""
    template_path = (
        Path(__file__).parent.parent / "cli" / "templates" /
        "views_admin_settings.ejs"
    )
    content = template_path.read_text()
    # Positive: the correct target is present.
    assert 'href="/dashboard"' in content, (
        "settings template must link 'Back to Dashboard' at /dashboard"
    )
    # Negative: the broken link is gone.
    assert 'href="/admin"' not in content, (
        "stale href=\"/admin\" still in settings template"
    )
